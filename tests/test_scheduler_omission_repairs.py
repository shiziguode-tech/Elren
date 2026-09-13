import socket
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from deepdesk.models import Risk
from deepdesk.plugins.builtin.cron import CronTool
from deepdesk.scheduler import DispatchNotStartedError, SchedulePatch, Scheduler, ScheduleRequest

NOW = datetime(2026, 9, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Scheduler regression must not use network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def make(tmp_path, *, kind="interval", end=None, dispatch=None):
    calls = []
    def fake(*args):
        calls.append(args)
        return f"local-task-{len(calls)}"
    scheduler = Scheduler(tmp_path / "synthetic.sqlite", dispatch or fake)
    schedule = scheduler.create(ScheduleRequest(
        name="QA schedule", prompt="No real work", kind=kind,
        expression="60" if kind == "interval" else "",
        start_at=(NOW + timedelta(minutes=1)).isoformat(), end_at=end,
    ), now=NOW)
    return scheduler, schedule, calls


@pytest.mark.parametrize("kind", ["interval", "at", "daily"])
def test_past_end_never_dispatches_on_recovery(tmp_path, kind):
    s, row, calls = make(tmp_path, kind=kind, end=(NOW + timedelta(minutes=2)).isoformat())
    assert s.run_due(NOW + timedelta(days=1)) == []
    assert not calls
    assert not s.get(row["id"])["enabled"]
    assert s.get(row["id"])["next_run"] is None


def test_exact_end_is_allowed(tmp_path):
    s, row, calls = make(tmp_path, end=(NOW + timedelta(minutes=2)).isoformat())
    assert len(s.run_due(NOW + timedelta(minutes=2))) == 1
    assert len(calls) == 1
    assert s.run_due(NOW + timedelta(minutes=2, microseconds=1)) == []
    assert s.get(row["id"])["next_run"] is None


@pytest.mark.parametrize("patch", [{}, {"name": "renamed"}, {"prompt": "edited"}, {"enabled": True}])
@pytest.mark.parametrize("kind", ["interval", "at"])
def test_metadata_patch_preserves_overdue_occurrence(tmp_path, patch, kind):
    s, row, _ = make(tmp_path, kind=kind)
    updated = s.update(row["id"], SchedulePatch(**patch), now=NOW + timedelta(minutes=2))
    assert updated["next_run"] == row["next_run"]
    assert updated["enabled"]


def test_optional_end_unset_null_and_replacement(tmp_path):
    end = (NOW + timedelta(minutes=2)).isoformat()
    s, row, calls = make(tmp_path, end=end)
    unchanged = s.update(row["id"], SchedulePatch(name="renamed"), now=NOW)
    assert unchanged["end_at"] == end
    cleared = s.update(row["id"], SchedulePatch(end_at=None), now=NOW)
    assert cleared["end_at"] is None
    assert cleared["next_run"] == row["next_run"]
    s.run_due(NOW + timedelta(days=1))
    assert len(calls) == 1
    replacement = (NOW + timedelta(days=3)).isoformat()
    assert s.update(row["id"], SchedulePatch(end_at=replacement), now=NOW)["end_at"] == replacement


def test_actual_interval_change_recalculates(tmp_path):
    s, row, _ = make(tmp_path)
    updated = s.update(row["id"], SchedulePatch(expression="120"), now=NOW + timedelta(minutes=2))
    assert updated["next_run"] == (NOW + timedelta(minutes=3)).isoformat()


@pytest.mark.parametrize("expression", ["NaN", "Infinity", "-Infinity", "0", "31536001", "not-a-number"])
@pytest.mark.parametrize("enabled", [True, False])
def test_interval_rejects_invalid_duration_even_with_future_anchor(tmp_path, expression, enabled):
    calls = []
    scheduler = Scheduler(tmp_path / "invalid.sqlite", lambda *args: calls.append(args) or "task")
    with pytest.raises(ValueError, match="interval"):
        scheduler.create(ScheduleRequest(name="invalid", prompt="never dispatch", kind="interval",
            expression=expression, enabled=enabled, start_at=(NOW + timedelta(days=1)).isoformat()), now=NOW)
    assert scheduler.list() == []
    assert calls == []


def test_invalid_interval_edit_preserves_old_schedule(tmp_path):
    scheduler, original, calls = make(tmp_path)
    with pytest.raises(ValueError, match="interval"):
        scheduler.update(original["id"], SchedulePatch(expression="NaN"), now=NOW)
    assert scheduler.get(original["id"]) == original
    assert len(scheduler.run_due(NOW + timedelta(minutes=1))) == 1
    assert len(calls) == 1


def test_daily_fold_never_repeats_in_same_tick(tmp_path):
    calls = []
    s = Scheduler(tmp_path / "fold.sqlite", lambda *args: calls.append(args) or "local-task")
    row = s.create(ScheduleRequest(name="fold", prompt="local", kind="daily",
        start_at="2026-10-31T01:30:00-04:00", timezone="America/New_York"),
        now=datetime(2026, 10, 30, tzinfo=UTC))
    now = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    assert len(s.run_due(now, limit=10)) == 1
    assert len(calls) == 1
    assert datetime.fromisoformat(s.get(row["id"])["next_run"]) > now


def test_expired_unstarted_claim_can_recover(tmp_path):
    s, row, calls = make(tmp_path, kind="at")
    due = NOW + timedelta(minutes=1)
    s._claim_next_due(due)
    restored = Scheduler(s.database, s.dispatch)
    assert restored.run_due(due + timedelta(seconds=299)) == []
    assert len(restored.run_due(due + timedelta(seconds=300))) == 1
    assert len(calls) == 1
    assert restored.get(row["id"])["dispatch_state"] == "idle"


def break_ack(s):
    with sqlite3.connect(s.database) as db:
        db.execute("CREATE TABLE fault_switch(enabled INTEGER)")
        db.execute("INSERT INTO fault_switch VALUES(1)")
        db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE OF last_task_id ON schedules "
                   "WHEN (SELECT enabled FROM fault_switch)=1 BEGIN "
                   "SELECT RAISE(ABORT, 'synthetic acknowledgement failure'); END")


@pytest.mark.parametrize("manual", [False, True])
def test_created_work_ack_failure_never_redispatches_after_restart(tmp_path, manual):
    s, row, calls = make(tmp_path, kind="at")
    if manual:
        s.update(row["id"], SchedulePatch(enabled=False), now=NOW)
    due = NOW + timedelta(minutes=1)
    break_ack(s)
    with pytest.raises(sqlite3.IntegrityError, match="acknowledgement"):
        s.run_now(row["id"], due) if manual else s.run_due(due)
    assert len(calls) == 1
    with sqlite3.connect(s.database) as db:
        db.execute("UPDATE fault_switch SET enabled=0")
        assert db.execute("SELECT dispatch_started_at FROM schedules").fetchone()[0]
    restored = Scheduler(s.database, s.dispatch)
    assert restored.run_due(due + timedelta(seconds=299)) == []
    later = due + timedelta(seconds=300)
    assert restored.run_due(later) == []
    paused = restored.get(row["id"])
    assert paused["dispatch_state"] == "uncertain"
    assert not paused["enabled"]
    assert len(calls) == 1
    renamed = restored.update(row["id"], SchedulePatch(name="rename retains fence"), now=later)
    assert renamed["dispatch_state"] == "uncertain"
    with pytest.raises(ValueError, match="resolve"):
        restored.update(row["id"], SchedulePatch(enabled=True), now=later)
    with pytest.raises(ValueError, match="resolve"):
        restored.run_now(row["id"], later)
    skipped = restored.update(row["id"], SchedulePatch(resolve_dispatch="skip"), now=later)
    assert skipped["dispatch_state"] == "idle"
    assert not skipped["enabled"]
    assert restored.run_due(later + timedelta(days=1)) == []
    assert len(calls) == 1


def test_callback_creates_then_throws_is_uncertain(tmp_path):
    created = []
    def ambiguous(*args):
        created.append("task exists")
        raise RuntimeError("lost acknowledgement after creation")
    s, row, _ = make(tmp_path, dispatch=ambiguous, kind="at")
    due = NOW + timedelta(minutes=1)
    result = s.run_due(due)[0]
    assert "uncertain" in result["error"]
    assert result["schedule"]["dispatch_state"] == "uncertain"
    assert s.run_due(due + timedelta(days=1)) == []
    assert len(created) == 1
    # Explicitly acknowledged retry is permitted, never inferred from enabled.
    s.update(row["id"], SchedulePatch(resolve_dispatch="retry"), now=due)
    s.dispatch = lambda *args: "manually-approved-local-retry"
    assert s.run_due(due)[0]["task_id"] == "manually-approved-local-retry"


def test_known_precreation_failure_has_bounded_retry(tmp_path):
    def unavailable(*args):
        raise DispatchNotStartedError("service unavailable before creating anything")
    s, row, _ = make(tmp_path, dispatch=unavailable, kind="at")
    due = NOW + timedelta(minutes=1)
    result = s.run_due(due)[0]
    assert result["schedule"]["dispatch_state"] == "idle"
    assert result["schedule"]["next_run"] == (due + timedelta(seconds=60)).isoformat()
    assert s.run_due(due + timedelta(seconds=59)) == []
    s.dispatch = lambda *args: "local-recovered"
    assert s.run_due(due + timedelta(seconds=60))[0]["task_id"] == "local-recovered"
    assert not s.get(row["id"])["enabled"]


def test_known_precreation_failure_does_not_retry_past_end(tmp_path):
    def unavailable(*args):
        raise DispatchNotStartedError("not started")
    s, row, _ = make(tmp_path, dispatch=unavailable, kind="at", end=(NOW + timedelta(seconds=90)).isoformat())
    s.run_due(NOW + timedelta(minutes=1))
    assert not s.get(row["id"])["enabled"]
    assert s.get(row["id"])["next_run"] is None


def test_edit_and_second_dispatch_during_callback_cannot_clear_fence(tmp_path):
    s, row, _ = make(tmp_path, kind="at")
    due = NOW + timedelta(minutes=1)
    second = Scheduler(s.database, s.dispatch)
    def dispatch(*args):
        updated = second.update(row["id"], SchedulePatch(name="during callback"), now=due)
        assert updated["dispatch_state"] == "dispatching"
        with pytest.raises(ValueError, match="in progress"):
            second.update(row["id"], SchedulePatch(resolve_dispatch="retry"), now=due)
        with pytest.raises(ValueError, match="resolve"):
            second.update(row["id"], SchedulePatch(enabled=False), now=due)
        assert second.run_due(due + timedelta(seconds=301)) == []
        return "single-local-task"
    s.dispatch = dispatch
    result = s.run_due(due)[0]
    assert result["task_id"] == "single-local-task"
    assert result["schedule"]["name"] == "during callback"
    assert result["schedule"]["dispatch_state"] == "idle"


def test_cron_clear_contract_and_resolution_risk(tmp_path):
    s, _, _ = make(tmp_path)
    tool = CronTool(s)
    schema = tool.parameters["properties"]["schedule"]["properties"]
    assert "null" in schema["end_at"]["type"]
    assert tool.risk({"action": "update", "schedule": {"resolve_dispatch": "retry"}}) == Risk.HIGH
    assert tool.risk({"action": "update", "schedule": {"name": "rename"}}) == Risk.MEDIUM


@pytest.mark.parametrize("resolution", ["skip", "retry"])
def test_manual_uncertain_resolution_preserves_future_automatic_plan(tmp_path, resolution):
    s, row, calls = make(tmp_path, kind="at")
    break_ack(s)
    with pytest.raises(sqlite3.IntegrityError):
        s.run_now(row["id"], NOW - timedelta(minutes=10))
    with sqlite3.connect(s.database) as db:
        db.execute("UPDATE fault_switch SET enabled=0")
    resolved = s.update(row["id"], SchedulePatch(resolve_dispatch=resolution), now=NOW)
    assert resolved["dispatch_state"] == "idle"
    assert resolved["enabled"]
    assert resolved["next_run"] == row["next_run"]
    assert len(calls) == (2 if resolution == "retry" else 1)
    assert s.run_due(NOW) == []
    s.run_due(NOW + timedelta(minutes=1))
    assert len(calls) == (3 if resolution == "retry" else 2)


def test_legacy_active_lease_migration_is_uncertain_not_assumed_unstarted(tmp_path):
    s, row, calls = make(tmp_path, kind="at")
    due = NOW + timedelta(minutes=1)
    s._claim_next_due(due)
    # Synthetic database only: emulate a pre-fence schema with a legacy lease.
    with sqlite3.connect(s.database) as db:
        db.execute("ALTER TABLE schedules DROP COLUMN dispatch_started_at")
        db.execute("ALTER TABLE schedules DROP COLUMN dispatch_preserve_schedule")
        db.execute("ALTER TABLE schedules DROP COLUMN dispatch_enabled_before")
    restored = Scheduler(s.database, s.dispatch)
    assert restored.get(row["id"])["dispatch_state"] == "uncertain"
    assert restored.run_due(due + timedelta(days=1)) == []
    assert not calls


def test_fence_write_failure_starts_no_work_and_unstarted_lease_can_recover(tmp_path):
    s, _, calls = make(tmp_path, kind="at")
    due = NOW + timedelta(minutes=1)
    with sqlite3.connect(s.database) as db:
        db.execute("CREATE TRIGGER fail_fence BEFORE UPDATE OF dispatch_started_at ON schedules "
                   "WHEN NEW.dispatch_started_at IS NOT NULL BEGIN "
                   "SELECT RAISE(ABORT, 'synthetic fence write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fence write"):
        s.run_due(due)
    assert not calls
    with sqlite3.connect(s.database) as db:
        assert db.execute("SELECT dispatch_started_at FROM schedules").fetchone() == (None,)
        db.execute("DROP TRIGGER fail_fence")
    assert len(s.run_due(due + timedelta(seconds=300))) == 1
    assert len(calls) == 1


def test_created_task_is_durable_when_scheduler_acknowledgement_fails(tmp_path):
    s, row, _ = make(tmp_path, kind="at")
    with sqlite3.connect(s.database) as db:
        db.execute("CREATE TABLE synthetic_tasks(id INTEGER PRIMARY KEY, status TEXT)")
    def create_local_record(*args):
        with sqlite3.connect(s.database) as db:
            cursor = db.execute("INSERT INTO synthetic_tasks(status) VALUES('completed')")
            return str(cursor.lastrowid)
    s.dispatch = create_local_record
    break_ack(s)
    due = NOW + timedelta(minutes=1)
    with pytest.raises(sqlite3.IntegrityError):
        s.run_due(due)
    with sqlite3.connect(s.database) as db:
        db.execute("UPDATE fault_switch SET enabled=0")
        assert db.execute("SELECT COUNT(*) FROM synthetic_tasks WHERE status='completed'").fetchone() == (1,)
    restored = Scheduler(s.database, create_local_record)
    assert restored.run_due(due + timedelta(seconds=300)) == []
    with sqlite3.connect(s.database) as db:
        assert db.execute("SELECT COUNT(*) FROM synthetic_tasks").fetchone() == (1,)
    assert restored.get(row["id"])["dispatch_state"] == "uncertain"
