from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk.engine import SYSTEM_PROMPT, AgentEngine
from deepdesk.platform_paths import unicode_font_candidates, windows_program_files, windows_root
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool


def test_windows_paths_follow_environment_instead_of_assuming_c_drive(monkeypatch):
    monkeypatch.setenv("WINDIR", "D:/Windows")
    monkeypatch.setenv("ProgramW6432", "E:/Applications")
    monkeypatch.setenv("ProgramFiles", "E:/Applications")
    monkeypatch.setenv("ProgramFiles(x86)", "F:/Legacy Apps")

    assert windows_root() == Path("D:/Windows")
    assert windows_program_files() == (Path("E:/Applications"), Path("F:/Legacy Apps"))
    assert unicode_font_candidates()[0] == Path("D:/Windows/Fonts/msyh.ttc")


def test_process_manager_uses_relocated_windows_install_roots(monkeypatch):
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.os.name", "nt")
    monkeypatch.setenv("ProgramW6432", "Z:/Programs")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("WINDIR", "Y:/Windows")
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.process_manager.Path.is_file",
        lambda path: str(path).replace("\\", "/")
        == "Z:/Programs/Microsoft/Edge/Application/msedge.exe",
    )

    assert ProcessManagerTool._resolve_application("edge").replace("\\", "/") == (
        "Z:/Programs/Microsoft/Edge/Application/msedge.exe"
    )


@pytest.mark.asyncio
async def test_background_browser_falls_back_from_edge_to_packaged_chromium(monkeypatch):
    attempts: list[str] = []
    expected = object()

    class Chromium:
        async def launch(self, **options):
            label = str(options.get("channel") or "bundled")
            attempts.append(label)
            if options.get("channel"):
                raise RuntimeError("channel unavailable")
            return expected

    tool = BackgroundBrowserTool()
    tool._playwright = SimpleNamespace(chromium=Chromium())
    monkeypatch.setattr("deepdesk.plugins.builtin.background_browser.sys.platform", "win32")

    assert await tool._launch_browser() is expected
    assert attempts == ["bundled"]


def test_background_browser_prefers_package_relative_headless_shell(tmp_path: Path):
    executable = (
        tmp_path / "work" / "browser-runtime" / "chromium-1" / "chrome-headless-shell.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"portable browser")

    tool = BackgroundBrowserTool(workspace=tmp_path)

    assert tool._packaged_browser_executable() == executable.resolve()


def test_background_browser_default_workspace_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    tool = BackgroundBrowserTool()

    assert tool.workspace == Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_packaged_background_browser_captures_without_opening_ui(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="built-in-browser-e2e", workspace=str(root))
    try:
        result = await tool.execute({"action": "screenshot", "full_page": True}, context)
    finally:
        await tool.cleanup(context)

    screenshot = Path(result["screenshot"])
    assert result["runtime"] == "elren-packaged"
    assert result["execution_mode"] == "background"
    assert result["browser_ui_opened"] is False
    assert screenshot.is_file()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_packaged_background_browser_opens_workspace_file_url(tmp_path: Path):
    preview_root = tmp_path / "local-preview"
    preview_root.mkdir()
    (preview_root / "theme.css").write_text("body{background:rgb(250,247,240)}", encoding="utf-8")
    page = preview_root / "index.html"
    page.write_text(
        "<!doctype html><html><head><title>Workspace file preview</title>"
        "<link rel='stylesheet' href='theme.css'></head><body><main id='result'>file protocol ready</main></body></html>",
        encoding="utf-8",
    )
    tool = BackgroundBrowserTool(workspace=tmp_path, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="built-in-file-url", workspace=str(tmp_path))
    try:
        result = await tool.execute(
            {"action": "open", "url": page.as_uri(), "capture": True}, context
        )
    finally:
        await tool.cleanup(context)

    assert result["title"] == "Workspace file preview"
    assert result["url"].startswith("file://")
    assert "file protocol ready" in result["text"]
    assert result["browser_ui_opened"] is False
    assert Path(result["screenshot"]).is_file()


@pytest.mark.asyncio
async def test_background_browser_reports_controls_and_resolves_ambiguous_index(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="built-in-browser-controls", workspace=str(root))
    try:
        _browser, page = await tool._session(context)
        await page.set_content(
            "<style>#responsive{display:grid;grid-template-columns:1fr 1fr;height:20px}"
            "@media(max-width:720px){#responsive{grid-template-columns:1fr}}</style>"
            "<div id='responsive'></div>"
            "<button id='first' onclick=\"document.querySelector('#out').textContent='first'\">Run</button>"
            "<button id='second' onclick=\"document.querySelector('#out').textContent='second'\">Run</button>"
            "<div id='drag-source' draggable='true'>Drag me</div>"
            "<div id='drop-target' ondragover='event.preventDefault()' "
            "ondrop=\"event.preventDefault();document.querySelector('#out').textContent='dropped'\">Drop</div>"
            "<div style='height:1200px'></div>"
            "<p id='out'>idle</p>"
        )
        inspected = await tool.execute({"action": "inspect"}, context)
        assert {item["selector"] for item in inspected["interactive_elements"]} >= {
            "#first", "#second"
        }
        first = next(item for item in inspected["interactive_elements"] if item["selector"] == "#first")
        assert first["bbox"]["width"] > 0
        assert first["computed_style"]["display"] != "none"
        responsive = next(item for item in inspected["layout_elements"] if item["id"] == "responsive")
        assert responsive["bbox"]["width"] > 0
        with pytest.raises(LookupError, match="pass index"):
            await tool.execute({"action": "click", "text": "Run"}, context)
        clicked = await tool.execute(
            {"action": "click", "text": "Run", "index": 1, "capture": True}, context
        )
        assert "second" in clicked["text"]
        assert Path(clicked["screenshot"]).is_file()
        assert clicked["visual_review_required"] is True
        assert clicked["recommended_next_tool"] == "vision"
        hovered = await tool.execute({"action": "hover", "selector": "#first"}, context)
        assert hovered["url"] == page.url
        dragged = await tool.execute(
            {
                "action": "drag",
                "selector": "#drag-source",
                "target_selector": "#drop-target",
            },
            context,
        )
        assert "dropped" in dragged["text"]
        before_scroll = await page.evaluate("scrollY")
        await tool.execute({"action": "scroll", "delta_y": 600}, context)
        assert await page.evaluate("scrollY") > before_scroll
        resized = await tool.execute(
            {
                "action": "resize",
                "viewport_width": 719,
                "viewport_height": 844,
                "capture": True,
            },
            context,
        )
        assert resized["viewport"] == {"width": 719, "height": 844}
        assert resized["viewport_changed"] is True
        assert await page.locator("#responsive").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns.split(' ').length"
        ) == 1
    finally:
        await tool.cleanup(context)


@pytest.mark.asyncio
async def test_background_browser_search_uses_bounded_backend_and_renders_clickable_results(
    tmp_path: Path, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")

    async def fake_search(query: str, limit: int):
        assert query == "Elren documentation"
        assert limit == 20
        return {
            "provider": "test-search",
            "results": [
                {
                    "title": "Elren docs",
                    "url": "https://example.com/docs",
                    "snippet": "Official documentation",
                }
            ],
        }

    monkeypatch.setattr(tool.validator, "_search", fake_search)
    context = ToolContext(task_id="built-in-browser-search", workspace=str(root))
    try:
        result = await tool.execute(
            {"action": "search", "query": "Elren documentation"}, context
        )
        assert result["search_provider"] == "test-search"
        assert result["programmatic_search"] is True
        assert result["results"][0]["url"] == "https://example.com/docs"
        assert any(
            item["selector"].startswith("a")
            and item["text"] == "Elren docs"
            for item in result["interactive_elements"]
        )
    finally:
        await tool.cleanup(context)


def test_engine_redirects_hand_rolled_cdp_but_not_project_browser_tests():
    assert AgentEngine._should_redirect_browser_automation(
        "process_manager",
        {
            "action": "launch",
            "application": "msedge.exe",
            "arguments": ["--headless", "--remote-debugging-port=9222"],
        },
    )
    assert AgentEngine._should_redirect_browser_automation(
        "shell", {"command": "chrome.exe --headless --remote-debugging-port=0"}
    )
    assert AgentEngine._should_redirect_browser_automation(
        "sandbox",
        {"script": "new ClientWebSocket(); fetch('http://127.0.0.1:9222/json/version')"},
    )
    assert not AgentEngine._should_redirect_browser_automation(
        "shell", {"command": "npm run test:playwright"}
    )
    assert "mandatory first choice" in SYSTEM_PROMPT
    assert AgentEngine._verification_result(
        "background_browser",
        {"action": "open", "capture": True},
        {"ok": True, "result": {"screenshot": "capture.png"}},
    ) is False
    assert AgentEngine._verification_result(
        "vision",
        {"image_path": "capture.png", "mode": "semantic"},
        {
            "ok": True,
            "result": {
                "description": "No overlap or clipping detected",
                "semantic_verified": True,
            },
        },
    ) is True
    assert AgentEngine._verification_result(
        "vision",
        {"image_path": "capture.png", "mode": "semantic"},
        {
            "ok": True,
            "result": {
                "description": "OCR text only",
                "semantic_verified": False,
                "semantic_limited": True,
            },
        },
    ) is False


def test_first_party_runtime_has_no_fixed_user_profile_paths():
    root = Path(__file__).resolve().parents[1]
    files = [
        *root.joinpath("deepdesk").rglob("*.py"),
        *root.joinpath("launcher").glob("*.py"),
        *root.joinpath("launcher").glob("*.ps1"),
        root / "start.ps1",
    ]
    fixed_profile = re.compile(r"(?i)(?:[a-z]:[/\\]users|/users)/[^/\\\s\"']+")
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert fixed_profile.search(source) is None, path
