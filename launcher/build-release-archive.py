"""Compatibility entry point for the canonical release archive builder.

Keep this hyphenated filename for existing release shortcuts.  All archive
filtering and verification lives in ``launcher.build_release_archive`` so the
public-package privacy boundary cannot drift between two implementations.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _main() -> None:
    if __package__ in {None, ""}:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from launcher.build_release_archive import main

    main()


if __name__ == "__main__":
    _main()

