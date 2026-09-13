"""Actual HTTP/SQLite failure paths; synthetic titles and no model requests."""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from test_loopback_web_security import _isolated_app

from deepdesk.models import AgentTask, TaskEvent, TaskStatus


def reject_updates(store):
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_title_update BEFORE UPDATE ON tasks "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize("cached", [True, False])
def test_failed_rename_preserves_cached_and_durable_title(tmp_path, status, cached):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic task", title="Original title", title_source="auto", status=status)
    manager.store.save(task)
    if cached:
        manager.tasks[task.id] = task
    reject_updates(manager.store)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.patch(f"/api/tasks/{task.id}/title", json={"title": "New title"})
    assert response.status_code >= 500
    assert task.title == "Original title"
    assert task.title_source == "auto"
    assert manager.store.get(task.id).title == "Original title"
    assert manager.store.get(task.id).title_source == "auto"
    if not cached:
        assert task.id not in manager.tasks
    assert client.get(f"/api/tasks/{task.id}").json()["title"] == "Original title"


@pytest.mark.parametrize("status", list(TaskStatus))
def test_rename_retries_after_storage_recovers_without_replacing_worker_object(tmp_path, status):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic task", title="Original title", status=status)
    manager.store.save(task)
    manager.tasks[task.id] = task
    reject_updates(manager.store)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.patch(f"/api/tasks/{task.id}/title", json={"title": "New title"}).status_code == 503
    with sqlite3.connect(manager.store.path) as db:
        db.execute("DROP TRIGGER reject_title_update")
    response = client.patch(f"/api/tasks/{task.id}/title", json={"title": "  New   title  "})
    assert response.status_code == 200
    assert response.json()["title"] == "New title"
    assert manager.tasks[task.id] is task
    assert task.title == "New title" and task.title_source == "user"
    # A running worker holds this original object and may emit after rename.
    manager.emit(task, "synthetic_test", {"message": "Existing worker continued"})
    assert manager.store.get(task.id).title == "New title"
    assert manager.store.get(task.id).status == status


@pytest.mark.parametrize("language", ["en", "zh"])
def test_rename_failure_is_localized(tmp_path, language):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = AgentTask(prompt="Synthetic task", title="Kept")
    manager.store.save(task)
    reject_updates(manager.store)
    response = TestClient(app).patch(f"/api/tasks/{task.id}/title", json={"title": "New"},
                                     headers={"Accept-Language": language})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "task_title_save_failed"
    assert ("标题" in detail["message"]) == (language == "zh")


def title_task():
    return AgentTask(prompt="Synthetic task", title="Original title", title_source="auto",
                     events=[TaskEvent(type="model_selected", data={"model": "synthetic-model"})])


def start_title(manager, task):
    manager.on_task_created(task)
    return next(worker for worker in asyncio.all_tasks() if worker.get_name() == f"task-title-{task.id}")


@pytest.mark.asyncio
@pytest.mark.parametrize("write_failure", [True, False])
async def test_automatic_title_uses_same_durable_boundary(tmp_path, monkeypatch, write_failure):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = title_task()
    manager.tasks[task.id] = task
    manager.store.save(task)
    if write_failure:
        reject_updates(manager.store)
    async def generated(*args, **kwargs):
        return "Synthetic automatic title"
    monkeypatch.setattr(manager.engine.client, "generate_task_title", generated)
    await asyncio.wait_for(start_title(manager, task), timeout=2)
    expected = "Original title" if write_failure else "Synthetic automatic title"
    assert task.title == expected
    assert task.title_source == "auto"
    assert manager.tasks[task.id] is task
    assert manager.store.get(task.id).title == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["manual", "replacement", "delete"])
async def test_late_generated_title_never_overwrites_newer_user_state(tmp_path, monkeypatch, change):
    app = _isolated_app(tmp_path)
    manager = app.state.manager
    task = title_task()
    manager.tasks[task.id] = task
    manager.store.save(task)
    entered, release = asyncio.Event(), asyncio.Event()
    async def generated(*args, **kwargs):
        entered.set()
        await release.wait()
        return "Late automatic title"
    monkeypatch.setattr(manager.engine.client, "generate_task_title", generated)
    worker = start_title(manager, task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == "manual":
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                response = await client.patch(f"/api/tasks/{task.id}/title", json={"title": "User title"})
                assert response.status_code == 200
        elif change == "replacement":
            replacement = task.model_copy(update={"title": "Continued task title"})
            manager.store.save(replacement)
            manager.tasks[task.id] = replacement
        else:
            manager.store.delete(task.id)
            manager.tasks.pop(task.id)
    finally:
        release.set()
        await asyncio.wait_for(worker, 2)
    if change == "delete":
        assert task.id not in manager.tasks
        assert manager.store.get(task.id) is None
    else:
        expected = "User title" if change == "manual" else "Continued task title"
        assert manager.tasks[task.id].title == expected
        assert manager.store.get(task.id).title == expected
