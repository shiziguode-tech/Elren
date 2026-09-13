from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from deepdesk.deepseek import DeepSeekClient, DeepSeekError, ProviderEndpoint
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.deepseek_search import ProviderWebSearchTool


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://example.test")
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed", request=self.request, response=httpx.Response(
                    self.status_code, request=self.request
                )
            )


def _client() -> DeepSeekClient:
    return DeepSeekClient(
        "https://api.deepseek.test", "deepseek-v4-flash", "ds-key", ""
    )


@pytest.mark.asyncio
async def test_openai_native_search_uses_responses_for_direct_and_relay(monkeypatch):
    calls: list[dict] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return _FakeResponse(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "web_search_call",
                            "action": {
                                "sources": [
                                    {"title": "OpenAI", "url": "https://openai.com/news"}
                                ]
                            },
                        },
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "grounded",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "title": "Docs",
                                            "url": "https://openai.com/docs",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                    "usage": {"total_tokens": 12},
                }
            )

    monkeypatch.setattr("deepdesk.deepseek.httpx.AsyncClient", Client)
    client = _client()
    for source, base in (
        ("direct", "https://api.openai.com/v1"),
        ("aicodemirror", "https://api.aicodemirror.ai/api/codex/backend-api/codex/v1"),
    ):
        endpoint = ProviderEndpoint(
            selector=f"{source}:gpt", provider="openai", model="gpt-test",
            base_url=base, keys=["secret"], source=source,
        )
        result = await client._openai_web_search_request(
            endpoint, "secret", "latest docs", 4
        )
        assert result["answer"] == "grounded"
        assert {item["url"] for item in result["sources"]} == {
            "https://openai.com/news", "https://openai.com/docs"
        }
        assert calls[-1]["url"] == f"{base}/responses"
        assert calls[-1]["json"]["tools"] == [{"type": "web_search"}]
        assert calls[-1]["json"]["tool_choice"] == "auto"
        assert calls[-1]["headers"]["Authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_anthropic_native_search_uses_messages_for_direct_and_relay(monkeypatch):
    calls: list[dict] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return _FakeResponse(
                {
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "text",
                            "text": "Claude grounded",
                            "citations": [
                                {"title": "Anthropic", "url": "https://anthropic.com/news"}
                            ],
                        }
                    ],
                    "usage": {"server_tool_use": {"web_search_requests": 1}},
                }
            )

    monkeypatch.setattr("deepdesk.deepseek.httpx.AsyncClient", Client)
    client = _client()
    for source, base in (
        ("direct", "https://api.anthropic.com/v1"),
        ("aicodemirror", "https://api.aicodemirror.ai/api/claudecode/v1"),
    ):
        endpoint = ProviderEndpoint(
            selector=f"{source}:claude", provider="anthropic", model="claude-test",
            base_url=base, keys=["secret"], source=source,
        )
        result = await client._anthropic_web_search_request(
            endpoint, "secret", "latest docs", 3
        )
        assert result["answer"] == "Claude grounded"
        assert calls[-1]["url"] == f"{base}/messages"
        assert calls[-1]["headers"]["x-api-key"] == "secret"
        assert calls[-1]["json"]["tools"] == [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ]


@pytest.mark.asyncio
async def test_gemini_grounding_urls_and_payload_for_direct_and_relay(monkeypatch):
    calls: list[dict] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return _FakeResponse(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {"parts": [{"text": "Gemini grounded"}]},
                            "groundingMetadata": {
                                "webSearchQueries": ["latest Gemini docs"],
                                "groundingChunks": [
                                    {"web": {"title": "Google", "uri": "https://ai.google.dev/"}}
                                ],
                            },
                        }
                    ],
                    "usageMetadata": {"totalTokenCount": 7},
                }
            )

    monkeypatch.setattr("deepdesk.deepseek.httpx.AsyncClient", Client)
    client = _client()
    cases = (
        (
            "direct",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
        ),
        (
            "aicodemirror",
            "https://api.aicodemirror.ai/api/gemini",
            "https://api.aicodemirror.ai/api/gemini/v1beta/models/gemini-test:generateContent",
        ),
    )
    for source, base, expected_url in cases:
        endpoint = ProviderEndpoint(
            selector=f"{source}:gemini", provider="google", model="gemini-test",
            base_url=base, keys=["secret"], source=source,
        )
        result = await client._google_web_search_request(
            endpoint, "secret", "latest docs", 5
        )
        assert result["answer"] == "Gemini grounded"
        assert result["sources"] == [
            {"title": "Google", "url": "https://ai.google.dev/"}
        ]
        assert calls[-1]["url"] == expected_url
        assert calls[-1]["json"]["tools"] == [{"google_search": {}}]
        assert calls[-1]["headers"]["x-goog-api-key"] == "secret"


@pytest.mark.asyncio
async def test_provider_search_tool_uses_current_route_and_keeps_verification(tmp_path: Path):
    captured: dict = {}

    class Client:
        async def provider_web_search(self, query, max_uses):
            captured.update(query=query, max_uses=max_uses)
            return {
                "answer": "verified",
                "sources": [
                    {"title": "OpenAI", "url": "https://developers.openai.com/api/docs/models"}
                ],
                "provider": "openai",
                "route": "direct",
            }

    tool = ProviderWebSearchTool(Client())
    assert tool.name == "provider_web_search"
    result = await tool.execute(
        {"query": "Is the current OpenAI model released?", "max_uses": 2},
        ToolContext(task_id="provider-search", workspace=str(tmp_path)),
    )
    assert captured["max_uses"] == 2
    assert result["provider"] == "openai"
    assert result["verification"]["official_source_found"] is True
    assert "openai.com" in captured["query"]


@pytest.mark.asyncio
async def test_provider_search_rejects_relay_response_without_real_grounding(monkeypatch):
    client = _client()
    endpoint = ProviderEndpoint(
        selector="aicodemirror:gemini",
        provider="google",
        model="gemini-test",
        base_url="https://api.aicodemirror.ai/api/gemini",
        keys=["secret"],
        source="aicodemirror",
    )

    async def ungrounded(*_args):
        return {
            "answer": "I do not have live internet access",
            "sources": [],
            "searches": [],
        }

    monkeypatch.setattr(client, "_endpoint_for", lambda _selector: endpoint)
    monkeypatch.setattr(client, "_google_web_search_request", ungrounded)

    with pytest.raises(DeepSeekError, match="did not execute a grounded web search"):
        await client.provider_web_search("latest AI news", 3)


@pytest.mark.asyncio
async def test_provider_search_automatically_uses_programmatic_fallback(tmp_path: Path):
    class Client:
        async def provider_web_search(self, _query, _max_uses):
            raise DeepSeekError("relay native search timed out")

    class Web:
        async def execute(self, arguments, _context):
            assert arguments["action"] == "search"
            return {
                "provider": "duckduckgo-html",
                "results": [
                    {
                        "title": "Primary source",
                        "url": "https://example.com/current",
                        "snippet": "Current information",
                    }
                ],
            }

    tool = ProviderWebSearchTool(Client(), web_tool=Web())
    result = await tool.execute(
        {"query": "latest AI news", "max_uses": 4},
        ToolContext(task_id="fallback", workspace=str(tmp_path)),
    )

    assert result["fallback_used"] is True
    assert result["route"] == "programmatic-fallback"
    assert result["sources"][0]["url"] == "https://example.com/current"
    assert "timed out" in result["native_failure"]


@pytest.mark.asyncio
async def test_provider_search_uses_isolated_edge_after_programmatic_failure(tmp_path: Path):
    class Client:
        async def provider_web_search(self, _query, _max_uses):
            raise DeepSeekError("Gemini returned no grounding")

    class Web:
        async def execute(self, _arguments, _context):
            return {"results": []}

    class Browser:
        async def execute(self, arguments, _context):
            assert arguments["action"] == "search"
            assert arguments["query"] == "current Gemini news"
            return {
                "text": "Search page",
                "results": [
                    {"title": "Result", "url": "https://example.org/result"}
                ],
            }

    tool = ProviderWebSearchTool(
        Client(), web_tool=Web(), background_browser_tool=Browser()
    )
    result = await tool.execute(
        {"query": "current Gemini news", "max_uses": 3},
        ToolContext(task_id="browser-fallback", workspace=str(tmp_path)),
    )

    assert result["route"] == "browser-fallback"
    assert result["provider"] == "isolated-edge"
    assert result["sources"][0]["url"] == "https://example.org/result"


def test_search_system_prompt_prefers_current_provider_native_tool():
    from deepdesk.engine import SYSTEM_PROMPT

    assert "provider_web_search" in SYSTEM_PROMPT
    assert "当前任务实际选择" in SYSTEM_PROMPT
    assert "官方/AI Code Mirror" in SYSTEM_PROMPT
