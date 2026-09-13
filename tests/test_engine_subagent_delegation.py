from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.egress_region import EgressRegion, EgressRegionDetector
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate
from deepdesk.models import AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry, ToolContext, ToolPlugin
from deepdesk.subagents import (
    DELEGATE_SPECIALISTS_NAME,
    SPECIALIST_SEARCH_NAME,
    catalog_by_id,
    load_subagent_catalog,
)

_CREDENTIAL_SENTINEL = "sk-test-engine-delegation-never-public-123456"
_HIDDEN_REASONING_SENTINEL = "HIDDEN_REASONING_MUST_NOT_REACH_PUBLIC_PAYLOAD"


@pytest.fixture(autouse=True)
def _disable_network_egress_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def detect(_self: EgressRegionDetector) -> EgressRegion:
        return EgressRegion()

    monkeypatch.setattr(EgressRegionDetector, "detect", detect)


def _reply(message: dict[str, Any], *, key: str = "fake-main") -> DeepSeekReply:
    return DeepSeekReply(message=message, usage={}, active_key=key)


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [str(tool.get("function", {}).get("name") or "") for tool in tools]


def _delegate_schema(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        tool
        for tool in tools
        if tool.get("function", {}).get("name") == DELEGATE_SPECIALISTS_NAME
    )


def _candidate_ids(tools: list[dict[str, Any]]) -> list[str]:
    schema = _delegate_schema(tools)
    return list(
        schema["function"]["parameters"]["properties"]["assignments"]["items"]
        ["properties"]["preset_id"]["enum"]
    )


def _public_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        result = {str(key) for key in value}
        for item in value.values():
            result.update(_public_keys(item))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_public_keys(item))
        return result
    return set()


def _engine(
    tmp_path: Path,
    client: Any,
    registry: PluginRegistry | None = None,
) -> AgentEngine:
    def emit(task: AgentTask, event_type: str, data: dict[str, Any]) -> None:
        task.events.append(TaskEvent(type=event_type, data=copy.deepcopy(data)))

    return AgentEngine(
        client=client,
        registry=registry or PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=emit,
    )


class _DelegatingClient:
    keys = [_CREDENTIAL_SENTINEL]

    def __init__(self) -> None:
        self.main_calls: list[dict[str, Any]] = []
        self.child_calls: list[dict[str, Any]] = []
        self.delegated_ids: list[str] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> DeepSeekReply:
        record = {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
        }
        system = str(messages[0].get("content") or "")
        if "preset specialist" in system:
            self.child_calls.append(record)
            return _reply(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "独立复核完成",
                            "evidence": ["可复核证据", _CREDENTIAL_SENTINEL],
                            "artifacts": [],
                            "risks": ["仍需主模型合并判断"],
                            "confidence": 0.84,
                            "needs_approval": False,
                            "hidden_reasoning": _HIDDEN_REASONING_SENTINEL,
                            "credential": _CREDENTIAL_SENTINEL,
                        },
                        ensure_ascii=False,
                    ),
                    "tool_calls": [],
                },
                key="fake-child",
            )

        self.main_calls.append(record)
        if len(self.main_calls) == 1:
            names = _tool_names(tools)
            assert SPECIALIST_SEARCH_NAME in names
            assert DELEGATE_SPECIALISTS_NAME in names
            candidates = _candidate_ids(tools)
            assert len(candidates) >= 2
            self.delegated_ids = candidates[:2]
            assignments = [
                {
                    "preset_id": self.delegated_ids[0],
                    "assignment": "独立检查实现风险和遗漏",
                    "expected_output": "给出证据与风险",
                },
                {
                    "preset_id": self.delegated_ids[1],
                    "assignment": "独立检查测试和发布质量",
                    "expected_output": "给出可复核结论",
                },
            ]
            return _reply(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "delegate-1",
                            "type": "function",
                            "function": {
                                "name": DELEGATE_SPECIALISTS_NAME,
                                "arguments": json.dumps(
                                    {"assignments": assignments}, ensure_ascii=False
                                ),
                            },
                        }
                    ],
                }
            )

        return _reply(
            {
                "role": "assistant",
                "content": "已汇总子智能体证据并完成最终答复",
                "tool_calls": [],
            }
        )


@pytest.mark.asyncio
async def test_engine_lazily_delegates_and_returns_to_the_main_model(tmp_path: Path) -> None:
    client = _DelegatingClient()
    task = AgentTask(
        prompt="深入审核这个复杂版本的实现、测试、安全和发布质量，并进行必要分工",
        model_preference="deepseek-v4-flash",
        active_model="deepseek-v4-flash",
        interface_language="zh",
    )

    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert task.result == "已汇总子智能体证据并完成最终答复"
    assert len(client.main_calls) == 2
    assert len(client.child_calls) == 2

    first_tools = client.main_calls[0]["tools"]
    candidates = _candidate_ids(first_tools)
    catalog = load_subagent_catalog()
    assert 2 <= len(candidates) <= 10
    assert len(candidates) < len(catalog) == 99
    assert set(candidates) <= {preset.id for preset in catalog}
    assert set(_tool_names(first_tools)) == {
        SPECIALIST_SEARCH_NAME,
        DELEGATE_SPECIALISTS_NAME,
    }
    serialized_tools = json.dumps(first_tools, ensure_ascii=False)
    assert all(catalog_by_id()[role_id].boundary not in serialized_tools for role_id in candidates)

    second_messages = client.main_calls[1]["messages"]
    tool_message = next(message for message in reversed(second_messages) if message["role"] == "tool")
    structured_result = json.loads(str(tool_message["content"]).split("\n", 1)[1])
    assert structured_result["ok"] is True
    assert structured_result["requested_count"] == 2
    assert structured_result["completed_count"] == 2
    assert structured_result["failed_count"] == 0
    assert len(structured_result["results"]) == 2
    assert all(result["status"] == "completed" for result in structured_result["results"])

    event_types = [event.type for event in task.events]
    assert event_types.count("subagent_dispatch_started") == 1
    assert event_types.count("subagent_started") == 2
    assert event_types.count("subagent_completed") == 2
    assert event_types.count("subagent_results_merged") == 1

    public_payload = task.model_dump(mode="json")
    public_text = json.dumps(public_payload, ensure_ascii=False)
    forbidden_public_fields = {
        "boundary",
        "credential",
        "credentials",
        "hidden_reasoning",
        "instructions",
        "system_prompt",
    }
    assert forbidden_public_fields.isdisjoint(_public_keys(public_payload))
    assert _CREDENTIAL_SENTINEL not in public_text
    assert _HIDDEN_REASONING_SENTINEL not in public_text
    assert "Your boundary is" not in public_text
    assert "preset specialist" not in public_text
    assert all(catalog_by_id()[role_id].boundary not in public_text for role_id in client.delegated_ids)


class _GreetingClient:
    keys: list[str] = []

    def __init__(self) -> None:
        self.tools: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        _messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> DeepSeekReply:
        self.tools.append(copy.deepcopy(tools))
        return _reply(
            {"role": "assistant", "content": "你好，需要我做什么？", "tool_calls": []}
        )


@pytest.mark.asyncio
async def test_simple_greeting_exposes_no_specialist_tools(tmp_path: Path) -> None:
    client = _GreetingClient()
    task = AgentTask(
        prompt="你好",
        model_preference="deepseek-v4-flash",
        active_model="deepseek-v4-flash",
    )

    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert len(client.tools) == 1
    names = _tool_names(client.tools[0])
    assert SPECIALIST_SEARCH_NAME not in names
    assert DELEGATE_SPECIALISTS_NAME not in names


class _MutatingTool(ToolPlugin):
    name = "mutating_test_tool"
    description = "Mutate test state to verify mixed delegation batches are rejected"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self) -> None:
        self.executed = False

    def risk(self, _arguments: dict[str, Any]) -> Risk:
        return Risk.MEDIUM

    async def execute(
        self,
        _arguments: dict[str, Any],
        _context: ToolContext,
    ) -> dict[str, Any]:
        self.executed = True
        return {"mutated": True}


class _MixedBatchClient:
    keys: list[str] = []

    def __init__(self) -> None:
        self.main_calls = 0

    async def chat(
        self,
        _messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> DeepSeekReply:
        self.main_calls += 1
        if self.main_calls == 1:
            candidates = _candidate_ids(tools)
            assert candidates
            return _reply(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mixed-delegation",
                            "type": "function",
                            "function": {
                                "name": DELEGATE_SPECIALISTS_NAME,
                                "arguments": json.dumps(
                                    {
                                        "assignments": [
                                            {
                                                "preset_id": candidates[0],
                                                "assignment": "独立复核风险",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        },
                        {
                            "id": "mixed-mutation",
                            "type": "function",
                            "function": {
                                "name": _MutatingTool.name,
                                "arguments": "{}",
                            },
                        },
                    ],
                }
            )
        return _reply(
            {"role": "assistant", "content": "批次已安全拒绝", "tool_calls": []}
        )


@pytest.mark.asyncio
async def test_mixed_delegation_and_side_effect_batch_is_rejected_atomically(
    tmp_path: Path,
) -> None:
    mutating_tool = _MutatingTool()
    registry = PluginRegistry()
    registry.register(mutating_tool)
    task = AgentTask(
        prompt=(
            "全面审核并修复这个复杂版本，同时调用 mutating_test_tool 验证发布流程"
        ),
        model_preference="deepseek-v4-flash",
        active_model="deepseek-v4-flash",
    )

    await _engine(tmp_path, _MixedBatchClient(), registry).run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert mutating_tool.executed is False
    assert task.subagent_runs == []
    assert any(
        event.type == "tool_rejected"
        and event.data.get("reason") == "mixed_delegation_batch"
        for event in task.events
    )
