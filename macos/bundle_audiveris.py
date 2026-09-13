"""Offline, fail-closed staging of the reviewed macOS Audiveris runtime.

The caller supplies an already reviewed local app; this module never downloads
or executes its code. Platform execution/signing still needs a real Mac.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepdesk.audiveris_tessdata import (
    DEFAULT_SOURCE,
    install_tessdata,
    validate_directory,
    validate_tessdata,
)

VERSION = "5.11.0"
MODEL_SHA256 = "327194c47116a8ebf5771d04cea08a4e563ec93d2955e1efd990a54a9245f29e"
LICENSE_SHA256 = "76a97c878c9c7a8321bb395c2b44d3fe2f8d81314d219b20138ed0e2dddd5182"
MODEL_ENTRY = "res/basic-classifier.zip"
MANIFEST = "elren-integrity.json"
RELATIVE_HOME = Path("work/tool-runtime/native/audiveris")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_tree(root: Path) -> None:
    """Allow internal file symlinks (JRE legal notices), never directory links."""
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError(f"Refusing a linked source/destination root: {root}")
    canonical = root.resolve(strict=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            linked = path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            )
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(canonical):
                raise ValueError(f"Filesystem link escapes the reviewed source: {path}")
            if linked and not resolved.is_file():
                raise ValueError(f"Directory links are not supported: {path}")
            if not path.is_dir() and not path.is_file():
                raise ValueError(f"Unsupported filesystem entry: {path}")


def arm64_macho(data: bytes) -> bool:
    if len(data) < 8:
        return False
    magic = data[:4]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        endian = "<" if magic[0] == 0xCF else ">"
        return struct.unpack(endian + "I", data[4:8])[0] == 0x0100000C
    if magic not in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        return False
    count = struct.unpack(">I", data[4:8])[0]
    stride = 32 if magic[-1] == 0xBF else 20
    if not 0 < count <= 16 or len(data) < 8 + count * stride:
        return False
    return any(
        struct.unpack(">I", data[8 + i * stride:12 + i * stride])[0] == 0x0100000C
        for i in range(count)
    )


def _native_file(path: Path) -> None:
    with path.open("rb") as stream:
        valid = arm64_macho(stream.read(1024))
    if not valid:
        raise ValueError(f"Expected a macOS arm64 Mach-O runtime: {path}")


def inspect_source(source: Path, license_path: Path) -> dict:
    check_tree(source)
    app = source / "app"
    runtime = source / "runtime"
    if (runtime / "Contents/Home/bin/java").is_file():
        runtime = runtime / "Contents/Home"
    java = runtime / "bin/java"
    if not java.is_file() or not (app / "audiveris.jar").is_file():
        raise ValueError("Audiveris app/audiveris.jar and its private Java runtime are required")
    if os.name != "nt" and not os.access(java, os.X_OK):
        raise ValueError("Bundled Java is not executable")
    _native_file(java)
    _native_file(runtime / "lib/server/libjvm.dylib")
    if not (runtime / "lib/modules").is_file() or not (runtime / "lib/modules").stat().st_size:
        raise ValueError("Java module image is missing or empty")
    release = (runtime / "release").read_text("utf-8")
    java_match = re.search(r'^JAVA_VERSION="(\d+)[^"\r\n]*"$', release, re.MULTILINE)
    if java_match is None:
        raise ValueError("Java release metadata is missing")
    legal = runtime / "legal/java.base/LICENSE"
    if not legal.is_file() or not legal.stat().st_size:
        raise ValueError("The private Java runtime license is missing")
    config = (app / "Audiveris.cfg").read_text("utf-8")
    if not re.search(r"jpackage\.app-version=" + re.escape(VERSION) + r"\s*$", config, re.MULTILINE):
        raise ValueError(f"Expected reviewed Audiveris {VERSION} configuration")
    with zipfile.ZipFile(app / "audiveris.jar") as jar:
        manifest = jar.read("META-INF/MANIFEST.MF").decode("utf-8").replace("\r", "")
        if f"Implementation-Version: {VERSION}" not in manifest.splitlines():
            raise ValueError(f"Audiveris jar is not version {VERSION}")
        main = jar.read("Audiveris.class")
        if len(main) < 8 or main[:4] != b"\xca\xfe\xba\xbe":
            raise ValueError("Audiveris main class is invalid")
        if int(java_match[1]) < struct.unpack(">H", main[6:8])[0] - 44:
            raise ValueError("Bundled Java is too old for this Audiveris jar")
        info = jar.getinfo(MODEL_ENTRY)
        if not 0 < info.file_size <= 64 * 1024 * 1024:
            raise ValueError("Classifier model has an invalid size")
        model = jar.read(info)
    if hashlib.sha256(model).hexdigest() != MODEL_SHA256:
        raise ValueError("Audiveris classifier model failed reviewed SHA-256 verification")
    with zipfile.ZipFile(io.BytesIO(model)) as archive:
        if not {"model.xml", "means.xml", "stds.xml"}.issubset(archive.namelist()):
            raise ValueError("Classifier model entries are missing")
        if archive.testzip() is not None:
            raise ValueError("Classifier model has a CRC error")
    for component in ("leptonica", "tesseract"):
        natives = list(app.glob(f"{component}-*-macosx-arm64.jar"))
        if not natives:
            raise ValueError(f"Missing macOS arm64 {component} native dependency")
        for native in natives:
            with zipfile.ZipFile(native) as archive:
                libraries = [item for item in archive.infolist() if item.filename.endswith(".dylib")]
                if not libraries or archive.testzip() is not None:
                    raise ValueError(f"Invalid native dependency: {native.name}")
                for library in libraries:
                    with archive.open(library) as stream:
                        if not arm64_macho(stream.read(1024)):
                            raise ValueError(f"Wrong native architecture in {native.name}")
    if not license_path.is_file() or digest(license_path) != LICENSE_SHA256:
        raise ValueError("Reviewed Audiveris license failed SHA-256 verification")
    return {"app": app, "runtime": runtime, "model": model, "java_version": java_match[0]}


def write_manifest(target: Path, java_version: str) -> None:
    manifest_path = target / MANIFEST
    if manifest_path.is_symlink() or (
        hasattr(manifest_path, "is_junction") and manifest_path.is_junction()
    ):
        raise ValueError("Refusing a linked integrity manifest")
    files = [
        {"path": path.relative_to(target).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)}
        for path in sorted(target.rglob("*"))
        if path.is_file() and path != manifest_path
    ]
    payload = {
        "schema": 1, "component": "elren-docker-lite-audiveris", "version": VERSION,
        "source": "https://github.com/Audiveris/audiveris", "license": "AGPL-3.0-only",
        "platform": "darwin-arm64", "java_release": java_version,
        "cloud_upload": False, "model": "model/basic-classifier.zip", "files": files,
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")


def verify(target: Path, *, refresh_manifest: bool = False) -> None:
    validate_tessdata(target)
    inspected = inspect_source(target, target / "licenses/audiveris-AGPL-3.0.txt")
    if (target / "model/basic-classifier.zip").read_bytes() != inspected["model"]:
        raise ValueError("Extracted classifier does not match the reviewed jar model")
    if refresh_manifest:
        write_manifest(target, inspected["java_version"])
    saved = json.loads((target / MANIFEST).read_text("utf-8"))
    actual = {
        path.relative_to(target).as_posix(): (path.stat().st_size, digest(path))
        for path in target.rglob("*") if path.is_file() and path != target / MANIFEST
    }
    recorded = {item["path"]: (item["bytes"], item["sha256"]) for item in saved["files"]}
    expected = {
        "schema": 1, "version": VERSION, "component": "elren-docker-lite-audiveris",
        "platform": "darwin-arm64", "model": "model/basic-classifier.zip",
        "license": "AGPL-3.0-only", "cloud_upload": False,
    }
    if any(saved.get(key) != value for key, value in expected.items()) or recorded != actual or len(recorded) != len(saved["files"]):
        raise ValueError("Audiveris installed file inventory failed integrity verification")


def install(source: Path, workspace: Path, license_path: Path, *, tessdata_source: Path = DEFAULT_SOURCE) -> Path:
    inspected = inspect_source(source, license_path)
    validate_directory(tessdata_source)
    # Canonicalize the caller-selected workspace, but never follow an existing
    # redirect anywhere below it or replace an existing runtime.
    workspace = workspace.resolve(strict=True)
    target = workspace / RELATIVE_HOME
    cursor = workspace
    for part in RELATIVE_HOME.parts:
        cursor /= part
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise ValueError(f"Refusing a redirected installation path: {cursor}")
    if target.exists():
        raise ValueError(f"Destination already exists; it will not be overwritten: {target}")
    if target.is_relative_to(source.resolve()) or source.resolve().is_relative_to(target):
        raise ValueError("Audiveris source and destination must not overlap")
    target.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory cleanup is limited to this newly created staging tree.
    with tempfile.TemporaryDirectory(prefix=".audiveris-staging-", dir=target.parent) as staging:
        staged = Path(staging) / "audiveris"
        staged.mkdir()
        shutil.copytree(inspected["app"], staged / "app", symlinks=False)
        shutil.copytree(inspected["runtime"], staged / "runtime", symlinks=False)
        (staged / "model").mkdir()
        (staged / "model/basic-classifier.zip").write_bytes(inspected["model"])
        (staged / "licenses").mkdir()
        shutil.copy2(license_path, staged / "licenses/audiveris-AGPL-3.0.txt")
        install_tessdata(tessdata_source, staged)
        verify(staged, refresh_manifest=True)
        # An existing directory is never merged into or replaced.
        if target.exists() or target.is_symlink():
            raise ValueError("Destination appeared during staging; refusing overwrite")
        staged.rename(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--license", type=Path, default=Path(__file__).resolve().parents[1] / "launcher/audiveris-AGPL-3.0-LICENSE.txt")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--tessdata-source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--forbid-root", type=Path)
    parser.add_argument("--verify-installed", action="store_true")
    parser.add_argument("--refresh-manifest", action="store_true")
    args = parser.parse_args()
    if args.forbid_root and args.source.resolve().is_relative_to(args.forbid_root.resolve()):
        parser.error("Audiveris source must be outside the disposable build directory")
    if args.refresh_manifest and not args.verify_installed:
        parser.error("--refresh-manifest requires --verify-installed after code signing")
    try:
        if args.verify_installed:
            verify(args.source, refresh_manifest=args.refresh_manifest)
        elif args.check_only:
            inspect_source(args.source, args.license)
            validate_directory(args.tessdata_source)
        else:
            print(install(args.source, args.workspace, args.license, tessdata_source=args.tessdata_source))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(2, f"Audiveris packaging failed: {error}\n")


if __name__ == "__main__":
    main()
