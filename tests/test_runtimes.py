import asyncio
import json
import socket
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deepdesk.deepseek import DeepSeekClient
from deepdesk.models import AgentTask, Risk, TaskEvent, TaskStatus
from deepdesk.openclaw_bridge import OpenClawBridge
from deepdesk.paddle_ocr import PaddleOCR
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool
from deepdesk.plugins.builtin.deepseek_search import ProviderWebSearchTool
from deepdesk.plugins.builtin.memory import MemoryTool
from deepdesk.plugins.builtin.openclaw import OpenClawBridgeTool
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool
from deepdesk.plugins.builtin.skills import SkillsTool
from deepdesk.plugins.builtin.vision import VisionTool
from deepdesk.plugins.builtin.web import WebTool
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch, RuntimeSettingsStore
from deepdesk.secret_storage import AesGcmProtector
from deepdesk.task_store import TaskStore
from deepdesk.vision_runtime import VisionEndpoint, VisionRuntime


def test_task_store_roundtrip(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    task = AgentTask(prompt="测试持久化", status=TaskStatus.COMPLETED, result="完成")
    task.events.append(TaskEvent(type="assistant", data={"content": "完成"}))
    store.save(task)

    restored = store.load_recent()

    assert len(restored) == 1
    assert restored[0].id == task.id
    assert restored[0].events[0].data["content"] == "完成"


def test_paddle_ocr_accepts_array_like_results_without_boolean_coercion(tmp_path: Path):
    class ArrayLike:
        def __init__(self, values):
            self.values = values

        def __iter__(self):
            return iter(self.values)

        def __bool__(self):
            raise ValueError("array truth value is ambiguous")

    fake_result = SimpleNamespace(
        txts=ArrayLike(["第一行", "second"]),
        scores=ArrayLike([0.98, 0.87]),
        boxes=ArrayLike(
            [
                [[0, 0], [10, 0], [10, 10], [0, 10]],
                [[0, 12], [20, 12], [20, 22], [0, 22]],
            ]
        ),
    )
    tool = PaddleOCR()
    tool._engine = lambda _path: fake_result
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")

    result = tool._recognize_sync(image)

    assert result["text"] == "第一行\nsecond"
    assert result["lines"][0]["confidence"] == pytest.approx(0.98)
    assert result["lines"][1]["box"][2] == [20.0, 22.0]


def test_cross_conversation_context_is_relevant_and_redacted(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    relevant = AgentTask(
        prompt="修复飞书消息发送，API key: sk-supersecret123456",
        status=TaskStatus.COMPLETED,
        result="飞书发送已恢复，token=very-secret-token-value",
    )
    unrelated = AgentTask(
        prompt="整理桌面文件",
        status=TaskStatus.COMPLETED,
        result="桌面文件已整理",
    )
    other_completed = AgentTask(
        prompt="整理本地相册",
        status=TaskStatus.COMPLETED,
        result="不应出现的不相关结果",
    )
    failed = AgentTask(
        prompt="飞书失败过程",
        status=TaskStatus.FAILED,
        result="不应带入的失败结果",
    )
    for task in (relevant, unrelated, other_completed, failed):
        store.save(task)

    context, task_ids = store.build_cross_conversation_context("继续优化飞书发送")

    assert task_ids == [relevant.id]
    assert "飞书发送已恢复" in context
    assert "supersecret" not in context
    assert "very-secret" not in context
    assert "[已隐藏" in context
    assert "不应出现" not in context
    assert unrelated.id not in task_ids


def test_cross_conversation_context_does_not_guess_when_history_is_unrelated(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    store.save(
        AgentTask(
            prompt="编写 Python 排序函数",
            status=TaskStatus.COMPLETED,
            result="排序函数已完成",
        )
    )

    context, task_ids = store.build_cross_conversation_context("查询明天天气")

    assert context == ""
    assert task_ids == []


def test_cross_conversation_context_uses_all_history_with_recency_decay(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    tasks = []
    for index in range(7):
        task = AgentTask(
            prompt=f"Elren finance market report phase {index}",
            status=TaskStatus.COMPLETED,
            result=f"finance market report result {index}",
            source="web",
            updated_at=f"2026-08-08T0{index}:00:00+00:00",
        )
        store.save(task)
        tasks.append(task)

    context, task_ids = store.build_cross_conversation_context(
        "continue Elren finance market report", source="web", limit=6
    )

    assert len(task_ids) == 6
    assert tasks[-1].id in task_ids
    # The implementation is no longer capped to the last two chats.
    assert tasks[-4].id in task_ids
    assert "参考权重 1.000" in context
    assert "参考权重 0.893" in context
    assert context.index("result 6") < context.index("result 5")


def test_cross_conversation_context_scans_at_most_50_turns_and_compresses_long_turns(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    tasks = []
    for index in range(55):
        task = AgentTask(
            prompt=f"shared-context-topic {index} " + ("A" * 1600),
            status=TaskStatus.COMPLETED,
            result=f"shared-context-result {index} " + ("B" * 2400),
            source="web",
            updated_at=f"2026-08-{index + 1:02d}T09:00:00+00:00",
        )
        store.save(task)
        tasks.append(task)

    context, task_ids = store.build_cross_conversation_context(
        "continue shared-context-topic", source="web"
    )

    assert len(task_ids) == 50
    assert tasks[-1].id in task_ids
    assert tasks[0].id not in task_ids
    assert "历史对话已压缩至约五分之一" in context
    assert len(context) < 55 * 4200 // 3
    weights = [
        float(value) for value in __import__("re").findall(r"参考权重 ([0-9.]+)", context)
    ]
    assert weights == sorted(weights, reverse=True)


def test_elliptical_retry_uses_immediately_previous_task_on_same_channel(tmp_path: Path):
    store = TaskStore(
        tmp_path / "deepdesk.db",
        route_protector=AesGcmProtector("test-context-routes", b"c" * 32),
    )
    older = AgentTask(
        prompt="识别经幡塔并生成高原场景音乐和视频",
        status=TaskStatus.COMPLETED,
        result="高原经幡塔图片已完成，音乐和视频未完成",
        source="telegram",
        remote_recipient_id="telegram-chat-a",
        remote_recipient_type="chat_id",
        updated_at="2026-08-08T08:00:00+00:00",
    )
    recent = AgentTask(
        prompt="生成一个人跳舞的视频、两个人在车上的图片和一段摇滚乐",
        status=TaskStatus.COMPLETED,
        result="图片已完成；一个人跳舞的视频和摇滚乐尚未生成",
        source="telegram",
        remote_recipient_id="telegram-chat-a",
        remote_recipient_type="chat_id",
        updated_at="2026-08-08T09:00:00+00:00",
    )
    newest_other_channel = AgentTask(
        prompt="生成海边视频和音乐",
        status=TaskStatus.COMPLETED,
        result="网页任务完成",
        source="web",
        updated_at="2026-08-08T09:30:00+00:00",
    )
    for task in (older, recent, newest_other_channel):
        store.save(task)

    context, task_ids = store.build_cross_conversation_context(
        "重试一下其他两项的生成",
        source="telegram",
        remote_recipient_id="telegram-chat-a",
        remote_recipient_type="chat_id",
    )

    assert task_ids == [recent.id]
    assert "一个人跳舞的视频和摇滚乐尚未生成" in context
    assert "经幡塔" not in context
    assert newest_other_channel.id not in task_ids


def test_feishu_context_is_isolated_by_recipient_id_and_type(tmp_path: Path):
    store = TaskStore(
        tmp_path / "feishu-context.db",
        route_protector=AesGcmProtector("test-context-routes", b"c" * 32),
    )
    intended = AgentTask(
        prompt="准备 atlas 发布检查",
        status=TaskStatus.COMPLETED,
        result="ALPHA_OPEN_ID_RESULT",
        source="feishu",
        remote_recipient_id="ou_alpha",
        remote_recipient_type="open_id",
        updated_at="2026-08-08T08:00:00+00:00",
    )
    other_recipient = AgentTask(
        prompt="准备 atlas 发布检查",
        status=TaskStatus.COMPLETED,
        result="BETA_PRIVATE_RESULT",
        source="feishu",
        remote_recipient_id="ou_beta",
        remote_recipient_type="open_id",
        updated_at="2026-08-08T09:00:00+00:00",
    )
    same_value_different_type = AgentTask(
        prompt="准备 atlas 发布检查",
        status=TaskStatus.COMPLETED,
        result="CHAT_ID_PRIVATE_RESULT",
        source="feishu",
        remote_recipient_id="ou_alpha",
        remote_recipient_type="chat_id",
        updated_at="2026-08-08T10:00:00+00:00",
    )
    same_value_other_channel = AgentTask(
        prompt="准备 atlas 发布检查",
        status=TaskStatus.COMPLETED,
        result="TELEGRAM_PRIVATE_RESULT",
        source="telegram",
        remote_recipient_id="ou_alpha",
        remote_recipient_type="open_id",
        updated_at="2026-08-08T11:00:00+00:00",
    )
    for task in (
        intended,
        other_recipient,
        same_value_different_type,
        same_value_other_channel,
    ):
        store.save(task)

    context, task_ids = store.build_cross_conversation_context(
        "继续 atlas 发布检查",
        source="feishu",
        remote_recipient_id="ou_alpha",
        remote_recipient_type="open_id",
    )

    assert task_ids == [intended.id]
    assert "ALPHA_OPEN_ID_RESULT" in context
    assert "BETA_PRIVATE_RESULT" not in context
    assert "CHAT_ID_PRIVATE_RESULT" not in context
    assert "TELEGRAM_PRIVATE_RESULT" not in context
    # If the caller loses either half of a remote route, recall fails closed.
    assert store.build_cross_conversation_context(
        "继续 atlas 发布检查", source="feishu"
    ) == ("", [])


def test_short_settings_confirmation_uses_immediately_previous_task(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    previous = AgentTask(
        prompt="Change the default model to GPT5.6SOU",
        status=TaskStatus.COMPLETED,
        result="Did you mean GPT-5.6 Sol? No setting has been changed yet.",
        source="web",
        updated_at="2026-08-10T09:00:00+00:00",
    )
    unrelated = AgentTask(
        prompt="Prepare a weekly report",
        status=TaskStatus.COMPLETED,
        result="Report prepared",
        source="telegram",
        updated_at="2026-08-10T09:30:00+00:00",
    )
    store.save(previous)
    store.save(unrelated)

    context, task_ids = store.build_cross_conversation_context("yes", source="web")

    assert task_ids == [previous.id]
    assert "GPT-5.6 Sol" in context


@pytest.mark.parametrize("answer", ["语音 1 转写：是的", "语音 2 转写：是啊", "语音 3 转写：对啊"])
def test_transcribed_voice_confirmation_uses_previous_task(tmp_path: Path, answer: str):
    store = TaskStore(
        tmp_path / "deepdesk.db",
        route_protector=AesGcmProtector("test-context-routes", b"c" * 32),
    )
    previous = AgentTask(
        prompt="语音 1 转写：把模型改为 GPT5.6SOUL",
        status=TaskStatus.COMPLETED,
        result="我将 GPT5.6SOUL 理解为 GPT-5.6 Sol。你想这样修改吗？",
        source="telegram",
        remote_recipient_id="telegram-chat-a",
        remote_recipient_type="chat_id",
        updated_at="2026-08-10T09:00:00+00:00",
    )
    store.save(previous)

    context, task_ids = store.build_cross_conversation_context(
        answer,
        source="telegram",
        remote_recipient_id="telegram-chat-a",
        remote_recipient_type="chat_id",
    )

    assert task_ids == [previous.id]
    assert "GPT-5.6 Sol" in context


def test_task_store_marks_interrupted_work_failed(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    task = AgentTask(prompt="尚未结束", status=TaskStatus.RUNNING)
    store.save(task)

    restored = store.load_recent()

    assert restored[0].status == TaskStatus.FAILED
    assert "重启" in restored[0].error
    assert TaskStore(tmp_path / "deepdesk.db").load_recent()[0].status == TaskStatus.FAILED


def test_task_store_pages_searches_and_loads_complete_history(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    tasks = []
    for index in range(125):
        task = AgentTask(
            prompt=f"history task {index:03d}" + (" needle" if index == 7 else ""),
            status=TaskStatus.COMPLETED if index % 2 else TaskStatus.CANCELLED,
        )
        store.save(task)
        tasks.append(task)

    first, total = store.list_page(limit=50, offset=0)
    third, third_total = store.list_page(limit=50, offset=100)
    found, found_total = store.list_page(query="needle")
    completed, completed_total = store.list_page(status=TaskStatus.COMPLETED.value)

    assert total == third_total == 125
    assert len(first) == 50
    assert len(third) == 25
    assert found_total == 1 and found[0].prompt.endswith("needle")
    assert completed_total == 62
    assert all(task.status == TaskStatus.COMPLETED for task in completed)
    assert store.get(tasks[7].id).prompt.endswith("needle")


def test_task_history_has_no_retired_session_isolation_filter(tmp_path: Path):
    store = TaskStore(tmp_path / "deepdesk.db")
    first = AgentTask(prompt="first", status=TaskStatus.COMPLETED)
    literal = AgentTask(
        prompt='Document the JSON field "private_chat":true without enabling privacy.',
        status=TaskStatus.COMPLETED,
    )
    store.save(first)
    store.save(literal)

    page, total = store.list_page()

    assert total == 2
    assert {task.prompt for task in page} == {"first", literal.prompt}
    with pytest.raises(TypeError):
        store.list_page(privacy_session="stale-page-capability")


def test_task_store_neutralizes_legacy_private_chat_metadata(tmp_path: Path):
    path = tmp_path / "legacy.db"
    legacy_session = "legacy-privacy-session-canary-8F4C2A71"
    legacy_task = AgentTask(
        prompt="legacy private",
        status=TaskStatus.COMPLETED,
    )
    legacy_payload = legacy_task.model_dump(mode="json")
    legacy_payload["private_chat"] = True
    legacy_payload["privacy_session"] = legacy_session
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                private_chat INTEGER NOT NULL DEFAULT 0,
                privacy_session TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                legacy_task.id,
                legacy_task.status.value,
                legacy_task.created_at,
                legacy_task.updated_at,
                1,
                legacy_session,
                json.dumps(legacy_payload),
            ),
        )
        connection.execute("CREATE TABLE legacy_update_audit (updates INTEGER NOT NULL)")
        connection.execute("INSERT INTO legacy_update_audit VALUES (0)")
        connection.execute(
            """
            CREATE TRIGGER audit_legacy_metadata_update
            AFTER UPDATE OF private_chat, privacy_session ON tasks
            BEGIN
                UPDATE legacy_update_audit SET updates = updates + 1;
            END
            """
        )

    store = TaskStore(path)
    visible, total = store.list_page()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT private_chat, privacy_session, payload FROM tasks WHERE id = ?",
            (legacy_task.id,),
        ).fetchone()

    assert total == 1 and visible[0].id == legacy_task.id
    assert row is not None and row[0:2] == (0, "")
    persisted = json.loads(row[2])
    assert "private_chat" not in persisted
    assert "privacy_session" not in persisted
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        if candidate.exists():
            assert legacy_session.encode() not in candidate.read_bytes()

    # Reopening an already migrated database must not issue another legacy
    # column update across the table.
    TaskStore(path)
    with sqlite3.connect(path) as connection:
        legacy_updates = connection.execute(
            "SELECT updates FROM legacy_update_audit"
        ).fetchone()
    assert legacy_updates == (1,)


def test_legacy_privacy_retirement_recovers_after_pre_vacuum_crash(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "legacy-crash.db"
    canary = "legacy-session-crash-canary-91D3F6B8"
    task = AgentTask(prompt="legacy crash", status=TaskStatus.COMPLETED)
    payload = task.model_dump(mode="json")
    payload["private_chat"] = True
    payload["privacy_session"] = canary
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                private_chat INTEGER NOT NULL DEFAULT 0,
                privacy_session TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                1,
                canary,
                json.dumps(payload),
            ),
        )

    original_compact = TaskStore._compact_if_changed
    failures = 0

    def fail_before_first_vacuum(connection, changed):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise RuntimeError("simulated crash before VACUUM")
        return original_compact(connection, changed)

    monkeypatch.setattr(
        TaskStore,
        "_compact_if_changed",
        staticmethod(fail_before_first_vacuum),
    )
    with pytest.raises(RuntimeError, match="before VACUUM"):
        TaskStore(path)

    with sqlite3.connect(path) as connection:
        pending = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone()
        logical = connection.execute(
            "SELECT private_chat, privacy_session, payload FROM tasks"
        ).fetchone()
    assert pending == ("physical-scrub-v1:pending",)
    assert logical is not None and logical[:2] == (0, "")
    assert "privacy_session" not in json.loads(logical[2])

    # The second startup must compact even though logical cleanup now reports
    # zero changed rows, then persist completion only after verification.
    recovered = TaskStore(path)
    assert recovered.get(task.id) is not None
    with sqlite3.connect(path) as connection:
        completed = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone()
    assert completed == ("physical-scrub-v1:completed",)
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        if candidate.exists():
            assert canary.encode() not in candidate.read_bytes()

    compact_calls_after_completion = 0

    def count_compaction(connection, changed):
        nonlocal compact_calls_after_completion
        compact_calls_after_completion += 1
        return original_compact(connection, changed)

    monkeypatch.setattr(
        TaskStore,
        "_compact_if_changed",
        staticmethod(count_compaction),
    )
    TaskStore(path)
    assert compact_calls_after_completion == 0


def test_legacy_privacy_retirement_is_invalidated_by_reintroduced_metadata(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "legacy-reintroduced.db"
    canary = "legacy-session-reintroduced-canary-4E72A1C9"
    task = AgentTask(prompt="clean legacy schema", status=TaskStatus.COMPLETED)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                private_chat INTEGER NOT NULL DEFAULT 0,
                privacy_session TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, 0, '', ?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                json.dumps(task.model_dump(mode="json")),
            ),
        )

    TaskStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone() == ("physical-scrub-v1:completed",)
        reintroduced = task.model_dump(mode="json")
        reintroduced["private_chat"] = True
        reintroduced["privacy_session"] = canary
        connection.execute(
            "UPDATE tasks SET private_chat = 1, privacy_session = ?, payload = ? "
            "WHERE id = ?",
            (canary, json.dumps(reintroduced), task.id),
        )
        assert connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone() is None

    recovered = TaskStore(path)
    assert recovered.get(task.id) is not None
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT private_chat, privacy_session, payload FROM tasks WHERE id = ?",
            (task.id,),
        ).fetchone()
        marker = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone()
    assert row is not None and row[:2] == (0, "")
    assert "private_chat" not in json.loads(row[2])
    assert "privacy_session" not in json.loads(row[2])
    assert marker == ("physical-scrub-v1:completed",)
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        if candidate.exists():
            assert canary.encode() not in candidate.read_bytes()

    # Current payloads do not carry retired top-level keys, so routine saves
    # must leave the completed marker valid and avoid another VACUUM on reopen.
    original_compact = TaskStore._compact_if_changed
    compact_calls = 0

    def count_compaction(connection, changed):
        nonlocal compact_calls
        compact_calls += 1
        return original_compact(connection, changed)

    monkeypatch.setattr(
        TaskStore,
        "_compact_if_changed",
        staticmethod(count_compaction),
    )
    recovered.save(AgentTask(prompt="ordinary current save"))
    TaskStore(path)
    assert compact_calls == 0


def test_pending_legacy_retirement_preserves_nested_keys_and_literal_text(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "legacy-nested-keys.db"
    task = AgentTask(
        prompt='Explain the literal keys "private_chat" and "privacy_session".',
        result='Keep ordinary text mentioning "privacy_session" unchanged.',
        status=TaskStatus.COMPLETED,
    )
    payload = task.model_dump(mode="json")
    payload["events"] = [
        {
            "id": "legacy-nested-event",
            "type": "tool_result",
            "timestamp": task.updated_at,
            "data": {
                "private_chat": "documented nested value",
                "privacy_session": "non-bearer nested fixture",
            },
        }
    ]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                json.dumps(payload),
            ),
        )
        connection.execute(
            "CREATE TABLE security_migrations (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO security_migrations VALUES (?, ?)",
            (
                "task-legacy-chat-isolation-retirement",
                "physical-scrub-v1:pending",
            ),
        )

    store = TaskStore(path)
    restored = store.get(task.id)
    assert restored is not None
    assert restored.prompt == task.prompt
    assert restored.result == task.result
    assert restored.events[0].data == payload["events"][0]["data"]
    with sqlite3.connect(path) as connection:
        persisted = json.loads(
            connection.execute(
                "SELECT payload FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()[0]
        )
        marker = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone()
    assert persisted["events"][0]["data"] == payload["events"][0]["data"]
    assert marker == ("physical-scrub-v1:completed",)

    original_compact = TaskStore._compact_if_changed
    compact_calls = 0

    def count_compaction(connection, changed):
        nonlocal compact_calls
        compact_calls += 1
        return original_compact(connection, changed)

    monkeypatch.setattr(
        TaskStore,
        "_compact_if_changed",
        staticmethod(count_compaction),
    )
    TaskStore(path)
    assert compact_calls == 0


def test_legacy_retirement_does_not_complete_after_concurrent_reintroduction(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "legacy-completion-race.db"
    task = AgentTask(prompt="completion race", status=TaskStatus.COMPLETED)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                privacy_session TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, '', ?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                json.dumps(task.model_dump(mode="json")),
            ),
        )

    TaskStore(path)
    first_canary = "legacy-race-first-5B091C"
    with sqlite3.connect(path) as connection:
        payload = task.model_dump(mode="json")
        payload["privacy_session"] = first_canary
        connection.execute(
            "UPDATE tasks SET privacy_session = ?, payload = ? WHERE id = ?",
            (first_canary, json.dumps(payload), task.id),
        )

    original_verify = TaskStore._verify_legacy_chat_retirement
    second_canary = "legacy-race-second-71DAE4"
    injected = False

    def verify_then_reintroduce(self, connection, columns, bearers):
        nonlocal injected
        original_verify(self, connection, columns, bearers)
        if injected:
            return
        injected = True
        with sqlite3.connect(path) as writer:
            payload = task.model_dump(mode="json")
            payload["privacy_session"] = second_canary
            writer.execute(
                "UPDATE tasks SET privacy_session = ?, payload = ? WHERE id = ?",
                (second_canary, json.dumps(payload), task.id),
            )

    monkeypatch.setattr(
        TaskStore,
        "_verify_legacy_chat_retirement",
        verify_then_reintroduce,
    )
    TaskStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone() is None
        assert connection.execute(
            "SELECT privacy_session FROM tasks WHERE id = ?", (task.id,)
        ).fetchone() == (second_canary,)

    # The invalidated state is safely retried, rather than being overwritten
    # with a false completed marker by the first startup.
    TaskStore(path)
    with sqlite3.connect(path) as connection:
        marker = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone()
        row = connection.execute(
            "SELECT privacy_session, payload FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
    assert marker == ("physical-scrub-v1:completed",)
    assert row is not None and row[0] == ""
    assert "privacy_session" not in json.loads(row[1])


def test_waiting_legacy_writer_invalidates_completed_marker(tmp_path: Path):
    path = tmp_path / "legacy-waiting-writer.db"
    task = AgentTask(prompt="waiting writer", status=TaskStatus.COMPLETED)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                privacy_session TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, '', ?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                json.dumps(task.model_dump(mode="json")),
            ),
        )
    TaskStore(path)

    started = threading.Event()
    finished = threading.Event()
    writer_errors: list[Exception] = []
    canary = "legacy-waiting-writer-9C4F20"

    def waiting_writer():
        try:
            with sqlite3.connect(path, timeout=5) as connection:
                payload = task.model_dump(mode="json")
                payload["privacy_session"] = canary
                started.set()
                connection.execute(
                    "UPDATE tasks SET privacy_session = ?, payload = ? WHERE id = ?",
                    (canary, json.dumps(payload), task.id),
                )
        except Exception as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            finished.set()

    with sqlite3.connect(path) as completing:
        completing.execute("BEGIN IMMEDIATE")
        completing.execute(
            "UPDATE security_migrations SET value = ? WHERE name = ?",
            (
                "physical-scrub-v1:pending",
                "task-legacy-chat-isolation-retirement",
            ),
        )
        writer = threading.Thread(target=waiting_writer)
        writer.start()
        assert started.wait(1)
        assert not finished.wait(0.1)
        completing.execute(
            "UPDATE security_migrations SET value = ? "
            "WHERE name = ? AND value = ?",
            (
                "physical-scrub-v1:completed",
                "task-legacy-chat-isolation-retirement",
                "physical-scrub-v1:pending",
            ),
        )
        completing.commit()
    writer.join(timeout=5)

    assert not writer_errors
    assert finished.is_set()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            ("task-legacy-chat-isolation-retirement",),
        ).fetchone() is None


def test_runtime_settings_support_unlimited_steps_and_provider_output_limit(tmp_path: Path):
    store = RuntimeSettingsStore(
        tmp_path / "runtime-settings.json",
        RuntimeSettings(max_steps=40, max_output_tokens=8192),
    )

    updated = store.update(
        RuntimeSettingsPatch(
            model="deepseek-v4-pro",
            reasoning_effort="xhigh",
            max_steps=None,
            max_output_tokens=None,
        )
    )

    assert updated.model == "deepseek-v4-pro"
    assert updated.reasoning_effort == "xhigh"
    assert updated.max_steps is None
    assert updated.max_output_tokens is None
    reloaded = RuntimeSettingsStore(store.path, RuntimeSettings(max_steps=20))
    assert reloaded.value.max_steps is None
    assert reloaded.value.max_output_tokens is None
    assert reloaded.value.model == "deepseek-v4-pro"
    assert reloaded.value.reasoning_effort == "xhigh"


def test_runtime_settings_persist_cross_conversation_context_toggle(tmp_path: Path):
    store = RuntimeSettingsStore(tmp_path / "runtime-settings.json", RuntimeSettings())

    assert store.value.cross_conversation_context is True
    store.update(RuntimeSettingsPatch(cross_conversation_context=False))

    assert RuntimeSettingsStore(store.path, RuntimeSettings()).value.cross_conversation_context is False


def test_runtime_settings_persist_and_clear_custom_system_prompt_suffix(tmp_path: Path):
    store = RuntimeSettingsStore(tmp_path / "runtime-settings.json", RuntimeSettings())

    updated = store.update(
        RuntimeSettingsPatch(custom_system_prompt_suffix="Always include a runnable example.")
    )
    assert updated.custom_system_prompt_suffix == "Always include a runnable example."
    assert RuntimeSettingsStore(store.path, RuntimeSettings()).value.custom_system_prompt_suffix == (
        "Always include a runnable example."
    )

    cleared = store.update(RuntimeSettingsPatch(custom_system_prompt_suffix=""))
    assert cleared.custom_system_prompt_suffix == ""


def test_vision_runtime_selects_configured_or_supported_visual_model(tmp_path: Path):
    runtime = VisionRuntime(
        base_url="",
        api_key="",
        model="configured-model",
        auto_discover=True,
        start_command="",
        start_timeout=1,
        workspace=tmp_path,
    )

    assert runtime._select_model(["other", "configured-model"]) == "configured-model"
    assert runtime._select_model(["models/configured-model"]) == "configured-model"
    assert runtime._select_model(["text-only", "qwen2.5-vl"]) == "qwen2.5-vl"
    assert runtime._select_model(["models/gemini-3.6-flash"]) == "models/gemini-3.6-flash"
    assert runtime._select_model(["text-only"]) is None


@pytest.mark.asyncio
async def test_configured_vision_runtime_reports_unprobed_without_false_failure(tmp_path: Path):
    runtime = VisionRuntime(
        base_url="https://vision.example/v1",
        api_key="configured-key",
        model="gemini-test",
        auto_discover=False,
        start_command="",
        start_timeout=1,
        workspace=tmp_path,
    )

    status = await runtime.status(probe=False)

    assert status["ready"] is False
    assert status["configured"] is True
    assert status["probed"] is False
    assert status["source"] == "configured"
    assert status["base_url"] == "https://vision.example/v1"
    assert status["last_error"] == ""


@pytest.mark.asyncio
async def test_configured_vision_runtime_preserves_actionable_provider_error(
    tmp_path: Path, monkeypatch
):
    runtime = VisionRuntime(
        base_url="https://vision.example/v1",
        api_key="configured-key",
        model="gemini-test",
        auto_discover=False,
        start_command="",
        start_timeout=1,
        workspace=tmp_path,
    )

    async def rejected(_base_url: str):
        runtime.probe_errors[runtime.configured_url] = "User location is not supported"

    monkeypatch.setattr(runtime, "_models", rejected)

    assert await runtime.discover() is None
    status = await runtime.status(probe=False)
    assert status["ready"] is False
    assert status["base_url"] == "https://vision.example/v1"
    assert status["last_error"] == (
        "Semantic vision fallback unavailable: User location is not supported"
    )


def test_vision_key_change_invalidates_stale_probe_state(tmp_path: Path):
    runtime = VisionRuntime(
        base_url="https://vision.example/v1",
        api_key="old-key",
        model="gemini-test",
        auto_discover=False,
        start_command="",
        start_timeout=1,
        workspace=tmp_path,
    )
    runtime.endpoint = VisionEndpoint("https://vision.example/v1", "gemini-test", "configured")
    runtime.probed = True
    runtime.last_error = "old failure"

    runtime.set_api_key("new-key")

    assert runtime.endpoint is None
    assert runtime.probed is False
    assert runtime.last_error == ""


@pytest.mark.asyncio
async def test_vision_runtime_fails_over_from_region_blocked_google_to_gemini_relay(
    tmp_path: Path, monkeypatch
):
    runtime = VisionRuntime(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="direct-key",
        model="gemini-direct",
        auto_discover=False,
        start_command="",
        start_timeout=1,
        workspace=tmp_path,
        relay_api_key="relay-key",
        relay_base_url="https://relay.example/gemini",
        relay_model="gemini-relay",
    )

    async def direct_blocked(base_url: str):
        runtime.probe_errors[base_url] = "User location is not supported"

    async def relay_models(base_url: str, api_key: str):
        assert base_url == "https://relay.example/gemini"
        assert api_key == "relay-key"
        return ["models/gemini-relay"]

    monkeypatch.setattr(runtime, "_models", direct_blocked)
    monkeypatch.setattr(runtime, "_gemini_models", relay_models)

    endpoint = await runtime.discover()

    assert endpoint is not None
    assert endpoint.source == "aicodemirror-gemini"
    assert endpoint.protocol == "gemini"
    assert endpoint.model == "gemini-relay"
    assert endpoint.api_key == "relay-key"
    assert (await runtime.status(probe=False))["ready"] is True


@pytest.mark.asyncio
async def test_vision_tool_sends_native_gemini_image_payload(tmp_path: Path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    observed: dict[str, Any] = {}

    class FakeRuntime:
        api_key = ""

        async def ensure_ready(self):
            return VisionEndpoint(
                "https://relay.example/gemini",
                "gemini-relay",
                "aicodemirror-gemini",
                protocol="gemini",
                api_key="relay-key",
            )

    class FakeOCR:
        async def recognize(self, *_args):
            return {"text": "visible local text", "source": "local"}

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "Layout and contrast are clear."}]}}
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            observed.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("deepdesk.plugins.builtin.vision.asyncio.sleep", no_sleep)
    tool = VisionTool(
        FakeRuntime(),
        tmp_path,
        windows_ocr=FakeOCR(),
        paddle_ocr=FakeOCR(),
    )

    result = await tool.execute(
        {
            "image_path": str(image),
            "question": "check layout and contrast",
            "mode": "semantic",
        },
        ToolContext(task_id="vision-relay", workspace=str(tmp_path)),
    )

    assert observed["url"].endswith(
        "/v1beta/models/gemini-relay:generateContent"
    )
    assert observed["headers"]["x-goog-api-key"] == "relay-key"
    parts = observed["payload"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert result["description"] == "Layout and contrast are clear."
    assert result["semantic_verified"] is True
    assert result["source"] == "aicodemirror-gemini"


@pytest.mark.asyncio
async def test_vision_tool_retries_rate_limit_inside_tool(tmp_path: Path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeRuntime:
        api_key = ""

        async def ensure_ready(self):
            return SimpleNamespace(base_url="https://vision.test/v1", model="vision", source="test")

    class FakeResponse:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "retry", request=httpx.Request("POST", "https://vision.test"), response=self
                )

        def json(self):
            return {"choices": [{"message": {"content": "OCR ok"}}]}

    responses = [FakeResponse(429, {"retry-after": "0"}), FakeResponse(200)]

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return responses.pop(0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("deepdesk.plugins.builtin.vision.asyncio.sleep", no_sleep)
    tool = VisionTool(FakeRuntime(), tmp_path)

    result = await tool.execute(
        {"image_path": str(image), "question": "read"},
        ToolContext(task_id="vision", workspace=str(tmp_path)),
    )

    assert result["description"] == "OCR ok"
    assert result["attempts"] == [429, 200]
    assert result["retried"] is True


@pytest.mark.asyncio
async def test_vision_tool_uses_local_ocr_without_cloud_for_text(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "Local text", "source": "Windows.Media.Ocr"}

    class NoCloud:
        api_key = ""

        async def ensure_ready(self):
            raise AssertionError("Google fallback must not be called")

    tool = VisionTool(NoCloud(), tmp_path, windows_ocr=FakeLocalOCR())
    result = await tool.execute(
        {"image_path": str(image), "question": "read the text"},
        ToolContext(task_id="vision-local", workspace=str(tmp_path)),
    )

    assert result["description"] == "Local text"
    assert result["cloud_used"] is False
    assert result["provider_chain"] == ["windows-local-ocr:ok"]


@pytest.mark.asyncio
async def test_vision_tool_uses_local_paddle_for_complex_ocr(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "local evidence"}

    class FakePaddleOCR:
        async def recognize(self, _image):
            return {
                "text": "# Structured document",
                "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
                "source": "paddleocr-local-ai",
            }

    class NoGoogle:
        api_key = ""

        async def ensure_ready(self):
            raise AssertionError("Google fallback must not be called")

    tool = VisionTool(
        NoGoogle(),
        tmp_path,
        windows_ocr=FakeLocalOCR(),
        paddle_ocr=FakePaddleOCR(),
    )
    result = await tool.execute(
        {"image_path": str(image), "question": "convert to markdown"},
        ToolContext(task_id="vision-paddle", workspace=str(tmp_path)),
    )

    assert result["description"] == "# Structured document"
    assert result["provider_chain"][-1] == "paddleocr-local-ai:ok"
    assert result["cloud_used"] is False


@pytest.mark.asyncio
async def test_vision_tool_uses_cloud_only_for_semantic_screen_analysis(tmp_path: Path, monkeypatch):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "Settings Save Cancel"}

    class FakePaddleOCR:
        async def recognize(self, _image):
            return {"text": "Settings Save Cancel", "model": "PaddleOCR", "source": "local"}

    class FakeGoogle:
        api_key = ""

        async def ensure_ready(self):
            return SimpleNamespace(base_url="https://vision.test/v1", model="vision", source="test")

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "A settings dialog with Save and Cancel controls."}}]}

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FakeClient)

    tool = VisionTool(
        FakeGoogle(),
        tmp_path,
        windows_ocr=FakeLocalOCR(),
        paddle_ocr=FakePaddleOCR(),
    )
    result = await tool.execute(
        {"image_path": str(image), "question": "What does this screen mean?"},
        ToolContext(task_id="vision-semantic", workspace=str(tmp_path), egress_country="US"),
    )

    assert result["model"] == "vision"
    assert result["provider_chain"] == [
        "windows-local-ocr:ok",
        "paddleocr-local-ai:ok",
        "google-vision-fallback:ok",
    ]
    assert result["description"].startswith("A settings dialog")
    assert result["semantic_verified"] is True
    assert result["cloud_used"] is True


@pytest.mark.asyncio
async def test_vision_tool_serializes_concurrent_semantic_cloud_requests(
    tmp_path: Path, monkeypatch
):
    images = [tmp_path / "first.png", tmp_path / "second.png"]
    for image in images:
        image.write_bytes(b"png")

    class FakeGoogle:
        api_key = ""

        async def ensure_ready(self):
            return SimpleNamespace(base_url="https://vision.test/v1", model="vision", source="test")

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Semantic description"}}]}

    active_requests = 0
    maximum_active_requests = 0

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal active_requests, maximum_active_requests
            active_requests += 1
            maximum_active_requests = max(maximum_active_requests, active_requests)
            await asyncio.sleep(0.02)
            active_requests -= 1
            return FakeResponse()

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FakeClient)
    tool = VisionTool(FakeGoogle(), tmp_path)

    results = await asyncio.gather(
        *[
            tool.execute(
                {"image_path": str(image), "question": "Describe this screen"},
                ToolContext(task_id=f"vision-{index}", workspace=str(tmp_path)),
            )
            for index, image in enumerate(images)
        ]
    )

    assert maximum_active_requests == 1
    assert all(result["semantic_verified"] is True for result in results)


def test_vision_auto_mode_does_not_mistake_visual_quality_for_text_ocr():
    assert VisionTool._select_mode(
        {"question": "Is the text readable, with good color contrast and no overlap?"}
    ) == "semantic"
    assert VisionTool._select_mode(
        {"question": "检查文字是否清晰，有没有遮挡、裁切和颜色对比问题"}
    ) == "semantic"
    assert VisionTool._select_mode({"question": "read the text"}) == "local_ocr"


@pytest.mark.asyncio
async def test_mainland_china_uses_local_paddle_ai_and_never_google(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "local evidence"}

    class FakePaddleOCR:
        async def recognize(self, _image):
            return {
                "text": "飞桨识别文本",
                "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
                "source": "paddleocr-local-ai",
            }

    class NoGoogle:
        api_key = "configured"

        async def ensure_ready(self):
            raise AssertionError("Google must not receive mainland-China OCR images")

    tool = VisionTool(
        NoGoogle(),
        tmp_path,
        windows_ocr=FakeLocalOCR(),
        paddle_ocr=FakePaddleOCR(),
    )
    result = await tool.execute(
        {"image_path": str(image), "question": "read the text", "mode": "local_ocr"},
        ToolContext(
            task_id="vision-cn",
            workspace=str(tmp_path),
            egress_country="CN",
        ),
    )

    assert result["description"] == "飞桨识别文本"
    assert result["cloud_used"] is False
    assert result["egress_route"] == "mainland-china"
    assert result["provider_chain"][-1] == "paddleocr-local-ai:ok"


@pytest.mark.asyncio
async def test_mainland_china_explicit_semantic_mode_uses_configured_cloud(
    tmp_path: Path, monkeypatch
):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "Settings Save Cancel"}

    class FakePaddleOCR:
        async def recognize(self, _image):
            return {"text": "Settings Save Cancel", "model": "PaddleOCR", "source": "local"}

    class FakeGoogle:
        api_key = "configured"

        async def ensure_ready(self):
            return SimpleNamespace(base_url="https://vision.test/v1", model="vision", source="test")

    class FakeResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "The layout is clear and readable."}}]}

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return FakeResponse()

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FakeClient)
    tool = VisionTool(
        FakeGoogle(), tmp_path, windows_ocr=FakeLocalOCR(), paddle_ocr=FakePaddleOCR()
    )
    result = await tool.execute(
        {
            "image_path": str(image),
            "question": "Check layout, color contrast, clipping and overlap",
            "mode": "semantic",
            "cloud_fallback": True,
        },
        ToolContext(task_id="vision-cn-semantic", workspace=str(tmp_path), egress_country="CN"),
    )

    assert result["description"].startswith("The layout")
    assert result["cloud_used"] is True
    assert result["semantic_verified"] is True
    assert result["egress_route"] == "mainland-china-semantic-cloud"


@pytest.mark.asyncio
async def test_semantic_cloud_retries_transport_errors_without_claiming_success(
    tmp_path: Path, monkeypatch
):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class Local:
        async def recognize(self, *_args):
            return {"text": "local OCR evidence", "model": "local", "source": "local"}

    class Runtime:
        api_key = "configured"

        async def ensure_ready(self):
            return SimpleNamespace(base_url="https://vision.test/v1", model="vision", source="test")

    class FailingClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError("disconnected")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("deepdesk.semantic_vision.httpx.AsyncClient", FailingClient)
    monkeypatch.setattr("deepdesk.plugins.builtin.vision.asyncio.sleep", no_sleep)
    tool = VisionTool(Runtime(), tmp_path, windows_ocr=Local(), paddle_ocr=Local())
    result = await tool.execute(
        {
            "image_path": str(image),
            "question": "Check layout and contrast",
            "mode": "semantic",
        },
        ToolContext(task_id="vision-transport", workspace=str(tmp_path), egress_country="CN"),
    )

    assert result["attempts"] == ["RemoteProtocolError"] * 3
    assert result["retried"] is True
    # A transport failure does not prove that image bytes never left the host.
    assert result["cloud_used"] is True
    assert result["semantic_verified"] is False
    assert result["semantic_limited"] is True
    assert result["provider_chain"][-1] == "google-vision-fallback:failed"


def test_vision_tool_rejects_visibly_corrupted_ocr_text():
    assert VisionTool._has_usable_text("清晰的中文和 English text") is True
    assert VisionTool._has_usable_text("��Ŀ������� OCR ��IP&�Ƶ���") is False


@pytest.mark.asyncio
async def test_vision_tool_blocks_cloud_when_local_ocr_finds_credential(tmp_path: Path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    class FakeLocalOCR:
        async def recognize(self, _image, _languages):
            return {"text": "Authorization Bearer protected-token-123456789"}

    class NoGoogle:
        api_key = ""

        async def ensure_ready(self):
            raise AssertionError("Google must not receive sensitive screenshot")

    tool = VisionTool(
        NoGoogle(),
        tmp_path,
        windows_ocr=FakeLocalOCR(),
    )
    result = await tool.execute(
        {"image_path": str(image), "question": "understand this screen"},
        ToolContext(task_id="vision-sensitive", workspace=str(tmp_path)),
    )

    assert result["cloud_blocked"] is True
    assert result["cloud_used"] is False


def test_openclaw_bridge_cli_resolution_and_http_url(tmp_path: Path, monkeypatch):
    entry = (
        tmp_path
        / "work"
        / "openclaw-runtime"
        / "node_modules"
        / "openclaw"
        / "openclaw.mjs"
    )
    entry.parent.mkdir(parents=True)
    entry.write_text("// package-local OpenClaw", encoding="utf-8")
    external_cli = tmp_path / "global" / "openclaw.cmd"
    external_cli.parent.mkdir()
    external_cli.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(
        "deepdesk.openclaw_bridge.shutil.which",
        lambda name: "C:\\Program Files\\nodejs\\node.exe" if name == "node" else str(external_cli),
    )
    bridge = OpenClawBridge(
        enabled=True,
        cli=str(external_cli),
        gateway_url="wss://example.test:18789/path",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
    )

    assert bridge.resolve_cli() == str(entry.resolve())
    assert bridge.http_base_url == "https://example.test:18789"
    assert bridge._cli_command(str(entry), "gateway") == [
        "C:\\Program Files\\nodejs\\node.exe",
        str(entry.resolve()),
        "gateway",
    ]
    with pytest.raises(RuntimeError, match="was not selected by Elren"):
        bridge._cli_command(str(external_cli), "gateway")
    config = __import__("json").loads(bridge.config_path.read_text(encoding="utf-8"))
    assert config["agents"]["defaults"]["workspace"] == str(tmp_path.resolve())
    assert config["skills"]["load"]["watch"] is True
    assert config["tools"]["web"]["search"]["enabled"] is True
    assert config["tools"]["web"]["search"]["provider"] == "duckduckgo"
    assert config["plugins"]["entries"]["duckduckgo"]["enabled"] is True
    assert config["browser"]["ssrfPolicy"]["dangerouslyAllowPrivateNetwork"] is False
    assert "allowedHostnames" not in config["browser"]["ssrfPolicy"]
    assert bridge.web_search_provider == "duckduckgo"


@pytest.mark.asyncio
async def test_openclaw_catalog_is_cached_to_keep_capability_map_responsive(
    tmp_path: Path, monkeypatch
):
    bridge = OpenClawBridge(
        enabled=True,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=True,
        workspace=tmp_path,
    )
    calls = 0

    async def ready():
        return None

    async def gateway_call(method, params):
        nonlocal calls
        calls += 1
        assert method == "tools.catalog"
        assert params == {}
        return {"groups": [{"id": "fs", "tools": [{"name": "read"}]}]}

    monkeypatch.setattr(bridge, "ensure_ready", ready)
    monkeypatch.setattr(bridge, "_gateway_call", gateway_call)

    assert await bridge.catalog() == await bridge.catalog()
    assert calls == 1


def test_openclaw_bridge_uses_configured_computer_fallback_only_when_local_missing(
    tmp_path: Path, monkeypatch
):
    global_cli = tmp_path / "machine-wide-openclaw.cmd"
    global_cli.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr("deepdesk.openclaw_bridge.shutil.which", lambda _name: str(global_cli))

    package = tmp_path / "package"
    bridge = OpenClawBridge(
        enabled=True,
        cli=str(global_cli),
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=True,
        workspace=package,
    )

    assert bridge.resolve_cli() == str(global_cli.resolve())
    assert bridge._resolved_cli()[1] == "configured-fallback"
    assert bridge._cli_command(str(global_cli), "--version") == [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        str(global_cli.resolve()),
        "--version",
    ]


def test_openclaw_bridge_uses_path_fallback_when_local_and_configured_are_missing(
    tmp_path: Path, monkeypatch
):
    system_cli = tmp_path / "system" / "openclaw.cmd"
    system_cli.parent.mkdir()
    system_cli.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(
        "deepdesk.openclaw_bridge.shutil.which",
        lambda name: str(system_cli) if name == "openclaw" else "node.exe",
    )
    bridge = OpenClawBridge(
        True, "", "ws://127.0.0.1:18789", "", True, tmp_path / "package"
    )

    assert bridge.resolve_cli() == str(system_cli)
    assert bridge._resolved_cli()[1] == "system-fallback"


def test_openclaw_environment_discards_machine_wide_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "C:\\global-state")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "C:\\global-config.json")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "global-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-secret")
    bridge = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path)

    environment = bridge._environment()

    assert environment["OPENCLAW_STATE_DIR"] == str(bridge.state_dir)
    assert environment["OPENCLAW_CONFIG_PATH"] == str(bridge.config_path)
    assert environment["OPENCLAW_GATEWAY_TOKEN"] == bridge.gateway_token
    assert "ANTHROPIC_API_KEY" not in environment


@pytest.mark.asyncio
async def test_openclaw_health_bypasses_system_proxy_and_adopts_existing(
    tmp_path: Path, monkeypatch
):
    observed: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *_, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url):
            observed["url"] = url
            return FakeResponse()

    monkeypatch.setattr("deepdesk.openclaw_bridge.httpx.AsyncClient", FakeClient)
    bridge = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path)

    status = await bridge.status()

    assert observed["trust_env"] is False
    assert observed["url"] == f"{bridge.http_base_url}/healthz"
    assert bridge.gateway_url != "ws://127.0.0.1:18789"
    assert status["gateway_ready"] is True
    assert status["gateway_state"] == "ready"
    assert status["process_ownership"] == "adopted_existing"


def test_openclaw_startup_log_tail_is_bounded(tmp_path: Path):
    bridge = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path)
    bridge.gateway_log_path.write_text("prefix-" + "x" * 100, encoding="utf-8")

    assert bridge._startup_log_tail(12) == "x" * 12


@pytest.mark.asyncio
async def test_openclaw_owned_subprocess_is_reaped_when_parent_task_is_cancelled():
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False
            self.exited = asyncio.Event()

        async def communicate(self):
            await self.exited.wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exited.set()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

    process = FakeProcess()
    operation = asyncio.create_task(
        OpenClawBridge._communicate_process(
            process,
            timeout=60,
            operation="test operation",
        )
    )
    await asyncio.sleep(0)
    operation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await operation
    assert process.killed is True
    assert process.returncode == -9


def test_openclaw_local_gateway_is_stable_and_isolated_per_package(tmp_path: Path, monkeypatch):
    first = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path / "a")
    same = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path / "a")
    other = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path / "b")

    assert first.gateway_url == same.gateway_url
    assert first.gateway_url != other.gateway_url
    assert 20000 <= int(first.gateway_url.rsplit(":", 1)[1]) < 30000
    entry = first.openclaw_entry
    entry.parent.mkdir(parents=True)
    entry.write_text("// local", encoding="utf-8")
    monkeypatch.setattr("deepdesk.openclaw_bridge.shutil.which", lambda _name: "node.exe")
    command = first._cli_command(str(entry), "gateway", "run", "--allow-unconfigured")
    parsed_port = first.gateway_url.rsplit(":", 1)[1]
    command.extend(["--bind", "loopback", "--port", parsed_port])
    assert command[-4:] == ["--bind", "loopback", "--port", parsed_port]


def test_openclaw_prefers_package_local_node_independent_of_host_path(
    tmp_path: Path, monkeypatch
):
    bridge = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", True, tmp_path)
    entry = bridge.openclaw_entry
    entry.parent.mkdir(parents=True)
    entry.write_text("// local", encoding="utf-8")
    packaged_node = tmp_path / "work/node-runtime/node.exe"
    packaged_node.parent.mkdir(parents=True)
    packaged_node.write_bytes(b"package node")
    monkeypatch.setattr(
        "deepdesk.openclaw_bridge.shutil.which",
        lambda name: "C:/host/node.exe" if name == "node" else None,
    )

    command = bridge._cli_command(str(entry), "plugins", "list")

    assert command[0] == str(packaged_node.resolve())
    assert command[1] == str(entry.resolve())


def test_openclaw_mutations_always_require_high_risk(tmp_path: Path):
    bridge = OpenClawBridge(True, "", "ws://127.0.0.1:18789", "", False, tmp_path)
    tool = OpenClawBridgeTool(bridge)

    assert tool.risk({"action": "status"}) == Risk.SAFE
    assert tool.risk({"action": "catalog"}) == Risk.SAFE
    assert tool.risk({"action": "plugins"}) == Risk.SAFE
    assert tool.risk({"action": "invoke"}) == Risk.HIGH
    assert tool.risk({"action": "agent_exec"}) == Risk.HIGH


async def test_memory_roundtrip_search_and_secret_guard(tmp_path: Path):
    tool = MemoryTool(tmp_path / "deepdesk.db")
    context = ToolContext(task_id="test", workspace=str(tmp_path))

    stored = await tool.execute(
        {"action": "put", "title": "项目偏好", "content": "界面使用深色主题", "tags": ["ui"]},
        context,
    )
    found = await tool.execute({"action": "search", "query": "深色"}, context)

    assert found["items"][0]["id"] == stored["id"]
    with pytest.raises(PermissionError):
        await tool.execute(
            {"action": "put", "title": "密钥", "content": "sk-1234567890abcdefghijklmnop"},
            context,
        )


async def test_workspace_skill_discovery(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: Demonstration workflow\n---\n# Demo\nInstructions", encoding="utf-8"
    )
    tool = SkillsTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))

    result = await tool.execute({"action": "list"}, context)
    loaded = await tool.execute({"action": "read", "skill": "demo"}, context)

    assert result["skills"][0]["description"] == "Demonstration workflow"
    assert "Instructions" in loaded["content"]


async def test_workspace_skill_discovery_finds_bundled_nested_skills_deterministically(
    tmp_path: Path,
):
    direct = tmp_path / "skills" / "demo"
    bundled = tmp_path / "skills" / "openclaw-bundled" / "demo"
    hidden = tmp_path / "skills" / ".cache" / "hidden"
    for directory, description in (
        (direct, "Workspace override"),
        (bundled, "Bundled duplicate"),
        (hidden, "Must stay hidden"),
    ):
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\ndescription: {description}\n---\n# {description}\n",
            encoding="utf-8",
        )

    discovered = SkillsTool._discover(tmp_path)

    assert discovered["demo"] == (direct / "SKILL.md").resolve()
    assert "hidden" not in discovered


async def test_workspace_skill_read_is_bounded_and_symlink_escape_is_ignored(tmp_path: Path):
    oversized = tmp_path / "skills" / "oversized"
    oversized.mkdir(parents=True)
    (oversized / "SKILL.md").write_bytes(b"x" * (100_000 + 1))
    external = tmp_path / "outside" / "SKILL.md"
    external.parent.mkdir()
    external.write_text("outside workspace", encoding="utf-8")
    escaped = tmp_path / "skills" / "escaped"
    escaped.mkdir()
    try:
        (escaped / "SKILL.md").symlink_to(external)
    except OSError:
        pass

    tool = SkillsTool()
    context = ToolContext(task_id="test", workspace=str(tmp_path))
    listed = await tool.execute({"action": "list"}, context)

    assert "escaped" not in {item["name"] for item in listed["skills"]}
    with pytest.raises(ValueError, match="exceeds"):
        await tool.execute({"action": "read", "skill": "oversized"}, context)


async def test_web_blocks_local_network_and_extracts_readable_html():
    with pytest.raises(PermissionError):
        await WebTool()._validate_public_url("http://127.0.0.1:8765/api/status")

    text = WebTool._html_text("<h1>标题</h1><script>secret()</script><p>正文 <b>内容</b></p>")
    assert "标题" in text
    assert "正文" in text
    assert "secret" not in text


@pytest.mark.asyncio
async def test_deepseek_retries_incomplete_transport_response(monkeypatch):
    client = DeepSeekClient("https://api.example", "model", "key", "", timeout=1)
    attempts = 0

    async def fake_request(endpoint, key, messages, tools):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.RemoteProtocolError("incomplete chunked read")
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("deepdesk.deepseek.asyncio.sleep", no_sleep)

    reply = await client.chat([{"role": "user", "content": "test"}], [])

    assert reply.message["content"] == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_deepseek_recovery_uses_non_thinking_request(monkeypatch):
    client = DeepSeekClient("https://api.example", "model", "key", "", timeout=1)
    recovery_flags = []

    async def fake_request(endpoint, key, messages, tools, *, recovery=False):
        recovery_flags.append(recovery)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "actionable"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"completion_tokens": 4},
        }

    monkeypatch.setattr(client, "_request", fake_request)

    reply = await client.chat_recovery([{"role": "user", "content": "continue"}], [])

    assert recovery_flags == [True]
    assert reply.finish_reason == "stop"
    assert reply.message["content"] == "actionable"


@pytest.mark.asyncio
async def test_web_detects_proxy_fake_ip_mode_without_weakening_ssrf(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        address = "198.18.2.70" if host != "localhost" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("deepdesk.plugins.builtin.web.socket.getaddrinfo", fake_getaddrinfo)

    await WebTool()._validate_public_url("https://developers.openai.com/codex/")
    await WebTool(["example.com"])._validate_public_url("https://example.com/")
    with pytest.raises(PermissionError):
        await WebTool(["localhost"])._validate_public_url("http://localhost/")
    with pytest.raises(PermissionError):
        await WebTool()._validate_public_url("http://198.18.0.220/")


@pytest.mark.asyncio
async def test_web_does_not_assume_fake_ip_mode_from_one_target(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        address = "198.18.0.220" if host == "public-looking.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("deepdesk.plugins.builtin.web.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(PermissionError):
        await WebTool()._validate_public_url("https://public-looking.example/")


@pytest.mark.asyncio
async def test_web_still_blocks_private_lan_in_fake_ip_mode(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        address = "192.168.1.20" if host == "router.example" else "198.18.0.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("deepdesk.plugins.builtin.web.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(PermissionError):
        await WebTool()._validate_public_url("https://router.example/")


def test_web_parses_general_duckduckgo_html_results():
    source = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">Example News</a>
      <a class="result__snippet">A &amp; B current report</a>
    </div>
    """

    results = WebTool._parse_duckduckgo_html(source, 5)

    assert results == [
        {
            "title": "Example News",
            "url": "https://example.com/news",
            "snippet": "A & B current report",
        }
    ]


@pytest.mark.asyncio
async def test_background_browser_allows_loopback_web_app_previews(monkeypatch):
    tool = BackgroundBrowserTool()
    actions = []

    class FakeRoute:
        async def abort(self, reason):
            actions.append(("abort", reason))

        async def continue_(self):
            actions.append(("continue", None))

    class FakeRequest:
        url = "http://127.0.0.1:8765/api/status"

    await tool._route_request(FakeRoute(), FakeRequest())

    assert actions == [("continue", None)]


@pytest.mark.asyncio
async def test_background_browser_still_blocks_private_lan_requests(monkeypatch):
    tool = BackgroundBrowserTool()
    actions = []

    class FakeRoute:
        async def abort(self, reason):
            actions.append(("abort", reason))

        async def continue_(self):
            actions.append(("continue", None))

    class FakeRequest:
        url = "http://192.168.1.10/admin"

    await tool._route_request(FakeRoute(), FakeRequest())

    assert actions == [("abort", "blockedbyclient")]


@pytest.mark.asyncio
async def test_background_browser_allows_non_network_runtime_resources():
    tool = BackgroundBrowserTool()
    actions = []

    class FakeRoute:
        async def abort(self, reason):
            actions.append(("abort", reason))

        async def continue_(self):
            actions.append(("continue", None))

    class FakeRequest:
        url = "data:text/plain,background-browser"

    await tool._route_request(FakeRoute(), FakeRequest())

    assert actions == [("continue", None)]


@pytest.mark.asyncio
async def test_background_browser_file_urls_are_workspace_scoped_and_type_limited(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    page = workspace / "preview.html"
    page.write_text("<!doctype html><title>Local preview</title>", encoding="utf-8")
    secret = workspace / ".env"
    secret.write_text("TOKEN=not-for-browser", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("outside", encoding="utf-8")
    tool = BackgroundBrowserTool(workspace=workspace)
    context = ToolContext(task_id="file-policy", workspace=str(workspace))

    await tool._validate_navigation_url(page.as_uri(), context)
    with pytest.raises(PermissionError, match="browser-renderable"):
        await tool._validate_navigation_url(secret.as_uri(), context)
    with pytest.raises(PermissionError, match="outside"):
        await tool._validate_navigation_url(outside.as_uri(), context)


@pytest.mark.asyncio
async def test_background_browser_allows_workspace_file_subresources(tmp_path: Path):
    page = tmp_path / "preview.html"
    page.write_text("<!doctype html>", encoding="utf-8")
    tool = BackgroundBrowserTool(workspace=tmp_path)
    actions = []

    class FakeRoute:
        async def abort(self, reason):
            actions.append(("abort", reason))

        async def continue_(self):
            actions.append(("continue", None))

    class FakeRequest:
        url = page.as_uri()

    await tool._route_request(FakeRoute(), FakeRequest())

    assert actions == [("continue", None)]


def test_process_manager_resolves_path_from_system_lookup(monkeypatch):
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.Path.is_file", lambda _path: False)
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.process_manager.shutil.which",
        lambda name: "C:/resolved/msedge.exe" if name == "msedge.exe" else None,
    )

    assert ProcessManagerTool._resolve_application("msedge.exe") == "C:/resolved/msedge.exe"
    assert ProcessManagerTool._resolve_application("msedge") == "C:/resolved/msedge.exe"
    assert ProcessManagerTool._resolve_application("Edge") == "C:/resolved/msedge.exe"


def test_deepseek_native_search_normalizes_text_sources_and_usage():
    result = DeepSeekClient._normalize_web_search(
        {
            "stop_reason": "end_turn",
            "usage": {"server_tool_use": {"web_search_requests": 1}},
            "content": [
                {"type": "server_tool_use", "name": "web_search", "input": {"query": "q"}},
                {
                    "type": "web_search_tool_result",
                    "content": [
                        {"type": "web_search_result", "title": "Primary", "url": "https://example.com/a"}
                    ],
                },
                {
                    "type": "text",
                    "text": "grounded answer",
                    "citations": [
                        {"title": "Duplicate", "url": "https://example.com/a"},
                        {"title": "Second", "url": "https://example.com/b"},
                    ],
                },
            ],
        }
    )

    assert result["answer"] == "grounded answer"
    assert [item["url"] for item in result["sources"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["usage"]["server_tool_use"]["web_search_requests"] == 1


def test_deepseek_merges_streamed_reasoning_content_and_tool_calls():
    result = DeepSeekClient._merge_stream_events(
        [
            {
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "check ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "web", "arguments": "{\"action\":"},
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "source",
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": "\"fetch\"}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        ]
    )

    message = result["choices"][0]["message"]
    assert message["content"] == ""
    assert message["reasoning_content"] == "check source"
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"] == {
        "name": "web",
        "arguments": '{"action":"fetch"}',
    }
    assert result["usage"]["total_tokens"] == 42
    assert result["model"] == "deepseek-v4-pro"


def test_deepseek_stream_accumulator_drops_unused_hidden_reasoning():
    from deepdesk.deepseek import _StreamAccumulator

    accumulator = _StreamAccumulator(retain_final_reasoning=False)
    try:
        accumulator.feed(
            {
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "private reasoning",
                            "content": "visible answer",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 10},
            }
        )
        progress = accumulator.progress(12.5)
        result = accumulator.result()
    finally:
        accumulator.close()

    message = result["choices"][0]["message"]
    assert message["content"] == "visible answer"
    assert "reasoning_content" not in message
    assert progress["reasoning_chars"] == len("private reasoning")
    assert progress["content_chars"] == len("visible answer")
    assert progress["elapsed_seconds"] == 12.5


def test_deepseek_stream_progress_binding_is_context_local():
    client = DeepSeekClient("https://api.deepseek.com", "deepseek-v4-flash", "key", "")
    callback = lambda _progress: None
    assert client._task_stream_progress.get() is None
    token = client.bind_task_stream_progress(callback)
    try:
        assert client._task_stream_progress.get() is callback
    finally:
        client.reset_task_stream_progress(token)
    assert client._task_stream_progress.get() is None


def test_deepseek_v4_payload_respects_thinking_protocol():
    client = DeepSeekClient("https://api.deepseek.com", "deepseek-v4-flash", "key", "")
    messages = [{"role": "user", "content": "build"}]
    tools = [{"type": "function", "function": {"name": "filesystem"}}]

    normal = client._chat_payload(messages, tools)
    recovery = client._chat_payload(messages, tools, recovery=True)

    assert normal["thinking"] == {"type": "enabled"}
    assert normal["reasoning_effort"] == "high"
    assert normal["max_tokens"] == 384_000
    assert "tool_choice" not in normal
    assert recovery["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in recovery
    assert recovery["max_tokens"] == 384_000

    client.max_output_tokens = 65_536
    assert client._chat_payload(messages, tools)["max_tokens"] == 65_536
    model_token = client.bind_task_model("deepseek-v4-pro")
    output_token = client._task_max_output_tokens.set(512)
    try:
        routed = client._chat_payload(messages, tools)
        assert routed["model"] == "deepseek-v4-pro"
        assert routed["max_tokens"] == 512
    finally:
        client._task_max_output_tokens.reset(output_token)
        client.reset_task_model(model_token)


@pytest.mark.asyncio
async def test_provider_search_tool_delegates_only_valid_queries(tmp_path: Path):
    calls = []

    class FakeClient:
        async def provider_web_search(self, query, max_uses):
            calls.append((query, max_uses))
            return {
                "answer": "ok",
                "sources": [{"title": "Index", "url": "https://example.com/search"}],
            }

    tool = ProviderWebSearchTool(FakeClient())
    context = ToolContext(task_id="search", workspace=str(tmp_path))

    result = await tool.execute({"query": " latest DeepSeek docs ", "max_uses": 3}, context)

    assert result["answer"] == "ok"
    assert calls[0][1] == 3
    assert calls[0][0].startswith("latest DeepSeek docs\n\nCurrent-date verification instructions")
    assert "deepseek.com" in calls[0][0]
    assert result["verification"]["time_sensitive"] is True
    assert result["verification"]["official_source_found"] is False
    assert result["verification"]["required_official_domains"] == ["deepseek.com"]
    with pytest.raises(ValueError):
        await tool.execute({"query": " "}, context)
    with pytest.raises(PermissionError):
        await tool.execute({"query": "look up sk-1234567890abcdefghijklmnop"}, context)


@pytest.mark.asyncio
async def test_current_openai_search_requires_and_recognizes_official_source(tmp_path: Path):
    captured = {}

    class FakeClient:
        async def provider_web_search(self, query, max_uses):
            captured.update(query=query, max_uses=max_uses)
            return {
                "answer": "Current model catalog",
                "sources": [
                    {"title": "Rumor", "url": "https://example.com/gpt-rumor"},
                    {
                        "title": "Models | OpenAI API",
                        "url": "https://developers.openai.com/api/docs/models",
                    },
                ],
            }

    tool = ProviderWebSearchTool(FakeClient())
    context = ToolContext(task_id="openai-release", workspace=str(tmp_path))
    result = await tool.execute(
        {"query": "Is GPT-5.6 released and currently available?", "max_uses": 5},
        context,
    )

    assert "202" in captured["query"]
    assert "openai.com" in captured["query"]
    assert result["verification"]["official_source_found"] is True
    assert result["verification"]["required_official_domains"] == ["openai.com"]
    assert result["verification"]["official_sources"] == [
        {
            "title": "Models | OpenAI API",
            "url": "https://developers.openai.com/api/docs/models",
        }
    ]
