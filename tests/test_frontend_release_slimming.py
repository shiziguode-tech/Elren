from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "deepdesk" / "static" / "app.js"
VOICE_JS = ROOT / "deepdesk" / "static" / "voice.js"
INDEX_HTML = ROOT / "deepdesk" / "static" / "index.html"


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


def test_hidden_runtime_probe_and_duplicate_capability_listener_are_removed() -> None:
    index = INDEX_HTML.read_text("utf-8")
    source = APP_JS.read_text("utf-8")

    assert 'id="probeRuntimes"' not in index
    assert "#probeRuntimes" not in source
    assert '$("#openCapabilities").onclick = async () =>' in source
    assert '$("#openCapabilities").addEventListener' not in source
    assert '<script src="/static/app.js?v=264"></script>' in index


def test_english_translation_keeps_only_final_assignments_and_async_refreshes() -> None:
    source = APP_JS.read_text("utf-8")
    english = _function_source(source, "applyEnglishInterface")

    assert english.count('setText("#newTaskLabel", "New task");') == 1
    assert 'setText("#newTask", "＋ New task");' not in english
    assert english.count('setAttr("#historySearch", "aria-label"') == 1
    assert 'setAttr("#historySearch", "aria-label", "Search all tasks");' in english
    assert english.count('setText(".mobile-setting > span", "Android phone control");') == 1
    assert 'setText("#panel-settings .mobile-setting > span", "Android phone control");' not in english
    assert "window.setTimeout(translateSettingsDynamic, 350);" in source
    assert "window.setTimeout(translateSettingsDynamic, 3000);" in source


def test_voice_preferences_are_loaded_on_first_open_not_during_script_startup() -> None:
    source = VOICE_JS.read_text("utf-8")
    open_voice = _function_source(source, "openVoice")

    assert "let preferencesReady = Promise.resolve();" in source
    assert source.count("preferencesReady = loadPreferences();") == 1
    assert "preferencesReady = loadPreferences();" in open_voice
    assert open_voice.index("preferencesReady = loadPreferences();") < open_voice.index("await preferencesReady;")
    assert "await loadPreferences();" not in open_voice
    assert "await preferencesReady;" in _function_source(source, "speakAnswer")


def test_waiting_states_are_not_presented_as_running_work() -> None:
    source = APP_JS.read_text("utf-8")
    voice = VOICE_JS.read_text("utf-8")
    overview = _function_source(source, "taskOverviewMarkup")
    commands = _function_source(source, "renderCommandActivity")

    assert '!["waiting_approval", "waiting_user"].includes(task.status)' in overview
    assert 'const active = ["queued", "running"].includes(taskStatus);' in commands
    assert 'const waitingApproval = taskStatus === "waiting_approval";' in commands
    assert 'waitingApproval && event.type === "tool_call"' in commands
    assert 'uiText("正在确认下一步操作", "Confirming the next action")' in overview
    assert 'querySelector(".command-activity-text")' in commands
    assert 'class="command-activity-text"' in commands
    assert 'waiting_approval: "Confirming"' in source
    assert 'waiting_approval: "确认中"' in source
    assert "Waiting for approval" not in source
    assert 'const waitingForUser = ["waiting_user", "waiting_human"].includes(task.status);' in voice
    assert 'const waitingForApproval = task.status === "waiting_approval";' in voice
    assert 'vt("确认完成后会自动继续。", "The task will continue automatically after confirmation.")' in voice


def test_tool_preview_has_global_work_budgets_and_stops_object_enumeration_early() -> None:
    source = APP_JS.read_text("utf-8")
    preview = _function_source(source, "boundedTechnicalPreview")

    assert "remainingItems: 240" in preview
    assert "state.remainingItems <= 0" in preview
    assert "for (const key in value)" in preview
    assert "Object.entries(value)" not in preview


def test_unreachable_tool_call_config_is_removed_but_command_rendering_remains() -> None:
    source = APP_JS.read_text("utf-8")
    render = _function_source(source, "renderEvent")

    assert '["tool_call", "tool_result"].includes(event.type)' in render
    assert "renderCommandActivity(event, taskStatus);" in render
    assert "tool_call:" not in render
    assert "tool_result:" in render


def test_workspace_open_does_not_repeat_the_same_select_sync_loop() -> None:
    source = APP_JS.read_text("utf-8")
    open_workspace = _function_source(source, "openWorkspacePanel")
    enhance = _function_source(source, "enhanceWorkspaceSelects")

    assert open_workspace.count("enhanceWorkspaceSelects();") == 1
    assert '["artifactType", "scheduleKind", "scheduleIntervalUnit"]' not in open_workspace
    assert open_workspace.index("applyWorkspaceEnglish(name);") < open_workspace.index("enhanceWorkspaceSelects();")
    assert '["artifactType", "scheduleKind", "scheduleIntervalUnit"]' in enhance
    assert "syncSettingsSelectWidget(select);" in enhance
