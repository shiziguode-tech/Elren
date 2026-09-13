import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.models import (
    AgentProfile,
    AgentTask,
    ApprovalPolicy,
    CreateTaskRequest,
    DiscussionTeamMember,
    HumanActionRequest,
    Risk,
    RunningTaskMessageRequest,
    TaskEvent,
    TaskStatus,
)
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.builtin.computer import ComputerTool
from deepdesk.plugins.builtin.computer_use import ComputerUseAgentTool
from deepdesk.plugins.builtin.document import DocumentTool
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.builtin.human_action import HumanActionTool
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool
from deepdesk.plugins.builtin.sandbox import SandboxTool
from deepdesk.plugins.builtin.shell import ShellTool
from deepdesk.plugins.builtin.windows_ui import (
    WM_GETTEXT,
    WM_GETTEXTLENGTH,
    WM_SETTEXT,
    WindowsUITool,
    _native_text_control,
    _set_native_text,
)
from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.task_store import TaskStore
from deepdesk.windows_text import type_unicode, utf16_code_units


def test_system_prompt_requires_real_visual_spatial_and_non_looping_code_verification():
    from deepdesk.engine import SYSTEM_PROMPT

    assert "AUTONOMOUS EXECUTION" in SYSTEM_PROMPT
    assert "VISUAL DELIVERY QUALITY" in SYSTEM_PROMPT
    assert "SPATIAL REASONING" in SYSTEM_PROMPT
    assert "EVIDENCE-BASED SELF-CHECK" in SYSTEM_PROMPT
    assert "CODING EXECUTION PROTOCOL" in SYSTEM_PROMPT
    assert "NON-BLOCKING POST-CHANGE VERIFICATION" in SYSTEM_PROMPT
    assert "must never start an automatic repair/retry loop" in SYSTEM_PROMPT
    assert "mostly blank, black, washed out" in SYSTEM_PROMPT
    assert "console/runtime logs" in SYSTEM_PROMPT
    assert "narrow reproducer" in SYSTEM_PROMPT
    assert "不得把 `gas` 擅自同义改写为 `gasoline`" in SYSTEM_PROMPT


def test_approval_matrix():
    assert ApprovalGate.required(ApprovalPolicy.CAUTIOUS, Risk.MEDIUM)
    assert not ApprovalGate.required(ApprovalPolicy.CAUTIOUS, Risk.SAFE)
    assert ApprovalGate.required(ApprovalPolicy.BALANCED, Risk.HIGH)
    assert not ApprovalGate.required(ApprovalPolicy.BALANCED, Risk.MEDIUM)
    assert not ApprovalGate.required(ApprovalPolicy.AUTONOMOUS, Risk.HIGH)


def test_new_tasks_default_to_autonomous_operation():
    assert AgentTask(prompt="close the page").policy == ApprovalPolicy.AUTONOMOUS


def test_retired_private_chat_fields_are_ignored_at_every_input_boundary():
    legacy = {"private_chat": True, "privacy_session": "stale-page-capability"}

    task = AgentTask.model_validate({"prompt": "legacy task", **legacy})
    create = CreateTaskRequest.model_validate({"prompt": "legacy request", **legacy})
    message = RunningTaskMessageRequest.model_validate(
        {"prompt": "legacy follow-up", "privacy_session": legacy["privacy_session"]}
    )

    assert "private_chat" not in task.model_dump()
    assert "privacy_session" not in task.model_dump()
    assert "private_chat" not in create.model_dump()
    assert "privacy_session" not in create.model_dump()
    assert "privacy_session" not in message.model_dump()


def test_model_stream_progress_coalesces_without_crowding_task_history():
    manager = TaskManager(object(), ApprovalGate())
    task = AgentTask(prompt="long reasoning")
    manager.emit(task, "thinking", {"step": 1})
    manager.emit(task, "model_stream", {"step": 1, "elapsed_seconds": 10})
    first_id = task.events[-1].id
    manager.emit(task, "model_stream", {"step": 1, "elapsed_seconds": 20})

    assert [event.type for event in task.events] == ["thinking", "model_stream"]
    assert task.events[-1].data["elapsed_seconds"] == 20
    assert task.events[-1].id != first_id

    manager.emit(task, "thinking", {"step": 2})
    manager.emit(task, "model_stream", {"step": 2, "elapsed_seconds": 10})
    assert [event.type for event in task.events].count("model_stream") == 2


@pytest.mark.asyncio
async def test_model_router_manual_choice_overrides_difficulty_and_auto_opt_out(tmp_path: Path):
    class RouterClient:
        keys = []

        @staticmethod
        def has_model_credentials(selector):
            return selector != "aicodemirror-openai:gpt-5.6-luna"

        async def classify_task_difficulty(self, prompt):
            return {"use_pro": True, "reason": "complex"}

    events = []
    engine = AgentEngine(
        client=RouterClient(),
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda task, event_type, data: events.append((event_type, data)),
    )

    manual_flash = AgentTask(
        prompt="设计并实现一个复杂软件",
        model_preference="deepseek-v4-flash",
    )
    await engine._select_task_model(manual_flash)
    assert manual_flash.active_model == "deepseek-v4-flash"
    assert events[-1][1]["mode"] == "manual"

    retired = AgentTask(
        prompt="继续旧任务",
        model_preference="aicodemirror-openai:gpt-5.6-luna",
    )
    await engine._select_task_model(retired)
    assert retired.model_preference == "auto"
    assert retired.active_model != "aicodemirror-openai:gpt-5.6-luna"
    assert "已不可用" in events[-1][1]["reason"]

    opted_out = AgentTask(prompt="全面调试这个系统，但不要使用 V4 Pro")
    await engine._select_task_model(opted_out)
    assert opted_out.active_model == "deepseek-v4-flash"
    assert events[-1][1]["mode"] == "user_opt_out"

    engine.client.default_model_preference = "openai:gpt-default"
    defaulted = AgentTask(prompt="设计并实现一个复杂软件")
    await engine._select_task_model(defaulted)
    assert defaulted.active_model == "openai:gpt-default"
    assert events[-1][1]["mode"] == "default"

    engine.client.default_model_preference = "auto"
    automatic = AgentTask(prompt="设计并实现一个复杂软件")
    await engine._select_task_model(automatic)
    assert automatic.active_model == "deepseek-v4-pro"
    assert events[-1][1]["mode"] == "automatic"


@pytest.mark.asyncio
async def test_automatic_sendbar_skips_cooling_concrete_default(tmp_path: Path):
    class CoolingDefaultClient:
        keys = []
        default_model_preference = "openai:gpt-dead"

        @staticmethod
        def has_model_credentials(_selector):
            return True

        @staticmethod
        def model_temporarily_unavailable(selector):
            return selector == "openai:gpt-dead"

        @staticmethod
        def select_automatic_model(_difficult, _prompt=""):
            return "google:gemini-healthy"

    events = []
    engine = AgentEngine(
        client=CoolingDefaultClient(),
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda task, event_type, data: events.append((event_type, data)),
    )
    task = AgentTask(prompt="Handle this task", model_preference="auto")

    await engine._select_task_model(task)

    assert task.active_model == "google:gemini-healthy"
    assert events[-1][1]["mode"] == "default_failover"
    assert "暂不可用" in events[-1][1]["reason"]


@pytest.mark.asyncio
async def test_engine_binding_failure_cancels_probe_and_resets_partial_scope(
    monkeypatch, tmp_path: Path
):
    probe_cancelled = asyncio.Event()

    async def blocking_probe(_detector):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise

    monkeypatch.setattr(
        "deepdesk.engine.EgressRegionDetector.detect", blocking_probe
    )

    class BindingClient:
        keys = []

        def __init__(self):
            self.reset: list[tuple[str, str]] = []

        def bind_task_model(self, _model):
            return "model-token"

        def reset_task_model(self, token):
            self.reset.append(("model", token))

        def bind_task_reasoning_effort(self, _effort):
            raise RuntimeError("reasoning binding failed")

    client = BindingClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "binding-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda *_args: None,
    )

    with pytest.raises(RuntimeError, match="reasoning binding failed"):
        await engine.run(
            AgentTask(prompt="test", model_preference="deepseek-v4-flash"),
            asyncio.Event(),
        )

    # The bind fails synchronously, so the newly-created probe may be cancelled
    # before its coroutine gets a first timeslice.  Either way, no probe task
    # may survive setup failure.
    live_probes = [
        pending
        for pending in asyncio.all_tasks()
        if pending is not asyncio.current_task()
        and getattr(pending.get_coro(), "__name__", "") == "blocking_probe"
        and not pending.done()
    ]
    assert live_probes == []
    assert client.reset == [("model", "model-token")]


@pytest.mark.asyncio
async def test_engine_preloop_failure_releases_every_task_scope(
    monkeypatch, tmp_path: Path
):
    probe_entered = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def blocking_probe(_detector):
        probe_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise

    monkeypatch.setattr(
        "deepdesk.engine.EgressRegionDetector.detect", blocking_probe
    )

    class BindingClient:
        keys = []

        def __init__(self):
            self.reset: list[tuple[str, object]] = []

        def bind_task_model(self, _value):
            return "model-token"

        def bind_task_reasoning_effort(self, _value):
            return "reasoning-token"

        def bind_task_model_failover(self, _callback):
            return "failover-token"

        def bind_task_stream_progress(self, _callback):
            return "stream-token"

        def reset_task_model(self, token):
            self.reset.append(("model", token))

        def reset_task_reasoning_effort(self, token):
            self.reset.append(("reasoning", token))

        def reset_task_model_failover(self, token):
            self.reset.append(("failover", token))

        def reset_task_stream_progress(self, token):
            self.reset.append(("stream", token))

    client = BindingClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "preloop-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda *_args: None,
    )

    async def fail_discussion(_task, _cancel):
        # Exercise cancellation after entry deterministically. If setup fails
        # before the scheduled probe starts, its coroutine never reaches the
        # cancellation handler, even though the engine correctly joins it.
        async with asyncio.timeout(2):
            await probe_entered.wait()
        raise RuntimeError("discussion setup failed")

    engine._run_team_discussion = fail_discussion

    with pytest.raises(RuntimeError, match="discussion setup failed"):
        await engine.run(
            AgentTask(prompt="test", model_preference="deepseek-v4-flash"),
            asyncio.Event(),
        )

    assert probe_cancelled.is_set()
    assert client.reset == [
        ("model", "model-token"),
        ("reasoning", "reasoning-token"),
        ("failover", "failover-token"),
        ("stream", "stream-token"),
    ]


def test_failed_repository_verifier_blocks_false_completion():
    arguments = {"command": "python work/evals/verify_solution.py"}
    failed = {"ok": True, "result": {"exit_code": 1, "stdout": "AssertionError"}}
    passed = {"ok": True, "result": {"exit_code": 0, "stdout": "passed"}}

    assert AgentEngine._verification_result("sandbox", arguments, failed) is False
    assert AgentEngine._verification_result("sandbox", arguments, passed) is True
    assert AgentEngine._verification_result(
        "sandbox", {"command": "python app.py"}, failed
    ) is None
    assert AgentEngine._requires_passing_verification(
        AgentTask(prompt="实现并调试这个软件")
    )
    assert not AgentEngine._requires_passing_verification(
        AgentTask(
            prompt=(
                "Complete the class in the supplied skeleton. "
                "Return the complete Python implementation only."
            )
        )
    )


def test_shell_risk_classification():
    tool = ShellTool()
    assert tool.risk({"command": "Get-ChildItem"}) == Risk.SAFE
    assert tool.risk({"command": "npm test"}) == Risk.MEDIUM
    assert tool.risk({"command": "Remove-Item -Recurse thing"}) == Risk.HIGH


@pytest.mark.asyncio
async def test_shell_uses_injected_platform_sandbox(tmp_path: Path):
    class FakeSandbox:
        def status(self):
            return {"available": True}

        async def run(self, command, **kwargs):
            return {"exit_code": 0, "command": command, "limits": kwargs}

    result = await ShellTool(FakeSandbox()).execute(
        {"command": "echo portable", "timeout_seconds": 9},
        ToolContext(task_id="portable-shell", workspace=str(tmp_path)),
    )

    assert result["command"] == "echo portable"
    assert result["limits"]["timeout_seconds"] == 9
    assert result["process_tree_terminated_on_timeout"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "Get-Process python | Stop-Process -Force",
        "taskkill.exe /IM python.exe /F",
        "pkill -f deepdesk.main",
    ],
)
async def test_shell_hard_blocks_process_termination_that_can_kill_elren(
    tmp_path: Path, command: str
):
    class NeverRunSandbox:
        called = False

        def status(self):
            return {"available": True}

        async def run(self, command, **kwargs):
            self.called = True
            return {"exit_code": 0}

    sandbox = NeverRunSandbox()
    with pytest.raises(PermissionError, match="process termination"):
        await ShellTool(sandbox).execute(
            {"command": command},
            ToolContext(task_id="host-protection", workspace=str(tmp_path)),
        )
    assert sandbox.called is False


@pytest.mark.asyncio
async def test_shell_reserves_elren_port_but_allows_a_separate_preview_port(
    tmp_path: Path, monkeypatch
):
    class RecordingSandbox:
        commands = []

        def status(self):
            return {"available": True}

        async def run(self, command, **kwargs):
            self.commands.append(command)
            return {"exit_code": 0}

    monkeypatch.setenv("ELREN_PORT", "8765")
    sandbox = RecordingSandbox()
    tool = ShellTool(sandbox)
    context = ToolContext(task_id="port-protection", workspace=str(tmp_path))
    with pytest.raises(PermissionError, match="reserved by Elren"):
        await tool.execute({"command": "python -m http.server 8765"}, context)

    result = await tool.execute({"command": "python -m http.server 9876"}, context)
    assert result["exit_code"] == 0
    assert sandbox.commands == ["python -m http.server 9876"]


@pytest.mark.asyncio
async def test_shell_injects_only_credentials_for_explicit_skill(tmp_path: Path):
    class FakeSandbox:
        def status(self):
            return {"available": True}

        async def run(self, command, **kwargs):
            return {"exit_code": 0, "environment": kwargs.get("environment", {})}

    credentials = {
        "github": {"GH_TOKEN": "github-secret"},
        "trello": {"TRELLO_API_KEY": "trello-key", "TRELLO_TOKEN": "trello-token"},
    }
    tool = ShellTool(FakeSandbox(), lambda skill: credentials.get(skill, {}))
    result = await tool.execute(
        {"command": "Write-Output $env:GH_TOKEN", "skill": "github"},
        ToolContext(task_id="skill-env", workspace=str(tmp_path)),
    )

    assert result["environment"] == {"GH_TOKEN": "github-secret"}
    assert "TRELLO_TOKEN" not in result["environment"]

    with pytest.raises(PermissionError):
        await tool.execute(
            {"command": "Write-Output $env:TRELLO_TOKEN", "skill": "github"},
            ToolContext(task_id="wrong-skill-env", workspace=str(tmp_path)),
        )


def test_shell_fallback_environment_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "safe-runtime-path")

    environment = credential_safe_environment()

    assert "OPENAI_API_KEY" not in environment
    assert environment["PATH"].split(os.pathsep)[-1] == "safe-runtime-path"
    if len(environment["PATH"].split(os.pathsep)) > 1:
        assert environment["PATH"].split(os.pathsep)[0].endswith("work\\tool-runtime\\bin")


@pytest.mark.asyncio
async def test_filesystem_cannot_escape_workspace(tmp_path: Path):
    tool = FileSystemTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))
    with pytest.raises(PermissionError):
        await tool.execute({"action": "read", "path": "../secret.txt"}, context)


@pytest.mark.asyncio
async def test_filesystem_roundtrip(tmp_path: Path):
    tool = FileSystemTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))
    await tool.execute({"action": "write", "path": "notes/a.txt", "content": "hello"}, context)
    result = await tool.execute({"action": "read", "path": "notes/a.txt"}, context)
    assert result["content"] == "hello"


@pytest.mark.asyncio
async def test_filesystem_delete_defaults_to_windows_recycle_bin(tmp_path: Path, monkeypatch):
    target = tmp_path / "recoverable.txt"
    target.write_text("keep recoverable", encoding="utf-8")
    recycled = []

    monkeypatch.setattr(
        FileSystemTool,
        "_send_to_recycle_bin",
        staticmethod(lambda path: recycled.append(path)),
    )
    tool = FileSystemTool()
    context = ToolContext(
        task_id="recycle-delete",
        workspace=str(tmp_path),
        user_prompt="删除 recoverable.txt",
    )

    result = await tool.execute({"action": "delete", "path": "recoverable.txt"}, context)

    assert recycled == [target.resolve()]
    assert result["destination"] == "windows_recycle_bin"
    assert result["recoverable"] is True
    assert tool.risk({"action": "delete", "path": "recoverable.txt"}) == Risk.MEDIUM


@pytest.mark.asyncio
async def test_filesystem_permanent_delete_requires_current_explicit_request(tmp_path: Path):
    target = tmp_path / "permanent.txt"
    target.write_text("delete only when explicit", encoding="utf-8")
    tool = FileSystemTool()

    with pytest.raises(PermissionError, match="explicit request"):
        await tool.execute(
            {"action": "delete", "path": "permanent.txt", "permanent": True},
            ToolContext(
                task_id="ordinary-delete",
                workspace=str(tmp_path),
                user_prompt="删除 permanent.txt",
            ),
        )
    assert target.exists()

    result = await tool.execute(
        {"action": "delete", "path": "permanent.txt", "permanent": True},
        ToolContext(
            task_id="permanent-delete",
            workspace=str(tmp_path),
            user_prompt="彻底删除 permanent.txt，不要放入回收站",
        ),
    )
    assert not target.exists()
    assert result["destination"] == "permanent"
    assert result["recoverable"] is False
    assert tool.risk({"action": "delete", "path": "permanent.txt", "permanent": True}) == Risk.HIGH


@pytest.mark.asyncio
async def test_shell_blocks_delete_unless_current_request_is_explicitly_permanent(tmp_path: Path):
    target = tmp_path / "shell-delete.txt"
    target.write_text("delete guard", encoding="utf-8")
    tool = ShellTool()
    command = "Remove-Item -LiteralPath 'shell-delete.txt' -Force"

    with pytest.raises(PermissionError, match="Raw shell deletion is blocked"):
        await tool.execute(
            {"command": command},
            ToolContext(
                task_id="shell-recycle",
                workspace=str(tmp_path),
                user_prompt="删除 shell-delete.txt",
            ),
        )
    assert target.exists()

    result = await tool.execute(
        {"command": command},
        ToolContext(
            task_id="shell-permanent",
            workspace=str(tmp_path),
            user_prompt="永久删除 shell-delete.txt",
        ),
    )
    assert result["exit_code"] == 0
    assert not target.exists()


@pytest.mark.asyncio
async def test_sandbox_uses_the_same_permanent_delete_guard(tmp_path: Path):
    class FakeSandbox:
        def status(self):
            return {"available": True}

        async def run(self, command, **_kwargs):
            return {"exit_code": 0, "command": command}

    tool = SandboxTool(FakeSandbox())
    arguments = {"action": "run", "command": "Remove-Item -Recurse -Force cache"}
    with pytest.raises(PermissionError, match="Raw sandbox deletion is blocked"):
        await tool.execute(
            arguments,
            ToolContext(
                task_id="sandbox-recycle",
                workspace=str(tmp_path),
                user_prompt="删除 cache",
            ),
        )

    result = await tool.execute(
        arguments,
        ToolContext(
            task_id="sandbox-permanent",
            workspace=str(tmp_path),
            user_prompt="彻底删除 cache",
        ),
    )
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_sandbox_cannot_terminate_host_or_bind_reserved_port(tmp_path: Path):
    class NeverRunSandbox:
        called = False

        def status(self):
            return {"available": True}

        async def run(self, command, **_kwargs):
            self.called = True
            return {"exit_code": 0, "command": command}

    sandbox = NeverRunSandbox()
    tool = SandboxTool(sandbox)
    context = ToolContext(task_id="sandbox-host-guard", workspace=str(tmp_path))
    with pytest.raises(PermissionError, match="process termination"):
        await tool.execute(
            {"action": "run", "command": "Get-Process python | Stop-Process -Force"},
            context,
        )
    with pytest.raises(PermissionError, match="reserved by Elren"):
        await tool.execute(
            {"action": "run", "command": "python -m http.server 8765"},
            context,
        )
    assert sandbox.called is False


@pytest.mark.asyncio
async def test_filesystem_blocks_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / "data/openclaw").mkdir(parents=True)
    for relative in (
        "data/provider-keys.json",
        "data/provider-secrets.vault",
        "data/openclaw/gateway.token",
        "data/openclaw/openclaw.json.last-good",
        "data/deepdesk.db",
        "data/audit.jsonl",
    ):
        (tmp_path / relative).write_text("private", encoding="utf-8")
    tool = FileSystemTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))
    for relative in (
        ".env",
        "data/provider-keys.json",
        "data/provider-secrets.vault",
        "data/openclaw/gateway.token",
        "data/openclaw/openclaw.json.last-good",
        "data/deepdesk.db",
        "data/audit.jsonl",
    ):
        with pytest.raises(PermissionError):
            await tool.execute({"action": "read", "path": relative}, context)


@pytest.mark.asyncio
async def test_filesystem_search_avoids_shell_and_secret_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text(
        "alpha\nNeedle value\nomega\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("Needle=secret", encoding="utf-8")
    tool = FileSystemTool()
    context = ToolContext(task_id="search", workspace=str(tmp_path))

    result = await tool.execute(
        {
            "action": "search",
            "path": ".",
            "query": "needle",
            "glob": "*.py",
            "max_results": 10,
        },
        context,
    )

    assert result["match_count"] == 1
    assert result["matches"][0]["line"] == 2
    assert result["matches"][0]["text"] == "Needle value"


@pytest.mark.asyncio
async def test_shell_blocks_environment_access(tmp_path: Path):
    tool = ShellTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))
    assert tool.risk({"command": "Get-ChildItem env:"}) == Risk.HIGH
    with pytest.raises(PermissionError):
        await tool.execute({"command": "Get-ChildItem env:"}, context)

    for private_path in (
        "data/provider-keys.json",
        "data/provider-secrets.vault",
        "data/openclaw/gateway.token",
        "data/openclaw/openclaw.json.last-good",
        "data/deepdesk.db",
        "data/audit.jsonl",
    ):
        with pytest.raises(PermissionError):
            await tool.execute({"command": f"Get-Content {private_path}"}, context)


@pytest.mark.asyncio
async def test_shell_allows_safe_temporary_directory_reference(tmp_path: Path):
    tool = ShellTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))

    assert tool.risk({"command": "Write-Output $env:TEMP"}) != Risk.HIGH
    result = await tool.execute(
        {"command": "Write-Output $env:TEMP", "timeout_seconds": 10}, context
    )

    assert result["exit_code"] == 0
    assert result["stdout"].strip()


def test_unambiguous_document_singletons_are_repaired_before_validation():
    arguments = {
        "action": "create",
        "path": "outputs/report.docx",
        "paragraphs": "one paragraph",
    }

    repairs = AgentEngine._repair_tool_arguments(DocumentTool.parameters, arguments)

    assert arguments["paragraphs"] == ["one paragraph"]
    assert repairs == ["$.paragraphs: wrapped singleton as array"]
    assert AgentEngine._validate_tool_arguments(DocumentTool.parameters, arguments) == []


@pytest.mark.asyncio
async def test_running_task_accepts_a_follow_up_without_cancellation():
    class SteeringEngine:
        human_actions = HumanActionGate()

        async def run(self, task, cancel, steering_queue):
            task.status = TaskStatus.RUNNING
            item = await asyncio.wait_for(steering_queue.get(), timeout=2)
            task.result = item["prompt"]
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(SteeringEngine(), ApprovalGate())
    task = manager.create("start", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    for _ in range(100):
        if task.status == TaskStatus.RUNNING:
            break
        await asyncio.sleep(0.01)

    running = manager.background[task.id]
    steered = manager.steer(task.id, "add a pause menu", ["inputs/mockup.png"])
    await running

    assert steered is task
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "add a pause menu"
    assert "inputs/mockup.png" in task.attachments
    assert any(event.type == "user_message_queued" for event in task.events)


@pytest.mark.asyncio
async def test_running_follow_up_never_bypasses_durable_attachment_limit():
    received: dict = {}

    class SteeringEngine:
        human_actions = HumanActionGate()

        async def run(self, task, cancel, steering_queue):
            task.status = TaskStatus.RUNNING
            received.update(await asyncio.wait_for(steering_queue.get(), timeout=2))
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(SteeringEngine(), ApprovalGate())
    initial = [f"inputs/original-{index}.txt" for index in range(19)]
    task = manager.create(
        "start",
        ApprovalPolicy.AUTONOMOUS,
        AgentProfile.GENERAL,
        attachments=initial,
    )
    await asyncio.sleep(0)
    manager.steer(
        task.id,
        "inspect the new inputs",
        ["inputs/new-a.txt", "inputs/new-b.txt", "inputs/new-a.txt", ""],
    )
    await manager.background[task.id]

    assert len(task.attachments) == 20
    assert received["attachments"] == ["inputs/new-a.txt", "inputs/new-b.txt"]
    queued = next(event for event in task.events if event.type == "user_message_queued")
    assert queued.data["dropped_attachment_count"] == 0
    assert queued.data["omitted_previous_attachments"] == [initial[-1]]


@pytest.mark.asyncio
async def test_plugin_cleanup_is_parallel_bounded_and_failure_isolated(tmp_path: Path):
    cleaned = asyncio.Event()

    class CleanupTool(ToolPlugin):
        description = "cleanup test"
        parameters = {"type": "object", "properties": {}}

        def __init__(self, name, cleanup):
            self.name = name
            self._cleanup = cleanup

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            return {"ok": True}

        async def cleanup(self, context):
            await self._cleanup()

    async def hang():
        await asyncio.Event().wait()

    async def fail():
        raise RuntimeError("cleanup failed")

    async def succeed():
        cleaned.set()

    registry = PluginRegistry()
    registry.register(CleanupTool("hang_cleanup", hang))
    registry.register(CleanupTool("fail_cleanup", fail))
    registry.register(CleanupTool("good_cleanup", succeed))
    context = ToolContext(task_id="cleanup-test", workspace=str(tmp_path))

    await asyncio.wait_for(registry.cleanup(context, timeout=0.05), timeout=0.25)

    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_task_manager_runs_multiple_projects_in_parallel_without_a_global_limit():
    release = asyncio.Event()
    started: set[str] = set()

    class ParallelEngine:
        human_actions = HumanActionGate()

        async def run(self, task, cancel, steering_queue):
            task.status = TaskStatus.RUNNING
            started.add(task.id)
            await release.wait()
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(ParallelEngine(), ApprovalGate())
    tasks = [
        manager.create(f"project {index}", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
        for index in range(8)
    ]
    for _ in range(100):
        if len(started) == len(tasks):
            break
        await asyncio.sleep(0.01)

    assert len(started) == 8
    assert all(task.status == TaskStatus.RUNNING for task in tasks)
    assert all(not manager.background[task.id].done() for task in tasks)

    release.set()
    await asyncio.gather(*(manager.background[task.id] for task in tasks))
    assert all(task.status == TaskStatus.COMPLETED for task in tasks)


@pytest.mark.asyncio
async def test_shell_timeout_terminates_the_windows_process_tree(tmp_path: Path):
    tool = ShellTool()
    context = ToolContext(task_id="timeout", workspace=str(tmp_path))

    result = await tool.execute(
        {
            "command": (
                "$child = Start-Process powershell.exe -ArgumentList "
                "'-NoProfile','-Command','Start-Sleep -Seconds 30' -PassThru; "
                "Start-Sleep -Seconds 30"
            ),
            "timeout_seconds": 1,
        },
        context,
    )

    assert result["timed_out"] is True
    assert result["process_tree_terminated_on_timeout"] is True


def test_unicode_input_uses_ime_independent_path(tmp_path: Path, monkeypatch):
    captured = {}
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace())
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.computer.type_unicode",
        lambda text, interval=0: captured.update(text=text, interval=interval),
    )

    result = ComputerTool(tmp_path)._execute_sync(
        {
            "action": "type",
            "text": "grok.com 中文",
            "allow_user_input_interference": True,
        }
    )

    assert captured["text"] == "grok.com 中文"
    assert result["input_mode"] == "unicode_ime_independent"
    assert utf16_code_units("A中😀") == [65, 20013, 55357, 56832]


def test_foreground_input_is_blocked_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace())

    with pytest.raises(PermissionError, match="stealing the user's focus"):
        ComputerTool(tmp_path)._execute_sync({"action": "type", "text": "do not type"})


def test_shift_delete_requires_current_explicit_permanent_request(tmp_path: Path, monkeypatch):
    hotkeys = []
    monkeypatch.setattr("deepdesk.windows_text.send_key_chord", lambda keys: hotkeys.append(tuple(keys)))
    monkeypatch.setitem(
        sys.modules,
        "pyautogui",
        SimpleNamespace(hotkey=lambda *keys: hotkeys.append(keys)),
    )
    arguments = {
        "action": "hotkey",
        "keys": ["shift", "delete"],
        "allow_user_input_interference": True,
    }

    with pytest.raises(PermissionError, match=r"Shift\+Delete is permanent"):
        ComputerTool(tmp_path)._execute_sync(
            arguments,
            ToolContext(
                task_id="recoverable-hotkey",
                workspace=str(tmp_path),
                user_prompt="删除选中的文件",
            ),
        )
    assert hotkeys == []

    result = ComputerTool(tmp_path)._execute_sync(
        arguments,
        ToolContext(
            task_id="permanent-hotkey",
            workspace=str(tmp_path),
            user_prompt="彻底删除选中的文件，不进入回收站",
        ),
    )
    assert hotkeys == [("shift", "delete")]
    assert result["action"] == "hotkey"


def test_close_shortcut_requires_observed_window_change(tmp_path: Path, monkeypatch):
    hotkeys = []
    monkeypatch.setattr("deepdesk.windows_text.send_key_chord", lambda keys: hotkeys.append(tuple(keys)))
    states = iter(
        [
            {"window_handle": 101, "window_title": "DeepSeek API", "process_id": 1},
            {"window_handle": 101, "window_title": "Elren", "process_id": 1},
        ]
    )
    monkeypatch.setitem(
        sys.modules,
        "pyautogui",
        SimpleNamespace(hotkey=lambda *keys: hotkeys.append(keys)),
    )
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.computer._foreground_window_state",
        lambda: next(states),
    )
    monkeypatch.setattr("deepdesk.plugins.builtin.computer.time.sleep", lambda _seconds: None)

    result = ComputerTool(tmp_path)._execute_sync(
        {
            "action": "hotkey",
            "keys": ["ctrl", "w"],
            "target_window": "DeepSeek API",
            "allow_user_input_interference": True,
        }
    )

    assert hotkeys == [("ctrl", "w")]
    assert result["effect_verified"] is True
    assert result["before"]["window_title"] == "DeepSeek API"
    assert result["after"]["window_title"] == "Elren"
    wrapped = {"ok": True, "result": result}
    assert AgentEngine._verification_result(
        "computer", {"action": "hotkey", "keys": ["ctrl", "w"]}, wrapped
    ) is True


def test_close_shortcut_fails_when_window_did_not_change(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("deepdesk.windows_text.send_key_chord", lambda keys: None)
    state = {"window_handle": 101, "window_title": "DeepSeek API", "process_id": 1}
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(hotkey=lambda *_keys: None))
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.computer._foreground_window_state",
        lambda: dict(state),
    )
    monkeypatch.setattr("deepdesk.plugins.builtin.computer.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="not verified as closed"):
        ComputerTool(tmp_path)._execute_sync(
            {
                "action": "hotkey",
                "keys": ["ctrl", "w"],
                "target_window": "DeepSeek API",
                "allow_user_input_interference": True,
            }
        )


def test_close_shortcut_is_blocked_until_the_target_page_is_focused(tmp_path: Path, monkeypatch):
    hotkeys = []
    monkeypatch.setitem(
        sys.modules,
        "pyautogui",
        SimpleNamespace(hotkey=lambda *keys: hotkeys.append(keys)),
    )
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.computer._foreground_window_state",
        lambda: {"window_handle": 101, "window_title": "Elren", "process_id": 1},
    )

    with pytest.raises(RuntimeError, match="does not match target_window"):
        ComputerTool(tmp_path)._execute_sync(
            {
                "action": "hotkey",
                "keys": ["ctrl", "w"],
                "target_window": "DeepSeek API",
                "allow_user_input_interference": True,
            }
        )

    assert hotkeys == []

    assert AgentEngine._verification_result(
        "computer",
        {"action": "hotkey", "keys": ["ctrl", "w"]},
        {"ok": True, "result": {"ok": True}},
    ) is False


def test_windows_ui_native_richedit_background_unicode_is_verified(monkeypatch):
    state = {"value": ""}

    def fake_send(_handle, message, _wparam, lparam):
        if message == WM_SETTEXT:
            state["value"] = ctypes.wstring_at(lparam)
            return 1
        if message == WM_GETTEXTLENGTH:
            return len(state["value"])
        if message == WM_GETTEXT:
            buffer = ctypes.create_unicode_buffer(state["value"])
            ctypes.memmove(lparam, buffer, ctypes.sizeof(buffer))
            return len(state["value"])
        raise AssertionError(f"unexpected message {message}")

    match = SimpleNamespace(
        element_info=SimpleNamespace(handle=1234, class_name="RichEditD2DPT")
    )
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.windows_ui._send_message_timeout", fake_send
    )

    result = _set_native_text(match, "神话级-后台-Δ🚀")

    assert state["value"] == "神话级-后台-Δ🚀"
    assert result["input_mode"] == "background_win32_message"
    assert result["verified_exact"] is True


def test_windows_ui_native_fallback_rejects_non_text_controls():
    match = SimpleNamespace(
        element_info=SimpleNamespace(handle=1234, class_name="Button")
    )

    with pytest.raises(RuntimeError, match="supported native Edit/RichEdit"):
        _native_text_control(match)


def test_process_launch_correlates_packaged_app_window(tmp_path: Path, monkeypatch):
    snapshots = iter(
        [
            {10: {"handle": 10, "title": "Existing", "process_id": 1}},
            {
                10: {"handle": 10, "title": "Existing", "process_id": 1},
                77: {
                    "handle": 77,
                    "title": "Untitled - Notepad",
                    "process_id": 9000,
                    "class_name": "Notepad",
                },
            },
        ]
    )
    monkeypatch.setattr(ProcessManagerTool, "_visible_windows", lambda: next(snapshots))
    monkeypatch.setattr(ProcessManagerTool, "_resolve_application", lambda _app: "notepad.exe")
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.process_manager.subprocess.Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=1234),
    )
    context = ToolContext(task_id="launch", workspace=str(tmp_path))

    result = ProcessManagerTool._execute_sync(
        {"action": "launch", "application": "notepad.exe", "background": True},
        context,
    )

    assert result["pid"] == 1234
    assert result["created_windows"] == [
        {
            "handle": 77,
            "title": "Untitled - Notepad",
            "process_id": 9000,
            "class_name": "Notepad",
            "correlation": "appeared_after_this_launch",
        }
    ]


@pytest.mark.asyncio
async def test_human_action_problem_description_reaches_agent(tmp_path: Path):
    gate = HumanActionGate()
    request = HumanActionRequest(
        task_id="task",
        summary="captcha",
        instructions="complete captcha",
    )
    waiting = asyncio.create_task(gate.request(request))
    await asyncio.sleep(0)
    assert gate.take_over(request.id)
    assert gate.resolve(request.id, False, "页面提示会话已过期", False)

    resolution = await waiting

    assert resolution["completed"] is False
    assert resolution["issue_description"] == "页面提示会话已过期"
    assert request.outcome == "problem"


def test_unicode_send_input_builds_key_down_and_up(monkeypatch):
    captured = {}
    from deepdesk import windows_activity
    monkeypatch.setattr(windows_activity._local, "permit", SimpleNamespace(check=lambda: None), raising=False)

    class FakeSendInput:
        argtypes = None
        restype = None

        def __call__(self, count, inputs, size):
            captured["count"] = count
            captured["size"] = size
            captured["flags"] = [inputs[index].ki.dwFlags for index in range(count)]
            return count

    fake_user32 = SimpleNamespace(SendInput=FakeSendInput())
    monkeypatch.setattr("deepdesk.windows_text.ctypes.WinDLL", lambda *args, **kwargs: fake_user32)

    type_unicode("A中")

    assert captured["count"] == 4
    assert captured["size"] > 0
    assert captured["flags"] == [4, 6, 4, 6]


@pytest.mark.asyncio
async def test_task_manager_cancel_interrupts_background_task(tmp_path: Path):
    class RecordingAudit:
        def __init__(self):
            self.records: list[tuple[str, str, dict]] = []

        async def write(self, event_type, task_id, data):
            self.records.append((event_type, task_id, data))

    class BlockingEngine:
        def __init__(self):
            self.audit = RecordingAudit()

        async def run(self, task, cancel):
            task.status = TaskStatus.RUNNING
            await asyncio.Event().wait()

    engine = BlockingEngine()
    store = TaskStore(tmp_path / "cancelled-tasks.db")
    manager = TaskManager(engine, ApprovalGate(), store=store)
    task = manager.create("等待取消", ApprovalPolicy.BALANCED, AgentProfile.GENERAL)
    await asyncio.sleep(0)
    background = manager.background[task.id]

    assert manager.cancel(task.id)
    await background
    await asyncio.sleep(0)

    assert task.status == TaskStatus.CANCELLED
    assert task.id not in manager.background
    assert not manager.cancel(task.id)
    restored = store.get(task.id)
    assert restored is not None
    assert restored.status == TaskStatus.CANCELLED
    assert engine.audit.records == [("task_cancelled", task.id, {})]


@pytest.mark.asyncio
async def test_task_failures_are_bounded_and_redacted_at_both_engine_boundaries(
    monkeypatch, tmp_path: Path
):
    secret = "sk-setup-secret-must-not-reach-task-api"

    class SetupFailureEngine:
        @staticmethod
        def _redact(value):
            return str(value).replace(secret, "[REDACTED]")

        async def run(self, task, cancel):
            raise RuntimeError(f"model setup failed with {secret}: " + ("x" * 8_000))

    manager = TaskManager(SetupFailureEngine(), ApprovalGate())
    task = manager.create("start", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    background = manager.background[task.id]
    await background

    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error.startswith("RuntimeError: model setup failed with [REDACTED]")
    assert secret not in task.error
    assert len(task.error) == 4_000
    assert any(event.type == "error" for event in task.events)

    async def local_region(_self):
        return SimpleNamespace(country_code="UNKNOWN")

    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", local_region)

    class FailingProviderClient:
        keys = [secret]
        provider_models = []

        @staticmethod
        def has_model_credentials(_selector):
            return True

        async def chat(self, _messages, _tools):
            raise RuntimeError(f"provider exposed {secret}: " + ("y" * 8_000))

    provider_task = AgentTask(
        prompt="fail safely",
        model_preference="deepseek-v4-flash",
    )
    engine = AgentEngine(
        client=FailingProviderClient(),
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda _task, _event_type, _data: None,
    )

    await engine.run(provider_task, asyncio.Event())

    assert provider_task.status == TaskStatus.FAILED
    assert provider_task.error is not None
    assert provider_task.error.startswith("RuntimeError: provider exposed [REDACTED]")
    assert secret not in provider_task.error
    assert len(provider_task.error) == 4_000


@pytest.mark.asyncio
async def test_task_manager_fails_worker_that_returns_without_terminal_status():
    class IncompleteEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.RUNNING

    manager = TaskManager(IncompleteEngine(), ApprovalGate())
    task = manager.create("incomplete", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    await manager.background[task.id]

    assert task.status == TaskStatus.FAILED
    assert task.error == "Agent execution ended without a terminal task status"
    assert any(event.type == "error" for event in task.events)


@pytest.mark.asyncio
async def test_task_manager_shutdown_cancels_and_observes_all_workers():
    class BlockingEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.RUNNING
            await asyncio.Event().wait()

    manager = TaskManager(BlockingEngine(), ApprovalGate())
    tasks = [
        manager.create(str(index), ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
        for index in range(2)
    ]
    await asyncio.sleep(0)

    await manager.shutdown(timeout=1)
    await asyncio.sleep(0)

    assert all(task.status == TaskStatus.CANCELLED for task in tasks)
    assert manager.background == {}


@pytest.mark.asyncio
async def test_task_manager_no_longer_creates_a_parallel_private_archive(tmp_path: Path):
    class CompletedEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(CompletedEngine(), ApprovalGate())
    task = manager.create("ordinary prompt", ApprovalPolicy.BALANCED, AgentProfile.GENERAL)
    await asyncio.sleep(0)

    assert task.status == TaskStatus.COMPLETED
    assert not hasattr(manager, "private_archive_dir")
    assert not (tmp_path / "private-chats").exists()


@pytest.mark.asyncio
async def test_task_manager_adds_cross_chat_context_only_to_new_top_level_tasks():
    class CompletedEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(
        CompletedEngine(),
        ApprovalGate(),
        cross_context_provider=lambda prompt: ("sanitized prior result", ["prior-id"]),
    )

    top_level = manager.create("new task", ApprovalPolicy.BALANCED, AgentProfile.GENERAL)
    continuation = manager.create(
        "continue task",
        ApprovalPolicy.BALANCED,
        AgentProfile.GENERAL,
        context_prompt="same-chat continuation",
    )
    await asyncio.sleep(0)

    assert top_level.cross_conversation_context == "sanitized prior result"
    assert top_level.cross_context_task_ids == ["prior-id"]
    assert continuation.cross_conversation_context == ""
    assert continuation.cross_context_task_ids == []


@pytest.mark.asyncio
async def test_task_manager_passes_remote_recipient_scope_to_context_provider():
    class CompletedEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.COMPLETED

    calls = []

    def context_provider(prompt, source, recipient_id, recipient_type):
        calls.append((prompt, source, recipient_id, recipient_type))
        return "recipient-scoped history", ["prior-feishu-task"]

    manager = TaskManager(
        CompletedEngine(),
        ApprovalGate(),
        cross_context_provider=context_provider,
    )

    task = manager.create(
        "继续飞书任务",
        ApprovalPolicy.AUTONOMOUS,
        AgentProfile.GENERAL,
        source="feishu",
        remote_recipient_id="ou_alpha",
        remote_recipient_type="open_id",
    )
    await asyncio.sleep(0)

    assert calls == [("继续飞书任务", "feishu", "ou_alpha", "open_id")]
    assert task.cross_conversation_context == "recipient-scoped history"
    assert task.cross_context_task_ids == ["prior-feishu-task"]


@pytest.mark.asyncio
async def test_output_format_rule_follows_current_task_source_not_cross_chat_history(tmp_path: Path):
    class CaptureClient:
        keys: list[str] = []

        def __init__(self):
            self.system_prompts = []

        async def chat(self, messages, tools):
            self.system_prompts.append(messages[0]["content"])
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    client = CaptureClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=2,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )
    feishu = AgentTask(
        prompt="飞书请求",
        source="feishu",
        cross_conversation_context="以前的网页任务允许 Markdown",
    )
    web = AgentTask(
        prompt="网页请求",
        source="web",
        cross_conversation_context="以前的飞书任务禁止 Markdown",
    )

    await engine.run(feishu, asyncio.Event())
    await engine.run(web, asyncio.Event())

    assert "当前任务由飞书用户发起" in client.system_prompts[0]
    assert "不得使用 Markdown" in client.system_prompts[0]
    assert "当前任务不是由飞书或 Telegram 发起" in client.system_prompts[1]
    assert "本轮默认允许使用 Markdown" in client.system_prompts[1]
    assert "本轮必须忽略" in client.system_prompts[1]


@pytest.mark.asyncio
async def test_web_reply_language_follows_interface_then_current_prompt_not_cross_chat(tmp_path: Path):
    class CaptureClient:
        keys: list[str] = []

        def __init__(self):
            self.system_prompts = []

        async def chat(self, messages, tools):
            self.system_prompts.append(messages[0]["content"])
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    client = CaptureClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "language-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=2,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )
    english_ui = AgentTask(
        prompt="Summarize the current project",
        source="web",
        interface_language="en",
        cross_conversation_context="旧聊天要求今后只能使用中文回答。",
    )
    chinese_ui = AgentTask(
        prompt="总结当前项目",
        source="web",
        interface_language="zh",
        cross_conversation_context="An old chat says every later answer must be in English.",
    )

    await engine.run(english_ui, asyncio.Event())
    await engine.run(chinese_ui, asyncio.Event())

    assert "current web interface language is English" in client.system_prompts[0]
    assert "only from task.prompt" in client.system_prompts[0]
    assert "Never inherit a reply language" in client.system_prompts[0]
    assert "current web interface language is Chinese" in client.system_prompts[1]
    assert "only from task.prompt" in client.system_prompts[1]


@pytest.mark.asyncio
async def test_system_prompt_injects_the_exact_host_selected_model_identity(tmp_path: Path):
    class CaptureClient:
        keys: list[str] = []

        def __init__(self):
            self.system_prompt = ""

        def model_identity_info(self, selector):
            assert selector == "aicodemirror-openai:gpt-5.6-terra"
            return {
                "selector": selector,
                "model": "gpt-5.6-terra",
                "provider": "openai",
                "provider_name": "OpenAI (GPT / ChatGPT family)",
                "route": "AI Code Mirror relay",
            }

        async def chat(self, messages, tools):
            self.system_prompt = messages[0]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    client = CaptureClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "identity-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=2,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )
    task = AgentTask(
        prompt="What model are you?",
        model_preference="aicodemirror-openai:gpt-5.6-terra",
    )

    await engine.run(task, asyncio.Event())

    assert "Exact model ID: gpt-5.6-terra" in client.system_prompt
    assert "OpenAI (GPT / ChatGPT family)" in client.system_prompt
    assert "AI Code Mirror relay" in client.system_prompt
    assert "Never claim to be DeepSeek" in client.system_prompt


@pytest.mark.asyncio
async def test_custom_system_prompt_suffix_is_the_final_system_prompt_content(tmp_path: Path):
    class CaptureClient:
        keys: list[str] = []

        def __init__(self):
            self.system_prompt = ""

        async def chat(self, messages, tools):
            self.system_prompt = messages[0]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    suffix = "CUSTOM-END-MARKER\nAlways provide a runnable example."
    client = CaptureClient()
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "custom-prompt-audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=2,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
        custom_system_prompt_suffix=lambda: suffix,
    )

    await engine.run(AgentTask(prompt="Write a small program"), asyncio.Event())

    assert client.system_prompt.endswith(suffix)
    assert client.system_prompt.count("CUSTOM-END-MARKER") == 1


@pytest.mark.asyncio
async def test_task_manager_continues_cancelled_task_with_safe_context():
    class BlockingEngine:
        async def run(self, task, cancel):
            task.status = TaskStatus.RUNNING
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED

    manager = TaskManager(BlockingEngine(), ApprovalGate())
    previous = AgentTask(
        prompt="打开网站并填写表单",
        status=TaskStatus.CANCELLED,
        error="用户已停止任务",
        active_model="deepseek-v4-pro",
        interface_language="en",
        remote_recipient_id="telegram-chat-42",
        remote_recipient_type="chat_id",
        attachments=["C:/evidence/original.png"],
        discussion_team_enabled=True,
        discussion_team=[
            DiscussionTeamMember(
                name="Lead",
                role="leader",
                model="deepseek-v4-pro",
                assignment="Own the final decision",
            )
        ],
        events=[TaskEvent(type="assistant", data={"content": "网站已经打开"})],
    )
    manager.tasks[previous.id] = previous

    continued = manager.continue_from(
        previous,
        "继续填写第二项",
        ApprovalPolicy.CAUTIOUS,
        AgentProfile.COMPUTER_USE,
        interface_language="zh",
        attachments=["C:/evidence/new.png"],
    )
    await asyncio.sleep(0)

    assert continued.prompt == "继续填写第二项"
    assert continued.parent_task_id == previous.id
    assert "网站已经打开" in continued.context_prompt
    assert "用户的新指令：继续填写第二项" in continued.context_prompt
    assert continued.policy == ApprovalPolicy.CAUTIOUS
    assert continued.active_model == "deepseek-v4-pro"
    assert continued.interface_language == "zh"
    assert continued.remote_recipient_id == "telegram-chat-42"
    assert continued.remote_recipient_type == "chat_id"
    assert continued.attachments == [
        "C:/evidence/original.png",
        "C:/evidence/new.png",
    ]
    assert continued.discussion_team_enabled is True
    assert [member.model_dump() for member in continued.discussion_team] == [
        member.model_dump() for member in previous.discussion_team
    ]

    assert manager.cancel(continued.id)
    await asyncio.sleep(0)


def test_task_manager_refuses_to_continue_remote_task_without_original_route():
    manager = TaskManager(SimpleNamespace(), ApprovalGate())
    previous = AgentTask(
        prompt="legacy remote task",
        source="telegram",
        status=TaskStatus.COMPLETED,
        result="done",
    )

    with pytest.raises(ValueError, match="原始收件人路由"):
        manager.continue_from(
            previous,
            "continue",
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
        )

    assert manager.tasks == {}


@pytest.mark.asyncio
async def test_human_takeover_pauses_and_resumes_same_task(tmp_path: Path):
    class FakeClient:
        keys: list[str] = []

        def __init__(self):
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
                                "id": "human-1",
                                "type": "function",
                                "function": {
                                    "name": "request_human_action",
                                    "arguments": (
                                        '{"summary":"完成人机验证",'
                                        '"instructions":"勾选验证框",'
                                        '"target_window":"Example"}'
                                    ),
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="测试",
                )
            assert messages[-1]["role"] == "tool"
            assert '"completed": true' in messages[-1]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "已经继续", "tool_calls": []},
                usage={},
                active_key="测试",
            )

    registry = PluginRegistry()
    registry.register(HumanActionTool())
    gate = HumanActionGate()
    task = AgentTask(prompt="打开网站")

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=FakeClient(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=gate,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=4,
        emit=emit,
    )
    running = asyncio.create_task(engine.run(task, asyncio.Event()))
    for _ in range(300):
        if gate.pending:
            break
        await asyncio.sleep(0.01)

    assert task.status == TaskStatus.WAITING_USER
    request = next(iter(gate.pending.values()))
    assert request.summary == "完成人机验证"
    assert not gate.resolve(request.id, True)
    assert gate.take_over(request.id).taken_over
    assert gate.resolve(request.id, True)

    await running
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "已经继续"
    assert not gate.pending
    assert any(
        event.type == "tool_call" and event.data.get("tool") == "request_human_action"
        for event in task.events
    )


@pytest.mark.asyncio
async def test_high_risk_approval_surfaces_exact_task_before_waiting(tmp_path: Path):
    class HighRiskTool(ToolPlugin):
        name = "high_risk"
        description = "A test-only high-risk operation"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        def risk(self, arguments):
            return Risk.HIGH

        async def execute(self, arguments, context):
            return {"done": True}

    class ApprovalClient:
        keys: list[str] = []

        def __init__(self):
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
                                "id": "approve-1",
                                "type": "function",
                                "function": {"name": "high_risk", "arguments": "{}"},
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "approved", "tool_calls": []},
                usage={},
                active_key="test",
            )

    registry = PluginRegistry()
    registry.register(HighRiskTool())
    gate = ApprovalGate()
    surfaced: list[str] = []
    task = AgentTask(prompt="perform high risk action", policy=ApprovalPolicy.CAUTIOUS)
    engine = AgentEngine(
        client=ApprovalClient(),
        registry=registry,
        approvals=gate,
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=3,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
        on_approval=lambda request: surfaced.append(request.task_id),
    )

    running = asyncio.create_task(engine.run(task, asyncio.Event()))
    for _ in range(300):
        if gate.pending:
            break
        await asyncio.sleep(0.01)

    assert surfaced == [task.id]
    request = next(iter(gate.pending.values()))
    assert gate.resolve(request.id, True)
    await running
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_engine_does_not_auto_stop_repeated_calls_across_steps(tmp_path: Path):
    class RepeatingClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls <= 3:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"read-{self.calls}",
                                "type": "function",
                                "function": {
                                    "name": "filesystem",
                                    "arguments": '{"action":"read","path":"note.txt"}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    registry = PluginRegistry()
    registry.register(FileSystemTool())
    task = AgentTask(prompt="read repeatedly")

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=RepeatingClient(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=5,
        emit=emit,
    )

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "done"
    assert not any(event.type in {"loop_review", "loop_detected"} for event in task.events)


@pytest.mark.asyncio
async def test_engine_skips_duplicate_tool_calls_in_one_response(tmp_path: Path):
    class CountingTool(ToolPlugin):
        name = "counter"
        description = "Count executions"
        parameters = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

        def __init__(self):
            self.executions = 0

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            self.executions += 1
            return {"value": arguments["value"]}

    class DuplicateClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                function = {"name": "counter", "arguments": '{"value":7}'}
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "count-1", "type": "function", "function": function},
                            {"id": "count-2", "type": "function", "function": function},
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            duplicate_result = next(
                item for item in messages if item.get("tool_call_id") == "count-2"
            )
            assert '"duplicate": true' in duplicate_result["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    tool = CountingTool()
    registry = PluginRegistry()
    registry.register(tool)
    task = AgentTask(prompt="call once")

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=DuplicateClient(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=3,
        emit=emit,
    )

    await engine.run(task, asyncio.Event())

    assert tool.executions == 1
    assert task.status == TaskStatus.COMPLETED
    assert any(event.type == "tool_skipped" for event in task.events)


def test_tool_argument_schema_validator_rejects_wrong_types_and_extra_keys() -> None:
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "mode": {"type": "string", "enum": ["safe", "fast"]},
            "items": {"type": "array", "items": {"type": "number"}},
        },
        "required": ["count", "mode"],
        "additionalProperties": False,
    }
    assert AgentEngine._validate_tool_arguments(
        schema, {"count": 2, "mode": "safe", "items": [1, 2.5]}
    ) == []
    issues = AgentEngine._validate_tool_arguments(
        schema,
        {"count": "2", "mode": "unsafe", "items": [True], "surprise": 1},
    )
    assert "$.count must be integer" in issues
    assert "$.mode must be one of ['safe', 'fast']" in issues
    assert "$.items[0] must be number" in issues
    assert "$.surprise is not allowed" in issues


def test_tool_argument_schema_validator_supports_union_types_without_crashing() -> None:
    schema = {
        "type": "object",
        "properties": {
            "max_steps": {"type": ["integer", "null"], "minimum": 1},
            "model": {"type": "string"},
        },
        "additionalProperties": False,
    }

    assert AgentEngine._validate_tool_arguments(
        schema, {"max_steps": None, "model": "aicodemirror-openai:gpt-5.6-sol"}
    ) == []
    issues = AgentEngine._validate_tool_arguments(schema, {"max_steps": []})
    assert issues == ["$.max_steps must be integer or null"]


@pytest.mark.asyncio
async def test_engine_retries_empty_response_instead_of_completing(tmp_path: Path):
    class EmptyThenAnswerClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.recovery_calls = 0
            self.second_call_messages = []

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 2:
                self.second_call_messages = list(messages)
            content = None if self.calls == 1 else "Recovered final answer"
            return DeepSeekReply(
                message={
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": "spent output budget" if self.calls == 1 else "",
                },
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            self.calls += 1
            self.recovery_calls += 1
            self.second_call_messages = list(messages)
            return DeepSeekReply(
                message={"role": "assistant", "content": "Recovered final answer"},
                usage={"completion_tokens": 12},
                active_key="test",
            )

    emitted: list[TaskEvent] = []
    client = EmptyThenAnswerClient()
    task = AgentTask(prompt="finish the task")

    def emit(current, event_type, data):
        event = TaskEvent(type=event_type, data=data)
        current.events.append(event)
        emitted.append(event)

    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=emit,
    )

    await engine.run(task, asyncio.Event())

    assert task.result == "Recovered final answer"
    assert client.calls == 2
    assert client.recovery_calls == 1
    assert [message["role"] for message in client.second_call_messages] == [
        "system",
        "user",
        "user",
    ]
    assert any(event.type == "model_retry" for event in emitted)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "empty_assistant_response"),
        ("Let me inspect the evidence, then verify", "unfinished_progress_narration"),
    ],
)
@pytest.mark.asyncio
async def test_engine_bounds_unusable_response_recovery_without_limiting_tool_steps(
    tmp_path: Path,
    monkeypatch,
    content: str | None,
    reason: str,
):
    async def local_region(_self):
        return SimpleNamespace(country_code="UNKNOWN")

    # This is a bounded model-recovery test, not a public-IP network probe.
    # Real network cleanup made its one-second deadline host-dependent on Mac.
    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", local_region)

    class AlwaysUnusableClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.recovery_calls = 0

        async def chat(self, _messages, _tools):
            self.calls += 1
            return DeepSeekReply(
                message={"role": "assistant", "content": content},
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            self.recovery_calls += 1
            return await self.chat(messages, tools)

    monkeypatch.setattr(
        "deepdesk.engine.HOST_EMPTY_RECOVERY_BACKOFF_SECONDS", (0.0, 0.0)
    )
    client = AlwaysUnusableClient()
    task = AgentTask(prompt="finish the task")
    audit_path = tmp_path / "audit.jsonl"
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(audit_path),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await asyncio.wait_for(engine.run(task, asyncio.Event()), timeout=1)

    assert task.status == TaskStatus.FAILED
    assert client.calls == 3
    assert client.recovery_calls == (2 if content is None else 0)
    retries = [
        event
        for event in task.events
        if event.type == "model_retry" and event.data.get("reason") == reason
    ]
    assert [event.data["attempt"] for event in retries] == [1, 2]
    assert all(event.data["max_attempts"] == 2 for event in retries)
    exhausted = [
        event for event in task.events if event.type == "model_recovery_exhausted"
    ]
    assert len(exhausted) == 1
    assert exhausted[0].data["category"] == "empty_or_progress"
    assert exhausted[0].data["invalid_response_count"] == 3
    assert "已停止自动重试" in (task.error or "")
    audit_events = [
        json.loads(line)["event"]
        for line in audit_path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert audit_events.count("model_recovery_exhausted") == 1


@pytest.mark.asyncio
async def test_engine_can_recover_on_last_bounded_unusable_attempt(
    tmp_path: Path, monkeypatch
):
    class ThirdReplySucceedsClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, _messages, _tools):
            self.calls += 1
            return DeepSeekReply(
                message={
                    "role": "assistant",
                    "content": None if self.calls < 3 else "Verified result",
                },
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            return await self.chat(messages, tools)

    monkeypatch.setattr(
        "deepdesk.engine.HOST_EMPTY_RECOVERY_BACKOFF_SECONDS", (0.0, 0.0)
    )
    client = ThirdReplySucceedsClient()
    task = AgentTask(prompt="finish the task")
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await asyncio.wait_for(engine.run(task, asyncio.Event()), timeout=1)

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "Verified result"
    assert client.calls == 3
    assert not any(
        event.type == "model_recovery_exhausted" for event in task.events
    )


def test_engine_extracts_explicit_filesystem_scope():
    extract = AgentEngine._explicit_filesystem_scope
    assert extract("Only work inside src") == "src"
    assert extract("只允许在 src 内工作") == "src"
    assert extract("Only work inside work/jobs/U01. Do not inspect siblings.") == "work/jobs/U01"
    assert extract("只允许在 work/jobs/U02 内工作") == "work/jobs/U02"
    assert extract("[ELREN_SCOPE: work/jobs/U03]") == "work/jobs/U03"
    assert extract("[DEEPDESK_SCOPE: work/jobs/legacy]") == "work/jobs/legacy"
    assert extract("Read work/jobs/U04 and compare work/jobs/U05") is None


def test_high_risk_for_real_mouse_click_and_discarding_unsaved_content(tmp_path: Path):
    assert ComputerUseAgentTool(tmp_path).risk({"action": "click"}) == Risk.HIGH
    assert WindowsUITool().risk(
        {"action": "close_window", "discard_unsaved": True}
    ) == Risk.HIGH
    assert WindowsUITool().risk(
        {"action": "close_window", "discard_unsaved": False}
    ) == Risk.MEDIUM


@pytest.mark.asyncio
async def test_engine_rejects_filesystem_path_outside_explicit_scope(tmp_path: Path):
    class ScopeClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.rejection_seen = False

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "outside",
                                "type": "function",
                                "function": {
                                    "name": "filesystem",
                                    "arguments": '{"action":"list","path":"work/jobs"}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            self.rejection_seen = any(
                message.get("role") == "tool"
                and "user_filesystem_scope" in str(message.get("content"))
                for message in messages
            )
            return DeepSeekReply(
                message={"role": "assistant", "content": "Stayed in scope."},
                usage={},
                active_key="test",
            )

    (tmp_path / "work" / "jobs" / "U01").mkdir(parents=True)
    registry = PluginRegistry()
    registry.register(FileSystemTool())
    client = ScopeClient()
    task = AgentTask(prompt="Only work inside work/jobs/U01. Finish safely.")
    engine = AgentEngine(
        client=client,
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

    assert client.rejection_seen
    assert task.status == TaskStatus.COMPLETED
    rejected = [event for event in task.events if event.type == "tool_rejected"]
    assert rejected and rejected[0].data["scope"] == "work/jobs/U01"


def test_engine_has_no_cross_step_loop_autostop_detector():
    assert not hasattr(AgentEngine, "_detect_tool_loop")
    assert not hasattr(AgentEngine, "_model_judges_loop")


def test_engine_distinguishes_progress_narration_from_final_answer():
    detect = AgentEngine._looks_like_progress_narration
    assert detect("Let me inspect the tests, then verify the counterexample")
    assert detect(
        "I now have the full picture. Let me pin down exact line numbers, then verify"
    )
    assert detect("接下来我检查产物并继续验证")
    assert not detect("I fixed the parser and verified all two tests passed.")
    assert not detect("任务完成：测试全部通过。")


def test_engine_identifies_browser_projects_and_real_headless_preview_evidence():
    task = AgentTask(
        prompt="开发一个响应式网站项目，完成后通过浏览器实测",
        agent_profile=AgentProfile.CODER,
    )
    assert AgentEngine._requires_background_browser_preview(task, [])

    vague_follow_up = AgentTask(
        prompt="把这个问题修好",
        agent_profile=AgentProfile.CODER,
    )
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "edit-css",
                    "type": "function",
                    "function": {
                        "name": "filesystem",
                        "arguments": json.dumps(
                            {"action": "edit", "path": "static/app.css", "edits": []}
                        ),
                    },
                }
            ],
        }
    ]
    assert AgentEngine._requires_background_browser_preview(vague_follow_up, messages)
    assert not AgentEngine._requires_background_browser_preview(
        AgentTask(prompt="解释浏览器预览的原理"), []
    )

    arguments = {"action": "open", "url": "http://127.0.0.1:8000"}
    successful = {
        "ok": True,
        "result": {
            "url": "http://127.0.0.1:8000/",
            "foreground_used": False,
            "browser_ui_opened": False,
            "isolated_profile": True,
        },
    }
    assert AgentEngine._background_browser_preview_succeeded(arguments, successful)
    assert not AgentEngine._background_browser_preview_succeeded(
        arguments,
        {"ok": True, "result": {**successful["result"], "browser_ui_opened": True}},
    )


@pytest.mark.asyncio
async def test_engine_requires_background_browser_preview_before_web_completion(
    tmp_path: Path,
):
    class HeadlessPreviewTool(ToolPlugin):
        name = "background_browser"
        description = "Preview a page in Elren's isolated headless browser"
        parameters = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "url": {"type": "string"},
                "capture": {"type": "boolean"},
            },
            "required": ["action"],
        }

        def __init__(self):
            self.calls = 0

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            self.calls += 1
            return {
                "url": arguments["url"],
                "title": "Preview",
                "execution_mode": "background",
                "foreground_used": False,
                "browser_ui_opened": False,
                "isolated_profile": True,
            }

    class PreviewGateClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.host_gate_seen = False

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={"role": "assistant", "content": "Implemented the website."},
                    usage={},
                    active_key="test",
                )
            if self.calls == 2:
                self.host_gate_seen = any(
                    "HOST BACKGROUND PREVIEW CHECK" in str(message.get("content") or "")
                    for message in messages
                )
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "background-preview",
                                "type": "function",
                                "function": {
                                    "name": "background_browser",
                                    "arguments": json.dumps(
                                        {
                                            "action": "open",
                                            "url": "http://127.0.0.1:8000",
                                            "capture": True,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "Previewed and verified."},
                usage={},
                active_key="test",
            )

    preview = HeadlessPreviewTool()
    registry = PluginRegistry()
    registry.register(preview)
    client = PreviewGateClient()
    task = AgentTask(
        prompt="Build a responsive website project and verify it.",
        agent_profile=AgentProfile.CODER,
    )
    engine = AgentEngine(
        client=client,
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

    assert client.host_gate_seen
    assert client.calls == 3
    assert preview.calls == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "Previewed and verified."
    assert any(event.type == "background_preview_required" for event in task.events)
    verified = next(
        event for event in task.events if event.type == "background_preview_verified"
    )
    assert verified.data["foreground_browser_opened"] is False


@pytest.mark.asyncio
async def test_engine_allows_bounded_fallback_after_concrete_background_browser_error(
    tmp_path: Path,
):
    class BrokenHeadlessBrowser(ToolPlugin):
        name = "background_browser"
        description = "Unavailable isolated headless browser"
        parameters = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["action"],
        }

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            raise RuntimeError("packaged runtime unavailable")

    class FailureAwareClient:
        keys: list[str] = []

        def __init__(self):
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
                                "id": "failed-preview",
                                "type": "function",
                                "function": {
                                    "name": "background_browser",
                                    "arguments": json.dumps(
                                        {"action": "open", "url": "http://127.0.0.1:8000"}
                                    ),
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={
                    "role": "assistant",
                    "content": (
                        "The background preview was unavailable; the limitation is reported."
                    ),
                },
                usage={},
                active_key="test",
            )

    registry = PluginRegistry()
    registry.register(BrokenHeadlessBrowser())
    client = FailureAwareClient()
    task = AgentTask(
        prompt="Build a responsive website project and verify it.",
        agent_profile=AgentProfile.CODER,
    )
    engine = AgentEngine(
        client=client,
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

    assert client.calls == 2
    assert task.status == TaskStatus.COMPLETED
    assert any(event.type == "background_preview_unavailable" for event in task.events)
    assert not any(event.type == "background_preview_required" for event in task.events)


def test_engine_detects_missing_required_python_functions():
    detect = AgentEngine._missing_required_python_functions
    prompt = "Complete this function:\n\ndef required_name(value: int) -> int:\n    pass"

    assert detect(prompt, "```python\ndef other_name(value):\n    return value\n```") == [
        "required_name"
    ]
    assert detect(prompt, "def required_name(value):\n    return value") == []
    assert detect("Explain def sample(value):", "No implementation was requested.") == []


def test_engine_validates_explicit_json_only_contract_without_rewriting():
    validate = AgentEngine._json_output_contract_error
    prompt = "Output valid JSON only (no markdown)."
    assert validate(prompt, '{"task_id":"T1","evidence":{"tool_calls":[]}}') is None
    error = validate(prompt, '{"task_id":"T1"')
    assert error is not None
    assert "invalid JSON" in error
    assert validate(prompt, "preface\n{\"task_id\":\"T1\"}") is not None
    assert validate("Explain JSON parsing in prose", "not JSON") is None


def test_engine_json_only_contract_accepts_chinese_marker_and_rejects_scalars():
    validate = AgentEngine._json_output_contract_error
    assert validate("只返回 JSON，不要解释", '[{"ok":true}]') is None
    assert validate("仅输出 JSON", '"plain scalar"') == (
        "final JSON must be an object or array, not a scalar"
    )


@pytest.mark.asyncio
async def test_coder_retries_when_final_code_omits_required_function(tmp_path: Path):
    class MissingThenCompleteClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.recovery_calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            content = (
                "def wrong_name(value):\n    return value"
                if self.calls == 1
                else "def required_name(value):\n    return value"
            )
            return DeepSeekReply(
                message={"role": "assistant", "content": content},
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            self.recovery_calls += 1
            return await self.chat(messages, tools)

    client = MissingThenCompleteClient()
    task = AgentTask(
        prompt="Complete this function:\n\ndef required_name(value: int) -> int:\n    pass",
        agent_profile=AgentProfile.CODER,
        model_preference="deepseek-v4-flash",
    )
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
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

    assert client.calls == 2
    assert client.recovery_calls == 0
    assert "def required_name" in task.result
    assert any(
        event.type == "model_retry"
        and event.data.get("reason") == "missing_required_python_functions"
        for event in task.events
    )


@pytest.mark.asyncio
async def test_engine_retries_malformed_explicit_json_only_final(tmp_path: Path):
    class TruncatedThenValidClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.correction_seen = False
            self.recovery_calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                content = '{"task_id":"T1","evidence":{"tool_calls":[]}'
            else:
                self.correction_seen = any(
                    "violated the explicit JSON-only contract" in str(message.get("content"))
                    for message in messages
                    if message.get("role") == "user"
                )
                content = '{"task_id":"T1","evidence":{"tool_calls":[]}}'
            return DeepSeekReply(
                message={"role": "assistant", "content": content},
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            self.recovery_calls += 1
            return await self.chat(messages, tools)

    client = TruncatedThenValidClient()
    task = AgentTask(prompt="Return valid JSON only: {\"task_id\":\"T1\"}")
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
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

    assert client.calls == 2
    assert client.recovery_calls == 1
    assert client.correction_seen is True
    assert json.loads(task.result or "") == {
        "task_id": "T1",
        "evidence": {"tool_calls": []},
    }
    assert any(
        event.type == "model_retry"
        and event.data.get("reason") == "invalid_json_output_contract"
        for event in task.events
    )


@pytest.mark.parametrize("contract", ["json", "python_function"])
@pytest.mark.asyncio
async def test_engine_bounds_final_output_contract_recovery(
    tmp_path: Path, contract: str
):
    class AlwaysInvalidContractClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.recovery_calls = 0

        async def chat(self, _messages, _tools):
            self.calls += 1
            content = (
                '{"task_id":"T1"'
                if contract == "json"
                else "def wrong_name(value):\n    return value"
            )
            return DeepSeekReply(
                message={"role": "assistant", "content": content},
                usage={},
                active_key="test",
            )

        async def chat_recovery(self, messages, tools):
            self.recovery_calls += 1
            return await self.chat(messages, tools)

    client = AlwaysInvalidContractClient()
    task = AgentTask(
        prompt=(
            'Return valid JSON only: {"task_id":"T1"}'
            if contract == "json"
            else "Complete this function:\n\ndef required_name(value: int) -> int:\n    pass"
        ),
        agent_profile=(
            AgentProfile.GENERAL if contract == "json" else AgentProfile.CODER
        ),
    )
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await asyncio.wait_for(engine.run(task, asyncio.Event()), timeout=1)

    category = (
        "json_contract" if contract == "json" else "missing_required_functions"
    )
    reason = (
        "invalid_json_output_contract"
        if contract == "json"
        else "missing_required_python_functions"
    )
    assert task.status == TaskStatus.FAILED
    assert client.calls == 3
    assert client.recovery_calls == (2 if contract == "json" else 0)
    retries = [
        event
        for event in task.events
        if event.type == "model_retry" and event.data.get("reason") == reason
    ]
    assert [event.data["attempt"] for event in retries] == [1, 2]
    exhausted = [
        event for event in task.events if event.type == "model_recovery_exhausted"
    ]
    assert len(exhausted) == 1
    assert exhausted[0].data["category"] == category
    assert exhausted[0].data["invalid_response_count"] == 3


@pytest.mark.asyncio
async def test_engine_enforces_explicit_user_tool_prohibition(tmp_path: Path):
    class ForbiddenThenAnswerClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0
            self.rejection_seen = False

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "forbidden-shell",
                                "type": "function",
                                "function": {
                                    "name": "shell",
                                    "arguments": '{"command":"Write-Output should-not-run"}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            self.rejection_seen = any(
                message.get("role") == "tool"
                and "user_tool_constraint" in str(message.get("content"))
                for message in messages
            )
            return DeepSeekReply(
                message={"role": "assistant", "content": "Constraint respected."},
                usage={},
                active_key="test",
            )

    registry = PluginRegistry()
    registry.register(ShellTool())
    client = ForbiddenThenAnswerClient()
    task = AgentTask(prompt="Do not call these tools: shell. Finish safely.")

    engine = AgentEngine(
        client=client,
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

    assert client.rejection_seen
    assert task.status == TaskStatus.COMPLETED
    assert not any(event.type == "tool_call" for event in task.events)
    assert any(event.type == "tool_rejected" for event in task.events)


@pytest.mark.asyncio
async def test_bounded_repetition_completes_without_a_loop_judge(tmp_path: Path):
    class BoundedPollClient:
        keys: list[str] = []

        def __init__(self):
            self.agent_calls = 0

        async def chat(self, messages, tools):
            self.agent_calls += 1
            if self.agent_calls <= 3:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"poll-{self.agent_calls}",
                                "type": "function",
                                "function": {
                                    "name": "filesystem",
                                    "arguments": '{"action":"list","path":"."}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "bounded polling done", "tool_calls": []},
                usage={},
                active_key="test",
            )

    client = BoundedPollClient()
    registry = PluginRegistry()
    registry.register(FileSystemTool())
    task = AgentTask(prompt="Call the same list operation exactly three times, then finish.")

    def emit(current, event_type, data):
        current.events.append(TaskEvent(type=event_type, data=data))

    engine = AgentEngine(
        client=client,
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=6,
        emit=emit,
    )

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert client.agent_calls == 4
    assert task.result == "bounded polling done"
    assert not any(event.type == "loop_review" for event in task.events)
    assert not any(event.type == "loop_detected" for event in task.events)


@pytest.mark.asyncio
async def test_agent_execution_ignores_legacy_numeric_step_limit(tmp_path: Path):
    class FiveStepClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls < 5:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"missing-{self.calls}",
                                "type": "function",
                                "function": {
                                    "name": "missing_tool",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "completed without a step cap"},
                usage={},
                active_key="test",
            )

    client = FiveStepClient()
    task = AgentTask(prompt="continue until complete")
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=1,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await engine.run(task, asyncio.Event())

    assert client.calls == 5
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "completed without a step cap"


@pytest.mark.asyncio
async def test_failed_verification_is_reported_without_a_host_repair_loop(tmp_path: Path):
    class VerificationTool(ToolPlugin):
        name = "sandbox"
        description = "Run the requested verifier"
        parameters = {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

        def __init__(self):
            self.calls = 0

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            self.calls += 1
            return {
                "exit_code": 1 if self.calls == 1 else 0,
                "stdout": "failed" if self.calls == 1 else "passed",
            }

    class RepairClient:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls in {1, 7}:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"verify-{self.calls}",
                                "type": "function",
                                "function": {
                                    "name": "sandbox",
                                    "arguments": '{"command":"python -m pytest"}',
                                },
                            }
                        ],
                    },
                    usage={},
                    active_key="test",
                )
            if self.calls < 7:
                return DeepSeekReply(
                    message={"role": "assistant", "content": "premature completion"},
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "verified completion"},
                usage={},
                active_key="test",
            )

    verifier = VerificationTool()
    registry = PluginRegistry()
    registry.register(verifier)
    task = AgentTask(prompt="Implement and test this software")
    engine = AgentEngine(
        client=RepairClient(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=1,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "premature completion"
    assert verifier.calls == 1
    retries = [
        event.data["attempt"]
        for event in task.events
        if event.type == "model_retry"
        and event.data.get("reason") == "unresolved_verification_failure"
    ]
    assert retries == []
    warnings = [
        event
        for event in task.events
        if event.type == "verification_unresolved"
    ]
    assert len(warnings) == 1
    assert warnings[0].data["failure"]["tool"] == "sandbox"
