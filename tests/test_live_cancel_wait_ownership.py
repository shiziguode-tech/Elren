"""No real UI: cancellation cleanup uses a synthetic controller only."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.live_computer_use import LiveComputerUseTool


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_repeated_cancel_joins_stop_without_logging_raw_exception(tmp_path, monkeypatch, caplog, failed):
    observing = asyncio.Event()
    stopping, release, stopped = threading.Event(), threading.Event(), threading.Event()
    canary = "opaque-synthetic-controller-stop-failure"

    def stop(task_id, reason):
        assert task_id == "synthetic-stop" and reason == "task_cancelled"
        stopping.set()
        try:
            assert release.wait(5)
            if failed:
                raise OSError(canary)
        finally:
            stopped.set()

    async def observe(*args):
        observing.set()
        await asyncio.Event().wait()

    tool = LiveComputerUseTool(tmp_path, windows_ocr=object(),
                               controller=SimpleNamespace(stop_task=stop))
    monkeypatch.setattr(tool, "_observe", observe)
    pending = asyncio.create_task(tool.execute({"action": "observe"},
                                  ToolContext(task_id="synthetic-stop", workspace=str(tmp_path))))
    try:
        await observing.wait()
        pending.cancel()
        assert await asyncio.to_thread(stopping.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pending.done(), "Stop cleanup must remain owned after repeated cancellation"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stopped.is_set()
        await asyncio.sleep(0)
        assert canary not in caplog.text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert await asyncio.to_thread(stopped.wait, 2)
