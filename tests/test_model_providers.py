import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from deepdesk.deepseek import (
    AICODEMIRROR_MODELS,
    DEEPSEEK_V4_MAX_OUTPUT_TOKENS,
    AnthropicError,
    DeepSeekClient,
    DeepSeekError,
    GeminiError,
    OpenAIError,
    XAIError,
)


def test_model_provider_endpoints_payloads_and_automatic_routing():
    client = DeepSeekClient(
        "https://api.deepseek.example",
        "deepseek-v4-flash",
        "deepseek-key",
        "",
    )
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-5-mini", "api_key": "oa"},
            {"provider": "anthropic", "model": "claude-opus-test", "api_key": "an"},
            {"provider": "google", "model": "gemini-test", "api_key": "gg"},
            {"provider": "xai", "model": "grok-4-test", "api_key": "xa"},
        ]
    )

    selectors = {item["selector"] for item in client.model_options()}
    assert selectors == {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
        "openai:gpt-5-mini",
        "anthropic:claude-opus-test",
        "google:gemini-test",
        "xai:grok-4-test",
    }
    assert client.select_automatic_model(False) in selectors
    assert client.select_automatic_model(True) == "anthropic:claude-opus-test"
    assert client._endpoint_for("deepseek-v4-flash").base_url == "https://api.deepseek.example"
    assert client._endpoint_for("openai:gpt-5-mini").base_url == "https://api.openai.com/v1"
    assert client._endpoint_for("anthropic:claude-opus-test").base_url == "https://api.anthropic.com/v1"
    assert client._endpoint_for("google:gemini-test").base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert client._endpoint_for("xai:grok-4-test").base_url == "https://api.x.ai/v1"

    token = client.bind_task_model("google:gemini-test")
    try:
        endpoint = client._endpoint_for(client.effective_selector)
        assert endpoint.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        payload = client._chat_payload(
            [{"role": "user", "content": "hello"}], [], endpoint=endpoint
        )
    finally:
        client.reset_task_model(token)
    assert payload["model"] == "gemini-test"
    assert payload["stream"] is True
    assert "thinking" not in payload
    assert "stream_options" not in payload
    assert "tools" not in payload


def test_automatic_routing_skips_a_known_cooling_down_model():
    client = DeepSeekClient(
        "https://api.deepseek.example",
        "deepseek-v4-flash",
        "",
        "",
    )
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-5.6-sol", "api_key": "oa"},
            {"provider": "google", "model": "gemini-3.5-flash", "api_key": "gg"},
        ]
    )
    assert client.select_automatic_model(True) == "openai:gpt-5.6-sol"

    client._temporarily_unavailable_models["openai:gpt-5.6-sol"] = (
        time.monotonic() + 60,
        "model_not_found",
    )

    assert client.select_automatic_model(True) == "google:gemini-3.5-flash"


def test_automatic_routing_fails_fast_when_every_cloud_route_is_cooling():
    client = DeepSeekClient(
        "https://api.deepseek.example", "deepseek-v4-flash", "", ""
    )
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-5.6-sol", "api_key": "oa"},
            {"provider": "google", "model": "gemini-3.5-flash", "api_key": "gg"},
        ]
    )
    deadline = time.monotonic() + 60
    for option in client.model_options():
        client._temporarily_unavailable_models[option["selector"]] = (
            deadline,
            "model_not_found",
        )

    with pytest.raises(DeepSeekError, match="均暂不可用"):
        client.select_automatic_model(True)


def test_changed_provider_key_clears_stale_route_cooldown():
    client = DeepSeekClient(
        "https://api.deepseek.example", "deepseek-v4-flash", "", ""
    )
    client.set_provider_models(
        [{"provider": "openai", "model": "gpt-5.6-sol", "api_key": "old-key"}]
    )
    selector = "openai:gpt-5.6-sol"
    client._temporarily_unavailable_models[selector] = (
        time.monotonic() + 60,
        "model_not_found",
    )

    client.set_provider_models(
        [{"provider": "openai", "model": "gpt-5.6-sol", "api_key": "new-key"}]
    )

    assert client.model_temporarily_unavailable(selector) is False


DEEPSEEK_V41_TEST_MODEL = "deepseek-v4.1-flash-expires-on-0910"


def test_removed_deepseek_v41_is_not_catalogued_or_routable():
    client = DeepSeekClient("https://configured-deepseek.test/custom/v1", "deepseek-v4-flash", "fake-primary", "fake-backup")
    assert DEEPSEEK_V41_TEST_MODEL not in {item["selector"] for item in client.model_options()}
    for selector in (DEEPSEEK_V41_TEST_MODEL, "deepseek:" + DEEPSEEK_V41_TEST_MODEL):
        with pytest.raises(DeepSeekError, match="removed"):
            client._endpoint_for(selector)
        assert not client.has_model_credentials(selector)
    assert client.model == client.default_model_preference == "deepseek-v4-flash"


@pytest.mark.parametrize("effort", ["high", "max"])
def test_removed_deepseek_v41_constructor_migrates_to_v4_flash(effort):
    client = DeepSeekClient("https://configured.test/v1", DEEPSEEK_V41_TEST_MODEL, "fake", "")
    token = client.bind_task_reasoning_effort(effort)
    try:
        payload = client._chat_payload([{"role": "user", "content": "Hello"}], [])
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == effort
        assert payload["max_tokens"] == DEEPSEEK_V4_MAX_OUTPUT_TOKENS
        recovery = client._chat_payload([{"role": "user", "content": "Hello"}], [], recovery=True)
        assert recovery["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in recovery
    finally:
        client.reset_task_reasoning_effort(token)


@pytest.mark.parametrize("limit", [2048, 8192])
def test_removed_model_migration_preserves_explicit_output_limit(limit):
    client = DeepSeekClient("https://configured.test/v1", DEEPSEEK_V41_TEST_MODEL, "fake", "", max_output_tokens=limit)
    assert client._chat_payload([{"role": "user", "content": "Hello"}], [])["max_tokens"] == limit


@pytest.mark.asyncio
async def test_removed_model_migration_preserves_primary_backup_without_network(monkeypatch):
    client = DeepSeekClient("https://configured.test/v1", DEEPSEEK_V41_TEST_MODEL, "fake-primary", "fake-backup")
    assert client.active_label == "主密钥"
    seen = []

    async def request(endpoint, key, messages, tools, *, recovery=False):
        seen.append((endpoint.model, endpoint.base_url, key))
        if key == "fake-primary":
            req = httpx.Request("POST", endpoint.base_url + "/chat/completions")
            raise httpx.HTTPStatusError("Synthetic primary rejected", request=req, response=httpx.Response(401, request=req))
        return {"choices": [{"message": {"role": "assistant", "content": "Synthetic success"}, "finish_reason": "stop"}], "usage": {}, "model": endpoint.model}

    monkeypatch.setattr(client, "_request", request)
    reply = await client.chat([{"role": "user", "content": "Hello"}], [])
    assert seen == [("deepseek-v4-flash", "https://configured.test/v1", "fake-primary"), ("deepseek-v4-flash", "https://configured.test/v1", "fake-backup")]
    assert reply.active_key == client.active_label == "备用密钥"


def test_removed_deepseek_v41_saved_default_migrates_and_static_options_are_gone():
    from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch
    from deepdesk.config import Settings
    from deepdesk.model_capabilities import reasoning_capability

    assert RuntimeSettings().model == "deepseek-v4-flash"
    saved = RuntimeSettings(model=DEEPSEEK_V41_TEST_MODEL, reasoning_effort="max")
    assert RuntimeSettings.model_validate_json(saved.model_dump_json()).model == "auto"
    assert RuntimeSettingsPatch(model=DEEPSEEK_V41_TEST_MODEL).model == "auto"
    assert RuntimeSettingsPatch().model is None
    assert Settings(_env_file=None, deepseek_model=DEEPSEEK_V41_TEST_MODEL).deepseek_model == "deepseek-v4-flash"
    assert not reasoning_capability("deepseek", DEEPSEEK_V41_TEST_MODEL).levels
    index = (Path(__file__).resolve().parents[1] / "deepdesk/static/index.html").read_text("utf-8")
    assert DEEPSEEK_V41_TEST_MODEL not in index


@pytest.mark.asyncio
async def test_old_model_name_cannot_activate_vision_exp_priority(tmp_path, monkeypatch):
    from test_deepseek_vision_fallback import OCR, mock_http, runtime, success

    from deepdesk.plugins.base import ToolContext
    from deepdesk.plugins.builtin.vision import VisionTool
    from deepdesk.vision_runtime import VisionEndpoint

    rt = runtime(tmp_path)
    seen = []

    async def ready():
        return VisionEndpoint(rt.configured_url, rt.configured_model, "configured", api_key=rt.api_key)

    def respond(request):
        seen.append(request.url.host)
        assert request.url.host == "google.test"
        return success("Google OCR fixture")

    monkeypatch.setattr(rt, "ensure_ready", ready)
    mock_http(monkeypatch, respond)
    image = tmp_path / "fixture.png"
    image.write_bytes(b"Mocked HTTP fixture only")
    tool = VisionTool(rt, tmp_path, windows_ocr=OCR(), paddle_ocr=OCR(), active_model=lambda: DEEPSEEK_V41_TEST_MODEL)
    result = await tool.execute({"image_path": str(image), "question": "Read text", "mode": "semantic"}, ToolContext(task_id="v41-ocr", workspace=str(tmp_path)))
    assert result["source"] == "configured" and seen == ["google.test"]


def test_deepseek_payload_keeps_provider_specific_reasoning_controls():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    payload = client._chat_payload(
        [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "continue this DeepSeek reasoning state",
            }
        ],
        [],
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["messages"][0]["reasoning_content"] == (
        "continue this DeepSeek reasoning state"
    )


def test_anthropic_uses_native_messages_shape_for_system_and_tools():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [{"provider": "anthropic", "model": "claude-sonnet-test", "api_key": "an"}]
    )
    endpoint = client._endpoint_for("anthropic:claude-sonnet-test")
    payload = client._anthropic_payload(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Inspect this."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "content": "done"},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "inspect",
                    "description": "Inspect a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        endpoint=endpoint,
    )

    assert endpoint.base_url == "https://api.anthropic.com/v1"
    assert payload["system"] == "Be precise."
    assert payload["model"] == "claude-sonnet-test"
    assert payload["max_tokens"] == 32768
    assert payload["stream"] is True
    assert payload["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "inspect",
        "input": {"path": "a.txt"},
    }
    assert payload["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "done",
    }
    assert payload["tools"][0]["input_schema"]["required"] == ["path"]
    assert "tools" not in client._chat_payload([], [], endpoint=endpoint)


def test_sonnet_45_declared_budget_is_emitted_by_anthropic_adapter():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "", "")
    client.set_provider_models(
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5-20250929",
                "api_key": "an",
            }
        ]
    )
    endpoint = client._endpoint_for("anthropic:claude-sonnet-4-5-20250929")
    token = client.bind_task_reasoning_effort("high")
    try:
        payload = client._anthropic_payload(
            [{"role": "user", "content": "Solve it."}], [], endpoint=endpoint
        )
    finally:
        client.reset_task_reasoning_effort(token)

    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8_192}
    assert payload["max_tokens"] >= 12_288


def test_anthropic_tool_roundtrip_preserves_signed_thinking_block():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "", "")
    client.set_provider_models(
        [{"provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "an"}]
    )
    endpoint = client._endpoint_for("anthropic:claude-sonnet-4-6")
    normalized = client._merge_anthropic_stream_events(
        [
            {
                "type": "message_start",
                "message": {"model": endpoint.model, "usage": {"input_tokens": 5}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "plan "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "carefully"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "inspect",
                    "input": {"path": "a.txt"},
                },
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8},
            },
        ]
    )
    message = normalized["choices"][0]["message"]
    payload = client._anthropic_payload(
        [
            {"role": "user", "content": "Inspect it."},
            message,
            {"role": "tool", "tool_call_id": "toolu_1", "content": "done"},
        ],
        [],
        endpoint=endpoint,
    )

    assistant_blocks = payload["messages"][1]["content"]
    assert assistant_blocks[0] == {
        "type": "thinking",
        "thinking": "plan carefully",
        "signature": "opaque-signature",
    }
    assert assistant_blocks[1]["type"] == "tool_use"


def test_generic_chat_strips_anthropic_private_state_but_keeps_standard_tool_call():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "", "")
    client.set_provider_models(
        [{"provider": "xai", "model": "grok-strict", "api_key": "xa"}]
    )
    message = {
        "role": "assistant",
        "content": "Checking.",
        "_anthropic_thinking_blocks": [
            {
                "type": "thinking",
                "thinking": "private plan",
                "signature": "opaque-anthropic-signature",
            }
        ],
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "inspect", "arguments": '{"path":"a.txt"}'},
            }
        ],
    }
    endpoint = client._endpoint_for("xai:grok-strict")

    payload = client._chat_payload([message], [], endpoint=endpoint)

    sent = payload["messages"][0]
    assert "_anthropic_thinking_blocks" not in sent
    assert sent["role"] == "assistant"
    assert sent["content"] == "Checking."
    assert sent["tool_calls"] == message["tool_calls"]
    assert "_anthropic_thinking_blocks" in message


def test_reasoning_depth_maps_to_each_native_provider_protocol():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [],
        {"openai": "mirror", "anthropic": "mirror", "google": "mirror"},
    )

    effort_token = client.bind_task_reasoning_effort("xhigh")
    try:
        openai = client._endpoint_for("aicodemirror-openai:gpt-5.6-sol")
        openai_payload = client._openai_responses_payload(
            openai, [{"role": "user", "content": "Solve it."}], [], None,
            client._effective_reasoning_effort(endpoint=openai),
        )
        assert openai_payload["reasoning"] == {"effort": "xhigh"}

        claude = client._endpoint_for("aicodemirror-anthropic:claude-opus-5")
        claude_payload = client._anthropic_payload(
            [{"role": "user", "content": "Solve it."}], [], endpoint=claude
        )
        assert claude_payload["thinking"] == {"type": "adaptive"}
        assert claude_payload["output_config"] == {"effort": "xhigh"}

        gemini = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")
        gemini_payload = client._gemini_payload(
            [{"role": "user", "content": "Solve it."}], [], endpoint=gemini
        )
        assert "generationConfig" not in gemini_payload
    finally:
        client.reset_task_reasoning_effort(effort_token)


def test_openai_compatible_gpt_chat_receives_selected_reasoning_effort():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [{"provider": "openai", "model": "gpt-5.6-sol", "api_key": "oa"}]
    )
    endpoint = client._endpoint_for("openai:gpt-5.6-sol")
    token = client.bind_task_reasoning_effort("max")
    try:
        assert client._chat_payload([], [], endpoint=endpoint)["reasoning_effort"] == "max"
    finally:
        client.reset_task_reasoning_effort(token)


@pytest.mark.asyncio
async def test_aicodemirror_openai_uses_responses_as_primary_protocol(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [], {"openai": "mirror", "anthropic": "mirror", "google": "mirror"}
    )
    endpoint = client._endpoint_for("aicodemirror-openai:gpt-5.6-terra")
    seen = {}

    async def fake_responses(target, key, messages, tools, *, recovery=False):
        seen.update(target=target, key=key, recovery=recovery)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(client, "_request_openai_responses", fake_responses)
    result = await client._request(endpoint, "mirror", [], [], recovery=True)

    assert result["choices"][0]["message"]["content"] == "ok"
    assert seen == {"target": endpoint, "key": "mirror", "recovery": True}


def test_openai_reasoning_does_not_send_unsupported_fast_tier():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [],
        {"openai": "mirror", "anthropic": "mirror", "google": "mirror"},
    )
    token = client.bind_task_reasoning_effort("high")
    try:
        deepseek = client._chat_payload([], [])
        assert deepseek["thinking"] == {"type": "enabled"}
        assert deepseek["reasoning_effort"] == "high"

        openai = client._endpoint_for("aicodemirror-openai:gpt-5.6-terra")
        responses = client._openai_responses_payload(
            openai, [{"role": "user", "content": "Hi"}], [], None,
            client._openai_reasoning_effort(endpoint=openai),
        )
        assert responses["reasoning"] == {"effort": "high"}
        assert "service_tier" not in responses

        claude = client._endpoint_for("aicodemirror-anthropic:claude-opus-5")
        anthropic = client._anthropic_payload(
            [{"role": "user", "content": "Hi"}], [], endpoint=claude
        )
        assert anthropic["thinking"] == {"type": "adaptive"}
        assert anthropic["output_config"] == {"effort": "high"}

        gemini = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")
        google = client._gemini_payload(
            [{"role": "user", "content": "Hi"}], [], endpoint=gemini
        )
        assert google["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "high"
        }
    finally:
        client.reset_task_reasoning_effort(token)


def test_anthropic_native_stream_is_normalized_for_agent_tool_loop():
    result = DeepSeekClient._merge_anthropic_stream_events(
        [
            {
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-test",
                    "usage": {"input_tokens": 12},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "Checking "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "now."},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "inspect",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"b.txt"}'},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
    )

    choice = result["choices"][0]
    assert result["model"] == "claude-sonnet-test"
    assert result["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 9,
        "total_tokens": 21,
        "reasoning_chars": 0,
    }
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Checking now."
    assert choice["message"]["tool_calls"][0]["function"] == {
        "name": "inspect",
        "arguments": '{"path":"b.txt"}',
    }


def test_aicodemirror_universal_key_adds_non_fable_models_and_protocol_endpoints():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [],
        {"anthropic": "mirror", "openai": "mirror", "google": "mirror"},
    )

    options = client.model_options()
    selectors = {item["selector"] for item in options}
    expected_count = sum(len(models) for models in AICODEMIRROR_MODELS.values()) - 2
    assert len(selectors) == expected_count
    assert "aicodemirror-openai:gpt-5.6-sol" in selectors
    assert "aicodemirror-openai:gpt-6-astra" in selectors
    assert "aicodemirror-anthropic:claude-opus-5" in selectors
    assert "aicodemirror-anthropic:claude-fable-5" not in selectors
    assert "aicodemirror-anthropic:claude-fable-5-1" not in selectors
    assert "aicodemirror-google:gemini-3.8-flash" in selectors
    assert "aicodemirror-google:gemini-3.7-flash" in selectors
    assert "aicodemirror-google:gemini-3.6-flash" in selectors
    assert client._endpoint_for("aicodemirror-openai:gpt-5.6-sol").base_url == (
        "https://api.aicodemirror.ai/api/codex/backend-api/codex/v1"
    )
    assert client._endpoint_for("aicodemirror-anthropic:claude-opus-5").base_url == (
        "https://api.aicodemirror.ai/api/claudecode/v1"
    )
    assert client._endpoint_for("aicodemirror-google:gemini-3.8-flash").base_url == (
        "https://api.aicodemirror.ai/api/gemini"
    )
    assert {item["provider"] for item in options} == {
        "aicodemirror-openai",
        "aicodemirror-anthropic",
        "aicodemirror-google",
    }
    assert client.select_automatic_model(False, "Summarize this report") == (
        "aicodemirror-openai:gpt-5.6-terra"
    )
    terra = next(
        item
        for item in options
        if item["selector"] == "aicodemirror-openai:gpt-5.6-terra"
    )
    assert terra["context_window_tokens"] == 1_050_000
    assert terra["context_window_source"] == "official_model_spec"
    opus = next(
        item
        for item in options
        if item["selector"] == "aicodemirror-anthropic:claude-opus-5"
    )
    haiku = next(
        item
        for item in options
        if item["selector"] == "aicodemirror-anthropic:claude-haiku-4-5-20251001"
    )
    gemini = next(
        item
        for item in options
        if item["selector"] == "aicodemirror-google:gemini-3.8-flash"
    )
    assert opus["context_window_tokens"] == 1_000_000
    assert haiku["context_window_tokens"] == 200_000
    assert gemini["context_window_tokens"] == 1_048_576
    assert {opus["context_window_source"], haiku["context_window_source"], gemini["context_window_source"]} == {"official_model_spec"}
    assert client.select_automatic_model(True, "Build and debug a complex service") in {
        "aicodemirror-openai:gpt-5.6-sol",
        "aicodemirror-anthropic:claude-opus-5",
    }
    client.set_default_model_preference("aicodemirror-openai:gpt-5.6-terra")
    assert client.default_model_preference == "aicodemirror-openai:gpt-5.6-terra"
    assert client.model == "aicodemirror-openai:gpt-5.6-terra"
    assert client.model_identity_info("aicodemirror-openai:gpt-5.6-terra") == {
        "selector": "aicodemirror-openai:gpt-5.6-terra",
        "model": "gpt-5.6-terra",
        "provider": "openai",
        "provider_name": "OpenAI (GPT / ChatGPT family)",
        "route": "AI Code Mirror relay",
    }
    assert client.model_identity_info("aicodemirror-anthropic:claude-opus-5")[
        "provider_name"
    ] == "Anthropic (Claude family)"
    client.set_default_model_preference("auto")
    assert client.default_model_preference == "auto"
    assert client.model == "aicodemirror-openai:gpt-5.6-terra"


def test_aicodemirror_fable_key_only_overrides_fable_and_can_enable_it_alone():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [],
        {"anthropic": "universal", "openai": "universal", "google": "universal"},
        "fable-only",
    )

    assert client._endpoint_for(
        "aicodemirror-anthropic:claude-fable-5"
    ).keys == ["fable-only"]
    assert client._endpoint_for(
        "aicodemirror-anthropic:claude-fable-5-1"
    ).keys == ["fable-only"]
    assert client._endpoint_for(
        "aicodemirror-anthropic:claude-opus-5"
    ).keys == ["universal"]
    assert client._endpoint_for(
        "aicodemirror-openai:gpt-5.6-terra"
    ).keys == ["universal"]

    client.set_provider_models([], {}, "fable-only")
    assert [item["selector"] for item in client.model_options()] == [
        "aicodemirror-anthropic:claude-fable-5-1",
        "aicodemirror-anthropic:claude-fable-5"
    ]


def test_aicodemirror_gemini_native_payload_and_tool_result_normalization():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"google": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")
    payload = client._gemini_payload(
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Inspect the file."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "inspect",
                    "description": "Inspect a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
        endpoint=endpoint,
    )
    assert payload["systemInstruction"]["parts"][0]["text"] == "Be precise."
    assert payload["contents"][1]["parts"][0]["functionCall"] == {
        "name": "inspect",
        "args": {"path": "a.txt"},
    }
    assert payload["contents"][2]["parts"][0]["functionResponse"] == {
        "name": "inspect",
        "response": {"ok": True},
    }
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "inspect"

    normalized = client._normalize_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Checking."},
                            {"functionCall": {"name": "inspect", "args": {"path": "b.txt"}}},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        },
        "gemini-3.8-flash",
    )
    assert normalized["choices"][0]["finish_reason"] == "tool_calls"
    assert normalized["choices"][0]["message"]["content"] == "Checking."
    assert normalized["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "inspect"
    assert normalized["usage"]["total_tokens"] == 15


def test_gemini_tool_roundtrip_preserves_thought_signature():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"google": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")
    normalized = client._normalize_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "inspect",
                                    "args": {"path": "a.txt"},
                                },
                                "thoughtSignature": "opaque-gemini-signature",
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        },
        endpoint.model,
    )
    message = normalized["choices"][0]["message"]
    call_id = message["tool_calls"][0]["id"]
    payload = client._gemini_payload(
        [
            {"role": "user", "content": "Inspect it."},
            message,
            {"role": "tool", "tool_call_id": call_id, "content": '{"ok":true}'},
        ],
        [],
        endpoint=endpoint,
    )

    function_part = payload["contents"][1]["parts"][0]
    assert function_part["functionCall"]["name"] == "inspect"
    assert function_part["thoughtSignature"] == "opaque-gemini-signature"


def test_gemini_tool_roundtrip_replays_native_parts_in_original_order():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"google": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")
    original_parts = [
        {
            "functionCall": {
                "id": "fc_1",
                "name": "lookup",
                "args": {"path": "a.txt"},
            },
            "thoughtSignature": "opaque-first-signature",
        },
        {"text": "Between calls.", "thoughtSignature": "opaque-text-signature"},
        {
            "functionCall": {
                "id": "fc_2",
                "name": "lookup",
                "args": {"path": "b.txt"},
            },
            "thoughtSignature": "opaque-second-signature",
        },
    ]
    normalized = client._normalize_gemini_response(
        {
            "candidates": [
                {
                    "content": {"parts": original_parts},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        },
        endpoint.model,
    )
    message = normalized["choices"][0]["message"]
    first_id, second_id = [call["id"] for call in message["tool_calls"]]
    assert (first_id, second_id) == ("fc_1", "fc_2")

    payload = client._gemini_payload(
        [
            {"role": "user", "content": "Inspect and scroll."},
            message,
            {"role": "tool", "tool_call_id": first_id, "content": '{"ok":true}'},
            {"role": "tool", "tool_call_id": second_id, "content": '{"ok":true}'},
        ],
        [],
        endpoint=endpoint,
    )

    assert payload["contents"][1]["parts"] == original_parts
    response_parts = payload["contents"][2]["parts"]
    assert len(response_parts) == 2
    assert [part["functionResponse"]["name"] for part in response_parts] == [
        "lookup",
        "lookup",
    ]
    assert [part["functionResponse"]["id"] for part in response_parts] == [
        "fc_1",
        "fc_2",
    ]
    assert len(payload["contents"]) == 3


def test_gemini_serial_tool_results_are_not_merged_across_model_turns():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"google": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-google:gemini-3.8-flash")

    def assistant_turn(call_id, path, signature):
        parts = [
            {
                "functionCall": {
                    "id": call_id,
                    "name": "lookup",
                    "args": {"path": path},
                },
                "thoughtSignature": signature,
            }
        ]
        return client._normalize_gemini_response(
            {
                "candidates": [
                    {"content": {"parts": parts}, "finishReason": "STOP"}
                ],
                "usageMetadata": {},
            },
            endpoint.model,
        )["choices"][0]["message"]

    payload = client._gemini_payload(
        [
            {"role": "user", "content": "Do both serially."},
            assistant_turn("fc_1", "a.txt", "sig-1"),
            {"role": "tool", "tool_call_id": "fc_1", "content": '{"ok":true}'},
            assistant_turn("fc_2", "b.txt", "sig-2"),
            {"role": "tool", "tool_call_id": "fc_2", "content": '{"ok":true}'},
        ],
        [],
        endpoint=endpoint,
    )

    assert [content["role"] for content in payload["contents"]] == [
        "user",
        "model",
        "user",
        "model",
        "user",
    ]
    assert payload["contents"][2]["parts"][0]["functionResponse"]["id"] == "fc_1"
    assert payload["contents"][4]["parts"][0]["functionResponse"]["id"] == "fc_2"


def test_generic_chat_strips_gemini_private_state_but_keeps_standard_tool_call():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [{"provider": "xai", "model": "grok-strict", "api_key": "xa"}]
    )
    message = client._normalize_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "inspect",
                                    "args": {"path": "a.txt"},
                                },
                                "thoughtSignature": "opaque-gemini-signature",
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        },
        "gemini-test",
    )["choices"][0]["message"]
    # Retain compatibility with task history written by the previous adapter,
    # which stored the signature beside the normalized tool call.
    message["tool_calls"][0]["provider_state"] = {
        "thought_signature": "legacy-signature"
    }
    endpoint = client._endpoint_for("xai:grok-strict")

    payload = client._chat_payload([message], [], endpoint=endpoint)

    sent = payload["messages"][0]
    assert "_gemini_model_parts" not in sent
    assert "provider_state" not in sent["tool_calls"][0]
    assert sent["tool_calls"][0]["function"] == {
        "name": "inspect",
        "arguments": '{"path": "a.txt"}',
    }
    assert "_gemini_model_parts" in message
    assert "provider_state" in message["tool_calls"][0]


def test_aicodemirror_openai_responses_payload_preserves_agent_tool_loop():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-openai:gpt-5.6-terra")
    payload = client._openai_responses_payload(
        endpoint,
        [
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Inspect it."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "inspect", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "inspect",
                    "description": "Inspect a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        2048,
    )

    assert payload["instructions"] == "Be precise."
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "inspect",
        "arguments": '{"path":"a.txt"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":true}',
    }
    assert payload["tools"][0]["name"] == "inspect"
    assert payload["max_output_tokens"] == 2048
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_openai_responses_tool_roundtrip_preserves_reasoning_items():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror"})
    endpoint = client._endpoint_for("aicodemirror-openai:gpt-5.6-terra")
    normalized = client._normalize_openai_response(
        {
            "status": "completed",
            "model": endpoint.model,
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "opaque-reasoning-state",
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "inspect",
                    "arguments": '{"path":"a.txt"}',
                },
            ],
            "usage": {},
        },
        endpoint.model,
    )
    message = normalized["choices"][0]["message"]
    payload = client._openai_responses_payload(
        endpoint,
        [
            {"role": "user", "content": "Inspect it."},
            message,
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
        ],
        [],
        2048,
        "high",
    )

    assert payload["input"][1] == {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-reasoning-state",
    }
    assert payload["input"][2]["type"] == "function_call"
    assert payload["input"][3]["type"] == "function_call_output"
    assert client._openai_responses_payload(endpoint, [], [], None, None)[
        "include"
    ] == ["reasoning.encrypted_content"]


def test_generic_chat_strips_openai_reasoning_items_but_keeps_standard_tool_call():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [{"provider": "xai", "model": "grok-strict", "api_key": "xa"}]
    )
    message = client._normalize_openai_response(
        {
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "opaque-reasoning-state",
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "inspect",
                    "arguments": '{"path":"a.txt"}',
                },
            ],
            "usage": {},
        },
        "gpt-test",
    )["choices"][0]["message"]
    message["reasoning_content"] = "nonstandard reasoning text"
    endpoint = client._endpoint_for("xai:grok-strict")

    payload = client._chat_payload([message], [], endpoint=endpoint)

    sent = payload["messages"][0]
    assert "_openai_reasoning_items" not in sent
    assert "reasoning_content" not in sent
    assert sent["tool_calls"][0]["id"] == "call_1"
    assert sent["tool_calls"][0]["function"] == {
        "name": "inspect",
        "arguments": '{"path":"a.txt"}',
    }
    assert "_openai_reasoning_items" in message
    assert "reasoning_content" in message


def test_openai_responses_normalization_supports_text_and_function_calls():
    result = DeepSeekClient._normalize_openai_response(
        {
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Checking."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "inspect",
                    "arguments": '{"path":"b.txt"}',
                },
            ],
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        },
        "gpt-test",
    )

    choice = result["choices"][0]
    assert choice["message"]["content"] == "Checking."
    assert choice["message"]["tool_calls"][0]["function"] == {
        "name": "inspect",
        "arguments": '{"path":"b.txt"}',
    }
    assert choice["finish_reason"] == "tool_calls"
    assert result["usage"]["total_tokens"] == 18


def test_empty_aicodemirror_chat_stream_falls_back_to_responses(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror"})
    token = client.bind_task_model("aicodemirror-openai:gpt-5.6-terra")
    calls = {"chat": 0, "responses": 0}

    async def empty_chat(endpoint, key, messages, tools, *, recovery=False):
        calls["chat"] += 1
        return {
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {},
            "model": endpoint.model,
        }

    async def responses_fallback(endpoint, key, messages, tools):
        calls["responses"] += 1
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 4},
            "model": endpoint.model,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", empty_chat)
    monkeypatch.setattr(client, "_request_openai_responses", responses_fallback)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        reply = asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model(token)

    assert reply.message["content"] == "Recovered."
    assert calls == {"chat": 3, "responses": 1}


def test_aicodemirror_http_524_immediately_falls_back_to_responses(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror"})
    token = client.bind_task_model("aicodemirror-openai:gpt-5.6-sol")
    calls = {"chat": 0, "responses": 0}

    async def timed_out_chat(endpoint, key, messages, tools, *, recovery=False):
        calls["chat"] += 1
        request = httpx.Request("POST", f"{endpoint.base_url}/chat/completions")
        response = httpx.Response(524, request=request)
        raise httpx.HTTPStatusError(
            "HTTP 524", request=request, response=response
        )

    async def responses_fallback(endpoint, key, messages, tools):
        calls["responses"] += 1
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 4},
            "model": endpoint.model,
        }

    monkeypatch.setattr(client, "_request", timed_out_chat)
    monkeypatch.setattr(client, "_request_openai_responses", responses_fallback)
    try:
        reply = asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model(token)

    assert reply.message["content"] == "Recovered."
    assert calls == {"chat": 1, "responses": 1}


def test_task_model_failover_prefers_another_ai_company_and_persists(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [], {"openai": "mirror", "anthropic": "mirror", "google": "mirror"}
    )
    model_token = client.bind_task_model("aicodemirror-openai:gpt-5.6-sol")
    events = []
    failover_token = client.bind_task_model_failover(events.append)
    calls = []

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        calls.append(endpoint.selector)
        if endpoint.provider == "openai":
            request = httpx.Request("POST", f"{endpoint.base_url}/chat/completions")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("HTTP 503", request=request, response=response)
        assert any(
            "MODEL FAILOVER OVERRIDE" in str(message.get("content") or "")
            for message in messages
        )
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered elsewhere."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
            "model": endpoint.model,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", provider_request)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def run_task():
        reply = await client.chat([{"role": "user", "content": "Help"}], [])
        return reply, client.effective_selector

    try:
        reply, selected_after_recovery = asyncio.run(run_task())
    finally:
        client.reset_task_model_failover(failover_token)
        client.reset_task_model(model_token)

    assert reply.message["content"] == "Recovered elsewhere."
    assert calls[:3] == ["aicodemirror-openai:gpt-5.6-sol"] * 3
    assert events and events[0]["from_provider"] == "openai"
    assert events[0]["to_provider"] != "openai"
    assert events[0]["different_provider"] is True
    assert selected_after_recovery == events[0]["to_model"]


def test_same_provider_different_model_failover_strips_model_bound_state(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [
            {"provider": "anthropic", "model": "claude-alpha", "api_key": "an"},
            {"provider": "anthropic", "model": "claude-beta", "api_key": "an"},
        ]
    )
    model_token = client.bind_task_model("anthropic:claude-alpha")
    failover_token = client.bind_task_model_failover(lambda _event: None)
    fallback_private_counts = []
    fallback_tool_calls = []
    fallback_systems = []
    original_message = {
        "role": "assistant",
        "content": "Inspecting.",
        "reasoning_content": "provider-private reasoning",
        "_anthropic_thinking_blocks": [
            {
                "type": "thinking",
                "thinking": "private plan",
                "signature": "model-bound-signature",
            }
        ],
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "inspect", "arguments": "{}"},
            }
        ],
    }
    history = [original_message]

    def private_state_count(value):
        private_keys = {
            "_anthropic_thinking_blocks",
            "_openai_reasoning_items",
            "_gemini_model_parts",
            "provider_state",
            "reasoning_content",
        }
        if isinstance(value, dict):
            return sum(key in private_keys for key in value) + sum(
                private_state_count(item) for item in value.values()
            )
        if isinstance(value, list):
            return sum(private_state_count(item) for item in value)
        return 0

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        if endpoint.selector == "anthropic:claude-alpha":
            request = httpx.Request("POST", f"{endpoint.base_url}/messages")
            raise httpx.ConnectError("alpha offline", request=request)
        fallback_private_counts.append(private_state_count(messages))
        # Verify the provider-facing payload, not just the internal event.
        fallback_systems.append(
            client._anthropic_payload(messages, tools, endpoint=endpoint)["system"]
        )
        assistant = next(
            message for message in messages if message.get("role") == "assistant"
        )
        fallback_tool_calls.append(assistant["tool_calls"])
        if len(fallback_private_counts) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "toolu_beta",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": '{"path":"b.txt"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {},
                "model": endpoint.model,
            }
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
            "model": endpoint.model,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", provider_request)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        first_reply = asyncio.run(client.chat(history, []))
        history.extend(
            [
                first_reply.message,
                {
                    "role": "tool",
                    "tool_call_id": "toolu_beta",
                    "content": '{"ok":true}',
                },
            ]
        )
        second_reply = asyncio.run(client.chat(history, []))
    finally:
        client.reset_task_model_failover(failover_token)
        client.reset_task_model(model_token)

    assert first_reply.message["tool_calls"][0]["id"] == "toolu_beta"
    assert second_reply.message["content"] == "Recovered."
    assert fallback_private_counts == [0, 0]
    assert len(fallback_systems) == 2
    for system in fallback_systems:
        assert "Host-recorded previous selector: anthropic:claude-alpha" in system
        assert "Host selector: anthropic:claude-beta" in system
        assert "Never claim that no other model was used" in system
        assert "briefly disclose this switch" in system
        assert "does not establish whether subagents were used" in system
    assert private_state_count(history) == 0
    assert fallback_tool_calls[0] == original_message["tool_calls"]
    assert history[0]["tool_calls"] == original_message["tool_calls"]
    assert "_anthropic_thinking_blocks" in original_message
    assert "reasoning_content" in original_message


def test_same_selector_key_failover_preserves_deepseek_reasoning_state(monkeypatch):
    client = DeepSeekClient(
        "https://api.deepseek.example",
        "deepseek-v4-pro",
        "primary-key",
        "backup-key",
    )
    history = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "same-model continuation state",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ],
        }
    ]
    backup_messages = []

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        if key == "primary-key":
            request = httpx.Request("POST", f"{endpoint.base_url}/chat/completions")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("HTTP 401", request=request, response=response)
        backup_messages.extend(messages)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
            "model": endpoint.model,
        }

    monkeypatch.setattr(client, "_request", provider_request)

    reply = asyncio.run(client.chat(history, []))

    assert reply.message["content"] == "Recovered."
    assert backup_messages[0]["reasoning_content"] == "same-model continuation state"
    assert history[0]["reasoning_content"] == "same-model continuation state"


def test_exhausted_failover_reports_every_attempted_provider(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-test", "api_key": "oa"},
            {"provider": "anthropic", "model": "claude-test", "api_key": "an"},
            {"provider": "google", "model": "gemini-test", "api_key": "gg"},
        ]
    )
    model_token = client.bind_task_model("openai:gpt-test")
    failover_token = client.bind_task_model_failover(lambda _event: None)

    async def offline(endpoint, key, messages, tools, *, recovery=False):
        request = httpx.Request("POST", f"{endpoint.base_url}/chat")
        raise httpx.ConnectError(f"{endpoint.provider} offline", request=request)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", offline)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        with pytest.raises(DeepSeekError) as captured:
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model_failover(failover_token)
        client.reset_task_model(model_token)

    message = str(captured.value)
    assert "openai:gpt-test" in message
    assert "anthropic:claude-test" in message
    assert "google:gemini-test" in message


def test_reasoning_capabilities_are_exact_and_unknown_models_are_not_guessed():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [{"provider": "openai", "model": "gpt-future-unknown", "api_key": "oa"}],
        {"openai": "mirror", "anthropic": "mirror", "google": "mirror"},
        "fable-only",
    )

    options = {item["selector"]: item["reasoning"] for item in client.model_options()}
    assert options["deepseek-v4-flash"]["levels"] == ["high", "max"]
    assert options["deepseek-v4-pro"]["levels"] == ["high", "max"]
    assert options["aicodemirror-openai:gpt-5.6-sol"]["levels"] == [
        "low", "medium", "high", "xhigh", "max"
    ]
    assert options["aicodemirror-google:gemini-3.8-flash"]["levels"] == [
        "low", "medium", "high"
    ]
    assert options["aicodemirror-google:gemini-3.7-flash"]["levels"] == [
        "low", "medium", "high"
    ]
    assert options["aicodemirror-google:gemini-3.6-flash"]["levels"] == [
        "minimal", "low", "medium", "high"
    ]
    assert options["aicodemirror-google:gemini-3.1-pro-preview"]["levels"] == [
        "low", "medium", "high"
    ]
    assert options["aicodemirror-anthropic:claude-opus-4-6"]["levels"] == [
        "low", "medium", "high", "max"
    ]
    assert options["aicodemirror-anthropic:claude-fable-5-1"]["levels"] == [
        "low", "medium", "high", "xhigh", "max"
    ]
    assert options["openai:gpt-future-unknown"] == {
        "supported": False,
        "levels": [],
        "control": "none",
        "verification": "unverified",
    }


def test_direct_google_compatibility_route_does_not_claim_native_reasoning_control():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "", "")
    client.set_provider_models(
        [
            {
                "provider": "google",
                "model": "gemini-3.5-flash",
                "api_key": "gg",
            }
        ]
    )
    endpoint = client._endpoint_for("google:gemini-3.5-flash")
    option = next(
        item
        for item in client.model_options()
        if item["selector"] == endpoint.selector
    )

    assert option["reasoning"] == {
        "supported": False,
        "levels": [],
        "control": "none",
        "verification": "google_openai_compatibility_reasoning_unverified",
    }
    assert "reasoning_effort" not in client._chat_payload([], [], endpoint=endpoint)


def test_direct_google_compat_stream_roundtrip_preserves_only_its_own_signature():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "", "")
    client.set_provider_models(
        [
            {
                "provider": "google",
                "model": "gemini-3.5-flash",
                "api_key": "gg",
            },
            {
                "provider": "google",
                "model": "gemini-3.1-pro-preview",
                "api_key": "gg",
            },
        ]
    )
    endpoint = client._endpoint_for("google:gemini-3.5-flash")
    other_endpoint = client._endpoint_for("google:gemini-3.1-pro-preview")
    normalized = client._merge_stream_events(
        [
            {
                "model": endpoint.model,
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_google_1",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": '{"path":',
                                    },
                                    "extra_content": {
                                        "google": {
                                            "thought_signature": "opaque-"
                                        }
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a.txt"}'},
                                    "extra_content": {
                                        "google": {
                                            "thought_signature": "signature"
                                        }
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
    )
    client._tag_google_compatibility_state(normalized, endpoint.selector)
    message = normalized["choices"][0]["message"]
    history = [
        {"role": "user", "content": "Inspect it."},
        message,
        {
            "role": "tool",
            "tool_call_id": "call_google_1",
            "content": '{"ok":true}',
        },
    ]

    own_payload = client._chat_payload(history, [], endpoint=endpoint)
    cross_model_payload = client._chat_payload(history, [], endpoint=other_endpoint)

    own_call = own_payload["messages"][1]["tool_calls"][0]
    assert own_call["function"] == {
        "name": "inspect",
        "arguments": '{"path":"a.txt"}',
    }
    assert own_call["extra_content"] == {
        "google": {"thought_signature": "opaque-signature"}
    }
    assert "_provider_state_selector" not in own_payload["messages"][1]
    assert own_payload["messages"][2] == history[2]
    assert "extra_content" not in cross_model_payload["messages"][1]["tool_calls"][0]
    assert cross_model_payload["messages"][1]["tool_calls"][0]["function"] == (
        own_call["function"]
    )


def test_cloud_model_options_truthfully_report_implemented_tool_adapters():
    client = DeepSeekClient(
        "https://api.deepseek.example", "deepseek-v4-pro", "deepseek-key", ""
    )
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-5.6-sol", "api_key": "oa"},
            {"provider": "anthropic", "model": "claude-sonnet-4-6", "api_key": "an"},
            {"provider": "google", "model": "gemini-3.5-flash", "api_key": "gg"},
        ],
        {"openai": "mirror", "anthropic": "mirror", "google": "mirror"},
    )

    options = client.model_options()
    cloud = [item for item in options if item["provider"] != "local"]
    assert cloud
    assert all(item["supports_tools"] is True for item in cloud)
    assert all("tools" in item["capabilities"] for item in cloud)


def test_unsupported_reasoning_level_is_omitted_not_remapped():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-pro", "key", "")
    client.set_provider_models(
        [], {"openai": "mirror", "anthropic": "mirror", "google": "mirror"}
    )
    token = client.bind_task_reasoning_effort("xhigh")
    try:
        gemini = client._endpoint_for("aicodemirror-google:gemini-3.1-pro-preview")
        assert "generationConfig" not in client._gemini_payload(
            [{"role": "user", "content": "Solve it."}], [], endpoint=gemini
        )
        claude = client._endpoint_for("aicodemirror-anthropic:claude-opus-4-6")
        payload = client._anthropic_payload(
            [{"role": "user", "content": "Solve it."}], [], endpoint=claude
        )
        assert "output_config" not in payload
    finally:
        client.reset_task_reasoning_effort(token)

def test_malformed_provider_response_uses_bounded_cross_provider_failover(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror", "anthropic": "mirror"})
    model_token = client.bind_task_model("aicodemirror-openai:gpt-5.6-sol")
    failover_events = []
    failover_token = client.bind_task_model_failover(failover_events.append)
    calls = []

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        calls.append(endpoint.selector)
        if endpoint.provider == "openai":
            raise json.JSONDecodeError("truncated relay JSON", "{", 1)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
            "model": endpoint.model,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", provider_request)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        reply = asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model_failover(failover_token)
        client.reset_task_model(model_token)

    assert reply.message["content"] == "Recovered."
    assert calls[:3] == ["aicodemirror-openai:gpt-5.6-sol"] * 3
    assert failover_events[0]["from_provider"] == "openai"
    assert failover_events[0]["to_provider"] == "anthropic"


def test_malformed_responses_fallback_does_not_escape_cross_provider_failover(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror", "anthropic": "mirror"})
    model_token = client.bind_task_model("aicodemirror-openai:gpt-5.6-sol")
    failover_token = client.bind_task_model_failover(lambda _event: None)
    calls = []

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        calls.append(endpoint.selector)
        if endpoint.provider == "openai":
            request = httpx.Request("POST", f"{endpoint.base_url}/chat/completions")
            response = httpx.Response(524, request=request)
            raise httpx.HTTPStatusError("HTTP 524", request=request, response=response)
        return {
            "choices": [{"message": {"role": "assistant", "content": "Recovered."}}],
            "usage": {},
            "model": endpoint.model,
        }

    async def malformed_fallback(*_args, **_kwargs):
        raise json.JSONDecodeError("truncated Responses JSON", "{", 1)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", provider_request)
    monkeypatch.setattr(client, "_request_openai_responses", malformed_fallback)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        reply = asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model_failover(failover_token)
        client.reset_task_model(model_token)

    assert reply.message["content"] == "Recovered."
    assert calls[:3] == ["aicodemirror-openai:gpt-5.6-sol"] * 3


def test_aicodemirror_settlement_error_fails_over_once_and_opens_circuit(monkeypatch):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "mirror", "anthropic": "mirror"})
    events = []
    failover_token = client.bind_task_model_failover(events.append)
    calls = []

    async def provider_request(endpoint, key, messages, tools, *, recovery=False):
        calls.append(endpoint.selector)
        if endpoint.provider == "openai":
            request = httpx.Request("POST", f"{endpoint.base_url}/responses")
            response = httpx.Response(
                503,
                request=request,
                json={
                    "error": "Settlement blocked",
                    "code": "SETTLEMENT_PRICING_NOT_FOUND",
                },
            )
            raise httpx.HTTPStatusError("HTTP 503", request=request, response=response)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Recovered."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
            "model": endpoint.model,
        }

    monkeypatch.setattr(client, "_request", provider_request)
    try:
        for _ in range(2):
            model_token = client.bind_task_model("aicodemirror-openai:gpt-5.6-terra")
            try:
                reply = asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
                assert reply.message["content"] == "Recovered."
            finally:
                client.reset_task_model(model_token)
    finally:
        client.reset_task_model_failover(failover_token)

    assert calls.count("aicodemirror-openai:gpt-5.6-terra") == 1
    assert events[0]["reason"].endswith("(SETTLEMENT_PRICING_NOT_FOUND)")
    assert "temporarily unavailable" in events[1]["reason"]


@pytest.mark.parametrize(
    ("provider", "error_type"),
    [
        ("openai", OpenAIError),
        ("anthropic", AnthropicError),
        ("google", GeminiError),
        ("xai", XAIError),
    ],
)
def test_multi_provider_failures_report_actual_provider(
    monkeypatch, provider, error_type
):
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [{"provider": provider, "model": "test-model", "api_key": "key"}]
    )
    token = client.bind_task_model(f"{provider}:test-model")

    async def empty_chat(endpoint, key, messages, tools, *, recovery=False):
        return {
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {},
            "model": endpoint.model,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", empty_chat)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    try:
        with pytest.raises(error_type):
            asyncio.run(client.chat([{"role": "user", "content": "Hello"}], []))
    finally:
        client.reset_task_model(token)


def test_retired_luna_is_not_selectable_from_relay_or_custom_provider_rows():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [
            {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "api_key": "direct-key",
            }
        ],
        {"openai": "relay-key"},
    )

    selectors = {item["selector"] for item in client.model_options()}
    assert "openai:gpt-5.6-luna" not in selectors
    assert "aicodemirror-openai:gpt-5.6-luna" not in selectors
    client.set_default_model_preference("aicodemirror-openai:gpt-5.6-luna")
    assert client.default_model_preference == "auto"
