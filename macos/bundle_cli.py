"""Bundle reviewed upstream arm64 CLI assets without host package managers.

Runs only at build time. Downloads are pinned by SHA-256; licenses travel with
the commands. The app signs these Mach-O files and hashes them after signing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ASSETS = {
    "jq-macos-arm64": ("https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-macos-arm64",
        "2d75340ba57a4b4b4c8708a21c2dc8e958a48aaa8bba13b27f77f6e4c0eca07e"),
    "jq-1.8.2.tar.gz": ("https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-1.8.2.tar.gz",
        "71b8d6e8f5fe81f6c6d0d110e3892251f6ce76ed095abd315e26e6e1193af3af"),
    "ripgrep-15.2.0-aarch64-apple-darwin.tar.gz": (
        "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-aarch64-apple-darwin.tar.gz",
        "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4"),
    "gh_2.97.0_macOS_arm64.zip": ("https://github.com/cli/cli/releases/download/v2.97.0/gh_2.97.0_macOS_arm64.zip",
        "a58b8fd77b417a38f47a0b54d1370c59b0fcdb324ccc9ca002b0998f7c4c999e"),
}
VERSIONS = {"jq": "1.8.2", "rg": "15.2.0", "gh": "2.97.0"}


def verify(path: Path, digest: str) -> None:
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError(f"SHA-256 mismatch: {path.name}")


def fetch(cache: Path, assets: dict | None = None) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in (ASSETS if assets is None else assets).items():
        destination = cache / name
        if destination.exists():
            verify(destination, digest)
            continue
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".download", delete=False) as stream:
            partial = Path(stream.name)
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Elren-build"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    shutil.copyfileobj(response, stream)
            except BaseException:
                stream.close()
                partial.unlink(missing_ok=True)
                raise
        try:
            verify(partial, digest)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)


def safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive member: {name}")


def member_bytes(archive: Path, suffix: str) -> bytes:
    """Read one regular file, never extract archive paths/links to the filesystem."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            for entry in entries:
                safe_member(entry.filename)
            matches = [e for e in entries if e.filename.endswith('/' + suffix) or e.filename == suffix]
            if len(matches) != 1 or matches[0].is_dir() or (matches[0].external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Missing, ambiguous or non-regular member: {suffix}")
            if matches[0].file_size > 100_000_000:
                raise ValueError("Archive member too large")
            return source.read(matches[0])
    with tarfile.open(archive, "r:gz") as source:
        entries = source.getmembers()
        for entry in entries:
            safe_member(entry.name)
        matches = [e for e in entries if e.name.endswith('/' + suffix) or e.name == suffix]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError(f"Missing, ambiguous or non-regular member: {suffix}")
        if matches[0].size > 100_000_000:
            raise ValueError("Archive member too large")
        stream = source.extractfile(matches[0])
        assert stream is not None
        return stream.read()


def install(cache: Path, native_root: Path) -> Path:
    target = native_root / "cli"
    if target.exists() or target.is_symlink():
        raise FileExistsError("Refusing to overwrite existing CLI runtime")
    for name, (_, digest) in ASSETS.items():
        verify(cache / name, digest)
    jq_source = cache / "jq-1.8.2.tar.gz"
    rg_source = cache / "ripgrep-15.2.0-aarch64-apple-darwin.tar.gz"
    gh_source = cache / "gh_2.97.0_macOS_arm64.zip"
    payload = {
        "bin/jq": (cache / "jq-macos-arm64").read_bytes(),
        "bin/rg": member_bytes(rg_source, "rg"),
        "bin/gh": member_bytes(gh_source, "bin/gh"),
        "licenses/jq/COPYING": member_bytes(jq_source, "jq-1.8.2/COPYING"),
        "licenses/ripgrep/COPYING": member_bytes(rg_source, "COPYING"),
        "licenses/ripgrep/LICENSE-MIT": member_bytes(rg_source, "LICENSE-MIT"),
        "licenses/ripgrep/UNLICENSE": member_bytes(rg_source, "UNLICENSE"),
        "licenses/gh/LICENSE": member_bytes(gh_source, "LICENSE"),
    }
    # Reject a wrong-architecture download before emitting a runtime. Upstream
    # assets here are thin little-endian Mach-O 64-bit executables, not scripts.
    for relative, data in payload.items():
        if relative.startswith("bin/") and (data[:4] != b"\xcf\xfa\xed\xfe" or data[4:8] != b"\x0c\x00\x00\x01"):
            raise ValueError(f"Expected native arm64 Mach-O: {relative}")
    native_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cli-stage-", dir=native_root) as staging:
        staged = Path(staging) / "cli"
        for relative, data in payload.items():
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if relative.startswith("bin/") else 0o644)
        (staged / "UPSTREAM.json").write_text(json.dumps({
            "platform": "darwin-arm64", "versions": VERSIONS,
            "assets": {name: {"url": url, "sha256": digest} for name, (url, digest) in ASSETS.items()},
        }, indent=2) + "\n", encoding="utf-8")
        staged.rename(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("native_root", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if not args.offline:
        fetch(args.cache)
    print(install(args.cache, args.native_root))
