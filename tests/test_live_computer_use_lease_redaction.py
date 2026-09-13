from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk.audit import AuditLog, sanitize_audit_value
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import (
    AgentEngine,
    ApprovalGate,
    HumanActionGate,
    _redact_session_lease_values,
)
from deepdesk.models import AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolPlugin
from deepdesk.task_store import TaskStore

LEASE = "live-control-lease-7b4b5dce22ce4d178a65"


class LeaseTool(ToolPlugin):
    name = "lease_test_tool"
    description = "Exercise the live control lease handoff"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "status"]},
            "session_lease": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.status_received_raw_lease = False

    def risk(self, arguments):
        return Risk.SAFE

    async def execute(self, arguments, context):
        del context
        if arguments["action"] == "start":
            return {"started": True, "session_lease": LEASE}
        self.status_received_raw_lease = arguments.get("session_lease") == LEASE
        return {
            "active": True,
            "session_lease": LEASE,
            "detail": f"runtime bearer {LEASE}",
        }


class LeaseRoundTripClient:
    keys: list[str] = []

    def __init__(self) -> None:
        self.calls = 0
        self.model_received_raw_lease = False

    async def chat(self, messages, tools):
        del tools
        self.calls += 1
        if self.calls == 1:
            return DeepSeekReply(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "lease-start",
                            "type": "function",
                            "function": {
                                "name": "lease_test_tool",
                                "arguments": '{"action":"start"}',
                            },
                        }
                    ],
                },
                usage={},
                active_key="test",
            )
        if self.calls == 2:
            start_result = next(
                item for item in messages if item.get("tool_call_id") == "lease-start"
            )
            self.model_received_raw_lease = LEASE in str(start_result.get("content"))
            return DeepSeekReply(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "lease-status",
                            "type": "function",
                            "function": {
                                "name": "lease_test_tool",
                                "arguments": json.dumps(
                                    {"action": "status", "session_lease": LEASE}
                                ),
                            },
                        }
                    ],
                },
                usage={},
                active_key="test",
            )
        return DeepSeekReply(
            message={
                "role": "assistant",
                # Deliberately echo the bare value to prove known-value
                # replacement protects final task persistence too.
                "content": f"finished with runtime bearer {LEASE}",
                "tool_calls": [],
            },
            usage={},
            active_key="test",
        )


def _engine(tmp_path: Path, client, tool: ToolPlugin | None = None) -> AgentEngine:
    registry = PluginRegistry()
    if tool is not None:
        registry.register(tool)
    return AgentEngine(
        client=client,
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )


def test_session_lease_redactor_is_recursive_and_does_not_hide_task_id() -> None:
    value = {
        "task_id": "task-visible",
        "session_lease": LEASE,
        "nested": [
            {"Session-Lease": LEASE},
            f'{{"session_lease":"{LEASE}","task_id":"task-visible"}}',
            f"unlabelled known bearer {LEASE}",
        ],
    }

    redacted = _redact_session_lease_values(value, {LEASE})
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert LEASE not in serialized
    assert serialized.count("[REDACTED]") >= 3
    assert redacted["task_id"] == "task-visible"


def test_audit_sanitizer_always_redacts_structural_session_lease() -> None:
    sanitized = sanitize_audit_value(
        {
            "task_id": "task-visible",
            "session_lease": LEASE,
            "encoded": f'{{"session_lease":"{LEASE}"}}',
        }
    )
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert LEASE not in serialized
    assert sanitized["task_id"] == "task-visible"


@pytest.mark.asyncio
async def test_model_keeps_runtime_lease_while_events_audit_and_result_redact_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def local_region(_detector):
        return SimpleNamespace(country_code="US")

    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", local_region)
    tool = LeaseTool()
    client = LeaseRoundTripClient()
    engine = _engine(tmp_path, client, tool)
    task = AgentTask(prompt="exercise lease continuity")

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert client.model_received_raw_lease
    assert tool.status_received_raw_lease
    assert LEASE not in task.result
    assert "[REDACTED]" in task.result
    event_text = json.dumps(
        [event.model_dump(mode="json") for event in task.events],
        ensure_ascii=False,
    )
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    store = TaskStore(tmp_path / "tasks.sqlite3")
    store.save(task)
    persisted = store.get(task.id)
    assert persisted is not None
    persisted_text = persisted.model_dump_json()
    assert LEASE not in event_text
    assert LEASE not in audit_text
    assert LEASE not in persisted_text
    assert "[REDACTED]" in event_text
    assert "[REDACTED]" in audit_text


def test_context_checkpoint_archives_redact_lease_but_keep_task_id(tmp_path: Path) -> None:
    client = SimpleNamespace(keys=[])
    engine = _engine(tmp_path, client)
    task = AgentTask(prompt="continue live control")
    engine._remember_session_leases(task.id, {"session_lease": LEASE})
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": f"using bearer {LEASE}",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "live_computer_use",
                        "arguments": json.dumps(
                            {"action": "status", "session_lease": LEASE}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps(
                {"ok": True, "result": {"session_lease": LEASE}}
            ),
        },
    ]
    archives: list[str] = []

    compacted, _details = engine._compact_context_messages(
        task,
        messages,
        checkpoint_number=1,
        reason="test",
        cumulative_ledger=[],
        checkpoint_archives=archives,
        runtime_state={"session_lease": LEASE, "task_id": task.id},
        target_tokens=20_000,
    )

    archive_text = Path(archives[0]).read_text(encoding="utf-8")
    compacted_text = json.dumps(compacted, ensure_ascii=False)
    assert LEASE not in archive_text
    assert LEASE not in compacted_text
    assert task.id in archive_text
    assert task.id in compacted_text
    assert "[REDACTED]" in archive_text
