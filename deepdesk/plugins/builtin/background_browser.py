from __future__ import annotations

import html
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit
from urllib.request import url2pathname

from deepdesk.browser_downloads import BrowserDownloads
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.builtin.web import WebTool


class BackgroundBrowserTool(ToolPlugin):
    name = "background_browser"
    description = (
        "Operate Elren's built-in isolated headless browser without opening Edge or touching the "
        "user's tabs, profile, foreground window, keyboard, mouse, or input method. Open a public, "
        "loopback, or workspace-scoped file:// preview URL, inspect DOM text and interactive controls, "
        "click/fill/press/select/check "
        "by selector or text, resize the viewport, and capture full-page screenshots. For responsive "
            "UI checks, render just above and below every declared CSS breakpoint. It also supports "
            "hover, drag-and-drop and explicit page scrolling without foreground input. After capture, call vision in semantic "
        "mode to verify actual pixels, overlap, clipping, black screens, contrast, and layout. Prefer "
        "this tool over launching a visible "
        "browser for web research and UI validation. The temporary session has no user cookies or "
        "logins and is destroyed automatically when the task ends. Download events are saved to the "
        "task workspace and returned in downloads with exact path, size and hash. Use inspect/wait for "
        "delayed downloads; never search personal directories or click again just to locate a file."
    )
    _FILE_DOCUMENT_EXTENSIONS = {
        ".htm", ".html", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg",
    }
    _FILE_RESOURCE_EXTENSIONS = _FILE_DOCUMENT_EXTENSIONS | {
        ".css", ".js", ".mjs", ".json", ".map", ".wasm", ".ico",
        ".woff", ".woff2", ".ttf", ".otf",
        ".mp3", ".wav", ".ogg", ".mp4", ".webm",
    }
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search", "open", "inspect", "screenshot", "click", "fill",
                    "press", "select", "check", "hover", "drag", "scroll", "set_slider",
                    "wait", "back", "reload", "resize", "close",
                ],
            },
            "query": {"type": "string", "maxLength": 500},
            "url": {"type": "string", "maxLength": 4000},
            "selector": {"type": "string", "maxLength": 1000},
            "text": {"type": "string", "maxLength": 4000},
            "target_selector": {
                "type": "string",
                "maxLength": 1000,
                "description": "Destination selector for drag",
            },
            "target_text": {
                "type": "string",
                "maxLength": 4000,
                "description": "Destination text for drag when a selector is unavailable",
            },
            "source_x": {"type": "number", "description": "Drag grab X in CSS pixels relative to the source"},
            "source_y": {"type": "number", "description": "Drag grab Y in CSS pixels relative to the source"},
            "target_x": {"type": "number", "description": "Drop X in CSS pixels relative to the destination"},
            "target_y": {"type": "number", "description": "Drop Y in CSS pixels relative to the destination"},
            "exact": {"type": "boolean"},
            "index": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based match index when a locator is ambiguous",
            },
            "key": {"type": "string", "maxLength": 100},
            "option": {"type": "string", "maxLength": 1000},
            "checked": {"type": "boolean"},
            "value": {
                "type": "number",
                "description": "Requested numeric value for set_slider; the browser clamps and snaps it to min/max/step",
            },
            "delta_x": {
                "type": "integer",
                "minimum": -10000,
                "maximum": 10000,
                "description": "Horizontal CSS pixels for scroll",
            },
            "delta_y": {
                "type": "integer",
                "minimum": -10000,
                "maximum": 10000,
                "description": "Vertical CSS pixels for scroll",
            },
            "capture": {
                "type": "boolean",
                "description": "Capture a full-page screenshot after this action",
            },
            "full_page": {"type": "boolean"},
            "viewport_width": {
                "type": "integer",
                "minimum": 320,
                "maximum": 3840,
                "description": "CSS viewport width in pixels; persists for this task session",
            },
            "viewport_height": {
                "type": "integer",
                "minimum": 480,
                "maximum": 2160,
                "description": "CSS viewport height in pixels; persists for this task session",
            },
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        allowed_hosts: list[str] | None = None,
        *,
        workspace: str | Path | None = None,
        screenshot_dir: str | Path | None = None,
    ) -> None:
        self.validator = WebTool(allowed_hosts)
        package_workspace = Path(__file__).resolve().parents[3]
        self.workspace = Path(workspace or package_workspace).expanduser().resolve()
        self.screenshot_dir = Path(
            screenshot_dir or self.workspace / "screenshots"
        ).resolve()
        configured_runtime = str(os.environ.get("ELREN_BROWSER_RUNTIME") or "").strip()
        self.browser_runtime = Path(configured_runtime).resolve() if configured_runtime else (
            self.workspace / "work" / "browser-runtime"
        )
        self._playwright = None
        self._sessions: dict[str, tuple[Any, Any]] = {}
        self._downloads: dict[str, BrowserDownloads] = {}

    @staticmethod
    def _is_loopback_url(url: str) -> bool:
        parsed = urlparse(str(url or ""))
        return (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").strip("[]").casefold()
            in {"localhost", "127.0.0.1", "::1"}
        )

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def _local_file_path(self, url: str, context: ToolContext | None) -> Path:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme.casefold() != "file":
            raise ValueError("URL is not a file:// URL")
        if parsed.username or parsed.password:
            raise PermissionError("file:// credentials are not allowed")
        host = (parsed.hostname or "").casefold()
        if host not in {"", "localhost"}:
            raise PermissionError("Network and UNC file:// hosts are not allowed")
        # url2pathname owns the single URL decode. Pre-decoding here changes
        # literal %20 into a space and lets an encoded # become a fragment on
        # Python versions whose url2pathname also splits a URL internally.
        raw_path = url2pathname(parsed.path or "")
        if raw_path.replace("\\", "/").startswith("//"):
            raise PermissionError("Network and UNC file:// paths are not allowed")
        if not Path(raw_path).is_absolute():
            raise PermissionError("file:// preview requires an absolute local path")
        try:
            target = Path(raw_path).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise FileNotFoundError("The local preview file does not exist") from exc
        roots = [self.workspace]
        if context is not None:
            roots.append(Path(context.workspace).expanduser().resolve())
        if not any(self._inside(target, root.resolve()) for root in roots):
            raise PermissionError("file:// preview is outside the active workspace")
        if not target.is_file():
            raise ValueError("file:// preview must target a regular file")
        return target

    async def _validate_navigation_url(
        self, url: str, context: ToolContext | None = None
    ) -> None:
        # Local web-app previews are a core coding workflow. Permit loopback only;
        # private LAN, link-local and cloud metadata addresses remain blocked.
        if self._is_loopback_url(url):
            return
        if urlparse(str(url or "")).scheme.casefold() == "file":
            target = self._local_file_path(url, context)
            if target.suffix.casefold() not in self._FILE_DOCUMENT_EXTENSIONS:
                raise PermissionError(
                    "file:// navigation is limited to browser-renderable workspace documents"
                )
            return
        await self.validator._validate_public_url(url)

    async def _route_request(
        self, route: Any, request: Any, context: ToolContext | None = None
    ) -> None:
        """Apply the same SSRF policy to redirects and page-created requests."""
        url = str(request.url)
        if url.startswith(("http://", "https://")):
            try:
                await self._validate_navigation_url(url, context)
            except (PermissionError, ValueError):
                await route.abort("blockedbyclient")
                return
        elif url.startswith("file://"):
            try:
                target = self._local_file_path(url, context)
                if target.suffix.casefold() not in self._FILE_RESOURCE_EXTENSIONS:
                    raise PermissionError("Local browser resource type is not allowed")
            except (FileNotFoundError, PermissionError, ValueError):
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.MEDIUM if arguments.get("action") in {"click", "fill", "drag"} else Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        target = arguments.get("url") or arguments.get("text") or arguments.get("selector") or ""
        return f"Elren built-in browser: {action} · {str(target)[:160]}"

    def _packaged_browser_executable(self) -> Path | None:
        """Return the immutable bundled headless shell without trusting PATH."""
        if not self.browser_runtime.is_dir():
            return None
        names = (
            "chrome-headless-shell.exe" if os.name == "nt" else "chrome-headless-shell",
            "headless_shell.exe" if os.name == "nt" else "headless_shell",
        )
        for name in names:
            for candidate in sorted(self.browser_runtime.glob(f"**/{name}")):
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(self.browser_runtime.resolve())
                except (OSError, ValueError):
                    continue
                if resolved.is_file():
                    return resolved
        return None

    async def _launch_browser(self) -> Any:
        launch_options: list[dict[str, Any]] = []
        packaged = self._packaged_browser_executable()
        if packaged is not None:
            launch_options.append({"executable_path": str(packaged)})
        # Use Playwright's own isolated runtime before branded browsers. A system
        # Edge/Chrome instance is only a final headless fallback.
        launch_options.append({})
        if sys.platform == "win32":
            launch_options.extend(({"channel": "msedge"}, {"channel": "chrome"}))
        errors: list[str] = []
        for option in launch_options:
            try:
                return await self._playwright.chromium.launch(
                    **option,
                    headless=True,
                    args=[
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-default-browser-check",
                        "--no-first-run",
                    ],
                )
            except Exception as exc:
                label = str(
                    option.get("channel")
                    or (
                        "Elren headless runtime"
                        if option.get("executable_path")
                        else "Playwright Chromium"
                    )
                )
                errors.append(f"{label}: {type(exc).__name__}")
        raise RuntimeError(
            "No compatible isolated browser runtime could be started (" + ", ".join(errors) + ")"
        )

    async def _session(self, context: ToolContext) -> tuple[Any, Any]:
        existing = self._sessions.get(context.task_id)
        if existing:
            return existing
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Elren built-in browser runtime is not installed; repair the portable runtime"
            ) from exc
        if self._playwright is None:
            if self.browser_runtime.is_dir():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browser_runtime)
                os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"
            self._playwright = await async_playwright().start()
        browser = await self._launch_browser()
        browser_context = await browser.new_context(
            accept_downloads=True,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
        )
        # page.goto validation alone is insufficient: a public page can redirect to,
        # or issue fetches against, a private address. Intercept every HTTP request.
        async def guarded_route(route: Any, request: Any) -> None:
            await self._route_request(route, request, context)

        await browser_context.route("**/*", guarded_route)
        page = await browser_context.new_page()
        downloads = BrowserDownloads(context.workspace, context.task_id)
        page.on('download', downloads.receive)
        self._downloads[context.task_id] = downloads
        self._sessions[context.task_id] = (browser, page)
        return browser, page

    @staticmethod
    async def _target_locator(page: Any, arguments: dict[str, Any], action: str) -> Any:
        selector = str(arguments.get("selector") or "").strip()
        text = str(arguments.get("text") or "").strip()
        if selector:
            locator = page.locator(selector)
        elif text:
            locator = page.get_by_text(text, exact=bool(arguments.get("exact", True)))
        else:
            raise ValueError(f"selector or text is required for {action}")
        count = await locator.count()
        if count == 0:
            raise LookupError(
                f"{action} target matched no elements; inspect interactive_elements and retry "
                "with a stable selector"
            )
        requested_index = arguments.get("index")
        if requested_index is None:
            if count != 1:
                samples = [value.strip()[:120] for value in (await locator.all_inner_texts())[:8]]
                raise LookupError(
                    f"{action} target matched {count} elements; pass index (0-{count - 1}) or a "
                    f"stable selector. Match text samples: {samples}"
                )
            return locator
        index = int(requested_index)
        if index >= count:
            raise IndexError(f"{action} index {index} is outside 0-{count - 1}")
        return locator.nth(index)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "close":
            await self.cleanup(context)
            return {
                "closed": True,
                "execution_mode": "background",
                "foreground_used": False,
                "browser_ui_opened": False,
            }
        _, page = await self._session(context)
        timeout = int(arguments.get("timeout_seconds", 30)) * 1000
        current_viewport = page.viewport_size or {"width": 1440, "height": 900}
        requested_width = arguments.get("viewport_width")
        requested_height = arguments.get("viewport_height")
        viewport_changed = requested_width is not None or requested_height is not None
        if viewport_changed:
            await page.set_viewport_size(
                {
                    "width": int(requested_width or current_viewport["width"]),
                    "height": int(requested_height or current_viewport["height"]),
                }
            )
            await page.wait_for_timeout(100)
        query = ""
        search_results: list[dict[str, str]] = []
        search_provider = ""
        action_observation: dict[str, Any] | None = None
        if action == "search":
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("query is required for search")
            # Major search pages frequently stall or present a CAPTCHA to an
            # isolated headless profile. Reuse the bounded, SSRF-safe public
            # search backend and render its real links in the browser session;
            # clicking a result then continues as normal browser navigation.
            search_payload = await self.validator._search(query, 20)
            search_results = [
                {
                    "title": str(item.get("title") or "")[:500],
                    "url": str(item.get("url") or "")[:4000],
                    "snippet": str(item.get("snippet") or "")[:2000],
                }
                for item in search_payload.get("results", [])
                if str(item.get("url") or "").startswith(("http://", "https://"))
            ]
            search_provider = str(search_payload.get("provider") or "public-search")
            cards = "".join(
                "<article><h2><a href=\"{}\">{}</a></h2><p>{}</p><small>{}</small></article>".format(
                    html.escape(item["url"], quote=True),
                    html.escape(item["title"] or item["url"]),
                    html.escape(item["snippet"]),
                    html.escape(item["url"]),
                )
                for item in search_results
            )
            await page.set_content(
                "<!doctype html><html><head><meta charset='utf-8'><title>Search results</title>"
                "<style>body{font:16px system-ui;max-width:960px;margin:32px auto;padding:0 20px}"
                "article{padding:16px 0;border-bottom:1px solid #ddd}h2{font-size:20px;margin:0 0 8px}"
                "p,small{line-height:1.5;color:#444;overflow-wrap:anywhere}</style></head><body>"
                f"<h1>Search results for {html.escape(query)}</h1>{cards or '<p>No results found.</p>'}"
                "</body></html>",
                wait_until="domcontentloaded",
                timeout=timeout,
            )
        elif action == "open":
            url = str(arguments.get("url") or "").strip()
            if not url:
                raise ValueError("url is required for open")
            await self._validate_navigation_url(url, context)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        elif action in {"inspect", "screenshot", "resize"}:
            pass
        elif action == "click":
            locator = await self._target_locator(page, arguments, action)
            await locator.click(timeout=timeout)
            await page.wait_for_timeout(150)
        elif action == "fill":
            locator = await self._target_locator(page, arguments, action)
            await locator.fill(str(arguments.get("text") or ""), timeout=timeout)
        elif action == "press":
            locator = await self._target_locator(page, arguments, action)
            key = str(arguments.get("key") or "").strip()
            if not key:
                raise ValueError("key is required for press")
            await locator.press(key, timeout=timeout)
        elif action == "select":
            locator = await self._target_locator(page, arguments, action)
            option = str(arguments.get("option") or "").strip()
            if not option:
                raise ValueError("option is required for select")
            await locator.select_option(option, timeout=timeout)
        elif action == "check":
            locator = await self._target_locator(page, arguments, action)
            if bool(arguments.get("checked", True)):
                await locator.check(timeout=timeout)
            else:
                await locator.uncheck(timeout=timeout)
        elif action == "hover":
            locator = await self._target_locator(page, arguments, action)
            await locator.hover(timeout=timeout)
            await page.wait_for_timeout(100)
        elif action == "drag":
            source = await self._target_locator(page, arguments, action)
            target_arguments = {
                "selector": arguments.get("target_selector"),
                "text": arguments.get("target_text"),
                "exact": arguments.get("exact", True),
            }
            target = await self._target_locator(page, target_arguments, "drag destination")
            source_box = await source.bounding_box()
            target_box = await target.bounding_box()
            if source_box is None or target_box is None:
                raise RuntimeError("drag source and destination must both have visible geometry")
            source_local = await source.evaluate("el => ({width:el.clientWidth,height:el.clientHeight})")
            target_local = await target.evaluate("el => ({width:el.clientWidth,height:el.clientHeight})")
            source_values = (arguments.get("source_x"), arguments.get("source_y"))
            target_values = (arguments.get("target_x"), arguments.get("target_y"))
            if (source_values[0] is None) != (source_values[1] is None):
                raise ValueError("source_x and source_y must be supplied together")
            if (target_values[0] is None) != (target_values[1] is None):
                raise ValueError("target_x and target_y must be supplied together")
            source_position = None
            target_position = None
            if source_values[0] is not None:
                source_position = {"x": float(source_values[0]), "y": float(source_values[1])}
                if not (0 <= source_position["x"] <= source_local["width"] and 0 <= source_position["y"] <= source_local["height"]):
                    raise ValueError("source drag position must be inside the source element-local box")
            if target_values[0] is not None:
                target_position = {"x": float(target_values[0]), "y": float(target_values[1])}
                if not (0 <= target_position["x"] <= target_local["width"] and 0 <= target_position["y"] <= target_local["height"]):
                    raise ValueError("drop position must be inside the destination element-local box")
            drag_options: dict[str, Any] = {"timeout": timeout}
            if source_position is not None:
                drag_options["source_position"] = source_position
            if target_position is not None:
                drag_options["target_position"] = target_position
            # A previous page transition or aborted drag can leave Chromium's synthetic pointer
            # pressed without surfacing an exception. Normalize it before every independent drag.
            await page.mouse.up()
            await source.drag_to(target, **drag_options)
            await page.wait_for_timeout(150)
            action_observation = {
                "kind": "drag",
                "source": {key: round(float(value), 2) for key, value in source_box.items()},
                "target": {key: round(float(value), 2) for key, value in target_box.items()},
                "source_local_size": source_local,
                "target_local_size": target_local,
                "destination_selector": str(arguments.get("target_selector") or ""),
                "requested_source_position": source_position,
                "requested_destination_position": target_position,
            }
        elif action == "scroll":
            delta_x = int(arguments.get("delta_x") or 0)
            delta_y = int(arguments.get("delta_y") or 0)
            if delta_x == 0 and delta_y == 0:
                raise ValueError("delta_x or delta_y is required for scroll")
            selector = str(arguments.get("selector") or "").strip()
            text = str(arguments.get("text") or "").strip()
            if selector or text:
                scroll_target = await self._target_locator(page, arguments, action)
                before = await scroll_target.evaluate(
                    "el => ({scroll_left:el.scrollLeft,scroll_top:el.scrollTop,"
                    "scroll_width:el.scrollWidth,scroll_height:el.scrollHeight,"
                    "client_width:el.clientWidth,client_height:el.clientHeight})"
                )
                await scroll_target.evaluate(
                    "(el,d) => el.scrollBy({left:d.x,top:d.y,behavior:'auto'})",
                    {"x": delta_x, "y": delta_y},
                )
                target_name = selector or f"text:{text[:120]}"

                async def read_scroll_state() -> dict[str, Any]:
                    return await scroll_target.evaluate(
                        "el => ({scroll_left:el.scrollLeft,scroll_top:el.scrollTop,"
                        "scroll_width:el.scrollWidth,scroll_height:el.scrollHeight,"
                        "client_width:el.clientWidth,client_height:el.clientHeight})"
                    )
            else:
                before = await page.evaluate(
                    "() => ({scroll_left:scrollX,scroll_top:scrollY,"
                    "scroll_width:document.documentElement.scrollWidth,"
                    "scroll_height:document.documentElement.scrollHeight,"
                    "client_width:innerWidth,client_height:innerHeight})"
                )
                viewport = page.viewport_size or current_viewport
                await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
                await page.mouse.wheel(delta_x, delta_y)
                target_name = "document"

                async def read_scroll_state() -> dict[str, Any]:
                    return await page.evaluate(
                        "() => ({scroll_left:scrollX,scroll_top:scrollY,"
                        "scroll_width:document.documentElement.scrollWidth,"
                        "scroll_height:document.documentElement.scrollHeight,"
                        "client_width:innerWidth,client_height:innerHeight})"
                    )

            stable_samples = 0
            previous: dict[str, Any] | None = None
            after = before
            for _ in range(40):
                await page.wait_for_timeout(50)
                after = await read_scroll_state()
                position = (round(float(after["scroll_left"]), 2), round(float(after["scroll_top"]), 2))
                old_position = None if previous is None else (
                    round(float(previous["scroll_left"]), 2),
                    round(float(previous["scroll_top"]), 2),
                )
                stable_samples = stable_samples + 1 if position == old_position else 0
                previous = after
                if stable_samples >= 3:
                    break
            action_observation = {
                "kind": "scroll",
                "target": target_name,
                "before": before,
                "after": after,
                "settled": stable_samples >= 3,
                "moved": (
                    round(float(after["scroll_left"]), 2),
                    round(float(after["scroll_top"]), 2),
                ) != (
                    round(float(before["scroll_left"]), 2),
                    round(float(before["scroll_top"]), 2),
                ),
                "at_horizontal_start": float(after["scroll_left"]) <= 0,
                "at_vertical_start": float(after["scroll_top"]) <= 0,
                "at_horizontal_end": float(after["scroll_left"]) >= max(
                    0, float(after["scroll_width"]) - float(after["client_width"]) - 1
                ),
                "at_vertical_end": float(after["scroll_top"]) >= max(
                    0, float(after["scroll_height"]) - float(after["client_height"]) - 1
                ),
            }
        elif action == "set_slider":
            locator = await self._target_locator(page, arguments, action)
            if "value" not in arguments:
                raise ValueError("value is required for set_slider")
            action_observation = await locator.evaluate(
                """(el,requested) => {
                    if (!(el instanceof HTMLInputElement) || el.type !== 'range') {
                        throw new Error('set_slider requires an input[type=range] target');
                    }
                    const before=Number(el.value);
                    el.value=String(requested);
                    el.dispatchEvent(new Event('input',{bubbles:true}));
                    el.dispatchEvent(new Event('change',{bubbles:true}));
                    return {kind:'slider',requested_value:Number(requested),before_value:before,
                        actual_value:Number(el.value),min:Number(el.min||0),max:Number(el.max||100),
                        step:el.step==='any'?null:Number(el.step||1)};
                }""",
                float(arguments["value"]),
            )
        elif action == "wait":
            locator = await self._target_locator(page, arguments, action)
            await locator.wait_for(state="visible", timeout=timeout)
        elif action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=timeout)
        elif action == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=timeout)
        else:
            raise ValueError(f"Unsupported background_browser action: {action}")
        current_url = page.url
        if current_url.startswith(("http://", "https://", "file://")):
            await self._validate_navigation_url(current_url, context)
        links = await page.locator("a").evaluate_all(
            "els => els.slice(0,50).map(a => ({text:(a.innerText||'').trim(), href:a.href}))"
        )
        interactive_elements = await page.locator(
            "a,button,input,textarea,select,[role=button],[role=link],[role=checkbox],[tabindex]"
        ).evaluate_all(
            """els => {
            const selectorFor=(el) => {
                if(!el || !(el instanceof Element)) return '';
                const esc=(value) => (window.CSS && CSS.escape) ? CSS.escape(value) : value;
                if(el.id) return '#'+esc(el.id);
                const test=el.getAttribute('data-testid');
                if(test) return '[data-testid="'+test.replace(/"/g,'\\"')+'"]';
                return el.tagName.toLowerCase();
            };
            const geometryFor=(el) => {
                const r=el.getBoundingClientRect();
                let left=Math.max(0,r.left), top=Math.max(0,r.top);
                let right=Math.min(innerWidth,r.right), bottom=Math.min(innerHeight,r.bottom);
                const clippedBy=[]; const scrollContainers=[];
                for(let p=el.parentElement;p;p=p.parentElement){
                    const ps=getComputedStyle(p); const pr=p.getBoundingClientRect();
                    const clipX=/(auto|scroll|hidden|clip)/.test(ps.overflowX);
                    const clipY=/(auto|scroll|hidden|clip)/.test(ps.overflowY);
                    if(clipX||clipY){
                        clippedBy.push(selectorFor(p));
                        if(clipX){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}
                        if(clipY){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}
                    }
                    if(p.scrollHeight>p.clientHeight+1 || p.scrollWidth>p.clientWidth+1){
                        scrollContainers.push({selector:selectorFor(p),scroll_left:p.scrollLeft,
                            scroll_top:p.scrollTop,scroll_width:p.scrollWidth,scroll_height:p.scrollHeight,
                            client_width:p.clientWidth,client_height:p.clientHeight});
                    }
                }
                const visibleArea=Math.max(0,right-left)*Math.max(0,bottom-top);
                const area=Math.max(0,r.width)*Math.max(0,r.height);
                const cx=r.left+r.width/2, cy=r.top+r.height/2;
                const centerInViewport=cx>=0&&cy>=0&&cx<innerWidth&&cy<innerHeight;
                const hit=centerInViewport?document.elementFromPoint(cx,cy):null;
                const obscured=Boolean(hit && hit!==el && !el.contains(hit));
                return {coordinate_space:'viewport-css-pixels',element_local_size:{width:el.clientWidth,height:el.clientHeight},
                    document_bbox:{x:Math.round((r.left+scrollX)*10)/10,y:Math.round((r.top+scrollY)*10)/10,
                        width:Math.round(r.width*10)/10,height:Math.round(r.height*10)/10},
                    visible_ratio:area?Math.round(visibleArea/area*1000)/1000:0,
                    center:{x:Math.round(cx*10)/10,y:Math.round(cy*10)/10},
                    center_in_viewport:centerInViewport,center_obscured:obscured,
                    obscured_by:obscured?selectorFor(hit):'',clipped_by:clippedBy.slice(0,8),
                    scroll_containers:scrollContainers.slice(0,8)};
            };
            const rank=(el) => {
                const r=el.getBoundingClientRect(),s=getComputedStyle(el),tag=el.tagName.toLowerCase();
                const interactive=/^(a|button|input|textarea|select)$/.test(tag)||Boolean(el.getAttribute('role')||el.tabIndex>=0);
                const inViewport=r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;
                return (el.id?120:0)+(el.getAttribute('data-testid')?100:0)+(interactive?80:0)+(inViewport?40:0)+(/fixed|sticky/.test(s.position)?30:0);
            };
            return els.filter(el => {
                const r=el.getBoundingClientRect();
                const s=getComputedStyle(el);
                return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';
            }).sort((a,b)=>rank(b)-rank(a)).slice(0,80).map((el,index) => {
                const tag=el.tagName.toLowerCase();
                const esc=(value) => (window.CSS && CSS.escape) ? CSS.escape(value) : value;
                let selector='';
                if(el.id) selector='#'+esc(el.id);
                else if(el.getAttribute('data-testid')) selector='[data-testid="'+el.getAttribute('data-testid').replace(/"/g,'\\"')+'"]';
                else if(el.getAttribute('name')) selector=tag+'[name="'+el.getAttribute('name').replace(/"/g,'\\"')+'"]';
                else selector=tag+':nth-of-type('+(Array.from(el.parentElement?.children||[]).filter(x=>x.tagName===el.tagName).indexOf(el)+1)+')';
                const r=el.getBoundingClientRect();
                const s=getComputedStyle(el);
                return {index,tag,type:el.getAttribute('type')||'',role:el.getAttribute('role')||'',
                    text:(el.innerText||el.value||'').trim().slice(0,180),
                    label:(el.getAttribute('aria-label')||el.getAttribute('title')||'').trim().slice(0,180),
                    selector,disabled:Boolean(el.disabled),
                    range:el instanceof HTMLInputElement && el.type==='range' ?
                        {min:Number(el.min||0),max:Number(el.max||100),step:el.step==='any'?null:Number(el.step||1),value:Number(el.value)} : null,
                    bbox:{x:Math.round(r.x*10)/10,y:Math.round(r.y*10)/10,
                        width:Math.round(r.width*10)/10,height:Math.round(r.height*10)/10},
                    computed_style:{display:s.display,visibility:s.visibility,color:s.color,
                        background_color:s.backgroundColor,font_size:s.fontSize,
                        overflow_x:s.overflowX,overflow_y:s.overflowY,opacity:s.opacity,
                        pointer_events:s.pointerEvents,z_index:s.zIndex,transform:s.transform},
                    geometry:geometryFor(el)};
            });}"""
        )
        layout_elements = await page.locator("body *").evaluate_all(
            """els => {
            const selectorFor=(el) => {
                if(!el || !(el instanceof Element)) return '';
                const esc=(value) => (window.CSS && CSS.escape) ? CSS.escape(value) : value;
                return el.id ? '#'+esc(el.id) : el.tagName.toLowerCase();
            };
            const geometryFor=(el) => {
                const r=el.getBoundingClientRect();
                let left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
                const clippedBy=[]; const scrollContainers=[];
                for(let p=el.parentElement;p;p=p.parentElement){
                    const ps=getComputedStyle(p); const pr=p.getBoundingClientRect();
                    const clipX=/(auto|scroll|hidden|clip)/.test(ps.overflowX);
                    const clipY=/(auto|scroll|hidden|clip)/.test(ps.overflowY);
                    if(clipX||clipY){clippedBy.push(selectorFor(p));if(clipX){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}if(clipY){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}}
                    if(p.scrollHeight>p.clientHeight+1 || p.scrollWidth>p.clientWidth+1){scrollContainers.push({selector:selectorFor(p),scroll_left:p.scrollLeft,scroll_top:p.scrollTop,scroll_width:p.scrollWidth,scroll_height:p.scrollHeight,client_width:p.clientWidth,client_height:p.clientHeight});}
                }
                const area=Math.max(0,r.width)*Math.max(0,r.height),visibleArea=Math.max(0,right-left)*Math.max(0,bottom-top);
                const cx=r.left+r.width/2,cy=r.top+r.height/2,inside=cx>=0&&cy>=0&&cx<innerWidth&&cy<innerHeight;
                const hit=inside?document.elementFromPoint(cx,cy):null,obscured=Boolean(hit&&hit!==el&&!el.contains(hit));
                return {coordinate_space:'viewport-css-pixels',element_local_size:{width:el.clientWidth,height:el.clientHeight},document_bbox:{x:Math.round((r.left+scrollX)*10)/10,y:Math.round((r.top+scrollY)*10)/10,width:Math.round(r.width*10)/10,height:Math.round(r.height*10)/10},visible_ratio:area?Math.round(visibleArea/area*1000)/1000:0,center:{x:Math.round(cx*10)/10,y:Math.round(cy*10)/10},center_in_viewport:inside,center_obscured:obscured,obscured_by:obscured?selectorFor(hit):'',clipped_by:clippedBy.slice(0,8),scroll_containers:scrollContainers.slice(0,8)};
            };
            const rank=(el) => {const r=el.getBoundingClientRect(),s=getComputedStyle(el),tag=el.tagName.toLowerCase();const interactive=/^(a|button|input|textarea|select)$/.test(tag)||Boolean(el.getAttribute('role')||el.tabIndex>=0);const inViewport=r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth;const scrollable=el.scrollHeight>el.clientHeight+1||el.scrollWidth>el.clientWidth+1;return (el.id?120:0)+(el.getAttribute('data-testid')?100:0)+(interactive?80:0)+(inViewport?40:0)+(scrollable?35:0)+(/fixed|sticky/.test(s.position)?30:0);};
            return els.filter(el => {
                const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
                if(r.width<=0 || r.height<=0 || s.visibility==='hidden' || s.display==='none') return false;
                const text=(el.innerText||el.value||'').trim();
                const hasIdentity=Boolean(el.id || el.className || el.getAttribute('role') || el.getAttribute('aria-label'));
                const styled=s.backgroundColor!=='rgba(0, 0, 0, 0)' || s.borderStyle!=='none' || s.position!=='static';
                return hasIdentity || styled || (text && text.length<=240);
            }).sort((a,b)=>rank(b)-rank(a)).slice(0,200).map(el => {
                const r=el.getBoundingClientRect(); const s=getComputedStyle(el);
                return {tag:el.tagName.toLowerCase(),id:el.id||'',
                    classes:typeof el.className==='string'?el.className.slice(0,240):'',
                    role:el.getAttribute('role')||'',
                    text:(el.innerText||el.value||'').trim().slice(0,240),
                    bbox:{x:Math.round(r.x*10)/10,y:Math.round(r.y*10)/10,
                        width:Math.round(r.width*10)/10,height:Math.round(r.height*10)/10},
                    computed_style:{display:s.display,position:s.position,visibility:s.visibility,
                        color:s.color,background_color:s.backgroundColor,border:s.border,
                        border_radius:s.borderRadius,font_size:s.fontSize,
                         overflow_x:s.overflowX,overflow_y:s.overflowY,opacity:s.opacity,
                         pointer_events:s.pointerEvents,z_index:s.zIndex,transform:s.transform},
                    geometry:geometryFor(el)};
            });}"""
        )
        # Standalone SVG/XML documents have no HTML body. Waiting for one
        # turns a successful navigation into a spurious 30-second timeout.
        raw_body_text = await page.evaluate("""() => {
            const root = document.body || document.documentElement;
            if (!root) return '';
            if (typeof root.innerText === 'string') return root.innerText;
            const copy = root.cloneNode(true);
            copy.querySelectorAll('script,style,template').forEach(el => el.remove());
            return copy.textContent || '';
        }""")
        body_text = raw_body_text[:20_000]
        result: dict[str, Any] = {
            "title": await page.title(),
            "url": current_url,
            "text": body_text,
            "text_length": len(raw_body_text),
            "text_truncated": len(raw_body_text) > len(body_text),
            "links": links,
            "interactive_elements": interactive_elements,
            "layout_elements": layout_elements,
            "execution_mode": "background",
            "foreground_used": False,
            "browser_ui_opened": False,
            "isolated_profile": True,
            "viewport": dict(page.viewport_size or current_viewport),
            "viewport_changed": viewport_changed,
            "runtime": (
                "elren-packaged"
                if self._packaged_browser_executable() is not None
                else "system-headless-fallback"
            ),
        }
        downloads = self._downloads.get(context.task_id)
        if downloads is not None:
            # Keep download evidence ahead of large DOM/layout payloads so the
            # engine's bounded tool serializer cannot bury the saved path.
            result = {'downloads': await downloads.collect(), **result}
        if action_observation is not None:
            result["action_observation"] = action_observation
        if action == "screenshot" or bool(arguments.get("capture")):
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = self.screenshot_dir / (
                f"elren-browser-{context.task_id[:12]}-{int(time.time() * 1000)}.png"
            )
            # Chromium/Playwright's full-page sizing assumes an HTML body;
            # standalone SVG must use its rendered viewport instead.
            has_html_body = await page.evaluate("() => !!document.body")
            await page.screenshot(
                path=str(screenshot_path),
                full_page=bool(arguments.get("full_page", True)) and has_html_body,
            )
            result["screenshot"] = str(screenshot_path)
            result["screenshot_url"] = f"/screenshots/{screenshot_path.name}"
            result["visual_review_required"] = True
            result["recommended_next_tool"] = "vision"
        if action == "search":
            result["query"] = query
            result["results"] = search_results
            result["search_provider"] = search_provider
            result["programmatic_search"] = True
        return result

    async def cleanup(self, context: ToolContext) -> None:
        downloads = self._downloads.pop(context.task_id, None)
        session = self._sessions.pop(context.task_id, None)
        try:
            if downloads:
                await downloads.close()
        finally:
            if session:
                browser, _page = session
                await browser.close()
        if not self._sessions and self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
