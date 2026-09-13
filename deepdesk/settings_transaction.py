"""Authenticated rollback journal for the local settings commit boundary.

No credentials are serialized as plaintext: the journal itself is an OS-bound
vault and snapshots the existing provider vault ciphertext, not its cleartext.
Only the exact targets supplied by the host can be restored.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from deepdesk.secret_storage import LocalSecretVault, SecretProtector, atomic_write_secure


class SettingsRecoveryError(RuntimeError):
    """A pending rollback must succeed before further settings writes/startup."""


class SettingsCandidateError(ValueError):
    """A validated transport patch cannot form a supported complete setting."""


class SettingsTransaction:
    def __init__(self, data_dir: Path, targets: dict[str, Path], protector: SecretProtector | None = None):
        self.path = Path(data_dir) / "settings-transaction.vault"
        self.targets = {name: Path(path).resolve() for name, path in targets.items()}
        self.identity = hashlib.sha256(os.path.normcase(str(Path(data_dir).resolve())).encode()).hexdigest()
        self.protector = protector
        self.snapshot: dict[str, Any] | None = None

    def _vault(self) -> LocalSecretVault:
        return LocalSecretVault(self.path, self.protector)

    def begin(self) -> None:
        self.recover()
        self.snapshot = {
            "workspace": self.identity,
            "phase": "pending",
            "before": {
                name: base64.b64encode(path.read_bytes()).decode("ascii") if path.is_file() else None
                for name, path in self.targets.items()
            },
        }
        self._vault().write_verified(self.snapshot)

    def commit(self) -> None:
        if self.snapshot is None:
            raise RuntimeError("Settings transaction has not started")
        committed = {**self.snapshot, "phase": "committed"}
        try:
            self._vault().write_verified(committed)
        except Exception:
            # A writer can report failure after the atomic replacement. Resolve
            # that ambiguity from the authenticated receipt, not the exception.
            if self._vault().read().values != committed:
                raise
        self.snapshot = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # A committed receipt is harmless and is cleaned at next startup.
            pass

    def rollback(self) -> None:
        if self.snapshot is None:
            return
        # Keep a durable pending receipt until every restore has succeeded.
        if not self.path.is_file() or self._vault().read().values != self.snapshot:
            self._vault().write_verified(self.snapshot)
        self._restore(self.snapshot)
        self.path.unlink(missing_ok=True)
        self.snapshot = None

    def recover(self) -> None:
        if not self.path.is_file():
            return
        try:
            record = self._vault().read().values
            if record.get("workspace") != self.identity:
                raise ValueError("Wrong settings workspace")
            if record.get("phase") == "pending":
                self._restore(record)
            elif record.get("phase") != "committed":
                raise ValueError("Unknown settings transaction phase")
            self.path.unlink(missing_ok=True)
        except Exception:
            raise SettingsRecoveryError("Settings recovery is pending; existing recovery data was preserved") from None

    def _restore(self, record: dict[str, Any]) -> None:
        if record.get("workspace") != self.identity or not isinstance(record.get("before"), dict):
            raise SettingsRecoveryError("Invalid settings recovery record")
        if not set(record["before"]).issubset(self.targets):
            raise SettingsRecoveryError("Unexpected settings recovery target")
        # Decode all values before the first write; malformed recovery is not
        # allowed to partially change the current files.
        restored = {
            name: base64.b64decode(value, validate=True) if value is not None else None
            for name, value in record["before"].items()
        }
        for name, raw in restored.items():
            target = self.targets[name]
            if raw is None:
                target.unlink(missing_ok=True)
            elif not target.is_file() or target.read_bytes() != raw:
                atomic_write_secure(target, raw)


def copy_settings_state(value: Any) -> Any:
    """Copy mutable settings containers, retaining owned workers/locks intact."""
    if isinstance(value, dict):
        return {key: copy_settings_state(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_settings_state(item) for item in value]
    if isinstance(value, set):
        return set(value)
    if isinstance(value, tuple):
        return tuple(copy_settings_state(item) for item in value)
    return value
