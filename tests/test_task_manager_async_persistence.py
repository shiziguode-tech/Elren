"""Real SQLite contention and publication ownership; no providers or user data."""
import asyncio
import sqlite3
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from deepdesk.engine import TaskManager
from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, TaskStatus
from deepdesk.secret_redaction import redact_sensitive
from deepdesk.task_store import TaskStore


def fixture(tmp_path):
    store = TaskStore(tmp_path / "任务 database.db")
    manager = TaskManager(SimpleNamespace(), SimpleNamespace(), store)
    task = AgentTask(prompt="Synthetic task", title="Original", title_source="auto",
                     status=TaskStatus.COMPLETED)
    store.save(task)
    manager.tasks[task.id] = task
    return manager, task


async def mutate(manager, task, operation):
    if operation == "emit":
        return await manager.emit_async(task, "assistant", {"content": "Accepted"})
    if operation == "rename":
        return await manager.rename_async(task.id, "Renamed")
    return await manager.delete_async(task.id)


def assert_original(manager, task):
    assert manager.tasks[task.id] is task
    assert task.title == "Original"
    assert not task.events


def assert_committed(manager, task, operation):
    stored = manager.store.get(task.id)
    if operation == "delete":
        assert stored is None and task.id not in manager.tasks
        assert task.id in manager.store._deleted_ids
    elif operation == "rename":
        assert stored.title == task.title == "Renamed"
        assert manager.tasks[task.id] is task
    else:
        assert stored.events == task.events
        assert [event.data["content"] for event in task.events] == ["Accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["emit", "rename", "delete"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_sqlite_contention_preserves_loop_and_commit_publication(tmp_path, monkeypatch, operation, cancelled):
    manager, task = fixture(tmp_path)
    entered = threading.Event()
    method = "delete" if operation == "delete" else "save_prepared"
    original = getattr(manager.store, method)
    owner_thread = threading.get_ident()

    def write(*args):
        assert threading.get_ident() != owner_thread
        entered.set()
        return original(*args)

    monkeypatch.setattr(manager.store, method, write)
    holder = sqlite3.connect(manager.store.path)
    holder.execute("BEGIN IMMEDIATE")
    pending = asyncio.create_task(mutate(manager, task, operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # Only this loop can release the real SQLite lock. An on-loop write
        # would time out and fail rather than satisfying these assertions.
        await asyncio.sleep(0.03)
        assert not pending.done()
        assert_original(manager, task)
        if cancelled:
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done(), "Do not abandon a writer after cancellation"
        holder.rollback()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 3)
        else:
            await asyncio.wait_for(pending, 3)
        assert_committed(manager, task, operation)
        assert not manager._mutation_lock.locked()
    finally:
        holder.close()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["emit", "rename", "delete"])
async def test_sql_rejection_does_not_publish_and_retry_succeeds(tmp_path, operation):
    manager, task = fixture(tmp_path)
    sql_operation = "DELETE" if operation == "delete" else "UPDATE"
    with sqlite3.connect(manager.store.path) as db:
        db.execute(f"CREATE TRIGGER reject_write BEFORE {sql_operation} ON tasks "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic write rejection'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write rejection"):
        await mutate(manager, task, operation)
    assert_original(manager, task)
    stored = manager.store.get(task.id)
    assert stored.title == "Original" and not stored.events
    assert task.id not in manager.store._deleted_ids
    with sqlite3.connect(manager.store.path) as db:
        db.execute("DROP TRIGGER reject_write")
    await mutate(manager, task, operation)
    assert_committed(manager, task, operation)


@pytest.mark.asyncio
async def test_ordered_mutations_keep_events_title_and_detached_event_data(tmp_path):
    manager, task = fixture(tmp_path)
    data = {"content": "first"}
    async with manager._mutation_scope():
        first = asyncio.create_task(manager.emit_async(task, "assistant", data))
        await asyncio.sleep(0)
        data["content"] = "changed after submission"
        rename = asyncio.create_task(manager.rename_async(task.id, "Manual"))
        second = asyncio.create_task(manager.emit_async(task, "assistant", {"content": "second"}))
        await asyncio.sleep(0)
        assert_original(manager, task)
    await asyncio.gather(first, rename, second)
    stored = manager.store.get(task.id)
    assert [event.data["content"] for event in stored.events] == ["first", "second"]
    assert stored.events == task.events
    assert stored.title == task.title == "Manual"


@pytest.mark.asyncio
async def test_delete_then_late_event_cannot_resurrect_task(tmp_path):
    manager, task = fixture(tmp_path)
    async with manager._mutation_scope():
        deleting = asyncio.create_task(manager.delete_async(task.id))
        late = asyncio.create_task(manager.emit_async(task, "assistant", {"content": "late"}))
        await asyncio.sleep(0)
    await asyncio.gather(deleting, late)
    manager.store.save_prepared(manager.store.prepare_save(task))
    assert manager.store.get(task.id) is None
    assert task.id not in manager.tasks and not task.events


@pytest.mark.asyncio
async def test_automatic_title_cannot_overwrite_newer_manual_title(tmp_path):
    manager, task = fixture(tmp_path)
    async with manager._mutation_scope():
        manual = asyncio.create_task(manager.rename_async(task.id, "Manual"))
        automatic = asyncio.create_task(manager.rename_async(task.id, "Late auto", "auto", expected_task=task))
        await asyncio.sleep(0)
    assert await manual is task
    assert await automatic is None
    assert manager.store.get(task.id).title == task.title == "Manual"


@pytest.mark.asyncio
async def test_sync_mutation_rejected_before_effects_on_other_owner_or_thread(tmp_path):
    manager, task = fixture(tmp_path)
    async with manager._mutation_scope():
        async def other_owner():
            with pytest.raises(RuntimeError, match="async mutation API"):
                manager.emit(task, "assistant", {"content": "must not publish"})

        await asyncio.create_task(other_owner())
        with pytest.raises(RuntimeError, match="async mutation API"):
            await asyncio.to_thread(manager.emit, task, "assistant", {"content": "thread"})
        assert_original(manager, task)


def test_prepared_snapshot_preserves_redaction_and_private_history_after_rotation(tmp_path):
    store = TaskStore(tmp_path / "snapshot.db")
    secret = "opaque-synthetic-old-secret-value"
    secrets = [secret]
    store.set_persistence_redactor(lambda value, task_id: redact_sensitive(value, secrets),
                                  secret_values=lambda: secrets)
    task = AgentTask(prompt=f"Do not persist {secret}", title="Original",
                     continuation_instructions=[f"Earlier {secret}"],
                     continuation_legacy_context=f"Legacy {secret}")
    prepared = store.prepare_save(task)
    with pytest.raises(FrozenInstanceError):
        prepared.payload = "modified"
    task.title = "Changed after capture"
    task.continuation_instructions.append("later")
    secrets[:] = ["different-synthetic-secret"]
    store.save_prepared(prepared)
    stored = store.get(task.id)
    assert stored.title == "Original"
    assert len(stored.continuation_instructions) == 1
    assert secret not in prepared.payload
    assert secret not in stored.continuation_legacy_context
    assert secret in task.prompt, "Do not mutate the actual user request"


@pytest.mark.asyncio
async def test_uncached_rename_and_in_memory_only_variants(tmp_path):
    manager, task = fixture(tmp_path)
    manager.tasks.clear()
    restored = await manager.rename_async(task.id, "Restored")
    assert restored is manager.tasks[task.id] and restored is not task
    assert manager.store.get(task.id).title == "Restored"
    assert await manager.get_async("absent") is None
    manager.store = None
    await manager.emit_async(restored, "assistant", {"content": "memory"})
    assert await manager.rename_async(task.id, "Memory") is restored
    assert await manager.delete_async(task.id) is True
    assert await manager.delete_async(task.id) is False


def install_engine(manager):
    calls = []

    async def run(task, cancel, queue):
        # No model, network, or user workspace access. Prove initial data is
        # durable before the engine gets its first execution opportunity.
        persisted = await asyncio.to_thread(manager.store.get, task.id)
        assert persisted.prompt == task.prompt
        calls.append(task.id)
        task.status = TaskStatus.COMPLETED
        task.result = "Synthetic completed result"
        await manager.emit_async(task, "assistant", {"content": task.result})

    manager.engine.run = run
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "continue"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_creation_commits_before_worker_registration_even_after_cancel(tmp_path, monkeypatch, operation, cancelled):
    manager, previous = fixture(tmp_path)
    calls = install_engine(manager)
    entered = threading.Event()
    original = manager.store.save_prepared

    def write(prepared):
        entered.set()
        return original(prepared)

    monkeypatch.setattr(manager.store, "save_prepared", write)
    holder = sqlite3.connect(manager.store.path)
    holder.execute("BEGIN IMMEDIATE")
    options = (ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    pending = asyncio.create_task(
        manager.create_async("New submission", *options) if operation == "create" else
        manager.continue_from_async(previous, "New submission", *options, same_conversation=True)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.02)
        assert_original(manager, previous)
        assert len(manager.tasks) == 1
        assert not manager.background and not calls
        if cancelled:
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
        holder.rollback()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await pending
            task = next(task for task in manager.tasks.values() if task.prompt == "New submission")
        else:
            task = await pending
        workers = list(manager.background.values())
        if workers:
            await asyncio.wait_for(asyncio.gather(*workers), 3)
        assert calls == [task.id]
        assert manager.store.get(task.id).result == "Synthetic completed result"
        if operation == "continue":
            assert task.id == previous.id and task is not previous
            assert task.conversation_turns[0].prompt == previous.prompt
            assert task.continuation_instructions == [previous.prompt, "New submission"]
        else:
            assert task.id != previous.id and len(manager.tasks) == 2
    finally:
        holder.close()
        await asyncio.gather(pending, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(TaskStatus))
async def test_async_continuation_all_states_preserve_identity_or_reject(tmp_path, status):
    manager, previous = fixture(tmp_path)
    previous.status = status
    manager.store.save(previous)
    calls = install_engine(manager)
    try:
        operation = manager.continue_from_async(previous, "Follow up", ApprovalPolicy.AUTONOMOUS,
                                                AgentProfile.GENERAL, same_conversation=True)
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            with pytest.raises(ValueError):
                await operation
            assert manager.tasks[previous.id] is previous
            assert not calls and not manager.background
        else:
            continued = await operation
            assert continued.id == previous.id and continued is not previous
            assert continued.conversation_turns[0].status == status
            await asyncio.gather(*list(manager.background.values()))
            assert manager.store.get(previous.id).prompt == "Follow up"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "continue"])
async def test_creation_rejection_does_not_launch_or_replace(tmp_path, operation):
    manager, previous = fixture(tmp_path)
    calls = install_engine(manager)
    with sqlite3.connect(manager.store.path) as db:
        db.execute("CREATE TRIGGER reject_new BEFORE INSERT ON tasks "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic no space'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic no space"):
            if operation == "create":
                await manager.create_async("New", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
            else:
                await manager.continue_from_async(previous, "New", ApprovalPolicy.AUTONOMOUS,
                                                   AgentProfile.GENERAL, same_conversation=True)
        assert_original(manager, previous)
        assert len(manager.tasks) == 1 and not manager.background and not calls
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_duplicate_continuation_cannot_replace_successor(tmp_path):
    manager, previous = fixture(tmp_path)
    calls = install_engine(manager)
    try:
        results = await asyncio.gather(*[
            manager.continue_from_async(previous, prompt, ApprovalPolicy.AUTONOMOUS,
                                        AgentProfile.GENERAL, same_conversation=True)
            for prompt in ("first", "duplicate")
        ], return_exceptions=True)
        assert isinstance(results[0], AgentTask)
        assert isinstance(results[1], ValueError)
        await asyncio.gather(*list(manager.background.values()))
        assert calls == [previous.id]
        assert manager.store.get(previous.id).prompt == "first"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(TaskStatus))
async def test_async_steering_all_states(tmp_path, status):
    manager, task = fixture(tmp_path)
    task.status = status
    worker = asyncio.create_task(asyncio.Event().wait())
    queue = asyncio.Queue()
    manager.background[task.id] = worker
    manager.steering_queues[task.id] = queue
    try:
        operation = manager.steer_async(task.id, "New instruction", ["synthetic.txt"])
        if status not in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING}:
            with pytest.raises(ValueError):
                await operation
            assert queue.empty() and not task.events
        else:
            assert await operation is task
            message = queue.get_nowait()
            stored = manager.store.get(task.id)
            assert message["message_id"] == stored.events[-1].id
            assert stored.continuation_instructions == [task.prompt, "New instruction"]
            assert stored.attachments == ["synthetic.txt"]
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_steering_commit_preserves_concurrent_terminal_output(tmp_path, monkeypatch, failed):
    manager, task = fixture(tmp_path)
    task.status = TaskStatus.RUNNING
    worker = asyncio.create_task(asyncio.Event().wait())
    queue = asyncio.Queue()
    manager.background[task.id] = worker
    manager.steering_queues[task.id] = queue
    entered, release = threading.Event(), threading.Event()
    original = manager.store.save_prepared

    def write(prepared):
        entered.set()
        assert release.wait(3)
        if failed:
            raise OSError("synthetic failed write")
        return original(prepared)

    monkeypatch.setattr(manager.store, "save_prepared", write)
    pending = asyncio.create_task(manager.steer_async(task.id, "Next"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert queue.empty() and not task.events
        task.status = TaskStatus.COMPLETED
        task.result = "Finished while disk was busy"
        release.set()
        if failed:
            with pytest.raises(OSError):
                await pending
            assert queue.empty() and not task.events
        else:
            await pending
            assert queue.qsize() == 1
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "Finished while disk was busy"
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(pending, worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_event_redacts_original_secret_before_key_rotation(tmp_path):
    manager, task = fixture(tmp_path)
    secret = "opaque-synthetic-old-event-secret"
    secrets = [secret]
    manager.engine._redact_for_persistence = lambda value, task_id: redact_sensitive(value, secrets)
    async with manager._mutation_scope():
        pending = asyncio.create_task(manager.emit_async(task, "assistant", {"content": secret}))
        await asyncio.sleep(0)
        secrets[:] = ["new-synthetic-secret"]
    await pending
    assert secret not in manager.store.get(task.id).model_dump_json()


@pytest.mark.asyncio
async def test_cross_context_io_is_off_loop_and_cancellation_does_not_create(tmp_path):
    manager, _ = fixture(tmp_path)
    calls = install_engine(manager)
    entered, release = threading.Event(), threading.Event()
    owner = threading.get_ident()

    def context(*args):
        assert threading.get_ident() != owner
        entered.set()
        assert release.wait(3)
        return "Synthetic recall", []

    manager.cross_context_provider = context
    pending = asyncio.create_task(manager.create_async("New", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(manager.tasks) == 1 and not calls and not manager.background
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
