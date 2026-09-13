"""Pinned, offline Audiveris text OCR data; no business imports or downloads.

The public runtime interface is validate_tessdata(audiveris_home) -> Path.
All three legacy-compatible languages and their provenance must validate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

LOCK_PATH = Path(__file__).with_name("audiveris_tessdata.lock.json")
RELATIVE_DATA = Path("work/tool-runtime/native/audiveris/tessdata")
DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / RELATIVE_DATA
LANGUAGES = ("eng", "chi_sim", "chi_tra")


def _unlinked(path: Path) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"Redirected tessdata path: {path}")


def validate_directory(path: Path, *, require_lock: bool = True) -> Path:
    """Check a flat local data source against the code-owned pinned lock."""
    path = Path(path)
    _unlinked(path)
    if not path.is_dir():
        raise ValueError(f"Missing offline tessdata directory: {path}")
    lock = json.loads(LOCK_PATH.read_text("utf-8"))
    for name, entry in lock["files"].items():
        candidate = path / name
        _unlinked(candidate)
        if not candidate.is_file() or candidate.stat().st_size != entry["bytes"]:
            raise ValueError(f"Missing or incorrect tessdata size: {name}")
        with candidate.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"Tessdata SHA-256 mismatch: {name}")
    if require_lock:
        saved = path / LOCK_PATH.name
        _unlinked(saved)
        if not saved.is_file() or saved.read_bytes() != LOCK_PATH.read_bytes():
            raise ValueError("Tessdata provenance lock is missing or differs")
        expected = {*lock["files"], LOCK_PATH.name}
        if {p.name for p in path.iterdir()} != expected:
            raise ValueError("Unexpected files in pinned tessdata directory")
    return path.resolve()


def validate_tessdata(audiveris_home: Path) -> Path:
    """Fail closed instead of inheriting host OCR data or downloading models."""
    home = Path(audiveris_home)
    _unlinked(home)
    return validate_directory(home / "tessdata")


def install_tessdata(source: Path, audiveris_home: Path) -> Path:
    """Stage checked data, then add only a new tessdata directory; never replace."""
    source = validate_directory(source, require_lock=False)
    home = Path(audiveris_home)
    for part in (home, *home.parents):
        _unlinked(part)
    home.mkdir(parents=True, exist_ok=True)
    destination = home / "tessdata"
    _unlinked(destination)
    if destination.exists():
        return validate_tessdata(home)
    with tempfile.TemporaryDirectory(prefix=".tessdata-stage-", dir=home) as temporary:
        staged = Path(temporary) / "tessdata"
        staged.mkdir()
        lock = json.loads(LOCK_PATH.read_text("utf-8"))
        for name in lock["files"]:
            shutil.copyfile(source / name, staged / name)
        shutil.copyfile(LOCK_PATH, staged / LOCK_PATH.name)
        validate_directory(staged)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Tessdata destination appeared during staging")
        staged.rename(destination)
    return validate_tessdata(home)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--install-to", type=Path)
    args = parser.parse_args()
    try:
        result = (install_tessdata(args.source, args.install_to) if args.install_to
                  else validate_directory(args.source))
    except (OSError, ValueError) as error:
        parser.exit(2, f"Offline tessdata verification failed: {error}\n")
    print(result)


if __name__ == "__main__":
    main()
