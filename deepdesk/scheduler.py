from __future__ import annotations

import asyncio
import inspect
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

from deepdesk.models import AgentProfile, ApprovalPolicy
from deepdesk.plugins.base import run_owned_thread
from deepdesk.secret_redaction import redact_text

logger = logging.getLogger(__name__)

_DISPATCH_LEASE_SECONDS = 300
_DISPATCH_UNCERTAIN = (
    "Dispatch outcome uncertain; automatic execution is paused. Check task history "
    "before resolving with skip or retry (retry may repeat an existing task)."
)
_SECRET_REDACTION_MARKER = "schedule-secret-redaction"
_SECRET_REDACTION_PENDING = "physical-scrub-v1:pending"
_SECRET_REDACTION_COMPLETED = "physical-scrub-v1:completed"


class DispatchNotStartedError(RuntimeError):
    """Dispatcher guarantees that no task was created and no work was started."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_timezone(value: str | None) -> tzinfo:
    if (value or "UTC") in {"UTC", "Etc/UTC", "GMT"}:
        return UTC
    try:
        return ZoneInfo(value or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA time zone") from exc


def _interval_seconds(expression: str) -> float:
    try:
        seconds = float(expression)
    except (ValueError, TypeError) as exc:
        raise ValueError("interval must be between 1 second and 365 days") from exc
    if not math.isfinite(seconds) or not 1 <= seconds <= 31_536_000:
        raise ValueError("interval must be between 1 second and 365 days")
    return seconds


class ScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=20_000)
    kind: str = "cron"
    # ``at`` and ``daily`` schedules already carry their first execution time
    # in ``start_at``.  Accept clients that omit the legacy duplicate
    # ``expression`` value and normalize it below.  Interval/cron schedules
    # still require a real expression.
    expression: str = Field(default="", max_length=120)
    policy: ApprovalPolicy = ApprovalPolicy.AUTONOMOUS
    agent_profile: AgentProfile = AgentProfile.GENERAL
    enabled: bool = True
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"daily", "interval", "at", "cron"}:
            raise ValueError("kind must be daily, interval, at, or legacy cron")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        _parse_timezone(value)
        return value

    @model_validator(mode="after")
    def valid_window(self):
        self.expression = self.expression.strip()
        if not self.expression and self.kind in {"at", "daily"} and self.start_at:
            self.expression = self.start_at
        if not self.expression:
            raise ValueError("expression is required for interval and cron tasks")
        if self.kind == "interval":
            # Disabled schedules and future anchors still need a valid period;
            # otherwise their first successful dispatch can fail during ack.
            _interval_seconds(self.expression)
        # Older one-time schedules stored the timestamp in expression. Keep
        # those databases/API clients loadable while the new UI uses start_at.
        if self.kind == "at" and not self.start_at:
            self.start_at = self.expression
        if self.kind == "daily" and not self.start_at:
            raise ValueError("start_at is required for daily tasks")
        start = _parse_time(self.start_at) if self.start_at else None
        end = _parse_time(self.end_at) if self.end_at else None
        if start and end and end <= start:
            raise ValueError("end_at must be later than start_at")
        return self


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    kind: str | None = None
    expression: str | None = Field(default=None, min_length=1, max_length=120)
    policy: ApprovalPolicy | None = None
    agent_profile: AgentProfile | None = None
    enabled: bool | None = None
    start_at: str | None = Field(default=None, max_length=80)
    end_at: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    resolve_dispatch: Literal["skip", "retry"] | None = None


class CronExpression:
    """Small, deterministic five-field UTC cron parser.

    Supports `*`, ranges, comma lists and steps such as `*/5` or `1-20/2`.
    """

    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("cron expression must contain five fields")
        self.day_any = fields[2] == "*"
        self.weekday_any = fields[4] == "*"
        self.values = [
            self._field(text, *limits)
            for text, limits in zip(fields, self.RANGES, strict=True)
        ]

    @staticmethod
    def _field(text: str, minimum: int, maximum: int) -> set[int]:
        values: set[int] = set()
        for item in text.split(","):
            base, slash, step_text = item.partition("/")
            step = int(step_text) if slash else 1
            if step < 1:
                raise ValueError("cron step must be positive")
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                left, right = base.split("-", 1)
                start, end = int(left), int(right)
            else:
                start = end = int(base)
            if start < minimum or end > maximum or start > end:
                raise ValueError(f"cron value outside {minimum}-{maximum}")
            values.update(range(start, end + 1, step))
        if not values:
            raise ValueError("empty cron field")
        return values

    def next_after(self, after: datetime) -> datetime:
        candidate = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        deadline = candidate + timedelta(days=366 * 5)
        minutes, hours, days, months, weekdays = self.values
        while candidate <= deadline:
            cron_weekday = (candidate.weekday() + 1) % 7
            day_match = candidate.day in days
            weekday_match = cron_weekday in weekdays
            if self.day_any:
                calendar_match = weekday_match
            elif self.weekday_any:
                calendar_match = day_match
            else:
                calendar_match = day_match or weekday_match
            if (
                candidate.minute in minutes
                and candidate.hour in hours
                and candidate.month in months
                and calendar_match
            ):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("cron schedule has no occurrence within five years")


class Scheduler:
    def __init__(
        self,
        database: Path,
        dispatch: Callable[[str, ApprovalPolicy, AgentProfile], str | Awaitable[str]],
        secret_values: Callable[[], list[str]] | None = None,
    ) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch = dispatch
        self.secret_values = secret_values
        self._lock = threading.RLock()
        self._dispatch_context = threading.local()
        self._runner: asyncio.Task[None] | None = None
        self._initialize()
        self.resanitize_existing_secrets()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=10000")
            # SQLite can return BUSY immediately while two brand-new
            # processes both try to make the database persistent-WAL. Retry
            # this short initialization race; normal transactional contention
            # remains governed by busy_timeout.
            for attempt in range(40):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        getattr(exc, "sqlite_errorcode", None)
                        not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        or attempt == 39
                    ):
                        raise
                    time.sleep(0.025)
            connection.execute("PRAGMA secure_delete=ON")
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            # Serialize schema inspection and additive migrations across
            # processes. Without the write lock, two first-start instances can
            # both observe a missing lease column and race the same ALTER.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    agent_profile TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_run TEXT,
                    last_run TEXT,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    last_task_id TEXT,
                    last_error TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    claim_token TEXT,
                    claim_until TEXT,
                    dispatch_started_at TEXT,
                    dispatch_preserve_schedule INTEGER NOT NULL DEFAULT 0,
                    dispatch_enabled_before INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS security_migrations ("
                "name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(schedules)").fetchall()
            }
            if "start_at" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN start_at TEXT")
            if "end_at" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN end_at TEXT")
            if "timezone" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'")
            if "claim_token" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN claim_token TEXT")
            if "claim_until" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN claim_until TEXT")
            if "dispatch_started_at" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN dispatch_started_at TEXT")
                # Legacy leases cannot tell whether work had already started.
                # Do not silently reclaim such a lease during this migration.
                connection.execute(
                    "UPDATE schedules SET dispatch_started_at=COALESCE(claim_until, updated_at), "
                    "enabled=0, last_error=? WHERE COALESCE(claim_token, '')<>''",
                    (_DISPATCH_UNCERTAIN,),
                )
            if "dispatch_preserve_schedule" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN dispatch_preserve_schedule INTEGER NOT NULL DEFAULT 0")
            if "dispatch_enabled_before" not in columns:
                connection.execute("ALTER TABLE schedules ADD COLUMN dispatch_enabled_before INTEGER")

    def _current_secret_values(self) -> list[str]:
        if self.secret_values is None:
            return []
        try:
            return [str(value) for value in self.secret_values() if value]
        except Exception as exc:
            raise RuntimeError(
                "Configured credential redaction is unavailable for schedules"
            ) from exc

    def _safe_text(self, value: Any) -> str:
        return redact_text(str(value or ""), self._current_secret_values())

    def _reject_secret_request(self, request: ScheduleRequest) -> None:
        values = (
            request.name,
            request.prompt,
            request.expression,
            request.start_at or "",
            request.end_at or "",
        )
        if any(self._safe_text(value) != value for value in values):
            raise ValueError("Schedule fields must not contain API keys, tokens, or passwords")

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
        """Rewrite legacy schedules and remove plaintext remnants from SQLite/WAL."""

        text_fields = (
            "name",
            "prompt",
            "expression",
            "last_error",
            "start_at",
            "end_at",
        )
        changed = 0
        with self._lock, self._connection() as connection:
            pending_scrub = (
                self._security_marker(connection, _SECRET_REDACTION_MARKER)
                == _SECRET_REDACTION_PENDING
            )
            rows = connection.execute(
                "SELECT id, name, prompt, expression, last_error, start_at, end_at "
                "FROM schedules"
            ).fetchall()
            for row in rows:
                safe = {field: self._safe_text(row[field]) for field in text_fields}
                if all(safe[field] == (row[field] or "") for field in text_fields):
                    continue
                connection.execute(
                    "UPDATE schedules SET name=?, prompt=?, expression=?, last_error=?, "
                    "start_at=?, end_at=? WHERE id=?",
                    (
                        safe["name"],
                        safe["prompt"],
                        safe["expression"],
                        safe["last_error"] or None,
                        safe["start_at"] or None,
                        safe["end_at"] or None,
                        row["id"],
                    ),
                )
                changed += 1
            if changed:
                self._set_security_marker(
                    connection,
                    _SECRET_REDACTION_MARKER,
                    _SECRET_REDACTION_PENDING,
                )
            if changed or pending_scrub:
                # Commit the logical rewrite and its pending marker together.
                # If the process exits before VACUUM, the next startup still
                # performs the physical scrub even though no row changes then.
                connection.commit()
                self._compact_redacted_pages(connection)
                self._set_security_marker(
                    connection,
                    _SECRET_REDACTION_MARKER,
                    _SECRET_REDACTION_COMPLETED,
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return changed

    @staticmethod
    def next_run(
        kind: str,
        expression: str,
        after: datetime,
        *,
        start_at: str | None = None,
        end_at: str | None = None,
        timezone: str | None = None,
    ) -> datetime:
        after = after.astimezone(UTC)
        start = _parse_time(start_at) if start_at else None
        if kind == "interval":
            seconds = _interval_seconds(expression)
            if start is None:
                due = after + timedelta(seconds=seconds)
            elif start > after:
                due = start
            else:
                steps = int((after - start).total_seconds() // seconds) + 1
                due = start + timedelta(seconds=steps * seconds)
        elif kind == "daily":
            if start is None:
                raise ValueError("daily schedule requires start_at")
            zone = _parse_timezone(timezone)
            start_local = start.astimezone(zone)
            after_local = after.astimezone(zone)
            candidate_date = max(start_local.date(), after_local.date())
            candidate = datetime.combine(
                candidate_date,
                start_local.time().replace(tzinfo=None),
                tzinfo=zone,
            )
            due = candidate.astimezone(UTC)
            # Comparing two datetimes with the same ZoneInfo compares wall
            # time, ignoring fold. During a fall-back this can put a completed
            # occurrence back in the past and dispatch it on every tick.
            # Retain the anchor's fold choice, but enforce ordering in UTC.
            while due <= after:
                candidate_date += timedelta(days=1)
                candidate = datetime.combine(
                    candidate_date,
                    start_local.time().replace(tzinfo=None),
                    tzinfo=zone,
                )
                due = candidate.astimezone(UTC)
        elif kind == "cron":
            due = CronExpression(expression).next_after(after)
        elif kind == "at":
            due = _parse_time(start_at or expression)
            if due <= after:
                raise ValueError("one-time start must be in the future")
        else:
            raise ValueError("unsupported schedule kind")
        if end_at and due > _parse_time(end_at):
            raise ValueError("the schedule has no run before its end time")
        return due

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["dispatch_state"] = (
            "uncertain" if str(value.get("last_error") or "").startswith(_DISPATCH_UNCERTAIN)
            else "dispatching" if value.get("dispatch_started_at") else "idle"
        )
        # Claims are internal coordination state. They are deliberately kept
        # out of API responses so an implementation detail cannot become a
        # client-visible capability or compatibility commitment.
        value.pop("claim_token", None)
        value.pop("claim_until", None)
        value.pop("dispatch_started_at", None)
        value.pop("dispatch_preserve_schedule", None)
        value.pop("dispatch_enabled_before", None)
        return value

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules ORDER BY created_at DESC"
            ).fetchall()
        return [self._row(row) for row in rows]

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._row(row) if row else None

    def create(self, request: ScheduleRequest, now: datetime | None = None) -> dict[str, Any]:
        self._reject_secret_request(request)
        now = (now or _utc_now()).astimezone(UTC)
        due = self.next_run(
            request.kind,
            request.expression,
            now,
            start_at=request.start_at,
            end_at=request.end_at,
            timezone=request.timezone,
        ) if request.enabled else None
        schedule_id = uuid4().hex
        stamp = _iso(now)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO schedules(
                    id, name, prompt, kind, expression, policy, agent_profile, enabled,
                    next_run, start_at, end_at, timezone, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule_id,
                    request.name,
                    request.prompt,
                    request.kind,
                    request.expression,
                    request.policy.value,
                    request.agent_profile.value,
                    int(request.enabled),
                    _iso(due) if due else None,
                    _iso(_parse_time(request.start_at)) if request.start_at else None,
                    _iso(_parse_time(request.end_at)) if request.end_at else None,
                    request.timezone,
                    stamp,
                    stamp,
                ),
            )
        return self.get(schedule_id) or {}

    def update(
        self, schedule_id: str, patch: SchedulePatch, now: datetime | None = None
    ) -> dict[str, Any] | None:
        now = (now or _utc_now()).astimezone(UTC)
        manual_retry_token: str | None = None
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                return None
            current = dict(row)
            data = patch.model_dump(exclude_unset=True)
            resolution = data.pop("resolve_dispatch", None)
            # Null is a real clearing operation only for optional bounds.
            for key, value in data.items():
                if value is None and key not in {"start_at", "end_at"}:
                    raise ValueError(f"{key} cannot be null")
            merged = {**current, **data}
            validated = ScheduleRequest.model_validate(merged)
            self._reject_secret_request(validated)
            merged.update(validated.model_dump(mode="json"))
            for key in ("start_at", "end_at"):
                merged[key] = _iso(_parse_time(merged[key])) if merged.get(key) else None
            timing = {"kind", "expression", "start_at", "end_at", "timezone", "enabled"}
            changed_timing = {key for key in timing if merged[key] != current[key]}
            changed = bool(changed_timing)
            started = bool(current["dispatch_started_at"])
            live = bool(current["claim_token"]) and bool(current["claim_until"]) and _parse_time(current["claim_until"]) > now
            if resolution:
                if not started:
                    raise ValueError("No uncertain dispatch to resolve")
                if live:
                    raise ValueError("Dispatch is still in progress; check again after its lease expires")
                if data:
                    raise ValueError("Resolve dispatch separately from other schedule edits")
                if resolution == "retry":
                    if current.get("end_at") and now > _parse_time(current["end_at"]):
                        raise ValueError("Cannot retry after the schedule end time")
                    if current["dispatch_preserve_schedule"]:
                        enabled = bool(current["dispatch_enabled_before"])
                        next_due = current["next_run"]
                        manual_retry_token = uuid4().hex
                    else:
                        enabled, next_due = True, _iso(now)
                else:
                    if current["dispatch_preserve_schedule"]:
                        enabled = bool(current["dispatch_enabled_before"])
                        next_due = current["next_run"]
                        if current.get("end_at") and now > _parse_time(current["end_at"]):
                            enabled, next_due = False, None
                    else:
                        enabled, next_due = self._following_run(current, now)
                connection.execute(
                    "UPDATE schedules SET enabled=?, next_run=?, last_error=NULL, "
                    "claim_token=NULL, claim_until=NULL, dispatch_started_at=NULL, "
                    "dispatch_preserve_schedule=0, dispatch_enabled_before=NULL, updated_at=? WHERE id=?",
                    (int(enabled), next_due, _iso(now), schedule_id),
                )
                if manual_retry_token:
                    connection.execute(
                        "UPDATE schedules SET claim_token=?, claim_until=? WHERE id=?",
                        (manual_retry_token, _iso(now + timedelta(seconds=_DISPATCH_LEASE_SECONDS)), schedule_id),
                    )
            else:
                if started and changed:
                    raise ValueError("Dispatch is in progress or uncertain; resolve it before changing its schedule")
                if live and changed:
                    raise ValueError("Schedule is claimed for dispatch; try again after completion")
                enabled = bool(merged["enabled"])
                next_due = current["next_run"]
                # Renaming, prompt/policy edits, empty PATCH, and unchanged full
                # forms must not shift a due occurrence (including past one-shots).
                if changed:
                    if not enabled:
                        next_due = None
                    elif changed_timing <= {"end_at"} and next_due:
                        if merged["end_at"] and _parse_time(next_due) > _parse_time(merged["end_at"]):
                            enabled, next_due = False, None
                    else:
                        next_due = _iso(self.next_run(
                            merged["kind"], merged["expression"], now,
                            start_at=merged.get("start_at"), end_at=merged.get("end_at"),
                            timezone=merged.get("timezone"),
                        ))
                connection.execute(
                    "UPDATE schedules SET name=?, prompt=?, kind=?, expression=?, policy=?, "
                    "agent_profile=?, enabled=?, next_run=?, start_at=?, end_at=?, timezone=?, updated_at=? WHERE id=?",
                    (merged["name"], merged["prompt"], merged["kind"], merged["expression"],
                     merged["policy"], merged["agent_profile"], int(enabled), next_due,
                     merged["start_at"], merged["end_at"], merged["timezone"], _iso(now), schedule_id),
                )
        if manual_retry_token:
            refreshed = self.get(schedule_id)
            if refreshed:
                return self._dispatch(refreshed, now, preserve_schedule=True, claim_token=manual_retry_token)["schedule"]
        return self.get(schedule_id)

    def delete(self, schedule_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0

    def run_now(self, schedule_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = (now or _utc_now()).astimezone(UTC)
        token = uuid4().hex
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                return None
            if row["dispatch_started_at"]:
                raise ValueError("Dispatch is in progress or uncertain; resolve it before running again")
            if row["claim_token"] and row["claim_until"] and _parse_time(row["claim_until"]) > now:
                raise ValueError("Schedule is already claimed for dispatch")
            connection.execute(
                "UPDATE schedules SET claim_token=?, claim_until=? WHERE id=?",
                (token, _iso(now + timedelta(seconds=_DISPATCH_LEASE_SECONDS)), schedule_id),
            )
        return self._dispatch(self._row(row), now, preserve_schedule=True, claim_token=token)

    def run_due(self, now: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
        now = (now or _utc_now()).astimezone(UTC)
        results: list[dict[str, Any]] = []
        for _ in range(min(max(limit, 1), 1000)):
            if self._async_call_cancelled():
                break
            claimed = self._claim_next_due(now)
            if claimed is None:
                break
            schedule, claim_token = claimed
            results.append(
                self._dispatch(
                    schedule,
                    now,
                    preserve_schedule=False,
                    claim_token=claim_token,
                )
            )
        return results

    def _claim_next_due(
        self, now: datetime
    ) -> tuple[dict[str, Any], str] | None:
        """Lease the next due row with a cross-process SQLite write lock.

        Only an unstarted claim may be reclaimed. Once dispatch has started,
        losing the acknowledgement must never be interpreted as no task created.
        """

        cutoff = _iso(now)
        claim_token = uuid4().hex
        claim_until = _iso(now + timedelta(seconds=_DISPATCH_LEASE_SECONDS))
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE schedules SET enabled=0, last_error=?, updated_at=? "
                "WHERE dispatch_started_at IS NOT NULL AND (claim_until IS NULL OR claim_until<=?)",
                (_DISPATCH_UNCERTAIN, cutoff, cutoff),
            )
            # end_at limits actual automatic startup, not only next_run math.
            # Keep started/uncertain fences intact for explicit reconciliation.
            connection.execute(
                "UPDATE schedules SET enabled=0, next_run=NULL, claim_token=NULL, claim_until=NULL, "
                "last_error='Schedule end time passed; missed run was not dispatched', updated_at=? "
                "WHERE enabled=1 AND end_at IS NOT NULL AND end_at<? AND dispatch_started_at IS NULL",
                (cutoff, cutoff),
            )
            row = connection.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1
                    AND dispatch_started_at IS NULL
                    AND next_run IS NOT NULL
                    AND next_run <= ?
                    AND (
                        COALESCE(claim_token, '') = ''
                        OR claim_until IS NULL
                        OR claim_until <= ?
                    )
                ORDER BY next_run, id
                LIMIT 1
                """,
                (cutoff, cutoff),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE schedules
                SET claim_token = ?, claim_until = ?
                WHERE id = ?
                    AND enabled = 1
                    AND dispatch_started_at IS NULL
                    AND next_run IS NOT NULL
                    AND next_run <= ?
                    AND (
                        COALESCE(claim_token, '') = ''
                        OR claim_until IS NULL
                        OR claim_until <= ?
                    )
                """,
                (claim_token, claim_until, row["id"], cutoff, cutoff),
            )
            if cursor.rowcount != 1:
                return None
        return self._row(row), claim_token

    def _following_run(self, schedule: dict[str, Any], now: datetime) -> tuple[bool, str | None]:
        if schedule["kind"] == "at":
            return False, None
        if schedule["kind"] == "interval" and schedule.get("next_run"):
            base = _parse_time(schedule["next_run"])
            seconds = _interval_seconds(schedule["expression"])
            steps = int(max((now - base).total_seconds(), 0) // seconds) + 1
            due = base + timedelta(seconds=steps * seconds)
        else:
            due = self.next_run(
                schedule["kind"], schedule["expression"], now,
                start_at=schedule.get("start_at"), timezone=schedule.get("timezone"),
            )
        if due <= now:
            raise ValueError("Next scheduled occurrence must be in the future")
        if schedule.get("end_at") and due > _parse_time(schedule["end_at"]):
            return False, None
        return True, _iso(due)

    def _dispatch(
        self,
        schedule: dict[str, Any],
        now: datetime,
        preserve_schedule: bool,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        if claim_token is None:
            raise ValueError("Dispatch requires a durable claim")
        if self._async_call_cancelled():
            # Stop arrived while acquiring the lease. No work has started;
            # release only our token so restart need not wait for lease expiry.
            with self._lock, self._connection() as connection:
                connection.execute(
                    "UPDATE schedules SET claim_token=NULL, claim_until=NULL "
                    "WHERE id=? AND claim_token=? AND dispatch_started_at IS NULL",
                    (schedule["id"], claim_token),
                )
            return {"schedule": self.get(schedule["id"]) or schedule, "task_id": None,
                    "error": "Schedule operation cancelled before dispatch"}
        # Commit a durable fence BEFORE invoking anything that can create work.
        # If this write/commit fails, no work is started. If acknowledgement
        # later fails, the fence remains and all instances fail closed.
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE schedules SET dispatch_started_at=?, dispatch_preserve_schedule=?, dispatch_enabled_before=enabled "
                "WHERE id=? AND claim_token=? "
                "AND dispatch_started_at IS NULL AND claim_until>?",
                (_iso(now), int(preserve_schedule), schedule["id"], claim_token, _iso(now)),
            )
            if cursor.rowcount != 1:
                return {"schedule": self.get(schedule["id"]) or schedule, "task_id": None,
                        "error": "Dispatch claim is no longer owned"}
        task_id = ""
        error = ""
        definitely_not_started = False
        try:
            task_id = self._call_dispatch(
                schedule["prompt"],
                ApprovalPolicy(schedule["policy"]),
                AgentProfile(schedule["agent_profile"]),
            )
            if not task_id:
                raise RuntimeError("Dispatcher returned no task acknowledgement")
        except DispatchNotStartedError as exc:
            definitely_not_started = True
            error = self._safe_text(f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            error = _DISPATCH_UNCERTAIN + " " + self._safe_text(f"{type(exc).__name__}: {exc}")
            with self._lock, self._connection() as connection:
                connection.execute(
                    "UPDATE schedules SET enabled=0, last_error=?, updated_at=?, claim_until=? "
                    "WHERE id=? AND claim_token=?",
                    (error, _iso(now), _iso(now), schedule["id"], claim_token),
                )
            return {"schedule": self.get(schedule["id"]) or schedule, "task_id": None, "error": error}

        enabled = bool(schedule["enabled"])
        next_due = schedule["next_run"]
        if not preserve_schedule:
            if schedule["kind"] == "at" and definitely_not_started:
                # A one-shot task must not disappear just because dispatch hit
                # a transient local failure (for example, the service was
                # shutting down while the due tick ran).  Keep it due for a
                # bounded retry while retaining last_error for visibility.
                retry_due = now + timedelta(seconds=60)
                if schedule.get("end_at") and retry_due > _parse_time(
                    schedule["end_at"]
                ):
                    enabled = False
                    next_due = None
                else:
                    enabled = True
                    next_due = _iso(retry_due)
            else:
                enabled, next_due = self._following_run(schedule, now)

        with self._lock, self._connection() as connection:
            query = """
                UPDATE schedules SET enabled=?, next_run=?, last_run=?, run_count=run_count+1,
                    last_task_id=?, last_error=?, updated_at=?
            """
            parameters: list[Any] = [
                int(enabled),
                next_due,
                _iso(now),
                task_id or None,
                error or None,
                _iso(now),
            ]
            query += ", claim_token=NULL, claim_until=NULL, dispatch_started_at=NULL, dispatch_preserve_schedule=0, dispatch_enabled_before=NULL WHERE id=? AND claim_token=?"
            parameters.extend((schedule["id"], claim_token))
            cursor = connection.execute(
                query,
                parameters,
            )
            if cursor.rowcount != 1:
                error = _DISPATCH_UNCERTAIN
        updated = self.get(schedule["id"]) or schedule
        return {"schedule": updated, "task_id": task_id or None, "error": error or None}

    def stats(self) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) total, SUM(enabled) enabled, COALESCE(SUM(run_count), 0) runs
                FROM schedules
                """
            ).fetchone()
        return {"total": row["total"], "enabled": row["enabled"] or 0, "runs": row["runs"]}

    async def start(self) -> None:
        if self._runner and not self._runner.done():
            return
        self._runner = asyncio.create_task(self._loop(), name="deepdesk-scheduler")

    async def stop(self) -> None:
        if not self._runner:
            return
        self._runner.cancel()
        try:
            await self._runner
        except asyncio.CancelledError:
            pass
        self._runner = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_async(self.run_due)
            except Exception:
                # Individual dispatch errors are stored on the schedule; the loop must survive.
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(1)

    def _async_call_cancelled(self) -> bool:
        cancelled = getattr(self._dispatch_context, "cancelled", None)
        return cancelled is not None and cancelled.is_set()

    def _call_dispatch(self, prompt, policy, profile):
        loop = getattr(self._dispatch_context, "loop", None)
        if loop is None:
            if inspect.iscoroutinefunction(self.dispatch):
                raise DispatchNotStartedError("Asynchronous dispatch requires Scheduler.run_async")
            return self.dispatch(prompt, policy, profile)
        cancelled = self._dispatch_context.cancelled

        async def dispatch_on_owner_loop():
            if cancelled.is_set():
                raise DispatchNotStartedError("Schedule operation cancelled before task creation")
            # TaskManager.create owns asyncio tasks and must stay on its loop.
            # SQLite leases/acknowledgements remain in the worker thread.
            result = self.dispatch(prompt, policy, profile)
            return await result if inspect.isawaitable(result) else result

        return asyncio.run_coroutine_threadsafe(dispatch_on_owner_loop(), loop).result()

    async def run_async(self, function: Callable, *args, **kwargs):
        """Run blocking schedule storage without abandoning a dispatch on Stop."""
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()

        def invoke():
            self._dispatch_context.loop = loop
            self._dispatch_context.cancelled = cancelled
            try:
                return function(*args, **kwargs)
            finally:
                del self._dispatch_context.loop
                del self._dispatch_context.cancelled

        return await run_owned_thread(invoke, on_cancel=cancelled.set)
