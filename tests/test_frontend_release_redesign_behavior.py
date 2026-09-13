from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "deepdesk" / "static" / "app.js"
INDEX_HTML = ROOT / "deepdesk" / "static" / "index.html"
EDITORIAL_CSS = ROOT / "deepdesk" / "static" / "editorial-ui.css"
THEME_CSS = ROOT / "deepdesk" / "static" / "theme.css"


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


def _run_javascript(expression: str, *function_names: str, prelude: str = ""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior regressions")
    source = APP_JS.read_text("utf-8")
    definitions = "\n".join(_function_source(source, name) for name in function_names)
    script = f"{prelude}\n{definitions}\nprocess.stdout.write(JSON.stringify({expression}));"
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("english", [True, False])
@pytest.mark.parametrize("source,expected", [
    ("Apple Vision VNRecognizeTextRequest", "Apple Vision OCR"),
    ("Windows.Media.Ocr", "Windows OCR"),
    ("", None),
])
def test_native_ocr_labels_follow_backend_not_browser(source, expected, english):
    fallback = "Local OCR" if english else "本机 OCR"
    result = _run_javascript(
        "nativeOcrLabel(" + json.dumps({"local_ocr": {"source": source}}) + ")",
        "nativeOcrLabel", prelude=f"const isEnglish = () => {str(english).lower()};",
    )
    assert result == (expected or fallback)
    code = APP_JS.read_text("utf-8")
    assert "nativeOcrLabel(vision)" in _function_source(code, "renderVisionSettings")
    assert "nativeOcrLabel(vision)" in _function_source(code, "visionCapabilityMarkup")


def test_welcome_examples_are_removed_and_settings_is_a_header_icon() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    sidebar = index.split("</aside>", 1)[0]
    header = index.split("<header>", 1)[1].split("</header>", 1)[0]

    assert 'class="suggestions"' not in index
    assert 'document.querySelectorAll(".suggestions button")' not in source
    assert 'id="openSettings"' not in sidebar
    assert header.index('id="openSettings"') < header.index('id="languageToggle"')
    assert '<button id="openSettings" class="header-settings-button settings-icon-button"' in header
    assert '<span>设置</span>' not in header
    assert 'class="runtime-bridge" hidden aria-hidden="true"' in index
    assert "本地模型已就绪" not in sidebar


def test_empty_composer_disables_send_until_there_is_text() -> None:
    source = APP_JS.read_text("utf-8")
    sync = _function_source(source, "syncComposerAction")
    prompt_input = source.split('$("#prompt").addEventListener("input"', 1)[1].split(
        'window.addEventListener("resize"', 1
    )[0]

    assert 'button.disabled = !acceptsFollowUps || !prompt.value.trim()' in sync
    assert "syncComposerAction();" in prompt_input
    assert source.index("resizePromptInput();\nsyncComposerAction();") < source.index(
        "const initialTaskId = new URLSearchParams"
    )


def test_history_accessible_name_is_title_plus_status_and_never_over_120_chars() -> None:
    result = _run_javascript(
        "historyItemAriaLabel({title:'',prompt:'非常长的任务'.repeat(40),status:'running'})",
        "taskStatusLabel",
        "historyItemAriaLabel",
        prelude="const isEnglish=()=>false; const uiText=(zh,en)=>zh;",
    )

    assert len(result) <= 120
    assert result.endswith(" · 执行中")
    assert "…" in result

    source = APP_JS.read_text("utf-8")
    assert 'aria-label="${escapeHtml(historyItemAriaLabel(task))}"' in source


def test_known_cancel_and_error_messages_are_localized_without_task_cancelled_copy() -> None:
    result = _run_javascript(
        "[localizeKnownSystemMessage('Task cancelled'),localizeKnownSystemMessage('Task error')]",
        "localizeKnownSystemMessage",
        prelude="const isEnglish=()=>false;",
    )
    assert result == ["任务已停止", "任务出错"]

    english = _run_javascript(
        "localizeKnownSystemMessage('Task cancelled')",
        "localizeKnownSystemMessage",
        prelude="const isEnglish=()=>true;",
    )
    assert english == "Task stopped."

    gateway_error = (
        "Gateway call failed: GatewayExplicitAuthRequiredError: gateway or override "
        "requires explicit credentials"
    )
    gateway_zh = _run_javascript(
        f"localizeKnownSystemMessage({gateway_error!r})",
        "localizeKnownSystemMessage",
        prelude="const isEnglish=()=>false;",
    )
    gateway_en = _run_javascript(
        f"localizeKnownSystemMessage({gateway_error!r})",
        "localizeKnownSystemMessage",
        prelude="const isEnglish=()=>true;",
    )
    assert gateway_zh == "OpenClaw Gateway 需要显式身份凭据（token 或 password），请先在 OpenClaw 配置中完成设置。"
    assert gateway_en == "OpenClaw Gateway requires an explicit token or password. Add it to the OpenClaw configuration, then probe again."

    source = APP_JS.read_text("utf-8")
    terminal = _function_source(source, "terminalStatePresentation")
    renderer = _function_source(source, "renderEvent")
    assert "const stoppedDetail = localizeKnownSystemMessage(task.error)" in terminal
    assert 'event.type === "error" || event.type === "cancelled"' in renderer


def test_internal_event_names_are_replaced_with_human_labels_and_delivery_copy() -> None:
    result = _run_javascript(
        "[localizedEventTypeLabel('telegram_delivery'),localizedEventTypeLabel('future_internal_event')]",
        "localizedEventTypeLabel",
        prelude="const isEnglish=()=>false; const uiText=(zh,en)=>zh;",
    )
    assert result == ["Telegram 发送状态", "系统进展"]

    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderEvent")
    assert 'telegram_delivery: [appSymbol("send"), localizedEventTypeLabel(event.type)]' in renderer
    assert 'remoteDeliveryEventMessage(event)' in renderer
    assert '|| [appSymbol("bolt"), event.type]' not in renderer


def test_output_limit_help_is_provider_neutral_and_updates_with_settings() -> None:
    source = APP_JS.read_text("utf-8")
    help_source = _function_source(source, "updateOutputTokenHelp")
    load_settings = _function_source(source, "loadSettings")

    assert "当前所选模型及供应商公布的输出上限" in help_source
    assert "selected model and provider's published output limit" in help_source
    assert "DeepSeek V4" not in help_source
    assert "hydrateSettingsPreferences(settings)" in load_settings
    assert "updateOutputTokenHelp()" in _function_source(source, "hydrateSettingsPreferences")
    assert source.count("updateOutputTokenHelp();") >= 3


def test_new_navigation_safely_routes_to_existing_surfaces_with_feedback() -> None:
    source = APP_JS.read_text("utf-8")
    wiring = _function_source(source, "initializePrimaryNavigation")
    history = _function_source(source, "showHistoryNavigation")

    for navigation_id in (
        "navChats",
        "navPlugins",
    ):
        assert f'$("#{navigation_id}")?.addEventListener' in wiring
    assert 'id="navRunning"' not in INDEX_HTML.read_text("utf-8")
    assert '#navRunning' not in source
    assert '<option value="running">' in INDEX_HTML.read_text("utf-8")
    for removed in ("navArtifacts", "navAutomations"):
        assert removed not in INDEX_HTML.read_text("utf-8")
        assert removed not in source
    assert '$("#openSettings").onclick = () => openWorkspacePanel("settings")' in source
    for panel in ("artifacts", "schedules"):
        assert f'data-panel="{panel}"' in INDEX_HTML.read_text("utf-8")
    assert '$("#openCapabilities")?.click()' in wiring
    assert 'filter.value = status' in history
    assert 'syncSettingsSelectWidget(filter)' in history
    assert 'aria-busy", "true"' in history
    assert 'aria-busy", "false"' in history


def test_new_task_translation_preserves_the_decorative_plus() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    assert '<span aria-hidden="true">＋</span> <span id="newTaskLabel">新任务</span>' in index
    assert 'setText("#newTaskLabel", "New task")' in source
    assert 'setText("#newTask",' not in source


def test_artifact_inspector_uses_real_file_and_ppt_quality_evidence() -> None:
    task = {
        "id": "task-1",
        "attachments": [r"C:\uploads\brief.docx"],
        "events": [
            {
                "id": "quality-1",
                "type": "artifact_quality_verified",
                "data": {
                    "format": "pptx",
                    "report": {
                        "ok": True,
                        "artifacts": [
                            {
                                "path": "outputs/release-deck.pptx",
                                "quality": {
                                    "qa_passed": True,
                                    "slides_checked": 12,
                                    "remaining_issue_count": 0,
                                },
                            }
                        ],
                    },
                },
            }
        ],
    }
    expression = (
        "(()=>{const task="
        + json.dumps(task)
        + ";const items=artifactInspectorItems(task);"
        "const deck=items.find(item=>item.name==='release-deck.pptx');"
        "return {items,check:presentationOverflowCheck(task,deck)};})()"
    )
    result = _run_javascript(
        expression,
        "artifactFileExtension",
        "artifactInspectorItems",
        "presentationOverflowCheck",
    )

    assert {item["name"] for item in result["items"]} == {"brief.docx", "release-deck.pptx"}
    assert result["check"] == {
        "applicable": True,
        "state": "passed",
        "slidesChecked": 12,
        "issueCount": 0,
    }

    unverified = _run_javascript(
        "presentationOverflowCheck({events:[]},{name:'draft.pptx',extension:'pptx'})",
        "artifactFileExtension",
        "presentationOverflowCheck",
    )
    assert unverified["state"] == "not_recorded"

    urls = _run_javascript(
        "[artifactInspectorUrl({path:'outputs/release deck.pptx',source:'quality'}),"
        "artifactInspectorUrl({path:'uploads/input brief.docx',source:'attachment'})]",
        "artifactInspectorUrl",
    )
    assert urls == [
        "/api/artifacts/release%20deck.pptx",
        "/api/uploads/input%20brief.docx",
    ]


def test_artifact_inspector_excludes_web_references_and_prioritizes_deliverables() -> None:
    task = {
        "events": [
            {
                "type": "tool_result",
                "data": {
                    "result": (
                        "Source https://news.example/story.html and "
                        "outputs/project/embed_and_verify.py\n"
                        "outputs/project/final-report.html\n"
                        "outputs/project/final-deck.pptx"
                    )
                },
            }
        ],
        "result": "Open `outputs/project/final-report.html` and `outputs/project/final-deck.pptx`.",
    }
    expression = (
        "(()=>{const items=artifactInspectorItems("
        + json.dumps(task)
        + ");return items.map(item=>item.name);})()"
    )
    names = _run_javascript(
        expression,
        "artifactFileExtension",
        "artifactInspectorItems",
    )

    assert names[0] == "final-deck.pptx"
    assert "final-report.html" in names
    assert "embed_and_verify.py" in names
    assert "story.html" not in names


def test_artifact_tabs_and_close_path_are_keyboard_operable() -> None:
    source = APP_JS.read_text("utf-8")
    initialization = _function_source(source, "initializeArtifactInspector")
    activation = _function_source(source, "activateArtifactInspectorTab")
    reset = _function_source(source, "reset")

    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in initialization
    assert 'event.key === "Escape"' in initialization
    assert 'event.key !== "Tab"' in initialization
    assert '$("#closeArtifactInspector")?.addEventListener("click"' in initialization
    assert '$("#toggleArtifactInspector")?.focus({ preventScroll: true })' in initialization
    assert 'candidate.setAttribute("aria-selected"' in activation
    assert "candidate.tabIndex = active ? 0 : -1" in activation
    assert "panel.hidden = !active" in activation
    assert "hideArtifactInspector()" in reset


def test_artifact_inspector_polling_preserves_unchanged_mounts_and_focus() -> None:
    result = _run_javascript(
        "(()=>{let writes=0;let value='';let focused=null;"
        "const mount={get innerHTML(){return value;},set innerHTML(next){writes+=1;value=next;focused=null;}};"
        "const first=updateArtifactInspectorMount(mount,'<a>deck</a>');"
        "focused={id:'open-artifact'};"
        "const unchanged=updateArtifactInspectorMount(mount,'<a>deck</a>');"
        "const focusRetained=focused?.id==='open-artifact';"
        "const changed=updateArtifactInspectorMount(mount,'<a>final deck</a>');"
        "return {first,unchanged,changed,writes,focusRetained,focusAfterChange:focused};})()",
        "updateArtifactInspectorMount",
    )

    assert result == {
        "first": True,
        "unchanged": False,
        "changed": True,
        "writes": 2,
        "focusRetained": True,
        "focusAfterChange": None,
    }

    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderArtifactInspector")
    assert renderer.count("updateArtifactInspectorMount(") == 6
    assert "preview.innerHTML" not in renderer
    assert "source.innerHTML" not in renderer
    assert "checks.innerHTML" not in renderer


def test_capability_dialog_escape_closes_and_returns_focus_to_the_composer() -> None:
    source = APP_JS.read_text("utf-8")
    close_path = _function_source(source, "closeCapabilityDialog")
    focus_path = _function_source(source, "focusMainContentTarget")

    assert 'if (dialog?.open) dialog.close()' in close_path
    assert 'setPrimaryNavigation("navChats")' in close_path
    assert "focusMainContentTarget()" in close_path
    assert 'takeover || $("#prompt") || $("#mobileNav")' in focus_path
    assert '$("#capabilityDialog")?.addEventListener("cancel"' in source
    assert '$("#capabilityDialog")?.addEventListener("keydown"' in source
    assert 'event.key !== "Escape"' in source


def test_dialog_and_mobile_navigation_closures_restore_main_state_and_focus() -> None:
    source = APP_JS.read_text("utf-8")
    close_navigation = _function_source(source, "closeNavigation")
    history_navigation = _function_source(source, "showHistoryNavigation")
    workspace_close = _function_source(source, "requestCloseWorkspaceDialog")
    open_task = source[source.index("async function openTask(") : source.index("function openNavigation(")]

    assert "restoreMainFocus = false" in close_navigation
    assert "restoreMainFocus && wasOpen" in close_navigation
    assert "focusMainContentTarget()" in close_navigation
    assert "closeNavigation({ restoreMainFocus: true })" in history_navigation
    assert "closeNavigation({ restoreMainFocus: true })" in open_task
    assert open_task.index("closeNavigation({ restoreMainFocus: true })") < open_task.index(
        "await api(`/api/tasks/${id}`)"
    )
    assert 'setPrimaryNavigation("navChats")' in workspace_close
    assert "focusMainContentTarget()" in workspace_close


def test_editorial_stylesheet_cache_key_tracks_final_ui_polish() -> None:
    index = INDEX_HTML.read_text("utf-8")
    assert '<link rel="stylesheet" href="/static/editorial-ui.css?v=38" />' in index
    assert "editorial-ui.css?v=15" not in index


def test_agent_team_cards_use_one_warm_editorial_surface() -> None:
    css = EDITORIAL_CSS.read_text("utf-8")

    assert ':root[data-theme] body .discussion-team-row[data-role="leader"]' in css
    assert "var(--editorial-sidebar) 72%" in css
    assert ":root[data-theme] body .discussion-team-row-header" in css
    assert "background: transparent" in css
    assert ".discussion-team-fields .team-select-button" in css
    assert "var(--editorial-paper) 58%" in css


def test_history_status_typography_has_a_stable_windows_line_box() -> None:
    css = EDITORIAL_CSS.read_text("utf-8")

    assert '.history-meta {' in css
    assert 'font-family: "Segoe UI", "Microsoft YaHei UI", "PingFang SC", system-ui, sans-serif;' in css
    assert "min-height: 18px" in css
    assert "font-synthesis: none" in css
    assert "line-height: 1.6" in css
    assert ".history-status {" in css
    assert "align-self: center" in css


def test_history_filtering_uses_aria_busy_without_a_visual_status_row() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    css = EDITORIAL_CSS.read_text("utf-8")
    loader = _function_source(source, "loadTaskHistory")

    assert 'id="historyFilterFeedback"' not in index
    assert 'history.setAttribute("aria-busy", "true")' in loader
    assert 'history.removeAttribute("aria-busy")' in loader
    assert "setHistoryFilterFeedback" not in source
    assert ".history-filter-feedback" not in css


def test_new_task_clears_persistent_history_filters_before_resetting() -> None:
    source = APP_JS.read_text("utf-8")
    starter = _function_source(source, "startBlankTask")

    assert 'const search = $("#historySearch")' in starter
    assert 'const filter = $("#historyStatus")' in starter
    assert 'search.value = ""' in starter
    assert 'filter.value = ""' in starter
    assert "syncSettingsSelectWidget(filter)" in starter
    assert starter.index('filter.value = ""') < starter.index("reset()")
    assert 'newTaskAction.onclick = startBlankTask' in source
    assert '$("#newTask").onclick' in source
    assert "startBlankTask();" in source[source.index('$("#newTask").onclick') : source.index('$("#mobileNav").onclick')]


def test_settings_aligns_the_active_category_after_the_dialog_opens() -> None:
    source = APP_JS.read_text("utf-8")
    opener = _function_source(source, "openWorkspacePanel")

    assert 'if (!dialog.open) dialog.showModal();' in opener
    assert 'if (name === "settings") scheduleActiveSettingsSectionAlignment();' in opener
    assert opener.index("dialog.showModal()") < opener.index("scheduleActiveSettingsSectionAlignment()")


def test_running_navigation_never_leaves_a_completed_task_in_the_main_view() -> None:
    source = APP_JS.read_text("utf-8")
    start = source.index("async function showHistoryNavigation(")
    end = source.index("function initializePrimaryNavigation", start)
    navigation = source[start:end]

    assert 'if (status === "running")' in navigation
    assert 'currentTaskSnapshot?.status === "running"' in navigation
    assert 'history?.querySelector(".history-item")' in navigation
    assert 'openTask(firstRunningTask.dataset.taskId, { activeNavigation: activeId })' in navigation
    assert 'setBlankTaskWelcome("running")' in navigation


def test_artifact_inspector_is_explicit_and_mobile_modal() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderArtifactInspector")
    modal = _function_source(source, "setArtifactInspectorModalState")

    assert 'id="toggleArtifactInspector"' in index
    assert 'hidden aria-expanded="false" aria-controls="artifactInspector"' in index
    assert "{ reveal = false }" in renderer
    assert "if (reveal || (wasOpen" in renderer
    assert "else hideArtifactInspector({ keepToggle: true })" in renderer
    assert 'window.matchMedia("(max-width: 720px)")' in modal
    assert "child.inert = mobileModal" in modal
    assert 'inspector.setAttribute("aria-modal", "true")' in modal


def test_foreground_history_filter_marks_busy_even_during_a_background_request() -> None:
    source = APP_JS.read_text("utf-8")
    loader_start = source.index("async function loadTaskHistory(")
    loader_end = source.index("function scheduleTaskHistorySync", loader_start)
    loader = source[loader_start:loader_end]

    busy = loader.index('history.setAttribute("aria-busy", "true")')
    in_flight_guard = loader.index("if (historyLoading)")
    assert busy < in_flight_guard
    assert 'history.setAttribute("aria-busy", "true")' in loader[:in_flight_guard]


def test_artifacts_default_to_recent_first_with_semantic_lucide_icons() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderArtifacts")
    icon_helper = _function_source(source, "artifactIconName")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert "Number(right.modified || 0) - Number(left.modified || 0)" in renderer
    assert "appSymbol(artifactIconName(artifact))" in renderer
    for icon in ("archive", "audio", "code", "file", "file-text", "folder", "image", "video"):
        assert f'"{icon}"' in icon_helper or icon == "folder"
    assert '.app-symbol-file-text { --app-icon:url("/static/icons/file-text.svg?v=1"); }' in css
    assert '.app-symbol-archive { --app-icon:url("/static/icons/archive.svg?v=1"); }' in css


def test_header_settings_control_and_capability_surfaces_use_cohesive_geometry_and_color() -> None:
    css = EDITORIAL_CSS.read_text("utf-8")
    theme = THEME_CSS.read_text("utf-8")

    assert ".header-settings-button.settings-icon-button" in css
    assert "border-radius: 9px !important" in css
    assert ":root[data-theme] body .settings-select-button:disabled" in css
    assert "opacity: 1 !important" in css
    assert ":root[data-theme] body .capability-runtime-notice" in css
    assert ":root[data-theme] body .capability-vision" in css
    assert ":root[data-theme] body .capability-group header" in css
    assert "min-height: 0" in css
    assert "#f7f2ff" not in theme
    assert "#665479" not in theme


def test_timeline_new_progress_counts_only_rendered_updates_and_preserves_scroll() -> None:
    result = _run_javascript(
        "countTimelineNewProgress([],[],{events:["
        "{type:'thinking'},{type:'status'},{type:'tool_call'},{type:'error'}],requiresRebuild:false},true)",
        "timelineEventIsRenderable",
        "countTimelineNewProgress",
    )
    assert result == 3

    rebuilt = _run_javascript(
        "countTimelineNewProgress([{id:'e2',type:'status'},{id:'e3',type:'assistant'}],"
        "[{id:'e1',type:'tool_call'},{id:'e2',type:'status'}],{events:[],requiresRebuild:true},false)",
        "timelineEventIsRenderable",
        "countTimelineNewProgress",
    )
    assert rebuilt == 1

    source = APP_JS.read_text("utf-8")
    poll = _function_source(source, "poll")
    assert "timelineUnseenProgressCount += newProgressCount" in poll
    assert "restoreTimelineScrollState(timelineScrollState)" in poll
    assert poll.index("restoreTimelineScrollState(timelineScrollState)") < poll.index(
        "timelineUnseenProgressCount += newProgressCount"
    )
    assert 'clearTimelineNewProgress({ scroll: true })' in source


def test_frontend_contains_no_private_chat_mode_entry_or_request_state() -> None:
    source = APP_JS.read_text("utf-8")
    assert "private_chat" not in source
    assert "privacy_session" not in source
    assert "applyPrivacyMode" not in source
