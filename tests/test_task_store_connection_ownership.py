"""Exercise actual SQLite while retaining every connection: GC cannot hide leaks."""
from __future__ import annotations

import gc
import sqlite3

import pytest

from deepdesk.models import AgentTask, TaskStatus
from deepdesk.task_store import TaskStore


@pytest.fixture
def connections(monkeypatch):
    original = sqlite3.connect
    opened = []
    fail = {"sql": "", "exit": False}

    class TrackedConnection(sqlite3.Connection):
        close_count = 0

        def execute(self, sql, parameters=(), /):
            if fail["sql"] and fail["sql"] in sql:
                raise sqlite3.OperationalError("synthetic SQL failure")
            return super().execute(sql, parameters)

        def __exit__(self, exc_type, exc_value, traceback):
            if fail["exit"]:
                self.rollback()
                raise sqlite3.OperationalError("synthetic transaction exit failure")
            return super().__exit__(exc_type, exc_value, traceback)

        def close(self):
            self.close_count += 1
            return super().close()

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=TrackedConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr("deepdesk.task_store.sqlite3.connect", connect)
    yield opened, fail, original
    for connection in opened:
        # Do not let test failure retain handles for other tests. Call the base
        # method only after assertions; it cannot fake the observed close count.
        sqlite3.Connection.close(connection)


def assert_closed(opened):
    assert opened
    assert all(connection.close_count == 1 for connection in opened)
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_repeated_operations_close_immediately_without_gc(tmp_path, connections):
    opened, _, _ = connections
    store = TaskStore(tmp_path / "tasks.sqlite")
    task = AgentTask(prompt="durable original constraint", status=TaskStatus.COMPLETED,
                     result="done", continuation_instructions=["original", "follow-up"],
                     continuation_legacy_context="historical reference")
    store.save(task)
    gc_enabled = gc.isenabled()
    gc.disable()
    try:
        for index in range(100):
            assert store.get(task.id).continuation_instructions == ["original", "follow-up"]
            assert_closed(opened)
            if index % 10 == 0:
                assert store.list_page()[1] == 1
                assert store.load_recent()[0].id == task.id
                assert store.list_restart_interrupted("web") == []
                store.build_cross_conversation_context("original constraint")
                assert_closed(opened)
    finally:
        if gc_enabled:
            gc.enable()
    assert store.rename(task.id, "chosen title").title_source == "user"
    assert store.get(task.id).continuation_legacy_context == "historical reference"
    assert store.delete(task.id)
    assert store.get(task.id) is None
    assert_closed(opened)


@pytest.mark.parametrize("sql", ["PRAGMA journal_mode", "PRAGMA secure_delete"])
def test_setup_failure_closes_connection_before_yield(tmp_path, connections, sql):
    opened, fail, _ = connections
    fail["sql"] = sql
    with pytest.raises(sqlite3.OperationalError, match="synthetic SQL"):
        TaskStore(tmp_path / "tasks.sqlite")
    assert len(opened) == 1
    assert_closed(opened)


def test_statement_failure_rolls_back_and_closes(tmp_path, connections):
    opened, _, _ = connections
    store = TaskStore(tmp_path / "tasks.sqlite")
    with store._connect() as connection:
        connection.execute("CREATE TABLE sample(value INTEGER UNIQUE)")
    with pytest.raises(sqlite3.IntegrityError), store._connect() as connection:
        connection.execute("INSERT INTO sample VALUES (7)")
        connection.execute("INSERT INTO sample VALUES (7)")
    assert_closed(opened)
    with store._connect() as connection:
        assert connection.execute("SELECT * FROM sample").fetchall() == []
        connection.execute("INSERT INTO sample VALUES (8)")
    with store._connect() as connection:
        assert connection.execute("SELECT * FROM sample").fetchall() == [(8,)]
    assert_closed(opened)


def test_iteration_failure_and_transaction_exit_failure_close(tmp_path, connections):
    opened, fail, _ = connections
    store = TaskStore(tmp_path / "tasks.sqlite")
    with pytest.raises(RuntimeError, match="consumer failed"), store._connect() as connection:
        for _row in connection.execute("SELECT 1 UNION ALL SELECT 2"):
            raise RuntimeError("consumer failed")
    assert_closed(opened)
    fail["exit"] = True
    with pytest.raises(sqlite3.OperationalError, match="transaction exit"), store._connect() as connection:
        connection.execute("SELECT 1")
    fail["exit"] = False
    assert_closed(opened)


def test_migration_query_failure_closes_without_deleting_data(tmp_path, connections):
    opened, fail, _ = connections
    path = tmp_path / "tasks.sqlite"
    store = TaskStore(path)
    task = AgentTask(prompt="keep this", status=TaskStatus.COMPLETED)
    store.save(task)
    fail["sql"] = "SELECT value FROM security_migrations"
    with pytest.raises(sqlite3.OperationalError):
        store.get(task.id)
    fail["sql"] = ""
    assert_closed(opened)
    assert TaskStore(path).get(task.id).prompt == "keep this"
    assert_closed(opened)


def test_restarts_preserve_history_and_never_close_other_owners(tmp_path, connections):
    opened, _, original = connections
    path = tmp_path / "tasks.sqlite"
    store = TaskStore(path)
    task = AgentTask(prompt="continue", status=TaskStatus.RUNNING,
                     title="chosen", title_source="user",
                     continuation_instructions=["original constraint", "continue"])
    store.save(task)
    independent = original(path)
    try:
        independent.execute("CREATE TABLE scheduler_owned(value TEXT)")
        independent.execute("INSERT INTO scheduler_owned VALUES ('untouched')")
        independent.commit()
        for _ in range(20):
            restarted = TaskStore(path)
            restored = restarted.load_recent()[0]
            assert restored.status == TaskStatus.FAILED
            assert restored.title_source == "user"
            assert restored.continuation_instructions == task.continuation_instructions
            assert independent.execute("SELECT value FROM scheduler_owned").fetchone() == ("untouched",)
            assert_closed(opened)
    finally:
        independent.close()
