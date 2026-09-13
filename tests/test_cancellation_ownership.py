"""Cancellation regressions: isolated stores/providers and owned local children only."""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import socket
import sys
import threading
from pathlib import Path

import psutil
import pytest
import pytest_asyncio

from deepdesk.audit import AuditLog
from deepdesk.control_files import ControlFileGuard
from deepdesk.deepseek import DeepSeekReply
from deepdesk.egress_region import EgressRegion
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.models import AgentProfile, ApprovalPolicy, Risk, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolPlugin
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.builtin.human_action import HumanActionTool
from deepdesk.plugins.builtin.shell import ShellTool
from deepdesk.posix_sandbox import PosixResourceSandbox
from deepdesk.task_store import TaskStore
from deepdesk.windows_sandbox import WindowsJobSandbox


async def eventually(predicate, seconds=10):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch):
    # The event loop has already made its local wakeup socket. No external
    # transport/provider/egress request is allowed from these regressions.
    def deny(*_args, **_kwargs):
        raise AssertionError("Network is prohibited in cancellation regressions")

    async def region(_self):
        return EgressRegion("US")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", region)


class Provider:
    keys = []

    def __init__(self, tool=None, arguments=None, waiting=False):
        self.tool, self.arguments, self.waiting = tool, arguments or {}, waiting
        self.started = asyncio.Event()
        self.calls = 0
        self.variables = {key: contextvars.ContextVar(key) for key in ("model", "reasoning", "failover", "stream")}
        self.resets = []

    def bind_task_model(self, value): return self.variables["model"].set(value)
    def bind_task_reasoning_effort(self, value): return self.variables["reasoning"].set(value)
    def bind_task_model_failover(self, value): return self.variables["failover"].set(value)
    def bind_task_stream_progress(self, value): return self.variables["stream"].set(value)
    def reset_task_model(self, token): self.reset("model", token)
    def reset_task_reasoning_effort(self, token): self.reset("reasoning", token)
    def reset_task_model_failover(self, token): self.reset("failover", token)
    def reset_task_stream_progress(self, token): self.reset("stream", token)

    def reset(self, key, token):
        self.variables[key].reset(token)
        self.resets.append(key)

    async def chat(self, _messages, _tools):
        self.calls += 1
        self.started.set()
        if self.waiting:
            await asyncio.Event().wait()
        message = {"role": "assistant", "content": "The local test is complete.", "tool_calls": []}
        if self.calls == 1 and self.tool:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "qa-call", "type": "function", "function": {"name": self.tool, "arguments": json.dumps(self.arguments)}}]}
        return DeepSeekReply(message=message, usage={}, active_key="synthetic-not-a-key")


class Harness:
    def __init__(self, workspace, provider, plugins=(), rules=False):
        self.workspace, self.provider = workspace, provider
        workspace.mkdir(parents=True, exist_ok=True)
        if rules:
            (workspace / "AGENTS.md").write_text("Synthetic QA rules. No external calls.\n", encoding="utf-8")
        self.approvals, self.humans = ApprovalGate(), HumanActionGate()
        guard = ControlFileGuard(workspace, state_path=workspace / "control.json")
        self.registry = PluginRegistry(control_guard=guard)
        self.registry.register_many(plugins)
        self.audit = AuditLog(workspace / "audit.jsonl")
        self.store = TaskStore(workspace / "tasks.sqlite")
        self.engine = AgentEngine(client=provider, registry=self.registry, approvals=self.approvals,
            human_actions=self.humans, audit=self.audit, workspace=str(workspace), max_steps=None,
            emit=lambda task, kind, data: self.manager.emit(task, kind, data))
        self.manager = TaskManager(self.engine, self.approvals, store=self.store)

    def start(self, policy=ApprovalPolicy.AUTONOMOUS):
        self.task = self.manager.create("Perform a local synthetic operation.", policy,
            AgentProfile.GENERAL, model_preference="deepseek-v4-flash")
        self.worker = self.manager.background[self.task.id]

    async def joined(self):
        await asyncio.wait_for(asyncio.shield(self.worker), 12)
        await asyncio.sleep(0)

    def assert_terminal(self, status):
        assert self.task.status == self.store.get(self.task.id).status == status
        assert self.task.id not in self.manager.background
        assert self.task.id not in self.manager.cancel_events
        assert self.task.id not in self.manager.steering_queues
        assert self.task.id not in self.manager._started_tasks
        for gate in (self.approvals, self.humans):
            assert not gate.pending and not gate._futures


@pytest.mark.asyncio
@pytest.mark.parametrize("yield_first", [False, True])
async def test_stop_before_or_after_first_timeslice_is_durable(tmp_path, yield_first):
    h = Harness(tmp_path, Provider(waiting=True))
    h.start()
    if yield_first:
        await h.provider.started.wait()
    assert h.manager.cancel(h.task.id)
    assert h.manager.cancel(h.task.id)  # repeated Stop must not interrupt cleanup
    await h.joined()
    h.assert_terminal(TaskStatus.CANCELLED)
    assert h.provider.calls == int(yield_first)
    assert not h.manager.cancel(h.task.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_atomic_commit_remains_owned_until_safe_boundary(tmp_path, monkeypatch, cancel):
    h = Harness(tmp_path, Provider("filesystem", {"action": "write", "path": "result.txt", "content": "new"}), [FileSystemTool()])
    destination = tmp_path / "result.txt"
    destination.write_text("old", encoding="utf-8")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    replace = os.replace

    def slow_replace(src, dst, *args, **kwargs):
        if Path(dst).resolve() == destination:
            entered.set()
            assert release.wait(10), "Unreleased synthetic commit"
            try:
                return replace(src, dst, *args, **kwargs)
            finally:
                done.set()
        return replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("deepdesk.plugins.builtin.filesystem.os.replace", slow_replace)
    h.start()
    try:
        await eventually(entered.is_set)
        if cancel:
            assert h.manager.cancel(h.task.id)
            await asyncio.sleep(0)
            h.worker.cancel()  # also emulate a second shutdown cancellation
            await asyncio.sleep(0.03)
            assert not h.worker.done()
            assert h.task.id in h.manager.background
            assert h.task.status == h.store.get(h.task.id).status == TaskStatus.RUNNING
        assert destination.read_text("utf-8") == "old"
        release.set()
        await h.joined()
        h.assert_terminal(TaskStatus.CANCELLED if cancel else TaskStatus.COMPLETED)
        assert done.is_set() and destination.read_text("utf-8") == "new"
        assert not list(tmp_path.glob(".result.txt.elren-*.tmp"))
        assert len(h.provider.resets) == 4
    finally:
        release.set()
        if not h.worker.done(): h.manager.cancel(h.task.id)
        await h.joined()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at_rules", [False, True])
async def test_rules_setup_cancellation_closes_probe_and_bindings(tmp_path, monkeypatch, cancel_at_rules):
    h = Harness(tmp_path, Provider(waiting=True), rules=True)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    probes = []

    async def pending_probe(_detector):
        probes.append(asyncio.current_task())
        await asyncio.Event().wait()

    original_append = h.audit._append

    def slow_append(line):
        if json.loads(line)["event"] == "project_rules_loaded":
            entered.set()
            assert release.wait(10)
            try: return original_append(line)
            finally: done.set()
        return original_append(line)

    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", pending_probe)
    monkeypatch.setattr(h.audit, "_append", slow_append)
    h.start()
    try:
        await eventually(entered.is_set)
        if not cancel_at_rules:
            release.set()
            await h.provider.started.wait()
        h.manager.cancel(h.task.id)
        await h.joined()
        h.assert_terminal(TaskStatus.CANCELLED)
        assert len(probes) == 1 and probes[0].done()
        assert sorted(h.provider.resets) == ["failover", "model", "reasoning", "stream"]
    finally:
        release.set()
        await eventually(done.is_set)
        if not h.worker.done(): h.manager.cancel(h.task.id)
        await h.joined()


class GateTool(ToolPlugin):
    name, description = "qa_local_operation", "Synthetic local gate control"
    parameters = {"type": "object", "properties": {}}
    executions = 0
    def risk(self, _arguments): return Risk.HIGH
    async def execute(self, _arguments, _context):
        self.executions += 1
        return {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["skip", "retry"])
@pytest.mark.parametrize("decision", ["allow", "deny", "cancel"])
async def test_uncertain_dispatch_resolution_requires_explicit_approval(tmp_path, resolution, decision):
    from deepdesk.plugins.builtin.cron import CronTool
    from deepdesk.scheduler import Scheduler as RealScheduler

    class Scheduler(RealScheduler):
        calls = []

        def update(self, schedule_id, patch):
            self.calls.append((schedule_id, patch.resolve_dispatch))
            return {"id": schedule_id, "resolved": patch.resolve_dispatch}

    scheduler = Scheduler(tmp_path / "gate-schedule.db", lambda *args: "synthetic-only")
    tool = CronTool(scheduler)
    args = {"action": "update", "id": "synthetic-schedule", "schedule": {"resolve_dispatch": resolution}}
    h = Harness(tmp_path, Provider("cron", args), [tool])
    h.start(ApprovalPolicy.AUTONOMOUS)
    try:
        await eventually(lambda: bool(h.approvals.pending))
        assert not scheduler.calls
        request_id = next(iter(h.approvals.pending))
        request = h.approvals.pending[request_id]
        assert request.arguments["schedule"]["resolve_dispatch"] == resolution
        if decision == "cancel":
            assert h.manager.cancel(h.task.id)
        else:
            assert h.approvals.resolve(request_id, decision == "allow")
        await h.joined()
        assert scheduler.calls == ([("synthetic-schedule", resolution)] if decision == "allow" else [])
        h.assert_terminal(TaskStatus.CANCELLED if decision == "cancel" else TaskStatus.COMPLETED)
    finally:
        if not h.worker.done():
            h.manager.cancel(h.task.id)
        await h.joined()


@pytest.mark.asyncio
async def test_normal_schedule_rename_remains_autonomous(tmp_path):
    from deepdesk.plugins.builtin.cron import CronTool
    from deepdesk.scheduler import Scheduler as RealScheduler

    class Scheduler(RealScheduler):
        calls = []

        def update(self, schedule_id, patch):
            self.calls.append((schedule_id, patch.name))
            return {"id": schedule_id, "name": patch.name}

    scheduler = Scheduler(tmp_path / "rename-schedule.db", lambda *args: "synthetic-only")
    args = {"action": "update", "id": "synthetic-schedule", "schedule": {"name": "Renamed"}}
    h = Harness(tmp_path, Provider("cron", args), [CronTool(scheduler)])
    h.start(ApprovalPolicy.AUTONOMOUS)
    await h.joined()
    assert scheduler.calls == [("synthetic-schedule", "Renamed")]
    assert not h.approvals.pending
    h.assert_terminal(TaskStatus.COMPLETED)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human", "approval"])
@pytest.mark.parametrize("ordering", ["cancel_only", "resolve_cancel", "cancel_resolve", "resolve_only"])
async def test_gate_cancel_complete_orderings(tmp_path, kind, ordering):
    tool = HumanActionTool() if kind == "human" else GateTool()
    args = {"summary": "Synthetic step", "instructions": "No real action"} if kind == "human" else {}
    h = Harness(tmp_path, Provider(tool.name, args), [tool])
    h.start(ApprovalPolicy.AUTONOMOUS if kind == "human" else ApprovalPolicy.CAUTIOUS)
    gate = h.humans if kind == "human" else h.approvals
    await eventually(lambda: bool(gate.pending))
    request = next(iter(gate.pending))
    if kind == "human": gate.take_over(request)
    resolve = lambda: gate.resolve(request, True)
    if ordering.startswith("resolve"):
        assert resolve()
    if ordering != "resolve_only":
        assert h.manager.cancel(h.task.id)
    if ordering == "cancel_resolve":
        assert not resolve()
    await h.joined()
    h.assert_terminal(TaskStatus.COMPLETED if ordering == "resolve_only" else TaskStatus.CANCELLED)
    if ordering != "resolve_only":
        assert h.provider.calls == 1
        if kind == "approval": assert tool.executions == 0


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Real Windows Job regression")
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_windows_child_cannot_write_after_stopped(tmp_path, cancel):
    h = Harness(tmp_path / "owned", Provider("shell"))
    runner = WindowsJobSandbox(h.workspace)
    if not runner.status()["available"]:
        pytest.skip("Windows Job dependencies are unavailable")
    h.registry.register(ShellTool(runner))
    child = Path(__file__).parent / "fixtures" / "cancellation_child.py"
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = lambda root: "& " + quote(sys.executable) + " " + quote(child) + " " + quote(root)
    h.provider.arguments = {"command": command(h.workspace), "timeout_seconds": 15}
    neighbor_root = tmp_path / "neighbor"
    neighbor_root.mkdir()
    neighbor = None
    if cancel:
        neighbor = asyncio.create_task(WindowsJobSandbox(neighbor_root).run(command(neighbor_root), timeout_seconds=15, max_processes=8))
        await eventually(lambda: (neighbor_root / "ready.json").exists())
    h.start()
    try:
        await eventually(lambda: (h.workspace / "ready.json").exists())
        pid = json.loads((h.workspace / "ready.json").read_text("utf-8"))["pid"]
        owned = psutil.Process(pid)
        if cancel:
            assert h.manager.cancel(h.task.id)
            await h.joined()
            h.assert_terminal(TaskStatus.CANCELLED)
            assert not owned.is_running()
            neighbor_pid = json.loads((neighbor_root / "ready.json").read_text("utf-8"))["pid"]
            assert psutil.Process(neighbor_pid).is_running()
        (h.workspace / "release").touch()
        if not cancel:
            await h.joined()
            h.assert_terminal(TaskStatus.COMPLETED)
        await asyncio.sleep(0.05)
        assert (h.workspace / "after-release.txt").exists() is (not cancel)
        assert not list(runner.temp_dir.glob("*.log"))
    finally:
        (h.workspace / "release").touch()
        if not h.worker.done(): h.manager.cancel(h.task.id)
        await h.joined()
        (neighbor_root / "release").touch()
        if neighbor is not None:
            await asyncio.wait_for(neighbor, 12)
            assert (neighbor_root / "after-release.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Real Windows suspended creation regression")
async def test_windows_cancel_during_creation_never_runs_unowned_child(tmp_path, monkeypatch):
    import deepdesk.windows_sandbox as windows

    runner = WindowsJobSandbox(tmp_path)
    if not runner.status()["available"]:
        pytest.skip("Windows Job dependencies are unavailable")
    created, release = threading.Event(), threading.Event()
    pids = []
    create_process = windows.win32process.CreateProcess

    def paused_create(*args, **kwargs):
        result = create_process(*args, **kwargs)
        pids.append(result[2])
        created.set()
        # Actual process is still CREATE_SUSPENDED; no command has executed.
        release.wait(10)
        return result

    monkeypatch.setattr(windows.win32process, "CreateProcess", paused_create)
    target = tmp_path / "must-not-exist.txt"
    command = "[IO.File]::WriteAllText('" + str(target).replace("'", "''") + "','unexpected')"
    task = asyncio.create_task(runner.run(command))
    try:
        await eventually(created.is_set)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 10)
        assert not any(psutil.pid_exists(pid) for pid in pids)
        assert not target.exists()
        assert not list(runner.temp_dir.glob("*.log"))
    finally:
        release.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_creation", [False, True])
async def test_posix_cancel_joins_creation_and_kills_only_owned_group(tmp_path, monkeypatch, during_creation):
    entered, release, communicating = asyncio.Event(), asyncio.Event(), asyncio.Event()
    killed = []

    class Process:
        pid, returncode = 424242, None
        async def communicate(self):
            communicating.set()
            if not killed: await asyncio.Event().wait()
            self.returncode = -9
            return b"", b""

    async def create(*_args, **_kwargs):
        entered.set()
        if during_creation: await release.wait()
        return Process()

    monkeypatch.setattr("deepdesk.posix_sandbox.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("deepdesk.posix_sandbox.os.killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    monkeypatch.setattr("deepdesk.posix_sandbox.signal.SIGKILL", 9, raising=False)
    task = asyncio.create_task(PosixResourceSandbox(tmp_path).run("synthetic"))
    await (entered if during_creation else communicating).wait()
    task.cancel()
    await asyncio.sleep(0)
    if during_creation:
        assert not task.done() and not killed
        task.cancel()
        release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(killed) == 1 and killed[0][0] == 424242


@pytest.mark.asyncio
async def test_windows_no_job_fails_closed_instead_of_unowned_shell(tmp_path, monkeypatch):
    from deepdesk.plugins.base import ToolContext
    class Unavailable:
        def status(self): return {"available": False}
    monkeypatch.setattr("deepdesk.plugins.builtin.shell.os.name", "nt")
    with pytest.raises(RuntimeError, match="required for safe command cancellation"):
        await ShellTool(Unavailable()).execute({"command": "Write-Output synthetic"}, ToolContext("qa", str(tmp_path)))
