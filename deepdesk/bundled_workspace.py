"""Read vendor workspace updates exclusively from the signed frozen Mac app.

No environment variable, user folder, API or model-supplied path selects these
instructions. Custom workspace packages remain the control guard's property.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANIFEST = "workspace-manifest.json"
MAX_FILES = 10_000
MAX_BYTES = 64 * 1024 * 1024


def safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Invalid vendor resource path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(
        p in {".", ".."} or p != p.rstrip(" .") for p in path.parts
    ):
        raise ValueError("Invalid vendor resource path")
    return value


def package_root(relative: str) -> str:
    parts = PurePosixPath(safe_relative(relative)).parts
    if len(parts) < 2 or parts[0] not in {"skills", "plugins"}:
        raise ValueError("Only vendor skills and plugins may be migrated")
    count = 3 if parts[:2] == ("skills", "openclaw-bundled") else 2
    if len(parts) < count:
        raise ValueError("Vendor package root is incomplete")
    return "/".join(parts[:count])


def digest_files(files: dict[str, bytes]) -> str:
    records = sorted((name.casefold(), hashlib.sha256(data).hexdigest()) for name, data in files.items())
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class VendorPackage:
    root: str
    files: dict[str, bytes]
    modes: dict[str, int]
    predecessors: tuple[str, ...]

    @property
    def digest(self) -> str:
        return digest_files(self.files)


@dataclass(frozen=True)
class VendorUpdate:
    manifest_digest: str
    packages: tuple[VendorPackage, ...]


def _read_verified_bundle(bundle: Path, raw: bytes) -> VendorUpdate:
    """Called only after verifying the app signature; never imports package code."""
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("Invalid vendor workspace manifest")
    records = manifest.get("files")
    predecessors = manifest.get("predecessors", {})
    if not isinstance(records, dict) or len(records) > MAX_FILES or not isinstance(predecessors, dict):
        raise ValueError("Invalid vendor workspace manifest entries")
    packages: dict[str, dict[str, bytes]] = {}
    modes: dict[str, dict[str, int]] = {}
    roots: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    for relative, expected in records.items():
        root = package_root(relative)
        if roots.setdefault(root.casefold(), root) != root:
            raise ValueError("Ambiguous vendor package casing")
        canonical = relative.casefold()
        if canonical in seen or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Ambiguous vendor resource or invalid digest")
        seen.add(canonical)
        path = bundle / relative
        # Even a signed package must not use a link to read outside the app.
        for parent in [path, *path.parents]:
            if parent == bundle:
                break
            if parent.is_symlink():
                raise ValueError("Linked vendor resource")
        path.resolve(strict=True).relative_to(bundle.resolve())
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Invalid vendor resource file")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BYTES or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Vendor resource failed integrity verification")
        packages.setdefault(root, {})[relative] = data
        modes.setdefault(root, {})[relative] = 0o755 if path.stat().st_mode & 0o111 else 0o644
    for root, hashes in predecessors.items():
        if package_root(root) != root or not isinstance(hashes, list) or len(hashes) > 100:
            raise ValueError("Invalid vendor predecessor record")
        if not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
            raise ValueError("Invalid vendor predecessor digest")
    return VendorUpdate(hashlib.sha256(raw).hexdigest(), tuple(
        VendorPackage(root, files, modes[root], tuple(predecessors.get(root, [])))
        for root, files in sorted(packages.items())
    ))


def packaged_update(previous_digest: str = "") -> VendorUpdate | None:
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    resources = executable.parent.parent
    if resources.name != "Resources" or resources.parent.name != "Contents":
        raise ValueError("Unexpected frozen macOS application layout")
    app = resources.parent.parent
    bundle = resources / "bundle"
    if bundle.is_symlink():
        raise ValueError("Linked vendor workspace root")
    manifest = bundle / MANIFEST
    if not manifest.exists():
        return None  # Older QA app; not an update source.
    if manifest.is_symlink() or manifest.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Invalid packaged workspace manifest")
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() == previous_digest:
        return None  # Already authenticated in the encrypted startup baseline.
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True, capture_output=True, timeout=120)
    # Signature validation precedes reading any new executable plugin bytes.
    if manifest.read_bytes() != raw:
        raise ValueError("Vendor workspace manifest changed during verification")
    return _read_verified_bundle(bundle, raw)
