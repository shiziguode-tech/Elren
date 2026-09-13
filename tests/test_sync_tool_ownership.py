"""No desktop interaction: synthetic workers exercise real tool async adapters."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from deepdesk.engine import ApprovalGate, TaskManager
from deepdesk.models import AgentProfile, ApprovalPolicy, TaskStatus
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.clipboard import ClipboardTool
from deepdesk.plugins.builtin.computer import ComputerTool
from deepdesk.plugins.builtin.computer_use import ComputerUseAgentTool
from deepdesk.plugins.builtin.document import DocumentTool
from deepdesk.plugins.builtin.jianpu_omr import JianpuOMRTool, JianpuToStaffTool
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool
from deepdesk.plugins.builtin.windows_ui import WindowsUITool
from deepdesk.task_store import TaskStore

TOOL_TYPES = (ClipboardTool, ComputerTool, ComputerUseAgentTool, DocumentTool,
              JianpuOMRTool, JianpuToStaffTool, ProcessManagerTool, WindowsUITool)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", TOOL_TYPES)
@pytest.mark.parametrize("mode", ["success", "failure", "cancel", "repeat-cancel", "cancel-error"])
async def test_sync_adapter_owns_side_effect_until_terminal(tmp_path, monkeypatch, caplog, tool_type, mode):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    tool = tool_type(tmp_path) if tool_type in {ComputerTool, ComputerUseAgentTool} else tool_type()

    def worker(*_):
        entered.set()
        try:
            assert release.wait(5), "Fixture release timeout"
            (tmp_path / "synthetic-output.txt").write_text("safe boundary", encoding="utf-8")
            if mode in {"failure", "cancel-error"}:
                raise ValueError("SYNTHETIC_PRIVATE_WORKER_DETAIL")
            return {"ok": True, "value": 17}
        finally:
            finished.set()

    monkeypatch.setattr(tool, "_execute_sync", worker)
    task = asyncio.create_task(tool.execute({}, ToolContext("synthetic", str(tmp_path))))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)

        if mode.startswith("cancel") or mode == "repeat-cancel":
            task.cancel()
            await asyncio.sleep(0.02)
            if mode == "repeat-cancel":
                task.cancel()
                await asyncio.sleep(0.02)
            assert not task.done(), "Cancellation detached a mutating synchronous worker"
            assert not finished.is_set()
        release.set()
        if mode in {"cancel", "repeat-cancel", "cancel-error"}:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
            assert "SYNTHETIC_PRIVATE_WORKER_DETAIL" not in caplog.text
        elif mode == "failure":
            with pytest.raises(ValueError, match="SYNTHETIC_PRIVATE_WORKER_DETAIL"):
                await task
        else:
            assert await task == {"ok": True, "value": 17}
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(2):
            while not finished.is_set():
                await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_type", [JianpuOMRTool, JianpuToStaffTool])
async def test_manager_does_not_publish_cancelled_while_export_is_live(tmp_path, monkeypatch, tool_type):
    entered, release = threading.Event(), threading.Event()
    tool = tool_type()

    def worker(*_):
        entered.set()
        assert release.wait(5)
        (tmp_path / "export.txt").write_text("complete", encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(tool, "_execute_sync", worker)

    async def run(task, cancel, steering=None):
        task.status = TaskStatus.RUNNING
        await manager.emit_async(task, "status", {"status": task.status})
        await tool.execute({}, ToolContext(task.id, str(tmp_path)))

    manager = TaskManager(SimpleNamespace(run=run, workspace=str(tmp_path)), ApprovalGate(),
                          TaskStore(tmp_path / "synthetic.db"))
    task = await manager.create_async("synthetic", ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL)
    background = manager.background[task.id]
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert manager.cancel(task.id)
        await asyncio.sleep(0.03)
        assert not background.done()
        assert task.status == TaskStatus.RUNNING
        assert manager.store.get(task.id).status == TaskStatus.RUNNING
        with pytest.raises(ValueError):
            await manager.continue_from_async(task, "too early", ApprovalPolicy.AUTONOMOUS,
                                              AgentProfile.GENERAL, same_conversation=True)
        release.set()
        await background
        assert (tmp_path / "export.txt").is_file()
        assert task.status == manager.store.get(task.id).status == TaskStatus.CANCELLED
    finally:
        release.set()
        await manager.shutdown()
