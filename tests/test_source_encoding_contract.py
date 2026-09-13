from __future__ import annotations

import unicodedata
from pathlib import Path


def test_backend_python_sources_are_strict_utf8_without_mojibake_markers() -> None:
    root = Path(__file__).resolve().parents[1]
    markers = ("\ufffd", "涓嶈", "鏈", "寮€", "锛", "銆", "馃", "鈥")
    legacy_private_use: set[tuple[str, int]] = set()

    for path in sorted((root / "deepdesk").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        text = path.read_bytes().decode("utf-8", errors="strict")
        for marker in markers:
            assert marker not in text, f"{relative} contains likely mojibake marker {marker!r}"
        for line_number, line in enumerate(text.splitlines(), start=1):
            for character in line:
                if unicodedata.category(character) == "Co":
                    assert (relative, line_number) in legacy_private_use, (
                        f"{relative}:{line_number} contains an unexpected private-use character"
                    )
