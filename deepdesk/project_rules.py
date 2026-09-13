from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from deepdesk.control_files import active_control_file_guard

RULE_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".github/copilot-instructions.md",
)
RULE_GLOBS = (
    ".cursor/rules/*.mdc",
    ".windsurf/rules/*.md",
    ".windsurf/rules/*.mdc",
)
_MAX_CACHE_ENTRIES = 64


@dataclass(frozen=True, slots=True)
class _RuleCacheEntry:
    signature: tuple[tuple[str, int, int], ...]
    content: str
    files: tuple[str, ...]
    fingerprint: str


_CACHE: OrderedDict[tuple[str, int], _RuleCacheEntry] = OrderedDict()
_CACHE_LOCK = RLock()


def _candidates(root: Path) -> list[Path]:
    candidates = [root / relative for relative in RULE_FILES]
    for pattern in RULE_GLOBS:
        candidates.extend(sorted(root.glob(pattern)))
    return candidates


def _safe_candidates(root: Path) -> list[Path]:
    safe: list[Path] = []
    seen: set[Path] = set()
    for candidate in _candidates(root):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or (resolved != root and root not in resolved.parents):
            continue
        seen.add(resolved)
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        safe.append(resolved)
    return safe


def _signature(root: Path, candidates: list[Path]) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    for path in candidates:
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        signature.append((relative, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def load_project_rules_with_metadata(
    workspace: str, *, max_chars: int = 32_000
) -> tuple[str, list[str], dict[str, Any]]:
    """Load bounded local guidance, re-reading files only when their metadata changes.

    Rule contents are fingerprinted after each actual read. Subsequent tasks use the
    in-process immutable snapshot while file paths, sizes and nanosecond mtimes stay
    unchanged. This keeps rule updates immediate without repeatedly decoding the same
    AGENTS.md for every model turn.
    """
    root = Path(workspace).resolve()
    guard = active_control_file_guard(root)
    if guard is not None:
        # The live repository is model-writable through multiple equivalent
        # capabilities. Only the authenticated startup snapshot may enter the
        # system-role project-guidance block.
        return guard.load_project_rules(max_chars)
    bounded_max = max(0, int(max_chars))
    cache_key = (str(root), bounded_max)
    candidates = _safe_candidates(root)
    signature = _signature(root, candidates)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None and cached.signature == signature:
            _CACHE.move_to_end(cache_key)
            return cached.content, list(cached.files), {
                "cache_hit": True,
                "fingerprint": cached.fingerprint,
            }

    blocks: list[str] = []
    loaded: list[str] = []
    remaining = bounded_max
    digest = hashlib.sha256()
    for resolved in candidates:
        if remaining <= 0:
            break
        try:
            stat = resolved.stat()
            if stat.st_size > 1_000_000:
                continue
            raw = resolved.read_bytes()
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        if b"\x00" in raw[:4096]:
            continue
        content = raw.decode("utf-8", errors="replace").strip()
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
        digest.update(raw)
        digest.update(b"\0")

    entry = _RuleCacheEntry(
        signature=signature,
        content="\n\n".join(blocks),
        files=tuple(loaded),
        fingerprint=digest.hexdigest(),
    )
    with _CACHE_LOCK:
        _CACHE[cache_key] = entry
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > _MAX_CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return entry.content, list(entry.files), {
        "cache_hit": False,
        "fingerprint": entry.fingerprint,
    }


def load_project_rules(workspace: str, *, max_chars: int = 32_000) -> tuple[str, list[str]]:
    """Backward-compatible two-value project-rule loader."""
    content, files, _metadata = load_project_rules_with_metadata(
        workspace, max_chars=max_chars
    )
    return content, files


def clear_project_rules_cache() -> None:
    """Clear the in-process rule cache (primarily useful to deterministic tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()
