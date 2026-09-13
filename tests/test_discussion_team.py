from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate
from deepdesk.main import resolve_task_model_preference
from deepdesk.models import AgentTask, DiscussionTeamMember, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry, ToolContext, ToolPlugin
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch


def _team() -> list[DiscussionTeamMember]:
    return [
        DiscussionTeamMember(
            id="lead",
            name="Leader",
            role="leader",
            model="leader-model",
            reasoning_effort="xhigh",
            assignment="Decide and verify",
        ),
        DiscussionTeamMember(
            id="member",
            name="Builder",
            role="member",
            model="member-model",
            reasoning_effort="low",
            assignment="Implement the requested change",
        ),
    ]


def test_runtime_settings_requires_two_people_and_exactly_one_leader() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(
            discussion_team_enabled=True,
            discussion_team=[_team()[0]],
        )
    with pytest.raises(ValidationError):
        RuntimeSettings(
            discussion_team_enabled=True,
            discussion_team=[
                _team()[0],
                _team()[1].model_copy(update={"role": "leader"}),
            ],
        )
    assert RuntimeSettings(
        discussion_team_enabled=False,
        discussion_team=_team(),
    ).discussion_team_enabled
    assert RuntimeSettings(discussion_team_enabled=True).discussion_team_enabled is False
    assert RuntimeSettingsPatch(
        discussion_team_enabled=False,
        discussion_team=_team(),
    ).discussion_team_enabled is True
    assert RuntimeSettingsPatch(
        discussion_team_enabled=True,
        discussion_team=[],
    ).discussion_team_enabled is False


def test_settings_patch_rejects_version_mismatches_but_keeps_retired_agent_compatibility() -> None:
    patch = RuntimeSettingsPatch.model_validate({"default_agent": "coder"})
    assert patch.default_agent == "coder"
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch.model_validate({"future_field_from_mismatched_ui": True})


def test_model_picker_team_selection_resolves_to_saved_leader_model() -> None:
    runtime = RuntimeSettings(
        discussion_team_enabled=True,
        discussion_team=_team(),
    )
    assert resolve_task_model_preference("discussion-team", runtime) == (
        True,
        "leader-model",
    )
    assert resolve_task_model_preference("deepseek-v4-flash", runtime) == (
        False,
        "deepseek-v4-flash",
    )
    with pytest.raises(ValueError, match="尚未在设置中完成配置"):
        resolve_task_model_preference(
            "discussion-team",
            RuntimeSettings(discussion_team_enabled=False),
        )


@pytest.mark.asyncio
async def test_team_discusses_hands_off_and_leader_finalizes(tmp_path: Path) -> None:
    class TeamClient:
        keys: list[str] = []
        default_model_preference = "auto"

        def __init__(self) -> None:
            self.current = "leader-model"
            self.models: list[str] = []
            self.current_reasoning = "high"
            self.reasoning_turns: list[str] = []
            self.member_execution_turns = 0
            self.system_prompts: list[str] = []

        def bind_task_model(self, model: str):
            previous = self.current
            self.current = model
            return previous

        def reset_task_model(self, token) -> None:
            self.current = token

        def bind_task_reasoning_effort(self, effort: str):
            previous = self.current_reasoning
            self.current_reasoning = effort
            return previous

        def reset_task_reasoning_effort(self, token) -> None:
            self.current_reasoning = token

        async def chat(self, messages, tools):
            self.models.append(self.current)
            self.reasoning_turns.append(self.current_reasoning)
            system_text = "\n".join(
                str(item.get("content") or "")
                for item in messages
                if item.get("role") == "system"
            )
            self.system_prompts.append(system_text)
            if "Propose a concrete execution plan" in system_text:
                content = "Builder implements; Leader verifies."
            elif "Review the leader proposal" in system_text:
                content = "Check the regression tests after implementation."
            elif "final decision maker" in system_text:
                content = "Builder implements and tests, then Leader verifies and reports."
            elif "CURRENT TEAM SPEAKER: Builder" in system_text:
                self.member_execution_turns += 1
                if self.member_execution_turns == 1:
                    return DeepSeekReply(
                        message={
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "team-tool-1",
                                    "type": "function",
                                    "function": {"name": "proof", "arguments": '{"value":"member-used-tools"}'},
                                }
                            ],
                        },
                        usage={},
                        active_key="test",
                    )
                content = "Implementation phase complete with regression evidence."
            else:
                content = "All evidence reviewed; task complete."
            return DeepSeekReply(
                message={"role": "assistant", "content": content, "tool_calls": []},
                usage={},
                active_key="test",
            )

    class ProofTool(ToolPlugin):
        name = "proof"
        description = "Record execution evidence"
        parameters = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

        def __init__(self) -> None:
            self.calls: list[str] = []

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context: ToolContext):
            self.calls.append(arguments["value"])
            return {"ok": True, "value": arguments["value"]}

    client = TeamClient()
    proof = ProofTool()
    registry = PluginRegistry()
    registry.register(proof)
    task = AgentTask(
        prompt="Implement a change and test it",
        model_preference="leader-model",
        active_model="leader-model",
        discussion_team_enabled=True,
        discussion_team=_team(),
    )
    engine = AgentEngine(
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

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "All evidence reviewed; task complete."
    event_types = [event.type for event in task.events]
    assert "team_discussion_started" in event_types
    assert "team_leader_proposal" in event_types
    assert "team_member_advice" in event_types
    assert "team_consensus" in event_types
    assert "team_member_report" in event_types
    assert client.reasoning_turns[:3] == ["xhigh", "low", "xhigh"]
    assert all(
        effort == ("low" if model == "member-model" else "xhigh")
        for model, effort in zip(client.models, client.reasoning_turns, strict=True)
    )
    assert client.models[:3] == ["leader-model", "member-model", "leader-model"]
    assert client.models[-3:] == ["member-model", "member-model", "leader-model"]
    assert proof.calls == ["member-used-tools"]
    assert any("distinct adversarial QA phase" in prompt for prompt in client.system_prompts)


@pytest.mark.asyncio
async def test_unavailable_leader_uses_acting_leader_then_restores_original(
    tmp_path: Path,
) -> None:
    class FailoverTeamClient:
        keys: list[str] = []
        default_model_preference = "auto"

        def __init__(self) -> None:
            self.current = "leader-model"
            self.current_reasoning = "high"
            self.failover_callback = None
            self.leader_failures = 0
            self.continuity_prompts: list[str] = []

        def bind_task_model(self, model: str):
            previous = self.current
            self.current = model
            return previous

        def reset_task_model(self, token) -> None:
            self.current = token

        def bind_task_reasoning_effort(self, effort: str):
            previous = self.current_reasoning
            self.current_reasoning = effort
            return previous

        def reset_task_reasoning_effort(self, token) -> None:
            self.current_reasoning = token

        def bind_task_model_failover(self, callback):
            previous = self.failover_callback
            self.failover_callback = callback
            return previous

        def reset_task_model_failover(self, token) -> None:
            self.failover_callback = token

        async def chat(self, messages, tools):
            system_text = "\n".join(
                str(item.get("content") or "")
                for item in messages
                if item.get("role") == "system"
            )
            if "HOST LEADERSHIP CONTINUITY" in system_text:
                self.continuity_prompts.append(system_text)
            if "Propose a concrete execution plan" in system_text:
                self._fail_leader_once()
                content = "Acting leader proposal while the configured leader is unavailable."
            elif "Review the leader proposal" in system_text:
                content = "Member review complete."
            elif "final decision maker" in system_text:
                self._fail_leader_once()
                content = "Acting leader consensus preserves the configured leader role."
            elif "CURRENT TEAM SPEAKER: Builder" in system_text:
                content = "Member implementation complete."
            else:
                content = "Configured leader recovered and finalized the task."
            return DeepSeekReply(
                message={"role": "assistant", "content": content, "tool_calls": []},
                usage={},
                active_key="test",
            )

        def _fail_leader_once(self) -> None:
            self.leader_failures += 1
            assert self.failover_callback is not None
            self.failover_callback(
                {
                    "from_model": "leader-model",
                    "to_model": "anthropic-temp",
                    "from_provider": "openai",
                    "to_provider": "anthropic",
                    "different_provider": True,
                    "reason": "HTTP 524",
                }
            )

    client = FailoverTeamClient()
    task = AgentTask(
        prompt="Build and verify a feature",
        model_preference="leader-model",
        active_model="leader-model",
        discussion_team_enabled=True,
        discussion_team=_team(),
    )
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda current, event_type, data: current.events.append(
            TaskEvent(type=event_type, data=data)
        ),
    )

    await engine.run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "Configured leader recovered and finalized the task."
    assert task.active_model == "leader-model"
    delegated = [event for event in task.events if event.type == "team_leader_delegated"]
    retries = [event for event in task.events if event.type == "team_leader_retry_fallback"]
    restored = [event for event in task.events if event.type == "team_leader_restored"]
    assert len(delegated) == 1
    assert len(retries) == 1
    assert len(restored) == 1
    assert delegated[0].data["leader_model"] == "leader-model"
    assert delegated[0].data["temporary_model"] == "anthropic-temp"
    assert delegated[0].data["temporary_leader"] is True
    assert retries[0].data["retry_attempts"] == 2
    assert restored[0].data["retry_attempts"] == 2
    assert restored[0].data["leader_model"] == "leader-model"
    assert client.leader_failures == 2
    assert any("temporary acting leader" in prompt for prompt in client.continuity_prompts)
    assert any("recovery attempt" in prompt for prompt in client.continuity_prompts)
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "team_leader_delegated"' in audit_text
    assert '"event": "team_leader_retry_fallback"' in audit_text
    assert '"event": "team_leader_restored"' in audit_text
