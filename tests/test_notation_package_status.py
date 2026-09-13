"""Explicit runtime selection must not change physical package membership."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from deepdesk.plugins.builtin.jianpu_omr import LILYPOND_VERSION, JianpuOMRTool, JianpuToStaffTool

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "app with spaces"
    native = root / "work/tool-runtime/native"
    notation = native / "jianpu-ly"
    notation.mkdir(parents=True)
    for name in ("jianpu_ly.py", "UPSTREAM.json", "LICENSE"):
        shutil.copyfile(ROOT / "work/tool-runtime/native/jianpu-ly" / name, notation / name)
    engraver = native / "lilypond" / f"lilypond-{LILYPOND_VERSION}" / "bin" / ("lilypond.exe" if os.name == "nt" else "lilypond")
    engraver.parent.mkdir(parents=True)
    engraver.write_bytes(b"not executed: package discovery fixture")
    audiveris = native / "audiveris"
    (audiveris / "runtime/bin").mkdir(parents=True)
    (audiveris / "runtime/bin" / ("java.exe" if os.name == "nt" else "java")).write_bytes(b"not executed")
    (audiveris / "app").mkdir()
    (audiveris / "app/audiveris.jar").write_bytes(b"not executed")
    monkeypatch.setattr(JianpuOMRTool, "_application_root", staticmethod(lambda: root))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    for name in ("ELREN_WORKSPACE", "ELREN_LILYPOND_HOME", "ELREN_JIANPU_LY_HOME", "ELREN_AUDIVERIS_HOME"):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "unrelated task"
    workspace.mkdir()
    return workspace, notation, engraver


@pytest.mark.parametrize("tool", [JianpuOMRTool, JianpuToStaffTool])
@pytest.mark.parametrize("selection", ["automatic", "directory", "executable"])
def test_same_package_remains_packaged_with_explicit_selection(package, monkeypatch, tool, selection):
    workspace, notation, engraver = package
    if selection != "automatic":
        monkeypatch.setenv("ELREN_JIANPU_LY_HOME", str(notation))
        monkeypatch.setenv("ELREN_LILYPOND_HOME", str(engraver if selection == "executable" else engraver.parent.parent))
    state = tool._status(workspace)
    assert state["ready"] and state["docker_lite_notation_present"]
    if selection != "automatic":
        assert state["notation_runtime_mode"] == "explicit-package-runtime"
        assert state["engraver_runtime_mode"] == "explicit-package-runtime"
    assert "外部路径" not in state["message"]


@pytest.mark.parametrize("tool", [JianpuOMRTool, JianpuToStaffTool])
@pytest.mark.parametrize("external", ["notation", "engraver", "both"])
def test_external_runtime_is_ready_but_not_reported_as_packaged(package, tmp_path, monkeypatch, tool, external):
    workspace, notation, engraver = package
    if external in {"notation", "both"}:
        outside = tmp_path / "external notation"
        shutil.copytree(notation, outside)
        monkeypatch.setenv("ELREN_JIANPU_LY_HOME", str(outside))
    if external in {"engraver", "both"}:
        outside = tmp_path / engraver.name
        shutil.copyfile(engraver, outside)
        monkeypatch.setenv("ELREN_LILYPOND_HOME", str(outside))
    state = tool._status(workspace)
    assert state["ready"] and not state["docker_lite_notation_present"]
    assert "外部路径" in state["message"]


@pytest.mark.parametrize("tool", [JianpuOMRTool, JianpuToStaffTool])
def test_missing_engine_is_not_marked_packaged(package, tool):
    workspace, _notation, engraver = package
    engraver.unlink()  # Only this test's generated non-executable fixture.
    state = tool._status(workspace)
    assert not state["ready"] and not state["docker_lite_notation_present"]
