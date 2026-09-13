"""Fail packaging when required non-Python runtime resources are missing/stale."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

REQUIRED = (
    "subagent_catalog.json",
    "audiveris_tessdata.lock.json",
    "java/audiveris-bootstrap.jar",
    "static/index.html",
    "static/app.js",
    "static/voice.js",
    "static/editorial-ui.css",
)


def verify(backend: Path, source: Path) -> None:
    for relative in REQUIRED:
        original = source / "deepdesk" / relative
        bundled = backend / "_internal/deepdesk" / relative
        if not bundled.is_file() or bundled.is_symlink():
            raise ValueError(f"Missing frozen runtime resource: {relative}")
        if hashlib.sha256(original.read_bytes()).digest() != hashlib.sha256(bundled.read_bytes()).digest():
            raise ValueError(f"Stale frozen runtime resource: {relative}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    verify(args.backend, args.source)
    print(f"Verified {len(REQUIRED)} frozen runtime resources against source")
