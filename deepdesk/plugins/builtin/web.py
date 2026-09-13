from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin

MAX_BYTES = 2_000_000
MAX_TEXT = 60_000

_DDG_RESULT_LINK = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bresult__a\b[^"]*")([^>]*)>([\s\S]*?)</a>',
    re.IGNORECASE,
)
_DDG_SNIPPET = re.compile(
    r'<(?:a|div)\b(?=[^>]*\bclass="[^"]*\bresult__snippet\b[^"]*")[^>]*>'
    r'([\s\S]*?)</(?:a|div)>',
    re.IGNORECASE,
)
_HREF = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in " ".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


class WebTool(ToolPlugin):
    name = "web"
    description = (
        "Search public web summaries or fetch readable text from a public HTTP(S) page. "
        "Private, loopback, link-local, and local-network targets are blocked to prevent SSRF."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "fetch"]},
            "query": {"type": "string", "maxLength": 500},
            "url": {"type": "string", "maxLength": 4000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    _PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
    _PROXY_FAKE_IP_PROBE_HOSTS = ("example.com", "www.iana.org")
    _PROXY_FAKE_IP_CACHE_SECONDS = 300.0
    _LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".lan", ".home.arpa", ".internal")
    def __init__(self, allowed_hosts: list[str] | tuple[str, ...] | None = None) -> None:
        # ``allowed_hosts`` is accepted for backward-compatible plugin loading,
        # but public browsing no longer depends on a per-domain allowlist.
        del allowed_hosts
        self._proxy_fake_ip_cache: tuple[float, bool] | None = None
        self._proxy_fake_ip_lock = asyncio.Lock()

    @classmethod
    def _resolve_addresses(cls, hostname: str, port: int) -> set[str]:
        return {
            item[4][0]
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }

    @classmethod
    def _is_proxy_fake_ip(cls, address: str) -> bool:
        try:
            value = ipaddress.ip_address(address)
        except ValueError:
            return False
        return isinstance(value, ipaddress.IPv4Address) and value in cls._PROXY_FAKE_IP_NETWORK

    async def _proxy_fake_ip_mode_enabled(self) -> bool:
        now = time.monotonic()
        cached = self._proxy_fake_ip_cache
        if cached and now - cached[0] < self._PROXY_FAKE_IP_CACHE_SECONDS:
            return cached[1]
        async with self._proxy_fake_ip_lock:
            now = time.monotonic()
            cached = self._proxy_fake_ip_cache
            if cached and now - cached[0] < self._PROXY_FAKE_IP_CACHE_SECONDS:
                return cached[1]

            # Clash/Mihomo and similar proxy DNS modes deliberately map unrelated
            # public hostnames into RFC 2544's 198.18.0.0/15 benchmark range. Only
            # treat that range as a proxy mapping when two independent, known-public
            # probes are mapped there too. A raw 198.18.x.x URL is never allowed.
            enabled = True
            for probe in self._PROXY_FAKE_IP_PROBE_HOSTS:
                try:
                    addresses = await asyncio.to_thread(self._resolve_addresses, probe, 443)
                except socket.gaierror:
                    enabled = False
                    break
                if not addresses or not any(self._is_proxy_fake_ip(item) for item in addresses):
                    enabled = False
                    break
            self._proxy_fake_ip_cache = (now, enabled)
            return enabled

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        return (
            f"搜索网页：{arguments.get('query', '')[:100]}"
            if arguments.get("action") == "search"
            else f"读取网页：{arguments.get('url', '')[:160]}"
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "fetch":
            url = self._required(arguments, "url")
            return await self._fetch(url)
        if action == "search":
            query = self._required(arguments, "query")
            return await self._search(query, min(max(int(arguments.get("limit", 8)), 1), 20))
        raise ValueError(f"Unsupported web action: {action}")

    async def _fetch(self, requested_url: str) -> dict[str, Any]:
        url = requested_url
        headers = {"User-Agent": "Elren/0 (local agent; readable web fetch)"}
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, headers=headers) as client:
            for _ in range(6):
                await self._validate_public_url(url)
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("Redirect response had no Location header")
                        url = urljoin(url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    allowed = ("text/", "application/json", "application/xml", "+xml")
                    if not any(item in content_type for item in allowed):
                        raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError(f"Response exceeds {MAX_BYTES} bytes")
                        chunks.append(chunk)
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                text = self._html_text(body) if "html" in content_type else body
                return {
                    "url": str(response.url),
                    "status": response.status_code,
                    "content_type": content_type,
                    "text": text[:MAX_TEXT],
                    "truncated": len(text) > MAX_TEXT,
                }
        raise RuntimeError("Too many redirects")

    async def _search(self, query: str, limit: int) -> dict[str, Any]:
        # The Instant Answer endpoint is not a general search API and often
        # returns nothing for current news. Scrape DuckDuckGo's public HTML
        # results first; retain Instant Answer only as a lightweight backup.
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122 Safari/537.36 Elren/1.0"
        )
        html_error = ""
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query, "kp": "-1"},
                    headers={"User-Agent": user_agent},
                )
                response.raise_for_status()
            results = self._parse_duckduckgo_html(response.text, limit)
            if results:
                return {
                    "query": query,
                    "results": results,
                    "provider": "duckduckgo-html",
                    "programmatic_fallback": True,
                }
            html_error = "DuckDuckGo HTML returned no search results"
        except (httpx.HTTPError, ValueError) as exc:
            html_error = f"{type(exc).__name__}: {exc}"

        endpoint = "https://api.duckduckgo.com/"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                endpoint,
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                headers={"User-Agent": user_agent},
            )
            response.raise_for_status()
            payload = response.json()
        results: list[dict[str, str]] = []
        if payload.get("AbstractText"):
            results.append(
                {
                    "title": payload.get("Heading") or query,
                    "url": payload.get("AbstractURL") or "",
                    "snippet": payload["AbstractText"],
                }
            )
        for item in payload.get("RelatedTopics", []):
            candidates = item.get("Topics", []) if isinstance(item, dict) else []
            if isinstance(item, dict) and item.get("Text"):
                candidates = [item]
            for candidate in candidates:
                if candidate.get("Text"):
                    results.append(
                        {
                            "title": candidate["Text"].split(" - ", 1)[0],
                            "url": candidate.get("FirstURL", ""),
                            "snippet": candidate["Text"],
                        }
                    )
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return {
            "query": query,
            "results": results[:limit],
            "provider": "duckduckgo-instant-answer",
            "programmatic_fallback": True,
            "note": (
                "HTML search was unavailable; Instant Answer was used instead. "
                f"HTML detail: {html_error}"
            ),
        }

    @classmethod
    def _parse_duckduckgo_html(
        cls, source: str, limit: int
    ) -> list[dict[str, str]]:
        matches = list(_DDG_RESULT_LINK.finditer(source))
        results: list[dict[str, str]] = []
        for index, match in enumerate(matches):
            attributes = match.group(1) or ""
            href_match = _HREF.search(attributes)
            href = html.unescape(href_match.group(1) if href_match else "")
            try:
                parsed = urlparse(urljoin("https://html.duckduckgo.com", href))
                redirect_target = parse_qs(parsed.query).get("uddg", [])
                if redirect_target:
                    href = redirect_target[0]
            except ValueError:
                continue
            if not href.startswith(("http://", "https://")):
                continue
            title = cls._strip_search_html(match.group(2) or "")
            if not title:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            snippet_match = _DDG_SNIPPET.search(source[match.end():end])
            snippet = cls._strip_search_html(snippet_match.group(1)) if snippet_match else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _strip_search_html(value: str) -> str:
        return " ".join(html.unescape(_TAG.sub(" ", value)).split())

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value.strip()

    @staticmethod
    def _html_text(source: str) -> str:
        parser = _ReadableHTML()
        parser.feed(source)
        return parser.text()

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only absolute HTTP(S) URLs are supported")
        if parsed.username or parsed.password:
            raise PermissionError("Credentials in URLs are not allowed")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname == "localhost" or hostname.endswith(self._LOCAL_HOST_SUFFIXES):
            raise PermissionError(f"Private or non-public network target blocked: {hostname}")
        try:
            ipaddress.ip_address(hostname)
            hostname_is_ip_literal = True
        except ValueError:
            hostname_is_ip_literal = False

        try:
            addresses = await asyncio.to_thread(
                self._resolve_addresses, hostname, parsed.port or 443
            )
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve host: {hostname}") from exc
        fake_ip_mode = False
        if (
            not hostname_is_ip_literal
            and any(self._is_proxy_fake_ip(address) for address in addresses)
        ):
            fake_ip_mode = await self._proxy_fake_ip_mode_enabled()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            proxy_fake_ip = (
                not hostname_is_ip_literal
                and isinstance(ip, ipaddress.IPv4Address)
                and ip in self._PROXY_FAKE_IP_NETWORK
                and fake_ip_mode
            )
            if not ip.is_global and not proxy_fake_ip:
                raise PermissionError(f"Private or non-public network target blocked: {address}")
