"""Real SQLite leases and async worker/dispatch ownership, no external tasks."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_loopback_web_security import _isolated_app

from deepdesk.models import TaskStatus
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.cron import CronTool
from deepdesk.scheduler import Scheduler, ScheduleRequest

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def make(tmp_path, dispatch):
    scheduler = Scheduler(tmp_path / "schedules.db", dispatch)
    row = scheduler.create(ScheduleRequest(name="Synthetic", prompt="No external work",
        kind="at", start_at=(NOW + timedelta(seconds=1)).isoformat()), now=NOW)
    return scheduler, row


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_async_dispatch_is_awaited_before_acknowledgement_even_on_stop(tmp_path, cancelled):
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    calls = []

    async def dispatch(*args):
        assert asyncio.get_running_loop() is loop and threading.get_ident() == owner
        entered.set()
        await release.wait()
        calls.append(args)
        return "committed-async-task"

    scheduler, row = make(tmp_path, dispatch)
    pending = asyncio.create_task(scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stored = scheduler.get(row["id"])
        assert stored["run_count"] == 0 and not stored["last_task_id"]
        if cancelled:
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done(), "Stop must join creation and its acknowledgement"
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            result = await pending
            assert result[0]["task_id"] == "committed-async-task"
        stored = scheduler.get(row["id"])
        assert stored["last_task_id"] == "committed-async-task" and stored["run_count"] == 1
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_dispatch_failure_does_not_record_coroutine_as_task_id(tmp_path):
    async def dispatch(*args):
        await asyncio.sleep(0)
        raise OSError("synthetic task persistence failure")

    scheduler, row = make(tmp_path, dispatch)
    result = await scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2))
    assert result[0]["task_id"] is None
    stored = scheduler.get(row["id"])
    assert not stored["last_task_id"] and stored["run_count"] == 0
    assert not stored["enabled"]  # uncertain dispatch remains duplicate-fenced


@pytest.mark.asyncio
async def test_concurrent_async_ticks_dispatch_once_on_owner_event_loop(tmp_path):
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    calls = []
    children = []
    def dispatch(*args):
        assert threading.get_ident() == owner
        assert asyncio.get_running_loop() is loop
        calls.append(args)
        children.append(asyncio.create_task(asyncio.sleep(0)))
        return "synthetic-created-task"
    scheduler, row = make(tmp_path, dispatch)
    results = await asyncio.gather(*(scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2))
                                     for _ in range(4)))
    await asyncio.gather(*children)
    assert len(calls) == 1
    assert sum(len(items) for items in results) == 1
    assert scheduler.get(row["id"])["last_task_id"] == "synthetic-created-task"
    assert scheduler.get(row["id"])["run_count"] == 1


@pytest.mark.asyncio
async def test_cancelled_tick_waits_for_owned_claim_then_releases_without_dispatch(tmp_path, monkeypatch):
    calls = []
    scheduler, row = make(tmp_path, lambda *args: calls.append(args) or "synthetic-task")
    entered, release = threading.Event(), threading.Event()
    real_claim = scheduler._claim_next_due
    def delayed_claim(now):
        result = real_claim(now)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(scheduler, "_claim_next_due", delayed_claim)
    worker = asyncio.create_task(scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2)))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        worker.cancel()
        await asyncio.sleep(0.02)
        assert not worker.done(), "Stop abandoned a worker that still owns a SQLite claim"
        assert not calls
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
    stored = scheduler.get(row["id"])
    with sqlite3.connect(scheduler.database) as db:
        assert db.execute("SELECT claim_token FROM schedules WHERE id=?", (row["id"],)).fetchone()[0] is None
    assert stored["dispatch_state"] == "idle"
    assert stored["run_count"] == 0
    monkeypatch.setattr(scheduler, "_claim_next_due", real_claim)
    result = await scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2))
    assert result[0]["task_id"] == "synthetic-task"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_async_ack_failure_keeps_cross_restart_duplicate_fence(tmp_path):
    calls = []
    scheduler, row = make(tmp_path, lambda *args: calls.append(args) or "synthetic-task")
    with sqlite3.connect(scheduler.database) as db:
        db.execute("CREATE TRIGGER reject_ack BEFORE UPDATE OF last_task_id ON schedules "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic ack failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2))
    with sqlite3.connect(scheduler.database) as db:
        db.execute("DROP TRIGGER reject_ack")
    restored = Scheduler(scheduler.database, scheduler.dispatch)
    assert await restored.run_async(restored.run_due, NOW + timedelta(minutes=10)) == []
    assert len(calls) == 1
    assert not restored.get(row["id"])["enabled"]


@pytest.mark.asyncio
async def test_scheduler_stop_joins_inflight_tick_without_dispatching_after_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("deepdesk.scheduler._utc_now", lambda: NOW + timedelta(seconds=2))
    calls = []
    scheduler, row = make(tmp_path, lambda *args: calls.append(args) or "synthetic-task")
    entered, release = threading.Event(), threading.Event()
    real_claim = scheduler._claim_next_due
    def delayed_claim(now):
        result = real_claim(now)
        entered.set()
        assert release.wait(3)
        return result
    monkeypatch.setattr(scheduler, "_claim_next_due", delayed_claim)
    await scheduler.start()
    stopper = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stopper = asyncio.create_task(scheduler.stop())
        await asyncio.sleep(0.02)
        assert not stopper.done()
        assert not calls
    finally:
        release.set()
        if stopper is not None:
            await asyncio.wait_for(stopper, 3)
        else:
            await scheduler.stop()
    assert scheduler._runner is None
    with sqlite3.connect(scheduler.database) as db:
        assert db.execute("SELECT claim_token FROM schedules WHERE id=?", (row["id"],)).fetchone()[0] is None
    assert not calls


@pytest.mark.asyncio
async def test_http_schedule_write_keeps_identity_responsive_while_database_locked(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path)
    scheduler = app.state.scheduler
    entered = threading.Event()
    original = scheduler.create
    def observed(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(scheduler, "create", observed)
    lock = sqlite3.connect(scheduler.database)
    lock.execute("BEGIN IMMEDIATE")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        request = asyncio.create_task(client.post("/api/schedules", json={
            "name": "Synthetic locked create", "prompt": "No work", "kind": "interval", "expression": "60",
        }))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not request.done()
            identity = await asyncio.wait_for(client.get("/api/desktop/identity"), 2)
            assert identity.status_code == 200
            assert not request.done(), "Background storage should still be waiting on our write lock"
        finally:
            lock.rollback()
            lock.close()
            result = await asyncio.wait_for(request, 3)
        assert result.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["http", "tool", "background"])
async def test_schedule_entrypoints_create_real_manager_worker_on_owner_loop(tmp_path, monkeypatch, entry):
    app = _isolated_app(tmp_path)
    scheduler = app.state.scheduler
    manager = app.state.manager
    manager.on_task_created = None
    loop = asyncio.get_running_loop()
    owner = threading.get_ident()
    async def synthetic_run(task, cancel, steering):
        assert asyncio.get_running_loop() is loop and threading.get_ident() == owner
        task.status = TaskStatus.COMPLETED
        task.result = "Synthetic schedule answer"
        manager.emit(task, "assistant", {"content": task.result})
    monkeypatch.setattr(manager.engine, "run", synthetic_run)
    row = scheduler.create(ScheduleRequest(name="Synthetic", prompt="No external work", kind="at",
        start_at=(NOW + timedelta(seconds=1)).isoformat()), now=NOW)
    if entry == "http":
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.post(f"/api/schedules/{row['id']}/run")
            assert response.status_code == 200
            result = response.json()
    elif entry == "tool":
        result = await CronTool(scheduler).execute({"action": "run_now", "id": row["id"]},
            ToolContext(task_id="synthetic-owner", workspace=str(tmp_path)))
    else:
        result = (await scheduler.run_async(scheduler.run_due, NOW + timedelta(seconds=2)))[0]
    assert result["task_id"], result
    workers = list(manager.background.values())
    if workers:
        await asyncio.wait_for(asyncio.gather(*workers), 3)
    assert len(manager.tasks) == 1
    task = manager.store.get(result["task_id"])
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "Synthetic schedule answer"
