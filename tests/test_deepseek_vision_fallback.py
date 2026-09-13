from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekClient
from deepdesk.egress_region import EgressRegion
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.model_selection import resolve_model_selection
from deepdesk.models import AgentProfile, ApprovalPolicy, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.vision import VisionTool
from deepdesk.vision_runtime import DEEPSEEK_VISION_MODEL, VisionEndpoint, VisionRuntime


class OCR:
    def __init__(self, text=""):
        self.text = text

    async def recognize(self, *_args):
        return {"text": self.text, "source": "local", "model": "local"}


def runtime(tmp_path, keys=lambda: ("primary-test", "backup-test")):
    return VisionRuntime(
        "https://google.test/v1", "google-test", "google-vision", False, "", 1,
        tmp_path, relay_api_key="relay-test", deepseek_keys=keys,
    )


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(**{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", factory)

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr("deepdesk.semantic_vision.asyncio.sleep", no_sleep)


def success(text="Recognized 7392"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


async def run_tool(tmp_path, rt, text="", **arguments):
    image = tmp_path / "fixture.png"
    image.write_bytes(b"synthetic test; HTTP is completely mocked")
    tool = VisionTool(rt, tmp_path, windows_ocr=OCR(text), paddle_ocr=OCR(text))
    return await tool.execute(
        {"image_path": str(image), "question": "read OCR", "mode": "semantic", **arguments},
        ToolContext(task_id="isolated-vision-test", workspace=str(tmp_path), egress_country="CN"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 402, 403, 404, 429, 500, "transport", "empty", "invalid", "length", "refusal", "no_text"])
async def test_google_failure_then_official_primary_then_backup(tmp_path, monkeypatch, failure):
    rt = runtime(tmp_path)
    seen = []

    async def ready():
        return VisionEndpoint(rt.configured_url, rt.configured_model, "configured", api_key=rt.api_key)

    monkeypatch.setattr(rt, "ensure_ready", ready)

    def handler(request):
        key = request.headers.get("authorization", "")
        seen.append((request.url.host, key))
        if request.url.host == "google.test":
            if failure == "transport":
                raise httpx.ConnectError("unavailable")
            if failure == "empty":
                return success("")
            if failure == "no_text":
                return success("NO_READABLE_TEXT")
            if failure == "invalid":
                return httpx.Response(200, content=b"not json")
            if failure in {"length", "refusal"}:
                return httpx.Response(200, json={"choices": [{"finish_reason": failure,
                    "message": {"content": "partial", "refusal": failure == "refusal"}}]})
            return httpx.Response(failure)
        assert request.url.host == "api.deepseek.com"
        payload = json.loads(request.content)
        assert payload["model"] == DEEPSEEK_VISION_MODEL
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        if key == "Bearer primary-test":
            return httpx.Response(401, json={"error": {"message": "primary-test secret must not leak"}})
        assert key == "Bearer backup-test"
        return success()

    mock_http(monkeypatch, handler)
    result = await run_tool(tmp_path, rt)
    assert result["source"] == "deepseek-official-backup"
    assert result["semantic_verified"] is True
    assert result["cloud_used"] is True
    assert [host for host, _ in seen][-2:] == ["api.deepseek.com"] * 2
    assert "primary-test" not in json.dumps(result)
    assert result["provider_chain"][-3:] == ["google-vision-fallback:failed", "deepseek-official-primary:failed", "deepseek-official-backup:ok"]


@pytest.mark.asyncio
async def test_discovery_prefers_google_then_deepseek_before_relay(tmp_path, monkeypatch):
    rt = runtime(tmp_path)

    async def models(_url):
        return [rt.configured_model]

    monkeypatch.setattr(rt, "_models", models)
    assert (await rt.discover()).source == "configured"

    async def missing(_url):
        return None

    monkeypatch.setattr(rt, "_models", missing)
    assert (await rt.discover()).source == "deepseek-official-primary"


@pytest.mark.asyncio
async def test_discovery_failure_still_attempts_official_and_then_relay(tmp_path, monkeypatch):
    rt = runtime(tmp_path)

    async def missing():
        raise RuntimeError("Google discovery unavailable")

    monkeypatch.setattr(rt, "ensure_ready", missing)
    keys = []

    def handler(request):
        if request.url.host == "api.deepseek.com":
            keys.append(request.headers["authorization"])
            return success("NO_READABLE_TEXT")
        assert request.url.host == "api.aicodemirror.ai"
        assert request.headers["x-goog-api-key"] == "relay-test"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Other method text"}]}}]})

    mock_http(monkeypatch, handler)
    result = await run_tool(tmp_path, rt)
    assert keys == ["Bearer primary-test", "Bearer backup-test"]
    assert result["source"] == "aicodemirror-gemini"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_local", [False, True])
async def test_exhaustion_is_explicit_not_fake_success(tmp_path, monkeypatch, has_local):
    rt = runtime(tmp_path)

    async def ready():
        return rt.deepseek_endpoints()[0]

    monkeypatch.setattr(rt, "ensure_ready", ready)
    mock_http(monkeypatch, lambda _request: httpx.Response(403))
    if not has_local:
        with pytest.raises(RuntimeError, match="All vision recognition methods failed"):
            await run_tool(tmp_path, rt)
    else:
        result = await run_tool(tmp_path, rt, "local evidence")
        assert result["semantic_verified"] is False
        assert result["semantic_limited"] is True
        assert result["cloud_used"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,sensitive", [(True, False), (False, True)])
async def test_privacy_gate_prevents_every_cloud_route(tmp_path, monkeypatch, disabled, sensitive):
    rt = runtime(tmp_path)

    async def forbidden():
        pytest.fail("Privacy gate must run before discovery")

    monkeypatch.setattr(rt, "ensure_ready", forbidden)
    result = await run_tool(tmp_path, rt, "Bearer protected-token-123456789" if sensitive else "", cloud_fallback=not disabled)
    assert result["cloud_used"] is False
    if sensitive:
        assert result["cloud_blocked"] is True


@pytest.mark.asyncio
async def test_cancel_never_advances_to_backup(tmp_path, monkeypatch):
    rt = runtime(tmp_path)

    async def ready():
        return rt.deepseek_endpoints()[0]

    monkeypatch.setattr(rt, "ensure_ready", ready)
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        raise asyncio.CancelledError

    mock_http(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        await run_tool(tmp_path, rt)
    assert seen == ["Bearer primary-test"]


def test_keys_are_live_deduplicated_and_not_in_repr(tmp_path):
    current = ["old", "old"]
    rt = runtime(tmp_path, lambda: tuple(current))
    assert len(rt.deepseek_endpoints()) == 1
    current[:] = ["", "rotated-backup"]
    endpoint = rt.deepseek_endpoints()[0]
    assert endpoint.source == "deepseek-official-backup"
    assert endpoint.api_key == "rotated-backup"
    assert "rotated-backup" not in repr(endpoint)
    assert endpoint.base_url == "https://api.deepseek.com"


@pytest.mark.asyncio
async def test_lazy_status_displays_actual_configured_provider_without_probe(tmp_path, monkeypatch):
    current = ["primary-test", ""]
    rt = runtime(tmp_path, lambda: tuple(current))

    async def forbidden():
        pytest.fail("Status must not probe a provider on the startup path")

    monkeypatch.setattr(rt, "discover", forbidden)
    # Google is still the default when both credentials exist.
    assert (await rt.status(probe=False))["model"] == "google-vision"
    rt.set_api_keys("", "relay-test")
    state = await rt.status(probe=False)
    assert state["configured"] and not state["ready"] and not state["probed"]
    assert state["base_url"] == "https://api.deepseek.com"
    assert state["model"] == DEEPSEEK_VISION_MODEL
    assert "primary-test" not in json.dumps(state)
    current[:] = ["", ""]
    assert (await rt.status(probe=False))["model"] == rt.relay_model
    rt.set_api_keys("", "")
    state = await rt.status(probe=False)
    assert not state["configured"] and not state["ready"]
    assert state["base_url"] == state["model"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("new_keys", [("rotated-test", ""), ("", "backup-test"), ("", "")])
async def test_status_revokes_cached_official_key_before_next_request(tmp_path, new_keys):
    current = ["primary-test", ""]
    rt = runtime(tmp_path, lambda: tuple(current))
    rt.set_api_keys("", "")
    rt.endpoint = rt.deepseek_endpoints()[0]
    rt.probed = True
    assert (await rt.status(probe=False))["ready"]
    current[:] = new_keys
    state = await rt.status(probe=False)
    assert not state["ready"] and not state["probed"]
    assert state["configured"] == bool(any(new_keys))
    assert rt.endpoint is None
    assert all(key not in json.dumps(state) for key in new_keys if key)


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_chat_catalog_and_payload_keep_image_and_effort(effort):
    client = DeepSeekClient("https://api.deepseek.com", DEEPSEEK_VISION_MODEL, "test-key", "test-backup")
    option = next(item for item in client.model_options() if item["selector"] == DEEPSEEK_VISION_MODEL)
    assert option["supports_vision"] and option["supports_tools"]
    assert option["reasoning"]["levels"] == ["low", "high", "max"]
    assert resolve_model_selection(DEEPSEEK_VISION_MODEL, client.model_options()) == DEEPSEEK_VISION_MODEL
    token = client.bind_task_reasoning_effort(effort)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]}]
    try:
        payload = client._chat_payload(messages, [])
    finally:
        client.reset_task_reasoning_effort(token)
    assert payload["model"] == DEEPSEEK_VISION_MODEL
    assert payload["reasoning_effort"] == effort
    assert payload["messages"] == messages
    assert "tools" not in payload
    assert client.has_model_credentials(DEEPSEEK_VISION_MODEL)


def test_no_keys_no_selectable_vision_model():
    assert DeepSeekClient("https://api.deepseek.com", "deepseek-v4-flash", "", "").model_options() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["local_ocr", "markdown", "semantic"])
async def test_selected_vision_model_precedes_google_and_local_result(tmp_path, monkeypatch, mode):
    rt = runtime(tmp_path)

    async def forbidden():
        pytest.fail("Google discovery must not delay a selected vision model")

    monkeypatch.setattr(rt, "ensure_ready", forbidden)
    requests = []

    def handler(request):
        requests.append((request.url.host, request.headers.get("authorization")))
        return success("Official OCR preferred")

    mock_http(monkeypatch, handler)
    image = tmp_path / "priority.png"
    image.write_bytes(b"mock fixture")
    client = DeepSeekClient("https://api.deepseek.com", "deepseek-v4-flash", "test-key", "")
    tool = VisionTool(rt, tmp_path, windows_ocr=OCR("usable local text"), paddle_ocr=OCR("usable local text"),
                      active_model=lambda: client.effective_model)
    token = client.bind_task_model(DEEPSEEK_VISION_MODEL)
    try:
        result = await tool.execute({"image_path": str(image), "question": "Read text", "mode": mode},
                                   ToolContext(task_id="priority", workspace=str(tmp_path)))
    finally:
        client.reset_task_model(token)
    assert requests == [("api.deepseek.com", "Bearer primary-test")]
    assert result["source"] == "deepseek-official-primary"


@pytest.mark.asyncio
async def test_selected_vision_uses_backup_before_google_and_other_methods(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    order = []

    async def ready():
        order.append("discover-google")
        return VisionEndpoint(rt.configured_url, rt.configured_model, "configured", api_key=rt.api_key)

    monkeypatch.setattr(rt, "ensure_ready", ready)

    def handler(request):
        if request.url.host == "api.deepseek.com":
            order.append(request.headers["authorization"])
            return httpx.Response(403)
        order.append("google-request")
        return success("Google fallback")

    mock_http(monkeypatch, handler)
    image = tmp_path / "priority-fallback.png"
    image.write_bytes(b"mock fixture")
    tool = VisionTool(rt, tmp_path, windows_ocr=OCR(), paddle_ocr=OCR(), active_model=lambda: DEEPSEEK_VISION_MODEL)
    result = await tool.execute({"image_path": str(image), "question": "Read text"},
                               ToolContext(task_id="priority-failure", workspace=str(tmp_path)))
    assert order == ["Bearer primary-test", "Bearer backup-test", "discover-google", "google-request"]
    assert result["source"] == "configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,sensitive", [(True, False), (False, True)])
async def test_selected_vision_does_not_override_privacy(tmp_path, monkeypatch, disabled, sensitive):
    rt = runtime(tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("No cloud request permitted")

    mock_http(monkeypatch, forbidden)
    monkeypatch.setattr(rt, "deepseek_endpoints", forbidden)
    image = tmp_path / "private.png"
    image.write_bytes(b"mock fixture")
    tool = VisionTool(rt, tmp_path, windows_ocr=OCR("Bearer protected-token-123456789" if sensitive else ""),
                      paddle_ocr=OCR(), active_model=lambda: DEEPSEEK_VISION_MODEL)
    result = await tool.execute({"image_path": str(image), "question": "Check layout", "cloud_fallback": not disabled},
                               ToolContext(task_id="priority-private", workspace=str(tmp_path)))
    assert result["cloud_used"] is False


@pytest.mark.asyncio
async def test_concurrent_conversations_keep_independent_ocr_preferences(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    client = DeepSeekClient("https://api.deepseek.com", DEEPSEEK_VISION_MODEL, "test-key", "")
    barrier = asyncio.Event()
    starts = 0

    class InterleavedOCR:
        async def recognize(self, *_args):
            nonlocal starts
            starts += 1
            if starts == 2:
                barrier.set()
            await barrier.wait()
            return {"text": "local evidence"}

    async def ready():
        assert client.effective_model == "deepseek-v4-pro"
        return VisionEndpoint(rt.configured_url, rt.configured_model, "configured", api_key=rt.api_key)

    monkeypatch.setattr(rt, "ensure_ready", ready)
    mock_http(monkeypatch, lambda request: success(request.url.host))
    image = tmp_path / "parallel.png"
    image.write_bytes(b"mock fixture")
    tool = VisionTool(rt, tmp_path, windows_ocr=InterleavedOCR(), paddle_ocr=OCR(), active_model=lambda: client.effective_model)

    async def invoke(selector):
        token = client.bind_task_model(selector)
        try:
            return await tool.execute({"image_path": str(image), "question": "Check layout", "mode": "semantic"},
                                      ToolContext(task_id=selector, workspace=str(tmp_path)))
        finally:
            client.reset_task_model(token)

    selected, other = await asyncio.gather(invoke(DEEPSEEK_VISION_MODEL), invoke("deepseek-v4-pro"))
    assert selected["source"] == "deepseek-official-primary"
    assert other["source"] == "configured"
    assert client.effective_model == DEEPSEEK_VISION_MODEL


@pytest.mark.asyncio
async def test_real_task_engine_propagates_selected_model_to_vision(tmp_path, monkeypatch):
    """Real TaskManager/Engine/client bindings/registry, with only providers mocked."""
    def no_network(*_args, **_kwargs):
        pytest.fail("This integration test must never make a real network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    async def region(_self):
        return EgressRegion("US")

    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", region)
    image = tmp_path / "engine-fixture.png"
    image.write_bytes(b"mock fixture")
    rt = runtime(tmp_path)
    requests = []

    def http(request):
        requests.append((request.url.host, request.headers.get("authorization")))
        return success("Engine vision result")

    mock_http(monkeypatch, http)

    async def no_google():
        pytest.fail("Selected vision task must not discover Google")

    monkeypatch.setattr(rt, "ensure_ready", no_google)

    class Provider(DeepSeekClient):
        def __init__(self):
            super().__init__("https://api.deepseek.com", "deepseek-v4-pro", "synthetic-key", "")
            self.calls = 0

        async def _request(self, endpoint, key, messages, tools, **_kwargs):
            assert endpoint.model == DEEPSEEK_VISION_MODEL
            self.calls += 1
            message = {"role": "assistant", "content": "Recognition finished."}
            if self.calls == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "synthetic-vision-call", "type": "function", "function": {
                        "name": "vision", "arguments": json.dumps({"image_path": str(image), "question": "Read the image text", "mode": "local_ocr"}),
                    },
                }]}
            return {"choices": [{"message": message}], "usage": {}, "model": endpoint.model}

    client = Provider()
    registry = PluginRegistry()
    registry.register(VisionTool(rt, tmp_path, windows_ocr=OCR("Local text must not win"), paddle_ocr=OCR(),
                                 active_model=lambda: client.effective_model))
    approvals = ApprovalGate()
    engine = AgentEngine(client=client, registry=registry, approvals=approvals,
                         human_actions=HumanActionGate(),
                         audit=AuditLog(tmp_path / "audit.jsonl"), workspace=str(tmp_path), max_steps=4,
                         emit=lambda task, kind, data: manager.emit(task, kind, data))
    manager = TaskManager(engine, approvals)
    task = manager.create("Read this supplied image using vision.", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL,
                          model_preference=DEEPSEEK_VISION_MODEL)
    await asyncio.wait_for(asyncio.shield(manager.background[task.id]), timeout=10)
    assert task.status == TaskStatus.COMPLETED, task.error
    assert requests == [("api.deepseek.com", "Bearer primary-test")]
    assert client.calls == 2
    assert client.effective_model == "deepseek-v4-pro"
