from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from deepdesk.deepseek import DeepSeekClient, DeepSeekError
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool
from deepdesk.plugins.builtin.web import WebTool

SECRET_QUERY_PATTERN = re.compile(
    r"(?:sk-[a-zA-Z0-9_-]{16,}|bearer\s+[a-zA-Z0-9._-]{16,}|"
    r"(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)

CURRENT_FACT_PATTERN = re.compile(
    r"(?:latest|current|today|now|released?|unreleased|available|availability|"
    r"最新|当前|现在|今天|发布|未发布|可用|上线)",
    re.IGNORECASE,
)

OFFICIAL_DOMAIN_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"(?:openai|chatgpt|gpt[-\s]?\d|codex|sora)", re.IGNORECASE), ("openai.com",)),
    (re.compile(r"(?:deepseek|深度求索)", re.IGNORECASE), ("deepseek.com",)),
    (re.compile(r"(?:anthropic|claude)", re.IGNORECASE), ("anthropic.com",)),
    (re.compile(r"(?:google|gemini)", re.IGNORECASE), ("google.com",)),
    (re.compile(r"(?:xai|x\.ai|grok)", re.IGNORECASE), ("x.ai",)),
)


class ProviderWebSearchTool(ToolPlugin):
    """Search through the native tool of the model selected for this task."""

    name = "provider_web_search"
    description = (
        "Use the currently selected model provider's native server-side web search only when "
        "current or changing information is needed, the user asks to search/verify online, or "
        "a key fact is uncertain. Supports direct OpenAI, Anthropic/Claude, Google/Gemini and "
        "DeepSeek credentials, plus the matching AI Code Mirror routes. The result includes "
        "a grounded answer, source URLs, provider/model/route metadata, and verification advice. "
        "For stable facts or local-file tasks, do not search. If native search times out, is "
        "unsupported, or returns no grounding, this tool automatically tries programmatic public "
        "Web search and an isolated Edge browser before returning control to the Agent."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 1000},
            "max_uses": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        client: DeepSeekClient,
        web_tool: WebTool | None = None,
        background_browser_tool: BackgroundBrowserTool | None = None,
    ) -> None:
        self.client = client
        self.web_tool = web_tool
        self.background_browser_tool = background_browser_tool

    async def _search(self, query: str, max_uses: int) -> Any:
        return await self.client.provider_web_search(query, max_uses)

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"当前模型原生联网搜索：{str(arguments.get('query') or '')[:160]}"

    async def _native_search(self, arguments: dict[str, Any]) -> Any:
        query = str(arguments.get("query") or "").strip()
        if len(query) < 2:
            raise ValueError("query is required")
        if SECRET_QUERY_PATTERN.search(query):
            raise PermissionError("Credentials and secrets cannot be sent to web search")
        current_date = datetime.now(UTC).date().isoformat()
        time_sensitive = bool(CURRENT_FACT_PATTERN.search(query))
        official_domains = self._official_domains_for_query(query)
        effective_query = query
        if time_sensitive:
            domain_hint = (
                " Prefer primary pages under " + ", ".join(official_domains) + "."
                if official_domains
                else " Prefer authoritative primary sources."
            )
            effective_query = (
                f"{query}\n\nCurrent-date verification instructions (not a factual premise): "
                f"Today is {current_date} UTC. Verify release and availability as of this date."
                f"{domain_hint} Do not treat rumors, cached snippets, predictions, or older articles "
                "as proof of current status."
            )

        result = await self._search(
            effective_query, int(arguments.get("max_uses", 5))
        )
        if not isinstance(result, dict):
            return result

        sources = result.get("sources") or []
        official_sources = [
            source
            for source in sources
            if isinstance(source, dict)
            and self._is_official_url(str(source.get("url") or ""), official_domains)
        ]
        official_required = time_sensitive and bool(official_domains)
        result["verification"] = {
            "requested_query": query,
            "effective_query": effective_query,
            "current_date_utc": current_date,
            "time_sensitive": time_sensitive,
            "required_official_domains": list(official_domains),
            "official_source_found": bool(official_sources),
            "official_sources": official_sources,
            "instruction": (
                "An official primary source was found; open/read it before making a release-status claim."
                if official_sources
                else (
                    "No official primary source was found. Do not claim released/unreleased/current status; "
                    "use background_browser to inspect an official page or report the status as unverified."
                    if official_required
                    else "Evaluate source dates and authority before relying on the synthesis."
                )
            ),
        }
        return result

    @staticmethod
    def _official_domains_for_query(query: str) -> tuple[str, ...]:
        domains: list[str] = []
        for pattern, candidates in OFFICIAL_DOMAIN_RULES:
            if pattern.search(query):
                domains.extend(candidates)
        return tuple(dict.fromkeys(domains))

    @staticmethod
    def _is_official_url(url: str, domains: tuple[str, ...]) -> bool:
        if not domains:
            return False
        try:
            host = (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        try:
            result = await self._native_search(arguments)
            if isinstance(result, dict) and result.get("sources"):
                return result
            failure = "Native search completed without any citable source"
        except DeepSeekError as exc:
            failure = str(exc)

        query = str(arguments.get("query") or "").strip()
        max_uses = min(max(int(arguments.get("max_uses", 5)), 1), 10)
        fallback_errors: list[str] = []

        if self.web_tool is not None:
            try:
                web_result = await self.web_tool.execute(
                    {"action": "search", "query": query, "limit": max_uses}, context
                )
                results = web_result.get("results") if isinstance(web_result, dict) else []
                if results:
                    sources = [
                        {
                            "title": str(item.get("title") or item.get("url") or "Source"),
                            "url": str(item.get("url") or ""),
                            "snippet": str(item.get("snippet") or ""),
                        }
                        for item in results
                        if isinstance(item, dict) and item.get("url")
                    ]
                    if sources:
                        return {
                            "answer": "\n\n".join(
                                f"{item['title']}\n{item['snippet']}\n{item['url']}"
                                for item in sources
                            ),
                            "sources": sources,
                            "searches": [{"query": query}],
                            "provider": str(web_result.get("provider") or "programmatic-web"),
                            "route": "programmatic-fallback",
                            "fallback_used": True,
                            "native_failure": failure,
                        }
                fallback_errors.append("programmatic web search returned no results")
            except Exception as exc:
                fallback_errors.append(f"programmatic search: {type(exc).__name__}: {exc}")

        if self.background_browser_tool is not None:
            try:
                browser_result = await self.background_browser_tool.execute(
                    {
                        "action": "search",
                        "query": query,
                        "timeout_seconds": 30,
                    },
                    context,
                )
                links = browser_result.get("results") if isinstance(browser_result, dict) else []
                sources: list[dict[str, str]] = []
                for item in links or []:
                    if not isinstance(item, dict):
                        continue
                    url = self._decode_bing_result_url(str(item.get("url") or ""))
                    title = str(item.get("title") or "").strip()
                    if not title and url.startswith(("http://", "https://")):
                        title = str(urlsplit(url).hostname or url)
                    if url.startswith(("http://", "https://")) and title:
                        sources.append(
                            {
                                "title": title,
                                "url": url,
                                "snippet": str(item.get("snippet") or ""),
                            }
                        )
                    if len(sources) >= max_uses:
                        break
                if sources:
                    return {
                        "answer": str(browser_result.get("text") or "")[:30_000],
                        "sources": sources,
                        "searches": [{"query": query}],
                        "provider": "isolated-edge",
                        "route": "browser-fallback",
                        "fallback_used": True,
                        "native_failure": failure,
                    }
                fallback_errors.append("isolated Edge returned no result links")
            except Exception as exc:
                fallback_errors.append(f"isolated Edge: {type(exc).__name__}: {exc}")

        return {
            "answer": "",
            "sources": [],
            "searches": [],
            "provider": "fallback-unavailable",
            "route": "agent-program-required",
            "fallback_used": True,
            "fallback_required": True,
            "native_failure": failure,
            "fallback_errors": fallback_errors,
            "instruction": (
                "Use sandbox to write and run a bounded public-web crawler for this query, "
                "then inspect the returned source URLs. Do not include credentials, cookies, "
                "private files, or local-network addresses in the program or request."
            ),
        }

    @staticmethod
    def _decode_bing_result_url(url: str) -> str:
        """Recover the public destination from Bing's a1/base64 redirect."""
        try:
            parsed = urlsplit(url)
            if not parsed.hostname or not parsed.hostname.endswith("bing.com"):
                return url
            token = (parse_qs(parsed.query).get("u") or [""])[0]
            if not token.startswith("a1"):
                return url
            encoded = token[2:]
            encoded += "=" * (-len(encoded) % 4)
            decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
            return decoded if decoded.startswith(("http://", "https://")) else url
        except (ValueError, UnicodeDecodeError):
            return url
