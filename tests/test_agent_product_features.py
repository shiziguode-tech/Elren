import asyncio
from pathlib import Path

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate
from deepdesk.models import AgentProfile, AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.project_rules import (
    clear_project_rules_cache,
    load_project_rules,
    load_project_rules_with_metadata,
)


def test_project_rules_are_bounded_and_only_use_supported_local_locations(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use atomic writes.", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Run focused tests first.", encoding="utf-8")
    (tmp_path / "random-instructions.md").write_text("must not load", encoding="utf-8")
    cursor = tmp_path / ".cursor" / "rules"
    cursor.mkdir(parents=True)
    (cursor / "python.mdc").write_text("Prefer type hints." * 200, encoding="utf-8")

    content, files = load_project_rules(str(tmp_path), max_chars=420)

    assert files[:2] == ["AGENTS.md", "CLAUDE.md"]
    assert ".cursor/rules/python.mdc" in files
    assert "random-instructions.md" not in files
    assert "Use atomic writes." in content
    assert len(content) <= 420


def test_project_rules_use_content_fingerprint_cache_and_invalidate_on_change(tmp_path: Path):
    clear_project_rules_cache()
    rules = tmp_path / "AGENTS.md"
    rules.write_text("Run the focused test.", encoding="utf-8")

    first_content, first_files, first_meta = load_project_rules_with_metadata(str(tmp_path))
    second_content, second_files, second_meta = load_project_rules_with_metadata(str(tmp_path))

    assert first_content == second_content
    assert first_files == second_files == ["AGENTS.md"]
    assert first_meta["cache_hit"] is False
    assert second_meta["cache_hit"] is True
    assert first_meta["fingerprint"] == second_meta["fingerprint"]

    rules.write_text("Run the complete regression test.", encoding="utf-8")
    changed_content, _files, changed_meta = load_project_rules_with_metadata(str(tmp_path))
    assert "complete regression" in changed_content
    assert changed_meta["cache_hit"] is False
    assert changed_meta["fingerprint"] != first_meta["fingerprint"]


@pytest.mark.asyncio
async def test_planner_profile_blocks_state_changing_tools_without_approval(tmp_path: Path):
    class WriteTool(ToolPlugin):
        name = "write_test"
        description = "Test write"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        def __init__(self):
            self.executed = False

        def risk(self, arguments):
            return Risk.MEDIUM

        async def execute(self, arguments, context):
            self.executed = True
            return {"ok": True}

    class Client:
        keys: list[str] = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return DeepSeekReply(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "planner-write",
                            "type": "function",
                            "function": {"name": "write_test", "arguments": "{}"},
                        }],
                    },
                    usage={},
                    active_key="test",
                )
            return DeepSeekReply(
                message={"role": "assistant", "content": "Read-only plan complete", "tool_calls": []},
                usage={},
                active_key="test",
            )

    tool = WriteTool()
    registry = PluginRegistry()
    registry.register(tool)
    task = AgentTask(prompt="Plan this change", agent_profile=AgentProfile.PLANNER)
    engine = AgentEngine(
        client=Client(),
        registry=registry,
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=3,
        emit=lambda current, event_type, data: current.events.append(TaskEvent(type=event_type, data=data)),
    )

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert not tool.executed
    assert any(event.type == "tool_rejected" and event.data["reason"] == "planner_read_only_boundary" for event in task.events)


@pytest.mark.asyncio
async def test_filesystem_write_and_append_are_atomic_and_leave_no_temp_files(tmp_path: Path):
    tool = FileSystemTool()
    context = ToolContext(task_id="atomic-test", workspace=str(tmp_path))

    first = await tool.execute({"action": "write", "path": "notes.txt", "content": "alpha"}, context)
    second = await tool.execute({"action": "append", "path": "notes.txt", "content": "-beta"}, context)

    assert first["atomic"] is True
    assert second["atomic"] is True
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "alpha-beta"
    assert not list(tmp_path.glob(".notes.txt.elren-*.tmp"))
