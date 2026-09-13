from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.tool_runtime import (
    ToolRuntimeManifestError,
    discover_tool_runtime,
    tool_runtime_path_entries,
)


def write_runtime(workspace: Path, *, tools: list[dict] | None = None) -> Path:
    root = workspace / "work/tool-runtime"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin/demo.cmd").write_text("@echo off\r\necho demo\r\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "bundle": "elren-tool-runtime",
                "product_version": "1.0",
                "bin_directories": ["bin"],
                "tools": tools
                or [
                    {
                        "id": "demo",
                        "version": "1.0",
                        "executable": "bin/demo.cmd",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_tool_runtime_is_package_first_without_docker(tmp_path: Path) -> None:
    root = write_runtime(tmp_path)

    runtime = discover_tool_runtime(tmp_path)

    assert runtime is not None
    assert runtime.path_entries == (str(root / "bin"),)
    assert runtime.status()["ready"] is True
    assert runtime.status()["available_tool_count"] == 1
    environment = runtime.environment({"PATH": "C:\\Windows"})
    assert environment["PATH"].split(os.pathsep)[0] == str(root / "bin")
    assert environment["ELREN_TOOL_RUNTIME"] == str(root)


def test_tool_runtime_rejects_escape_and_hash_mismatch(tmp_path: Path) -> None:
    write_runtime(
        tmp_path,
        tools=[{"id": "escape", "executable": "../outside.exe"}],
    )
    with pytest.raises(ToolRuntimeManifestError, match="relative"):
        discover_tool_runtime(tmp_path)

    root = write_runtime(
        tmp_path,
        tools=[{"id": "demo", "executable": "bin/demo.cmd", "sha256": "0" * 64}],
    )
    assert root.is_dir()
    with pytest.raises(ToolRuntimeManifestError, match="SHA-256"):
        discover_tool_runtime(tmp_path, verify_hashes=True)


def test_tool_runtime_accepts_verified_hash(tmp_path: Path) -> None:
    root = write_runtime(tmp_path)
    executable = root / "bin/demo.cmd"
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    payload["tools"][0]["sha256"] = digest
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    runtime = discover_tool_runtime(tmp_path, verify_hashes=True)

    assert runtime is not None
    assert runtime.tools[0]["present"] is True


def test_normal_discovery_does_not_rehash_large_runtime_payloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = write_runtime(tmp_path)
    payload = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    payload["tools"][0]["sha256"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    def fail_if_hashed(_path: Path) -> str:
        raise AssertionError("routine discovery must not hash packaged binaries")

    monkeypatch.setattr("deepdesk.tool_runtime._sha256", fail_if_hashed)

    runtime = discover_tool_runtime(tmp_path)
    assert runtime is not None
    assert runtime.tools[0]["present"] is True


def test_credential_safe_environment_uses_explicit_package_root(monkeypatch, tmp_path: Path) -> None:
    root = write_runtime(tmp_path)
    monkeypatch.setenv("ELREN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    environment = credential_safe_environment()

    assert environment["PATH"].split(os.pathsep)[0] == str(root / "bin")
    assert "OPENAI_API_KEY" not in environment


def test_tool_runtime_default_root_is_source_package_not_current_directory(
    monkeypatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "package"
    write_runtime(package_root)
    fake_module = package_root / "deepdesk/tool_runtime.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("ELREN_WORKSPACE", raising=False)
    monkeypatch.setattr("deepdesk.tool_runtime.__file__", str(fake_module))
    monkeypatch.chdir(elsewhere)

    entries = tool_runtime_path_entries()

    assert entries == (str(package_root / "work/tool-runtime/bin"),)
