from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import ContextWindowError, DeepSeekClient, DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate
from deepdesk.local_models import LocalModelRegistry, LocalRuntimeCandidate
from deepdesk.models import AgentTask, DiscussionTeamMember, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.task_store import TaskStore


def _engine(tmp_path: Path, client) -> AgentEngine:
    return AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda task, kind, data: None,
    )


@pytest.mark.parametrize("row_count,budget", [(1, 128), (20, 8_000), (500, 20_000)])
def test_team_section_budget_never_overflows_and_keeps_all_affordable_labels(
    row_count: int, budget: int
) -> None:
    rows = [
        (f"participant-{index:04d}", f"evidence-{index}-" + ("x" * 2_000))
        for index in range(row_count)
    ]
    rendered = AgentEngine._bounded_team_sections(rows, max_chars=budget)

    assert len(rendered) <= budget
    label_cost = sum(len(label) + 2 for label, _body in rows) + max(0, row_count - 1) * 2
    if label_cost <= budget:
        assert all(label in rendered for label, _body in rows)


@pytest.mark.asyncio
async def test_large_team_keeps_late_participants_in_proposal_and_consensus(
    tmp_path: Path,
) -> None:
    class Client:
        keys: list[str] = []

        def __init__(self) -> None:
            self.proposal_input = ""
            self.consensus_input = ""

        async def chat(self, messages, tools):
            system = "\n".join(
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "system"
            )
            user = "\n".join(
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "user"
            )
            if "Propose a concrete execution plan" in system:
                self.proposal_input = user
                content = "leader-proposal"
            elif "Review the leader proposal" in system:
                participant = system.split("You are ", 1)[1].split(",", 1)[0]
                content = f"{participant}-advice-" + ("evidence " * 1_500)
            elif "final decision maker" in system:
                self.consensus_input = user
                content = "bounded consensus"
            else:
                raise AssertionError(system)
            return DeepSeekReply(
                message={"role": "assistant", "content": content, "tool_calls": []},
                usage={},
                active_key="test",
            )

    members = [
        DiscussionTeamMember(
            id="leader",
            name="Leader",
            role="leader",
            model="leader-model",
            assignment="coordinate",
        )
    ]
    members.extend(
        DiscussionTeamMember(
            id=f"member-{index:02d}",
            name=f"Member-{index:02d}",
            role="member",
            model=f"model-{index:02d}",
            assignment=f"assignment-{index:02d}-" + ("detail " * 550),
        )
        for index in range(1, 21)
    )
    client = Client()
    task = AgentTask(
        prompt="Build a robust application",
        active_model="leader-model",
        discussion_team_enabled=True,
        discussion_team=members,
    )

    state = await _engine(tmp_path, client)._run_team_discussion(
        task, asyncio.Event()
    )

    assert state is not None
    assert len(client.proposal_input) <= 60_000
    assert len(client.consensus_input) <= 60_000
    for member in members:
        assert member.name in client.proposal_input
    for member in members[1:]:
        assert member.name in client.consensus_input


@pytest.mark.asyncio
async def test_local_catalog_normalizes_declared_aliases_and_reasoning_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "local-multimodal",
                        "modalities": ["text", "image_url", "function_calling"],
                        "capabilities": {
                            "function_calling": True,
                            "audio": "false",
                        },
                        "supported_reasoning_efforts": [
                            "max", "low", "high", "low", "unsupported"
                        ],
                    }
                ]
            },
        )

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "test",
                "Test runtime",
                "http://127.0.0.1:9999/v1/models",
                "http://127.0.0.1:9999/v1",
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    model = (await registry.discover())[0]
    assert model["supports_vision"] is True
    assert model["supports_tools"] is True
    assert "audio" not in model["capabilities"]
    assert model["reasoning_levels"] == ["low", "high", "max"]


@pytest.mark.asyncio
async def test_lm_studio_uses_loaded_instance_context_not_architectural_maximum() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "loaded-model", "max_context_length": 131_072}
                    ]
                },
            )
        if request.url.path == "/api/v0/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "loaded-model",
                            "max_context_length": 131_072,
                            "loaded_instances": [
                                {"config": {"context_length": 16_384}}
                            ],
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "lm-studio",
                "LM Studio",
                "http://127.0.0.1:1234/v1/models",
                "http://127.0.0.1:1234/v1",
                runtime_catalog_url="http://127.0.0.1:1234/api/v0/models",
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    model = (await registry.discover())[0]
    assert model["context_window_tokens"] == 16_384
    assert model["context_window_source"] == "local_runtime_loaded_instance"
    assert model["maximum_context_window_tokens"] == 131_072


@pytest.mark.asyncio
async def test_ollama_architectural_maximum_remains_unknown_when_not_loaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "idle-model"}]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={"model_info": {"architecture.context_length": 262_144}},
            )
        raise AssertionError(request.url)

    registry = LocalModelRegistry(
        candidates=(
            LocalRuntimeCandidate(
                "ollama",
                "Ollama",
                "http://127.0.0.1:11434/api/tags",
                "http://127.0.0.1:11434/v1",
                "ollama",
            ),
        ),
        transport=httpx.MockTransport(handler),
        ollama_context_reader=lambda: None,
    )

    model = (await registry.discover())[0]
    assert model["context_window_tokens"] is None
    assert model["context_window_source"] == "ollama_active_allocation_not_exposed"
    assert model["maximum_context_window_tokens"] == 262_144


def test_undeclared_local_context_remains_unknown_and_normalizes_levels() -> None:
    client = DeepSeekClient("https://example.invalid/v1", "fallback", "", "")
    client.set_local_models(
        [
            {
                "selector": "local-test:unknown-window",
                "model": "unknown-window",
                "base_url": "http://127.0.0.1:9999/v1",
                "reasoning_levels": ["max", "low", "max", "bogus"],
                "context_window_tokens": -1,
            }
        ]
    )

    selector = "local-test:unknown-window"
    assert client.context_window_info(selector) == {
        "context_window_tokens": None,
        "context_window_source": "local_runtime_not_declared",
    }
    model_token = client.bind_task_model(selector)
    try:
        assert client.context_window_tokens() is None
        assert AgentEngine._context_window_for_client(client) is None
    finally:
        client.reset_task_model(model_token)
    assert client.reasoning_capability(selector).levels == ("low", "max")


@pytest.mark.asyncio
async def test_unknown_local_context_compacts_progressively_only_after_real_overflow(
    tmp_path: Path,
) -> None:
    class Client:
        keys: list[str] = []
        effective_selector = "local-test:unknown"

        def __init__(self) -> None:
            self.calls = 0
            self.message_sizes: list[int] = []

        @staticmethod
        def context_window_tokens() -> None:
            return None

        async def chat(self, messages, tools):
            self.calls += 1
            self.message_sizes.append(
                AgentEngine._estimate_context_tokens(messages, tools)
            )
            if self.calls <= 2:
                raise ContextWindowError("local runtime rejected actual request size")
            return DeepSeekReply(
                message={"role": "assistant", "content": "recovered", "tool_calls": []},
                usage={},
                active_key="local",
            )

    client = Client()
    task = AgentTask(prompt="preserve this goal " + ("long evidence " * 8_000))
    await _engine(tmp_path, client).run(task, asyncio.Event())

    assert task.status == TaskStatus.COMPLETED
    assert client.calls == 3
    assert client.message_sizes[0] > client.message_sizes[1] > client.message_sizes[2]


def test_local_tool_schemas_are_sent_only_when_runtime_declares_tool_support() -> None:
    client = DeepSeekClient("https://example.invalid/v1", "fallback", "", "")
    base = {
        "model": "local-model",
        "base_url": "http://127.0.0.1:9999/v1",
    }
    client.set_local_models(
        [
            {**base, "selector": "local-test:text-only", "capabilities": ["vision"]},
            {**base, "selector": "local-test:tool-model", "capabilities": ["tools"]},
        ]
    )
    tools = [{"type": "function", "function": {"name": "inspect"}}]

    text_only = client._endpoint_for("local-test:text-only")
    tool_model = client._endpoint_for("local-test:tool-model")

    assert "tools" not in client._chat_payload([], tools, endpoint=text_only)
    assert client._chat_payload([], tools, endpoint=tool_model)["tools"] == tools


def test_cross_chat_recall_uses_latest_fifty_with_monotonic_recency_weights(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(55):
        timestamp = (start + timedelta(minutes=index)).isoformat()
        store.save(
            AgentTask(
                id=f"task-{index:02d}",
                prompt=f"orion-marker decision {index}",
                result=f"orion-marker result {index}",
                status=TaskStatus.COMPLETED,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    context, task_ids = store.build_cross_conversation_context(
        "continue orion-marker", max_chars=200_000
    )

    assert len(task_ids) == 50
    assert task_ids[0] == "task-54"
    assert task_ids[-1] == "task-05"
    assert "task-00" not in task_ids
    weights = [
        float(line.rsplit(" ", 1)[1].rstrip("]"))
        for line in context.splitlines()
        if line.startswith("[历史对话 ")
    ]
    assert all(left > right for left, right in pairwise(weights))
