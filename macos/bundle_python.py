"""Stage a pinned, relocatable CPython; never copy the builder's framework."""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath

VERSION = "3.12.14"
URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-apple-darwin-install_only.tar.gz"
SHA256 = "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"


def install(archive: Path, native_root: Path) -> Path:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SHA256:
        raise ValueError("Portable Python archive failed SHA-256 verification")
    native_root = native_root.resolve()
    target = native_root / "python"
    if target.exists() or target.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing Python runtime")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "python":
                raise ValueError("Portable Python archive path escapes its root")
            # Preflight all links and special files before writing any payload.
            tarfile.data_filter(member, str(native_root))
        native_root.mkdir(parents=True, exist_ok=True)
        source.extractall(native_root, members=members, filter="data")
    for relative in ("bin/python3.12", "lib/libpython3.12.dylib", "lib/python3.12/LICENSE.txt"):
        if not (target / relative).is_file():
            raise ValueError(f"Portable Python is incomplete: {relative}")
    (target / "ELREN-UPSTREAM.json").write_text(json.dumps({
        "project": "astral-sh/python-build-standalone", "version": VERSION,
        "url": URL, "sha256": SHA256,
    }, indent=2) + "\n", encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("native_root", type=Path)
    args = parser.parse_args()
    print(install(args.archive, args.native_root))
