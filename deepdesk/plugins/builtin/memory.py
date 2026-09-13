from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from deepdesk.models import Risk, utc_now
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.secret_redaction import redact_text

SECRET_PATTERN = re.compile(
    r"(?:sk-[a-zA-Z0-9_-]{16,}|bearer\s+[a-zA-Z0-9._-]{16,})", re.IGNORECASE
)

_SECRET_REDACTION_MARKER = "memory-secret-redaction-physical-v1"
_SECRET_REDACTION_PENDING = "pending"
_SECRET_REDACTION_COMPLETED = "completed"


class MemoryTool(ToolPlugin):
    name = "memory"
    description = (
        "Durable local memory backed by SQLite. Search/list/get notes, or store/delete a note. "
        "Never store passwords, API keys, tokens, or other credentials."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "list", "get", "put", "delete"],
            },
            "query": {"type": "string"},
            "id": {"type": "string"},
            "title": {"type": "string", "maxLength": 200},
            "content": {"type": "string", "maxLength": 20000},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        database: Path,
        secret_values: Callable[[], list[str]] | None = None,
    ) -> None:
        self.database = database
        self.secret_values = secret_values
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS security_migrations ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        self.resanitize_existing_secrets()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def _current_secret_values(self) -> list[str]:
        if self.secret_values is None:
            return []
        try:
            return [str(value) for value in self.secret_values() if value]
        except Exception as exc:
            raise RuntimeError(
                "Configured credential redaction is unavailable for durable memory"
            ) from exc

    def _safe_text(self, value: Any) -> str:
        return redact_text(str(value or ""), self._current_secret_values())

    @staticmethod
    def _security_marker(connection: sqlite3.Connection, name: str) -> str:
        row = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?", (name,)
        ).fetchone()
        return str(row[0]) if row else ""

    @staticmethod
    def _set_security_marker(
        connection: sqlite3.Connection,
        name: str,
        value: str,
    ) -> None:
        connection.execute(
            "INSERT INTO security_migrations(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value),
        )

    @staticmethod
    def _compact_redacted_pages(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def resanitize_existing_secrets(self) -> int:
        """Rewrite legacy notes and remove plaintext remnants from SQLite/WAL."""

        changed = 0
        # Fetch the current values for this scan only. The durable marker below
        # deliberately records only a fixed migration version/state: it must
        # never contain a configured secret, fingerprint, or derived verifier.
        secret_values = self._current_secret_values()
        with self._lock, self._connect() as connection:
            pending_scrub = (
                self._security_marker(connection, _SECRET_REDACTION_MARKER)
                != _SECRET_REDACTION_COMPLETED
            )
            rows = connection.execute(
                "SELECT id, title, content, tags FROM memories"
            ).fetchall()
            for row in rows:
                title = redact_text(str(row["title"] or ""), secret_values)
                content = redact_text(str(row["content"] or ""), secret_values)
                tags = redact_text(str(row["tags"] or ""), secret_values)
                if (title, content, tags) == (
                    row["title"],
                    row["content"],
                    row["tags"],
                ):
                    continue
                connection.execute(
                    "UPDATE memories SET title=?, content=?, tags=? WHERE id=?",
                    (title, content, tags, row["id"]),
                )
                changed += 1
            if changed or pending_scrub:
                # Persist the logical rewrite and pending state together before
                # VACUUM. If compaction is interrupted, the next construction
                # sees ``pending`` and retries the physical scrub. A missing
                # marker is also pending so the first upgraded startup compacts
                # legacy free pages even when every live row is already safe.
                self._set_security_marker(
                    connection,
                    _SECRET_REDACTION_MARKER,
                    _SECRET_REDACTION_PENDING,
                )
                connection.commit()
                self._compact_redacted_pages(connection)
                self._set_security_marker(
                    connection,
                    _SECRET_REDACTION_MARKER,
                    _SECRET_REDACTION_COMPLETED,
                )
                connection.commit()
        return changed

    def risk(self, arguments: dict[str, Any]) -> Risk:
        action = arguments.get("action")
        if action == "delete":
            return Risk.HIGH
        if action == "put":
            return Risk.MEDIUM
        return Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "put":
            return f"写入长期记忆：{arguments.get('title', '')[:80]}"
        if action == "delete":
            return f"删除长期记忆：{arguments.get('id', '')}"
        return f"读取长期记忆：{action}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        limit = min(max(int(arguments.get("limit", 20)), 1), 100)
        if action == "put":
            return self._put(arguments)
        if action == "delete":
            memory_id = self._required(arguments, "id")
            with self._lock, self._connect() as connection:
                cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return {"deleted": cursor.rowcount == 1, "id": memory_id}
        if action == "get":
            memory_id = self._required(arguments, "id")
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
            if not row:
                raise KeyError(f"Memory not found: {memory_id}")
            return self._row(row)
        if action == "search":
            query = self._required(arguments, "query")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM memories
                    WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\'
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (pattern, pattern, pattern, limit),
                ).fetchall()
            return {"items": [self._row(row) for row in rows], "query": query}
        if action == "list":
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return {"items": [self._row(row) for row in rows]}
        raise ValueError(f"Unsupported memory action: {action}")

    def _put(self, arguments: dict[str, Any]) -> dict[str, Any]:
        title = self._required(arguments, "title").strip()
        content = self._required(arguments, "content").strip()
        tags = sorted({str(tag).strip() for tag in arguments.get("tags", []) if str(tag).strip()})
        memory_id = str(arguments.get("id") or uuid4().hex)
        safe_title = self._safe_text(title)
        safe_content = self._safe_text(content)
        safe_tags = [self._safe_text(tag) for tag in tags]
        safe_id = self._safe_text(memory_id)
        if (
            SECRET_PATTERN.search(title)
            or SECRET_PATTERN.search(content)
            or safe_title != title
            or safe_content != content
            or safe_tags != tags
            or safe_id != memory_id
        ):
            raise PermissionError("Refusing to store a value that looks like an API key or token")
        now = utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO memories(id, title, content, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    tags=excluded.tags,
                    updated_at=excluded.updated_at
                """,
                (memory_id, title, content, "\n".join(tags), created_at, now),
            )
        return {"id": memory_id, "title": title, "tags": tags, "updated_at": now}

    @staticmethod
    def _required(arguments: dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
        return value

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "tags": row["tags"].splitlines() if row["tags"] else [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
