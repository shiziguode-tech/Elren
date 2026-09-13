from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk import secret_redaction
from deepdesk.audit import AuditLog
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry
from deepdesk.secret_redaction import REDACTED, redact_sensitive
from deepdesk.secret_storage import (
    AesGcmProtector,
    SecretStorageIntegrityError,
    SecretStorageUnavailable,
    WindowsDPAPIProtector,
)
from deepdesk.task_store import TaskStore

EXACT_CANARY = "opaque-provider-canary-7F3A91E6C2D84B50"
VENDOR_CANARIES = (
    "sk-proj-abcdefghijklmnop123456",
    "AIzaSyAabcdefghijklmnopqrstuvwx",
    "github_pat_abcdefghijklmnop123456",
    "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef",
)


def _route_protector() -> AesGcmProtector:
    return AesGcmProtector("test-task-route-aesgcm", b"r" * 32)


def _all_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _assert_canaries_absent(path: Path, canaries: tuple[str, ...]) -> None:
    payload = path.read_bytes()
    for canary in canaries:
        assert canary.encode() not in payload, f"plaintext credential persisted in {path}"


def test_generic_vendor_and_structural_credentials_never_enter_sqlite_or_wal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.db"
    store = TaskStore(database)
    task = AgentTask(
        prompt=" ".join(VENDOR_CANARIES),
        context_prompt=(
            'normal text {"api_key":"unlabelled-custom-value-123",'
            '"nested":{"client_secret":"oauth-secret-456"}}'
        ),
    )

    store.save(task)

    # Runtime input is detached from the serialized copy.
    assert all(canary in task.prompt for canary in VENDOR_CANARIES)
    restored = store.get(task.id)
    assert restored is not None
    assert REDACTED in restored.prompt
    for path in (database, database.with_name("tasks.db-wal"), database.with_name("tasks.db-shm")):
        if path.exists():
            _assert_canaries_absent(path, (*VENDOR_CANARIES, "unlabelled-custom-value-123", "oauth-secret-456"))


def test_remote_reply_route_is_protected_and_round_trips_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "remote.db"
    recipient = "telegram-private-route-981723"
    task = AgentTask(
        prompt=f"Reply through {recipient}",
        source="telegram",
        remote_recipient_id=recipient,
        remote_recipient_type="chat_id",
        status=TaskStatus.COMPLETED,
        result="done",
        events=[TaskEvent(type="channel", data={"chat_id": recipient})],
    )
    store = TaskStore(database, route_protector=_route_protector())

    store.save(task)

    # The live task retains its route for immediate delivery, while every
    # durable copy contains only an authenticated current-user envelope.
    assert task.remote_recipient_id == recipient
    with store._connect() as connection:
        payload = str(
            connection.execute(
                "SELECT payload FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()[0]
        )
    assert "elren-local-route:v1:test-task-route-aesgcm:" in payload
    for path in (
        database,
        database.with_name("remote.db-wal"),
        database.with_name("remote.db-shm"),
    ):
        if path.exists():
            _assert_canaries_absent(path, (recipient,))

    restored = TaskStore(
        database,
        route_protector=_route_protector(),
    ).get(task.id)
    assert restored is not None
    assert restored.remote_recipient_id == recipient
    assert restored.prompt == f"Reply through {REDACTED}"
    assert restored.events[0].data["chat_id"] == REDACTED


def test_legacy_plaintext_remote_route_is_migrated_and_physically_compacted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-route.db"
    recipient = "legacy-telegram-route-771192"
    initial = TaskStore(database, route_protector=_route_protector())
    task = AgentTask(
        prompt="legacy remote task",
        source="telegram",
        remote_recipient_id=recipient,
        remote_recipient_type="chat_id",
        status=TaskStatus.COMPLETED,
        result="done",
    )
    with initial._connect() as connection:
        connection.execute(
            "INSERT INTO tasks(id,status,created_at,updated_at,payload) VALUES(?,?,?,?,?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                task.model_dump_json(),
            ),
        )
    assert any(
        path.exists() and recipient.encode() in path.read_bytes()
        for path in (
            database,
            database.with_name("legacy-route.db-wal"),
            database.with_name("legacy-route.db-shm"),
        )
    )

    migrated = TaskStore(database, route_protector=_route_protector())
    restored = migrated.get(task.id)

    assert restored is not None
    assert restored.remote_recipient_id == recipient
    for path in (
        database,
        database.with_name("legacy-route.db-wal"),
        database.with_name("legacy-route.db-shm"),
    ):
        if path.exists():
            _assert_canaries_absent(path, (recipient,))


def test_remote_route_envelope_is_bound_to_its_task_and_rejects_swapping(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tampered-route.db"
    store = TaskStore(database, route_protector=_route_protector())
    first = AgentTask(
        prompt="first",
        source="telegram",
        remote_recipient_id="route-first-991",
        remote_recipient_type="chat_id",
    )
    second = AgentTask(
        prompt="second",
        source="telegram",
        remote_recipient_id="route-second-992",
        remote_recipient_type="chat_id",
    )
    store.save(first)
    store.save(second)
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT id, payload FROM tasks ORDER BY id"
        ).fetchall()
        payloads = [(task_id, json.loads(payload)) for task_id, payload in rows]
        first_envelope = payloads[0][1]["remote_recipient_id"]
        second_envelope = payloads[1][1]["remote_recipient_id"]
        payloads[0][1]["remote_recipient_id"] = second_envelope
        payloads[1][1]["remote_recipient_id"] = first_envelope
        for task_id, payload in payloads:
            connection.execute(
                "UPDATE tasks SET payload = ? WHERE id = ?",
                (json.dumps(payload, separators=(",", ":")), task_id),
            )

    with pytest.raises(
        SecretStorageIntegrityError,
        match="does not match its task",
    ):
        TaskStore(database, route_protector=_route_protector())


def test_remote_route_persistence_fails_closed_without_credential_backend(
    tmp_path: Path,
) -> None:
    class UnavailableProtector:
        algorithm = "unavailable-test-protector"

        def protect(self, _plaintext: bytes) -> bytes:
            raise RuntimeError("backend offline")

        def unprotect(self, _protected: bytes) -> bytes:
            raise RuntimeError("backend offline")

    database = tmp_path / "unavailable-route.db"
    store = TaskStore(database, route_protector=UnavailableProtector())
    ordinary = AgentTask(prompt="ordinary local web task")
    store.save(ordinary)
    assert store.get(ordinary.id) is not None

    recipient = "must-not-fallback-44129"
    with pytest.raises(
        SecretStorageUnavailable,
        match="could not protect a remote reply route",
    ):
        store.save(
            AgentTask(
                prompt="remote",
                source="telegram",
                remote_recipient_id=recipient,
                remote_recipient_type="chat_id",
            )
        )
    _assert_canaries_absent(database, (recipient,))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_remote_route_round_trips_with_real_windows_dpapi(tmp_path: Path) -> None:
    database = tmp_path / "dpapi-route.db"
    recipient = "dpapi-telegram-route-552210"
    store = TaskStore(
        database,
        route_protector=WindowsDPAPIProtector(),
    )
    task = AgentTask(
        prompt="real DPAPI route",
        source="telegram",
        remote_recipient_id=recipient,
        remote_recipient_type="chat_id",
    )
    store.save(task)

    restored = TaskStore(
        database,
        route_protector=WindowsDPAPIProtector(),
    ).get(task.id)

    assert restored is not None
    assert restored.remote_recipient_id == recipient
    _assert_canaries_absent(database, (recipient,))


def test_exact_configured_value_migration_scrubs_legacy_db_and_free_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    store = TaskStore(database)
    task = AgentTask(prompt=f"legacy prose contains {EXACT_CANARY}")

    # Simulate a pre-guard row without routing plaintext through the new save boundary.
    unsafe_payload = task.model_dump_json()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO tasks(id,status,created_at,updated_at,payload) VALUES(?,?,?,?,?)",
            (
                task.id,
                task.status.value,
                task.created_at,
                task.updated_at,
                unsafe_payload,
            ),
        )

    legacy_files = (
        database,
        database.with_name("legacy.db-wal"),
        database.with_name("legacy.db-shm"),
    )
    assert any(
        path.exists() and EXACT_CANARY.encode() in path.read_bytes()
        for path in legacy_files
    )
    store.set_persistence_redactor(
        lambda value, _task_id: redact_sensitive(value, [EXACT_CANARY])
    )

    restored = store.get(task.id)
    assert restored is not None
    assert EXACT_CANARY not in restored.prompt
    for path in (database, database.with_name("legacy.db-wal"), database.with_name("legacy.db-shm")):
        if path.exists():
            _assert_canaries_absent(path, (EXACT_CANARY,))


def test_exact_configured_value_resanitizes_legacy_audit_log(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "legacy",
                "task_id": "task",
                "data": {"message": f"opaque value {EXACT_CANARY}"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    audit = AuditLog(path)
    audit.set_secret_values(lambda: [EXACT_CANARY])
    assert audit.resanitize_existing_secrets() is True

    _assert_canaries_absent(path, (EXACT_CANARY,))
    assert REDACTED in path.read_text(encoding="utf-8")


def test_task_store_warm_restart_does_not_recursively_redact_safe_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "deepdesk.db"
    initial = TaskStore(database)
    for index in range(40):
        initial.save(AgentTask(prompt=f"ordinary persisted task {index}"))

    rewritten_row_counts: list[int] = []
    original = TaskStore._rewrite_rows

    def counted_rewrite(self, connection, rows, *, exact):
        materialized = list(rows)
        rewritten_row_counts.append(len(materialized))
        return original(self, connection, materialized, exact=exact)

    monkeypatch.setattr(TaskStore, "_rewrite_rows", counted_rewrite)
    restarted = TaskStore(database)
    restarted.set_persistence_redactor(
        lambda value, _task_id: redact_sensitive(value, [EXACT_CANARY]),
        secret_values=lambda: [EXACT_CANARY],
    )

    # The durable generic policy marker skips all 40 rows. The exact SQL
    # preflight finds no candidates, so no historical payload enters Python's
    # recursive redactor on an ordinary restart.
    assert sum(rewritten_row_counts) == 0
    assert len(restarted.load_recent(limit=100)) == 40
    assert sum(rewritten_row_counts) == 0


def test_task_store_new_configured_secret_resanitizes_only_matching_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "deepdesk.db"
    newly_configured = 'opaque-new-value-91\n"quoted"'
    configured = [EXACT_CANARY]
    store = TaskStore(database)
    store.set_persistence_redactor(
        lambda value, _task_id: redact_sensitive(value, configured),
        secret_values=lambda: list(configured),
    )
    matching = AgentTask(prompt=f"legacy value: {newly_configured}")
    store.save(matching)
    for index in range(25):
        store.save(AgentTask(prompt=f"unrelated history {index}"))

    rewritten_row_counts: list[int] = []
    original = store._rewrite_rows

    def counted_rewrite(connection, rows, *, exact):
        materialized = list(rows)
        rewritten_row_counts.append(len(materialized))
        return original(connection, materialized, exact=exact)

    monkeypatch.setattr(store, "_rewrite_rows", counted_rewrite)
    configured[:] = [EXACT_CANARY, newly_configured]

    restored = store.get(matching.id)

    assert restored is not None
    assert newly_configured not in restored.prompt
    assert REDACTED in restored.prompt
    assert rewritten_row_counts == [1]


def test_audit_exact_preflight_skips_safe_log_and_detects_new_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "audit.jsonl"
    newly_configured = 'opaque-audit-value-72\n"quoted"'
    path.write_text(
        json.dumps(
            {"event": "legacy", "data": {"message": f"value={newly_configured}"}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    audit = AuditLog(path)
    configured = [EXACT_CANARY]
    audit.set_secret_values(lambda: list(configured))

    calls = 0
    original = __import__("deepdesk.audit", fromlist=["sanitize_audit_value"]).sanitize_audit_value

    def counted_sanitize(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("deepdesk.audit.sanitize_audit_value", counted_sanitize)
    assert audit.resanitize_existing_secrets() is True
    assert calls == 0

    configured.append(newly_configured)
    assert audit.resanitize_existing_secrets() is True
    assert calls > 0
    assert newly_configured not in path.read_text(encoding="utf-8")

    calls = 0
    assert audit.resanitize_existing_secrets() is True
    assert calls == 0


def test_agent_engine_automatically_resanitizes_exact_legacy_audit_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "legacy",
                "task_id": "task",
                "data": {"message": f"opaque value {EXACT_CANARY}"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    AgentEngine(
        client=SimpleNamespace(keys=[EXACT_CANARY], provider_models=[]),
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(path),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda _task, _event_type, _data: None,
    )

    _assert_canaries_absent(path, (EXACT_CANARY,))
    assert REDACTED in path.read_text(encoding="utf-8")


class _RuntimeCaptureEngine:
    def __init__(self) -> None:
        self.received_prompt = ""
        self.emit = None

    @staticmethod
    def _redact(value):
        return redact_sensitive(value, [EXACT_CANARY], include_session_leases=False)

    @staticmethod
    def _redact_for_persistence(value, _task_id=""):
        return redact_sensitive(value, [EXACT_CANARY])

    async def run(self, task, _cancel, _steering_queue):
        self.received_prompt = task.prompt
        task.status = TaskStatus.COMPLETED
        task.result = f"provider returned {EXACT_CANARY}"
        task.error = f"diagnostic repeated {EXACT_CANARY}"
        self.emit(
            task,
            "assistant",
            {
                "content": f"answer includes {EXACT_CANARY}",
                "nested": {"opaque": EXACT_CANARY},
            },
        )


class _FailingRuntimeEngine(_RuntimeCaptureEngine):
    async def run(self, task, _cancel, _steering_queue):
        self.received_prompt = task.prompt
        raise RuntimeError(f"provider exception contains {EXACT_CANARY}")


@pytest.mark.asyncio
async def test_runtime_prompt_stays_raw_but_task_audit_and_logs_are_safe(
    tmp_path: Path,
) -> None:
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    engine_logger = logging.getLogger("deepdesk.engine")
    engine_logger.addHandler(handler)
    engine_logger.setLevel(logging.INFO)
    try:
        engine = _RuntimeCaptureEngine()
        store = TaskStore(tmp_path / "deepdesk.db")
        manager = TaskManager(
            engine,
            ApprovalGate(),
            store,
        )
        engine.emit = manager.emit
        task = manager.create(
            f"use this current credential once: {EXACT_CANARY}",
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
        )
        await manager.background[task.id]

        assert engine.received_prompt.endswith(EXACT_CANARY)
        assert task.prompt.endswith(EXACT_CANARY)
        assert EXACT_CANARY not in (task.result or "")
        assert EXACT_CANARY not in (task.error or "")
        assert EXACT_CANARY not in json.dumps(
            [event.model_dump(mode="json") for event in task.events],
            ensure_ascii=False,
        )

        audit = AuditLog(tmp_path / "audit.jsonl")
        audit_engine = AgentEngine(
            client=SimpleNamespace(keys=[EXACT_CANARY], provider_models=[]),
            registry=PluginRegistry(),
            approvals=ApprovalGate(),
            human_actions=HumanActionGate(),
            audit=audit,
            workspace=str(tmp_path),
            max_steps=None,
            emit=lambda _task, _event_type, _data: None,
        )
        await audit_engine.audit.write(
            "provider_failure",
            task.id,
            {"error": f"unlabelled configured value {EXACT_CANARY}"},
        )
        # Direct AuditLog has the generic safety net; AgentEngine's facade adds
        # exact configured values before reaching it.
        await audit.write(
            "provider_failure",
            task.id,
            {"authorization": VENDOR_CANARIES[0]},
        )

        manager.emit(task, "status", {"status": task.status.value})
        durable_paths = _all_files(tmp_path)
        for path in durable_paths:
            _assert_canaries_absent(path, (EXACT_CANARY,))
        _assert_canaries_absent(tmp_path / "audit.jsonl", (VENDOR_CANARIES[0],))

        failing_engine = _FailingRuntimeEngine()
        failing_manager = TaskManager(failing_engine, ApprovalGate())
        failed = failing_manager.create(
            f"runtime failure request {EXACT_CANARY}",
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
        )
        await failing_manager.background[failed.id]
        assert failing_engine.received_prompt.endswith(EXACT_CANARY)
        assert EXACT_CANARY not in (failed.error or "")
        assert EXACT_CANARY not in log_stream.getvalue()
    finally:
        engine_logger.removeHandler(handler)


def test_checkpoint_archive_uses_exact_configured_value_redaction(tmp_path: Path) -> None:
    client = SimpleNamespace(keys=[EXACT_CANARY], provider_models=[])
    engine = AgentEngine(
        client=client,
        registry=PluginRegistry(),
        approvals=ApprovalGate(),
        human_actions=HumanActionGate(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        workspace=str(tmp_path),
        max_steps=None,
        emit=lambda _task, _event_type, _data: None,
    )
    task = AgentTask(prompt=f"runtime goal {EXACT_CANARY}")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": task.prompt},
        {"role": "assistant", "content": f"provider echo {EXACT_CANARY}"},
    ]

    engine._compact_context_messages(
        task,
        messages,
        checkpoint_number=1,
        reason="test",
        cumulative_ledger=[],
        checkpoint_archives=[],
        runtime_state={"opaque": EXACT_CANARY},
        target_tokens=8_000,
        active_schemas=[],
    )

    assert task.prompt.endswith(EXACT_CANARY)
    archive = tmp_path / "data" / "context-checkpoints" / f"{task.id}-0001.json"
    _assert_canaries_absent(archive, (EXACT_CANARY,))
    assert REDACTED in archive.read_text(encoding="utf-8")


def test_long_plain_tool_output_skips_backtracking_assignment_patterns(monkeypatch):
    """Large credential-like output must use one bounded linear parser pass.

    This is a call-count regression rather than a machine-speed threshold: the
    old expressions could spend unbounded time backtracking over a long string
    that contained no credential assignment at all.
    """

    calls: list[str] = []

    original_parser = secret_redaction._redact_assignments_linear

    def probe(value, *, include_session_leases):
        calls.append(len(value))
        return original_parser(
            value, include_session_leases=include_session_leases
        )

    monkeypatch.setattr(
        secret_redaction, "_redact_assignments_linear", probe
    )

    # Include a credential-looking word and delimiter: this was the concrete
    # worst case for the old regexes, yet it is not an assignment.
    result = secret_redaction.redact_text("api:" + "x" * 60_000)

    assert result == "api:" + "x" * 60_000
    assert calls == [60_004]

    assert secret_redaction.redact_text('API_KEY="opaque-value"') == (
        'API_KEY="[REDACTED]"'
    )
    assert secret_redaction.redact_text('API_KEY="a\\\"b"') == (
        'API_KEY="[REDACTED]"'
    )
    assert secret_redaction.redact_text("密钥：秘密值") == "密钥：[REDACTED]"
