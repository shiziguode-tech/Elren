from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deepdesk.mcp_runtime import MCPRuntime
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.cron import CronTool
from deepdesk.scheduler import CronExpression, DispatchNotStartedError, Scheduler, ScheduleRequest
from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.windows_sandbox import WindowsJobSandbox

_SCHEDULER_TEST_CREDENTIAL = "scheduler-redaction-canary-91D3F6B8"


def test_daily_schedule_preserves_local_wall_clock_across_dst() -> None:
    start = datetime.fromisoformat("2026-10-31T09:00:00-06:00")
    after_first_run = datetime.fromisoformat("2026-10-31T15:00:00+00:00")

    due = Scheduler.next_run(
        "daily",
        "86400",
        after_first_run,
        start_at=start.isoformat(),
        timezone="America/Denver",
    )

    assert due == datetime.fromisoformat("2026-11-01T16:00:00+00:00")


def test_mcp_child_environment_filter_is_credential_safe(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "safe-runtime-path")
    runtime = MCPRuntime(Path.cwd())

    environment = credential_safe_environment()
    assert runtime.workspace == Path.cwd().resolve()
    assert "ANTHROPIC_API_KEY" not in environment
    path_parts = environment["PATH"].split(os.pathsep)
    assert path_parts[-1] == "safe-runtime-path"
    if len(path_parts) > 1:
        assert path_parts[0].endswith("work\\tool-runtime\\bin")


def test_macos_mcp_uses_signed_bundle_but_keeps_writable_workspace(tmp_path, monkeypatch):
    from deepdesk import mcp_runtime

    package = tmp_path / "Elren.app" / "Contents" / "Resources" / "bundle"
    native = package / "work" / "tool-runtime" / "native" / "node"
    (native / "bin").mkdir(parents=True)
    (native / "bin" / "node").write_text("synthetic executable", encoding="utf-8")
    workspace = tmp_path / "Application Support" / "Elren"
    monkeypatch.setattr(mcp_runtime, "packaged_workspace_root", lambda _: package, raising=False)
    runtime = MCPRuntime(workspace)
    assert runtime.node == str(native / "bin" / "node")
    assert runtime.server == native / "mcp-servers" / "elren-workspace.mjs"
    assert runtime.workspace == workspace.resolve()


def test_macos_builder_copies_bundled_mcp_server_next_to_node_modules():
    source = (Path(__file__).parents[1] / "macos" / "build-macos-app.sh").read_text("utf-8")
    assert '"$ROOT/work/openclaw-runtime/mcp-servers/elren-workspace.mjs"' in source
    assert '"$NODE_ROOT/mcp-servers/"' in source


def test_macos_source_archive_includes_only_reviewed_mcp_source(tmp_path):
    from launcher.build_macos_source_archive import iter_macos_source_paths

    (tmp_path / "deepdesk").mkdir()
    (tmp_path / "deepdesk/mcp_runtime.py").write_text("# marker", encoding="utf-8")
    relative = Path("work/openclaw-runtime/mcp-servers/elren-workspace.mjs")
    server = tmp_path / relative
    server.parent.mkdir(parents=True)
    server.write_text("// marker", encoding="utf-8")
    (server.parent / "private-qa.txt").write_text("must not package", encoding="utf-8")
    paths = {str(item) for _, item in iter_macos_source_paths(tmp_path)}
    assert str(relative) in paths
    assert not any("private-qa" in item for item in paths)


@pytest.mark.asyncio
async def test_real_mcp_initialize_list_and_call():
    runtime = MCPRuntime(Path.cwd())
    if not runtime.status:
        pytest.skip("MCP runtime unavailable")
    status = await runtime.status(probe=True)
    if not status["configured"]:
        pytest.skip("Bundled Node MCP dependencies are not installed")
    assert status["ready"]
    assert status["protocol"] == "2025-06-18"
    assert {"runtime_info", "list_artifacts", "read_artifact", "write_artifact"} <= set(
        status["tools"]
    )
    result = await runtime.call_tool("runtime_info", {})
    assert result["content"][0]["type"] == "text"


@pytest.mark.asyncio
async def test_mcp_failed_probe_stays_not_ready_until_a_real_probe_recovers(
    tmp_path: Path, monkeypatch
):
    runtime = MCPRuntime(tmp_path)
    runtime.node = "node"
    runtime.server.parent.mkdir(parents=True)
    runtime.server.write_text("// test server", encoding="utf-8")

    async def fail(_method, _params):
        raise RuntimeError("handshake failed")

    monkeypatch.setattr(runtime, "request", fail)
    failed = await runtime.status(probe=True)
    cheap_poll = await runtime.status(probe=False)

    assert failed["configured"] is True
    assert failed["ready"] is False
    assert cheap_poll["ready"] is False
    assert "handshake failed" in cheap_poll["last_error"]

    async def recover(_method, _params):
        return {"tools": [{"name": "runtime_info"}]}

    monkeypatch.setattr(runtime, "request", recover)
    recovered = await runtime.status(probe=True)
    assert recovered["ready"] is True
    assert recovered["last_error"] == ""


@pytest.mark.asyncio
async def test_mcp_normalizes_optional_outputs_prefix(tmp_path):
    runtime = MCPRuntime(Path.cwd())
    # Keep the installed server/dependencies, but route every artifact into a
    # unique test workspace. Never unlink a fixed filename in user outputs.
    runtime.workspace = tmp_path
    status = await runtime.status(probe=True)
    if not status["configured"]:
        pytest.skip("Bundled Node MCP dependencies are not installed")
    target = tmp_path / "outputs" / "mcp-prefix-regression.txt"
    nested = tmp_path / "outputs" / "outputs" / "mcp-prefix-regression.txt"
    result = await runtime.call_tool(
        "write_artifact",
        {"path": "outputs/mcp-prefix-regression.txt", "content": "normalized"},
    )
    payload = result["content"][0]["text"]
    assert '"path":"mcp-prefix-regression.txt"' in payload
    assert target.read_text("utf-8") == "normalized"
    assert not nested.exists()


@pytest.mark.asyncio
async def test_mcp_write_artifact_replaces_hardlink_without_writing_through():
    runtime = MCPRuntime(Path.cwd())
    status = await runtime.status(probe=True)
    if not status["configured"]:
        pytest.skip("Bundled Node MCP dependencies are not installed")
    # A pytest temp root may be on another drive. Keep the protected source
    # outside the workspace but on its volume; hardlinks cannot cross volumes.
    # Both paths are unique test-owned files, never fixed user output names.
    outputs = Path.cwd() / "outputs"
    outputs.mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="elren-mcp-protected-", dir=Path.cwd().parent) as source_dir,
        tempfile.TemporaryDirectory(prefix="mcp-hardlink-", dir=outputs) as target_dir,
    ):
        source = Path(source_dir) / "protected-control-file.md"
        source.write_text("trusted", encoding="utf-8")
        target = Path(target_dir) / "artifact.txt"
        os.link(source, target)
        assert os.path.samefile(source, target)
        result = await runtime.call_tool(
            "write_artifact",
            {"path": target.relative_to(outputs).as_posix(), "content": "artifact"},
        )
        assert result["content"][0]["type"] == "text"
        assert source.read_text("utf-8") == "trusted"
        assert target.read_text("utf-8") == "artifact"
        assert not os.path.samefile(source, target)


def test_cron_year_and_restart_regression(tmp_path: Path):
    calls: list[str] = []
    database = tmp_path / "scheduler.sqlite"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler = Scheduler(
        database, lambda prompt, policy, profile: calls.append(prompt) or f"task-{len(calls)}"
    )
    schedule = scheduler.create(
        ScheduleRequest(
            name="long-run",
            prompt="tick",
            kind="cron",
            expression="*/5 * * * *",
        ),
        now=start,
    )
    for day in range(1, 366):
        scheduler.run_due(start + timedelta(days=day))

    restored = Scheduler(database, lambda prompt, policy, profile: "restored-task")
    persisted = restored.get(schedule["id"])
    assert persisted is not None
    assert persisted["run_count"] == 365
    assert persisted["next_run"] > (start + timedelta(days=365)).isoformat()
    assert len(calls) == 365


def test_scheduler_skips_compaction_without_redaction_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "no-redaction-compaction.sqlite"

    def unexpected_compaction(_connection) -> None:
        raise AssertionError("ordinary scheduler startup must not compact SQLite")

    monkeypatch.setattr(
        Scheduler,
        "_compact_redacted_pages",
        staticmethod(unexpected_compaction),
    )
    scheduler = Scheduler(database, lambda *_args: "task")
    scheduler.create(
        ScheduleRequest(
            name="ordinary schedule",
            prompt="prepare a local report",
            kind="interval",
            expression="60",
        )
    )

    restored = Scheduler(database, lambda *_args: "restored-task")

    assert restored.stats() == {"total": 1, "enabled": 1, "runs": 0}


def test_scheduler_recovers_interrupted_redaction_compaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "pending-redaction-compaction.sqlite"
    legacy = Scheduler(database, lambda *_args: "legacy-task")
    created = legacy.create(
        ScheduleRequest(
            name="legacy schedule",
            prompt=f"use {_SCHEDULER_TEST_CREDENTIAL}",
            kind="interval",
            expression="60",
        )
    )
    original_compact = Scheduler._compact_redacted_pages

    def interrupt_before_compaction(_connection) -> None:
        raise RuntimeError("simulated interruption before scheduler VACUUM")

    monkeypatch.setattr(
        Scheduler,
        "_compact_redacted_pages",
        staticmethod(interrupt_before_compaction),
    )
    with pytest.raises(RuntimeError, match="before scheduler VACUUM"):
        Scheduler(
            database,
            lambda *_args: "migrated-task",
            secret_values=lambda: [_SCHEDULER_TEST_CREDENTIAL],
        )

    with sqlite3.connect(database) as connection:
        pending = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("schedule-secret-redaction",),
        ).fetchone()
        prompt = connection.execute(
            "SELECT prompt FROM schedules WHERE id = ?", (created["id"],)
        ).fetchone()
    assert pending == ("physical-scrub-v1:pending",)
    assert prompt is not None and _SCHEDULER_TEST_CREDENTIAL not in prompt[0]

    compactions = 0

    def count_compaction(connection) -> None:
        nonlocal compactions
        compactions += 1
        original_compact(connection)

    monkeypatch.setattr(
        Scheduler,
        "_compact_redacted_pages",
        staticmethod(count_compaction),
    )
    recovered = Scheduler(
        database,
        lambda *_args: "recovered-task",
        secret_values=lambda: [_SCHEDULER_TEST_CREDENTIAL],
    )
    assert compactions == 1
    assert "[REDACTED]" in recovered.get(created["id"])["prompt"]
    with sqlite3.connect(database) as connection:
        completed = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("schedule-secret-redaction",),
        ).fetchone()
    assert completed == ("physical-scrub-v1:completed",)

    Scheduler(
        database,
        lambda *_args: "steady-state-task",
        secret_values=lambda: [_SCHEDULER_TEST_CREDENTIAL],
    )
    assert compactions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "delete", "run_now"])
async def test_cron_tool_reports_missing_schedule_as_failure(
    tmp_path: Path, action: str
) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.sqlite", lambda *_a: "task")
    tool = CronTool(scheduler)
    arguments: dict[str, object] = {"action": action, "id": "missing-schedule"}
    if action == "update":
        arguments["schedule"] = {"name": "still missing"}

    with pytest.raises(KeyError, match="missing-schedule"):
        await tool.execute(
            arguments,
            ToolContext(task_id="test", workspace=str(tmp_path)),
        )


def test_cron_standard_day_or_weekday_semantics():
    cron = CronExpression("0 9 1 * 1")
    # Standard cron treats day-of-month and weekday as OR when both are restricted.
    assert cron.next_after(datetime(2026, 1, 2, tzinfo=UTC)) == datetime(
        2026, 1, 5, 9, tzinfo=UTC
    )


def test_one_time_schedule_accepts_start_at_without_duplicate_expression(tmp_path: Path):
    scheduler = Scheduler(tmp_path / "at.sqlite", lambda *_: "task")
    schedule = scheduler.create(
        ScheduleRequest(
            name="tomorrow",
            prompt="prepare plan",
            kind="at",
            start_at="2026-08-12T09:00:00-06:00",
        ),
        now=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    assert schedule["expression"] == "2026-08-12T09:00:00-06:00"
    assert schedule["next_run"] == "2026-08-12T15:00:00+00:00"


def test_failed_one_time_dispatch_is_retained_for_bounded_retry(tmp_path: Path):
    def unavailable(*_args):
        # Only the dispatcher can assert no task or side effect was created.
        # An arbitrary exception is ambiguous and must not trigger a retry.
        raise DispatchNotStartedError("task service is restarting")

    scheduler = Scheduler(tmp_path / "at-retry.sqlite", unavailable)
    due = datetime(2026, 8, 12, 15, tzinfo=UTC)
    schedule = scheduler.create(
        ScheduleRequest(
            name="one shot",
            prompt="run once",
            kind="at",
            start_at=due.isoformat(),
        ),
        now=due - timedelta(minutes=1),
    )

    result = scheduler.run_due(due)[0]
    updated = scheduler.get(schedule["id"])

    assert result["error"] == "DispatchNotStartedError: task service is restarting"
    assert updated is not None
    assert updated["enabled"] is True
    assert updated["next_run"] == (due + timedelta(seconds=60)).isoformat()
    assert updated["run_count"] == 1


def test_interval_coalesces_years_of_missed_runs_in_one_dispatch(tmp_path: Path):
    calls: list[str] = []
    scheduler = Scheduler(
        tmp_path / "interval.sqlite",
        lambda prompt, policy, profile: calls.append(prompt) or "task-once",
    )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        ScheduleRequest(name="fast", prompt="tick", kind="interval", expression="1"),
        now=start,
    )
    scheduler.run_due(datetime(2030, 1, 1, tzinfo=UTC))
    updated = scheduler.get(schedule["id"])
    assert len(calls) == 1
    assert updated is not None
    assert updated["next_run"] > datetime(2030, 1, 1, tzinfo=UTC).isoformat()


def test_two_scheduler_instances_atomically_claim_one_due_run(tmp_path: Path):
    database = tmp_path / "shared-scheduler.sqlite"
    due = datetime(2026, 8, 12, 15, tzinfo=UTC)
    start_gate = threading.Barrier(3)
    dispatch_entered = threading.Event()
    release_dispatch = threading.Event()
    loser_finished = threading.Event()
    result_lock = threading.Lock()
    dispatch_count = 0
    results: list[list[dict]] = []
    errors: list[BaseException] = []

    def dispatch(*_args):
        nonlocal dispatch_count
        with result_lock:
            dispatch_count += 1
            task_id = f"task-{dispatch_count}"
        dispatch_entered.set()
        release_dispatch.wait(10)
        return task_id

    first = Scheduler(database, dispatch)
    schedule = first.create(
        ScheduleRequest(
            name="one shared tick",
            prompt="dispatch once",
            kind="at",
            start_at=due.isoformat(),
        ),
        now=due - timedelta(minutes=1),
    )
    second = Scheduler(database, dispatch)

    def run(scheduler: Scheduler) -> None:
        try:
            start_gate.wait(5)
            outcome = scheduler.run_due(due)
            with result_lock:
                results.append(outcome)
            if not outcome:
                loser_finished.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    workers = [
        threading.Thread(target=run, args=(first,)),
        threading.Thread(target=run, args=(second,)),
    ]
    for worker in workers:
        worker.start()
    start_gate.wait(5)
    dispatch_started = dispatch_entered.wait(5)
    loser_returned_while_winner_was_blocked = loser_finished.wait(5)
    release_dispatch.set()
    for worker in workers:
        worker.join(5)

    assert dispatch_started
    assert loser_returned_while_winner_was_blocked
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert dispatch_count == 1
    assert sorted(len(outcome) for outcome in results) == [0, 1]
    persisted = first.get(schedule["id"])
    assert persisted is not None
    assert persisted["run_count"] == 1
    assert persisted["last_task_id"] == "task-1"


def test_two_scheduler_instances_can_initialize_a_new_database_concurrently(
    tmp_path: Path,
):
    database = tmp_path / "concurrent-first-start.sqlite"
    start_gate = threading.Barrier(3)
    result_lock = threading.Lock()
    instances: list[Scheduler] = []
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            start_gate.wait(5)
            scheduler = Scheduler(database, lambda *_args: "task")
            with result_lock:
                instances.append(scheduler)
        except BaseException as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    workers = [threading.Thread(target=initialize) for _ in range(2)]
    for worker in workers:
        worker.start()
    start_gate.wait(5)
    for worker in workers:
        worker.join(10)

    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert len(instances) == 2
    assert instances[0].stats() == {"total": 0, "enabled": 0, "runs": 0}
    assert instances[1].stats() == {"total": 0, "enabled": 0, "runs": 0}


def test_expired_scheduler_claim_is_recovered_after_crashed_instance(tmp_path: Path):
    database = tmp_path / "scheduler-lease-recovery.sqlite"
    due = datetime(2026, 8, 12, 15, tzinfo=UTC)
    dispatched: list[str] = []

    def dispatch(prompt, *_args):
        dispatched.append(prompt)
        return "recovered-task"

    crashed = Scheduler(database, dispatch)
    schedule = crashed.create(
        ScheduleRequest(
            name="recover abandoned claim",
            prompt="lease recovery",
            kind="at",
            start_at=due.isoformat(),
        ),
        now=due - timedelta(minutes=1),
    )
    claimed = crashed._claim_next_due(due)
    assert claimed is not None
    assert claimed[0]["id"] == schedule["id"]
    assert "claim_token" not in claimed[0]
    assert "claim_until" not in claimed[0]

    recovered = Scheduler(database, dispatch)
    assert recovered.run_due(due + timedelta(seconds=299)) == []
    assert dispatched == []

    result = recovered.run_due(due + timedelta(seconds=300))
    assert len(result) == 1
    assert result[0]["task_id"] == "recovered-task"
    assert dispatched == ["lease recovery"]
    persisted = recovered.get(schedule["id"])
    assert persisted is not None
    assert persisted["enabled"] is False
    assert persisted["next_run"] is None
    assert persisted["run_count"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are Windows-only")
async def test_windows_job_sandbox_success_and_timeout(tmp_path: Path):
    sandbox = WindowsJobSandbox(tmp_path)
    assert sandbox.status()["os_enforced"]
    success = await sandbox.run("Write-Output 'SANDBOX_OK'", timeout_seconds=5)
    assert success["exit_code"] == 0
    assert "SANDBOX_OK" in success["stdout"]
    timeout = await sandbox.run("Start-Sleep -Seconds 5", timeout_seconds=1)
    assert timeout["timed_out"]
    assert timeout["exit_code"] == 1460
    child = await sandbox.run(
        "$ErrorActionPreference='Stop'; Start-Process powershell.exe; Write-Output 'CHILD_STARTED'",
        timeout_seconds=5,
        max_processes=1,
    )
    assert child["exit_code"] != 0
    assert "CHILD_STARTED" not in child["stdout"]


def test_windows_sandbox_locked_log_cleanup_never_overwrites_result(monkeypatch):
    class LockedLog:
        calls = 0

        def unlink(self, *, missing_ok=False):
            self.calls += 1
            raise PermissionError(32, "still inherited by a just-exited child")

    log = LockedLog()
    monkeypatch.setattr("deepdesk.windows_sandbox.time.sleep", lambda _seconds: None)

    WindowsJobSandbox._unlink_output(log)

    assert log.calls == 5
