from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "deepdesk" / "static" / "app.js"
KATEX_JS = ROOT / "deepdesk" / "static" / "vendor" / "katex" / "katex.min.js"


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
        pytest.skip("Node.js is required for the frontend helper regression")
    source = APP_JS.read_text("utf-8")
    definitions = "\n".join(_function_source(source, name) for name in function_names)
    script = f'{definitions}\nprocess.stdout.write(JSON.stringify({expression}));'
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def _run_javascript_with_katex(expression: str, *function_names: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the KaTeX frontend regression")
    source = APP_JS.read_text("utf-8")
    definitions = "\n".join(_function_source(source, name) for name in function_names)
    script = (
        f"const katex = require({json.dumps(str(KATEX_JS))});\n"
        f"{definitions}\nprocess.stdout.write(JSON.stringify({expression}));"
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


def test_event_id_cursor_survives_the_1000_event_rolling_window() -> None:
    result = _run_javascript(
        "eventDelta(Array.from({length: 1000}, (_, i) => ({id: `e${i + 2}`})), 'e1000')",
        "eventDelta",
    )
    assert [event["id"] for event in result["events"]] == ["e1001"]
    assert result["cursor"] == "e1001"
    assert result["requiresRebuild"] is False


def test_missing_event_cursor_requests_a_reliable_timeline_rebuild() -> None:
    result = _run_javascript(
        "eventDelta(Array.from({length: 1000}, (_, i) => ({id: `e${i + 501}`})), 'e12')",
        "eventDelta",
    )
    assert result == {"events": [], "cursor": "e1500", "requiresRebuild": True}


def test_incremental_task_poll_replaces_the_inclusive_cursor_suffix() -> None:
    result = _run_javascript(
        "mergeTaskEventUpdate("
        "{id:'task-1',events:[{id:'a',data:{v:1}},{id:'b',data:{v:1}},{id:'old-tail'}]},"
        "{id:'task-1',event_delta:true,status:'running',events:[{id:'b',data:{v:2}},{id:'new-tail'}]}"
        ")",
        "mergeTaskEventUpdate",
    )

    assert [event["id"] for event in result["events"]] == ["a", "b", "new-tail"]
    assert result["events"][1]["data"]["v"] == 2
    assert result["status"] == "running"


def test_incremental_task_poll_rejects_an_unmergeable_suffix() -> None:
    result = _run_javascript(
        "mergeTaskEventUpdate("
        "{id:'task-1',events:[{id:'a'}]},"
        "{id:'task-1',event_delta:true,events:[{id:'missing'}]}"
        ")",
        "mergeTaskEventUpdate",
    )

    assert result is None


def test_task_switch_and_start_paths_are_guarded_against_stale_responses() -> None:
    source = APP_JS.read_text("utf-8")
    open_task = _function_source(source, "openTask")
    start = _function_source(source, "start")
    poll = _function_source(source, "poll")

    assert "const viewGeneration = ++taskViewGeneration" in open_task
    assert "if (!isCurrentTaskRequest(id, viewGeneration)) return;" in open_task
    assert "poll(id, viewGeneration)" in open_task
    assert "if (!prompt || startRequestPending || uploadRequestPending || taskViewLoading || stopRequestPending) return false;" in start
    assert "return sendRunningMessage(prompt)" in start
    assert "startRequestPending = true" in start
    assert "startRequestPending = false" in start
    assert "if (viewGeneration !== taskViewGeneration)" in start
    assert "isCurrentTaskRequest(expectedTaskId, expectedGeneration)" in poll
    assert "knownEvents.at(-2)" in poll
    assert "after_event_id=${encodeURIComponent(eventCursor)}" in poll
    assert "mergeTaskEventUpdate(previousTaskSnapshot, update)" in poll
    assert "currentTaskSnapshot !== snapshotAtRequest" in poll
    assert "task = await api(`/api/tasks/${expectedTaskId}`);" in poll
    assert "setTimeout(() => poll(expectedTaskId, expectedGeneration)" in poll


def test_context_badge_ignores_broken_one_token_relay_samples() -> None:
    source = APP_JS.read_text("utf-8")
    usage = _function_source(source, "updateContextUsage")

    assert "context_tokens_used" in usage
    assert "estimated_context_tokens" in usage
    assert "relevantUsage.reduce" in usage
    assert "Math.max(1" in usage


def test_running_task_composer_queues_follow_up_without_stopping() -> None:
    source = APP_JS.read_text("utf-8")
    sender = _function_source(source, "sendRunningMessage")
    start = _function_source(source, "start")

    assert "`/api/tasks/${expectedTaskId}/messages`" in sender
    assert "privacy_session" not in sender
    assert "return sendRunningMessage(prompt)" in start
    assert "requestStop" not in sender


def test_new_task_keeps_existing_projects_running_in_parallel() -> None:
    source = APP_JS.read_text("utf-8")
    start = source.index('$("#newTask").onclick = () => {')
    end = source.index("};", start)
    handler = source[start:end]

    assert "startBlankTask();" in handler
    assert "requestStop" not in handler
    assert "askForConfirmation" not in handler
    starter = _function_source(source, "startBlankTask")
    assert "reset();" in starter
    assert "requestStop" not in starter


def test_custom_system_prompt_suffix_is_loaded_saved_and_categorized() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    assert 'id="settingCustomSystemPromptSuffix"' in index
    assert 'maxlength="20000"' in index
    assert '".custom-system-prompt-setting"' in source
    assert 'settings.custom_system_prompt_suffix || ""' in source
    assert '["custom_system_prompt_suffix", "#settingCustomSystemPromptSuffix", "string"]' in source


def test_terminal_tasks_support_one_click_and_prompted_continuation() -> None:
    source = APP_JS.read_text("utf-8")
    render_terminal = _function_source(source, "renderTerminalState")
    one_click = _function_source(source, "continueTaskImmediately")
    start = _function_source(source, "start")

    assert "continueTaskImmediately(task)" in render_terminal
    assert "continuationTaskId = task.id" in one_click
    assert "taskId = null" in one_click
    assert "继续完成上一任务" in one_click
    assert "return start({ promptOverride: prompt })" in one_click
    assert "setSystemComposerDraft" not in one_click
    assert 'const prompt = String(promptOverride ?? $("#prompt").value).trim()' in start
    assert "const continuingFrom = continuationTaskId" in start


def test_private_chat_mode_is_not_exposed_by_the_frontend() -> None:
    source = APP_JS.read_text("utf-8")
    open_task = _function_source(source, "openTask")
    start = _function_source(source, "start")
    assert "applyPrivacyMode" not in source
    assert "private_chat" not in open_task
    assert "private_chat" not in start
    assert "privacy_session" not in source


def test_openclaw_idle_copy_explains_both_auto_start_modes() -> None:
    result = _run_javascript(
        "[openClawStatusText({cli_installed: true, gateway_ready: false, auto_start: true}), "
        "openClawStatusText({cli_installed: true, gateway_ready: false, auto_start: false})]",
        "openClawStatusText",
    )
    assert result == [
        "OpenClaw：Gateway 未启动（需要时自动启动）",
        "OpenClaw：Gateway 未启动（自动启动已关闭）",
    ]


def test_google_fallback_distinguishes_unprobed_from_unavailable() -> None:
    result = _run_javascript(
        "(() => { globalThis.isEnglish = () => false; return ["
        "visionFallbackStatus({configured:true, probed:false, ready:false}),"
        "visionFallbackStatus({configured:true, probed:true, ready:false}),"
        "visionFallbackStatus({configured:true, probed:true, ready:true}),"
        "visionFallbackStatus({configured:true, probed:true, ready:false,last_error:'Google semantic fallback unavailable: User location is not supported for the API use.'})]; })()",
        "visionFallbackStatus",
    )
    assert result == [
        {"label": "验证中", "className": "pending"},
        {"label": "不可用", "className": "unavailable", "hint": ""},
        {"label": "已就绪", "className": "ready"},
        {
            "label": "出口地区受限",
            "className": "unavailable",
            "hint": "当前 Google API 不接受这条网络出口；切换到支持 Gemini API 的节点后重新探测",
        },
    ]

    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "history.css").read_text("utf-8")
    assert 'class="capability-vision-item is-${className}"' in source
    assert 'role="listitem"' in source
    assert "const semanticState = visionFallbackStatus(semantic)" in source
    assert "Google 语义视觉备用${semanticState.label}" in source
    assert ".capability-vision-item.is-ready" in css
    assert ".capability-vision-item.is-unavailable" in css


def test_capability_map_keeps_local_tools_visible_when_openclaw_is_down() -> None:
    source = APP_JS.read_text("utf-8")
    assert "groupLocalCapabilities" in source
    assert 'uiText("文件与开发", "Files & development")' in source
    assert 'uiText("电脑与设备", "Computer & devices")' in source
    assert "其他能力不受影响" in source
    assert 'const status = await api("/api/status")' in source
    assert 'api("/api/openclaw/catalog")' in source


def test_capability_groups_never_render_with_a_blank_heading() -> None:
    result = _run_javascript(
        "(() => { globalThis.isEnglish = () => true; return ["
        "capabilityGroupTitle({label: '', id: ''}, 1), "
        "capabilityGroupTitle({id: 'browser'}, 0)]; })()",
        "capabilityGroupTitle",
    )
    assert result == ["OpenClaw tool group 2", "browser"]


def test_task_title_preserves_mixed_language_text_without_question_mark_substitution() -> None:
    result = _run_javascript(
        "taskDisplayTitle('Markdown 中文与 API 说明', '')",
        "taskDisplayTitle",
    )
    assert result == "Markdown 中文与 API 说明"


def test_every_builtin_tool_has_a_real_localized_name() -> None:
    tool_ids = [
        "filesystem", "document", "request_human_action", "memory", "generate_media",
        "jianpu_omr", "jianpu_to_staff",
        "mcp", "skills", "shell", "process_manager", "clipboard", "background_browser",
        "windows_ui", "computer_use", "computer", "vision", "web", "openclaw",
        "update_settings", "feishu", "telegram",
        "sandbox", "provider_web_search", "cron", "mobile_device",
        "macos_ui",
        "live_computer_use",
    ]
    result = _run_javascript(
        f"(() => {{ globalThis.isEnglish = () => false; return {json.dumps(tool_ids)}.map(localizedToolName); }})()",
        "localizedToolName",
    )
    assert len(result) == len(tool_ids)
    assert "工具" not in result
    assert result[1] == "文档处理"
    assert result[4] == "媒体生成"
    assert result[16] == "图片识别"
    assert result[-3:-1] == ["手机设备", "macOS 界面操作"]
    assert result[-1] == "实时电脑控制"

    english = _run_javascript(
        "(() => { globalThis.isEnglish = () => true; return localizedToolName('unknown_custom_tool'); })()",
        "localizedToolName",
    )
    assert english == "Unknown Custom Tool"


def test_frontend_cache_version_was_bumped() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    assert '<script src="/static/theme-init.js?v=10"></script>' in index
    assert '<script src="/static/app.js?v=264"></script>' in index
    assert '<script src="/static/voice.js?v=18"></script>' in index
    assert '<link rel="stylesheet" href="/static/style.css?v=4" />' in index
    assert '<link rel="stylesheet" href="/static/scroll-fix.css?v=2" />' in index
    assert '<link rel="stylesheet" href="/static/history.css?v=33" />' in index
    assert '<link rel="stylesheet" href="/static/theme.css?v=19" />' in index
    assert '<link rel="stylesheet" href="/static/professional-ui.css?v=62" />' in index
    assert '<link rel="stylesheet" href="/static/voice.css?v=21" />' in index
    assert '<link rel="stylesheet" href="/static/editorial-ui.css?v=38" />' in index


def test_history_filters_keep_their_height_and_outer_corner_geometry() -> None:
    css = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")

    assert "flex: 0 0 36px;" in css
    assert "min-height: 36px;" in css
    assert "overflow: visible;" in css
    assert "border-radius: 7px 0 0 7px;" in css
    assert "border-radius: 0 7px 7px 0;" in css


def test_saved_model_and_reasoning_are_hydrated_before_live_settings_arrive() -> None:
    source = APP_JS.read_text("utf-8")

    assert 'const SETTINGS_DISPLAY_STORAGE_KEY = "elren.settings-display.v1";' in source
    assert "function cacheSettingsDisplay(settings)" in source
    assert "function hydrateSettingsDisplayFromCache()" in source
    assert "function preloadSettingsDisplay()" in source
    assert "cacheSettingsDisplay(settings);" in source
    assert "cacheSettingsDisplay(confirmed);" in source
    assert '{ ...status, model: status.default_model, active_model: status.model }' in source
    assert "automaticDefaultModelSelector || latestStatus?.model" in source
    assert source.index("hydrateSettingsDisplayFromCache();") < source.index("initializeModelPreferencePicker();")
    assert source.index("preloadSettingsDisplay();") < source.index("initializeModelPreferencePicker();")


def test_discussion_team_settings_and_initial_reasoning_control_are_present() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert 'data-settings-target="team"' in index
    assert 'id="settingDiscussionTeamEnabled"' not in index
    assert 'id="discussionTeamRows"' in index
    assert 'id="discussionTeamMemberCount"' in index
    assert 'id="clearDiscussionTeam"' in index
    assert 'value="discussion-team"' in source
    assert 'task.discussion_team_enabled' in source
    assert 'discussionTeamConfigured' in source
    assert 'box-shadow:none' in css
    assert 'class="reasoning-picker" aria-label="思考深度" hidden>' in index
    assert "collectDiscussionTeamRows" in source
    assert "renderDiscussionTeamRows" in source
    assert 'reasoning_effort: row.querySelector(".team-member-reasoning")?.value || "default"' in source
    assert "enhanceDiscussionTeamSelect" in source
    assert 'row.querySelector(".remove-team-member").onclick = async () =>' in source
    assert 'title: uiText("移除讨论团成员", "Remove team participant")' in source
    assert 'confirmLabel: uiText("确认移除", "Remove participant")' in source
    assert "if (!confirmed) return;" in source
    assert "discussion-team-row-header" in css
    assert "discussion-team-fields" in css
    assert 'discussion_team_enabled: $("#settingDiscussionTeamEnabled")' not in source


def test_all_settings_selects_use_the_shared_polished_picker() -> None:
    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert "function enhanceAllSettingsSelects()" in source
    assert 'document.querySelectorAll("#panel-settings select")' in source
    assert "function positionSettingsSelectMenu(shell)" in source
    assert 'menu.style.maxHeight = `${Math.round(maxHeight)}px`' in source
    assert 'observer.observe(panel, { childList: true, subtree: true })' in source
    assert 'event.target.closest(".settings-select-menu")' in source
    assert "else positionSettingsSelectMenu(shell);" in source
    assert 'document.querySelector(".settings-select-shell[data-open=\'true\']")' in source
    assert ".settings-native-select" in css
    assert ".settings-select-button" in css
    assert ".settings-select-menu" in css
    assert 'position:fixed;z-index:2500' in css
    assert '.settings-select-option[aria-selected="true"]' in css
    assert "function enhanceHistoryStatusSelect()" in source
    assert 'shell.classList.add("history-status-select-shell")' in source
    assert 'menu.classList.add("history-status-select-menu")' in source
    assert 'shell.classList.contains("artifact-type-select-shell")' in source
    assert 'shell.classList.contains("schedule-unit-select-shell")' in source
    assert 'Math.max(180, rect.width)' in source
    assert 'event.target.classList.contains("settings-select-option")' in source
    assert ".history-status-select-menu" in css
    assert "function enhanceWorkspaceSelects()" in source
    assert '["artifactType", "scheduleKind", "scheduleIntervalUnit"]' in source
    assert 'shell.classList.add("artifact-type-select-shell")' in source
    assert 'shell.classList.add("schedule-kind-select-shell")' in source
    assert ".artifact-type-select-shell" in css
    assert ".schedule-kind-select-shell" in css


def test_composer_upload_and_voice_actions_use_compact_vector_icons() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    app = (ROOT / "deepdesk" / "static" / "app.js").read_text("utf-8")
    voice = (ROOT / "deepdesk" / "static" / "voice.js").read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert 'id="uploadFile" class="upload-file composer-icon-button"' in index
    assert 'class="app-symbol app-symbol-folder"' in index
    assert 'id="voiceConversation" class="voice-conversation composer-icon-button"' in index
    assert 'class="app-symbol app-symbol-mic"' in index
    assert 'class="composer-icon-actions" role="group"' in index
    assert 'setText("#uploadFile", "Upload file")' not in app
    assert 'set("#voiceConversation", "Voice")' not in voice
    assert ".composer-tools .composer-icon-button" in css
    assert ".composer-icon-actions" in css
    assert "width:38px" in css
    assert (ROOT / "deepdesk" / "static" / "icons" / "folder.svg").is_file()


def test_running_empty_composer_reuses_send_button_as_stop_control() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    app = (ROOT / "deepdesk" / "static" / "app.js").read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert 'id="stop"' not in index
    assert 'id="send" type="button"' in index
    assert "function syncComposerAction()" in app
    assert 'button.dataset.action = shouldStop ? "stop" : "send"' in app
    assert 'dataset.action === "stop"' in app
    assert 'const button = $("#send");' in app
    assert '.composer #send[data-action="stop"]::after' in css
    assert 'typeof requestStop === "function"' in (ROOT / "deepdesk" / "static" / "voice.js").read_text("utf-8")
    assert '<link rel="stylesheet" href="/static/human-action.css?v=3" />' in index
    assert 'id="settingDeepSeekKey"' in index
    assert 'id="settingDeepSeekBackupKey"' in index
    assert 'id="settingGeminiKey"' in index
    assert 'id="settingCrossConversationContext"' in index
    assert 'id="modelProviderRows"' in index
    assert 'id="settingTelegramBotToken"' in index
    source = APP_JS.read_text("utf-8")
    assert "cross_conversation_context" in source
    assert "cross_context_task_ids" in source
    assert 'task.source === "feishu"' in source
    assert 'document: "文档处理"' in source
    assert 'generate_media: "媒体生成"' in source
    assert 'skills: "技能库"' in source
    assert 'vision: "图片识别"' in source
    assert 'update_settings: "设置修改"' in source
    assert 'provider_web_search: "模型联网搜索"' in source
    assert 'mobile_device: "手机设备"' in source
    assert 'macos_ui: "macOS 界面操作"' in source
    assert 'return names[tool] || (isEnglish() ? "Tool" : "工具")' not in source
    assert 'label: localizedToolName(tool.name)' in source
    assert "开发中 · 尚未发布最终版" not in index
    assert "Development build · not a final release" not in source
    theme_css = (ROOT / "deepdesk" / "static" / "theme.css").read_text("utf-8")
    assert '.language-glyph-zh' in theme_css
    assert '.language-glyph-en' in theme_css


def test_fenced_code_never_guesses_that_literal_escapes_are_newlines() -> None:
    escaped = "```js\nconst first = 1;\\nconst second = 2;\n```"
    quoted = '```js\nconst text = "first\\nsecond";\\nconsole.log(text);\n```'
    native = "```python\nif ready:\n    run()\n```"
    regex = "```js\nconst newline = /\\n/;\n```"
    complex_regex = "```js\nconst pattern = /^a;\\nb:foo{\\n}bar$/;\n```"
    windows_path = "```powershell\nGet-Content C:\\new\\report.txt\n```"
    shell_literal = "```sh\nprintf 'first\\nsecond' | sed 's/\\r$//'\n```"
    json_literal = '```json\n{"value":"first\\nsecond"}\n```'
    prose_literal = "```text\nUse this;\\nnot that as literal text\n```"
    doubled = "```text\nshow \\\\n literally\n```"
    result = _run_javascript(
        f"(() => ({{escaped: renderMarkdown({json.dumps(escaped)}), "
        f"quoted: renderMarkdown({json.dumps(quoted)}), "
        f"native: renderMarkdown({json.dumps(native)}), "
        f"regex: renderMarkdown({json.dumps(regex)}), "
        f"complexRegex: renderMarkdown({json.dumps(complex_regex)}), "
        f"windowsPath: renderMarkdown({json.dumps(windows_path)}), "
        f"shellLiteral: renderMarkdown({json.dumps(shell_literal)}), "
        f"jsonLiteral: renderMarkdown({json.dumps(json_literal)}), "
        f"proseLiteral: renderMarkdown({json.dumps(prose_literal)}), "
        f"doubled: renderMarkdown({json.dumps(doubled)})}}))()",
        "escapeHtml",
        "renderInlineMarkdown",
        "tableCells",
        "isTableDivider",
        "isBlockStart",
        "renderMarkdown",
    )

    assert "const first = 1;\\nconst second = 2;" in result["escaped"]
    assert 'const text = &quot;first\\nsecond&quot;;\\nconsole.log(text);' in result["quoted"]
    assert "if ready:\n    run()" in result["native"]
    assert "/\\n/" in result["regex"]
    assert "/^a;\\nb:foo{\\n}bar$/" in result["complexRegex"]
    assert "C:\\new\\report.txt" in result["windowsPath"]
    assert "printf &#39;first\\nsecond&#39; | sed &#39;s/\\r$//&#39;" in result["shellLiteral"]
    assert "{&quot;value&quot;:&quot;first\\nsecond&quot;}" in result["jsonLiteral"]
    assert "Use this;\\nnot that as literal text" in result["proseLiteral"]
    assert "show \\\\n literally" in result["doubled"]


def test_markdown_renders_latex_and_commonmark_hard_breaks_without_touching_code() -> None:
    sample = """这是关于代数方程的求解问题。

方程为：\\
$$\\frac{1}{7}x = \\frac{1}{4}x + 450$$

**答案：**\\
$x = -4200$

`$literal$`"""
    result = _run_javascript(
        f"renderMarkdown({json.dumps(sample)})",
        "escapeHtml",
        "renderMathToHtml",
        "renderInlineMarkdown",
        "tableCells",
        "isTableDivider",
        "isBlockStart",
        "renderMarkdown",
    )

    assert '<pre class="math-fallback">' in result
    assert '$$\\frac{1}{7}x = \\frac{1}{4}x + 450$$' in result
    assert '<code class="inline-code math-fallback">$x = -4200$</code>' in result
    assert '<code class="inline-code">$literal$</code>' in result
    assert "方程为：\\" not in result
    assert "<strong>答案：</strong>\\" not in result

    rendered_with_katex = _run_javascript_with_katex(
        f"renderMarkdown({json.dumps(sample)})",
        "escapeHtml",
        "renderMathToHtml",
        "renderInlineMarkdown",
        "tableCells",
        "isTableDivider",
        "isBlockStart",
        "renderMarkdown",
    )
    assert '<div class="math-display">' in rendered_with_katex
    assert 'class="katex"' in rendered_with_katex
    assert 'class="katex-mathml"' in rendered_with_katex
    assert '<span class="math-inline">' in rendered_with_katex


def test_structured_tool_display_restores_only_real_newlines() -> None:
    value = {
        "multi\nline key": "key line break is real",
        "command": "first line\r\nsecond line\nthird line",
        "legacy_carriage_return": "older output\rnext line",
        "literal_escape": r"keep \n and \r literal",
        "regex": r"/^first\nsecond$/",
        "windows_path": r"C:\new\report.txt",
    }
    result = _run_javascript(
        f"stringifyStructuredValueForDisplay({json.dumps(value)})",
        "stringifyStructuredValueForDisplay",
    )

    assert '"multi\nline key": "key line break is real"' in result
    assert "first line\nsecond line\nthird line" in result
    assert "older output\nnext line" in result
    assert r"keep \\n and \\r literal" in result
    assert r"/^first\\nsecond$/" in result
    assert r"C:\\new\\report.txt" in result


def test_tool_technical_preview_is_bounded_before_entering_the_dom() -> None:
    result = _run_javascript(
        """(() => {
          globalThis.TECHNICAL_PREVIEW_MAX_CHARS = 16000;
          globalThis.TECHNICAL_PREVIEW_MAX_DEPTH = 7;
          globalThis.TECHNICAL_PREVIEW_MAX_ITEMS = 60;
          const value = { rows: Array.from({ length: 5000 }, (_, index) => ({ index, text: 'x'.repeat(5000) })) };
          const output = technicalPreviewText(value);
          return { length: output.length, truncated: output.includes('preview truncated'), more: output.includes('more items') };
        })()""",
        "stringifyStructuredValueForDisplay",
        "boundedTechnicalPreview",
        "technicalPreviewText",
    )

    assert result["length"] <= 16000
    assert result["truncated"] is True
    assert result["more"] is True


def test_markdown_renderer_escapes_html_and_rejects_script_urls() -> None:
    malicious = '<img src=x onerror="globalThis.pwned=1">\n[bad](javascript:alert(1))\n[good](https://example.com/path)'
    rendered = _run_javascript(
        f"renderMarkdown({json.dumps(malicious)})",
        "escapeHtml",
        "renderInlineMarkdown",
        "tableCells",
        "isTableDivider",
        "isBlockStart",
        "renderMarkdown",
    )

    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="https://example.com/path"' in rendered


def test_native_datetime_controls_inherit_language_before_creation() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    theme_init = (ROOT / "deepdesk" / "static" / "theme-init.js").read_text("utf-8")

    assert 'type="datetime-local" lang="zh-CN"' not in index
    assert 'document.documentElement.lang = savedLanguage === "en" ? "en-US" : "zh-CN"' in theme_init
    assert index.index('theme-init.js?v=10') < index.index('id="scheduleStartAt"')
    assert '/static/vendor/katex/katex.min.css?v=1' in index
    assert '/static/vendor/katex/katex.min.js?v=1' in index

    source = APP_JS.read_text("utf-8")
    editorial = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")
    assert 'function initializeScheduleDatePickers()' in source
    assert index.count('<span class="datetime-input-shell"><input') == 2
    assert 'localized-datetime-empty' not in editorial
    assert 'content: attr(data-datetime-placeholder)' not in editorial
    assert '.datetime-input-shell > input[data-local-datetime]' in editorial
    assert '/static/vendor/air-datepicker/air-datepicker.js?v=3.6.0' in index


def test_header_settings_and_language_icons_share_the_same_geometry() -> None:
    editorial = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")

    assert '.header-settings-button .settings-icon,\n.language-toggle .language-icon {' in editorial
    shared_icons = editorial[editorial.index('.header-settings-button .settings-icon,') :]
    assert 'width: 22px;' in shared_icons
    assert 'height: 22px;' in shared_icons
    assert (
        '.language-toggle .language-icon {\n'
        '  display: block;\n'
        '  flex: 0 0 22px;\n'
        '}'
    ) in editorial
    language_button = editorial[editorial.index('.language-toggle,\n.mobile-nav {') :]
    assert 'width: 40px;' in language_button
    assert 'height: 40px;' in language_button
    assert 'padding: 0;' in language_button
    assert '.header-actions .language-toggle {\n  padding: 0;\n}' in editorial


def test_voice_final_response_prefers_the_safe_display_result() -> None:
    voice = (ROOT / "deepdesk" / "static" / "voice.js").read_text("utf-8")

    assert "const displayResult = task?.display_result ?? task?.result;" in voice
    assert 'appendMessage("assistant", displayResult)' in voice
    assert "speechQueue = speechChunks(displayResult)" in voice


def test_assistant_and_team_events_prefer_the_safe_display_copy() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderEvent")

    assert "event.data.display_content ?? event.data.content" in renderer
    assert "renderMarkdown(assistantContent)" in renderer
    assert "generatedMediaMarkup(assistantContent)" in renderer


def test_code_blocks_wrap_long_lines_without_losing_indentation() -> None:
    css = (ROOT / "deepdesk" / "static" / "scroll-fix.css").read_text("utf-8")
    code_rule = css[css.index(".markdown pre code {") :]
    code_rule = code_rule[: code_rule.index("}")]
    assert "white-space: pre-wrap" in code_rule
    assert "overflow-wrap: anywhere" in code_rule
    assert "word-break: break-word" in code_rule


def test_runtime_status_recovers_automatically_after_a_service_restart() -> None:
    source = APP_JS.read_text("utf-8")
    load_status = _function_source(source, "loadStatus")

    assert "let statusRetryDelay = 1000" in source
    assert "statusRetryDelay = 1000" in load_status
    assert "setTimeout(loadStatus, statusRetryDelay)" in load_status
    assert "Math.min(statusRetryDelay * 2, 10000)" in load_status
    assert "runtimePollTimer = setTimeout(loadStatus, 15000)" in load_status


def test_settings_panel_recovers_automatically_after_a_service_restart() -> None:
    source = APP_JS.read_text("utf-8")
    load_settings = _function_source(source, "loadSettings")
    css = (ROOT / "deepdesk" / "static" / "history.css").read_text("utf-8")

    assert "let settingsRetryDelay = 1000" in source
    assert "const requestGeneration = ++settingsRequestGeneration" in load_settings
    assert "settingsRetryDelay = 1000" in load_settings
    assert "setTimeout(() =>" in load_settings
    assert 'panel?.classList.contains("active")' in load_settings
    assert "Math.min(settingsRetryDelay * 2, 10000)" in load_settings
    assert 'id="retrySettings"' in load_settings
    assert "Settings will load automatically" in load_settings
    assert ".settings-reconnect { grid-column:1 / -1" in css
    assert ".artifact-list,.schedule-list { display:grid; grid-template-columns:minmax(0,1fr); min-width:0" in css
    assert ".artifact-card,.schedule-card { display:flex; align-items:center; gap:12px; width:100%; min-width:0; max-width:100%" in css


def test_read_only_api_calls_retry_short_loopback_disconnects_only() -> None:
    source = APP_JS.read_text("utf-8")
    api_source = _function_source(source, "api")

    assert 'const attempts = method === "GET" ? 3 : 1' in api_source
    assert "250 * (2 ** attempt)" in api_source
    assert "failed to fetch|networkerror|load failed" in api_source


def test_dynamic_english_feedback_is_localized() -> None:
    source = APP_JS.read_text("utf-8")
    for label in [
        "Settings saved and applied",
        "Stopping task…",
        "Chat deleted",
        "Delete schedule",
        "Continue this task: enter the next instruction…",
        "tool calls",
    ]:
        assert label in source
    assert "The OpenClaw extension catalog is unavailable" in source
    assert "other capabilities are unaffected" in source


def test_english_schedule_panel_has_no_initial_chinese_or_stuck_loading_state() -> None:
    source = APP_JS.read_text("utf-8")
    workspace_english = _function_source(source, "applyWorkspaceEnglish")
    interface_english = _function_source(source, "applyEnglishInterface")
    schedule_loader = _function_source(source, "loadSchedules")
    start = _function_source(source, "start")

    # This helper must be scoped here. It previously existed only inside
    # applyEnglishInterface, causing a ReferenceError before loadSchedules().
    assert "const replaceLabelTexts =" in workspace_english
    assert workspace_english.index("const replaceLabelTexts =") < workspace_english.index(
        'if (name === "schedules")'
    )
    for text in [
        "Describe what the Agent should complete when the time arrives",
        "Daily and repeating tasks may have an end time",
        "Create scheduled task",
    ]:
        assert text in workspace_english
        assert text in interface_english
    assert "Loading scheduled tasks…" in interface_english
    assert "No scheduled tasks yet." in schedule_loader
    assert 'interface_language: isEnglish() ? "en" : "zh"' in start
    assert "voice_request: Boolean(voiceRequest)" in start


def test_human_takeover_system_messages_are_localized_by_structured_code() -> None:
    source = APP_JS.read_text("utf-8")
    focus_message = _function_source(source, "takeoverFocusMessage")
    known_message = _function_source(source, "localizeKnownSystemMessage")
    assert 'focus.code === "target_window_missing"' in focus_message
    assert 'focus.code === "target_window_not_found"' in focus_message
    assert 'focus.code === "target_window_focused"' in focus_message
    assert 'focus.code === "target_window_focus_failed"' in focus_message
    assert "The model did not identify a target window" in focus_message
    assert "请先点击接管操作" in known_message
    assert "Select Take over before continuing." in known_message


def test_provider_key_status_survives_switching_to_english() -> None:
    source = APP_JS.read_text("utf-8")
    load_settings = _function_source(source, "loadSettings")
    placeholders = _function_source(source, "setProviderKeyPlaceholders")
    dynamic_translation = _function_source(source, "translateSettingsDynamic")

    assert "hydrateSettingsPreferences(settings)" in load_settings
    assert "setProviderKeyPlaceholders(settings)" in _function_source(source, "hydrateSettingsPreferences")
    assert 'settings.feishu_configured' in placeholders
    assert 'settings.feishu_open_id_configured' in placeholders
    assert 'settings.aicodemirror_key_configured' in placeholders
    assert 'settings.aicodemirror_fable_key_configured' in placeholders
    assert "Saved securely; enter a new key to replace it" in placeholders
    assert "settings.openclaw_deepseek_key_configured" in source
    assert 'settingAicodemirrorKeyStatus' in placeholders
    assert 'settingAicodemirrorFableKeyStatus' in placeholders
    assert 'setAttr("#settingFeishuAppId", "placeholder"' not in dynamic_translation
    assert 'setAttr("#settingFeishuOpenId", "placeholder"' not in dynamic_translation
    assert 'setAttr("#settingAicodemirrorKey", "placeholder"' not in dynamic_translation


def test_english_settings_remove_retired_trace_and_theme_controls() -> None:
    source = APP_JS.read_text("utf-8")
    interface_english = _function_source(source, "applyEnglishInterface")
    dynamic_translation = _function_source(source, "translateSettingsDynamic")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    assert 'id="settingCompactTrace"' not in index
    assert 'id="settingThemeMode"' not in index
    assert 'data-settings-target="appearance"' not in index
    assert "trace-view-options" not in interface_english
    assert "accent-options" not in dynamic_translation


def test_additional_provider_picker_does_not_duplicate_deepseek() -> None:
    source = APP_JS.read_text("utf-8")
    render_rows = _function_source(source, "renderModelProviderRows")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    configurable = source[
        source.index("const configurableModelProviderLabels"):
        source.index("function modelDisplayName")
    ]
    assert 'deepseek: "DeepSeek"' not in configurable
    assert 'openai: "OpenAI"' in configurable
    assert 'anthropic: "Anthropic"' in configurable
    assert 'google: "Google"' in configurable
    assert 'xai: "xAI"' in configurable
    assert 'entry.provider !== "deepseek"' in render_rows
    assert "DeepSeek 使用上方独立的主密钥和备用密钥" in index
    assert 'id="settingAicodemirrorKey"' in index
    assert 'id="settingAicodemirrorKeyStatus"' in index
    assert 'id="settingAicodemirrorFableKey"' in index
    assert 'id="settingAicodemirrorFableKeyStatus"' in index
    assert 'id="settingAicodemirrorFableKeyHelp"' in index
    assert "Official Channel Stable" in source
    assert '<label class="model-setting">默认模型' in index
    assert '["Default model", "Default reasoning depth", "Request timeout (seconds)"]' in source


def test_reasoning_depth_slider_is_task_scoped_and_saved_as_default() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "composer-controls.css").read_text("utf-8")

    assert 'id="reasoningPreference" type="range"' in index
    assert 'id="settingReasoningEffort"' in index
    assert 'reasoning_effort: reasoningPreferenceValue()' in source
    assert '["reasoning_effort", "#settingReasoningEffort", "string"]' in source
    assert 'syncReasoningAvailability(activeSelector, task.reasoning_effort || "auto")' in source
    runtime_status = _function_source(source, "renderRuntimeStatus")
    assert "trustModelCapabilities = true" in runtime_status
    assert "trustModelCapabilities && Array.isArray(status.available_models)" in runtime_status
    assert 'syncModelSelectors(settingsProjection)' in runtime_status
    selectors = _function_source(source, "syncModelSelectors")
    assert 'syncReasoningAvailability("", initialReasoning)' in selectors
    assert '!reasoningDefaultLoaded' in selectors
    assert 'let activeReasoningLevels = ["auto"];' in source
    assert 'container.replaceChildren(...activeReasoningLevels.map(() => document.createElement("i")))' in source
    assert "syncReasoningDots();" in source
    assert 'class="reasoning-picker" aria-label="思考深度" hidden' in index
    assert '<div class="reasoning-dots" aria-hidden="true"><i></i></div>' in index
    assert 'id="reasoningPreference" type="range" min="0" max="0"' in index
    assert 'availableModelOptions.find((item) => item.selector === selector)?.reasoning' in source
    assert 'id="fastAnswer"' not in index
    assert 'fast_answer' not in source
    assert 'function syncFastAnswerAvailability' not in source
    assert 'service_tier: fast' not in source
    assert 'input.disabled = !capability.supported;' in source
    assert '<option value="fast">' not in index
    assert "#reasoningPreference::-webkit-slider-thumb" in css
    assert '.reasoning-picker[data-reasoning-tone="intensive"]' in css
    assert "reasoning-particles-purple" in css
    assert "reasoning-particles-gold" not in css
    assert "reasoning-dot-drift-a" in css
    assert "reasoning-dot-drift-b" in css
    assert "reasoning-dot-drift-c" in css
    assert "1.37s" in css and "1.91s" in css and "2.23s" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "--reasoning-fill-width" in css
    assert 'class="reasoning-thumb-mask"' in index
    assert "left: calc(var(--reasoning-thumb-center) - 13.5px)" in css
    assert "const thumbCenter = thumbDiameter / 2 + progress * Math.max(0, trackWidth - thumbDiameter)" in source
    assert "const fillWidth = index === activeReasoningLevels.length - 1 ? trackWidth : thumbCenter" in source
    assert "clip-path: inset(0 round 999px);" in css
    assert 'picker.dataset.reasoningEdge = index === 0 ? "min"' in source
    assert '.reasoning-picker[data-reasoning-edge="min"] .reasoning-slider-wrap::after' in css
    assert "opacity: 0;" in css
    assert "reasoningSliderResizeObserver.observe(reasoningSliderWrap)" in source
    assert 'wrap.style.setProperty("--reasoning-thumb-center"' in source
    assert 'picker.dataset.reasoningTone = intensive ? "intensive" : "standard"' in source
    selectors = _function_source(source, "syncModelSelectors")
    assert 'defaultModel.innerHTML = `<option value="auto">' in selectors
    assert 'currentDefault !== "auto"' in selectors
    assert 'defaultModelSelector = settings.model || "auto";' in selectors
    assert 'syncReasoningAvailability("", initialReasoning);' in selectors
    assert 'renderRuntimeStatus(latestStatus, { trustModelCapabilities: false })' in source
    assert 'href="https://www.aicodemirror.ai/"' in index
    assert 'if (option) return option.model || option.selector;' in source
    assert 'AI Code Mirror · OpenAI' not in source
    assert 'AI Code Mirror · Claude' not in source
    assert 'AI Code Mirror · Gemini' not in source


@pytest.mark.parametrize("width,expected_height", [(390, 44), (800, 27)])
def test_reasoning_native_thumb_and_mask_share_center_without_hit_area_overlap(monkeypatch, width, expected_height):
    playwright = pytest.importorskip("playwright.sync_api")
    if not (ROOT / "work/browser-runtime").is_dir():
        pytest.skip("Packaged Chromium runtime is unavailable in this fixture")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "work/browser-runtime"))
    css = "\n".join((ROOT / "deepdesk/static" / name).read_text("utf-8") for name in (
        "style.css", "composer-controls.css", "theme.css", "professional-ui.css", "editorial-ui.css",
    ))
    markup = """<div style="display:flex;flex-direction:column;align-items:start;margin:20px 8px;width:350px;max-width:90vw">
<button id="before">Before</button><div class="reasoning-picker" style="width:100%;margin:0;flex:none">
<div class="reasoning-picker-head">Reasoning</div><div class="reasoning-slider-wrap">
<input id="reasoningPreference" type="range" min="0" max="5" value="3">
<div class="reasoning-dots"><i></i></div><span class="reasoning-thumb-mask"></span>
</div></div><button id="after">After</button></div>"""
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 260})
            page.route("**/*", lambda route: route.abort())
            page.set_content("<style>" + css + "</style>" + markup)
            page.add_script_tag(content="""const $=s=>document.querySelector(s),activeReasoningLevels=['auto','low','medium','high','xhigh','max'];
const reasoningPreferenceValue=()=>activeReasoningLevels[Number($('#reasoningPreference').value)];
""" + _function_source(APP_JS.read_text("utf-8"), "updateReasoningVisualState") + """;
$('#reasoningPreference').addEventListener('input',()=>updateReasoningVisualState());updateReasoningVisualState();""")
            geometry = page.evaluate("""() => Object.fromEntries(['#reasoningPreference','.reasoning-slider-wrap','.reasoning-thumb-mask','#before','#after'].map(s=>{
const r=document.querySelector(s).getBoundingClientRect();return [s,{top:r.top,bottom:r.bottom,height:r.height,center:r.top+r.height/2}]}))""")
            control = geometry["#reasoningPreference"]
            assert control["height"] == expected_height
            assert abs(control["center"] - geometry[".reasoning-thumb-mask"]["center"]) < 0.1
            assert control["top"] >= geometry["#before"]["bottom"]
            assert control["bottom"] <= geometry["#after"]["top"]
            slider = page.locator("#reasoningPreference")
            slider.focus()
            for key, expected in [("Home", "0"), ("ArrowRight", "1"), ("End", "5")]:
                page.keyboard.press(key)
                assert slider.input_value() == expected
            dots = page.locator(".reasoning-dots").bounding_box()
            assert abs(dots["y"] + dots["height"] / 2 - control["center"]) < 0.1
            box = slider.bounding_box()
            page.mouse.move(box["x"] + box["width"] - 13.5, control["center"])
            page.mouse.down()
            page.mouse.move(box["x"] + 13.5, control["center"], steps=8)
            page.mouse.up()
            assert slider.input_value() == "0"
            page.locator("#after").click()
            assert slider.input_value() == "0"
        finally:
            browser.close()


def test_reasoning_fill_has_no_independent_position_or_visibility_animation() -> None:
    css = (ROOT / "deepdesk/static/composer-controls.css").read_text("utf-8")
    rule = css.split(".reasoning-slider-wrap::after {", 2)[2].split("}", 1)[0]
    transition = rule.split("transition:", 1)[1].split(";", 1)[0]
    properties = [part.strip().split()[0] for part in transition.split(",")]
    assert properties == ["background", "box-shadow"]
    source = APP_JS.read_text("utf-8")
    listener = source.split('$("#reasoningPreference").addEventListener("input", () => {', 1)[1].split("});", 1)[0]
    assert "setReasoningPreference(reasoningPreferenceValue());" in listener
    assert not any(timer in listener for timer in ("setTimeout", "requestAnimationFrame", "debounce", "await"))


def test_reasoning_fill_and_mask_follow_rapid_back_and_forth_values_synchronously() -> None:
    result = _run_javascript(
        """(() => {
          globalThis.activeReasoningLevels = ['auto', 'low', 'medium', 'high', 'max'];
          const styles = {};
          const wrap = {getBoundingClientRect: () => ({width: 205}),
            style: {setProperty: (name, value) => { styles[name] = value; }}};
          const picker = {dataset: {}, querySelector: () => wrap};
          globalThis.$ = () => ({closest: () => picker});
          const sequence = [0,4,1,3,0,2,4,0,4,2];
          return sequence.map(index => {
            updateReasoningVisualState(activeReasoningLevels[index]);
            const center = 13.5 + index / 4 * (205 - 27);
            return styles['--reasoning-thumb-center'] === `${center}px`
              && styles['--reasoning-fill-width'] === `${index === 4 ? 205 : center}px`
              && picker.dataset.reasoningEdge === (index === 0 ? 'min' : index === 4 ? 'max' : 'middle');
          });
        })()""",
        "updateReasoningVisualState",
    )
    assert result == [True] * 10


def test_response_model_picker_is_polished_accessible_and_model_only() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    composer = (ROOT / "deepdesk" / "static" / "composer-controls.css").read_text("utf-8")
    professional = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert 'id="modelPreferenceButton"' in index
    assert 'aria-haspopup="listbox"' in index
    assert 'id="modelPreferenceMenu" class="model-select-menu" role="listbox"' in index
    assert '<option value="auto" selected>自动</option>' in index
    assert 'uiText("自动", "Automatic")' in source
    assert '自动选择最合适的已配置模型' not in source
    assert "function initializeModelPreferencePicker()" in source
    assert "function refreshModelPreferenceUI()" in source
    assert 'item.setAttribute("role", "option")' in source
    assert 'item.setAttribute("aria-selected"' in source
    assert 'item.classList.add("model-select-option-long")' in source
    assert '.model-select-menu' in composer
    assert 'width: 100%;' in composer
    assert 'max-width: 100%;' in composer
    assert 'backdrop-filter: blur(20px)' in composer
    assert '.model-select-option[aria-selected="true"]' in composer
    assert 'white-space: nowrap;' in composer
    assert '.model-select-option-long {' in composer
    assert 'font-size: 10.75px' not in composer
    assert 'padding-left: 11px;' in composer
    assert 'width:clamp(229px,22vw,236px)' in professional
    assert '.model-picker .model-select-shell' in professional


def test_custom_system_prompt_uses_readable_prose_typography() -> None:
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert '#settingCustomSystemPromptSuffix {' in css
    assert '"Microsoft YaHei UI","Microsoft YaHei",Inter,"Segoe UI",system-ui,sans-serif' in css
    assert 'font-size:14px !important' in css
    assert 'line-height:1.72 !important' in css


def test_private_chat_controls_and_assets_are_removed() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "composer-controls.css").read_text("utf-8")

    assert "privacyChatToggle" not in index
    assert "privacyComposerState" not in index
    assert "privacyComposerState" not in source
    assert not (ROOT / "deepdesk" / "static" / "icons" / "ghost.svg").exists()
    assert not (ROOT / "deepdesk" / "static" / "privacy-chat.css").exists()
    assert "privacyMode" not in source
    assert "private-chat" not in css


def test_unsaved_default_model_is_not_overwritten_by_late_settings_refresh() -> None:
    source = APP_JS.read_text("utf-8")
    selectors = _function_source(source, "syncModelSelectors")

    assert "let settingModelDirty = false" in source
    assert "let settingReasoningDirty = false" in source
    assert "const pendingDefault = settingModelDirty" in selectors
    assert 'const currentDefault = isRetiredBuiltinModel(pendingDefault) ? "auto" : pendingDefault;' in selectors
    assert "const currentReasoning = settingReasoningDirty" in selectors
    assert '$("#settingModel").addEventListener("change"' in source
    assert '$("#settingReasoningEffort").addEventListener("change"' in source
    assert "syncDefaultReasoningOptions(currentDefault, currentReasoning)" in selectors
    assert "settingModelDirty = false;" in source
    hydrate = _function_source(source, "hydrateSettingsPreferences")
    assert hydrate.index("settingModelDirty = false;") < hydrate.index("syncModelSelectors(settings)")
    assert hydrate.index("settingReasoningDirty = false;") < hydrate.index("syncModelSelectors(settings)")
    reconcile = _function_source(source, "reconcileSettingsSave")
    assert reconcile.index("hydrateSettingsPreferences(updated)") < reconcile.index("applySettingsPreferenceDraft(keep)")
    assert '$("#workspaceDialog").addEventListener("close", () =>' in source


def test_sidebar_resizes_responsively_and_never_exceeds_one_third() -> None:
    source = APP_JS.read_text("utf-8")
    history_css = (ROOT / "deepdesk" / "static" / "history.css").read_text("utf-8")
    professional_css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")
    editorial_css = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    assert 'id="sidebarResizer"' in index
    assert 'role="separator"' in index
    assert "Math.floor(shellWidth / 3)" in source
    assert 'window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY' in source
    assert 'event.key === "ArrowRight"' in source
    assert "grid-template-columns:minmax(236px,var(--sidebar-width)) 14px minmax(0,1fr)" in professional_css
    assert "width:14px" in professional_css
    assert "height:100%" in professional_css
    assert "margin:0" in professional_css
    # editorial-ui.css is loaded last and therefore owns the effective grid.
    # It must consume the same custom property written by applySidebarWidth(),
    # retain a forgiving hit target, and share the JavaScript drawer cutoff.
    assert "minmax(236px, var(--sidebar-width, var(--editorial-sidebar-size))) 14px" in editorial_css
    assert "width: 14px" in editorial_css
    assert "min-width: 14px" in editorial_css
    assert "background: var(--editorial-sidebar);" in editorial_css
    assert "right: 0;" in editorial_css
    assert "left: auto;" in editorial_css
    assert "transform: none;" in editorial_css
    assert "touch-action: none" in editorial_css
    assert ".sidebar-resizer:focus-visible {" in editorial_css
    assert "outline: none" in editorial_css
    assert "@media (max-width: 960px)" in editorial_css
    assert "@media (max-width: 1024px)" not in editorial_css
    assert 'resizer.addEventListener("dblclick"' in source
    assert 'resizer.addEventListener("mousedown", beginDrag)' in source
    assert 'window.addEventListener("mousemove"' in source
    assert 'document.addEventListener("pointerdown", beginDragFromSidebarEdge, true)' in source
    assert 'const modalLayerOpen = () => Boolean(document.querySelector("dialog[open]"))' in source
    assert "dragging || modalLayerOpen()" in source
    assert "body:has(dialog[open]) .sidebar-resizer" in editorial_css
    assert "flex:0 0 6px" in professional_css
    assert "flex:0 0 8px" in professional_css
    assert "grid-template-columns: minmax(0, 1fr) minmax(112px, .82fr)" in history_css
    assert "padding: 7px 26px 7px 9px" not in history_css


def test_rename_dialog_is_fully_localized_in_english() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    assert 'id="renameTaskDialogKicker"' in index
    assert 'id="renameTaskHint"' in index
    assert 'setText("#renameTaskDialogKicker", "Chat name")' in source
    assert 'setText("#renameTaskHint", "The name is used only in the task list and does not change the chat.")' in source
    assert 'setAttr("#closeRenameTaskDialog", "aria-label", "Close rename dialog")' in source


def test_remote_tasks_are_synced_into_the_open_history_without_restart() -> None:
    source = (ROOT / "deepdesk" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function scheduleTaskHistorySync" in source
    assert "loadTaskHistory({ background: true })" in source
    assert '["feishu", "telegram"].includes(task.source)' in source
    assert 'document.addEventListener("visibilitychange"' in source


def test_task_submission_is_blocked_immediately_when_no_model_key_is_configured() -> None:
    source = APP_JS.read_text("utf-8")
    start = _function_source(source, "start")

    assert "latestStatus?.primary_key_configured" in start
    assert "latestStatus?.model_provider_count" in start
    assert "latestStatus?.local_models?.ready" in start
    assert 'openWorkspacePanel("settings")' in start
    assert 'activateSettingsSection("providers")' in start
    assert start.index("if (latestStatus && !modelApiConfigured)") < start.index("startRequestPending = true")


def test_ordinary_actions_are_autonomous_but_mandatory_approvals_require_consent() -> None:
    source = APP_JS.read_text("utf-8")
    start = _function_source(source, "start")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")

    assert 'policy: "autonomous"' in start
    assert "default_policy" not in _function_source(source, "settingsPreferenceValues")
    assert '"default_policy": ApprovalPolicy.AUTONOMOUS' in (ROOT / "deepdesk/runtime_settings.py").read_text("utf-8")
    assert "pendingApproval" not in source
    assert "rememberApprovalPolicy" not in source
    assert 'id="policy"' not in index
    assert 'id="approval"' not in index
    assert '/api/approvals/' not in _function_source(source, "poll")
    assert 'id="taskApproval"' in index
    assert 'approved: Boolean(approved)' in _function_source(source, "resolveTaskApproval")


def test_retired_frontend_controls_and_brand_migrations_are_removed() -> None:
    static = ROOT / "deepdesk" / "static"
    index = (static / "index.html").read_text("utf-8")
    app = (static / "app.js").read_text("utf-8")
    voice = (static / "voice.js").read_text("utf-8")
    theme = (static / "theme-init.js").read_text("utf-8")

    for retired_id in ("settingPolicy", "settingUnlimitedSteps", "settingMaxSteps", "stepReminder"):
        assert f'id="{retired_id}"' not in index
        assert f'#{retired_id}' not in app
    assert "voice-privacy" not in index
    assert "voice-privacy" not in voice
    assert not (static / "approval-fix.css").exists()
    assert not (static / "step-reminder.css").exists()
    assert "max_steps" not in _function_source(app, "settingsPreferenceValues")
    assert 'data["max_steps"] = None' in (ROOT / "deepdesk/runtime_settings.py").read_text("utf-8")
    assert 'const PREVIOUS_LANGUAGE_STORAGE_KEY = "milo.ui-language.v1"' in app
    assert 'const PREVIOUS_RUNTIME_STATUS_STORAGE_KEY = "milo.runtime-status.v1"' in app
    assert 'const PREVIOUS_SIDEBAR_WIDTH_STORAGE_KEY = "milo.sidebar-width.v1"' in app
    assert 'const DEFAULT_MODE = "light"' in theme
    assert 'const DEFAULT_ACCENT = "#a6533f"' in theme
    assert "PREVIOUS_MODE_KEY" not in theme
    combined_css = "\n".join(path.read_text("utf-8") for path in static.glob("*.css"))
    for retired_selector in (
        ".approval", ".step-reminder", ".trace-mode-toggle", ".surface-actions",
        ".agent-card", ".agent-grid", ".composer-toolbar", ".history-title", ".vision-install",
    ):
        assert retired_selector not in combined_css


def test_tool_summaries_localize_internal_names_in_both_languages() -> None:
    source = APP_JS.read_text("utf-8")
    summary = _function_source(source, "localizedToolSummary")
    render = _function_source(source, "renderEvent")

    assert 'if (!isEnglish()) return data.summary' not in summary
    assert 'localizedToolName(tool)' in summary
    assert 'process_manager: "进程管理"' in source
    assert 'process_manager: "Process manager"' in source
    assert 'renderCommandActivity(event, taskStatus);' in render
    assert "tool_call:" not in render
    assert '`${localizedToolName(event.data.tool)} · ${uiText("返回结果", "Result")}`' in render


def test_language_switch_keeps_the_open_chat() -> None:
    source = APP_JS.read_text("utf-8")

    assert "taskId || continuationTaskId || currentTaskSnapshot?.id" in source
    assert 'nextUrl.searchParams.set("task", activeTaskId)' in source
    assert "currentTaskSnapshot?.private_chat" not in source
    assert "window.location.assign(nextUrl.toString())" in source
    assert 'nextUrl.searchParams.set("lang", nextLanguage)' in source


def test_language_switch_preserves_the_manual_response_model() -> None:
    source = APP_JS.read_text("utf-8")
    language_switch = source[source.index('$("#languageToggle").onclick'):]
    selector_sync = _function_source(source, "syncModelSelectors")

    assert 'nextUrl.searchParams.set("model", $("#modelPreference")?.value || "auto")' in language_switch
    assert 'pendingLanguageSwitchModel = String(initialQuery.get("model") || "").slice(0, 200)' in source
    assert "preference.dataset?.languageResumeModel || pendingLanguageSwitchModel" in selector_sync
    assert 'cleanUrl.searchParams.delete("model")' in selector_sync


def test_language_switch_survives_unavailable_web_storage() -> None:
    source = APP_JS.read_text("utf-8")

    assert 'new URLSearchParams(window.location.search).get("lang")' in source
    assert 'const nextLanguage = isEnglish() ? "zh" : "en"' in source


def test_language_switch_reuses_runtime_status_without_reprobing() -> None:
    source = APP_JS.read_text("utf-8")
    language_switch = source[source.index('$("#languageToggle").onclick'):]

    assert 'const RUNTIME_STATUS_STORAGE_KEY = "elren.runtime-status.v1"' in source
    assert "latestStatus = readCachedRuntimeStatus()" in source
    assert "cacheRuntimeStatus(latestStatus)" in language_switch
    assert "if (latestStatus) renderRuntimeStatus(latestStatus, { trustModelCapabilities: false })" in source
    assert 'api("/api/runtimes/probe"' not in language_switch.split("applyEnglishInterface();", 1)[0]


def test_release_ui_uses_real_brand_asset_and_bounded_takeover_panel() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    human_css = (ROOT / "deepdesk" / "static" / "human-action.css").read_text("utf-8")
    professional_css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert '<div class="orb"><img src="/static/elren-icon.png?v=2" alt="" /></div>' in index
    assert "top:50%" in human_css
    assert "max-height:calc(100% - 250px)" in human_css
    assert "overflow:auto" in human_css
    assert "inset:14px" in human_css
    assert ".orb img" in professional_css


def test_language_neutral_icons_replace_chinese_event_glyphs() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    app = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert "app-symbol-hand" in index
    assert ">手<" not in index
    assert "taskSourceIcon(task.source)" in app
    assert 'tool_result: [appSymbol("send")' in app
    assert 'error: [appSymbol("warning")' in app
    terminal = _function_source(app, "terminalStatePresentation")
    assert 'appSymbol("warning")' in terminal
    assert 'appSymbol("close")' in terminal
    assert '${appSymbol(artifactIconName(artifact))}' in app
    assert '<span class="artifact-icon">${artifact.kind === "folder" ? "▦" : "↧"}</span>' not in app
    assert 'uiText("飞", "F")' not in app
    assert 'uiText("你", "You")' not in app
    assert '-webkit-mask:var(--app-icon)' in css
    assert 'url("/static/icons/hand.svg?v=1")' in css
    assert (ROOT / "deepdesk" / "static" / "icons" / "LICENSE-lucide.txt").is_file()


def test_transient_service_failures_recover_without_polluting_the_chat() -> None:
    source = APP_JS.read_text("utf-8")
    api = _function_source(source, "api")
    status = _function_source(source, "loadStatus")
    poll = _function_source(source, "poll")

    assert "const retryableStatuses = new Set([408, 425, 429, 502, 503, 504])" in api
    assert "new AbortController()" in api
    assert 'controller.abort("request-timeout")' in api
    assert "const recovered = runtimeConnectionLost" in status
    assert 'uiText("本地服务连接已恢复", "Local service connection restored")' in status
    assert "const recoveredFromPollFailure = pollFailureCount > 0" in poll
    assert 'uiText("任务连接已恢复", "Task connection restored")' in poll
    poll_catch = poll.split("} catch (error) {", 1)[1]
    assert "renderEvent" not in poll_catch
    assert "document.hidden ? 2500" in poll


def test_settings_drafts_are_not_silently_overwritten_or_discarded() -> None:
    source = APP_JS.read_text("utf-8")
    opener = _function_source(source, "openWorkspacePanel")
    loader = _function_source(source, "loadSettings")
    closer = _function_source(source, "requestCloseWorkspaceDialog")

    assert "let settingsFormDirty = false" in source
    assert "let settingsFormHydrating = true" in source
    assert 'form.toggleAttribute("inert", settingsFormHydrating)' in source
    assert 'form.setAttribute("aria-busy", settingsFormHydrating ? "true" : "false")' in source
    assert "let settingsFormBaseline = null" in source
    assert '.filter((control) => !control.closest("#discussionTeamRows"))' in source
    assert "discussion_team: canonicalDiscussionTeam(team)" in source
    assert "let recoveredDiscussionTeamDraftPending = false" in source
    assert "showRecoveredDiscussionTeamDraftHint()" in source
    assert "let discussionTeamDraftTimer = null" in source
    assert "setTimeout(() => {\n    persistDiscussionTeamDraft();\n    markSettingsFormDirty();\n  }, 180)" in source
    assert 'event.target.closest?.("#discussionTeamRows")' in source
    assert 'if (name === "settings" && !settingsFormDirty) loadSettings()' in opener
    assert "if (settingsFormDirty) return" in loader
    assert "const hasRecoverableDraft = discussionTeamDraftDiffers(recoverableDraft, savedTeam)" in loader
    assert "const teamToRender = hasRecoverableDraft ? recoverableDraft : savedTeam" in loader
    assert "if (recoverableDraft && !hasRecoverableDraft) clearDiscussionTeamDraft()" in loader
    assert "recoveredDiscussionTeamDraftPending = hasRecoverableDraft" in loader
    assert "setSettingsFormDirty(false, { announce: false })" in loader
    assert "rememberSettingsFormBaseline()" in loader
    assert "discardSettingsChanges()" in closer
    assert '$("#workspaceDialog").addEventListener("cancel"' in source
    assert '$("#settingsForm").addEventListener("input", (event) =>' in source
    assert '$("#settingsForm").addEventListener("change", markSettingsFormDirty)' in source
    assert 'window.addEventListener("beforeunload"' in source
    assert 'title: uiText("放弃未保存的设置？", "Discard unsaved settings?")' in source


def test_settings_dirty_state_is_value_based_and_ignores_identical_stale_team_drafts() -> None:
    source = APP_JS.read_text("utf-8")
    marker = _function_source(source, "markSettingsFormDirty")
    comparison = _function_source(source, "discussionTeamDraftDiffers")

    assert "settingsFormSignature() !== settingsFormBaseline" in marker
    assert "if (settingsFormHydrating) return" in marker
    assert 'if (!dialog?.open || !settingsActive) return' in marker
    assert "setSettingsFormDirty(dirty, { announce: dirty })" in marker
    assert "if (recoveredDiscussionTeamDraftPending)" in marker
    assert "showRecoveredDiscussionTeamDraftHint()" in marker
    assert "JSON.stringify(canonicalDiscussionTeam(draft))" in comparison
    assert "JSON.stringify(canonicalDiscussionTeam(saved))" in comparison
    assert 'originalName === "组长" || originalName === "Leader"' in source
    assert "id:" not in _function_source(source, "canonicalDiscussionTeam")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    assert 'id="settingEightctlEmail" type="email" autocomplete="off"' in index
    assert "accent-swatch" not in source
    assert "settingAccentCustom" not in source


def test_settings_refresh_preserves_unsaved_reasoning_choices() -> None:
    source = APP_JS.read_text("utf-8")
    selectors = _function_source(source, "syncModelSelectors")
    team_reasoning = _function_source(source, "discussionTeamReasoningOptions")

    assert "const currentReasoning = settingReasoningDirty" in selectors
    assert '$("#settingReasoningEffort")?.value' in selectors
    assert "syncDefaultReasoningOptions(currentDefault, currentReasoning)" in selectors
    assert 'if (selected !== "default" && !values.includes(selected)) values.push(selected)' in team_reasoning
    assert 'uiText("当前不可用", "currently unavailable")' in team_reasoning


def test_settings_search_filters_credential_fields_and_small_screen_nav_stays_usable() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")
    voice_css = (ROOT / "deepdesk" / "static" / "voice.css").read_text("utf-8")
    editorial_css = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")
    search = _function_source(source, "applySettingsSearch")

    assert 'class="settings-section-tabs" role="list"' in index
    assert 'id="settingsTabsMore"' in index
    assert "filterToolCredentialSearch(item, query)" in search
    assert "settings-search-subhidden" in source
    assert 'details.dataset.searchOpened = "true"' in source
    assert 'grid-template-columns:minmax(0,1fr) auto minmax(180px,230px)' in voice_css
    assert '.settings-search{grid-column:1/-1;grid-row:1;width:100%;min-width:0;margin-left:0}' in voice_css
    assert 'data-tabs-overflow="true"' in css
    assert "grid-template-columns:minmax(0,1fr) auto minmax(180px,230px)" in css
    assert ".settings-section-nav .settings-tabs-more" in css
    assert ".settings-section-nav .settings-tabs-more[hidden] { display:none!important; }" in css
    assert "grid-column:3" in css
    assert '$("#settingsTabsMore")?.addEventListener("click"' in source
    assert "function alignActiveSettingsSectionTab()" in source
    assert 'tabs?.querySelector("button.active[data-settings-target]")' in source
    assert "function scheduleActiveSettingsSectionAlignment()" in source
    assert "let settingsSectionAlignmentFrame = 0;" in source
    assert "let settingsSectionSettleFrame = 0;" in source
    assert "cancelAnimationFrame(settingsSectionAlignmentFrame)" in source
    assert "cancelAnimationFrame(settingsSectionSettleFrame)" in source
    assert "const rightFadeInset = hasHiddenRight ? 28 : 0;" in source
    assert 'window.addEventListener("resize", scheduleActiveSettingsSectionAlignment);' in source
    assert (
        'window.visualViewport?.addEventListener?.("resize", '
        "scheduleActiveSettingsSectionAlignment);"
    ) in source
    assert "if (nav.hidden || tabs.clientWidth <= 2)" in source
    assert "const availableWithoutMore = tabs.clientWidth + moreWidth" in source
    assert 'uiText("显示更多设置分类", "Show more settings categories")' in source
    assert "flex:0 0 auto" in css
    assert "white-space:nowrap" in css
    assert '#closeWorkspaceDialog:focus-visible' in css
    assert ':root[data-theme="light"] body .history-item:not(.active) .history-prompt' in css
    assert "ArrowRight: index === lastIndex ? 0 : index + 1" in source
    phone_rules = editorial_css[editorial_css.index("@media (max-width: 390px)") :]
    assert ".settings-section-tabs > button[data-settings-target]" in phone_rules
    assert "min-width: 100%" in phone_rules
    assert "flex-basis: 100%" in phone_rules
    assert "min-width: 72%" not in phone_rules


def test_workspace_form_actions_stack_on_phone_widths() -> None:
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    phone_rules = css[css.index("@media (max-width:600px)") :]
    assert ".workspace-dialog .form-actions" in phone_rules
    assert "flex-direction:column" in phone_rules
    assert "align-items:stretch" in phone_rules
    assert ".workspace-dialog .form-actions button { width:100%; }" in phone_rules


def test_runtime_translation_keeps_plugin_card_labels_aligned() -> None:
    source = APP_JS.read_text("utf-8")

    assert '"Model providers", "Tools & plugins", "Workspace", "Portable runtime", "Portable tools", "MCP"' in source
    assert '["工具与插件", status.plugin_health?.ready !== false' in source
    assert "未发现 DeepSeek 视觉端点" not in source


def test_release_composer_keeps_controls_compact_without_hiding_accessibility() -> None:
    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert ".composer-tools { gap:8px; flex-wrap:nowrap; }" in css
    assert ".model-picker { min-width:0; flex:1 1 300px; }" in css
    assert "width:clamp(229px,22vw,236px)" in css
    assert "color-mix(in srgb,var(--accent) 38%,#7b8ca2)" in css
    assert "box-shadow:0 0 0 3px" in css
    assert 'input.closest(".reasoning-picker")?.setAttribute(' in source
    assert 'uiText("思考深度", "Reasoning depth")' in source


def test_desktop_server_avoids_polling_noise_in_launcher_logs_by_default() -> None:
    main_source = (ROOT / "deepdesk" / "main.py").read_text("utf-8")
    assert '"ELREN_ACCESS_LOG"' in main_source
    assert 'os.getenv("MILO_ACCESS_LOG", os.getenv("DEEPDESK_ACCESS_LOG", ""))' in main_source
    assert "access_log=os.getenv(" in main_source


def test_task_history_reloads_after_an_inflight_filter_change() -> None:
    source = APP_JS.read_text("utf-8")
    history = _function_source(source, "loadTaskHistory")

    assert "let historyReloadPending = false;" in source
    assert "historyReloadPending = true;" in history
    assert "historyReloadWaiters.push(resolve)" in history
    assert 'query !== $("#historySearch").value.trim()' in history
    assert 'status !== $("#historyStatus").value' in history
    assert "queueMicrotask(() => loadTaskHistory())" in history


def test_unchanged_background_history_refresh_preserves_the_append_cursor() -> None:
    source = APP_JS.read_text("utf-8")
    history = _function_source(source, "loadTaskHistory")

    request_offset = "const requestOffset = append ? historyOffset : 0;"
    unchanged_return = "if (background && requestFilter === historyLoadedFilter && firstPageSignature === historyFirstPageSignature) return;"
    replacement_reset = "if (!append) historyOffset = 0;"
    assert request_offset in history
    assert "offset: String(requestOffset)" in history
    assert history.index(unchanged_return) < history.index(replacement_reset)
    assert history.count(replacement_reset) == 1


def test_history_append_deduplicates_ids_without_rewinding_the_server_cursor() -> None:
    source = APP_JS.read_text("utf-8")
    history = _function_source(source, "loadTaskHistory")

    assert "result.tasks.filter((task) => !knownHistoryTaskIds.has(task.id))" in history
    assert 'historyMarkup(tasksToRender)' in history
    assert "historyOffset += result.tasks.length;" in history


def test_voice_can_queue_a_follow_up_while_the_agent_is_running() -> None:
    source = APP_JS.read_text("utf-8")
    voice = (ROOT / "deepdesk" / "static" / "voice.js").read_text("utf-8")
    sender = _function_source(source, "sendRunningMessage")
    starter = _function_source(source, "start")

    assert "return sendRunningMessage(prompt);" in starter
    assert "return true;" in sender
    assert "return false;" in sender
    assert 'currentTaskSnapshot?.status === "running" || startRequestPending' not in voice
    assert "const sent = await start({ voiceRequest: true });" in voice
    assert "setVoiceDraft(text);" in voice


def test_event_renderer_recovers_from_missing_or_malformed_event_data() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderEvent")

    assert 'if (!event || typeof event.type !== "string") return;' in renderer
    assert 'typeof event.data !== "object"' in renderer
    assert "Array.isArray(event.data)" in renderer
    assert "event = { ...event, data: {} };" in renderer


def test_frontend_has_one_authoritative_renderer_per_shared_surface() -> None:
    source = APP_JS.read_text("utf-8")

    assert source.count("async function loadArtifacts(") == 1
    assert source.count("function renderCommandActivity(") == 1
    assert "function applyTraceMode(" not in source
    artifacts = _function_source(source, "loadArtifacts")
    assert "result.summary_pending" in artifacts
    assert "loadArtifacts({ background: true })" in artifacts
    assert "if (!background)" in artifacts


def test_mobile_pairing_poll_is_serial_and_stops_with_the_dialog() -> None:
    source = APP_JS.read_text("utf-8")
    pairing = _function_source(source, "createMobilePairing")

    assert "setInterval(async" not in pairing
    assert "const pollPairing = async () =>" in pairing
    assert "if (!dialog.open) return;" in pairing
    assert "mobilePairingPollTimer = setTimeout(pollPairing, 1500);" in pairing


def test_artifact_library_supports_search_and_type_filters() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderArtifacts")

    assert 'id="artifactSearch"' in index
    assert 'id="artifactType"' in index
    assert "latestArtifacts" in renderer
    assert ".filter((artifact) =>" in renderer
    assert "searchable.includes(query)" in renderer
    assert "artifactCategory(artifact) === type" in renderer
    assert '$("#artifactSearch").addEventListener("input", renderArtifacts);' in source
    assert '$("#artifactType").addEventListener("change", renderArtifacts);' in source


def test_workspace_tabs_keep_one_active_accessible_panel_across_switches() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = APP_JS.read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")
    opener = _function_source(source, "openWorkspacePanel")

    assert 'class="workspace-tabs" role="tablist"' in index
    assert index.count('role="tab" data-panel=') == 3
    workspace_dialog = index[index.index('id="workspaceDialog"') : index.index("</dialog>", index.index('id="workspaceDialog"'))]
    assert workspace_dialog.count('role="tabpanel"') == 3
    assert 'button.setAttribute("aria-selected", active ? "true" : "false")' in opener
    assert 'button.tabIndex = active ? 0 : -1' in opener
    assert 'panel.setAttribute("aria-hidden", active ? "false" : "true")' in opener
    assert 'button.focus({ preventScroll: true })' in source
    assert 'ArrowLeft: index === 0 ? lastIndex : index - 1' in source
    assert 'ArrowRight: index === lastIndex ? 0 : index + 1' in source
    assert 'setAttr(".workspace-tabs", "aria-label", "Control center pages")' in source
    assert 'setAttr("#settingsSectionNav", "aria-label", "Settings categories")' in source
    assert '.workspace-tabs button:focus-visible:not(.active)::after,.settings-section-nav button:focus-visible:not(.active)::after' in css
    assert 'background:color-mix(in srgb,var(--accent) 6%,transparent)' in css


def test_artifact_and_schedule_dates_follow_the_current_interface_locale() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    artifacts = _function_source(source, "renderArtifacts")
    schedules = _function_source(source, "loadSchedules")
    description = _function_source(source, "scheduleDescription")
    locale = _function_source(source, "uiDateTimeLocale")

    assert 'isEnglish() ? "en-US" : "zh-CN"' in locale
    assert "formatUiDateTime(artifact.modified * 1000)" in artifacts
    assert 'time datetime="${escapeHtml(new Date(artifact.modified * 1000).toISOString())}"' in artifacts
    assert "formatUiDateTime(schedule.next_run)" in schedules
    assert "formatUiDateTime(schedule.start_at)" in description
    assert "formatUiDateTime(schedule.end_at)" in description
    assert 'id="scheduleStartAt" type="text" data-local-datetime' in index
    assert 'type="datetime-local" lang="zh-CN"' not in index
    assert 'attr("#scheduleStartAt", "lang", "en-US")' in source

    formatted = _run_javascript(
        "(() => { const sample = new Date(2026, 7, 6, 10, 14, 6); "
        "globalThis.isEnglish = () => false; const zh = formatUiDateTime(sample); "
        "globalThis.isEnglish = () => true; const en = formatUiDateTime(sample); "
        "return {zh, en}; })()",
        "uiDateTimeLocale",
        "formatUiDateTime",
    )
    assert "年" in formatted["zh"]
    assert "AM" not in formatted["zh"]
    assert "AM" in formatted["en"]


def test_capability_map_distinguishes_connected_loading_from_connecting() -> None:
    source = APP_JS.read_text("utf-8")

    assert "status.openclaw?.gateway_ready" in source
    assert "OpenClaw connected; loading extensions" in source
    assert "connecting to OpenClaw Gateway" in source


def test_sidebar_runtime_summary_is_compact_but_keeps_full_titles() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderRuntimeStatus")
    professional_css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")

    assert "workspace.split(/[\\\\/]/).filter(Boolean).at(-1)" in renderer
    assert '$("#workspaceText").title = workspace;' in renderer
    assert '$("#modelText").title = modelLabel;' in renderer
    assert "#modelText,#workspaceText,#openclawText" in professional_css
    assert "text-overflow:ellipsis" in professional_css


def test_background_browser_screenshot_uses_wrapped_tool_result_and_compacts_trace() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "renderCommandActivity")

    assert "value?.result && typeof value.result === \"object\"" in renderer
    assert "browserPayload?.screenshot_url" in renderer
    assert "compactPayload.interactive_elements" in renderer
    assert "compactPayload.text.slice(0, 4000)" in renderer


def test_settings_form_can_shrink_without_horizontal_overflow_at_320px() -> None:
    css = (ROOT / "deepdesk" / "static" / "history.css").read_text("utf-8")
    professional_css = (ROOT / "deepdesk" / "static" / "professional-ui.css").read_text("utf-8")
    voice_css = (ROOT / "deepdesk" / "static" / "voice.css").read_text("utf-8")
    editorial_css = (ROOT / "deepdesk" / "static" / "editorial-ui.css").read_text("utf-8")

    assert ".form-grid,.runtime-cards { grid-template-columns:minmax(0,1fr); }" in css
    assert ".form-grid > * { min-width:0; }" in css
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in professional_css
    assert ".command-activity-summary" in editorial_css
    assert "text-overflow: ellipsis" in editorial_css
    assert ".form-grid>.voice-setting{grid-template-columns:minmax(0,1fr)}" in voice_css
    assert ".form-grid>.voice-setting>*{min-width:0}" in voice_css


def test_enhanced_selects_expose_only_the_visible_keyboard_control() -> None:
    source = APP_JS.read_text("utf-8")
    team = _function_source(source, "enhanceDiscussionTeamSelect")
    settings = _function_source(source, "enhanceSettingsSelect")

    for enhancer in (team, settings):
        assert 'select.tabIndex = -1' in enhancer
        assert 'select.setAttribute("aria-hidden", "true")' in enhancer
        assert 'button.id = `${menu.id}-button`' in enhancer
        assert 'containingLabel.htmlFor = button.id' in enhancer


def test_custom_listboxes_keep_tab_navigation_in_the_control_flow() -> None:
    source = APP_JS.read_text("utf-8")
    helper = _function_source(source, "focusAdjacentTabStop")
    model_picker = _function_source(source, "initializeModelPreferencePicker")
    team_picker = _function_source(source, "enhanceDiscussionTeamSelect")
    settings_picker = _function_source(source, "enhanceSettingsSelect")

    assert 'reference.closest("dialog[open]") || document' in helper
    assert '!element.closest("[hidden]")' in helper
    for picker in (model_picker, team_picker, settings_picker):
        assert 'event.preventDefault()' in picker
        assert 'focusAdjacentTabStop(button, { backwards: event.shiftKey })' in picker


def test_mobile_navigation_moves_and_restores_keyboard_focus() -> None:
    source = APP_JS.read_text("utf-8")
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    opener = _function_source(source, "openNavigation")
    closer = _function_source(source, "closeNavigation")

    assert '$("#newTask")?.focus({ preventScroll: true })' in opener
    assert '$("#historySearch")?.focus({ preventScroll: true })' not in opener
    assert '$("#mobileNav")?.focus({ preventScroll: true })' in closer
    assert '$("#navOverlay").onclick = () => closeNavigation({ restoreFocus: true })' in source
    assert 'document.body.classList.contains("nav-open")' in source
    assert 'id="navOverlay" class="nav-overlay" type="button"' in index


def test_long_attachment_names_are_constrained_without_losing_the_full_name() -> None:
    source = APP_JS.read_text("utf-8")
    renderer = _function_source(source, "sentAttachmentMarkup")
    css = (ROOT / "deepdesk" / "static" / "composer-controls.css").read_text("utf-8")

    assert 'class="sent-attachment-name"' in renderer
    assert 'title="${safeName}"' in renderer
    assert ".sent-attachment-names" in css
    assert ".sent-attachment-name" in css
    assert "text-overflow: ellipsis" in css
    assert "max-width: 100%" in css
