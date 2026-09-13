from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deepdesk.models import AgentTask
from deepdesk.subagents import (
    MAX_ASSIGNMENT_CHARS,
    MAX_CANDIDATES,
    MAX_CONCURRENCY,
    MAX_PER_DISPATCH,
    SubagentOrchestrator,
    SubagentValidationError,
    delegate_specialists_schema,
    load_subagent_catalog,
    search_specialists,
    should_offer_delegation,
    specialist_tool_schemas,
)
from deepdesk.task_store import TaskStore


@dataclass(slots=True)
class _Reply:
    message: dict[str, Any]


class _RecordingClient:
    """Network-free client that records child isolation and concurrency."""

    def __init__(
        self,
        *,
        delay: float = 0.02,
        fail_name: str = "",
        block: bool = False,
    ) -> None:
        self.delay = delay
        self.fail_name = fail_name
        self.block = block
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.started_count = 0
        self.started = asyncio.Event()
        self._never = asyncio.Event()

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.calls.append(
            {
                "messages_id": id(messages),
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
            }
        )
        self.active += 1
        self.started_count += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        system = str(messages[0].get("content") or "")
        user = str(messages[1].get("content") or "")
        try:
            if self.block:
                await self._never.wait()
            else:
                await asyncio.sleep(self.delay)
            if self.fail_name and self.fail_name in system:
                raise RuntimeError("one specialist failed")
            assignment = user.split("MAIN AGENT ASSIGNMENT:\n", 1)[-1]
            assignment = assignment.split("\n\nEXPECTED EVIDENCE", 1)[0].strip()
            return _Reply(
                message={
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": f"report for {assignment}",
                            "evidence": [f"evidence for {assignment}"],
                            "artifacts": ["artifact.txt"],
                            "risks": ["bounded risk"],
                            "confidence": 0.8,
                            "needs_approval": False,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        finally:
            self.active -= 1


def _task(prompt: str = "全面审核、实现并测试这个复杂版本") -> AgentTask:
    return AgentTask(
        prompt=prompt,
        active_model="fake-model",
        reasoning_effort="high",
        interface_language="zh",
    )


def _assignments(count: int) -> tuple[list[dict[str, str]], set[str]]:
    presets = list(load_subagent_catalog()[:count])
    return (
        [
            {
                "preset_id": preset.id,
                "assignment": f"independent assignment {index} for {preset.id}",
                "expected_output": f"evidence set {index}",
            }
            for index, preset in enumerate(presets)
        ],
        {preset.id for preset in presets},
    )


def _event_sinks():
    emitted: list[tuple[str, dict[str, Any]]] = []
    audited: list[tuple[str, dict[str, Any]]] = []

    def emit(event_type: str, data: dict[str, Any]) -> None:
        emitted.append((event_type, copy.deepcopy(data)))

    async def audit(event_type: str, data: dict[str, Any]) -> None:
        audited.append((event_type, copy.deepcopy(data)))

    return emitted, audited, emit, audit


async def _delegate(
    orchestrator: SubagentOrchestrator,
    task: AgentTask,
    assignments: Any,
    allowed_ids: set[str],
    *,
    cancel: asyncio.Event | None = None,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    emitted, audited, emit, audit = _event_sinks()
    result = await orchestrator.delegate(
        task,
        assignments,
        allowed_ids=allowed_ids,
        cancel=cancel or asyncio.Event(),
        emit=emit,
        audit=audit,
        redact=lambda value: value,
    )
    return result, emitted, audited


def test_simple_prompt_does_not_expose_delegation_tools() -> None:
    prompt = "你好"

    assert should_offer_delegation(prompt) is False
    candidates = search_specialists(prompt)
    assert candidates == []
    assert specialist_tool_schemas(candidates) == []


def test_complex_prompt_exposes_only_a_bounded_candidate_shortlist() -> None:
    prompt = "深入调研并改版界面，同时做安全、性能、回归测试和发布审核"

    assert should_offer_delegation(prompt) is True
    candidates = search_specialists(prompt, limit=10_000)
    catalog = load_subagent_catalog()
    assert 1 <= len(candidates) <= MAX_CANDIDATES
    assert len(candidates) < len(catalog) == 99

    schema = delegate_specialists_schema(candidates)
    preset_schema = schema["function"]["parameters"]["properties"]["assignments"][
        "items"
    ]["properties"]["preset_id"]
    candidate_ids = [preset.id for preset in candidates]
    assert preset_schema["enum"] == candidate_ids
    assert set(preset_schema["enum"]) == set(candidate_ids)
    assert not ({preset.id for preset in catalog} - set(candidate_ids)) & set(
        preset_schema["enum"]
    )


@pytest.mark.asyncio
async def test_concurrency_is_a_hard_host_cap_of_three() -> None:
    assignments, allowed_ids = _assignments(MAX_PER_DISPATCH)
    client = _RecordingClient(delay=0.05)
    # A caller-provided value must never widen the production safety cap.
    orchestrator = SubagentOrchestrator(client, concurrency=10_000)

    result, _emitted, _audited = await _delegate(
        orchestrator, _task(), assignments, allowed_ids
    )

    assert result["completed_count"] == MAX_PER_DISPATCH
    assert client.max_active == MAX_CONCURRENCY
    assert client.max_active <= 3


@pytest.mark.asyncio
async def test_each_specialist_receives_independent_messages_and_no_tools() -> None:
    assignments, allowed_ids = _assignments(3)
    client = _RecordingClient()

    result, _emitted, _audited = await _delegate(
        SubagentOrchestrator(client), _task(), assignments, allowed_ids
    )

    assert result["completed_count"] == 3
    assert len(client.calls) == 3
    assert len({call["messages_id"] for call in client.calls}) == 3
    assert all(call["tools"] == [] for call in client.calls)
    assert all(len(call["messages"]) == 2 for call in client.calls)
    assert all(
        [message["role"] for message in call["messages"]] == ["system", "user"]
        for call in client.calls
    )
    user_messages = [str(call["messages"][1]["content"]) for call in client.calls]
    for item in assignments:
        containing = [text for text in user_messages if item["assignment"] in text]
        assert len(containing) == 1
        assert all(
            other["assignment"] not in containing[0]
            for other in assignments
            if other is not item
        )


@pytest.mark.asyncio
async def test_structured_reports_are_persisted_in_task_store(tmp_path: Path) -> None:
    assignments, allowed_ids = _assignments(2)
    task = _task()

    result, emitted, audited = await _delegate(
        SubagentOrchestrator(_RecordingClient()), task, assignments, allowed_ids
    )

    assert result["completed_count"] == 2
    assert all(run.status == "completed" for run in task.subagent_runs)
    assert all(run.summary.startswith("report for ") for run in task.subagent_runs)
    assert all(run.evidence and run.artifacts and run.risks for run in task.subagent_runs)
    assert all(run.confidence == pytest.approx(0.8) for run in task.subagent_runs)
    assert [name for name, _data in emitted].count("subagent_completed") == 2
    assert [name for name, _data in audited].count("subagent_completed") == 2

    store = TaskStore(tmp_path / "tasks.db")
    store.save(task)
    restored = store.get(task.id)
    assert restored is not None
    assert [run.model_dump() for run in restored.subagent_runs] == [
        run.model_dump() for run in task.subagent_runs
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assignments_factory", "message"),
    [
        (
            lambda values: [
                {
                    "preset_id": "not_a_real_preset",
                    "assignment": "inspect one bounded concern",
                }
            ],
            "Unknown or inactive",
        ),
        (
            lambda values: [values[0], {**values[0], "assignment": "different work"}],
            "only once",
        ),
        (
            lambda values: [values[0], {**values[1], "assignment": values[0]["assignment"]}],
            "Duplicate specialist assignments",
        ),
        (
            lambda values: [
                *values,
                {
                    "preset_id": load_subagent_catalog()[4].id,
                    "assignment": "fifth assignment",
                },
            ],
            "between 1 and 4",
        ),
    ],
)
async def test_unknown_duplicate_and_over_count_dispatches_are_rejected_without_runs(
    assignments_factory,
    message: str,
) -> None:
    base, allowed_ids = _assignments(4)
    attempted = assignments_factory(base)
    allowed_ids |= {str(item.get("preset_id") or "") for item in attempted}
    task = _task()
    emitted, audited, emit, audit = _event_sinks()

    with pytest.raises(SubagentValidationError, match=message):
        await SubagentOrchestrator(_RecordingClient()).delegate(
            task,
            attempted,
            allowed_ids=allowed_ids,
            cancel=asyncio.Event(),
            emit=emit,
            audit=audit,
            redact=lambda value: value,
        )

    assert task.subagent_runs == []
    assert emitted == []
    assert audited == []


@pytest.mark.asyncio
async def test_oversized_assignment_is_rejected_instead_of_silently_truncated() -> None:
    assignments, allowed_ids = _assignments(1)
    assignments[0]["assignment"] = "x" * (MAX_ASSIGNMENT_CHARS + 1)
    task = _task()

    with pytest.raises(SubagentValidationError, match="assignment"):
        await _delegate(
            SubagentOrchestrator(_RecordingClient()),
            task,
            assignments,
            allowed_ids,
        )

    assert task.subagent_runs == []


@pytest.mark.asyncio
async def test_one_specialist_failure_does_not_cancel_successful_siblings() -> None:
    assignments, allowed_ids = _assignments(3)
    failing_preset = load_subagent_catalog()[1]
    client = _RecordingClient(fail_name=failing_preset.name_en)
    task = _task()

    result, emitted, _audited = await _delegate(
        SubagentOrchestrator(client), task, assignments, allowed_ids
    )

    assert result["completed_count"] == 2
    assert result["failed_count"] == 1
    assert [run.status for run in task.subagent_runs].count("failed") == 1
    assert [run.status for run in task.subagent_runs].count("completed") == 2
    assert [name for name, _data in emitted].count("subagent_failed") == 1
    assert [name for name, _data in emitted].count("subagent_completed") == 2
    assert len(result["results"]) == 3


@pytest.mark.asyncio
async def test_parent_cancel_cancels_and_marks_every_child_run() -> None:
    assignments, allowed_ids = _assignments(MAX_PER_DISPATCH)
    client = _RecordingClient(block=True)
    orchestrator = SubagentOrchestrator(client)
    task = _task()
    cancel = asyncio.Event()
    emitted, audited, emit, audit = _event_sinks()

    delegation = asyncio.create_task(
        orchestrator.delegate(
            task,
            assignments,
            allowed_ids=allowed_ids,
            cancel=cancel,
            emit=emit,
            audit=audit,
            redact=lambda value: value,
        )
    )
    for _ in range(100):
        if client.started_count >= MAX_CONCURRENCY:
            break
        await asyncio.sleep(0.005)
    assert client.started_count == MAX_CONCURRENCY

    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await delegation

    assert len(task.subagent_runs) == MAX_PER_DISPATCH
    assert all(run.status == "cancelled" for run in task.subagent_runs)
    assert all(run.error_code == "parent_cancelled" for run in task.subagent_runs)
    assert [name for name, _data in emitted].count("subagent_cancelled") == MAX_PER_DISPATCH
    assert [name for name, _data in audited].count("subagent_cancelled") == MAX_PER_DISPATCH
    assert client.active == 0
