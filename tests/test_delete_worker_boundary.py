"""Deletion owns storage cleanup, not success of a cancelled worker's final save."""
import asyncio
import sqlite3

import httpx
import pytest
from test_loopback_web_security import _isolated_app

from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, TaskStatus


def transport(app):
    return httpx.ASGITransport(app=app, raise_app_exceptions=False,
                              client=("127.0.0.1", 12345))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(TaskStatus))
async def test_delete_each_state_without_worker(tmp_path, status):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic delete", status=status)
    manager.store.save(task)
    manager.tasks[task.id] = task
    async with httpx.AsyncClient(transport=transport(app), base_url="http://127.0.0.1") as client:
        response = await client.delete(f"/api/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert manager.store.get(task.id) is None
    assert task.id not in manager.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING,
                                    TaskStatus.WAITING, TaskStatus.WAITING_USER])
@pytest.mark.parametrize("reject_delete", [False, True])
async def test_worker_save_failure_does_not_prevent_delete(tmp_path, caplog, status, reject_delete):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    manager.on_task_created = None
    started = asyncio.Event()

    async def waiting(task, cancel, steering=None):
        task.status = status
        manager.emit(task, "status", {"status": status})
        started.set()
        await asyncio.Event().wait()

    manager.engine.run = waiting
    task = manager.create("Synthetic cancellation", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    try:
        await asyncio.wait_for(started.wait(), 2)
        with sqlite3.connect(manager.store.path) as db:
            db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'opaque-synthetic-test-canary'); END")
            if reject_delete:
                db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END")
        async with httpx.AsyncClient(transport=transport(app), base_url="http://127.0.0.1") as client:
            response = await client.delete(f"/api/tasks/{task.id}")
            if reject_delete:
                assert response.status_code >= 500
                assert manager.store.get(task.id) is not None
                assert task.id in manager.tasks
                assert task.id not in manager.store._deleted_ids
                with sqlite3.connect(manager.store.path) as db:
                    db.execute("DROP TRIGGER reject_delete")
                response = await client.delete(f"/api/tasks/{task.id}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True
        assert manager.store.get(task.id) is None
        assert task.id not in manager.tasks
        assert task.id not in manager.background
        assert "opaque-synthetic-test-canary" not in caplog.text
        # The still-failing UPDATE trigger does not block the deletion fence.
        manager.store.save(task)
        assert manager.store.get(task.id) is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_http_owner_is_not_mistaken_for_cancelled_worker(tmp_path):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic request cancellation", status=TaskStatus.RUNNING)
    manager.store.save(task)
    manager.tasks[task.id] = task
    entered, release = asyncio.Event(), asyncio.Event()

    async def unwinding():
        entered.set()
        await release.wait()

    background = asyncio.create_task(unwinding())
    manager.background[task.id] = background
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, "path", "") == "/api/tasks/{task_id}"
                    and "DELETE" in getattr(route, "methods", set()))
    request = asyncio.create_task(endpoint(task.id))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert manager.store.get(task.id) is not None
        assert task.id in manager.tasks
        assert not background.cancelled()
    finally:
        release.set()
        await background
        manager.background.pop(task.id, None)


@pytest.mark.asyncio
async def test_direct_worker_cancellation_still_allows_delete(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic worker cancellation", status=TaskStatus.RUNNING)
    manager.store.save(task)
    manager.tasks[task.id] = task
    worker = asyncio.create_task(asyncio.Event().wait())
    manager.background[task.id] = worker
    monkeypatch.setattr(manager, "cancel", lambda _id: worker.cancel())
    try:
        async with httpx.AsyncClient(transport=transport(app), base_url="http://127.0.0.1") as client:
            response = await client.delete(f"/api/tasks/{task.id}")
        assert response.status_code == 200
        assert worker.cancelled()
        assert manager.store.get(task.id) is None
        assert task.id not in manager.background
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_worker_remains_owned_and_cannot_resurrect_deleted_task(tmp_path):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    manager.on_task_created = None
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_unwind(task, cancel, steering=None):
        task.status = TaskStatus.RUNNING
        manager.emit(task, "status", {"status": task.status})
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        task.status = TaskStatus.COMPLETED
        manager.emit(task, "status", {"status": task.status})

    manager.engine.run = slow_unwind
    task = manager.create("Synthetic slow unwind", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    worker = manager.background[task.id]
    try:
        await asyncio.wait_for(started.wait(), 2)
        async with httpx.AsyncClient(transport=transport(app), base_url="http://127.0.0.1") as client:
            response = await client.delete(f"/api/tasks/{task.id}")
        assert response.status_code == 200
        assert manager.background.get(task.id) is worker
        assert not worker.done()
        assert manager.store.get(task.id) is None
        release.set()
        await asyncio.wait_for(worker, 2)
        await asyncio.sleep(0)
        assert task.id not in manager.background
        assert manager.store.get(task.id) is None
    finally:
        release.set()
        await manager.shutdown()
