from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "deepdesk" / "static"
APP = STATIC / "app.js"


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


def _run_javascript(expression: str, *function_names: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend helper tests")
    source = APP.read_text("utf-8")
    definitions = "\n".join(_function_source(source, name) for name in function_names)
    script = f"const uiText=(zh,en)=>zh;\n{definitions}\nprocess.stdout.write(JSON.stringify({expression}));"
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def _run_localized_system_event(language: str, event: dict) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend helper tests")
    source = APP.read_text("utf-8")
    definition = _function_source(source, "localizedSystemEventMessage")
    script = (
        f"const uiText=(zh,en)=>{json.dumps(language)}==='en'?en:zh;\n"
        f"{definition}\n"
        f"process.stdout.write(JSON.stringify(localizedSystemEventMessage({json.dumps(event)})));"
    )
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def _run_terminal_presentations(language: str, tasks: list[dict | None]):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend terminal-state tests")
    source = APP.read_text("utf-8")
    definitions = "\n".join(
        _function_source(source, name)
        for name in ("appSymbol", "humanizeTaskFailure", "terminalStatePresentation")
    )
    script = (
        f"const isEnglish=()=>{json.dumps(language)}==='en';\n"
        "const uiText=(zh,en)=>isEnglish()?en:zh;\n"
        "const localizeKnownSystemMessage=(value)=>String(value||'');\n"
        f"{definitions}\n"
        f"process.stdout.write(JSON.stringify({json.dumps(tasks)}.map(terminalStatePresentation)));"
    )
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def test_local_capability_groups_preserve_real_tool_truth_once() -> None:
    grouped = _run_javascript(
        "groupLocalCapabilities(["
        "{name:'filesystem',ready:true},{name:'background_browser',ready:false},"
        "{name:'live_computer_use',ready:true},{name:'document',ready:true},"
        "{name:'custom_plugin',ready:false}])",
        "groupLocalCapabilities",
    )
    flattened = [tool for group in grouped for tool in group["tools"]]
    assert [tool["name"] for tool in flattened] == [
        "filesystem",
        "background_browser",
        "live_computer_use",
        "document",
        "custom_plugin",
    ]
    assert [tool["ready"] for tool in flattened] == [True, False, True, True, False]
    assert [group["id"] for group in grouped] == [
        "files-development",
        "web-research",
        "computer-devices",
        "media-documents",
        "other",
    ]


def test_failure_copy_is_actionable_and_raw_details_are_progressively_disclosed() -> None:
    result = _run_javascript(
        "[humanizeTaskFailure('ProviderError: request timed out'),"
        "humanizeTaskFailure('HTTP 429 quota exceeded'),"
        "humanizeTaskFailure('unknown crash')]",
        "humanizeTaskFailure",
    )
    assert "规定时间" in result[0]["summary"]
    assert "当前模型继续" in result[0]["action"]
    assert "菜单选择其他模型" in result[0]["action"]
    assert "额度" in result[1]["summary"]
    assert "已有进度" in result[2]["summary"]

    source = APP.read_text("utf-8")
    terminal = _function_source(source, "renderTerminalState")
    rebuild = _function_source(source, "rebuildTaskTimeline")
    assert 'class="terminal-technical"' in terminal
    assert 'continuationControlMarkup(task)' in terminal
    assert 'class="terminal-continue-split"' in source
    assert 'uiText("继续", "Continue")' in source
    assert 'class="terminal-continue-menu-toggle"' in source
    assert 'class="terminal-continue-model-option"' in source
    assert 'continueTaskImmediately(task, selector)' in terminal
    assert 'role="${task.status === "failed" ? "alert" : "status"}"' in terminal
    assert '["error", "loop_detected"].includes(event.type)' in rebuild
    assert 'if (task.status === "failed") showToast' not in terminal


@pytest.mark.parametrize("language", ["zh", "en"])
def test_terminal_state_is_selected_lazily_for_history_and_new_tasks(language: str) -> None:
    presentations = _run_terminal_presentations(
        language,
        [
            None,
            {},
            {"status": "running"},
            {"status": "completed"},
            {"status": "completed", "summary": None},
            {"status": "completed", "summary": "legacy summary"},
            {"status": "cancelled", "error": None},
            {"status": "failed", "error": "ProviderError: request timed out"},
        ],
    )

    # New/active tasks have no terminal card, irrespective of whether a legacy
    # API payload omitted, nullified, or populated an unrelated summary field.
    assert presentations[:3] == [None, None, None]
    assert presentations[3] == presentations[4] == presentations[5] is None
    assert presentations[6][1] == ("任务已停止" if language == "zh" else "Task stopped")
    assert "null" not in presentations[6][2].lower()
    assert presentations[7][1] == ("任务需要处理" if language == "zh" else "Task needs attention")
    assert "timed out" not in presentations[7][2].lower()

    source = APP.read_text("utf-8")
    renderer = _function_source(source, "renderTerminalState")
    presentation = _function_source(source, "terminalStatePresentation")
    assert "terminalStatePresentation(task)" in renderer
    assert "terminalStatusRendered = task.status" in renderer
    assert 'if (task.status === "completed")' in renderer
    assert 'uiText("任务已完成", "Task completed")' not in presentation
    assert renderer.index('if (task.status === "completed")') < renderer.index("terminalStatePresentation(task)")
    assert 'if (task.status === "failed")' in presentation
    assert "failure.summary" not in renderer


def test_new_task_clears_only_generated_continuation_drafts() -> None:
    source = APP.read_text("utf-8")
    generated_draft = _function_source(source, "setSystemComposerDraft")
    clear_generated_draft = _function_source(source, "clearSystemComposerDraft")
    continue_immediately = _function_source(source, "continueTaskImmediately")
    reset = _function_source(source, "reset")

    assert "prompt.dataset.systemDraft = kind" in generated_draft
    assert "if (!prompt?.dataset.systemDraft) return false" in clear_generated_draft
    assert 'setSystemComposerDraft' not in continue_immediately
    assert 'return start({ promptOverride: prompt })' in continue_immediately
    assert "originalContinuationModel(task)" in continue_immediately
    assert "preference.value = requestedModel" in continue_immediately
    assert "clearSystemComposerDraft()" in reset
    assert "delete event.currentTarget.dataset.systemDraft" in source
    assert '$("#prompt").value = ""' not in reset


def test_history_model_projection_cannot_leak_into_the_next_new_task() -> None:
    source = APP.read_text("utf-8")
    open_task = _function_source(source, "openTask")
    reset = _function_source(source, "reset")
    blank = _function_source(source, "isBlankNewTaskComposer")
    remember = _function_source(source, "rememberNewTaskComposerPreferences")
    restore = _function_source(source, "restoreNewTaskComposerPreferences")

    assert 'if (isBlankNewTaskComposer() && !$("#modelPreference")?.dataset.languageResumeModel) rememberNewTaskComposerPreferences()' in open_task
    assert "restoreNewTaskComposerPreferences()" in reset
    assert "!taskId" in blank
    assert "!continuationTaskId" in blank
    assert "!currentTaskSnapshot" in blank
    assert "!startRequestPending" in blank
    assert 'newTaskModelPreference = $("#modelPreference")?.value || "auto"' in remember
    assert "newTaskReasoningPreference = reasoningPreferenceValue()" in remember
    assert 'modelAvailable ? desiredModel : "auto"' in restore
    assert 'syncReasoningAvailability("", newTaskReasoningPreference || "auto")' in restore
    assert source.count("if (isBlankNewTaskComposer()) rememberNewTaskComposerPreferences()") == 3
    assert 'cleanUrl.searchParams.delete("task")' in reset
    assert 'window.history.replaceState(null, "", cleanUrl.toString())' in reset

    states = _run_javascript(
        "(()=>{const check=(active,continuation,snapshot,pending)=>{"
        "taskId=active;continuationTaskId=continuation;"
        "currentTaskSnapshot=snapshot;startRequestPending=pending;"
        "return isBlankNewTaskComposer();};return ["
        "check(null,null,null,false),"
        "check('running',null,null,false),"
        "check(null,'failed',{status:'failed'},false),"
        "check(null,null,{status:'completed'},false),"
        "check(null,null,null,true)];})()",
        "isBlankNewTaskComposer",
    )
    assert states == [True, False, False, False, False]


def test_persisted_system_events_replay_in_the_current_ui_language() -> None:
    retry_event = {
        "type": "model_retry",
        "data": {
            "reason": "unresolved_verification_failure",
            "attempt": 4,
            # Persisted legacy prose is deliberately ignored. Rendering must use
            # the stable event type and structured fields instead.
            "message": "最近一次验证仍失败，正在要求模型读取证据并修复",
        },
    }
    exhausted_event = {
        "type": "model_recovery_exhausted",
        "data": {"category": "empty_or_progress", "invalid_response_count": 3},
    }
    model_event = {
        "type": "model_selected",
        "data": {
            "model": "deepseek-v4-flash",
            "mode": "automatic_fallback",
            "reason": "本机规则判定为常规任务",
        },
    }
    tools_event = {
        "type": "tool_catalog_scoped",
        "data": {
            "visible": 8,
            "total": 27,
            "message": "已按当前任务优先加载相关工具；其余注册工具可通过 tool_search 即时启用",
        },
    }

    assert "验证仍未通过" in _run_localized_system_event("zh", retry_event)
    assert "第 4 次恢复尝试" in _run_localized_system_event("zh", retry_event)
    assert "verification still failed" in _run_localized_system_event("en", retry_event)
    assert "Recovery attempt 4" in _run_localized_system_event("en", retry_event)
    assert "最近一次验证仍失败，正在要求模型读取证据并修复" not in _run_localized_system_event("en", retry_event)
    assert "本地规则" in _run_localized_system_event("zh", model_event)
    assert "local rules" in _run_localized_system_event("en", model_event)
    assert "8/27" in _run_localized_system_event("zh", tools_event)
    assert "8 of 27" in _run_localized_system_event("en", tools_event)
    assert "停止自动重试" in _run_localized_system_event("zh", exhausted_event)
    assert "stopped automatic retries" in _run_localized_system_event(
        "en", exhausted_event
    )
    assert "本机规则" not in _run_localized_system_event("en", model_event)
    assert "已按当前任务" not in _run_localized_system_event("en", tools_event)

    source = APP.read_text("utf-8")
    renderer = _function_source(source, "renderEvent")
    assert "localizedSystemEventMessage(event)" in renderer
    assert '["model_retry", "model_recovery_exhausted", "model_selected", "tool_catalog_scoped"]' in renderer
    assert '"model_recovery_exhausted"' in _function_source(
        source, "eventPresentation"
    )


def test_task_overview_uses_only_observed_events_without_fake_percentages() -> None:
    source = APP.read_text("utf-8")
    overview = _function_source(source, "taskOverviewMarkup")
    assert 'event.type === "thinking"' in overview
    assert 'event.type === "tool_call"' in overview
    assert "taskStatusLabel(task.status)" in overview
    assert "%" not in overview
    assert 'class="progress-pulse"' in overview


def test_live_activity_is_visible_before_tools_and_yields_to_command_disclosure() -> None:
    source = APP.read_text("utf-8")
    overview = _function_source(source, "taskOverviewMarkup")
    starter = _function_source(source, "start")
    poller = _function_source(source, "poll")

    assert 'uiText("正在准备任务", "Preparing task")' in overview
    assert 'uiText("正在等待模型响应", "Waiting for the model")' in overview
    assert "latestToolCallIndex > latestToolResultIndex" in overview
    assert 'class="event-card live-activity"' in overview
    assert 'updateTaskOverview({ status: "queued", events: [] })' in starter
    assert poller.index("delta.events.forEach") < poller.index("updateTaskOverview(task)")


def test_timeline_rebuild_preserves_open_command_and_live_disclosures() -> None:
    source = APP.read_text("utf-8")
    rebuild = _function_source(source, "rebuildTaskTimeline")
    capture = _function_source(source, "captureTimelineDisclosureState")
    restore = _function_source(source, "restoreTimelineDisclosureState")

    assert 'querySelectorAll(".command-activity")' in capture
    assert 'querySelectorAll(".event-technical-details")' in capture
    assert "details.open = Boolean(saved.open)" in restore
    assert "captureTimelineDisclosureState(timeline)" in rebuild
    assert "restoreTimelineDisclosureState(timeline, disclosureState)" in rebuild


def test_routine_tools_are_lightweight_rows_with_disclosed_payloads() -> None:
    source = APP.read_text("utf-8")
    renderer = _function_source(source, "renderEvent")
    activity = _function_source(source, "renderCommandActivity")
    css = (STATIC / "editorial-ui.css").read_text("utf-8")
    assert "renderCommandActivity(event, taskStatus)" in renderer
    assert 'class="event-technical-details"' in activity
    assert 'uiText("查看参数", "View parameters")' in activity
    assert 'uiText("查看结果", "View result")' in activity
    assert 'class="event-card command-activity"' in activity
    assert ".command-activity > summary" in css
    assert "body.compact-trace" not in (STATIC / "trace-view.css").read_text("utf-8")


def test_responsive_settings_and_sidebar_have_no_850_851_cliff() -> None:
    source = APP.read_text("utf-8")
    history = (STATIC / "history.css").read_text("utf-8")
    professional = (STATIC / "professional-ui.css").read_text("utf-8")
    assert source.count('matchMedia("(max-width: 960px)")') >= 3
    assert "@media (max-width: 960px)" in history
    assert "@media (max-width:960px)" in professional
    assert "@media (min-width:601px) and (max-width:960px)" in professional
    assert "body .form-grid .provider-keys-setting { grid-template-columns:minmax(0,1fr)!important; }" in professional


def test_mobile_sidebar_scrim_hitbox_starts_after_the_visible_drawer() -> None:
    history = (STATIC / "history.css").read_text("utf-8")
    assert "width: min(320px, 86vw)" in history
    assert "inset: 0 0 0 min(320px, 86vw)" in history
    assert "body.nav-open .nav-overlay" in history


def test_settings_save_status_is_a_persistent_dialog_footer_not_nested_in_form() -> None:
    index = (STATIC / "index.html").read_text("utf-8")
    source = APP.read_text("utf-8")
    css = (STATIC / "professional-ui.css").read_text("utf-8")
    form_end = index.index("</form>", index.index('id="settingsForm"'))
    save_bar = index.index('id="settingsSaveBar"')
    assert form_end < save_bar
    assert 'form="settingsForm"' in index[save_bar:]
    assert '$("#settingsSaveBar").hidden = name !== "settings";' in source
    assert ".settings-save-bar" in css
    assert "border-top:1px solid var(--line)" in css


def test_sidebar_shows_readiness_summary_instead_of_twenty_seven_chips() -> None:
    source = APP.read_text("utf-8")
    runtime = _function_source(source, "renderRuntimeStatus")
    assert "readyToolCount" in runtime
    assert "capabilities\"} ready" in runtime
    assert "tool-chip" not in runtime
    assert "按任务自动调用" in runtime


def test_known_openclaw_install_error_is_localized_in_chinese_catalog() -> None:
    source = APP.read_text("utf-8")
    localizer = _function_source(source, "localizeKnownSystemMessage")
    assert '"OpenClaw CLI is not installed": "未安装 OpenClaw CLI"' in localizer
    assert "OpenClaw 扩展目录暂不可用：${localizeKnownSystemMessage(error.message)}" in source
