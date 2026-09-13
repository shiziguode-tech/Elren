"""An async persistence sink must be awaited before execution continues."""
import asyncio

import pytest
from test_cancellation_ownership import Harness, Provider

from deepdesk.models import TaskStatus


@pytest.mark.asyncio
async def test_real_engine_awaits_async_event_sink_and_retains_history(tmp_path):
    h = Harness(tmp_path, Provider())
    events = []

    async def sink(task, kind, data):
        await asyncio.sleep(0)
        h.manager.emit(task, kind, data)
        events.append(kind)

    h.engine._emit_sink = sink
    h.start()
    try:
        await h.joined()
        assert "model_selected" in events and "assistant" in events
        h.assert_terminal(TaskStatus.COMPLETED)
    finally:
        await h.manager.shutdown()


@pytest.mark.asyncio
async def test_model_request_waits_for_selection_event_commit(tmp_path):
    h = Harness(tmp_path, Provider())
    entered, release = asyncio.Event(), asyncio.Event()

    async def sink(task, kind, data):
        if kind == "model_selected":
            entered.set()
            await release.wait()
        h.manager.emit(task, kind, data)

    h.engine._emit_sink = sink
    h.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        assert h.provider.calls == 0
        release.set()
        await h.joined()
        h.assert_terminal(TaskStatus.COMPLETED)
    finally:
        release.set()
        await h.manager.shutdown()


@pytest.mark.asyncio
async def test_async_selection_event_failure_stops_before_model_request(tmp_path):
    h = Harness(tmp_path, Provider())

    async def sink(task, kind, data):
        await asyncio.sleep(0)
        if kind == "model_selected":
            raise OSError("Synthetic event persistence rejection")
        h.manager.emit(task, kind, data)

    h.engine._emit_sink = sink
    h.start()
    try:
        await h.joined()
        assert h.provider.calls == 0
        h.assert_terminal(TaskStatus.FAILED)
    finally:
        await h.manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_async_callback_events_are_lazy_and_ordered(tmp_path, fail_first):
    h = Harness(tmp_path, Provider())
    stages = []

    async def sink(task, kind, data):
        stages.append("start-" + kind)
        await asyncio.sleep(0)
        if fail_first and kind == "first":
            raise OSError("Synthetic callback rejection")
        stages.append("end-" + kind)

    h.engine._emit_sink = sink
    from deepdesk.models import AgentTask
    task = AgentTask(prompt="Synthetic callback order")
    pending = h.engine.emit_callback_events(task, [("first", {}), ("second", {})])
    assert stages == []
    if fail_first:
        with pytest.raises(OSError):
            await pending
        assert stages == ["start-first"]
    else:
        await pending
        assert stages == ["start-first", "end-first", "start-second", "end-second"]


@pytest.mark.asyncio
async def test_specialist_orchestrator_awaits_async_event_sink():
    from test_subagent_orchestrator import _assignments, _RecordingClient, _task

    from deepdesk.subagents import SubagentOrchestrator

    task = _task()
    assignments, allowed = _assignments(1)
    events = []

    async def emit(kind, data):
        await asyncio.sleep(0)
        events.append(kind)

    async def audit(kind, data):
        assert kind in events, "Audit must not overtake event persistence"

    await SubagentOrchestrator(_RecordingClient()).delegate(
        task, assignments, allowed_ids=allowed, cancel=asyncio.Event(),
        emit=emit, audit=audit, redact=lambda value: value,
    )
    assert events == ["subagent_dispatch_started", "subagent_started", "subagent_completed"]
