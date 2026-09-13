from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "deepdesk" / "static" / "app.js"
INDEX_HTML = ROOT / "deepdesk" / "static" / "index.html"
EDITORIAL_CSS = ROOT / "deepdesk" / "static" / "editorial-ui.css"
LIST_FILTER_ICON = ROOT / "deepdesk" / "static" / "icons" / "list-filter.svg"
LUCIDE_LICENSE = ROOT / "deepdesk" / "static" / "icons" / "LICENSE-lucide.txt"


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening = source.index(") {", start) + 2
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Could not extract JavaScript function {name}")


def _media_source(source: str, query: str) -> str:
    start = source.index(f"@media ({query})")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Could not extract CSS media query {query}")


def _subagent_source(source: str) -> str:
    return source[
        source.index("const SUBAGENT_STATUS_ORDER") : source.index("function artifactFileExtension")
    ]


def test_header_toggle_is_contextual_accessible_and_in_the_expected_order() -> None:
    index = INDEX_HTML.read_text("utf-8")
    header = index[index.index('<div class="header-actions">') : index.index("</header>")]
    toggle = re.search(r'<button\s+id="toggleSubagents"[^>]*>', header)

    assert toggle is not None
    toggle_tag = toggle.group(0)
    assert re.search(r"\shidden(?:\s|>)", toggle_tag)
    assert 'aria-expanded="false"' in toggle_tag
    assert 'aria-controls="subagentPanel"' in toggle_tag
    assert 'aria-haspopup="dialog"' in toggle_tag
    assert 'aria-label="打开子智能体"' in toggle_tag
    assert header.index('id="toggleSubagents"') < header.index('id="toggleArtifactInspector"')
    assert header.index('id="toggleArtifactInspector"') < header.index('id="openSettings"')
    assert header.index('id="openSettings"') < header.index('id="languageToggle"')

    panel = re.search(r'<section\s+id="subagentPanel"[^>]*>', index)
    assert panel is not None
    panel_tag = panel.group(0)
    assert 'popover="auto"' in panel_tag
    assert 'role="dialog"' in panel_tag
    assert 'aria-labelledby="subagentPanelTitle"' in panel_tag
    assert 'aria-describedby="subagentSummary"' in panel_tag
    assert 'id="subagentSummary" role="status" aria-live="polite" aria-atomic="true"' in index
    assert 'id="subagentList" class="subagent-list" role="list"' in index


def test_toggle_uses_a_bundled_lucide_list_filter_icon() -> None:
    index = INDEX_HTML.read_text("utf-8")
    css = EDITORIAL_CSS.read_text("utf-8")
    svg = LIST_FILTER_ICON.read_text("utf-8")
    license_text = LUCIDE_LICENSE.read_text("utf-8")

    assert '<span class="app-symbol app-symbol-list-filter" aria-hidden="true"></span>' in index
    assert '.app-symbol-list-filter { --app-icon: url("/static/icons/list-filter.svg?v=1"); }' in css
    assert "http://" not in css[css.index(".app-symbol-list-filter") :][:160]
    assert "https://" not in css[css.index(".app-symbol-list-filter") :][:160]
    assert 'viewBox="0 0 24 24"' in svg
    assert 'fill="none"' in svg
    assert 'stroke="currentColor"' in svg
    assert 'stroke-linecap="round"' in svg
    assert 'stroke-linejoin="round"' in svg
    assert len(re.findall(r"<path\b", svg)) == 3
    assert "Lucide Icons and Contributors" in license_text
    assert "ISC License" in license_text


def test_popover_is_only_revealed_by_the_user_and_has_every_close_path() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    initialize = _function_source(source, "initializeSubagentPanel")
    show = _function_source(source, "showSubagentPanel")
    hide = _function_source(source, "hideSubagentPanel")
    render = _function_source(source, "renderSubagentPanel")

    # `auto` supplies native light-dismiss and Escape behavior where reliable;
    # JS keeps the same outside-click path even in embedded WebViews that expose
    # the Popover API without consistently performing native light-dismiss.
    assert '<section id="subagentPanel" class="subagent-panel" popover="auto"' in index
    assert 'toggle.addEventListener("click"' in initialize
    assert "if (subagentPanelIsOpen()) hideSubagentPanel" in initialize
    assert "else showSubagentPanel();" in initialize
    assert '$("#closeSubagentPanel")?.addEventListener("click"' in initialize
    assert 'if (event.key !== "Escape") return;' in initialize
    assert "event.preventDefault();" in initialize
    assert 'document.addEventListener("pointerdown"' in initialize
    assert 'if (!subagentPanelIsOpen()) return;' in initialize
    assert 'typeof panel.showPopover === "function"' not in initialize
    assert "panel.contains(event.target) || toggle.contains(event.target)" in initialize
    assert "hideSubagentPanel();" in initialize
    assert 'panel.addEventListener("toggle"' in initialize
    assert 'event.newState === "open"' in initialize
    assert 'toggle.setAttribute("aria-expanded"' in _function_source(source, "syncSubagentToggle")
    assert "panel.showPopover()" in show
    assert "panel.hidePopover()" in hide

    # Rendering task updates may update a panel that the user left open, but it
    # must never reveal a closed panel on its own.
    assert source.count("showSubagentPanel();") == 1
    assert "showSubagentPanel" not in render
    assert "showPopover" not in render
    assert 'classList.add("is-open")' not in render


def test_no_run_means_no_toggle_and_only_task_subagent_runs_are_rendered() -> None:
    source = APP_JS.read_text("utf-8")
    runs = _function_source(source, "subagentRuns")
    sync = _function_source(source, "syncSubagentToggle")
    show = _function_source(source, "showSubagentPanel")
    render = _function_source(source, "renderSubagentPanel")

    assert "Array.isArray(task?.subagent_runs)" in runs
    assert "return task.subagent_runs" in runs
    assert ".slice(0, 100)" in runs
    assert "events" not in runs
    assert "discussion" not in runs.lower()
    assert "agents" not in runs.lower().replace("subagent", "")
    assert "const runs = subagentRuns(task);" in render
    assert "toggle.hidden = !available;" in sync
    assert "const available = runs.length > 0;" in sync
    assert "if (!panel || !runs.length) return;" in show
    assert "else if (!runs.length) hideSubagentPanel({ keepToggle: false });" in render
    assert "syncSubagentToggle([], false);" in _function_source(source, "initializeSubagentPanel")


def test_statuses_are_visible_sorted_and_bilingual() -> None:
    source = APP_JS.read_text("utf-8")
    statuses = source[
        source.index("const SUBAGENT_STATUS_ORDER") : source.index("function subagentDuration")
    ]
    summary = _function_source(source, "subagentSummaryText")
    details = _function_source(source, "subagentListDetails")

    for status in ("queued", "running", "completed", "failed"):
        assert status in statuses
    for chinese, english in (
        ("排队中", "Queued"),
        ("运行中", "Running"),
        ("已完成", "Completed"),
        ("未完成", "Failed"),
    ):
        assert f'uiText("{chinese}", "{english}")' in statuses
    assert "SUBAGENT_STATUS_ORDER[subagentStatus(left.status)]" in _function_source(
        source, "renderSubagentList"
    )
    assert 'uiText("尚无子智能体", "No specialists yet")' in summary
    assert 'uiText("模型", "Model")' in details
    assert 'uiText("分工", "Assignment")' in details
    assert 'listMarkup("证据", "Evidence", evidence)' in details
    assert 'listMarkup("风险", "Risks", risks)' in details


def test_keyed_dom_updates_preserve_each_details_disclosure_state() -> None:
    source = APP_JS.read_text("utf-8")
    render = _function_source(source, "renderSubagentList")

    assert "const nodesById = new Map();" in render
    assert "nodesById.set(runId, node);" in render
    assert "let node = nodesById.get(item.id);" in render
    assert ".find(" not in render
    assert 'node = document.createElement("details")' in render
    assert "node.dataset.runId = item.id" in render
    assert "const wasOpen = Boolean(node.open);" in render
    assert "if (node.dataset.signature !== item.signature)" in render
    assert "node.innerHTML = item.markup;" in render
    assert "node.dataset.signature = item.signature;" in render
    assert "node.open = wasOpen;" in render
    assert "staleNodes.delete(node);" in render
    assert "list.appendChild(node);" in render
    assert "staleNodes.forEach((node) => node.remove());" in render
    assert render.count("list.innerHTML") == 1  # empty state only, never a live-list rebuild


def test_closed_panel_defers_content_work_but_open_panel_stays_live() -> None:
    source = APP_JS.read_text("utf-8")
    show = _function_source(source, "showSubagentPanel")
    render = _function_source(source, "renderSubagentPanel")
    contents = _function_source(source, "renderSubagentPanelContents")

    assert "renderSubagentPanelContents(runs);" in show
    assert show.index("renderSubagentPanelContents(runs);") < show.index("panel.showPopover()")
    assert "syncSubagentToggle(runs, wasOpen);" in render
    assert render.index("syncSubagentToggle(runs, wasOpen);") < render.index("if (wasOpen) {")
    assert render.count("renderSubagentPanelContents(runs);") == 1
    assert "if (wasOpen) {\n    renderSubagentPanelContents(runs);" in render
    assert "subagentSummaryText(runs)" in contents
    assert '$("#subagentAvatarStrip")' in contents
    assert "renderSubagentList(runs);" in contents


def test_subagent_panel_is_mutually_exclusive_with_artifacts_and_settings() -> None:
    source = APP_JS.read_text("utf-8")
    show_subagents = _function_source(source, "showSubagentPanel")
    show_artifacts = _function_source(source, "showArtifactInspector")
    open_workspace = _function_source(source, "openWorkspacePanel")

    assert "hideArtifactInspector({ dismiss: true });" in show_subagents
    assert "hideSubagentPanel();" in show_artifacts
    assert "hideSubagentPanel();" in open_workspace
    settings_handler = source[source.index('$("#openSettings").onclick') :]
    assert 'openWorkspacePanel("settings")' in settings_handler[:240]


def test_static_and_dynamic_labels_are_complete_in_both_languages() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    sync = _function_source(source, "syncSubagentToggle")
    english = _function_source(source, "applyEnglishInterface")

    assert ">任务分工</span>" in index
    assert '<h2 id="subagentPanelTitle">子智能体</h2>' in index
    assert 'aria-label="关闭子智能体"' in index
    assert 'uiText("关闭子智能体", "Close specialists")' in sync
    assert 'uiText("打开子智能体", "Open specialists")' in sync
    assert 'setText("#subagentPanel .subagent-panel-kicker", "Task delegation")' in english
    assert 'setText("#subagentPanelTitle", "Specialists")' in english
    assert 'setAttr("#closeSubagentPanel", "aria-label", "Close specialists")' in english


def test_compact_breakpoints_forced_colors_and_reduced_motion_cover_subagents() -> None:
    css = EDITORIAL_CSS.read_text("utf-8")
    mobile = _media_source(css, "max-width: 760px")
    compact = _media_source(css, "max-width: 390px")
    narrow = _media_source(css, "max-width: 320px")
    reduced_motion = _media_source(css, "prefers-reduced-motion: reduce")
    forced_colors = _media_source(css, "forced-colors: active")

    assert ".header-subagent-button.settings-icon-button" in mobile
    assert "min-width: 44px" in mobile
    assert ".subagent-panel" in mobile
    assert "width: calc(100vw - 16px)" in mobile
    assert ".header-actions" in compact and "gap: 4px" in compact
    assert ".subagent-panel" in compact and "right: 8px" in compact
    # At 320px the 760/390 rules still cascade; this narrower rule protects the
    # title and frees the header width needed by the four right-side actions.
    assert ".header-copy h1" in narrow
    assert "font-size: 15px" in narrow
    assert "max-width: 100%" in narrow
    assert "transition-duration: .01ms !important" in reduced_motion
    assert "animation-duration: .01ms !important" in reduced_motion
    assert ".subagent-activity-dot" in forced_colors
    assert ".subagent-item-status::before" in forced_colors
    assert "border: 1px solid CanvasText" in forced_colors


def test_subagent_dom_uses_a_safe_display_field_allowlist() -> None:
    source = APP_JS.read_text("utf-8")
    subagents = _subagent_source(source)
    lowered = subagents.lower()

    for forbidden in (
        "system_prompt",
        "systemprompt",
        "reasoning",
        "credential",
        "api_key",
        "apikey",
        "secret",
        "chain_of_thought",
        "raw_messages",
        "tool_arguments",
    ):
        assert forbidden not in lowered

    run_fields = set(re.findall(r"\brun(?:\?\.|\.)([A-Za-z_$][\w$]*)", subagents))
    assert run_fields <= {
        "actual_model",
        "assignment",
        "configured_model",
        "elapsed_ms",
        "evidence",
        "id",
        "name_en",
        "name_zh",
        "preset_id",
        "risks",
        "started_at",
        "status",
        "summary",
    }
    assert "JSON.stringify(run" not in subagents
    assert "Object.entries(run" not in subagents
    assert "Object.values(run" not in subagents
    assert re.search(r"\.\.\.\s*run\b", subagents) is None
