from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 1
MANIFEST_BUNDLE = "elren-tool-runtime"
DEFAULT_RELATIVE_ROOT = Path("work/tool-runtime")


def packaged_workspace_root(workspace: Path | str) -> Path:
    """Mac data is writable Application Support; signed tools stay inside the app."""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        resources = Path(sys.executable).resolve().parent.parent
        if resources.name == "Resources" and resources.parent.name == "Contents":
            return resources / "bundle"
    return Path(workspace).expanduser().resolve()


class ToolRuntimeManifestError(ValueError):
    """The package-local tool runtime manifest is malformed or unsafe."""


@dataclass(frozen=True)
class ToolRuntime:
    """Verified package-local command runtime.

    This is deliberately not a Docker dependency.  It uses the same useful
    properties for a desktop release (declared contents, a private filesystem,
    package-first command resolution and reproducible verification) while all
    executables remain ordinary Windows programs.  Host-native UI tools stay on
    the host; portable command-line dependencies live below ``work/tool-runtime``.
    """

    workspace: Path
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    bin_directories: tuple[Path, ...]
    tools: tuple[dict[str, Any], ...]

    @property
    def path_entries(self) -> tuple[str, ...]:
        return tuple(str(path) for path in self.bin_directories)

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        prepend_unique_path(environment, self.path_entries)
        environment["ELREN_TOOL_RUNTIME"] = str(self.root)
        return environment

    def status(self) -> dict[str, Any]:
        available = [tool for tool in self.tools if tool.get("present")]
        return {
            "ready": bool(self.bin_directories),
            "mode": "package-first-with-host-native-fallback",
            "root": str(self.root),
            "tool_count": len(self.tools),
            "available_tool_count": len(available),
            "tools": [
                {
                    "id": str(tool.get("id") or ""),
                    "version": str(tool.get("version") or ""),
                    "present": bool(tool.get("present")),
                    "host_only": bool(tool.get("host_only")),
                }
                for tool in self.tools
            ],
        }


def _within(root: Path, relative: str, *, kind: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ToolRuntimeManifestError(f"{kind} must be relative to the tool runtime")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ToolRuntimeManifestError(f"{kind} escapes the tool runtime") from exc
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        # Keep explicit integrity checks usable under the launcher's constrained
        # job and on low-memory computers.  A 1 MiB allocation failed in a real
        # status request even though hashing itself is streaming.
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolRuntimeManifestError("tool runtime manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise ToolRuntimeManifestError("tool runtime manifest must be an object")
    if payload.get("schema") != MANIFEST_SCHEMA or payload.get("bundle") != MANIFEST_BUNDLE:
        raise ToolRuntimeManifestError("unsupported tool runtime manifest")
    if str(payload.get("product_version") or "") != "1.0":
        raise ToolRuntimeManifestError("tool runtime product version does not match 1.0")
    return payload


def discover_tool_runtime(
    workspace: Path | str,
    *,
    verify_hashes: bool = False,
) -> ToolRuntime | None:
    """Return the declared tool runtime, or ``None`` when it is not packaged.

    Missing optional commands do not invalidate the entire runtime. Malformed
    paths and duplicate IDs always invalidate it. Full binary hashing is
    intentionally opt-in: release construction already performs it, while
    status polling and every child-process environment must remain lightweight.
    Callers performing an explicit integrity audit pass ``verify_hashes=True``.
    """

    workspace_path = packaged_workspace_root(workspace)
    root = (workspace_path / DEFAULT_RELATIVE_ROOT).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = _load_manifest(manifest_path)

    raw_bins = manifest.get("bin_directories", ["bin"])
    if not isinstance(raw_bins, list) or not all(isinstance(item, str) for item in raw_bins):
        raise ToolRuntimeManifestError("bin_directories must be a string list")
    bin_directories = tuple(
        path for item in raw_bins if (path := _within(root, item, kind="bin directory")).is_dir()
    )

    raw_tools = manifest.get("tools", [])
    if not isinstance(raw_tools, list):
        raise ToolRuntimeManifestError("tools must be a list")
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tools:
        if not isinstance(raw, dict):
            raise ToolRuntimeManifestError("each tool must be an object")
        tool_id = str(raw.get("id") or "").strip().casefold()
        if not tool_id or tool_id in seen:
            raise ToolRuntimeManifestError("tool IDs must be non-empty and unique")
        seen.add(tool_id)
        executable = str(raw.get("executable") or "").strip()
        host_only = bool(raw.get("host_only"))
        resolved = _within(root, executable, kind=f"tool {tool_id} executable") if executable else None
        present = bool(resolved and resolved.is_file())
        expected_hash = str(raw.get("sha256") or "").strip().casefold()
        if verify_hashes and expected_hash and present and _sha256(resolved) != expected_hash:
            raise ToolRuntimeManifestError(f"tool {tool_id} failed SHA-256 verification")
        tools.append(
            {
                **raw,
                "id": tool_id,
                "executable": executable,
                "resolved_executable": str(resolved) if resolved else "",
                "host_only": host_only,
                "present": present,
            }
        )
    return ToolRuntime(
        workspace=workspace_path,
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        bin_directories=bin_directories,
        tools=tuple(tools),
    )


def tool_runtime_path_entries(workspace: Path | str | None = None) -> tuple[str, ...]:
    root = (
        workspace
        or os.environ.get("ELREN_WORKSPACE")
        or os.environ.get("MILO_WORKSPACE")
        or os.environ.get("DEEPDESK_WORKSPACE")
        or Path(__file__).resolve().parents[1]
    )
    try:
        runtime = discover_tool_runtime(root)
    except ToolRuntimeManifestError:
        return ()
    return runtime.path_entries if runtime else ()


def prepend_unique_path(environment: dict[str, str], entries: Iterable[str]) -> None:
    # Windows environment names are case-insensitive, unlike ordinary dicts.
    # Merge aliases before launching a child so no stale Path can shadow PATH.
    keys = [key for key in environment if key.upper() == "PATH"] if os.name == "nt" else ["PATH"]
    current = [item for key in keys for item in environment.get(key, "").split(os.pathsep) if item]
    seen: set[str] = set()
    ordered: list[str] = []
    # Package entries must move ahead of host entries even if already present.
    for entry in [*entries, *current]:
        if not entry:
            continue
        comparable = entry.strip('"') if os.name == "nt" else entry
        normalized = os.path.normcase(os.path.abspath(comparable))
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(entry)
    for key in keys:
        environment.pop(key, None)
    environment["PATH"] = os.pathsep.join(ordered)
