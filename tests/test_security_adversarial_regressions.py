from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from deepdesk.command_safety import accesses_sensitive_environment
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.builtin.memory import MemoryTool
from deepdesk.plugins.builtin.process_manager import _is_script_or_command_launcher
from deepdesk.plugins.builtin.sandbox import SandboxTool
from deepdesk.scheduler import Scheduler, ScheduleRequest
from deepdesk.subprocess_env import credential_safe_environment

_OPAQUE_TEST_CREDENTIAL = "ZXQ7m2P9v4N8c6B3w5R1"
_MEMORY_REDACTION_MARKER = "memory-secret-redaction-physical-v1"


def _assert_sqlite_has_no_value(database: Path, value: str) -> None:
    encoded = value.encode("utf-8")
    for candidate in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        if candidate.exists():
            assert encoded not in candidate.read_bytes()


def _memory_marker(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT value FROM security_migrations WHERE name = ?",
            (_MEMORY_REDACTION_MARKER,),
        ).fetchone()
    return str(row[0]) if row else ""


def test_memory_physical_scrub_runs_once_for_stable_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "memory.sqlite"
    compactions: list[int] = []

    def record_compaction(connection: sqlite3.Connection) -> None:
        compactions.append(int(connection.execute("PRAGMA page_count").fetchone()[0]))

    monkeypatch.setattr(
        MemoryTool, "_compact_redacted_pages", staticmethod(record_compaction)
    )

    MemoryTool(database)
    assert len(compactions) == 1
    assert _memory_marker(database) == "completed"

    MemoryTool(database)
    assert len(compactions) == 1
    assert _memory_marker(database) == "completed"


@pytest.mark.asyncio
async def test_memory_new_secret_rewrites_and_compacts_without_persisting_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "memory.sqlite"
    context = ToolContext(task_id="memory-new-secret-test", workspace=str(tmp_path))
    compactions: list[int] = []

    def record_compaction(connection: sqlite3.Connection) -> None:
        compactions.append(int(connection.execute("PRAGMA page_count").fetchone()[0]))

    monkeypatch.setattr(
        MemoryTool, "_compact_redacted_pages", staticmethod(record_compaction)
    )
    legacy = MemoryTool(database)
    created = await legacy.execute(
        {
            "action": "put",
            "title": "legacy note",
            "content": f"opaque value {_OPAQUE_TEST_CREDENTIAL}",
        },
        context,
    )
    assert len(compactions) == 1

    migrated = MemoryTool(database, secret_values=lambda: [_OPAQUE_TEST_CREDENTIAL])
    stored = await migrated.execute(
        {"action": "get", "id": created["id"]}, context
    )

    assert len(compactions) == 2
    assert _OPAQUE_TEST_CREDENTIAL not in stored["content"]
    assert "[REDACTED]" in stored["content"]
    with sqlite3.connect(database) as connection:
        markers = connection.execute(
            "SELECT name, value FROM security_migrations ORDER BY name"
        ).fetchall()
    assert markers == [(_MEMORY_REDACTION_MARKER, "completed")]


def test_memory_pending_physical_scrub_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "memory.sqlite"
    attempts = 0

    def fail_once(_connection: sqlite3.Connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated interrupted compaction")

    monkeypatch.setattr(
        MemoryTool, "_compact_redacted_pages", staticmethod(fail_once)
    )

    with pytest.raises(RuntimeError, match="interrupted compaction"):
        MemoryTool(database)
    assert attempts == 1
    assert _memory_marker(database) == "pending"

    MemoryTool(database)
    assert attempts == 2
    assert _memory_marker(database) == "completed"

    MemoryTool(database)
    assert attempts == 2


@pytest.mark.asyncio
async def test_memory_rejects_configured_secret_and_scrubs_legacy_sqlite_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.sqlite"
    context = ToolContext(task_id="memory-secret-test", workspace=str(tmp_path))
    legacy = MemoryTool(database)
    created = await legacy.execute(
        {
            "action": "put",
            "title": "legacy note",
            "content": f"opaque value {_OPAQUE_TEST_CREDENTIAL}",
        },
        context,
    )

    migrated = MemoryTool(database, secret_values=lambda: [_OPAQUE_TEST_CREDENTIAL])
    stored = await migrated.execute(
        {"action": "get", "id": created["id"]},
        context,
    )

    assert _OPAQUE_TEST_CREDENTIAL not in stored["content"]
    assert "[REDACTED]" in stored["content"]
    _assert_sqlite_has_no_value(database, _OPAQUE_TEST_CREDENTIAL)
    with pytest.raises(PermissionError, match="Refusing to store"):
        await migrated.execute(
            {
                "action": "put",
                "title": "new note",
                "content": _OPAQUE_TEST_CREDENTIAL,
            },
            context,
        )


def test_scheduler_rejects_configured_secret_and_scrubs_legacy_sqlite_pages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "scheduler.sqlite"
    legacy = Scheduler(database, lambda *_args: "legacy-task")
    created = legacy.create(
        ScheduleRequest(
            name="legacy schedule",
            prompt=f"use opaque value {_OPAQUE_TEST_CREDENTIAL}",
            kind="interval",
            expression="60",
        )
    )

    migrated = Scheduler(
        database,
        lambda *_args: "migrated-task",
        secret_values=lambda: [_OPAQUE_TEST_CREDENTIAL],
    )
    stored = migrated.get(created["id"])

    assert stored is not None
    assert _OPAQUE_TEST_CREDENTIAL not in stored["prompt"]
    assert "[REDACTED]" in stored["prompt"]
    _assert_sqlite_has_no_value(database, _OPAQUE_TEST_CREDENTIAL)
    with pytest.raises(ValueError, match="must not contain"):
        migrated.create(
            ScheduleRequest(
                name="new schedule",
                prompt=_OPAQUE_TEST_CREDENTIAL,
                kind="interval",
                expression="60",
            )
        )


@pytest.mark.asyncio
async def test_sandbox_blocks_os_persistence_before_runner(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeSandbox:
        def status(self) -> dict[str, bool]:
            return {"available": True}

        async def run(self, command: str, **_kwargs):
            calls.append(command)
            return {"exit_code": 0}

    tool = SandboxTool(FakeSandbox())
    arguments = {
        "action": "run",
        "command": "schtasks.exe /Create /TN AgentTest /TR notepad.exe /SC ONLOGON",
    }

    assert tool.risk(arguments) == Risk.HIGH
    with pytest.raises(PermissionError, match="OS-level persistence"):
        await tool.execute(
            arguments,
            ToolContext(task_id="persistence-test", workspace=str(tmp_path)),
        )
    assert calls == []


@pytest.mark.parametrize(
    "application",
    [
        "msiexec.exe",
        "rundll32.exe",
        "regsvr32.exe",
        "wmic.exe",
        "bitsadmin.exe",
    ],
)
def test_process_manager_rejects_detached_native_launchers(application: str) -> None:
    assert _is_script_or_command_launcher(application)


def test_child_environment_strips_cookie_credentials(monkeypatch) -> None:
    monkeypatch.setenv("DELIVEROO_COOKIE", _OPAQUE_TEST_CREDENTIAL)
    monkeypatch.setenv("ORDINARY_FEATURE_FLAG", "enabled")

    environment = credential_safe_environment()

    assert "DELIVEROO_COOKIE" not in environment
    assert environment["ORDINARY_FEATURE_FLAG"] == "enabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "feishu-listener-token",
        "feishu-listener-token.vault",
        "mobile-device-secrets.vault",
        "mobile-devices.json",
    ],
)
async def test_filesystem_blocks_feishu_and_mobile_credential_files(
    tmp_path: Path,
    filename: str,
) -> None:
    target = tmp_path / filename
    target.write_text(_OPAQUE_TEST_CREDENTIAL, encoding="utf-8")

    with pytest.raises(PermissionError, match="Credential and secret files"):
        await FileSystemTool().execute(
            {"action": "read", "path": filename},
            ToolContext(task_id="credential-file-test", workspace=str(tmp_path)),
        )


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content data/feishu-listener-token.vault",
        "Get-Content data/mobile-device-secrets.vault",
        "Get-Content data/mobile-devices.json",
        "rg key_local data",
    ],
)
def test_shell_classifier_blocks_feishu_and_mobile_credential_reads(command: str) -> None:
    assert accesses_sensitive_environment(command)
