import json

import httpx
import pytest

from deepdesk.deepseek import DeepSeekClient
from deepdesk.model_selection import resolve_model_selection


def _client():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    client.set_provider_models(
        [{"provider": "openai", "model": "gpt-6-astra", "api_key": "direct-test"}],
        {"openai": "relay-test", "anthropic": "relay-test"},
    )
    return client


def test_astra_catalog_key_isolation_selection_and_context():
    client = _client()
    selector = "aicodemirror-openai:gpt-6-astra"
    relay_options = [item for item in client.model_options()
                     if item["provider"] == "aicodemirror-openai"]
    assert resolve_model_selection("GPT 6 Astra", relay_options) == selector
    assert resolve_model_selection(selector, client.model_options()) == selector
    assert client._endpoint_for(selector).keys == ["relay-test"]
    assert client._endpoint_for("openai:gpt-6-astra").keys == ["direct-test"]
    assert client.context_window_info(selector) == {
        "context_window_tokens": 1_050_000,
        "context_window_source": "official_model_spec",
    }
    assert client.reasoning_capability(selector).public() == {
        "supported": True,
        "levels": ["low", "medium", "high", "xhigh", "max"],
        "control": "openai_reasoning_effort",
        "verification": "openai_model_spec",
    }
    client.set_default_model_preference(selector)
    assert client.default_model_preference == selector
    assert client.effective_selector == selector

    fable_only = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "", "")
    fable_only.set_provider_models([], {}, aicodemirror_fable_key="fable-test")
    assert selector not in {item["selector"] for item in fable_only.model_options()}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["openai:gpt-6-astra", "aicodemirror-openai:gpt-6-astra"])
@pytest.mark.parametrize("effort", ["auto", "low", "medium", "high", "xhigh", "max"])
async def test_astra_responses_tool_continuation_and_reasoning(monkeypatch, selector, effort):
    client = _client()
    endpoint = client._endpoint_for(selector)
    requests = []
    reasoning = {"type": "reasoning", "id": "rs_test", "encrypted_content": "opaque"}

    def handle(request):
        assert str(request.url) == endpoint.base_url + "/responses"
        assert request.headers["Authorization"] == "Bearer " + endpoint.keys[0]
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "gpt-6-astra"
        assert not {"temperature", "top_p", "top_logprobs", "logprobs"} & payload.keys()
        assert payload["include"] == ["reasoning.encrypted_content"]
        if effort == "auto":
            assert "reasoning" not in payload
        else:
            assert payload["reasoning"] == {"effort": effort}
        assert payload["tools"][0]["name"] == "inspect"
        output = (
            [reasoning, {
                "type": "function_call", "call_id": "call_test",
                "name": "inspect", "arguments": '{"path":"a.txt"}',
            }]
            if len(requests) == 1 else
            [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Done"},
            ]}]
        )
        return httpx.Response(200, json={"model": "gpt-6-astra", "output": output})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    messages = [{"role": "user", "content": "Inspect a.txt"}]
    tools = [{"type": "function", "function": {
        "name": "inspect", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
        }},
    }}]
    token = client.bind_task_reasoning_effort(effort)
    try:
        first = await client._request(endpoint, endpoint.keys[0], messages, tools)
        message = first["choices"][0]["message"]
        assert message["tool_calls"][0]["id"] == "call_test"
        messages.extend([message, {
            "role": "tool", "tool_call_id": "call_test", "content": "File contents",
        }])
        final = await client._request(endpoint, endpoint.keys[0], messages, tools)
    finally:
        client.reset_task_reasoning_effort(token)
    assert final["choices"][0]["message"]["content"] == "Done"
    items = requests[1]["input"]
    assert reasoning in items
    assert {"type": "function_call_output", "call_id": "call_test",
            "output": "File contents"} in items
    assert items.index(reasoning) < next(
        i for i, item in enumerate(items) if item.get("type") == "function_call"
    )
