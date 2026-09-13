"""Cancellation must not make an accepted remote event eligible for duplication."""
import asyncio
import sqlite3
import threading

import httpx
import pytest
from test_feishu import _install_completed_task_factory, _isolated_feishu_app, _raw_feishu_event

from deepdesk.feishu_pending import PendingFeishuAttachments
from deepdesk.models import TaskStatus
from deepdesk.telegram import TelegramBridge


@pytest.mark.asyncio
async def test_cancel_during_ack_does_not_rollback_already_created_task(tmp_path):
    app = _isolated_feishu_app(tmp_path)
    calls = _install_completed_task_factory(app)
    entered, release = asyncio.Event(), asyncio.Event()

    async def acknowledge(*args, **kwargs):
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return {"ok": True}

    app.state.feishu_bridge.send_text = acknowledge
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        event = _raw_feishu_event("synthetic-cancel-after-creation")
        pending = asyncio.create_task(client.post("/api/feishu/events", json=event))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert len(calls) == 1 and len(app.state.manager.tasks) == 1
            pending.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            retry = await client.post("/api/feishu/events", json=event)
            assert len(calls) == 1, "Retry created another task after the first was already accepted"
            assert retry.json().get("reason") == "duplicate_event"
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await app.state.manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_telegram_stop_owns_callback_and_offset_ack(tmp_path, failure, caplog):
    bridge = TelegramBridge("synthetic-token", "123")
    entered, release = asyncio.Event(), asyncio.Event()
    created = []
    canary = "opaque-synthetic-callback-error"
    updates = [
        {"update_id": index, "message": {"message_id": index, "chat": {"id": 123}, "text": "Synthetic"}}
        for index in (41, 42)
    ]

    async def api(method, **kwargs):
        assert method == "getUpdates"
        return updates

    async def callback(event):
        entered.set()
        await release.wait()
        if failure:
            raise OSError(canary)
        created.append(event["update_id"])
        return {"ok": True, "ack_update": True}

    bridge._api = api
    bridge._callback = callback
    pending = asyncio.create_task(bridge._poll_loop())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        bridge._stop.set()
        for _ in range(3):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done(), "Stopping detached callback from update acknowledgement"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert bridge._offset == (0 if failure else 42)
        assert created == ([] if failure else ["41"]), "Do not start another update after Stop"
        assert canary not in caplog.text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_creation", [False, True])
async def test_feishu_cancel_during_real_sqlite_creation_preserves_retry_contract(tmp_path, monkeypatch, reject_creation):
    app = _isolated_feishu_app(tmp_path)
    manager = app.state.manager
    entered = threading.Event()
    original = manager.store.save_prepared

    async def synthetic_engine(task, cancel, queue):
        task.status = TaskStatus.COMPLETED
        task.result = "Synthetic local completion"
        await manager.emit_async(task, "assistant", {"content": task.result})

    manager.engine.run = synthetic_engine

    def write(prepared):
        entered.set()
        return original(prepared)

    monkeypatch.setattr(manager.store, "save_prepared", write)
    if reject_creation:
        with sqlite3.connect(manager.store.path) as db:
            db.execute("CREATE TRIGGER reject_task_creation BEFORE INSERT ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic creation failure'); END")
    holder = sqlite3.connect(manager.store.path)
    holder.execute("BEGIN IMMEDIATE")
    event = _raw_feishu_event("synthetic-sqlite-cancel")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pending = asyncio.create_task(client.post("/api/feishu/events", json=event))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not manager.tasks
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done()
            holder.rollback()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
            assert len(manager.tasks) == (0 if reject_creation else 1)
            if reject_creation:
                with sqlite3.connect(manager.store.path) as db:
                    db.execute("DROP TRIGGER reject_task_creation")
            retry = await client.post("/api/feishu/events", json=event)
            assert retry.json()["ok"] is True
            assert bool(retry.json().get("ignored")) is not reject_creation
            assert len(manager.tasks) == 1
            await asyncio.gather(*list(manager.background.values()))
            stored, total = manager.store.list_page()
            assert total == 1 and stored[0].result == "Synthetic local completion"
        finally:
            holder.close()
            await asyncio.gather(pending, return_exceptions=True)
            await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_pending_close_joins_expired_callback_even_when_cancelled(failure, caplog):
    pending = PendingFeishuAttachments(delay_seconds=0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    committed = []
    canary = "opaque-synthetic-attachment-error"

    async def flush(entry):
        entered.set()
        await release.wait()
        if failure:
            raise OSError(canary)
        committed.extend(entry["attachments"])

    await pending.stage("synthetic-owner", ["synthetic.txt"], {}, flush)
    timer = pending._pending["synthetic-owner"]["timer"]
    await asyncio.wait_for(entered.wait(), 2)
    assert not pending._pending  # expiry transferred ownership to the callback
    closing = asyncio.create_task(pending.close())
    try:
        await asyncio.sleep(0.02)
        assert not closing.done(), "Close forgot an expired callback still creating work"
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert timer.done()
        assert committed == ([] if failure else ["synthetic.txt"])
        assert canary not in caplog.text
        with pytest.raises(RuntimeError, match="closed"):
            await pending.stage("too-late", ["late.txt"], {}, flush)
    finally:
        release.set()
        await asyncio.gather(closing, timer, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_feishu_pending_flush_commits_dedupe_before_close_finishes(tmp_path):
    app = _isolated_feishu_app(tmp_path)
    manager = app.state.manager
    calls = _install_completed_task_factory(app)
    original_create = manager.create_async
    entered, release = asyncio.Event(), asyncio.Event()
    pending = app.state.pending_feishu_attachments
    pending.delay_seconds = 0.02

    async def create(*args, **kwargs):
        task = await original_create(*args, **kwargs)
        entered.set()
        await release.wait()
        return task

    async def download(*args, **kwargs):
        target = tmp_path / "synthetic.txt"
        target.write_bytes(b"synthetic fixture")
        return target

    manager.create_async = create
    app.state.feishu_bridge.download_message_resource = download
    event = _raw_feishu_event("synthetic-pending-close", with_resource=True, prompt="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post("/api/feishu/events", json=event)
        assert response.json()["pending_attachments"] is True
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(pending.close())
        try:
            await asyncio.sleep(0.02)
            assert not closing.done() and len(calls) == 1
            release.set()
            await asyncio.wait_for(closing, 2)
            retry = await client.post("/api/feishu/events", json=event)
            assert retry.json()["reason"] == "duplicate_event"
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            await manager.shutdown()
