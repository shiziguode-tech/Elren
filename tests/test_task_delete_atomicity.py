"""A rejected DELETE must not silently disable later history persistence."""
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from deepdesk.models import AgentTask, TaskStatus
from deepdesk.task_store import TaskStore


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize("failure", ["statement", "commit"])
def test_rejected_delete_preserves_future_writes(tmp_path, monkeypatch, status, failure):
    store = TaskStore(tmp_path / "任务.db")
    task = AgentTask(prompt="Synthetic original", title="Original", status=status)
    store.save(task)
    connect = store._connect
    if failure == "statement":
        with sqlite3.connect(store.path) as db:
            db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON tasks "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic delete rejection'); END")
    else:
        @contextmanager
        def reject_commit():
            with connect() as db:
                db.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                                  else sqlite3.SQLITE_OK)
                yield db
        monkeypatch.setattr(store, "_connect", reject_commit)
    with pytest.raises(sqlite3.Error):
        store.delete(task.id)
    monkeypatch.setattr(store, "_connect", connect)
    assert store.get(task.id).title == "Original"
    assert task.id not in store._deleted_ids
    task.title = "Changed after rejection"
    store.save(task)
    fresh = TaskStore(store.path).get(task.id)
    assert fresh.title == task.title and fresh.status == status
    if failure == "statement":
        with sqlite3.connect(store.path) as db:
            db.execute("DROP TRIGGER reject_delete")
    assert store.delete(task.id)
    store.save(task)
    assert store.get(task.id) is None


def test_committed_delete_fences_save_before_releasing_lock(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    task = AgentTask(prompt="Synthetic original")
    store.save(task)
    connect = store._connect
    committed, attempting, finished = (threading.Event() for _ in range(3))
    errors = []

    @contextmanager
    def observe_commit():
        with connect() as db:
            yield db
        if threading.current_thread() is threading.main_thread():
            committed.set()
            assert attempting.wait(3)
            assert not finished.wait(0.05), "Save must remain behind deletion's lock"

    def late_save():
        try:
            assert committed.wait(3)
            attempting.set()
            store.save(task)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(store, "_connect", observe_commit)
    worker = threading.Thread(target=late_save)
    worker.start()
    try:
        assert store.delete(task.id)
    finally:
        worker.join(5)
        monkeypatch.setattr(store, "_connect", connect)
    assert not worker.is_alive() and not errors
    assert finished.is_set() and store.get(task.id) is None


def test_missing_task_delete_still_fences_late_insert(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    task = AgentTask(prompt="Not yet persisted")
    assert not store.delete(task.id)
    store.save(task)
    assert store.get(task.id) is None
