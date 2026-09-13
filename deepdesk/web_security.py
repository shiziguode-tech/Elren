from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_RESPONSE_HEADERS = {
    "Content-Security-Policy": (
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
USER_DOCUMENT_CSP = (
    "sandbox allow-scripts allow-downloads allow-modals; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
)
PRIVATE_RESPONSE_CACHE_HEADERS = {
    # The desktop API returns chat history, local paths, pairing data and other
    # user-private state. WebView2's disk HTTP cache outlives an Elren process,
    # so a normal browser cache entry would become an unencrypted second copy of
    # data that is encrypted or access-controlled at rest elsewhere.
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _normalized_hostname(authority: str) -> str:
    """Return an authority's hostname without accepting malformed URL syntax."""

    value = authority.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(f"//{value}")
        # Userinfo is never valid in an HTTP Host header.
        if parsed.username is not None or parsed.password is not None:
            return ""
        # Accessing .port deliberately validates an invalid/non-numeric port.
        _ = parsed.port
    except ValueError:
        return ""
    return (parsed.hostname or "").rstrip(".").casefold()


def _is_loopback_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme.casefold() not in {"http", "https"}:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        _ = parsed.port
    except ValueError:
        return False
    return bool(parsed.hostname and _is_loopback_hostname(parsed.hostname))


def _is_testclient_request(scope: Scope, hostname: str) -> bool:
    """Keep Starlette TestClient's synthetic host out of the production allowlist."""

    client = scope.get("client")
    client_host = str(client[0]) if client else ""
    return hostname == "testserver" and client_host == "testclient"


class LoopbackWebSecurityMiddleware:
    """Protect the unauthenticated desktop loopback API from browser attacks.

    Elren intentionally accepts trusted native/server clients without an Origin
    header (including the Feishu callback and its local long-connection worker).
    Browser state changes, however, must come from a loopback page.  A null
    Origin is retained for native file/WebView shells unless Fetch Metadata says
    the request was initiated cross-site by a browser.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        hostname = _normalized_hostname(headers.get("host", ""))
        if not (_is_loopback_hostname(hostname) or _is_testclient_request(scope, hostname)):
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                detail="Elren only accepts loopback Host headers",
            )
            return

        method = str(scope.get("method") or "GET").upper()
        if method in STATE_CHANGING_METHODS and not self._trusted_write(headers):
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                detail="Cross-site state-changing request blocked",
            )
            return

        await self.app(
            scope,
            receive,
            self._send_with_security_headers(
                send,
                path=str(scope.get("path") or ""),
            ),
        )

    @staticmethod
    def _trusted_write(headers: Headers) -> bool:
        origin = headers.get("origin", "").strip()
        fetch_site = headers.get("sec-fetch-site", "").strip().casefold()

        if origin:
            if origin == "null":
                # Sandboxed cross-site frames also serialize their origin as
                # null, so Fetch Metadata must distinguish them from a native
                # file/WebView client.
                return fetch_site != "cross-site"
            return _is_loopback_origin(origin)

        # Server-to-server and native clients generally send neither header.
        # A browser that omits Origin still identifies a cross-site navigation
        # through Fetch Metadata on supported engines.
        return fetch_site != "cross-site"

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(
            scope,
            receive,
            self._send_with_security_headers(
                send,
                path=str(scope.get("path") or ""),
            ),
        )

    @staticmethod
    def _send_with_security_headers(send: Send, *, path: str) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in SECURITY_RESPONSE_HEADERS.items():
                    response_headers[name] = value
                # Uploaded HTML/SVG can execute script when opened as a document.
                # Keep interactive previews, but never grant user-authored content
                # the main application's origin, storage or same-origin API access.
                # Cover the mounted screenshot alias as well as the upload route.
                if path.startswith(("/api/uploads/", "/screenshots/")):
                    response_headers["Content-Security-Policy"] = USER_DOCUMENT_CSP
                # Only code-owned frontend assets are cacheable. Everything
                # else is dynamic or user-private (including future routes,
                # errors, screenshots, uploads and outputs), so fail closed
                # instead of maintaining a fragile sensitive-route allowlist.
                is_static_asset = path == "/static" or path.startswith("/static/")
                if not is_static_asset:
                    for name, value in PRIVATE_RESPONSE_CACHE_HEADERS.items():
                        response_headers[name] = value
            await send(message)

        return wrapped
