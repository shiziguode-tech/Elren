from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from deepdesk.deepseek import DeepSeekClient, _sse_data_payload, _StreamAccumulator
from deepdesk.engine import AgentEngine
from deepdesk.models import AgentTask, TaskStatus
from deepdesk.task_store import TaskStore


def test_streamed_tool_arguments_without_repeated_index_remain_one_literal_call():
    accumulator = _StreamAccumulator(retain_final_reasoning=False)
    try:
        accumulator.feed(
            {
                "model": "relay-model",
                "choices": [{"delta": {"tool_calls": [{
                    "id": "call_critical",
                    "type": "function",
                    "function": {"name": "write", "arguments": '{"path":"报告'},
                }]}}],
            }
        )
        # Some compatible relays omit `index` on continuation chunks. They must
        # append to the open call, not silently create a second malformed call.
        accumulator.feed(
            {"choices": [{"delta": {"tool_calls": [{
                "function": {"arguments": ' Δ.txt","content":"100% / gas / 05:30"}'},
            }]}, "finish_reason": "tool_calls"}]}
        )
        result = accumulator.result()
    finally:
        accumulator.close()

    calls = result["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_critical"
    assert calls[0]["function"] == {
        "name": "write",
        "arguments": '{"path":"报告 Δ.txt","content":"100% / gas / 05:30"}',
    }


def test_streamed_tool_metadata_repeated_by_relay_is_not_duplicated():
    accumulator = _StreamAccumulator(retain_final_reasoning=False)
    try:
        accumulator.feed({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "write", "arguments": '{"path":"'},
        }]}}]})
        accumulator.feed({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "write", "arguments": 'a.txt"}'},
        }]}, "finish_reason": "tool_calls"}]})
        result = accumulator.result()
    finally:
        accumulator.close()

    call = result["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"] == {"name": "write", "arguments": '{"path":"a.txt"}'}


def test_sse_first_event_accepts_bom_without_accepting_non_data_fields():
    assert _sse_data_payload('\ufeffdata: {"choices":[]}') == '{"choices":[]}'
    assert _sse_data_payload("  data: [DONE]  ") == "[DONE]"
    assert _sse_data_payload(": keep-alive") is None
    assert _sse_data_payload("event: message") is None


def test_tool_schema_rejects_nonfinite_and_oversized_nested_values():
    schema = {
        "type": "object",
        "properties": {
            "coordinate": {"type": "number", "minimum": -100, "maximum": 100},
            "label": {"type": "string", "maxLength": 8},
            "items": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 4}},
        },
        "required": ["coordinate", "label", "items"],
        "additionalProperties": False,
    }
    issues = AgentEngine._validate_tool_arguments(
        schema,
        {"coordinate": math.nan, "label": "x" * 20, "items": ["valid", "toolong", "third"]},
    )
    assert any("finite" in issue for issue in issues)
    assert any("label" in issue and "at most 8" in issue for issue in issues)
    assert any("items" in issue and "at most 2 items" in issue for issue in issues)
    assert any("items[1]" in issue and "at most 4" in issue for issue in issues)


def test_long_cross_chat_compression_preserves_salient_middle_constraints(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.db")
    marker = "关键约束：最终交付必须保留文件名 Project-Δ-100%.docx，并使用 05:30 时区值。"
    result = "开头记录。" + ("普通过程内容。" * 900) + marker + ("普通验证内容。" * 900) + "最终结果：等待继续。"
    task = AgentTask(
        prompt="继续 Project-Δ 项目并保留全部关键约束",
        status=TaskStatus.COMPLETED,
        result=result,
        source="web",
    )
    store.save(task)

    context, task_ids = store.build_cross_conversation_context(
        "继续 Project-Δ 项目", source="web", max_chars=12_000
    )

    assert task_ids == [task.id]
    assert marker in context
    assert len(context) <= 12_000
    assert "历史对话已压缩" in context


@pytest.mark.asyncio
async def test_model_and_reasoning_bindings_survive_40_way_context_switching():
    client = DeepSeekClient("https://api.deepseek.example", "deepseek-v4-flash", "key", "")
    client.set_provider_models(
        [
            {"provider": "openai", "model": "gpt-test", "api_key": "oa"},
            {"provider": "anthropic", "model": "claude-test", "api_key": "an"},
        ]
    )

    async def worker(index: int) -> tuple[str, str | None]:
        selector = "openai:gpt-test" if index % 2 else "anthropic:claude-test"
        effort = "high" if index % 3 else "low"
        model_token = client.bind_task_model(selector)
        effort_token = client.bind_task_reasoning_effort(effort)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return client.effective_selector, client._task_reasoning_effort.get()
        finally:
            client.reset_task_reasoning_effort(effort_token)
            client.reset_task_model(model_token)

    results = await asyncio.gather(*(worker(index) for index in range(40)))
    for index, (selector, effort) in enumerate(results):
        assert selector == ("openai:gpt-test" if index % 2 else "anthropic:claude-test")
        assert effort == ("high" if index % 3 else "low")
