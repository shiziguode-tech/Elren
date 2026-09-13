from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk import lilypond_windows
from deepdesk.lilypond_windows import WINDOWS_XML_READY, windows_engraver_arguments
from deepdesk.plugins.builtin import jianpu_omr
from deepdesk.plugins.builtin.jianpu_omr import JianpuOMRTool
from deepdesk.subprocess_env import credential_safe_environment


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_arguments_are_windows_only_and_not_caller_mutable(monkeypatch, platform):
    monkeypatch.setattr(lilypond_windows, "sys", SimpleNamespace(platform=platform))
    arguments = windows_engraver_arguments()
    if platform != "win32":
        assert arguments == []
        return
    assert arguments[0] == "-e"
    assert '"xmllite.dll") (make-pointer 0) 2048)' in arguments[1]
    assert "FreeLibrary" not in arguments[1]
    assert "null-pointer?" in arguments[1]
    assert WINDOWS_XML_READY in arguments[1]
    arguments.clear()
    assert len(windows_engraver_arguments()) == 2


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("outcome", ["success", "no_marker", "native_failure", "timeout"])
def test_real_typesetting_command_and_failure_boundaries(tmp_path, monkeypatch, platform, outcome):
    monkeypatch.setattr(lilypond_windows, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(JianpuOMRTool, "_lilypond_runtime", lambda _: (tmp_path / "lilypond.exe", "bundled"))
    monkeypatch.setattr(JianpuOMRTool, "_prepare_lilypond_cache", lambda _: None)
    monkeypatch.setattr(JianpuOMRTool, "_compile_jianpu_source", lambda *_: ("trusted compiled notation", "bundled"))
    source = tmp_path / "曲子 & O'Brien.jly"
    compiled = source.with_suffix(".ly")
    prefix = tmp_path / "曲子 & O'Brien"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[-1] == str(compiled)
        assert kwargs["timeout"] == 180
        assert not kwargs.get("shell", False)
        if platform == "win32":
            assert command[1:3] == windows_engraver_arguments()
            assert str(compiled) not in command[2]
        else:
            assert "-e" not in command
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 180)
        for suffix in (".pdf", ".svg", ".midi"):
            prefix.with_suffix(suffix).touch()
        return SimpleNamespace(returncode=3221225477 if outcome == "native_failure" else 0,
                               stdout="" if outcome == "no_marker" else WINDOWS_XML_READY + "\n" + "x" * 15000,
                               stderr="synthetic log")

    monkeypatch.setattr(jianpu_omr.subprocess, "run", run)
    fails = outcome in {"native_failure", "timeout"} or (platform == "win32" and outcome == "no_marker")
    if fails:
        with pytest.raises(RuntimeError):
            JianpuOMRTool._typeset_jianpu_source("1=C\n4/4\n1 2 3 4", tmp_path, tmp_path, source, compiled, prefix)
        assert len(calls) == 1  # No hidden retry or false success from existing artifacts.
    else:
        _, _, _, info = JianpuOMRTool._typeset_jianpu_source("1=C\n4/4\n1 2 3 4", tmp_path, tmp_path, source, compiled, prefix)
        assert len(calls) == 2
        assert "--pdf" in calls[0] and "--svg" in calls[1]
        assert info["windows_xml_lifetime_guard"] == (platform == "win32")


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows DLL loader test")
@pytest.mark.parametrize("missing_library", [False, True])
def test_native_initialization_ignores_working_directory_and_fails_closed(tmp_path, missing_library):
    root = Path(__file__).resolve().parents[1]
    runtime = JianpuOMRTool._lilypond_runtime(root)
    assert runtime and runtime[0].is_relative_to(root / "work/tool-runtime")
    JianpuOMRTool._prepare_lilypond_cache(runtime[0])
    # A non-executable test decoy must never be selected over the OS library.
    (tmp_path / "xmllite.dll").write_bytes(b"not a DLL: isolated test data")
    arguments = windows_engraver_arguments()
    if missing_library:
        arguments[1] = arguments[1].replace('"xmllite.dll"', '"elren-nonexistent-xml-qa.dll"')
    completed = subprocess.run(
        [str(runtime[0]), *arguments, "-e", "(exit 0)"], cwd=tmp_path,
        env=credential_safe_environment({"GUILE_AUTO_COMPILE": "0", "ELREN_ENGRAVER_OFFLINE": "1"}),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )
    combined = completed.stdout + completed.stderr
    if missing_library:
        assert WINDOWS_XML_READY not in combined
        assert "system XML library reference unavailable" in combined
        assert completed.returncode != 0
    else:
        assert completed.returncode == 0, combined
        assert WINDOWS_XML_READY in combined
