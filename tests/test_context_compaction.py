from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import ContextWindowError, DeepSeekReply
from deepdesk.engine import (
    CONTEXT_COMPACTION_RATIO,
    CONTEXT_COMPACTION_TARGET_RATIO,
    AgentEngine,
    ApprovalGate,
    HumanActionGate,
)
from deepdesk.models import AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolPlugin


def _emit(task: AgentTask, kind: str, data: dict) -> None:
    task.events.append(TaskEvent(type=kind, data=data))


def _engine(tmp_path: Path, client) -> AgentEngine:
    return AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=6,
        emit=_emit,
    )


def test_compaction_threshold_is_near_limit_and_target_keeps_substantial_context():
    assert CONTEXT_COMPACTION_RATIO >= 0.80
    assert 0.40 <= CONTEXT_COMPACTION_TARGET_RATIO <= 0.60


def test_recursive_checkpoints_keep_all_prior_archives_and_redact_new_secrets(tmp_path: Path):
    class Client:
        keys = []

    engine = _engine(tmp_path, Client())
    task = AgentTask(prompt="Finish the original multi-stage task")
    ledger: list[str] = []
    archives: list[str] = []
    first_messages = [
        {"role": "system", "content": "system constraints"},
        {"role": "user", "content": task.prompt},
        {
            "role": "assistant",
            "content": "stage one finished; output C:/work/one.txt",
            "tool_calls": [{
                "id": "one", "type": "function",
                "function": {
                    "name": "update_settings",
                    "arguments": json.dumps({"api_key": "new-secret-value"}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "one", "content": "first evidence"},
    ]
    compacted_one, info_one = engine._compact_context_messages(
        task,
        first_messages,
        checkpoint_number=1,
        reason="test",
        cumulative_ledger=ledger,
        checkpoint_archives=archives,
        runtime_state={"step": 2},
        target_tokens=50_000,
    )
    compacted_one.extend([
        {"role": "assistant", "content": "stage two finished; output C:/work/two.txt"},
        {"role": "tool", "tool_call_id": "two", "content": "second evidence"},
    ])
    compacted_two, info_two = engine._compact_context_messages(
        task,
        compacted_one,
        checkpoint_number=2,
        reason="test_again",
        cumulative_ledger=ledger,
        checkpoint_archives=archives,
        runtime_state={"step": 4},
        target_tokens=50_000,
    )

    checkpoint = compacted_two[1]["content"]
    assert task.prompt in checkpoint
    assert "stage one finished" in checkpoint
    assert "stage two finished" in checkpoint
    assert info_two["archive_count"] == 2
    assert all(path in checkpoint for path in archives)
    assert all(Path(path).is_file() for path in archives)
    assert "new-secret-value" not in checkpoint
    assert "new-secret-value" not in Path(info_one["archive"]).read_text(encoding="utf-8")


def test_compaction_counts_only_schemas_sent_to_the_model(tmp_path: Path):
    class LargeTool(ToolPlugin):
        parameters = {
            "type": "object",
            "properties": {"payload": {"type": "string", "description": "x" * 4_000}},
        }

        def __init__(self, index: int) -> None:
            self.name = f"large_tool_{index}"
            self.description = "Deferred tool " + ("detail " * 500)

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):  # pragma: no cover - not invoked
            return {"ok": True}

    class Client:
        keys = []

    engine = _engine(tmp_path, Client())
    for index in range(20):
        engine.registry.register(LargeTool(index))
    active_schemas = [engine.registry.schemas()[0]]
    task = AgentTask(prompt="Preserve the current implementation evidence")
    messages = [
        {"role": "system", "content": "system constraints"},
        {"role": "user", "content": task.prompt},
        {"role": "assistant", "content": "important evidence " * 1_000},
    ]
    expected_active = engine._estimate_context_tokens(messages, active_schemas)
    expected_full = engine._estimate_context_tokens(messages, engine.registry.schemas())

    compacted, info = engine._compact_context_messages(
        task,
        messages,
        checkpoint_number=1,
        reason="active_schema_regression",
        cumulative_ledger=[],
        checkpoint_archives=[],
        runtime_state={"step": 2},
        target_tokens=20_000,
        active_schemas=active_schemas,
    )

    assert expected_full > expected_active
    assert info["before_estimated_tokens"] == expected_active
    assert info["before_estimated_tokens"] != expected_full
    assert info["after_estimated_tokens"] == engine._estimate_context_tokens(
        compacted, active_schemas
    )


@pytest.mark.asyncio
async def test_engine_preemptively_compacts_and_continues_without_stopping(tmp_path: Path):
    class Client:
        keys = []

        def __init__(self):
            self.calls = 0
            self.second_messages = []

        def context_window_tokens(self):
            return 16_000

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "large", "type": "function",
                            "function": {"name": "unknown_tool", "arguments": json.dumps({"data": "x" * 60_000})},
                        }],
                    },
                    usage={"prompt_tokens": 100}, active_key="test",
                )
            self.second_messages = messages
            return DeepSeekReply(
                message={"role": "assistant", "content": "completed after compaction", "tool_calls": []},
                usage={"prompt_tokens": 8_000}, active_key="test",
            )

    client = Client()
    task = AgentTask(prompt="Complete the long task")
    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "completed after compaction"
    assert any(event.type == "context_compacted" for event in task.events)
    assert "AUTOMATIC CONTEXT COMPACTION CHECKPOINT" in client.second_messages[1]["content"]


@pytest.mark.asyncio
async def test_provider_overflow_triggers_checkpoint_recovery_and_continues(tmp_path: Path):
    class Client:
        keys = []

        def __init__(self):
            self.calls = 0

        def context_window_tokens(self):
            return 1_000_000

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise ContextWindowError("provider context window exceeded")
            assert "AUTOMATIC CONTEXT COMPACTION CHECKPOINT" in messages[1]["content"]
            return DeepSeekReply(
                message={"role": "assistant", "content": "recovered", "tool_calls": []},
                usage={}, active_key="test",
            )

    client = Client()
    task = AgentTask(prompt="Continue even after context overflow")
    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert client.calls == 2
    assert task.status == TaskStatus.COMPLETED
    event = next(event for event in task.events if event.type == "context_compacted")
    assert event.data["reason"] == "provider_context_limit_recovery"


@pytest.mark.asyncio
async def test_initial_cross_chat_overflow_is_compacted_preserved_and_continues(tmp_path: Path):
    class Client:
        keys = []

        def __init__(self):
            self.calls = 0
            self.messages = []

        def context_window_tokens(self):
            return 16_000

        async def chat(self, messages, tools):
            self.calls += 1
            self.messages = messages
            checkpoint = messages[1]["content"]
            assert "AUTOMATIC CONTEXT COMPACTION CHECKPOINT" in checkpoint
            assert "CROSS-CONVERSATION REFERENCE" in checkpoint
            assert "important earlier decision" in checkpoint
            assert "current cross-chat request" in checkpoint
            return DeepSeekReply(
                message={"role": "assistant", "content": "continued after cross-chat compaction", "tool_calls": []},
                usage={"prompt_tokens": 7_000}, active_key="test",
            )

    client = Client()
    task = AgentTask(
        prompt="current cross-chat request",
        cross_conversation_context=(
            "important earlier decision: retain the verified output path\n" + "历史证据" * 30_000
        ),
    )
    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert client.calls == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.result == "continued after cross-chat compaction"
    event = next(event for event in task.events if event.type == "context_compacted")
    assert event.data["reason"] == "preemptive_near_context_limit"
    assert event.data["cross_context_preserved"] is True
    assert Path(event.data["archive"]).is_file()


@pytest.mark.asyncio
async def test_repeated_provider_overflow_progressively_shrinks_and_recovers(tmp_path: Path):
    class Client:
        keys = []

        def __init__(self):
            self.calls = 0

        def context_window_tokens(self):
            return 1_000_000

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls < 4:
                raise ContextWindowError("relay uses a smaller real context window")
            return DeepSeekReply(
                message={"role": "assistant", "content": "recovered after progressive compaction", "tool_calls": []},
                usage={}, active_key="test",
            )

    client = Client()
    task = AgentTask(prompt="Keep recovering without asking the user to continue")
    await _engine(tmp_path, client).run(task, asyncio.Event())

    targets = [
        event.data["target_tokens"]
        for event in task.events
        if event.type == "context_compacted"
        and event.data["reason"] == "provider_context_limit_recovery"
    ]
    assert client.calls == 4
    assert task.status == TaskStatus.COMPLETED
    assert targets == sorted(targets, reverse=True)
    assert len(set(targets)) == 3
    assert targets[-1] < targets[0]


@pytest.mark.asyncio
async def test_irreducible_provider_overflow_fails_instead_of_compacting_forever(
    tmp_path: Path,
):
    class Client:
        keys = []

        def __init__(self):
            self.calls = 0

        def context_window_tokens(self):
            return 16_000

        async def chat(self, messages, tools):
            self.calls += 1
            raise ContextWindowError("provider still rejects the minimum checkpoint")

    client = Client()
    task = AgentTask(prompt="This request is already much smaller than the context window")

    await asyncio.wait_for(
        _engine(tmp_path, client).run(task, asyncio.Event()),
        timeout=2,
    )

    compactions = [
        event for event in task.events if event.type == "context_compacted"
    ]
    assert task.status == TaskStatus.FAILED
    assert "无法进一步压缩" in (task.error or "")
    assert 3 <= client.calls <= 10
    assert len(compactions) == client.calls
