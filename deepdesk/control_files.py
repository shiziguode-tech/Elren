from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import stat
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from deepdesk.secret_storage import (
    LocalSecretVault,
    SecretProtector,
    atomic_write_secure,
)


class ControlFileProtectionError(PermissionError):
    """A model-accessible capability attempted to change Elren's control plane."""


class ControlFileStateError(RuntimeError):
    """The authenticated control-plane baseline could not be loaded safely."""


class ControlFilePolicyUpgradeRequired(ControlFileStateError):
    """A human must explicitly accept a changed protected-file manifest."""


# These files can enter a system/developer prompt directly, define the long-lived
# identity, or instruct an autonomous runtime.  They are immutable while Elren is
# running.  A human accepts an intentional edit by stopping Elren and starting it
# once with ELREN_ACCEPT_CONTROL_FILE_CHANGES=1 in the *process environment*.
CONTROL_FILES = (
    "AGENTS.md",
    "BOOTSTRAP.md",
    "CLAUDE.md",
    "GEMINI.md",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "TOOLS.md",
    "MEMORY.md",
    "HEARTBEAT.md",
    # Defense-in-depth recovery for the loaders/guard themselves. Python imports
    # precede this in-process guard, so these entries are tamper evidence and
    # restart recovery—not a replacement for a signed native pre-import verifier
    # or a restricted operating-system account.
    "deepdesk/__init__.py",
    "deepdesk/main.py",
    "deepdesk/engine.py",
    "deepdesk/harness.py",
    "deepdesk/models.py",
    "deepdesk/deepseek.py",
    "deepdesk/subagents.py",
    "deepdesk/subagent_catalog.json",
    "deepdesk/control_files.py",
    "deepdesk/project_rules.py",
    "deepdesk/runtime_settings.py",
    "deepdesk/web_security.py",
    "deepdesk/command_safety.py",
    "deepdesk/mcp_runtime.py",
    "deepdesk/openclaw_bridge.py",
    "deepdesk/subprocess_env.py",
    "deepdesk/posix_sandbox.py",
    "deepdesk/windows_sandbox.py",
    "deepdesk/secret_storage.py",
    "deepdesk/provider_secrets.py",
    "deepdesk/custom_providers.py",
    "deepdesk/secret_redaction.py",
    "deepdesk/audit.py",
    "deepdesk/task_store.py",
    "deepdesk/task_file_links.py",
    "deepdesk/scheduler.py",
    "deepdesk/mobile_bridge.py",
    "deepdesk/feishu.py",
    "deepdesk/telegram.py",
    "deepdesk/tool_runtime.py",
    "deepdesk/plugins/base.py",
    "deepdesk/plugins/registry.py",
    "deepdesk/plugins/builtin/filesystem.py",
    "deepdesk/plugins/builtin/shell.py",
    "deepdesk/plugins/builtin/sandbox.py",
    "deepdesk/plugins/builtin/process_manager.py",
    "deepdesk/plugins/builtin/memory.py",
    "deepdesk/plugins/builtin/jianpu_omr.py",
    "deepdesk/plugins/builtin/remote_settings.py",
    "deepdesk/plugins/builtin/skills.py",
    "deepdesk/plugins/builtin/mcp.py",
    "deepdesk/plugins/builtin/openclaw.py",
    "deepdesk/plugins/builtin/computer.py",
    "deepdesk/plugins/builtin/computer_use.py",
    "deepdesk/plugins/builtin/live_computer_use.py",
    "deepdesk/live_control_flow.py",
    "deepdesk/live_control_vision.py",
    "deepdesk/live_control_geometry.py",
    "deepdesk/live_control_desktop.py",
    "deepdesk/plugins/builtin/windows_ui.py",
    "deepdesk/plugins/builtin/macos_ui.py",
    # The same-origin Web UI can invoke local settings, approvals, and task
    # actions. Protect its executable surface and final-cascade stylesheet from
    # autonomous runtime modification just like the Python control plane.
    "deepdesk/static/index.html",
    "deepdesk/static/app.js",
    "deepdesk/static/editorial-ui.css",
)

# Protect complete packages, not only the visible instruction file.  Otherwise a
# SKILL.md can remain unchanged while an Agent rewrites the script it tells the
# next Agent to execute.  External Python plugins are executable host code.
CONTROL_DIRECTORIES = (
    "skills",
    ".deepdesk/skills",
    "plugins",
    ".github",
    ".cursor/rules",
    ".windsurf/rules",
    "work/openclaw-runtime/mcp-servers",
)

PROJECT_RULE_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
)

_MAX_CONTROL_ENTRIES = 10_000
_MAX_CONTROL_FILE_BYTES = 4 * 1024 * 1024
_MAX_CONTROL_TOTAL_BYTES = 64 * 1024 * 1024
_STATE_FORMAT = 2
_RUNTIME_CONTROL_KEYS = (
    "custom_system_prompt_suffix",
    "discussion_team_enabled",
    "discussion_team",
)
_RUNTIME_INVALID = object()
_T = TypeVar("_T")


def _policy_fingerprint() -> str:
    payload = json.dumps(
        {
            "files": sorted(_canonical_relative(item) for item in CONTROL_FILES),
            "directories": sorted(
                _canonical_relative(item) for item in CONTROL_DIRECTORIES
            ),
            "runtime_control_keys": sorted(_RUNTIME_CONTROL_KEYS),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_part(value: str) -> str:
    # Win32 treats names case-insensitively and normally removes trailing spaces
    # and dots.  A colon in the final component selects an NTFS alternate stream;
    # classify it as the underlying control file even though an ADS is not loaded.
    normalized = value.rstrip(" .").casefold()
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0].rstrip(" .")
    return normalized


def _canonical_relative(value: str | Path) -> str:
    text = str(value).replace("\\", "/")
    parts = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            parts.append(part)
        else:
            parts.append(_canonical_part(part))
    return "/".join(parts)


_CONTROL_FILE_KEYS = frozenset(_canonical_relative(item) for item in CONTROL_FILES)
_CONTROL_DIRECTORY_KEYS = tuple(_canonical_relative(item) for item in CONTROL_DIRECTORIES)


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == (
            os.path.normcase(str(root))
        )
    except ValueError:
        return False


def _relative_without_resolving(path: Path, root: Path) -> str | None:
    absolute = Path(os.path.abspath(path))
    if not _within(absolute, root):
        return None
    try:
        return absolute.relative_to(root).as_posix()
    except ValueError:
        # pathlib.relative_to is case-sensitive on some Python/Windows builds.
        root_text = os.path.normcase(str(root)).rstrip("\\/")
        path_text = os.path.normcase(str(absolute))
        if path_text == root_text:
            return ""
        prefix = root_text + os.sep
        return path_text[len(prefix) :].replace("\\", "/") if path_text.startswith(prefix) else None


def _relative_is_control_path(relative: str, *, include_ancestors: bool = True) -> bool:
    candidate = _canonical_relative(relative)
    if candidate in _CONTROL_FILE_KEYS:
        return True
    for exact in _CONTROL_FILE_KEYS:
        if candidate.startswith(exact + "/"):
            return True
        if include_ancestors and (not candidate or exact.startswith(candidate + "/")):
            return True
    for directory in _CONTROL_DIRECTORY_KEYS:
        if candidate == directory or candidate.startswith(directory + "/"):
            return True
        if include_ancestors and (not candidate or directory.startswith(candidate + "/")):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _Entry:
    relative: str
    kind: str
    data: bytes = b""
    mode: int = 0
    links: int = 1

    def serialized(self) -> dict[str, Any]:
        return {
            "relative": self.relative,
            "kind": self.kind,
            "data": base64.b64encode(self.data).decode("ascii") if self.data else "",
            "mode": self.mode,
            "links": self.links,
        }

    @classmethod
    def from_serialized(cls, value: Any) -> _Entry:
        if not isinstance(value, dict):
            raise ControlFileStateError("The control-plane baseline contains an invalid entry")
        relative = str(value.get("relative") or "")
        kind = str(value.get("kind") or "")
        if kind not in {"file", "directory"} or not _relative_is_control_path(
            relative, include_ancestors=False
        ):
            raise ControlFileStateError("The control-plane baseline contains an unsafe path")
        try:
            data = base64.b64decode(str(value.get("data") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ControlFileStateError("The control-plane baseline contains invalid data") from exc
        if kind == "directory" and data:
            raise ControlFileStateError("A control-plane directory contains file data")
        return cls(
            relative=relative,
            kind=kind,
            data=data,
            mode=int(value.get("mode") or 0),
            links=max(1, int(value.get("links") or 1)),
        )


def _default_state_path(workspace: Path) -> Path:
    identity = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        root = base / "Elren" / "security" / "control-plane"
    elif sys_platform() == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Elren" / "security"
    else:
        state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        root = (Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state")
        root = root / "elren" / "security"
    return root / f"{identity}.vault"


def sys_platform() -> str:
    # A tiny indirection keeps platform-specific state-path tests deterministic.
    import sys

    return sys.platform


class ControlFileGuard:
    """Host-owned immutable snapshot for every model-reachable control surface."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        persistent: bool = False,
        state_path: Path | None = None,
        protector: SecretProtector | None = None,
        accept_changes: bool | None = None,
        watchdog_interval: float = 0.25,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._sync_lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self._watchdog_interval = max(0.05, float(watchdog_interval))
        self._stop_event = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._project_rule_cache_keys: set[tuple[int, int]] = set()
        self._generation = 0
        self._active_model_tool_calls = 0
        self._persistent = bool(persistent)
        self._policy_fingerprint = _policy_fingerprint()
        self._state_lease: Any | None = None
        self._trusted_state_bytes = b""
        self._bundled_manifest_digest = ""
        self._bundled_package_digests: dict[str, str] = {}
        self._state_parent_identity: tuple[int, int] | None = None
        self._workspace_identity = hashlib.sha256(
            os.path.normcase(str(self.workspace)).encode("utf-8")
        ).hexdigest()
        self.state_path = state_path or _default_state_path(self.workspace)
        self.audit_path = self.state_path.with_suffix(".audit.jsonl")
        # In-memory guards never serialize secrets. Do not trigger Keychain
        # access (or require a logged-in desktop) for offline/source checks.
        self._vault = LocalSecretVault(self.state_path, protector=protector) if self._persistent else None
        self._runtime_settings_path = self.workspace / "data" / "runtime-settings.json"

        if self._persistent:
            self._acquire_state_lease()

        try:
            self._initialize_baseline(accept_changes)
            self._upgrade_packaged_workspace()
        except BaseException:
            self._release_state_lease()
            raise

    def _initialize_baseline(self, accept_changes: bool | None) -> None:
        if accept_changes is None:
            # Read only the inherited process environment. Settings/.env parsing
            # never grants this capability. Consume it so child tools do not
            # inherit a reusable acceptance switch.
            accept_changes = os.environ.pop("ELREN_ACCEPT_CONTROL_FILE_CHANGES", "") == "1"

        if (
            self._persistent
            and not self.state_path.is_file()
            and not accept_changes
            and self._quarantined_predecessor_exists()
        ):
            self._audit("quarantined_policy_upgrade_required")
            raise ControlFilePolicyUpgradeRequired(
                "A previous control-plane baseline was quarantined after a policy change. "
                "Elren did not trust or modify the workspace. Review the installed files, "
                "then perform a one-time cold start with "
                "ELREN_ACCEPT_CONTROL_FILE_CHANGES=1 in the process environment."
            )

        restoring_persistent = self._persistent and self.state_path.is_file() and not accept_changes
        current = self._capture_surface(allow_untrusted_links=restoring_persistent)
        current_runtime_control = self._read_runtime_control()
        if self._persistent and self.state_path.is_file() and not accept_changes:
            self._entries, self._trusted_runtime_control = self._load_persistent_state()
            changed = self._difference_paths(current, self._entries)
            if current_runtime_control != self._trusted_runtime_control:
                changed.append("data/runtime-settings.json#ai_control_fields")
            if changed:
                self._restore_locked(changed, reason="cold_start_persistent_baseline")
                self._audit("cold_start_restored", paths=changed)
        else:
            self._entries = current
            self._trusted_runtime_control = (
                self._default_runtime_control()
                if current_runtime_control is _RUNTIME_INVALID
                else dict(current_runtime_control)
            )
            self._detach_initial_hardlinks()
            if self._persistent:
                self._save_persistent_state()
            self._audit(
                "baseline_accepted" if accept_changes else "baseline_created",
                paths=sorted(entry.relative for entry in self._entries.values()),
            )
        if self._persistent:
            self._remember_state_snapshot()

    def _quarantined_predecessor_exists(self) -> bool:
        quarantine = self.state_path.parent / "quarantine"
        if not quarantine.is_dir():
            return False
        try:
            return any(
                candidate.is_file()
                for candidate in quarantine.glob(f"*/{self.state_path.name}")
            )
        except OSError:
            return False

    def _upgrade_packaged_workspace(self) -> None:
        # Only the signed frozen Mac application is an automatic vendor-update
        # authority. Source runs and Windows never import this migration path.
        import sys

        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            return
        from deepdesk.bundled_workspace import packaged_update

        try:
            update = packaged_update(self._bundled_manifest_digest)
            if update is not None:
                self._migrate_vendor_update(update)
        except Exception as exc:
            raise ControlFileStateError("Could not safely update bundled skills/plugins") from exc

    def _migrate_vendor_update(self, update: Any) -> None:
        """Cold-start-only, signature-verified migration, never a tool/API action.

        Compare the authenticated baseline, NOT possibly tampered on-disk files.
        Preserve a whole customized package; do not mix user and vendor code.
        """
        from deepdesk.bundled_workspace import digest_files

        with self._sync_lock:
            if self._watchdog is not None or self._active_model_tool_calls:
                raise ControlFileStateError("Vendor migration requires a cold start")
            old_entries = self._entries
            old_packages = self._bundled_package_digests
            old_manifest = self._bundled_manifest_digest
            candidate = dict(old_entries)
            managed = dict(old_packages)
            updated: list[str] = []
            preserved: list[str] = []
            for package in update.packages:
                key = _canonical_relative(package.root)
                entries = {k: e for k, e in old_entries.items() if k == key or k.startswith(key + "/")}
                current = {e.relative: e.data for e in entries.values() if e.kind == "file"}
                known = {package.digest, *package.predecessors}
                if key in old_packages:
                    known.add(old_packages[key])
                # A custom file used as an ancestor must never be replaced with
                # a directory merely to make a newly bundled package fit.
                ancestors = list(Path(package.root).parents)[:-1]
                conflict = any(
                    (e := old_entries.get(_canonical_relative(parent.as_posix()))) is not None
                    and e.kind != "directory" for parent in ancestors
                )
                conflict = conflict or any(
                    (e := old_entries.get(_canonical_relative(relative))) is not None
                    and e.kind == "directory" for relative in package.files
                )
                if conflict or (entries and (not current or digest_files(current) not in known)):
                    preserved.append(package.root)
                    continue
                if current and digest_files(current) == package.digest:
                    managed[key] = package.digest
                    continue
                # Only pristine vendor files can be replaced/retired. Keep
                # directories (including user-created empty directories).
                for entry_key, entry in entries.items():
                    if entry.kind == "file":
                        candidate.pop(entry_key)
                for relative, data in package.files.items():
                    path = Path(relative)
                    for parent in reversed(list(path.parents)[:-1]):
                        parent_relative = parent.as_posix()
                        parent_key = _canonical_relative(parent_relative)
                        existing = candidate.get(parent_key)
                        if existing is not None and existing.kind != "directory":
                            raise ControlFileStateError("Vendor resource parent conflicts with a file")
                        candidate.setdefault(parent_key, _Entry(parent_relative, "directory", mode=0o777 if os.name == "nt" else 0o755))
                    file_key = _canonical_relative(relative)
                    if file_key in candidate and candidate[file_key].kind != "file":
                        raise ControlFileStateError("Vendor resource conflicts with an existing directory")
                    mode = package.modes[relative]
                    if os.name == "nt":  # In-process portability tests; macOS uses POSIX modes.
                        mode = 0o666 | (0o111 if path.suffix.lower() in {".exe", ".com", ".bat", ".cmd"} else 0)
                    candidate[file_key] = _Entry(relative, "file", data, mode)
                managed[key] = package.digest
                updated.append(package.root)
            if len(candidate) > _MAX_CONTROL_ENTRIES or sum(len(e.data) for e in candidate.values()) > _MAX_CONTROL_TOTAL_BYTES:
                raise ControlFileStateError("Vendor update exceeds protected snapshot limits")
            self._entries = candidate
            self._bundled_package_digests = managed
            self._bundled_manifest_digest = update.manifest_digest
            try:
                if updated:
                    self._restore_locked(updated, reason="signed_vendor_upgrade")
                if self._persistent:
                    self._save_persistent_state()
            except BaseException:
                self._entries = old_entries
                self._bundled_package_digests = old_packages
                self._bundled_manifest_digest = old_manifest
                # A crash before vault commit also recovers this old snapshot
                # on next boot; no acceptance switch or half-trusted baseline.
                self._restore_locked(updated, reason="signed_vendor_upgrade_rollback")
                if self._persistent:
                    self._save_persistent_state()
                raise
            self._generation += 1
            self._project_rule_cache_keys.clear()
            self._audit("signed_vendor_upgrade", updated=updated, preserved_custom=preserved)

    @property
    def trusted_runtime_system_prompt_suffix(self) -> str:
        with self._sync_lock:
            return str(self._trusted_runtime_control["custom_system_prompt_suffix"])

    @property
    def persistent(self) -> bool:
        return self._persistent

    @property
    def trusted_runtime_discussion_team(self) -> list[dict[str, Any]]:
        with self._sync_lock:
            return json.loads(json.dumps(self._trusted_runtime_control["discussion_team"]))

    @property
    def local_ui_control_update_allowed(self) -> bool:
        with self._sync_lock:
            return self._active_model_tool_calls == 0

    @property
    def _lease_path(self) -> Path:
        return self.state_path.with_suffix(self.state_path.suffix + ".lock")

    def _acquire_state_lease(self) -> None:
        if not self._persistent or self._state_lease is not None:
            return
        self._lease_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lease_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise ControlFileStateError(
                "Another Elren process already owns this control-plane baseline"
            ) from exc
        self._state_lease = handle
        try:
            self._lease_path.chmod(0o600)
        except OSError:
            pass

    def _release_state_lease(self) -> None:
        handle = self._state_lease
        self._state_lease = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def _remember_state_snapshot(self) -> None:
        if not self._persistent:
            return
        try:
            parent = self.state_path.parent.stat()
            metadata = self.state_path.lstat()
            if (
                _is_reparse_point(self.state_path)
                or not stat.S_ISREG(metadata.st_mode)
                or int(metadata.st_nlink) != 1
            ):
                raise OSError("unsafe control-plane state file")
            self._state_parent_identity = (int(parent.st_dev), int(parent.st_ino))
            self._trusted_state_bytes = self.state_path.read_bytes()
        except OSError as exc:
            raise ControlFileStateError(
                "Could not pin the authenticated control-plane state"
            ) from exc

    def _verify_persistent_state_locked(self) -> list[str]:
        if not self._persistent or not self._trusted_state_bytes:
            return []
        try:
            parent = self.state_path.parent.stat()
            parent_identity = (int(parent.st_dev), int(parent.st_ino))
        except OSError as exc:
            raise ControlFileStateError(
                "The control-plane state directory is unavailable"
            ) from exc
        if parent_identity != self._state_parent_identity:
            raise ControlFileStateError(
                "The control-plane state directory identity changed; Elren failed closed"
            )
        safe = False
        try:
            metadata = self.state_path.lstat()
            safe = (
                not _is_reparse_point(self.state_path)
                and stat.S_ISREG(metadata.st_mode)
                and int(metadata.st_nlink) == 1
                and self.state_path.read_bytes() == self._trusted_state_bytes
            )
        except OSError:
            safe = False
        if safe:
            return []
        atomic_write_secure(self.state_path, self._trusted_state_bytes)
        self._remember_state_snapshot()
        self._audit("persistent_state_restored")
        return ["host:authenticated-control-plane-baseline"]

    def start(self) -> None:
        with self._sync_lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._acquire_state_lease()
            self.verify_and_restore(reason="watchdog_start")
            self._stop_event = threading.Event()
            self._watchdog = threading.Thread(
                target=self._watch_loop,
                name="elren-control-file-guard",
                daemon=True,
            )
            self._watchdog.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._sync_lock:
            thread = self._watchdog
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout)))
        try:
            with self._sync_lock:
                if self._watchdog is thread and (thread is None or not thread.is_alive()):
                    self._watchdog = None
                self.verify_and_restore(reason="shutdown")
        finally:
            self._release_state_lease()

    close = stop

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self._watchdog_interval):
            try:
                self.verify_and_restore(reason="watchdog")
            except Exception as exc:
                self._audit("watchdog_error", error=f"{type(exc).__name__}: {exc}")

    def _capture_surface(
        self, *, allow_untrusted_links: bool = False
    ) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        total_bytes = 0

        def capture(path: Path, relative: str, *, recursive: bool) -> None:
            nonlocal total_bytes
            if len(entries) >= _MAX_CONTROL_ENTRIES:
                raise ControlFileStateError("The control-plane baseline has too many entries")
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ControlFileStateError(f"Could not inspect protected path: {relative}") from exc
            if _is_reparse_point(path):
                if allow_untrusted_links:
                    entries[_canonical_relative(relative)] = _Entry(
                        relative=relative.replace("\\", "/"),
                        kind="link",
                    )
                    return
                raise ControlFileStateError(
                    f"Protected paths may not contain symbolic links or junctions: {relative}"
                )
            key = _canonical_relative(relative)
            if stat.S_ISDIR(metadata.st_mode):
                entries[key] = _Entry(
                    relative=relative.replace("\\", "/"),
                    kind="directory",
                    mode=stat.S_IMODE(metadata.st_mode),
                )
                if not recursive:
                    return
                try:
                    children = sorted(path.iterdir(), key=lambda item: item.name.casefold())
                except OSError as exc:
                    raise ControlFileStateError(
                        f"Could not enumerate protected directory: {relative}"
                    ) from exc
                for child in children:
                    child_relative = f"{relative.rstrip('/')}/{child.name}"
                    capture(child, child_relative, recursive=True)
                return
            if not stat.S_ISREG(metadata.st_mode):
                raise ControlFileStateError(f"Protected path is not a regular file: {relative}")
            if metadata.st_size > _MAX_CONTROL_FILE_BYTES:
                raise ControlFileStateError(f"Protected file exceeds the size limit: {relative}")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ControlFileStateError(f"Could not read protected file: {relative}") from exc
            total_bytes += len(data)
            if total_bytes > _MAX_CONTROL_TOTAL_BYTES:
                raise ControlFileStateError("The control-plane baseline exceeds the total size limit")
            entries[key] = _Entry(
                relative=relative.replace("\\", "/"),
                kind="file",
                data=data,
                mode=stat.S_IMODE(metadata.st_mode),
                links=max(1, int(metadata.st_nlink)),
            )

        for relative in CONTROL_FILES:
            capture(self.workspace / relative, relative, recursive=False)
        for relative in CONTROL_DIRECTORIES:
            capture(self.workspace / relative, relative, recursive=True)
        return entries

    @staticmethod
    def _difference_paths(
        current: dict[str, _Entry], expected: dict[str, _Entry]
    ) -> list[str]:
        changed = []
        for key in sorted(set(current) | set(expected)):
            if current.get(key) != expected.get(key):
                entry = current.get(key) or expected.get(key)
                changed.append(entry.relative if entry is not None else key)
        return changed

    @staticmethod
    def _default_runtime_control() -> dict[str, Any]:
        return {
            "custom_system_prompt_suffix": "",
            "discussion_team_enabled": False,
            "discussion_team": [],
        }

    def _read_runtime_control(self) -> dict[str, Any] | object:
        if not self._runtime_settings_path.is_file():
            return self._default_runtime_control()
        try:
            value = json.loads(self._runtime_settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return _RUNTIME_INVALID
        if not isinstance(value, dict):
            return _RUNTIME_INVALID
        suffix = value.get("custom_system_prompt_suffix", "")
        team_enabled = value.get("discussion_team_enabled", False)
        team = value.get("discussion_team", [])
        if (
            not isinstance(suffix, str)
            or len(suffix) > 20_000
            or not isinstance(team_enabled, bool)
            or not isinstance(team, list)
        ):
            return _RUNTIME_INVALID
        try:
            encoded_team = json.dumps(team, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return _RUNTIME_INVALID
        if len(encoded_team) > 500_000:
            return _RUNTIME_INVALID
        return {
            "custom_system_prompt_suffix": suffix,
            "discussion_team_enabled": team_enabled,
            "discussion_team": json.loads(encoded_team),
        }

    def verify_and_restore(self, *, reason: str) -> list[str]:
        with self._sync_lock:
            state_changed = self._verify_persistent_state_locked()
            try:
                current = self._capture_surface(allow_untrusted_links=True)
            except ControlFileStateError:
                current = {}
            changed = self._difference_paths(current, self._entries)
            if self._read_runtime_control() != self._trusted_runtime_control:
                changed.append("data/runtime-settings.json#ai_control_fields")
            if changed:
                self._restore_locked(changed, reason=reason)
            all_changed = [*state_changed, *changed]
            if all_changed:
                self._audit("integrity_restored", reason=reason, paths=all_changed)
            return all_changed

    def _detach_initial_hardlinks(self) -> None:
        hardlinks = [entry.relative for entry in self._entries.values() if entry.links > 1]
        if not hardlinks:
            return
        self._restore_locked(hardlinks, reason="detach_hardlink")
        self._entries = self._capture_surface()
        self._audit("hardlinks_detached", paths=hardlinks)

    def _safe_target(self, relative: str) -> Path:
        if not _relative_is_control_path(relative, include_ancestors=False):
            raise ControlFileStateError("Refused to restore an unprotected path")
        target = Path(os.path.abspath(self.workspace / relative))
        if not _within(target, self.workspace):
            raise ControlFileStateError("Refused to restore a path outside the workspace")
        return target

    @staticmethod
    def _remove_node(path: Path) -> None:
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        if _is_reparse_point(path) or path.is_symlink():
            try:
                path.unlink()
            except IsADirectoryError:
                os.rmdir(path)
            return
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _ensure_real_parent(self, path: Path) -> None:
        relative = path.parent.relative_to(self.workspace)
        current = self.workspace
        for part in relative.parts:
            current /= part
            if current.exists() or current.is_symlink():
                if _is_reparse_point(current) or not current.is_dir():
                    self._remove_node(current)
                    current.mkdir()
            else:
                current.mkdir()

    def _atomic_restore_file(self, path: Path, entry: _Entry) -> None:
        self._ensure_real_parent(path)
        temporary = path.with_name(f".{path.name}.elren-control-{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(entry.data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary.chmod(entry.mode or 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_runtime_control(self) -> None:
        path = self._runtime_settings_path
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, UnicodeError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        value.update(json.loads(json.dumps(self._trusted_runtime_control)))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.elren-control-{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_locked(self, changed: list[str], *, reason: str) -> None:
        del changed, reason  # The full snapshot is small; restore deterministically.
        try:
            current = self._capture_surface(allow_untrusted_links=True)
        except ControlFileStateError:
            current = {}

        # Remove additions and type substitutions deepest-first without ever
        # following a link/junction supplied by a tool.
        for key in sorted(set(current) - set(self._entries), key=lambda item: item.count("/"), reverse=True):
            self._remove_node(self._safe_target(current[key].relative))
        for key, entry in sorted(
            self._entries.items(), key=lambda item: item[1].relative.count("/")
        ):
            target = self._safe_target(entry.relative)
            if entry.kind == "directory":
                if target.exists() or target.is_symlink():
                    if _is_reparse_point(target) or not target.is_dir():
                        self._remove_node(target)
                        self._ensure_real_parent(target)
                        target.mkdir()
                else:
                    self._ensure_real_parent(target)
                    target.mkdir()
                try:
                    target.chmod(entry.mode or 0o700)
                except OSError:
                    pass
            else:
                self._atomic_restore_file(target, entry)
        self._restore_runtime_control()

        restored = self._capture_surface()
        remaining = self._difference_paths(restored, self._entries)
        if remaining or self._read_runtime_control() != self._trusted_runtime_control:
            raise ControlFileStateError(
                "Elren could not restore its protected control-plane baseline"
            )

    def affected_paths(self, requested: str | Path) -> list[str]:
        raw = Path(str(requested or ""))
        lexical = raw if raw.is_absolute() else self.workspace / raw
        candidates: set[str] = set()
        relative = _relative_without_resolving(lexical, self.workspace)
        if relative is not None:
            candidates.add(_canonical_relative(relative))
        try:
            resolved = lexical.resolve(strict=False)
        except OSError:
            resolved = lexical.absolute()
        resolved_relative = _relative_without_resolving(resolved, self.workspace)
        if resolved_relative is not None:
            candidates.add(_canonical_relative(resolved_relative))

        affected: set[str] = set()
        for candidate in candidates:
            if _relative_is_control_path(candidate):
                affected.add(candidate or ".")
        try:
            if lexical.exists():
                for entry in self._entries.values():
                    if entry.kind != "file":
                        continue
                    protected = self.workspace / entry.relative
                    try:
                        if protected.exists() and os.path.samefile(lexical, protected):
                            affected.add(entry.relative)
                    except OSError:
                        continue
        except OSError:
            pass
        return sorted(affected)

    def _known_target(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        action = str(arguments.get("action") or "")
        if tool_name == "filesystem" and action in {
            "edit",
            "write",
            "append",
            "mkdir",
            "delete",
        }:
            return str(arguments.get("path") or "")
        if tool_name == "document" and action == "create":
            return str(arguments.get("path") or "")
        return None

    async def call_plugin(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: Any,
        callback: Callable[[], Awaitable[_T]],
    ) -> _T:
        async with self._async_lock:
            changed_before = self.verify_and_restore(reason=f"before:{tool_name}")
            if changed_before:
                raise ControlFileProtectionError(
                    "Elren restored an unauthorized control-file change before this tool call"
                )
            target = self._known_target(tool_name, arguments)
            if target is not None:
                affected = self.affected_paths(target)
                if affected:
                    self._audit(
                        "tool_write_blocked",
                        task_id=str(getattr(context, "task_id", "")),
                        source=str(getattr(context, "source", "")),
                        tool=tool_name,
                        paths=affected,
                    )
                    raise ControlFileProtectionError(
                        "AI tools cannot modify Elren control files, instruction packages, "
                        "skills, plugins, or their parent directories"
                    )

            value: _T | None = None
            error: BaseException | None = None
            with self._sync_lock:
                self._active_model_tool_calls += 1
            try:
                value = await callback()
            except BaseException as exc:
                error = exc
            finally:
                with self._sync_lock:
                    self._active_model_tool_calls = max(
                        0, self._active_model_tool_calls - 1
                    )
            changed_after = self.verify_and_restore(reason=f"after:{tool_name}")
            if changed_after:
                self._audit(
                    "tool_mutation_rolled_back",
                    task_id=str(getattr(context, "task_id", "")),
                    source=str(getattr(context, "source", "")),
                    tool=tool_name,
                    paths=changed_after,
                )
                raise ControlFileProtectionError(
                    f"{tool_name} changed protected control files; the host restored them"
                )
            if error is not None:
                raise error
            return value  # type: ignore[return-value]

    def run_untrusted_import(self, label: str, callback: Callable[[], _T]) -> _T:
        with self._sync_lock:
            changed_before = self.verify_and_restore(reason=f"before_import:{label}")
            if changed_before:
                raise ControlFileProtectionError("Control files changed before plugin import")
            value: _T | None = None
            error: BaseException | None = None
            try:
                value = callback()
            except BaseException as exc:
                error = exc
            changed_after = self.verify_and_restore(reason=f"after_import:{label}")
            if changed_after:
                raise ControlFileProtectionError(
                    "External plugin import changed protected control files; changes were restored"
                )
            if error is not None:
                raise error
            return value  # type: ignore[return-value]

    def trusted_bytes(self, path: str | Path) -> bytes | None:
        raw = Path(path)
        absolute = raw if raw.is_absolute() else self.workspace / raw
        relative = _relative_without_resolving(absolute, self.workspace)
        if relative is None:
            return None
        with self._sync_lock:
            entry = self._entries.get(_canonical_relative(relative))
            return bytes(entry.data) if entry is not None and entry.kind == "file" else None

    def trusted_skill_files(self) -> list[Path]:
        prefixes = (_canonical_relative("skills") + "/", _canonical_relative(".deepdesk/skills") + "/")
        with self._sync_lock:
            return [
                self.workspace / entry.relative
                for key, entry in sorted(self._entries.items())
                if entry.kind == "file"
                and key.endswith("/skill.md")
                and key.startswith(prefixes)
            ]

    def load_project_rules(self, max_chars: int) -> tuple[str, list[str], dict[str, Any]]:
        bounded_max = max(0, int(max_chars))
        with self._sync_lock:
            restored = self.verify_and_restore(reason="project_rules_load")
            paths: list[str] = []
            for relative in PROJECT_RULE_FILES:
                entry = self._entries.get(_canonical_relative(relative))
                if entry is not None and entry.kind == "file":
                    paths.append(entry.relative)
            for key, entry in sorted(self._entries.items()):
                if entry.kind != "file":
                    continue
                if key.startswith(".cursor/rules/") and key.endswith(".mdc"):
                    paths.append(entry.relative)
                if key.startswith(".windsurf/rules/") and key.endswith((".md", ".mdc")):
                    paths.append(entry.relative)
            blocks: list[str] = []
            loaded: list[str] = []
            remaining = bounded_max
            digest = hashlib.sha256()
            for relative in paths:
                entry = self._entries[_canonical_relative(relative)]
                if b"\x00" in entry.data[:4096]:
                    continue
                content = entry.data.decode("utf-8", errors="replace").strip()
                if not content:
                    continue
                allowance = max(0, remaining - len(relative) - 16)
                if allowance <= 0:
                    break
                clipped = content[:allowance]
                blocks.append(f"[{relative}]\n{clipped}")
                loaded.append(relative)
                remaining -= len(relative) + len(clipped) + 16
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(entry.data)
                digest.update(b"\0")
            cache_key = (self._generation, bounded_max)
            cache_hit = cache_key in self._project_rule_cache_keys
            self._project_rule_cache_keys.add(cache_key)
            return "\n\n".join(blocks), loaded, {
                "cache_hit": cache_hit,
                "fingerprint": digest.hexdigest(),
                "trusted_snapshot": True,
                "integrity_restored": bool(restored),
            }

    def accept_runtime_ai_control_settings(
        self,
        *,
        custom_system_prompt_suffix: str,
        discussion_team_enabled: bool,
        discussion_team: list[dict[str, Any]],
    ) -> None:
        """Commit values already saved by the local loopback Settings API."""

        candidate = {
            "custom_system_prompt_suffix": custom_system_prompt_suffix,
            "discussion_team_enabled": discussion_team_enabled,
            "discussion_team": discussion_team,
        }
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        if (
            not isinstance(custom_system_prompt_suffix, str)
            or len(custom_system_prompt_suffix) > 20_000
            or not isinstance(discussion_team_enabled, bool)
            or not isinstance(discussion_team, list)
            or len(encoded) > 520_000
        ):
            raise ValueError("Invalid AI control settings")
        with self._sync_lock:
            self._trusted_runtime_control = json.loads(encoded)
            if self._persistent:
                self._save_persistent_state()
            self._audit("local_ui_ai_control_settings_accepted")

    @contextmanager
    def runtime_settings_transaction(self) -> Iterator[None]:
        """Serialize a trusted Settings write with watchdog verification.

        The guard and ``RuntimeSettingsStore`` both write runtime-settings.json.
        Callers must keep the store commit and any trusted AI-control acceptance
        inside this boundary so a watchdog restore cannot publish a stale copy
        of unrelated user preferences between those operations.
        """

        with self._sync_lock:
            yield

    def _persistent_payload(self) -> dict[str, Any]:
        return {
            "format": _STATE_FORMAT,
            "policy_fingerprint": self._policy_fingerprint,
            "workspace_identity": self._workspace_identity,
            "entries": {
                key: entry.serialized() for key, entry in sorted(self._entries.items())
            },
            "trusted_runtime_control": self._trusted_runtime_control,
            "bundled_manifest_digest": self._bundled_manifest_digest,
            "bundled_package_digests": self._bundled_package_digests,
        }

    def _save_persistent_state(self) -> None:
        try:
            if self._vault is None:
                raise ControlFileStateError("No persistent vault configured")
            self._vault.write_verified(self._persistent_payload())
            self._remember_state_snapshot()
        except Exception as exc:
            raise ControlFileStateError(
                "Could not save the authenticated control-plane baseline"
            ) from exc

    def _load_persistent_state(self) -> tuple[dict[str, _Entry], dict[str, Any]]:
        try:
            if self._vault is None:
                raise ControlFileStateError("No persistent vault configured")
            payload = self._vault.read().values
        except Exception as exc:
            raise ControlFileStateError(
                "The authenticated control-plane baseline is unavailable or was tampered with"
            ) from exc
        if (
            int(payload.get("format") or 0) != _STATE_FORMAT
            or str(payload.get("policy_fingerprint") or "")
            != self._policy_fingerprint
        ):
            self._audit(
                "policy_upgrade_required",
                stored_format=int(payload.get("format") or 0),
                stored_policy_fingerprint=str(
                    payload.get("policy_fingerprint") or ""
                ),
                expected_policy_fingerprint=self._policy_fingerprint,
            )
            raise ControlFilePolicyUpgradeRequired(
                "The protected-file policy changed. Elren did not modify the workspace. "
                "Review the installed files, then perform a one-time cold start with "
                "ELREN_ACCEPT_CONTROL_FILE_CHANGES=1 in the process environment to accept them."
            )
        if str(payload.get("workspace_identity") or "") != self._workspace_identity:
            raise ControlFileStateError("The control-plane baseline belongs to another workspace")
        manifest_digest = payload.get("bundled_manifest_digest", "")
        package_digests = payload.get("bundled_package_digests", {})
        def valid_digest(value: Any) -> bool:
            return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        if (manifest_digest != "" and not valid_digest(manifest_digest)) or not isinstance(package_digests, dict):
            raise ControlFileStateError("Invalid vendor upgrade baseline")
        if len(package_digests) > _MAX_CONTROL_ENTRIES or not all(
            isinstance(key, str) and key.startswith(("skills/", "plugins/"))
            and key == _canonical_relative(key) and ".." not in key.split("/") and valid_digest(value)
            for key, value in package_digests.items()
        ):
            raise ControlFileStateError("Invalid vendor package baseline")
        self._bundled_manifest_digest = manifest_digest
        self._bundled_package_digests = dict(package_digests)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, dict) or len(raw_entries) > _MAX_CONTROL_ENTRIES:
            raise ControlFileStateError("The control-plane baseline entries are invalid")
        entries: dict[str, _Entry] = {}
        total = 0
        for key, raw in raw_entries.items():
            entry = _Entry.from_serialized(raw)
            canonical = _canonical_relative(entry.relative)
            if canonical != str(key) or canonical in entries:
                raise ControlFileStateError("The control-plane baseline path index is invalid")
            total += len(entry.data)
            if total > _MAX_CONTROL_TOTAL_BYTES:
                raise ControlFileStateError("The control-plane baseline is too large")
            entries[canonical] = entry
        runtime_control = payload.get("trusted_runtime_control")
        if not isinstance(runtime_control, dict):
            raise ControlFileStateError("The trusted runtime control settings are invalid")
        if set(runtime_control) != set(_RUNTIME_CONTROL_KEYS):
            raise ControlFileStateError("The trusted runtime control settings are incomplete")
        suffix = runtime_control.get("custom_system_prompt_suffix")
        team_enabled = runtime_control.get("discussion_team_enabled")
        team = runtime_control.get("discussion_team")
        if (
            not isinstance(suffix, str)
            or len(suffix) > 20_000
            or not isinstance(team_enabled, bool)
            or not isinstance(team, list)
            or len(json.dumps(team, ensure_ascii=False, sort_keys=True)) > 500_000
        ):
            raise ControlFileStateError("The trusted runtime control settings are invalid")
        return entries, json.loads(json.dumps(runtime_control))

    def _audit(self, event: str, **data: Any) -> None:
        if not self._persistent:
            return
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **data,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            try:
                self.audit_path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            # Protection never depends on diagnostic persistence.
            pass


_ACTIVE_GUARDS: dict[str, ControlFileGuard] = {}
_ACTIVE_LOCK = threading.RLock()


def _guard_key(workspace: str | Path) -> str:
    return os.path.normcase(str(Path(workspace).resolve()))


def activate_control_file_guard(
    workspace: str | Path,
    *,
    persistent: bool = False,
    state_path: Path | None = None,
    protector: SecretProtector | None = None,
    accept_changes: bool | None = None,
) -> ControlFileGuard:
    key = _guard_key(workspace)
    with _ACTIVE_LOCK:
        guard = _ACTIVE_GUARDS.get(key)
        if guard is not None:
            if persistent and not guard.persistent:
                raise ControlFileStateError(
                    "A non-persistent control guard is already active for this workspace"
                )
            return guard
        guard = ControlFileGuard(
            workspace,
            persistent=persistent,
            state_path=state_path,
            protector=protector,
            accept_changes=accept_changes,
        )
        _ACTIVE_GUARDS[key] = guard
        return guard


def active_control_file_guard(workspace: str | Path) -> ControlFileGuard | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_GUARDS.get(_guard_key(workspace))


def deactivate_control_file_guard(workspace: str | Path) -> None:
    with _ACTIVE_LOCK:
        guard = _ACTIVE_GUARDS.pop(_guard_key(workspace), None)
    if guard is not None:
        guard.stop()


def clear_control_file_guards_for_tests() -> None:
    with _ACTIVE_LOCK:
        guards = list(_ACTIVE_GUARDS.values())
        _ACTIVE_GUARDS.clear()
    for guard in guards:
        guard.stop()
