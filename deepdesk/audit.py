from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from deepdesk.models import utc_now
from deepdesk.secret_redaction import (
    is_secret_field,
    persistence_secret_needles,
    redact_text,
)

_MAX_AUDIT_STRING = 8_000
_MAX_AUDIT_ITEMS = 100
_MAX_AUDIT_DEPTH = 8
_SECRET_FIELD = re.compile(
    r"(?:^|_)(?:api_?key|secret(?:_key)?|token|password|passphrase|authorization|cookie|session_?lease)(?:$|_)",
    re.IGNORECASE,
)
_SESSION_LEASE_FIELD = re.compile(r"^session[\s_-]*lease$", re.IGNORECASE)


def _safe_audit_text(
    value: str,
    known_secrets: Iterable[Any] | None = None,
) -> str:
    """Keep audit JSON readable without persisting binary or unbounded payloads."""
    if not value:
        return value
    sample = value[:4096]
    control_count = sum(
        1 for character in sample if ord(character) < 32 and character not in "\n\r\t"
    )
    if "\x00" in sample or control_count > max(4, len(sample) // 50):
        return f"[binary content omitted: {len(value)} characters]"
    replacement_count = value.count("\ufffd")
    if replacement_count > max(2, len(value) // 100):
        return (
            f"[text with invalid encoding omitted: {len(value)} characters, "
            f"{replacement_count} invalid sequences]"
        )
    if replacement_count:
        value = value.replace("\ufffd", "[invalid byte]")
    # JSON cannot safely round-trip lone UTF-16 surrogates. Replacement here is
    # deliberate and affects only the diagnostic copy, never the task result.
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    value = redact_text(value, known_secrets)
    # Audit data is a diagnostic copy, so credential-shaped assignments and
    # authorization headers must never be persisted even when a secret has not
    # yet been saved in Settings and therefore is absent from runtime redaction.
    value = re.sub(
        r'''(?ix)(["']?(?:api_?key|secret(?:_key)?|token|password|passphrase|authorization|cookie)["']?\s*[:=]\s*["'])([^"']+)(["'])''',
        r"\1[REDACTED]\3",
        value,
    )
    value = re.sub(
        r'''(?ix)(["']?session[\s_-]*lease["']?\s*[:=]\s*["'])([^"']+)(["'])''',
        r"\1[REDACTED]\3",
        value,
    )
    value = re.sub(
        r"(?i)(\bAuthorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{8,}",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)([?&](?:api_?key|token|secret|password)=)[^&\s]+",
        r"\1[REDACTED]",
        value,
    )
    if len(value) > _MAX_AUDIT_STRING:
        omitted = len(value) - _MAX_AUDIT_STRING
        return f"{value[:_MAX_AUDIT_STRING]}\n[audit text truncated: {omitted} characters omitted]"
    return value


def sanitize_audit_value(
    value: Any,
    *,
    depth: int = 0,
    known_secrets: Iterable[Any] | None = None,
) -> Any:
    """Return a bounded, UTF-8-safe representation suitable for JSONL logs."""
    if depth >= _MAX_AUDIT_DEPTH:
        return "[audit nesting limit reached]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _safe_audit_text(value, known_secrets)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[binary content omitted: {len(value)} bytes]"
    if isinstance(value, dict):
        items = list(value.items())
        result = {}
        for key, item in items[:_MAX_AUDIT_ITEMS]:
            safe_key = _safe_audit_text(str(key), known_secrets)[:200]
            result[safe_key] = (
                "[REDACTED]"
                if (
                    is_secret_field(key)
                    or _SECRET_FIELD.search(str(key))
                    or _SESSION_LEASE_FIELD.fullmatch(str(key).strip())
                )
                and not str(key).casefold().endswith("_configured")
                and item not in (None, "")
                else sanitize_audit_value(
                    item,
                    depth=depth + 1,
                    known_secrets=known_secrets,
                )
            )
        if len(items) > _MAX_AUDIT_ITEMS:
            result["_audit_omitted_items"] = len(items) - _MAX_AUDIT_ITEMS
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [
            sanitize_audit_value(
                item,
                depth=depth + 1,
                known_secrets=known_secrets,
            )
            for item in items[:_MAX_AUDIT_ITEMS]
        ]
        if len(items) > _MAX_AUDIT_ITEMS:
            result.append(f"[audit items omitted: {len(items) - _MAX_AUDIT_ITEMS}]")
        return result
    return _safe_audit_text(str(value), known_secrets)


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._secret_values: Callable[[], Iterable[Any]] | None = None
        self._sanitize_legacy_log_once()

    def set_secret_values(
        self,
        secret_values: Callable[[], Iterable[Any]] | None,
    ) -> None:
        """Attach a dynamic exact-value source without retaining credentials."""

        self._secret_values = secret_values

    def resanitize_existing_secrets(self) -> bool:
        """Best-effort offline cleanup for an audit file from an older build.

        The active desktop process may be appending to this path and Windows
        correctly refuses an atomic replacement in that case.  Normal writes
        are protected immediately by ``set_secret_values``; callers should run
        this optional migration only while no other Elren process owns the log.
        """

        secrets = self._current_secret_values()
        return bool(secrets) and self._resanitize_existing_log(secrets)

    def _current_secret_values(self) -> list[Any]:
        if self._secret_values is None:
            return []
        try:
            return list(self._secret_values())
        except Exception:
            return []

    def _resanitize_existing_log(self, known_secrets: Iterable[Any]) -> bool:
        """Remove newly known exact values from legacy audit records."""

        if not self.path.is_file() or self.path.stat().st_size == 0:
            return True
        needles = tuple(
            needle.encode("utf-8", errors="surrogatepass")
            for needle in persistence_secret_needles(known_secrets)
        )
        if not needles:
            return True
        # Exact migration used to JSON-decode and recursively redact the whole
        # audit log at every startup. A streaming byte preflight is sufficient
        # to prove that no configured value (raw or JSON-escaped) is present;
        # only a positive match pays the full rewrite cost. No secret-derived
        # fingerprint or plaintext marker is persisted.
        try:
            overlap = max(len(needle) for needle in needles) - 1
            tail = b""
            found = False
            with self.path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    sample = tail + chunk
                    if any(needle in sample for needle in needles):
                        found = True
                        break
                    tail = sample[-overlap:] if overlap > 0 else b""
            if not found:
                return True
        except OSError:
            return False
        temporary = self.path.with_suffix(self.path.suffix + ".resanitizing")
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                "w", encoding="utf-8", newline="\n"
            ) as target:
                for line_number, line in enumerate(source, 1):
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        record = {
                            "timestamp": utc_now(),
                            "event": "legacy_log_line_omitted",
                            "task_id": "",
                            "data": {"line": line_number, "reason": "invalid legacy JSON"},
                        }
                    target.write(
                        json.dumps(
                            sanitize_audit_value(
                                record,
                                known_secrets=known_secrets,
                            ),
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
            temporary.replace(self.path)
            return True
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _sanitize_legacy_log_once(self) -> None:
        """Rewrite pre-v1 logs that may contain binary document payloads."""
        marker = self.path.with_suffix(self.path.suffix + ".sanitized-v2")
        if marker.exists() or not self.path.is_file() or self.path.stat().st_size == 0:
            return
        temporary = self.path.with_suffix(self.path.suffix + ".sanitizing")
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                "w", encoding="utf-8", newline="\n"
            ) as target:
                for line_number, line in enumerate(source, 1):
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        record = {
                            "timestamp": utc_now(),
                            "event": "legacy_log_line_omitted",
                            "task_id": "",
                            "data": {"line": line_number, "reason": "invalid legacy JSON"},
                        }
                    target.write(
                        json.dumps(
                            sanitize_audit_value(record),
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
            temporary.replace(self.path)
            marker.write_text("sanitized audit format v2\n", encoding="utf-8")
        except OSError:
            temporary.unlink(missing_ok=True)

    async def write(self, event: str, task_id: str, data: dict[str, Any]) -> None:
        secrets = self._current_secret_values()
        record = {
            "timestamp": utc_now(),
            "event": _safe_audit_text(str(event), secrets),
            "task_id": _safe_audit_text(str(task_id), secrets),
            "data": sanitize_audit_value(data, known_secrets=secrets),
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
