"""Build-time portable Git installation; never invokes host Git or a package manager."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macos.bundle_cli import fetch, verify

ARCHIVE = "dugite-native-v2.53.0-4098283-macOS-arm64.tar.gz"
VERSION = "2.53.0"
ASSETS = {
    ARCHIVE: (
        "https://github.com/desktop/dugite-native/releases/download/v2.53.0-4/" + ARCHIVE,
        "f9dc64635a5b62fbd7ad95db73268bbb8912255ac516d65d37bf7af22fcb8ffe",
    ),
    "git-COPYING": ("https://raw.githubusercontent.com/git/git/v2.53.0/COPYING",
        "5b2198d1645f767585e8a88ac0499b04472164c0d2da22e75ecf97ef443ab32e"),
    "git-lfs-LICENSE": ("https://raw.githubusercontent.com/git-lfs/git-lfs/v3.7.1/LICENSE.md",
        "4fae9062ab5cdd5fb15486b728534d8aded8b4ae9d84b6d66a956f5162c366b6"),
    "git-credential-manager-LICENSE": (
        "https://raw.githubusercontent.com/git-ecosystem/git-credential-manager/v2.9.0/LICENSE",
        "b47f1a8a744ecdc7a3da35804f88552805d33f51a726b87a2105acdfae406b07"),
}
REQUIRED = (
    "bin/git", "libexec/git-core/git", "libexec/git-core/git-remote-http",
    "libexec/git-core/git-remote-https", "libexec/git-core/git-lfs",
    "libexec/git-core/git-credential-manager", "libexec/git-core/NOTICE",
    "share/git-core/templates/description", "etc/gitconfig",
)
LAUNCHER = '''#!/bin/sh
# Resolve helpers relative to this bundle, not dugite's build-machine prefix.
ELREN_GIT_ROOT=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")/.." && pwd -P) || exit 1
export GIT_EXEC_PATH="$ELREN_GIT_ROOT/libexec/git-core"
export GIT_TEMPLATE_DIR="${GIT_TEMPLATE_DIR:-$ELREN_GIT_ROOT/share/git-core/templates}"
export GIT_CONFIG_SYSTEM="${GIT_CONFIG_SYSTEM:-$ELREN_GIT_ROOT/etc/gitconfig}"
exec "$ELREN_GIT_ROOT/libexec/git-core/git" "$@"
'''


def validate_members(members: list[tarfile.TarInfo], destination: Path) -> None:
    if len(members) > 20000 or sum(m.size for m in members) > 600 * 1024**2:
        raise ValueError("Git archive exceeds size limits")
    seen = set()
    for member in members:
        name = PurePosixPath(member.name)
        if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                or ":" in member.name or name.as_posix() in seen
                or (name.parts and name.parts[0] not in {"bin", "libexec", "share", "etc"})):
            raise ValueError("Unsafe Git archive path")
        if not name.parts and not member.isdir():
            raise ValueError("Invalid Git archive root")
        seen.add(name.as_posix())
        tarfile.data_filter(member, str(destination))


def install(cache: Path, native_root: Path) -> Path:
    target = native_root / "git"
    if target.exists() or target.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing Git runtime")
    for name, (_, digest) in ASSETS.items():
        verify(cache / name, digest)
    native_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".git-stage-", dir=native_root) as temporary:
        staged = Path(temporary) / "git"
        staged.mkdir()
        with tarfile.open(cache / ARCHIVE, "r:gz") as archive:
            members = archive.getmembers()
            validate_members(members, staged)
            archive.extractall(staged, members=members, filter="data")
        for relative in REQUIRED:
            path = (staged / relative).resolve(strict=True)
            path.relative_to(staged.resolve())
            if not path.is_file():
                raise ValueError(f"Missing Git runtime file: {relative}")
        for relative in ("bin/git", "libexec/git-core/git"):
            with (staged / relative).open("rb") as stream:
                if stream.read(8) != b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01":
                    raise ValueError("Expected arm64 Mach-O Git")
        # Unlink first: upstream may hardlink bin/git to libexec/git-core/git.
        launcher = staged / "bin/git"
        launcher.unlink()
        launcher.write_text(LAUNCHER, encoding="utf-8", newline="\n")
        launcher.chmod(0o755)
        licenses = staged / "licenses"
        licenses.mkdir()
        for name in ASSETS:
            if name != ARCHIVE:
                shutil.copyfile(cache / name, licenses / name)
        (staged / "ELREN-UPSTREAM.json").write_text(json.dumps({
            "project": "desktop/dugite-native", "version": VERSION,
            "git_lfs": "3.7.1", "git_credential_manager": "2.9.0",
            "assets": {name: {"url": url, "sha256": sha} for name, (url, sha) in ASSETS.items()},
            "sources": ["https://github.com/desktop/dugite-native/tree/v2.53.0-4",
                        "https://github.com/git/git/tree/v2.53.0",
                        "https://github.com/git-lfs/git-lfs/tree/v3.7.1",
                        "https://github.com/git-ecosystem/git-credential-manager/tree/v2.9.0"],
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
        fetch(args.cache, ASSETS)
    print(install(args.cache, args.native_root))
