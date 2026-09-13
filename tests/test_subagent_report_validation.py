from __future__ import annotations

import asyncio
import json

import pytest

from deepdesk.deepseek import DeepSeekReply
from deepdesk.models import AgentTask
from deepdesk.subagents import SubagentOrchestrator, _parse_report, load_subagent_catalog


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"])
def test_nonfinite_confidence_is_not_upgraded_to_certainty(value):
    report = _parse_report(json.dumps({"summary": "Uncertain", "confidence": value}))
    assert report["confidence"] == 0.5


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), ("false", False), ("true", True)])
def test_approval_flag_is_not_python_string_truthiness(value, expected):
    report = _parse_report(json.dumps({"summary": "Report", "needs_approval": value}))
    assert report["needs_approval"] is expected


@pytest.mark.parametrize("status", ["failed", "blocked"])
@pytest.mark.asyncio
async def test_reported_failure_never_becomes_success(status):
    class Client:
        async def chat(self, messages, tools):
            return DeepSeekReply(message={"role": "assistant", "content": json.dumps({
                "status": status, "summary": "Cannot verify the requested evidence",
                "confidence": 0.1, "needs_approval": False,
            })}, usage={}, active_key="synthetic")

    preset = load_subagent_catalog()[0]
    task = AgentTask(prompt="Review evidence", active_model="test-model")
    events = []

    async def audit(kind, data):
        pass

    result = await SubagentOrchestrator(Client()).delegate(
        task, [{"preset_id": preset.id, "assignment": "Verify missing evidence"}],
        allowed_ids={preset.id}, cancel=asyncio.Event(), emit=lambda kind, data: events.append(kind),
        audit=audit, redact=lambda value: value,
    )
    assert task.subagent_runs[0].status == "failed"
    assert task.subagent_runs[0].error_code == f"report_{status}"
    assert "subagent_failed" in events
    assert "subagent_completed" not in events
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cancelling_delegate_coroutine_cancels_and_joins_children():
    started = asyncio.Event()
    finished = asyncio.Event()

    class Client:
        async def chat(self, messages, tools):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    preset = load_subagent_catalog()[0]
    task = AgentTask(prompt="Review", active_model="test-model")

    async def audit(kind, data):
        pass

    before = asyncio.all_tasks()
    parent = asyncio.create_task(SubagentOrchestrator(Client()).delegate(
        task, [{"preset_id": preset.id, "assignment": "Review this case"}],
        allowed_ids={preset.id}, cancel=asyncio.Event(), emit=lambda *args: None,
        audit=audit, redact=lambda value: value,
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert finished.is_set(), "child API stream outlived the cancelled caller"
        assert task.subagent_runs[0].status == "cancelled"
    finally:
        pending = [job for job in asyncio.all_tasks() - before if not job.done()]
        for job in pending:
            job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_assignment_is_separate_from_parent_delegation_request():
    captured = []

    class Client:
        async def chat(self, messages, tools):
            captured.extend(messages)
            assert tools == []
            return DeepSeekReply(message={"role": "assistant", "content": json.dumps({
                "status": "completed", "summary": "Empty lists divide by zero; not executed.",
            })}, usage={}, active_key="synthetic")

    preset = load_subagent_catalog()[0]
    task = AgentTask(prompt="Delegate two specialists then combine reports. def mean(xs): return sum(xs)/len(xs)")

    async def audit(kind, data):
        pass

    await SubagentOrchestrator(Client()).delegate(
        task, [{"preset_id": preset.id, "assignment": "Analyze empty input; do not execute."}],
        allowed_ids={preset.id}, cancel=asyncio.Event(), emit=lambda *args: None,
        audit=audit, redact=lambda value: value,
    )
    system, context = [m["content"] for m in captured]
    assert "Complete only MAIN AGENT ASSIGNMENT" in system
    assert "CURRENT USER REQUEST is background context" in system
    assert "If your own assignment requires missing evidence or actual execution" in system
    assert task.prompt in context and task.subagent_runs[0].assignment in context
    assert task.subagent_runs[0].status == "completed"
