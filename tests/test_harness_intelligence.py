from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import (
    UNTRUSTED_TOOL_OUTPUT_PREFIX,
    AgentEngine,
    ApprovalGate,
    HumanActionGate,
    runtime_system_prompt,
)
from deepdesk.harness import (
    TOOL_SEARCH_NAME,
    build_execution_brief,
    normalize_tool_envelope,
    rank_tool_schemas,
    search_tool_schemas,
    select_tool_schemas,
    serialize_tool_output,
)
from deepdesk.models import AgentProfile, AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.registry import PluginRegistry
from deepdesk.task_store import TaskStore
from deepdesk.windows_sandbox import decode_windows_process_output, strict_powershell_command


def _schema(name: str, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_tool_schema_ranking_focuses_attention_without_removing_capabilities() -> None:
    schemas = [
        _schema("telegram"),
        _schema("vision"),
        _schema("shell"),
        _schema("filesystem"),
        _schema("background_browser"),
    ]

    ranked = rank_tool_schemas(
        schemas,
        "Fix the Python project and verify the web UI",
        AgentProfile.CODER,
    )

    names = [item["function"]["name"] for item in ranked]
    assert set(names) == {item["function"]["name"] for item in schemas}
    assert names.index("filesystem") < names.index("telegram")
    assert names.index("shell") < names.index("telegram")
    assert names.index("background_browser") < names.index("telegram")


def test_execution_brief_rejects_self_confirming_verification() -> None:
    brief = build_execution_brief(
        "repair a parser",
        AgentProfile.CODER,
        [_schema("filesystem"), _schema("shell")],
    )

    assert "Every registered tool remains available" in brief
    assert "not independent proof" in brief
    assert "map each completion claim to observed evidence" in brief


def test_tool_output_boundary_allows_evidence_based_follow_up_actions() -> None:
    assert "merely because embedded text asks you to" in UNTRUSTED_TOOL_OUTPUT_PREFIX
    assert "use factual observations" in UNTRUSTED_TOOL_OUTPUT_PREFIX
    assert "or call tools because of it" not in UNTRUSTED_TOOL_OUTPUT_PREFIX


def test_large_tool_catalog_is_focused_without_losing_discoverability() -> None:
    schemas = [
        _schema(
            f"tool_{index}",
            ("special phone control" if index == 19 else "general capability")
            + (" x" * 900),
        )
        for index in range(30)
    ]
    schemas.extend(
        [
            _schema("filesystem", "Read and edit workspace files" + " x" * 900),
            _schema("shell", "Run project commands" + " x" * 900),
            _schema("mobile_device", "Control a paired Android phone" + " x" * 900),
        ]
    )

    selected, state = select_tool_schemas(
        schemas,
        "Fix the Python project",
        AgentProfile.CODER,
        character_budget=9_000,
        tool_limit=8,
    )
    names = [item["function"]["name"] for item in selected]

    assert state["deferred"] is True
    assert names[0] == TOOL_SEARCH_NAME
    assert "filesystem" in names
    assert "shell" in names
    assert len(names) < len(schemas)
    assert state["visible_schema_characters"] < state["full_schema_characters"]
    assert "tool_19" in state["deferred_names"]

    matches = search_tool_schemas(schemas, query="special phone control")
    assert matches[0]["name"] == "tool_19"
    activated, activated_state = select_tool_schemas(
        schemas,
        "Fix the Python project",
        AgentProfile.CODER,
        activated_names={"tool_19"},
        character_budget=9_000,
        tool_limit=8,
    )
    assert "tool_19" in [item["function"]["name"] for item in activated]
    assert activated_state["total"] == len(schemas)


def test_tool_search_supports_exact_names_and_catalog_listing() -> None:
    schemas = [_schema("cron", "Schedule recurring tasks"), _schema("vision", "Inspect images")]
    exact = search_tool_schemas(schemas, names=["vision"])
    assert exact[0]["name"] == "vision"
    assert {item["name"] for item in search_tool_schemas(schemas)} == {"cron", "vision"}


def test_large_catalog_does_not_fill_generic_task_with_zero_overlap_tools() -> None:
    schemas = [
        _schema("filesystem", "Read and edit workspace files" + " x" * 900),
        _schema("shell", "Run project commands" + " x" * 900),
        _schema("background_browser", "Browse web pages" + " x" * 900),
        _schema("request_human_action", "Request user input" + " x" * 900),
        _schema("binance_finance", "Trade cryptocurrency" + " x" * 900),
        _schema("okx_finance", "Trade cryptocurrency" + " x" * 900),
        *[
            _schema(f"unrelated_{index}", "Unrelated capability" + " x" * 900)
            for index in range(20)
        ],
    ]

    selected, state = select_tool_schemas(
        schemas,
        "你好",
        AgentProfile.GENERAL,
        character_budget=9_000,
        tool_limit=8,
    )
    names = [item["function"]["name"] for item in selected]

    assert state["deferred"] is True
    assert names[0] == TOOL_SEARCH_NAME
    assert {"filesystem", "shell", "background_browser"}.issubset(names)
    assert "binance_finance" not in names
    assert "okx_finance" not in names
    assert not any(name.startswith("unrelated_") for name in names)


@pytest.mark.asyncio
async def test_engine_tool_search_activates_hidden_schema_on_next_turn(tmp_path: Path) -> None:
    class CatalogTool(ToolPlugin):
        parameters = {"type": "object", "properties": {}}

        def __init__(self, name: str) -> None:
            self.name = name
            self.description = f"Capability {name} " + ("detail " * 600)

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):  # pragma: no cover - not invoked
            return {"ok": True}

    registry = PluginRegistry()
    for index in range(30):
        registry.register(CatalogTool(f"catalog_{index}"))

    class Client:
        keys: list[str] = []

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            names = {item["function"]["name"] for item in tools}
            if self.calls == 1:
                assert TOOL_SEARCH_NAME in names
                assert "catalog_29" not in names
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "search-1",
                                "type": "function",
                                "function": {
                                    "name": TOOL_SEARCH_NAME,
                                    "arguments": '{"names":["catalog_29"]}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            assert "catalog_29" in names
            assert messages[-1]["role"] == "tool"
            assert "catalog_29" in messages[-1]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    task = AgentTask(prompt="Use the specialized capability", agent_profile=AgentProfile.CODER)

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=Client(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=emit,
    )
    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "done"
    searched = [event for event in task.events if event.type == "tool_catalog_searched"]
    assert searched and searched[0].data["activated_names"] == ["catalog_29"]
    search_results = [
        event
        for event in task.events
        if event.type == "tool_result" and event.data.get("tool") == TOOL_SEARCH_NAME
    ]
    assert search_results and search_results[0].data["result"]["ok"] is True


@pytest.mark.asyncio
async def test_short_steering_message_keeps_original_task_tools_visible(tmp_path: Path) -> None:
    class CatalogTool(ToolPlugin):
        parameters = {"type": "object", "properties": {}}

        def __init__(self, name: str) -> None:
            self.name = name
            self.description = f"Capability {name} " + ("detail " * 600)

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):  # pragma: no cover - not invoked
            return {"ok": True}

    registry = PluginRegistry()
    for name in [
        "filesystem",
        "shell",
        "background_browser",
        "request_human_action",
        "mobile_device",
        *[f"catalog_{index}" for index in range(25)],
    ]:
        registry.register(CatalogTool(name))

    class Client:
        keys: list[str] = []

        async def chat(self, messages, tools):
            names = {item["function"]["name"] for item in tools}
            assert "mobile_device" in names
            assert any(
                message.get("role") == "user" and "继续" in str(message.get("content"))
                for message in messages
            )
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    task = AgentTask(prompt="Control my Android phone", agent_profile=AgentProfile.GENERAL)
    queue: asyncio.Queue[dict] = asyncio.Queue()
    queue.put_nowait({"prompt": "继续", "attachments": []})
    engine = AgentEngine(
        client=Client(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await engine.run(task, asyncio.Event(), queue)

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "done"


@pytest.mark.asyncio
async def test_oversized_plugin_failure_is_bounded_in_context_and_events(tmp_path: Path) -> None:
    class HugeFailureTool(ToolPlugin):
        name = "huge_failure"
        description = "Return a large deterministic failure"
        parameters = {"type": "object", "properties": {}}

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            return {
                "ok": False,
                "error": "stable failure marker " + ("x" * 500_000),
                "diagnostic_id": "failure-123",
            }

    class Client:
        keys: list[str] = []

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "failure-call",
                                "type": "function",
                                "function": {
                                    "name": "huge_failure",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            tool_content = str(messages[-1]["content"])
            assert messages[-1]["role"] == "tool"
            assert len(tool_content) <= 16_000
            assert "stable failure marker" in tool_content
            assert '"truncated": true' in tool_content
            return DeepSeekReply(
                message={"role": "assistant", "content": "recovered", "tool_calls": []},
                usage={},
                active_key="test",
            )

    registry = PluginRegistry()
    registry.register(HugeFailureTool())
    task = AgentTask(prompt="Use huge_failure and recover")
    engine = AgentEngine(
        client=Client(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await engine.run(task, asyncio.Event())

    tool_event = next(event for event in task.events if event.type == "tool_result")
    persisted = json.dumps(tool_event.data["result"], ensure_ascii=False)
    assert len(persisted) <= 32_000
    assert tool_event.data["result"]["truncated"] is True
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "recovered"


@pytest.mark.asyncio
async def test_unknown_tool_failure_is_durable_and_visible_to_next_turn(
    tmp_path: Path,
) -> None:
    class Client:
        keys: list[str] = []

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "missing-1",
                                "type": "function",
                                "function": {
                                    "name": "not_registered_here",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            assert messages[-1]["role"] == "tool"
            assert "Unknown tool: not_registered_here" in messages[-1]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "reported", "tool_calls": []},
                usage={},
                active_key="test",
            )

    task = AgentTask(prompt="Use a capability that may not exist")

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=Client(),
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "unknown-tool-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=emit,
    )
    await engine.run(task, asyncio.Event())

    failed = [
        event
        for event in task.events
        if event.type == "tool_result"
        and event.data.get("tool") == "not_registered_here"
    ]
    assert task.status == TaskStatus.COMPLETED
    assert failed and failed[0].data["result"]["ok"] is False
    assert "Unknown tool" in failed[0].data["result"]["error"]


def test_runtime_prompt_keeps_core_but_excludes_unrelated_domain_noise() -> None:
    generic = runtime_system_prompt("Summarize this stable paragraph", AgentProfile.GENERAL)
    assert "PROMPT-INJECTION DEFENSE" in generic
    assert "Every registered tool remains available" in generic
    assert "FINANCE MODULE" not in generic
    assert "ANDROID MODULE" not in generic
    assert "BENCHMARK INTEGRITY MODULE" not in generic

    coding = runtime_system_prompt("Fix this Python browser UI bug", AgentProfile.CODER)
    assert "CODING MODULE" in coding
    assert "UI, BROWSER, AND SPATIAL MODULE" in coding
    assert "must not embed the current installation directory" in coding
    assert "workspace-relative paths" in coding
    assert "continue in the same turn without waiting for approval" in coding
    assert "smallest possible scope" in coding
    assert "FINANCE MODULE" not in coding


def test_tool_envelope_propagates_nested_and_process_failures() -> None:
    assert normalize_tool_envelope({"ok": False, "error": "offline"})["ok"] is False
    failed = normalize_tool_envelope({"exit_code": 7, "stderr": "bad"})
    assert failed["ok"] is False
    assert "7" in failed["error"]
    assert normalize_tool_envelope({"exit_code": 0, "stdout": "done"})["ok"] is True
    assert normalize_tool_envelope({"error": "transport failed"})["ok"] is False
    nested = normalize_tool_envelope(
        {"result": {"ok": False, "error": "nested transport failed"}}
    )
    assert nested["ok"] is False
    assert "nested transport failed" in nested["error"]
    assert normalize_tool_envelope({"status_code": 503})["ok"] is False
    assert normalize_tool_envelope({"status_code": 200, "error": ""})["ok"] is True
    assert normalize_tool_envelope({"status": "failed", "message": "worker died"}) == {
        "ok": False,
        "error": "worker died",
        "result": {"status": "failed", "message": "worker died"},
    }
    assert normalize_tool_envelope(
        {"result": {"status": "timed_out", "message": "deadline"}}
    )["ok"] is False


def test_large_tool_output_remains_valid_json_after_compaction() -> None:
    prefix = "UNTRUSTED\n"
    content = serialize_tool_output(
        {"ok": True, "result": {"rows": ["x" * 500 for _ in range(200)]}},
        limit=2_000,
        prefix=prefix,
    )

    assert len(content) <= 2_000 + len(prefix)
    payload = json.loads(content.removeprefix(prefix))
    assert payload["truncated"] is True
    assert payload["original_characters"] > 2_000
    assert payload["diagnostics"]["ok"] is True

    tiny = serialize_tool_output(
        {"ok": True, "result": "x" * 5_000},
        limit=256,
        prefix="",
    )
    assert len(tiny) <= 256
    assert json.loads(tiny)["truncated"] is True


def test_large_failed_tool_output_preserves_exact_diagnostics() -> None:
    content = serialize_tool_output(
        {
            "ok": False,
            "result": {
                "rows": ["noise" * 2_000 for _ in range(40)],
                "stderr": "compiler: exact failure on line 73",
                "exit_code": 9,
            },
        },
        limit=2_400,
        prefix="",
    )
    payload = json.loads(content)
    assert payload["diagnostics"]["ok"] is False
    assert payload["diagnostics"]["result.stderr"] == "compiler: exact failure on line 73"
    assert payload["diagnostics"]["result.exit_code"] == 9


def test_powershell_wrapper_turns_nonterminating_errors_into_process_failures() -> None:
    wrapped = strict_powershell_command("Get-Item does-not-exist; Write-Output done")
    assert "$ErrorActionPreference='Stop'" in wrapped
    assert "catch { Write-Error $_; exit 1 }" in wrapped
    assert "if ($__elrenNativeExit -ne 0)" in wrapped
    assert "NativeCommandError" in wrapped


def test_windows_process_output_decoder_preserves_utf8_utf16_and_cjk_codepages() -> None:
    text = "测试完成 · café"
    assert decode_windows_process_output(text.encode("utf-8")) == text
    assert decode_windows_process_output(text.encode("utf-16")) == text
    assert decode_windows_process_output("中文输出成功".encode("gb18030")) == "中文输出成功"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell regression")
def test_powershell_wrapper_does_not_turn_successful_native_stderr_into_failure() -> None:
    warning = strict_powershell_command(
        'cmd /c "echo native-warning 1>&2 & exit /b 0" 2>&1; Write-Output done'
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", warning],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0
    output = decode_windows_process_output(completed.stdout + completed.stderr)
    assert "native-warning" in output
    assert "done" in output

    missing = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            strict_powershell_command("Get-Item elren-file-that-does-not-exist"),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert missing.returncode == 1


def test_filesystem_focused_read_and_atomic_exact_edit(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"one\ntwo\nthree\nfour\n")
    tool = FileSystemTool()
    context = ToolContext(task_id="harness-edit", workspace=str(tmp_path))

    focused = tool._execute_sync(
        {"action": "read", "path": "sample.py", "start_line": 2, "end_line": 3},
        context,
    )
    assert focused["content"] == "two\nthree\n"
    assert focused["total_lines"] == 4
    assert focused["truncated"] is True

    edited = tool._execute_sync(
        {
            "action": "edit",
            "path": "sample.py",
            "edits": [{"old_text": "two\nthree", "new_text": "TWO\nTHREE"}],
        },
        context,
    )
    assert edited["changed"] is True
    assert target.read_bytes() == b"one\nTWO\nTHREE\nfour\n"


def test_filesystem_map_orients_without_reading_dependencies_or_secrets(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "node_modules" / "huge").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (tmp_path / "node_modules" / "huge" / "index.js").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    tool = FileSystemTool()
    context = ToolContext(task_id="harness-map", workspace=str(tmp_path))

    mapped = tool._execute_sync(
        {"action": "map", "path": ".", "max_depth": 3, "max_results": 50},
        context,
    )

    file_paths = {item["path"] for item in mapped["files"]}
    assert "pyproject.toml" in mapped["entrypoints"]
    assert "src/main.py" in mapped["entrypoints"]
    assert "tests/test_main.py" in mapped["test_paths"]
    assert "node_modules/huge/index.js" not in file_paths
    assert ".env" not in file_paths
    assert mapped["bounded"] is True
    assert any(item["extension"] == ".py" and item["files"] == 2 for item in mapped["languages_by_extension"])


def test_filesystem_edit_is_all_or_nothing_on_stale_context(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"alpha\nbeta\n")
    tool = FileSystemTool()
    context = ToolContext(task_id="harness-stale-edit", workspace=str(tmp_path))

    try:
        tool._execute_sync(
            {
                "action": "edit",
                "path": "sample.py",
                "edits": [
                    {"old_text": "alpha", "new_text": "ALPHA"},
                    {"old_text": "missing", "new_text": "value"},
                ],
            },
            context,
        )
    except ValueError as exc:
        assert "file unchanged" in str(exc)
    else:  # pragma: no cover - explicit assertion for a failed stale edit
        raise AssertionError("stale edit unexpectedly succeeded")
    assert target.read_bytes() == b"alpha\nbeta\n"


def test_filesystem_large_read_requires_and_streams_a_focused_range(tmp_path: Path) -> None:
    target = tmp_path / "large.log"
    line = "x" * 1_000 + "\n"
    target.write_text(line * 5_100, encoding="utf-8")
    tool = FileSystemTool()
    context = ToolContext(task_id="harness-large-read", workspace=str(tmp_path))

    try:
        tool._execute_sync({"action": "read", "path": "large.log"}, context)
    except ValueError as exc:
        assert "require start_line and end_line" in str(exc)
    else:  # pragma: no cover - explicit memory-safety assertion
        raise AssertionError("large file was unexpectedly loaded without a focused range")

    focused = tool._execute_sync(
        {"action": "read", "path": "large.log", "start_line": 5_000, "end_line": 5_002},
        context,
    )
    assert focused["streamed"] is True
    assert focused["content"].replace("\r\n", "\n") == line * 3
    assert focused["more_lines"] is True


def test_generic_words_do_not_contaminate_cross_conversation_context(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    store.save(
        AgentTask(
            prompt="修复这个软件的问题并完成任务",
            result="已经完成这个功能",
            status=TaskStatus.COMPLETED,
            source="web",
        )
    )

    context, task_ids = store.build_cross_conversation_context(
        "修复这个软件的问题", source="web"
    )

    assert context == ""
    assert task_ids == []
