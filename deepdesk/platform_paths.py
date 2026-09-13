from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path


def _unique_paths(values: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = os.path.normcase(str(value))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def windows_root() -> Path | None:
    """Return the configured Windows directory without assuming the C: drive."""

    value = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    return Path(value).expanduser() if value else None


def windows_program_files() -> tuple[Path, ...]:
    """Return every configured Program Files root in stable priority order."""

    roots = []
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value).expanduser())
    return _unique_paths(roots)


def unicode_font_candidates() -> tuple[Path, ...]:
    """Return portable Unicode font candidates for the active host platform."""

    candidates: list[Path] = []
    root = windows_root()
    if root is not None:
        fonts = root / "Fonts"
        candidates.extend(
            fonts / name
            for name in ("msyh.ttc", "msyhbd.ttc", "msyh.ttf", "simhei.ttf", "segoeui.ttf")
        )
    if sys.platform == "darwin":
        candidates.extend(
            Path(value)
            for value in (
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            )
        )
    candidates.extend(
        Path(value)
        for value in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    )
    return _unique_paths(candidates)
