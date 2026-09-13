from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepdesk.models import AgentTask, TaskEvent, TaskStatus, utc_now
from deepdesk.secret_redaction import persistence_secret_needles, redact_sensitive
from deepdesk.secret_storage import (
    SecretProtector,
    SecretStorageFormatError,
    SecretStorageIntegrityError,
    SecretStorageUnavailable,
    select_secret_protector,
)

logger = logging.getLogger(__name__)

_GENERIC_REDACTION_MARKER = "task-payload-generic-redaction"
_EXACT_REDACTION_MARKER = "task-payload-exact-process"
_GENERIC_REDACTION_POLICY = "generic-redaction-v3-linear-assignments"
_REMOTE_ROUTE_MARKER = "task-remote-route-encryption"
_REMOTE_ROUTE_POLICY = "current-user-protected-v1"
_REMOTE_ROUTE_PREFIX = "elren-local-route:v1:"
_REMOTE_CONTEXT_SOURCES = frozenset({"feishu", "telegram"})
_LEGACY_CHAT_RETIREMENT_MARKER = "task-legacy-chat-isolation-retirement"
_LEGACY_CHAT_RETIREMENT_PENDING = "physical-scrub-v1:pending"
_LEGACY_CHAT_RETIREMENT_COMPLETED = "physical-scrub-v1:completed"

_CONTEXT_STOP_TERMS = {
    "the", "and", "for", "with", "this", "that", "please", "task", "agent", "elren",
    "continue", "fix", "make", "use", "using", "work", "result", "done",
    "这个", "那个", "这些", "那些", "问题", "功能", "任务", "工具", "进行", "使用",
    "帮我", "需要", "然后", "还有", "继续", "修复", "完成", "结果", "软件", "项目",
    # Shared platform / reporting boilerplate is not a shared task subject.
    "mac", "macos", "windows", "linux", "test", "tests", "testing", "report",
    "其他", "实际", "真实", "报告", "全程", "验收", "通过", "联网", "搜索",
    "不联", "不读", "不调", "只使", "网搜",
}
_CONTEXT_STOP_CHARS = set("这那个的是了着在为与和及并把被让要需用进行帮我问题功能任务软件项目修复继续完成结果")


@dataclass(frozen=True, slots=True)
class _PreparedTaskWrite:
    """Immutable, already-redacted/encrypted snapshot; never accept from an API."""
    task_id: str
    status: str
    created_at: str
    updated_at: str
    payload: str


class TaskStore:
    """Small durable task/event store backed by SQLite."""

    def __init__(
        self,
        path: Path,
        persistence_redactor: Callable[[Any, str], Any] | None = None,
        *,
        route_protector: SecretProtector | None = None,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._persistence_redactor = persistence_redactor
        # Resolve the OS-backed protector lazily: ordinary local/web tasks do
        # not need a credential backend, while a remote reply route must never
        # fall back to plaintext if that backend is unavailable. ``main``
        # passes the already-selected provider-vault protector so both stores
        # share the same current-user security boundary.
        self._route_protector = route_protector
        self._secret_values: Callable[[], Iterable[Any]] | None = None
        self._exact_process_marker = secrets.token_hex(24)
        self._exact_values_fingerprint: str | None = None
        self._exact_unknown_checked = False
        # A cancelled worker can take a few moments to unwind after the user
        # deletes its chat. Remember deletions for this process lifetime so a
        # late final save/title update cannot resurrect the removed record.
        self._deleted_ids: set[str] = set()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Own one transaction and close its connection on every exit path.

        SQLite's native context manager commits/rolls back but does not close.
        Keep that transaction contract while releasing handles immediately,
        including PRAGMA setup failures before the caller receives the handle.
        """
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def set_persistence_redactor(
        self,
        redactor: Callable[[Any, str], Any] | None,
        *,
        secret_values: Callable[[], Iterable[Any]] | None = None,
    ) -> None:
        """Install exact-value redaction and resanitize legacy durable rows.

        Generic credential shapes are always removed.  ``TaskManager`` adds
        the live provider-key redactor here after settings have been loaded,
        without ever copying those keys into this store object.
        """

        with self._lock:
            self._persistence_redactor = redactor
            self._secret_values = secret_values
            self._exact_values_fingerprint = None
            self._exact_unknown_checked = False
            self._ensure_persistence_sanitized()

    def _redact_payload(self, value: Any, task_id: str = "") -> Any:
        redactor = self._persistence_redactor
        redacted = value
        if redactor is not None:
            try:
                redacted = redactor(value, task_id)
            except Exception:
                # Persistence must continue in a safe degraded mode even if an
                # optional settings-backed exact-value lookup fails.
                logger.warning(
                    "Exact task persistence redaction failed for %s; using generic redaction",
                    task_id,
                )
        return redact_sensitive(redacted)

    def _get_route_protector(self) -> SecretProtector:
        protector = self._route_protector
        if protector is None:
            try:
                protector = select_secret_protector()
            except Exception as exc:
                raise SecretStorageUnavailable(
                    "A current-user OS credential store is required to persist "
                    "remote reply routes"
                ) from exc
            self._route_protector = protector
        return protector

    @staticmethod
    def _route_context(value: dict[str, Any]) -> dict[str, str]:
        return {
            "task_id": str(value.get("id") or ""),
            "source": str(value.get("source") or ""),
            "recipient_type": str(value.get("remote_recipient_type") or ""),
        }

    def _protect_remote_route(self, value: dict[str, Any], recipient: str) -> str:
        protector = self._get_route_protector()
        context = self._route_context(value)
        plaintext = json.dumps(
            {
                "version": 1,
                **context,
                "recipient": recipient,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            protected = protector.protect(plaintext)
        except Exception as exc:
            raise SecretStorageUnavailable(
                "The current-user credential store could not protect a remote reply route"
            ) from exc
        algorithm = str(getattr(protector, "algorithm", "") or "")
        if not algorithm or ":" in algorithm:
            raise SecretStorageFormatError(
                "The remote reply route protector identifier is invalid"
            )
        encoded = base64.urlsafe_b64encode(protected).decode("ascii").rstrip("=")
        return f"{_REMOTE_ROUTE_PREFIX}{algorithm}:{encoded}"

    def _unprotect_remote_route(self, value: dict[str, Any], envelope: str) -> str:
        suffix = envelope[len(_REMOTE_ROUTE_PREFIX):]
        try:
            algorithm, encoded = suffix.split(":", 1)
        except ValueError as exc:
            raise SecretStorageFormatError(
                "The protected remote reply route header is malformed"
            ) from exc
        protector = self._get_route_protector()
        current_algorithm = str(getattr(protector, "algorithm", "") or "")
        legacy_dpapi_name = bool(
            algorithm == "windows-dpapi"
            and current_algorithm == "windows-dpapi-current-user"
        )
        if algorithm != current_algorithm and not legacy_dpapi_name:
            raise SecretStorageUnavailable(
                f"This remote reply route requires {algorithm}; the current "
                f"protector is {current_algorithm or 'unavailable'}"
            )
        try:
            padding = "=" * (-len(encoded) % 4)
            protected = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise SecretStorageFormatError(
                "The protected remote reply route payload is malformed"
            ) from exc
        try:
            plaintext = protector.unprotect(protected)
        except Exception as exc:
            raise SecretStorageIntegrityError(
                "The protected remote reply route failed authentication"
            ) from exc
        try:
            decoded = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStorageIntegrityError(
                "The protected remote reply route content is malformed"
            ) from exc
        expected = self._route_context(value)
        if (
            not isinstance(decoded, dict)
            or decoded.get("version") != 1
            or any(decoded.get(name) != item for name, item in expected.items())
            or not isinstance(decoded.get("recipient"), str)
            or not decoded["recipient"]
        ):
            raise SecretStorageIntegrityError(
                "The protected remote reply route does not match its task"
            )
        return str(decoded["recipient"])

    def _deserialize_task(self, payload: str | bytes) -> AgentTask:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("Task payload is malformed") from exc
        if not isinstance(value, dict):
            raise ValueError("Task payload must be an object")
        recipient = str(value.get("remote_recipient_id") or "")
        if recipient.startswith(_REMOTE_ROUTE_PREFIX):
            value["remote_recipient_id"] = self._unprotect_remote_route(
                value,
                recipient,
            )
        elif recipient:
            raise SecretStorageFormatError(
                "A plaintext remote reply route reached the task read boundary"
            )
        return AgentTask.model_validate(value)

    def _serialize_task(self, task: AgentTask) -> str:
        raw_payload = task.model_dump(mode="json")
        # Server-only continuation history is intentionally absent from public
        # model_dump/poll payloads. Persist it through the same redaction path
        # as all other task text, without broadcasting it on every UI poll.
        raw_payload["continuation_instructions"] = list(task.continuation_instructions)
        raw_payload["continuation_legacy_context"] = task.continuation_legacy_context
        recipient = str(raw_payload.get("remote_recipient_id") or "")
        payload = self._redact_payload(raw_payload, task.id)
        if recipient:
            # A route can also appear in channel event metadata. Remove exact
            # task-local copies there without adding chat IDs to the global
            # credential redactor (which would break live delivery/retry).
            payload = redact_sensitive(payload, [recipient])
            payload["remote_recipient_id"] = self._protect_remote_route(
                raw_payload,
                recipient,
            )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _marker(connection: sqlite3.Connection, name: str) -> str:
        row = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?", (name,)
        ).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _set_marker(connection: sqlite3.Connection, name: str, value: str) -> None:
        connection.execute(
            "INSERT INTO security_migrations(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value),
        )

    @staticmethod
    def _complete_legacy_chat_retirement(connection: sqlite3.Connection) -> bool:
        """Atomically transition an unchanged pending retirement to completed."""

        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                "UPDATE security_migrations SET value = ? "
                "WHERE name = ? AND value = ?",
                (
                    _LEGACY_CHAT_RETIREMENT_COMPLETED,
                    _LEGACY_CHAT_RETIREMENT_MARKER,
                    _LEGACY_CHAT_RETIREMENT_PENDING,
                ),
            )
            completed = cursor.rowcount == 1
            connection.commit()
            return completed
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _compact_if_changed(connection: sqlite3.Connection, changed: int) -> None:
        if not changed:
            return
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @staticmethod
    def _retire_legacy_chat_isolation_metadata(
        connection: sqlite3.Connection,
        columns: set[str],
    ) -> int:
        """Neutralize metadata written by the removed private-chat feature.

        Existing installations may still have the two old columns and the
        matching JSON keys.  Keep that database readable, but erase the old
        bearer value and flag so neither a stale client nor a downgrade can
        silently reactivate the retired behavior.
        """

        assignments: list[str] = []
        predicates: list[str] = []
        if "private_chat" in columns:
            assignments.append("private_chat = 0")
            predicates.append("COALESCE(private_chat, 0) <> 0")
        if "privacy_session" in columns:
            assignments.append("privacy_session = ''")
            predicates.append("COALESCE(privacy_session, '') <> ''")
        changed = 0
        if assignments:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} "
                f"WHERE {' OR '.join(predicates)}"
            )
            changed += max(0, cursor.rowcount)

        for task_id, payload in connection.execute(
            "SELECT id, payload FROM tasks "
            "WHERE instr(payload, '\"private_chat\"') > 0 "
            "OR instr(payload, '\"privacy_session\"') > 0"
        ).fetchall():
            try:
                value = json.loads(payload)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            removed = False
            for field in ("private_chat", "privacy_session"):
                if field in value:
                    value.pop(field, None)
                    removed = True
            if not removed:
                continue
            connection.execute(
                "UPDATE tasks SET payload = ? WHERE id = ?",
                (
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    task_id,
                ),
            )
            changed += 1
        return changed

    @staticmethod
    def _install_legacy_chat_retirement_triggers(
        connection: sqlite3.Connection,
        columns: set[str],
    ) -> None:
        """Invalidate completed retirement if legacy data is written again."""

        payload_has_legacy = (
            "json_valid(NEW.payload) AND ("
            "json_type(NEW.payload, '$.private_chat') IS NOT NULL OR "
            "json_type(NEW.payload, '$.privacy_session') IS NOT NULL)"
        )
        column_checks: list[str] = []
        legacy_columns: list[str] = []
        if "private_chat" in columns:
            column_checks.append("COALESCE(NEW.private_chat, 0) <> 0")
            legacy_columns.append("private_chat")
        if "privacy_session" in columns:
            column_checks.append("COALESCE(NEW.privacy_session, '') <> ''")
            legacy_columns.append("privacy_session")
        insert_checks = [payload_has_legacy, *column_checks]
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS invalidate_legacy_chat_retirement_on_insert
            AFTER INSERT ON tasks
            WHEN {' OR '.join(f'({check})' for check in insert_checks)}
            BEGIN
                DELETE FROM security_migrations
                WHERE name = '{_LEGACY_CHAT_RETIREMENT_MARKER}';
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS invalidate_legacy_chat_retirement_on_payload_update
            AFTER UPDATE OF payload ON tasks
            WHEN {payload_has_legacy}
            BEGIN
                DELETE FROM security_migrations
                WHERE name = '{_LEGACY_CHAT_RETIREMENT_MARKER}';
            END
            """
        )
        if legacy_columns:
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS invalidate_legacy_chat_retirement_on_column_update
                AFTER UPDATE OF {', '.join(legacy_columns)} ON tasks
                WHEN {' OR '.join(f'({check})' for check in column_checks)}
                BEGIN
                    DELETE FROM security_migrations
                    WHERE name = '{_LEGACY_CHAT_RETIREMENT_MARKER}';
                END
                """
            )

    @staticmethod
    def _legacy_chat_bearers(
        connection: sqlite3.Connection,
        columns: set[str],
    ) -> tuple[str, ...]:
        """Collect legacy bearer values in memory for post-VACUUM verification."""

        values: set[str] = set()
        if "privacy_session" in columns:
            for (raw_value,) in connection.execute(
                "SELECT DISTINCT privacy_session FROM tasks "
                "WHERE COALESCE(privacy_session, '') <> ''"
            ).fetchall():
                value = str(raw_value or "")
                if value:
                    values.add(value)
        for (payload,) in connection.execute(
            "SELECT payload FROM tasks "
            "WHERE instr(payload, '\"privacy_session\"') > 0"
        ).fetchall():
            try:
                decoded = json.loads(payload)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                continue
            if isinstance(decoded, dict):
                value = str(decoded.get("privacy_session") or "")
                if value:
                    values.add(value)
        return tuple(values)

    def _verify_legacy_chat_retirement(
        self,
        connection: sqlite3.Connection,
        columns: set[str],
        bearers: tuple[str, ...],
    ) -> None:
        """Verify logical retirement and the completed SQLite physical rewrite."""

        predicates: list[str] = []
        if "private_chat" in columns:
            predicates.append("COALESCE(private_chat, 0) <> 0")
        if "privacy_session" in columns:
            predicates.append("COALESCE(privacy_session, '') <> ''")
        if predicates and connection.execute(
            f"SELECT 1 FROM tasks WHERE {' OR '.join(predicates)} LIMIT 1"
        ).fetchone():
            raise SecretStorageIntegrityError(
                "Legacy private-chat columns remain populated after retirement"
            )
        if connection.execute(
            "SELECT 1 FROM tasks WHERE json_valid(payload) AND ("
            "json_type(payload, '$.private_chat') IS NOT NULL OR "
            "json_type(payload, '$.privacy_session') IS NOT NULL) LIMIT 1"
        ).fetchone():
            raise SecretStorageIntegrityError(
                "Legacy private-chat payload keys remain after retirement"
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).casefold() != "ok":
            raise SecretStorageIntegrityError(
                "Task database integrity check failed after private-chat retirement"
            )
        if int(connection.execute("PRAGMA freelist_count").fetchone()[0]) != 0:
            raise SecretStorageIntegrityError(
                "Task database still has free pages after private-chat retirement"
            )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        paths = (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )
        for path in paths:
            if not path.is_file():
                continue
            raw = path.read_bytes()
            for bearer in bearers:
                candidates = {
                    bearer.encode("utf-8"),
                    json.dumps(bearer, ensure_ascii=False)[1:-1].encode("utf-8"),
                    json.dumps(bearer, ensure_ascii=True)[1:-1].encode("ascii"),
                }
                if any(candidate and candidate in raw for candidate in candidates):
                    raise SecretStorageIntegrityError(
                        "Legacy private-chat bearer bytes remain after SQLite compaction"
                    )

    def _ensure_remote_routes_encrypted(self) -> None:
        """Migrate durable remote recipients to current-user protected envelopes."""

        with self._lock, self._connect() as connection:
            if self._marker(connection, _REMOTE_ROUTE_MARKER) == _REMOTE_ROUTE_POLICY:
                return
            rows = connection.execute("SELECT id, payload FROM tasks").fetchall()
            changed = 0
            migrated_recipients: list[str] = []
            for _task_id, payload in rows:
                try:
                    value = json.loads(payload)
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                    # Preserve the store's longstanding tolerant handling of
                    # unrelated corrupt history. A damaged row that actually
                    # names the protected field is different: accepting it
                    # could leave a route in plaintext, so fail closed without
                    # deleting that row.
                    raw_text = (
                        payload.decode("utf-8", errors="ignore")
                        if isinstance(payload, bytes)
                        else str(payload or "")
                    )
                    if "remote_recipient_id" in raw_text:
                        raise SecretStorageFormatError(
                            "A task with a possible remote reply route is malformed; "
                            "route migration stopped without deleting history"
                        ) from exc
                    continue
                if not isinstance(value, dict):
                    continue
                recipient = str(value.get("remote_recipient_id") or "")
                if not recipient:
                    continue
                if recipient.startswith(_REMOTE_ROUTE_PREFIX):
                    # Authenticate the envelope and its task binding before
                    # accepting the durable migration marker.
                    self._unprotect_remote_route(value, recipient)
                    continue
                try:
                    task = AgentTask.model_validate(value)
                except (ValueError, TypeError) as exc:
                    raise SecretStorageFormatError(
                        "A legacy remote task cannot be validated; route migration "
                        "stopped without deleting history"
                    ) from exc
                safe_payload = self._serialize_task(task)
                connection.execute(
                    "UPDATE tasks SET payload = ? WHERE id = ?",
                    (safe_payload, task.id),
                )
                migrated_recipients.append(recipient)
                changed += 1
            connection.commit()
            self._compact_if_changed(connection, changed)

        if migrated_recipients:
            # VACUUM plus a truncated WAL must remove the legacy plaintext
            # bytes, not merely hide them from logical SELECTs. Verify without
            # logging or persisting any recipient value. The durable marker is
            # written only after this physical check, so an interrupted VACUUM
            # is retried rather than silently accepted on the next startup.
            paths = (
                self.path,
                self.path.with_name(f"{self.path.name}-wal"),
                self.path.with_name(f"{self.path.name}-shm"),
            )
            for path in paths:
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                for recipient in migrated_recipients:
                    candidates = {
                        recipient.encode("utf-8"),
                        json.dumps(recipient, ensure_ascii=False)[1:-1].encode(
                            "utf-8"
                        ),
                        json.dumps(recipient, ensure_ascii=True)[1:-1].encode(
                            "ascii"
                        ),
                    }
                    if any(candidate and candidate in raw for candidate in candidates):
                        raise SecretStorageIntegrityError(
                            "Legacy remote reply route bytes remain after SQLite compaction"
                        )

        with self._lock, self._connect() as connection:
            self._set_marker(
                connection,
                _REMOTE_ROUTE_MARKER,
                _REMOTE_ROUTE_POLICY,
            )

    def _rewrite_rows(
        self,
        connection: sqlite3.Connection,
        rows: Iterable[tuple[Any, Any]],
        *,
        exact: bool,
    ) -> int:
        changed = 0
        for task_id, payload in rows:
            try:
                decoded = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                # Leave corrupt rows for the existing tolerant loader; do not
                # risk changing recovery semantics during sanitization.
                continue
            safe_value = (
                self._redact_payload(decoded, str(task_id))
                if exact
                else redact_sensitive(decoded)
            )
            # Avoid rewriting every safe legacy row merely because its JSON
            # whitespace differs from the current compact serializer.
            if safe_value == decoded:
                continue
            safe_payload = json.dumps(
                safe_value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            connection.execute(
                "UPDATE tasks SET payload = ? WHERE id = ?",
                (safe_payload, task_id),
            )
            changed += 1
        return changed

    def _ensure_generic_sanitized(self) -> None:
        """Run the expensive generic legacy migration once per policy version."""

        with self._lock, self._connect() as connection:
            if self._marker(connection, _GENERIC_REDACTION_MARKER) == _GENERIC_REDACTION_POLICY:
                return
            rows = connection.execute("SELECT id, payload FROM tasks").fetchall()
            changed = self._rewrite_rows(connection, rows, exact=False)
            self._set_marker(
                connection,
                _GENERIC_REDACTION_MARKER,
                _GENERIC_REDACTION_POLICY,
            )
            self._set_marker(
                connection,
                _REMOTE_ROUTE_MARKER,
                _REMOTE_ROUTE_POLICY,
            )
            connection.commit()
            self._compact_if_changed(connection, changed)

    @staticmethod
    def _fingerprint_needles(needles: tuple[str, ...]) -> str:
        digest = hashlib.sha256(b"Elren task exact-redaction values v1\0")
        for needle in needles:
            encoded = needle.encode("utf-8", errors="surrogatepass")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    @staticmethod
    def _candidate_rows(
        connection: sqlite3.Connection,
        needles: tuple[str, ...],
    ) -> list[tuple[Any, Any]]:
        if not needles:
            return []
        found: dict[str, tuple[Any, Any]] = {}
        # Stay far below SQLite's host-parameter limit while keeping the scan
        # in SQLite's C implementation instead of recursively redacting every
        # task in Python on every startup.
        for offset in range(0, len(needles), 200):
            chunk = needles[offset:offset + 200]
            predicate = " OR ".join("instr(payload, ?) > 0" for _ in chunk)
            for task_id, payload in connection.execute(
                f"SELECT id, payload FROM tasks WHERE {predicate}",
                chunk,
            ).fetchall():
                found[str(task_id)] = (task_id, payload)
        return list(found.values())

    def _ensure_exact_sanitized(self) -> None:
        redactor = self._persistence_redactor
        if redactor is None:
            return
        values_source = self._secret_values
        with self._lock, self._connect() as connection:
            marker_matches = (
                self._marker(connection, _EXACT_REDACTION_MARKER)
                == self._exact_process_marker
            )
            if values_source is None:
                if marker_matches and self._exact_unknown_checked:
                    return
                # Compatibility path for direct/custom redactors which cannot
                # expose their exact-value source separately. It remains safe,
                # but intentionally cannot be cached across processes.
                rows = connection.execute("SELECT id, payload FROM tasks").fetchall()
                changed = self._rewrite_rows(connection, rows, exact=True)
                self._exact_unknown_checked = True
            else:
                try:
                    needles = persistence_secret_needles(values_source())
                except Exception as exc:
                    raise RuntimeError(
                        "Configured secret values are unavailable for task migration"
                    ) from exc
                fingerprint = self._fingerprint_needles(needles)
                if marker_matches and self._exact_values_fingerprint == fingerprint:
                    return
                rows = self._candidate_rows(connection, needles)
                changed = self._rewrite_rows(connection, rows, exact=True)
                self._exact_values_fingerprint = fingerprint
            self._set_marker(
                connection,
                _EXACT_REDACTION_MARKER,
                self._exact_process_marker,
            )
            self._set_marker(
                connection,
                _REMOTE_ROUTE_MARKER,
                _REMOTE_ROUTE_POLICY,
            )
            connection.commit()
            self._compact_if_changed(connection, changed)

    def _ensure_persistence_sanitized(self) -> None:
        # Generic shapes have a durable policy marker. Exact secrets use a
        # process marker plus an in-memory value fingerprint: raw SQL writes
        # invalidate both via triggers, while no secret-derived hash is ever
        # persisted to disk.
        self._ensure_remote_routes_encrypted()
        self._ensure_generic_sanitized()
        self._ensure_exact_sanitized()

    def _restore_sanitization_markers(self, connection: sqlite3.Connection) -> None:
        self._set_marker(
            connection,
            _REMOTE_ROUTE_MARKER,
            _REMOTE_ROUTE_POLICY,
        )
        self._set_marker(
            connection,
            _GENERIC_REDACTION_MARKER,
            _GENERIC_REDACTION_POLICY,
        )
        if self._persistence_redactor is not None and (
            self._exact_values_fingerprint is not None or self._exact_unknown_checked
        ):
            self._set_marker(
                connection,
                _EXACT_REDACTION_MARKER,
                self._exact_process_marker,
            )

    def _sanitize_existing_records(self) -> int:
        """Compatibility helper: force a full current-redactor migration."""

        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT id, payload FROM tasks").fetchall()
            changed = self._rewrite_rows(connection, rows, exact=True)
            connection.commit()
            self._compact_if_changed(connection, changed)
            return changed

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_pins (task_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS remove_deleted_task_pin AFTER DELETE ON tasks "
                "BEGIN DELETE FROM task_pins WHERE task_id = OLD.id; END"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS security_migrations ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            # Keep this guard installed after retirement: an old binary or raw
            # SQL writer can otherwise repopulate retired bearer metadata after
            # the completed marker has been persisted.
            self._install_legacy_chat_retirement_triggers(connection, columns)
            legacy_state = self._marker(
                connection, _LEGACY_CHAT_RETIREMENT_MARKER
            )
            legacy_pending = legacy_state != _LEGACY_CHAT_RETIREMENT_COMPLETED
            legacy_bearers: tuple[str, ...] = ()
            legacy_changes = 0
            if legacy_pending:
                legacy_bearers = self._legacy_chat_bearers(connection, columns)
                # This marker and the logical cleanup commit atomically. If the
                # process dies before VACUUM, the next startup sees ``pending``
                # and forces another physical rewrite even when no rows change.
                self._set_marker(
                    connection,
                    _LEGACY_CHAT_RETIREMENT_MARKER,
                    _LEGACY_CHAT_RETIREMENT_PENDING,
                )
                legacy_changes = self._retire_legacy_chat_isolation_metadata(
                    connection, columns
                )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS invalidate_task_redaction_on_insert
                AFTER INSERT ON tasks
                BEGIN
                    DELETE FROM security_migrations
                    WHERE name IN (
                        'task-payload-generic-redaction',
                        'task-payload-exact-process'
                    );
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS invalidate_task_redaction_on_payload_update
                AFTER UPDATE OF payload ON tasks
                BEGIN
                    DELETE FROM security_migrations
                    WHERE name IN (
                        'task-payload-generic-redaction',
                        'task-payload-exact-process'
                    );
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS invalidate_task_route_encryption_on_insert
                AFTER INSERT ON tasks
                BEGIN
                    DELETE FROM security_migrations
                    WHERE name = 'task-remote-route-encryption';
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS invalidate_task_route_encryption_on_payload_update
                AFTER UPDATE OF payload ON tasks
                BEGIN
                    DELETE FROM security_migrations
                    WHERE name = 'task-remote-route-encryption';
                END
                """
            )
            if legacy_pending:
                connection.commit()
                self._compact_if_changed(connection, max(1, legacy_changes))
                self._verify_legacy_chat_retirement(
                    connection,
                    columns,
                    legacy_bearers,
                )
                if self._complete_legacy_chat_retirement(connection):
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._ensure_remote_routes_encrypted()

    def save(self, task: AgentTask) -> None:
        self._ensure_persistence_sanitized()
        self._write_prepared(self.prepare_save(task))

    def prepare_save(self, task: AgentTask) -> _PreparedTaskWrite:
        """Capture live redaction context before handing immutable bytes to IO."""
        # Serialize a detached copy.  The live ``task`` deliberately retains
        # the user's exact current request so the selected model can act on it;
        # only the database/history copy is redacted.
        payload = self._serialize_task(task)
        return _PreparedTaskWrite(task.id, task.status.value, task.created_at, task.updated_at, payload)

    def save_prepared(self, prepared: _PreparedTaskWrite) -> None:
        """Write only an internally prepared snapshot, retaining deletion fences."""
        self._ensure_persistence_sanitized()
        self._write_prepared(prepared)

    def _write_prepared(self, prepared: _PreparedTaskWrite) -> None:
        with self._lock, self._connect() as connection:
            if prepared.task_id in self._deleted_ids:
                return
            connection.execute(
                """
                INSERT INTO tasks(id, status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    prepared.task_id,
                    prepared.status,
                    prepared.created_at,
                    prepared.updated_at,
                    prepared.payload,
                ),
            )
            self._restore_sanitization_markers(connection)

    def load_recent(self, limit: int = 100) -> list[AgentTask]:
        self._ensure_persistence_sanitized()
        with self._lock, self._connect() as connection:
            self._recover_interrupted_tasks(connection)
            self._restore_sanitization_markers(connection)
            rows = connection.execute(
                "SELECT payload FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        tasks: list[AgentTask] = []
        for (payload,) in rows:
            try:
                tasks.append(self._deserialize_task(payload))
            except (ValueError, TypeError):
                continue
        return tasks

    def _recover_interrupted_tasks(self, connection: sqlite3.Connection) -> int:
        """Atomically terminalize every task left active by a previous process.

        Startup keeps only a bounded in-memory task cache, but recovery must not
        share that bound.  Otherwise an older queued/running record remains
        permanently active in SQLite even though no worker can own it anymore.
        """

        active_values = tuple(
            status.value
            for status in (
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.WAITING,
                TaskStatus.WAITING_USER,
            )
        )
        placeholders = ",".join("?" for _ in active_values)
        rows = connection.execute(
            f"SELECT id, payload FROM tasks WHERE status IN ({placeholders})",
            active_values,
        ).fetchall()
        recovered = 0
        for task_id, payload in rows:
            try:
                task = self._deserialize_task(payload)
            except (ValueError, TypeError):
                continue
            if task.status.value not in active_values:
                continue
            previous_status = task.status.value
            task.status = TaskStatus.FAILED
            task.error = "Elren 重启时任务仍在运行；已安全标记为中断"
            task.updated_at = utc_now()
            for run in task.subagent_runs:
                if run.status in {"queued", "running"}:
                    run.status = "interrupted"
                    run.error_code = "host_restart"
                    run.finished_at = task.updated_at
            task.events.append(
                TaskEvent(
                    type="restart_interrupted",
                    data={
                        "previous_status": previous_status,
                        "message": task.error,
                    },
                )
            )
            if len(task.events) > 1000:
                task.events = task.events[-1000:]
            connection.execute(
                "UPDATE tasks SET status=?, updated_at=?, payload=? WHERE id=?",
                (
                    task.status.value,
                    task.updated_at,
                    self._serialize_task(task),
                    task_id,
                ),
            )
            recovered += 1
        return recovered

    def get(self, task_id: str) -> AgentTask | None:
        """Load one persisted task by id, including tasks outside the startup cache."""
        self._ensure_persistence_sanitized()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return self._deserialize_task(row[0])
        except (ValueError, TypeError):
            return None

    def list_restart_interrupted(self, source: str) -> list[AgentTask]:
        """Return every channel task eligible for automatic restart recovery.

        ``TaskManager`` intentionally caches only the newest 100 conversations,
        but a crash can leave more than 100 remote tasks active.  Recovery must
        query durable state rather than silently ignoring interrupted tasks
        that fell outside that presentation cache.
        """

        self._ensure_persistence_sanitized()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM tasks WHERE status = ? ORDER BY updated_at ASC",
                (TaskStatus.FAILED.value,),
            ).fetchall()
        interrupted: list[AgentTask] = []
        for (payload,) in rows:
            try:
                task = self._deserialize_task(payload)
            except (ValueError, TypeError):
                continue
            error = str(task.error or "")
            if (
                task.source == source
                and error.startswith("Elren 重启时任务仍在运行")
                and "已由任务" not in error
            ):
                interrupted.append(task)
        return interrupted

    @staticmethod
    def _context_terms(text: str) -> set[str]:
        lowered = text.casefold()
        words = {
            item for item in re.findall(r"[a-z0-9_\-]{2,}", lowered)
            if item not in _CONTEXT_STOP_TERMS
        }
        # Keep punctuation/Latin/whitespace boundaries. Concatenating all Han
        # characters manufactured terms such as 南京 from "南 HTML 京".
        for chinese in re.findall(r"[\u3400-\u9fff]+", lowered):
            words.update(
                term
                for index in range(max(0, len(chinese) - 1))
                if len(term := chinese[index:index + 2]) == 2
                and not any(character in _CONTEXT_STOP_CHARS for character in term)
            )
        words.difference_update(_CONTEXT_STOP_TERMS)
        return words

    @staticmethod
    def _redact_context(text: str) -> str:
        value = str(text or "")
        patterns = (
            (r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已隐藏密钥]"),
            (r"\bAQ\.[A-Za-z0-9_-]{8,}\b", "[已隐藏令牌]"),
            (r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}", "Bearer [已隐藏]"),
            (
                r"(?i)((?:api[_ -]?key|secret|token|password|密钥|密码)\s*[:=：]\s*)[^\s,，;；]{6,}",
                r"\1[已隐藏]",
            ),
        )
        for pattern, replacement in patterns:
            value = re.sub(pattern, replacement, value)
        return value

    @staticmethod
    def _compress_context_turn(value: str) -> str:
        """Keep salient middle constraints while reducing a long turn to about one fifth."""
        if len(value) <= 1200:
            return value
        target = max(320, (len(value) + 4) // 5)
        marker = "\n…[历史对话已压缩至约五分之一]…\n"
        available = max(120, target - len(marker))
        head_budget = int(available * 0.27)
        tail_budget = int(available * 0.25)
        salient_budget = int(available * 0.38)
        middle_budget = max(0, available - head_budget - tail_budget - salient_budget)
        salient_pattern = re.compile(
            r"(?i)(关键|约束|必须|不得|不要|未完成|待处理|错误|失败|决定|文件|路径|"
            r"版本|端口|模型|设置|保留|时区|数值|must|required|never|constraint|"
            r"decision|unresolved|pending|error|fail|file|path|version|port|model|setting)"
        )
        sentences = re.split(r"(?<=[。！？.!?])\s*|\n+", value)
        salient_parts: list[str] = []
        salient_chars = 0
        for sentence in sentences:
            candidate = sentence.strip()
            if not candidate or not salient_pattern.search(candidate):
                continue
            if candidate in salient_parts:
                continue
            remaining = salient_budget - salient_chars
            if remaining <= 0:
                break
            salient_parts.append(candidate[:remaining])
            salient_chars += min(len(candidate), remaining) + 1
        salient = "\n".join(salient_parts)[:salient_budget]
        if middle_budget:
            center = len(value) // 2
            half = middle_budget // 2
            middle = value[max(0, center - half):center + (middle_budget - half)]
        else:
            middle = ""
        compressed = (
            value[:head_budget]
            + marker
            + salient
            + ("\n…\n" if salient and middle else "")
            + middle
            + value[-tail_budget:]
        )
        return compressed[:target]

    def build_cross_conversation_context(
        self,
        prompt: str,
        *,
        source: str = "",
        remote_recipient_id: str = "",
        remote_recipient_type: str = "",
        limit: int = 50,
        max_chars: int = 64_000,
    ) -> tuple[str, list[str]]:
        """Rank relevant chats within the latest 50 turns with recency decay."""
        normalized_source = str(source or "").strip().casefold()
        normalized_recipient_id = str(remote_recipient_id or "").strip()
        normalized_recipient_type = str(remote_recipient_type or "").strip().casefold()
        remote_scope = bool(normalized_recipient_id or normalized_recipient_type) or (
            normalized_source in _REMOTE_CONTEXT_SOURCES
        )
        # A remote request without its complete reply route cannot be assigned a
        # safe history partition. Fail closed instead of recalling another
        # Telegram chat or Feishu recipient merely because the source matches.
        if remote_scope and not (
            normalized_source
            and normalized_recipient_id
            and normalized_recipient_type
        ):
            return "", []
        self._ensure_persistence_sanitized()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM tasks
                WHERE status = ?
                ORDER BY updated_at DESC
                """,
                (TaskStatus.COMPLETED.value,),
            ).fetchall()
        candidates: list[tuple[float, int, float, AgentTask]] = []
        current_terms = self._context_terms(prompt)
        continuity = bool(
            re.search(r"(?i)(上次|之前|刚才|继续|以前|earlier|previous|last time|as before)", prompt)
        )
        direct_follow_up = bool(
            re.search(
                # Bare 其他 is also used for unrelated categories (其他功能),
                # and unbounded "them" matched "themes". Neither is evidence
                # that the immediately preceding chat is the intended subject.
                r"(?i)(刚才|重试|再试|其余|剩下|未完成|上述|前述|"
                r"其他\s*[一二三四五六七八九十两百\d]+\s*[个项份种]|"
                r"\b(?:retry|try again|remaining|the other|those|them)\b)",
                prompt,
            )
        )
        # A short answer to a settings-ambiguity question belongs to the
        # immediately preceding turn even when it has no lexical overlap.
        confirmation_text = re.sub(
            r"^\s*(?:\u8bed\u97f3\s*\d*\s*\u8f6c\u5199\s*[:\uff1a]\s*)",
            "",
            prompt,
            flags=re.IGNORECASE,
        )
        confirmation_follow_up = bool(
            re.fullmatch(
                r"(?i)\s*(?:\u662f|\u662f\u7684|\u662f\u554a|\u5bf9|\u5bf9\u7684|\u5bf9\u554a|"
                r"\u6ca1\u9519|\u786e\u8ba4|\u53ef\u4ee5|\u597d|\u597d\u7684|\u55ef|\u55ef\u55ef|"
                r"\u4e0d\u662f|\u4e0d\u662f\u7684|\u4e0d\u5bf9|\u5426|"
                r"yes|correct|confirm|confirmed|no|not that)\s*[.!\u3002\uff01]?\s*",
                confirmation_text,
            )
        )
        valid_tasks: list[AgentTask] = []
        for (payload,) in rows:
            try:
                task = self._deserialize_task(payload)
            except (ValueError, TypeError):
                continue
            if task.status != TaskStatus.COMPLETED or not task.result:
                continue
            if task.project_path:
                # Repository conversations are private to their own task until
                # a project-aware history index exists; global recall must not
                # import code or decisions from an unrelated repository.
                continue
            if normalized_source and task.source.strip().casefold() != normalized_source:
                continue
            if remote_scope and (
                task.remote_recipient_id.strip() != normalized_recipient_id
                or task.remote_recipient_type.strip().casefold()
                != normalized_recipient_type
            ):
                continue
            recency = len(valid_tasks)
            valid_tasks.append(task)
            history_terms = self._context_terms(f"{task.prompt} {task.result}")
            shared_terms = current_terms & history_terms
            overlap = len(shared_terms)
            # Every earlier chat remains eligible. Distance lowers its influence
            # smoothly instead of cutting history off after two recent turns.
            # Generic workflow words are excluded above, and a single shared
            # short token is not enough to contaminate an unrelated task.
            recency_weight = max(0.05, 1.0 / (1.0 + 0.12 * recency))
            distinctive_single = any(len(term) >= 5 for term in shared_terms)
            if overlap >= 2 or distinctive_single:
                normalized = overlap / max(1.0, math.sqrt(len(current_terms) * len(history_terms)))
                candidates.append(
                    (normalized * recency_weight, overlap, recency_weight, task)
                )
            if len(valid_tasks) >= 50:
                break
        # Pronouns and elliptical instructions such as "retry the other two"
        # refer to the immediately preceding completed task on the same channel.
        # Lexical overlap must not pull in a much older task merely because it
        # repeats generic words such as "generate".
        if (direct_follow_up or confirmation_follow_up) and valid_tasks:
            candidates = [(10_000.0, 10_000, 1.0, valid_tasks[0])]
        elif continuity and not candidates:
            candidates.extend(
                (1.0 / (1.0 + 0.12 * recency), 0, 1.0 / (1.0 + 0.12 * recency), task)
                for recency, task in enumerate(valid_tasks)
            )
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        selected = candidates[:max(1, min(limit, 50))]
        # Present the chosen references in chronological influence order. The
        # relevance score decides membership; recency decay decides reading
        # order so the model never sees an older turn as more authoritative
        # merely because it contains one additional matching token.
        selected.sort(key=lambda item: item[2], reverse=True)
        blocks: list[str] = []
        task_ids: list[str] = []
        for index, (_score, _overlap, reference_weight, task) in enumerate(selected, 1):
            goal = self._redact_context(task.prompt).strip()
            outcome = self._redact_context(task.result or "").strip()
            if not goal or not outcome:
                continue
            original = f"用户目标：{goal}\n最终结果：{outcome}"
            # Short turns are already efficient.  Longer turns retain about a
            # fifth of their text, preserving both the beginning (goal and key
            # constraints) and the end (final outcome and unresolved work).
            if len(original) > 1200:
                original = self._compress_context_turn(original)
            block = (
                f"[历史对话 {index}｜参考权重 {reference_weight:.3f}]\n"
                f"{original}"
            )
            if blocks and len("\n\n".join([*blocks, block])) > max_chars:
                break
            blocks.append(block)
            task_ids.append(task.id)
        return "\n\n".join(blocks)[:max_chars], task_ids

    def delete(self, task_id: str) -> bool:
        """Permanently remove one task from the visible task history."""
        with self._lock:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                deleted = cursor.rowcount > 0
            # Publish the fence only after COMMIT succeeds, while still owning
            # the lock. A rejected DELETE/COMMIT must not poison future saves;
            # a late worker must not insert between COMMIT and publication.
            self._deleted_ids.add(task_id)
            return deleted

    def rename(self, task_id: str, title: str) -> AgentTask | None:
        """Persist a user-defined conversation title atomically."""
        with self._lock:
            if task_id in self._deleted_ids:
                return None
            task = self.get(task_id)
            if not task:
                return None
            task.title = str(title or "").strip()[:60]
            if not task.title:
                return None
            task.title_source = "user"
            task.updated_at = utc_now()
            self.save(task)
            return task

    def set_pinned(self, task_id: str, pinned: bool) -> bool:
        """Persist UI intent independently of concurrent agent payload writes."""
        with self._lock, self._connect() as connection:
            if not connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                return False
            if pinned:
                connection.execute("INSERT OR IGNORE INTO task_pins(task_id) VALUES (?)", (task_id,))
            else:
                connection.execute("DELETE FROM task_pins WHERE task_id = ?", (task_id,))
        return True

    def list_page(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str = "",
        status: str = "",
    ) -> tuple[list[AgentTask], int]:
        """Return a filtered page from the complete durable task history."""
        self._ensure_persistence_sanitized()
        normalized_query = query.strip()
        normalized_status = status.strip()
        escaped_query = ""
        if normalized_query:
            escaped_query = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
        query_pattern = f"%{escaped_query}%" if escaped_query else ""
        parameters: list[object] = [
            normalized_status,
            normalized_status,
            normalized_query,
            query_pattern,
        ]
        page_limit = min(max(int(limit), 1), 100)
        page_offset = max(int(offset), 0)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks"
                    " WHERE (? = '' OR status = ?)"
                    " AND (? = '' OR payload LIKE ? ESCAPE '\\')",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT payload, EXISTS(SELECT 1 FROM task_pins WHERE task_id = tasks.id) AS pinned FROM tasks"
                " WHERE (? = '' OR status = ?)"
                " AND (? = '' OR payload LIKE ? ESCAPE '\\')"
                " ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*parameters, page_limit, page_offset],
            ).fetchall()
        tasks: list[AgentTask] = []
        for payload, pinned in rows:
            try:
                task = self._deserialize_task(payload)
                task.pinned = bool(pinned)
                tasks.append(task)
            except (ValueError, TypeError):
                continue
        return tasks, total
