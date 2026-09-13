"""Prove commit/publication/cancellation order without real user data."""
import asyncio
import sqlite3
import threading

import pytest

from deepdesk.persistence_work import run_owned_commit
from deepdesk.plugins.base import run_owned_thread, wait_owned_result


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_owned_commit_waits_for_write_and_publishes_on_owner(tmp_path, caplog, cancelled, failed):
    database = tmp_path / "事务.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probe (value TEXT)")
    owner = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    published = []
    canary = "opaque-synthetic-persistence-error-canary"

    def write():
        assert threading.get_ident() != owner
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO probe VALUES ('saved')")
            entered.set()
            assert release.wait(5)
            if failed:
                raise OSError(canary)
        return "saved"

    def publish(value):
        assert threading.get_ident() == owner
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM probe").fetchall() == [(value,)]
        published.append(value)

    pending = asyncio.create_task(run_owned_commit(write, publish=publish))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not published
        if cancelled:
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done(), "Cancellation must not detach the writer"
        release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await pending
        elif failed:
            with pytest.raises(OSError, match=canary):
                await pending
        else:
            assert await pending == "saved"
        assert published == ([] if failed else ["saved"])
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM probe").fetchall() == ([] if failed else [("saved",)])
        assert canary not in caplog.text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_write_contention_does_not_stall_other_owner_loop_work(tmp_path):
    database = tmp_path / "contention.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE probe (value INTEGER)")
    holder = sqlite3.connect(database)
    holder.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    published = []

    def write():
        entered.set()
        with sqlite3.connect(database, timeout=3) as connection:
            connection.execute("INSERT INTO probe VALUES (1)")
        return 1

    pending = asyncio.create_task(run_owned_commit(write, publish=published.append))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # This owner-loop task releases the actual lock. A synchronous SQLite
        # write on this same loop would time out before release can execute.
        await asyncio.sleep(0.05)
        assert not pending.done() and not published
        holder.rollback()
        assert await asyncio.wait_for(pending, 2) == 1
        assert published == [1]
    finally:
        holder.close()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_mutation_lock_has_no_side_effects():
    lock = asyncio.Lock()
    effects = []
    await lock.acquire()

    async def mutation():
        async with lock:
            return await run_owned_commit(lambda: effects.append("write"),
                                          publish=lambda _: effects.append("publish"))

    pending = asyncio.create_task(mutation())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    lock.release()
    assert effects == []


@pytest.mark.asyncio
async def test_existing_owned_thread_does_not_leak_exception_after_repeated_cancel(caplog):
    entered, release = threading.Event(), threading.Event()
    canary = "opaque-synthetic-worker-failure-canary"

    def write():
        entered.set()
        assert release.wait(5)
        raise OSError(canary)

    pending = asyncio.create_task(run_owned_thread(write))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(2):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0)
        assert canary not in caplog.text
        assert "exception in shielded future" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_does_not_cancel_worker_or_install_raw_error_logger(caplog):
    release = asyncio.Event()
    canary = "opaque-synthetic-late-owned-error"

    async def later():
        await release.wait()
        raise ValueError(canary)

    worker = asyncio.create_task(later())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(wait_owned_result(worker), 0.02)
        assert not worker.done()
        release.set()
        with pytest.raises(ValueError, match=canary):
            await wait_owned_result(worker)
        await asyncio.sleep(0)
        assert canary not in caplog.text
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_cancellation_remains_distinct_from_waiter_cancellation():
    worker = asyncio.create_task(asyncio.Event().wait())
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_owned_result(worker)
    assert asyncio.current_task().cancelling() == 0
