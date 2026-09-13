"""Real streaming adapters with in-memory HTTP transport, no provider requests."""
import asyncio
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest

import deepdesk.deepseek as client_module
from deepdesk.deepseek import DeepSeekClient, ProviderEndpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["deepseek", "anthropic"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_streaming_adapter_awaits_observers_and_keeps_error_contract(monkeypatch, provider, outcome):
    if provider == "deepseek":
        events = [{"choices": [{"delta": {"content": "OK"}}], "model": "synthetic-model"},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    else:
        events = [
            {"type": "message_start", "message": {"model": "synthetic-model", "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    requests = []

    def handle(request):
        assert request.url.host == "synthetic.invalid"
        requests.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **kwargs:
                        original(transport=httpx.MockTransport(handle), **kwargs))
    clock = itertools.count(0, 11)
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    observed = []

    async def observer(event):
        await asyncio.sleep(0)
        observed.append(event)
        if outcome == "failure":
            raise OSError("Synthetic observer storage failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError

    client = DeepSeekClient("https://synthetic.invalid/v1", "deepseek-v4-flash", "synthetic-key", "")
    token = client.bind_task_stream_progress(observer)
    endpoint = ProviderEndpoint("synthetic", provider, "synthetic-model", "https://synthetic.invalid/v1",
                                ["synthetic-key"])
    try:
        operation = client._request(endpoint, "synthetic-key", [{"role": "user", "content": "Synthetic input"}], [])
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            result = await operation
            assert result["choices"][0]["message"]["content"] == "OK"
        assert requests == ["/v1/messages" if provider == "anthropic" else "/v1/chat/completions"]
        assert observed
        if outcome == "success":
            assert len(observed) >= 2 and observed[-1]["final"] is True
        else:
            assert len(observed) == 1
    finally:
        client.reset_task_stream_progress(token)
