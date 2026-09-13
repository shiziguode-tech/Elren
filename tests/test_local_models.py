from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from deepdesk.deepseek import DeepSeekClient
from deepdesk.engine import AgentEngine
from deepdesk.local_models import (
    LocalModelRegistry,
    LocalRuntimeCandidate,
    _ollama_app_context_length,
)
from deepdesk.vision_runtime import VisionRuntime


@pytest.mark.asyncio
async def test_ollama_discovery_uses_declared_capabilities_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "local-tool:latest"}]},
            )
        if request.url.path == "/api/show":
            assert json.loads(request.content)["model"] == "local-tool:latest"
            return httpx.Response(
                200,
                json={
                    "capabilities": ["completion", "tools", "thinking"],
                    "model_info": {"family.context_length": 32_768},
                },
            )
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "local-tool:latest",
                            "context_length": 16_384,
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "ollama",
                "Ollama",
                "http://127.0.0.1:11434/api/tags",
                "http://127.0.0.1:11434/v1",
                "ollama",
            ),
        ),
        transport=httpx.MockTransport(handler),
        ollama_context_reader=lambda: None,
    )
    models = await registry.discover()
    assert models == [
        {
            "selector": "local-ollama:local-tool:latest",
            "provider": "local",
            "model": "local-tool:latest",
            "base_url": "http://127.0.0.1:11434/v1",
            "source": "local-ollama",
            "runtime": "Ollama",
            "capabilities": ["completion", "thinking", "tools"],
            "reasoning_levels": [],
            "supports_thinking": True,
            "supports_vision": False,
            "supports_tools": True,
            "supports_documents": True,
            "context_window_tokens": 16_384,
            "context_window_source": "ollama_active_allocation",
            "maximum_context_window_tokens": 32_768,
        }
    ]
    assert registry.status()["vision_model_count"] == 0


@pytest.mark.asyncio
async def test_openai_compatible_runtime_preserves_explicit_reasoning_levels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "declared-vision-reasoner",
                        "capabilities": {
                            "vision": True,
                            "tools": True,
                            "reasoning_efforts": ["low", "high"],
                        },
                        "supported_reasoning_efforts": ["low", "high"],
                        "loaded_context_length": 32_768,
                        "context_length": 65_536,
                    }
                ]
            },
        )

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "lm-studio",
                "LM Studio",
                "http://127.0.0.1:1234/v1/models",
                "http://127.0.0.1:1234/v1",
            ),
        ),
        transport=httpx.MockTransport(handler),
    )
    model = (await registry.discover())[0]
    assert model["supports_vision"] is True
    assert model["reasoning_levels"] == ["low", "high"]
    assert model["context_window_tokens"] == 32_768
    assert model["context_window_source"] == "local_runtime_loaded_instance"
    assert model["maximum_context_window_tokens"] == 65_536


@pytest.mark.asyncio
async def test_failed_reprobe_removes_stale_local_models() -> None:
    healthy = True

    def handler(_request: httpx.Request) -> httpx.Response:
        if healthy:
            return httpx.Response(200, json={"data": [{"id": "present-now"}]})
        raise httpx.ConnectError("offline")

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "test",
                "Test",
                "http://127.0.0.1:9999/v1/models",
                "http://127.0.0.1:9999/v1",
            ),
        ),
        transport=httpx.MockTransport(handler),
    )
    assert len(await registry.discover()) == 1
    healthy = False
    assert await registry.discover() == []
    assert registry.status()["model_count"] == 0


@pytest.mark.asyncio
async def test_one_unexpected_runtime_failure_does_not_hide_healthy_local_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken/models":
            raise RuntimeError("runtime adapter crashed")
        if request.url.path == "/healthy/models":
            return httpx.Response(200, json={"data": [{"id": "healthy-model"}]})
        raise AssertionError(request.url)

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "broken",
                "Broken runtime",
                "http://127.0.0.1:9901/broken/models",
                "http://127.0.0.1:9901/v1",
            ),
            LocalRuntimeCandidate(
                "healthy",
                "Healthy runtime",
                "http://127.0.0.1:9902/healthy/models",
                "http://127.0.0.1:9902/v1",
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    models = await registry.discover()
    status = registry.status()

    assert [model["selector"] for model in models] == ["local-healthy:healthy-model"]
    assert status["runtimes"][0]["ready"] is False
    assert status["runtimes"][0]["error"] == "RuntimeError"
    assert status["runtimes"][1]["ready"] is True


def test_local_model_is_available_everywhere_without_invented_reasoning() -> None:
    client = DeepSeekClient(
        "https://example.invalid/v1",
        "deepseek-v4-flash",
        "cloud-key",
        "",
    )
    client.set_local_models(
        [
            {
                "selector": "local-ollama:gemma-local",
                "model": "gemma-local",
                "base_url": "http://127.0.0.1:11434/v1",
                "source": "local-ollama",
                "runtime": "Ollama",
                "capabilities": ["completion", "thinking", "tools"],
                "reasoning_levels": [],
                "context_window_tokens": 32_768,
            }
        ]
    )
    option = next(
        item
        for item in client.model_options()
        if item["selector"] == "local-ollama:gemma-local"
    )
    assert option["provider"] == "local"
    assert option["supports_tools"] is True
    assert option["reasoning"] == {
        "supported": False,
        "levels": [],
        "control": "none",
        "verification": "local_runtime_did_not_declare_levels",
    }
    client.set_default_model_preference(option["selector"])
    assert client.default_model_preference == option["selector"]
    # Installing a local model must not unexpectedly steal the generic Auto route.
    assert client.select_automatic_model(False) != option["selector"]


def test_only_declared_local_reasoning_is_forwarded() -> None:
    client = DeepSeekClient("https://example.invalid/v1", "fallback", "", "")
    client.set_local_models(
        [
            {
                "selector": "local-test:reasoner",
                "model": "reasoner",
                "base_url": "http://127.0.0.1:9999/v1",
                "capabilities": ["thinking"],
                "reasoning_levels": ["low", "high"],
            }
        ]
    )
    endpoint = client._endpoint_for("local-test:reasoner")
    model_token = client.bind_task_model(endpoint.selector)
    effort_token = client.bind_task_reasoning_effort("high")
    try:
        payload = client._chat_payload([], [], endpoint=endpoint)
    finally:
        client.reset_task_reasoning_effort(effort_token)
        client.reset_task_model(model_token)
    assert payload["reasoning_effort"] == "high"


def test_removed_local_route_is_never_reinterpreted_as_deepseek() -> None:
    client = DeepSeekClient(
        "https://api.deepseek.example/v1",
        "deepseek-v4-flash",
        "deepseek-key",
        "",
    )
    entry = {
        "selector": "local-ollama:temporary",
        "model": "temporary",
        "base_url": "http://127.0.0.1:11434/v1",
        "capabilities": ["tools"],
    }
    client.set_local_models([entry])
    client.set_local_models([])
    endpoint = client._endpoint_for(entry["selector"])
    assert endpoint.provider == "local"
    assert endpoint.base_url == "http://127.0.0.1:11434/v1"
    assert entry["selector"] not in {
        item["selector"] for item in client.model_options()
    }
    assert client.has_model_credentials(entry["selector"]) is False


def test_vision_runtime_requires_explicit_local_vision_declaration(tmp_path) -> None:
    runtime = VisionRuntime("", "", "", True, "", 1, tmp_path)
    runtime.set_local_model_capabilities(
        [
            {
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "text-only",
                "supports_vision": False,
            },
            {
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "vision-declared",
                "supports_vision": True,
            },
        ]
    )
    assert runtime._select_model(
        ["text-only", "vision-declared"],
        base_url="http://127.0.0.1:11434/v1",
    ) == "vision-declared"
    assert runtime._select_model(
        ["llava-by-name-only"],
        base_url="http://127.0.0.1:11434/v1",
    ) is None


def test_ollama_app_context_setting_is_read_without_hardcoded_user_path(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "Ollama" / "db.sqlite"
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE settings (id INTEGER PRIMARY KEY, context_length INTEGER)"
    )
    connection.execute("INSERT INTO settings VALUES (1, 65536)")
    connection.commit()
    connection.close()
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert _ollama_app_context_length() == 65_536


def test_engine_respects_small_real_local_context_instead_of_cloud_fallback() -> None:
    class Client:
        @staticmethod
        def context_window_tokens() -> int:
            return 4_096

    assert AgentEngine._context_window_for_client(Client()) == 4_096


def test_required_html_artifact_needs_a_real_write_tool_call() -> None:
    extensions = AgentEngine._requested_artifact_extensions(
        "请帮我制作一个 HTML 来介绍今天的科技新闻"
    )
    assert extensions == {".html"}
    assert AgentEngine._artifact_materialized([], extensions) is False
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "filesystem",
                        "arguments": json.dumps(
                            {
                                "action": "write",
                                "path": "outputs/today-tech.html",
                                "content": "<!doctype html>",
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps(
                {"ok": True, "result": {"path": "outputs/today-tech.html"}},
                ensure_ascii=False,
            ),
        },
    ]
    assert AgentEngine._artifact_materialized(messages, extensions) is True
