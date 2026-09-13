#!/usr/bin/env python3
"""Verify Elren's Docker-free portable tool payload.

The verifier intentionally does not make network requests. It validates the checked-in
lock contract, payload shape, exact Node package versions/integrities, license presence,
size budget, shim allowlist, and optionally executes low-cost version smoke tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = PROJECT_ROOT / "launcher" / "tool-runtime.lock.json"
sys.path.insert(0, str(PROJECT_ROOT))
from deepdesk.audiveris_tessdata import validate_tessdata


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def package_path(node_root: Path, package_name: str) -> Path:
    return node_root / "node_modules" / Path(*package_name.split("/"))


def run_smoke(command: list[str], *, contains: str, timeout: int = 20, expected_exit: int = 0) -> str:
    invocation: str | list[str] = command
    if os.name == "nt" and Path(command[0]).name.casefold() in {"cmd", "cmd.exe"} and "/c" in command:
        index = command.index("/c")
        # CMD /S /C strips one outer quote pair. Without it a shim path with
        # spaces and a quoted argument is split at the first path space.
        prefix = subprocess.list2cmdline([*command[:index], "/s", "/c"])
        arguments = subprocess.list2cmdline(command[index + 1:])
        invocation = f'{prefix} "{arguments}"'
    completed = subprocess.run(
        invocation,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != expected_exit:
        raise RuntimeError(f"Smoke command failed ({completed.returncode}): {command!r}\n{output}")
    if contains.casefold() not in output.casefold():
        raise RuntimeError(f"Smoke output for {command!r} did not contain {contains!r}:\n{output}")
    return output.splitlines()[0] if output else "ok"


def verify(root: Path, profile: str, *, smoke: bool) -> list[str]:
    errors: list[str] = []
    lock = load_json(LOCK_PATH)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return [f"Missing manifest: {manifest_path}"]
    manifest = load_json(manifest_path)

    expected_ids = {
        item for item in lock["profiles"][profile] if not item.startswith("node-cli-")
    }
    actual_ids = {item.get("id") for item in manifest.get("components", [])}
    if actual_ids != expected_ids:
        errors.append(f"Component mismatch: expected {sorted(expected_ids)}, got {sorted(actual_ids)}")
    if manifest.get("profile") != profile:
        errors.append(f"Manifest profile is {manifest.get('profile')!r}, expected {profile!r}")
    if manifest.get("product_version") != "1.0":
        errors.append("Tool runtime product version must remain 1.0")
    if manifest.get("docker_required") is not False:
        errors.append("Portable tool runtime must not require Docker")
    if manifest.get("lock_sha256") != sha256(LOCK_PATH):
        errors.append("Manifest was not built from the current tool-runtime lock")

    duplicate_runtimes = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name.casefold() in {"python.exe", "node.exe"}
        and not (
            path.name.casefold() == "python.exe"
            and path.relative_to(root).parts[:3] == (
                "native",
                "lilypond",
                "lilypond-2.26.0",
            )
        )
    ]
    if duplicate_runtimes:
        errors.append(f"Payload duplicates Python/Node: {duplicate_runtimes}")

    allowlist = {f"{name}.cmd".casefold() for name in lock["shim_allowlist"]}
    shims = list((root / "bin").glob("*.cmd")) if (root / "bin").is_dir() else []
    unexpected_shims = [shim.name for shim in shims if shim.name.casefold() not in allowlist]
    if unexpected_shims:
        errors.append(f"Non-allowlisted command shims: {unexpected_shims}")
    manifest_shims = sorted(manifest.get("shims", []), key=str.casefold)
    disk_shims = sorted((shim.name for shim in shims), key=str.casefold)
    if manifest_shims != disk_shims:
        errors.append("Manifest shim inventory differs from files on disk")
    manifest_tools = manifest.get("tools", [])
    if not isinstance(manifest_tools, list):
        errors.append("Manifest tools inventory must be a list")
        manifest_tools = []
    tool_ids: set[str] = set()
    tool_executables: list[str] = []
    for item in manifest_tools:
        if not isinstance(item, dict):
            errors.append("Manifest tool entries must be objects")
            continue
        tool_id = str(item.get("id") or "").casefold()
        executable = str(item.get("executable") or "")
        if not tool_id or tool_id in tool_ids:
            errors.append("Manifest tool IDs must be non-empty and unique")
            continue
        tool_ids.add(tool_id)
        relative = Path(executable)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"Manifest tool path escapes the payload: {executable}")
            continue
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            errors.append(f"Manifest tool path escapes the payload: {executable}")
            continue
        if not resolved.is_file():
            # Missing optional commands degrade individually at runtime.
            continue
        expected_hash = str(item.get("sha256") or "").casefold()
        if expected_hash and sha256(resolved) != expected_hash:
            errors.append(f"Manifest tool hash mismatch: {tool_id}")
        tool_executables.append(relative.as_posix().casefold())
    declared_runtime_executables = {
        "native/audiveris/runtime/bin/java.exe"
        for item in manifest_tools
        if isinstance(item, dict)
        and item.get("id") == "audiveris"
        and item.get("execution_mode") == "docker-lite-isolated-subprocess"
    }
    expected_tool_executables = sorted(
        [
            *(f"bin/{shim.name}".casefold() for shim in shims),
            *declared_runtime_executables,
        ],
        key=str.casefold,
    )
    if sorted(tool_executables, key=str.casefold) != expected_tool_executables:
        errors.append("Manifest tool inventory differs from executable shims on disk")

    required_licenses = {
        "jq": "jq-LICENSE",
        "ripgrep": "ripgrep-COPYING",
        "github-cli": "github-cli-LICENSE",
        "ffmpeg-lgpl": "ffmpeg-LICENSE.txt",
        "xurl-binary": "xurl-LICENSE",
        "pandoc": "pandoc-COPYING.md",
        "audiveris": "audiveris-AGPL-3.0.txt",
    }
    for component_id in expected_ids:
        license_name = required_licenses.get(component_id)
        if license_name and not (root / "licenses" / license_name).is_file():
            errors.append(f"Missing license notice for {component_id}: {license_name}")

    if "audiveris" in expected_ids:
        try:
            validate_tessdata(root / "native" / "audiveris")
        except (OSError, ValueError) as exc:
            errors.append(f"Audiveris offline OCR data failed verification: {exc}")
        integrity_path = root / "native" / "audiveris" / "elren-integrity.json"
        model_path = root / "native" / "audiveris" / "model" / "basic-classifier.zip"
        if not integrity_path.is_file():
            errors.append("Missing Audiveris integrity inventory")
        elif not model_path.is_file():
            errors.append("Missing package-local Audiveris classifier model")
        else:
            integrity = load_json(integrity_path)
            if integrity.get("version") != "5.11.0" or integrity.get("license") != "AGPL-3.0-only":
                errors.append("Audiveris integrity metadata does not match the reviewed release")
            runtime_root = integrity_path.parent.resolve()
            for entry in integrity.get("files", []):
                if not isinstance(entry, dict):
                    errors.append("Audiveris integrity entries must be objects")
                    continue
                relative = Path(str(entry.get("path") or ""))
                candidate = (runtime_root / relative).resolve()
                try:
                    candidate.relative_to(runtime_root)
                except ValueError:
                    errors.append(f"Audiveris integrity path escapes its runtime: {relative}")
                    continue
                if not candidate.is_file() or sha256(candidate) != str(entry.get("sha256") or ""):
                    errors.append(f"Audiveris integrity mismatch: {relative.as_posix()}")
                    break

    node_lock_path = root / "node" / "package-lock.json"
    if not node_lock_path.is_file():
        errors.append("Missing exact npm package-lock.json")
    else:
        package_lock = load_json(node_lock_path)
        wanted: dict[str, Any] = dict(lock["node"]["default_packages"])
        if profile == "extended":
            wanted.update(lock["node"]["extended_packages"])
        lock_packages = package_lock.get("packages", {})
        for name, metadata in wanted.items():
            installed_path = package_path(root / "node", name)
            installed_manifest = installed_path / "package.json"
            if not installed_manifest.is_file():
                errors.append(f"Missing Node package: {name}")
                continue
            installed = load_json(installed_manifest)
            if installed.get("version") != metadata["version"]:
                errors.append(
                    f"Node package {name} version {installed.get('version')} != {metadata['version']}"
                )
            package_lock_key = "node_modules/" + name
            locked = lock_packages.get(package_lock_key, {})
            if locked.get("integrity") != metadata["integrity"]:
                errors.append(f"Node package integrity mismatch in package-lock: {name}")

    total_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    if profile == "default" and total_bytes > int(lock["policy"]["default_max_extracted_bytes"]):
        errors.append(f"Default payload is too large: {total_bytes} bytes")

    credential_pattern = re.compile(
        rb"(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})"
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            continue
        if path.suffix.casefold() not in {".json", ".txt", ".md", ".cmd", ".js", ".mjs"}:
            continue
        if credential_pattern.search(path.read_bytes()):
            errors.append(f"Credential-like value found in portable payload: {path}")

    if smoke and not errors:
        try:
            declared_versions = {
                str(tool.get("id")): str(tool.get("version"))
                for tool in manifest.get("tools", [])
                if isinstance(tool, dict)
            }
            ffmpeg_build = declared_versions.get("ffmpeg", "").split("+", 1)[0]
            checks = [
                (root / "bin" / "jq.cmd", ["--version"], "jq-1.8.2"),
                (root / "bin" / "rg.cmd", ["--version"], "ripgrep 15.2.0"),
                (root / "bin" / "gh.cmd", ["--version"], "gh version 2.97.0"),
                (root / "bin" / "ffmpeg.cmd", ["-version"], f"ffmpeg version n{ffmpeg_build}"),
                (root / "bin" / "ffprobe.cmd", ["-version"], f"ffprobe version n{ffmpeg_build}"),
                (
                    root / "bin" / "python3.cmd",
                    ["--version"],
                    f"Python {declared_versions.get('python3', '3.')}",
                ),
                (root / "bin" / "curl.cmd", ["--version"], "curl "),
            ]
            for shim, args, marker in checks:
                if not shim.is_file():
                    errors.append(f"Missing smoke-test shim: {shim.name}")
                    continue
                run_smoke(["cmd.exe", "/d", "/c", str(shim), *args], contains=marker)

            python_shim = root / "bin" / "python3.cmd"
            # A successful version probe cannot prove failure propagation.
            # These deliberate failures require no network or external state.
            run_smoke(
                ["cmd.exe", "/d", "/c", str(python_shim), "-c",
                 "import sys; print('elren-expected-failure'); sys.exit(7)"],
                contains="elren-expected-failure", expected_exit=7,
            )
            run_smoke(
                ["cmd.exe", "/d", "/c", str(root / "bin" / "curl.cmd"),
                 "--elren-intentionally-invalid-option"],
                contains="--elren-intentionally-invalid-option", expected_exit=2,
            )
            run_smoke(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    str(python_shim),
                    "-c",
                    "import debugpy, fastapi; print('elren-python-dependencies-ok')",
                ],
                contains="elren-python-dependencies-ok",
            )

            python_shim = root / "bin" / "python3.cmd"
            if python_shim.is_file():
                run_smoke(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        str(python_shim),
                        "-c",
                        "import debugpy; print(debugpy.__version__)",
                    ],
                    contains="1.8.21",
                )

            node_version_shims = {
                "mcporter.cmd": "0.13.4",
                "xurl.cmd": "1.3.1",
            }
            for name, marker in node_version_shims.items():
                shim = root / "bin" / name
                if not shim.is_file():
                    errors.append(f"Missing default Node CLI shim: {name}")
                    continue
                run_smoke(["cmd.exe", "/d", "/c", str(shim), "--version"], contains=marker)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "work" / "tool-runtime")
    parser.add_argument("--profile", choices=("default", "extended"), default="default")
    parser.add_argument("--no-smoke", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    errors = verify(root, args.profile, smoke=not args.no_smoke)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    total_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    print(f"Verified {root} ({total_bytes / 1024 / 1024:.1f} MiB, {args.profile})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
