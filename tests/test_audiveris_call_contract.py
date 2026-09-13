"""Verify the actual product's child-process boundary, without running OMR."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from deepdesk.plugins.builtin import jianpu_omr as module

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("languages_available", [True, False])
@pytest.mark.parametrize("native_failure", [True, False])
def test_recognize_wires_verified_data_and_task_state(tmp_path, monkeypatch, languages_available, native_failure):
    image = tmp_path / "input original.png"
    image.write_bytes(b"synthetic: never sent to an actual recognizer")
    job = tmp_path / "job"
    job.mkdir()
    runtime = ROOT / "work/tool-runtime/native/audiveris"
    monkeypatch.setattr(module.JianpuOMRTool, "_audiveris_runtime", classmethod(lambda _cls, _workspace: (runtime, "docker-lite-package-local")))
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "synthetic-host-setting-not-used")
    monkeypatch.setenv("TESSDATA_PREFIX", "synthetic-host-tessdata-not-used")
    if not languages_available:
        def missing(_home):
            raise ValueError("synthetic corrupted weights")
        monkeypatch.setattr(module, "validate_tessdata", missing)
    calls = []

    def recognize(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments.count("-cp") == 1
        assert "org.elren.omr.ElrenAudiverisBootstrap" in arguments
        assert "Audiveris" not in arguments
        assert "audiveris-bootstrap.jar" in arguments[arguments.index("-cp") + 1]
        assert "--add-opens=java.desktop/javax.swing.filechooser=ALL-UNNAMED" in arguments
        assert f"-Delren.omr.state.root={job / 'runtime-state'}" in arguments
        assert f"-Djava.io.tmpdir={job / 'runtime-state/tmp'}" in arguments
        assert kwargs["cwd"] == job and kwargs["timeout"] == 600
        environment = kwargs["env"]
        assert "JAVA_TOOL_OPTIONS" not in environment
        assert Path(environment["APPDATA"]).is_relative_to(job)
        if languages_available:
            if environment["TESSDATA_PREFIX"] == "runtime-state/tessdata":
                assert (job / environment["TESSDATA_PREFIX"] / "eng.traineddata").is_file()
            else:
                assert environment["TESSDATA_PREFIX"] == str(runtime / "tessdata")
            assert "org.audiveris.omr.text.Language.defaultSpecification=eng+chi_sim+chi_tra" in arguments
        else:
            assert Path(environment["TESSDATA_PREFIX"]).is_relative_to(job)
            assert not list(Path(environment["TESSDATA_PREFIX"]).iterdir())
            assert "-constant" not in arguments
        output = Path(arguments[arguments.index("-output") + 1]) / "synthetic.musicxml"
        output.write_text("<score-partwise/>", encoding="utf-8")
        log = "No installed OCR languages" if native_failure else "synthetic recognition boundary only"
        return subprocess.CompletedProcess(arguments, 0, log, "")

    monkeypatch.setattr(module.subprocess, "run", recognize)
    result, info = module.JianpuOMRTool._recognize(image, tmp_path, job)
    assert len(calls) == 1 and result.is_relative_to(job)
    ready = languages_available and not native_failure
    assert info["offline_text_ocr_ready"] is ready
    assert bool(info["text_ocr_warning"]) is not ready
    assert info["offline_text_ocr_languages"] == (list(module.LANGUAGES) if ready else [])
    assert info["omr_log_tail"] == info["log_tail"]


@pytest.mark.parametrize("languages_available", [True, False])
def test_status_does_not_hide_incomplete_text_ocr(tmp_path, monkeypatch, languages_available):
    if not languages_available:
        def missing(_home):
            raise ValueError("synthetic missing data")
        monkeypatch.setattr(module, "validate_tessdata", missing)
    state = module.JianpuOMRTool._status(tmp_path)
    assert state["ready"]
    assert state["offline_text_ocr_ready"] is languages_available
    assert ("语言数据不完整" in state["message"]) is not languages_available
