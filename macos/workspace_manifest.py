"""Build a signed inventory for safe per-package workspace upgrades."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepdesk.bundled_workspace import MANIFEST, MAX_BYTES, MAX_FILES, digest_files, package_root

SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".gradle", ".kotlin", ".pnpm"}


def inventory(bundle: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    for name in ("skills", "plugins"):
        root = bundle / name
        if root.is_symlink():
            raise ValueError("Vendor workspace root is a link")
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Vendor workspace contains links")
            if not path.is_file() or any(part in SKIP for part in path.relative_to(root).parts):
                continue
            if path.suffix == ".pyc" or path.name == "local.properties":
                continue
            relative = path.relative_to(bundle).as_posix()
            package_root(relative)
            if relative.casefold() in seen or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Invalid or ambiguous vendor resource")
            seen.add(relative.casefold())
            files[relative] = path.read_bytes()
            total += len(files[relative])
            if len(files) > MAX_FILES or total > MAX_BYTES:
                raise ValueError("Vendor resources exceed control guard limits")
    return files


def package_digests(bundle: Path) -> dict[str, str]:
    groups: dict[str, dict[str, bytes]] = {}
    for relative, data in inventory(bundle).items():
        groups.setdefault(package_root(relative), {})[relative] = data
    return {root: digest_files(files) for root, files in sorted(groups.items())}


def write_manifest(bundle: Path, history: Path) -> None:
    predecessors = json.loads(history.read_text("utf-8"))
    if not isinstance(predecessors, dict):
        raise ValueError("Invalid reviewed vendor history")
    files = inventory(bundle)
    (bundle / MANIFEST).write_text(json.dumps({"schema": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "predecessors": predecessors,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("history", type=Path)
    args = parser.parse_args()
    write_manifest(args.bundle, args.history)
