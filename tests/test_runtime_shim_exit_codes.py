"""Execute actual generated CMD shims with local, harmless exit-code probes."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native CMD behavior requires Windows")


def run(command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=25, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    folder = tmp_path_factory.mktemp("shim-probe")
    target = folder / "Probe.exe"
    # CMD is native: unlike CLR executables, it can start with a QA-local
    # SystemRoot while exercising both curl lookup branches without host writes.
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32/cmd.exe", target)
    return target


def generate(folder):
    destination = folder / "work/tool-runtime"
    (destination / "bin").mkdir(parents=True)
    text = (ROOT / "launcher/build-tool-runtime.ps1").read_text("utf-8")
    names = ("Get-RelativePath", "Write-CmdShim", "Write-NativeShims")
    functions = []
    for name in names:
        match = re.search(rf"^function {re.escape(name)}\b[\s\S]*?^}}", text, re.MULTILINE)
        assert match, name
        functions.append(match[0])
    script = folder / "generate.ps1"
    script.write_text('param([string]$ProjectRoot,[string]$Destination)\n'
                      '$ErrorActionPreference="Stop"\n'
                      '$Lock=@{shim_allowlist=@("python3","curl")}\n'
                      + "\n".join(functions) + "\nWrite-NativeShims\n", encoding="utf-8-sig")
    result = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                  str(script), str(folder), str(destination)])
    assert result.returncode == 0, result.stdout + result.stderr
    return destination


@pytest.mark.parametrize("tool,branch", [("python3", "preferred"), ("python3", "fallback"),
                                         ("curl", "preferred"), ("curl", "fallback")])
@pytest.mark.parametrize("code", [0, 2, 7, 42])
def test_generated_shim_propagates_exact_exit_and_arguments(tmp_path, probe, tool, branch, code):
    folder = tmp_path / "package with spaces"
    folder.mkdir()
    destination = generate(folder)
    paths = {
        "python3": [folder / ".venv/Scripts/python.exe", folder / "work/python-runtime/python.exe"],
        "curl": [folder / "fake-windows/System32/curl.exe", destination / "native/mingit/mingw64/bin/curl.exe"],
    }[tool]
    for target in paths[0 if branch == "preferred" else 1:]:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(probe, target)
    expected = paths[0 if branch == "preferred" else 1]
    environment = dict(os.environ)
    environment["SystemRoot"] = str(folder / "fake-windows")
    command = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
    (folder / "probe.cmd").write_text(
        "@echo off\necho %CMDCMDLINE%\necho %~2\nexit /b %~1\n", encoding="ascii")
    arguments = (f'"{destination / f"bin/{tool}.cmd"}" /d /v:off /c '
                 f'probe.cmd {code} "spaces and !bang!"')
    result = run(f'"{command}" /d /v:off /s /c "{arguments}"', env=environment, cwd=folder)
    assert result.returncode == code, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert Path(lines[0].split('"')[1]).resolve() == expected.resolve()
    assert lines[1] == "spaces and !bang!"


@pytest.mark.parametrize("tool", ["python3", "curl"])
def test_missing_runtime_is_not_success(tmp_path, tool):
    destination = generate(tmp_path)
    environment = dict(os.environ)
    environment["SystemRoot"] = str(tmp_path / "no-windows-here")
    command = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
    result = run([command, "/d", "/c", str(destination / f"bin/{tool}.cmd")], env=environment)
    assert result.returncode == 9009
    assert "unavailable" in result.stderr


def test_verifier_rejects_swallowed_failure(monkeypatch):
    spec = importlib.util.spec_from_file_location("shim_verifier", ROOT / "launcher/verify-tool-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, "expected marker", ""))
    with pytest.raises(RuntimeError, match="Smoke command failed"):
        module.run_smoke(["never-executed"], contains="expected marker", expected_exit=7)


def test_verifier_executes_quoted_shim_and_argument(tmp_path):
    spec = importlib.util.spec_from_file_location("shim_verifier_paths", ROOT / "launcher/verify-tool-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shim = tmp_path / "smoke probe with spaces.cmd"
    shim.write_text("@echo off\nsetlocal DisableDelayedExpansion\necho %~1\nexit /b 7\n", encoding="ascii")
    command = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
    assert module.run_smoke([command, "/d", "/c", str(shim), "spaces and !bang!"],
                            contains="spaces and !bang!", expected_exit=7) == "spaces and !bang!"
