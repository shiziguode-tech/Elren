"""Observe outer worker failures even when error persistence fails as well."""
import asyncio
import gc
import sqlite3

import pytest

from deepdesk.engine import ApprovalGate, TaskManager
from deepdesk.models import AgentProfile, ApprovalPolicy, TaskStatus
from deepdesk.task_store import TaskStore


class FailingEngine:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task, cancel):
        self.started.set()
        await self.release.wait()
        raise RuntimeError("Synthetic engine failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_double_failure_is_observed_without_asyncio_unhandled_error(tmp_path, caplog, cancelled):
    store = TaskStore(tmp_path / "tasks.db")
    engine = FailingEngine()
    manager = TaskManager(engine, ApprovalGate(), store=store)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context.get("message", "")))
    try:
        task = manager.create("Synthetic question", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
        await asyncio.wait_for(engine.started.wait(), 2)
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TRIGGER reject_task_update BEFORE UPDATE ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'Synthetic disk write rejection'); END")
        if cancelled:
            manager.cancel(task.id)
        else:
            engine.release.set()
        for _ in range(30):
            await asyncio.sleep(0)
            if task.id not in manager.background:
                break
        assert task.id not in manager.background
        assert task.id not in manager.cancel_events
        assert task.id not in manager.steering_queues
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        assert not unhandled, unhandled
        assert "persistence" in caplog.text.casefold()
        assert "IntegrityError" in caplog.text
        assert "Synthetic disk write rejection" not in caplog.text
        assert task.status == (TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED)
        # A failed write was NOT successful durable completion.
        assert store.get(task.id).status == TaskStatus.QUEUED
    finally:
        engine.release.set()
        await manager.shutdown()
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("redaction_mode", ["missing", "identity", "raises"])
async def test_setup_failure_never_logs_raw_credentials(tmp_path, caplog, redaction_mode):
    vendor_canary = "sk-proj-abcdefghijklmnop123456"
    opaque_canary = "opaque-synthetic-credential-7F3A91E6"

    class SetupFailureEngine:
        async def run(self, task, cancel):
            raise RuntimeError(f"Setup failed: {vendor_canary} {opaque_canary}")

    engine = SetupFailureEngine()
    if redaction_mode == "identity":
        engine._redact = lambda value: value
    elif redaction_mode == "raises":
        def broken_redactor(value):
            raise ValueError("Synthetic redaction failure")
        engine._redact = broken_redactor
    store = TaskStore(tmp_path / "tasks.db")
    manager = TaskManager(engine, ApprovalGate(), store=store)
    try:
        task = manager.create("Synthetic setup test", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
        await manager.background[task.id]
        await asyncio.sleep(0)
        assert task.status == TaskStatus.FAILED
        assert "RuntimeError" in task.error
        assert vendor_canary not in caplog.text
        assert vendor_canary not in task.error
        assert vendor_canary not in store.get(task.id).error
        if redaction_mode == "raises":
            # When the settings-backed redactor breaks, generic patterns cannot
            # recognize arbitrary provider secrets. Do not publish the raw text.
            assert opaque_canary not in caplog.text
            assert opaque_canary not in task.error
            assert opaque_canary not in store.get(task.id).error
    finally:
        await manager.shutdown()
