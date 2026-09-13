from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk.harness import recommended_tool_names
from deepdesk.models import AgentProfile
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin import jianpu_omr as jianpu_omr_module
from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    JianpuToStaffTool,
    _companion_title_image,
    _parse_musicxml,
    _score_to_jianpu_source,
)
from deepdesk.score_title_ocr import clean_title, select_tempo, select_title


def _sample(tmp_path: Path) -> Path:
    target = tmp_path / "score.musicxml"
    target.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0"><work><work-title>测试曲</work-title></work>
<part-list><score-part id="P1"><part-name>旋律</part-name></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>1</divisions><key><fifths>0</fifths></key>
<time><beats>4</beats><beat-type>4</beat-type></time></attributes>
<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice></note>
</measure></part></score-partwise>""",
        encoding="utf-8",
    )
    return target


@pytest.mark.parametrize("normalize_omr", [False, True])
def test_omr_page_number_and_punctuation_are_not_engraved_as_composer(tmp_path, normalize_omr):
    source = _sample(tmp_path).read_text("utf-8").replace(
        "<part-list>",
        '<identification><creator type="composer">104</creator>'
        '<creator type="composer">)</creator></identification><part-list>',
    )
    score = _parse_musicxml(source, normalize_omr=normalize_omr)
    if normalize_omr:
        assert score["creators"] == []
        assert score["omr_original_creators"] == ["104", ")"]
        assert any("omr_original_creators" in item for item in score["warnings"])
        assert "composer=104" not in _score_to_jianpu_source(score)
    else:
        assert score["creators"] == ["104", ")"]
        assert "omr_original_creators" not in score


@pytest.mark.parametrize("name", ["J. S. Bach", "勤泰利埃", "坂本龍一", "Ólafur Arnalds", "李某 Ⅱ"])
def test_omr_preserves_readable_creator_names(tmp_path, name):
    source = _sample(tmp_path).read_text("utf-8").replace(
        "<part-list>",
        f'<identification><creator type="composer">{name}</creator></identification><part-list>',
    )
    score = _parse_musicxml(source, normalize_omr=True)
    assert score["creators"] == [name]
    assert "omr_original_creators" not in score


@pytest.mark.asyncio
async def test_direct_musicxml_exports_html_text_json_and_source(tmp_path: Path) -> None:
    source = _sample(tmp_path)
    tool = JianpuOMRTool()
    result = await tool.execute(
        {"action": "convert", "input_path": str(source), "output_name": "练习曲"},
        ToolContext(task_id="jianpu-test", workspace=str(tmp_path)),
    )

    assert result["ok"] is True
    assert result["source_identification"]["title"] == "测试曲"
    assert result["source_identification"]["confidence_label"] == "high"
    assert "原谱曲名是《测试曲》" in result["message"]
    assert result["runtime"]["runtime_mode"] == "docker-lite-parser"
    assert len(result["paths"]) >= 8
    paths = [Path(value) for value in result["paths"]]
    assert all(path.is_file() and tmp_path in path.parents for path in paths)
    text = next(path for path in paths if path.suffix == ".txt").read_text("utf-8")
    page = next(path for path in paths if path.suffix == ".html").read_text("utf-8")
    assert "1=" in text
    assert "|" in text
    assert "jianpu-ly 1.889" in page
    assert any(path.suffix == ".pdf" for path in paths)
    assert any(path.suffix == ".svg" for path in paths)
    assert any(path.suffix == ".jly" for path in paths)
    assert all(item["sha256"] and len(item["sha256"]) == 64 for item in result["artifacts"])


@pytest.mark.asyncio
async def test_status_prefers_package_local_docker_lite_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "work" / "tool-runtime" / "native" / "audiveris"
    (runtime / "runtime" / "bin").mkdir(parents=True)
    (runtime / "app").mkdir()
    (runtime / "runtime" / "bin" / ("java.exe" if __import__("os").name == "nt" else "java")).write_bytes(b"java")
    (runtime / "app" / "audiveris.jar").write_bytes(b"jar")

    result = await JianpuOMRTool().execute(
        {"action": "status"}, ToolContext(task_id="status", workspace=str(tmp_path))
    )

    assert result["ready"] is True
    assert result["runtime_mode"] == "docker-lite-package-local"
    assert result["docker_lite_model_present"] is True
    assert result["engraver_ready"] is True
    assert result["notation_runtime_ready"] is True
    assert result["docker_lite_notation_present"] is True
    assert result["cloud_upload"] is False


@pytest.mark.asyncio
async def test_package_runtime_is_independent_of_user_task_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = tmp_path / "elren-install"
    fake_module = install_root / "deepdesk" / "plugins" / "builtin" / "jianpu_omr.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    runtime = install_root / "work" / "tool-runtime" / "native" / "audiveris"
    (runtime / "runtime" / "bin").mkdir(parents=True)
    (runtime / "app").mkdir()
    (runtime / "runtime" / "bin" / ("java.exe" if __import__("os").name == "nt" else "java")).write_bytes(b"java")
    (runtime / "app" / "audiveris.jar").write_bytes(b"jar")
    task_workspace = tmp_path / "unrelated-user-project"
    task_workspace.mkdir()
    monkeypatch.setattr(jianpu_omr_module, "__file__", str(fake_module))
    monkeypatch.delenv("ELREN_WORKSPACE", raising=False)
    monkeypatch.delenv("ELREN_AUDIVERIS_HOME", raising=False)

    result = await JianpuOMRTool().execute(
        {"action": "status"},
        ToolContext(task_id="external-workspace", workspace=str(task_workspace)),
    )

    assert result["ready"] is False
    assert result["runtime_mode"] == "docker-lite-package-local"
    assert result["docker_lite_model_present"] is True
    assert result["engraver_ready"] is False


@pytest.mark.asyncio
async def test_score_outside_workspace_and_attachment_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = _sample(tmp_path)

    with pytest.raises(PermissionError, match="outside"):
        await JianpuOMRTool().execute(
            {"action": "convert", "input_path": str(outside)},
            ToolContext(task_id="scope", workspace=str(workspace)),
        )


def test_staff_to_jianpu_intent_prioritizes_dedicated_tool() -> None:
    names = recommended_tool_names("请把附件里的五线谱转换成简谱并导出", AgentProfile.GENERAL)
    assert names[0] == "jianpu_omr"


def test_jianpu_to_staff_intent_prioritizes_reverse_converter() -> None:
    names = recommended_tool_names("请把这份简谱转换成五线谱并导出", AgentProfile.GENERAL)
    assert names[0] == "jianpu_to_staff"


@pytest.mark.asyncio
async def test_jianpu_to_staff_exports_open_source_staff_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "练习.jly"
    source.write_text(
        "title=双向转换测试\n1=C\n4/4\nq1 q2 q3 q4 q5 q6 q7 q1'\n",
        encoding="utf-8",
    )
    result = await JianpuToStaffTool().execute(
        {"action": "convert", "input_path": str(source), "output_name": "双向转换"},
        ToolContext(task_id="staff-test", workspace=str(tmp_path)),
    )

    assert result["ok"] is True
    assert result["runtime"]["notation_runtime_mode"] == "docker-lite-package-local"
    paths = [Path(value) for value in result["paths"]]
    assert any(path.suffix == ".pdf" for path in paths)
    assert any(path.suffix == ".svg" for path in paths)
    assert any(path.suffix == ".mid" for path in paths)
    jly = next(path for path in paths if path.suffix == ".jly").read_text("utf-8")
    lilypond = next(path for path in paths if path.suffix == ".ly").read_text("utf-8")
    page = next(path for path in paths if path.suffix == ".html").read_text("utf-8")
    assert "WithStaff" in jly
    assert "\\new Staff" in lilypond
    assert "RhythmicStaff" in lilypond
    assert "保留原简谱" in page


@pytest.mark.asyncio
async def test_jianpu_to_staff_rejects_lilypond_injection(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.jly"
    source.write_text(
        "1=C\n4/4\nLP:\n#(system \"whoami\")\n:LP\n1 2 3 4\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="注入"):
        await JianpuToStaffTool().execute(
            {"action": "convert", "input_path": str(source)},
            ToolContext(task_id="unsafe", workspace=str(tmp_path)),
        )


def test_music_or_generic_ocr_does_not_false_trigger_jianpu() -> None:
    assert "jianpu_omr" not in recommended_tool_names("生成一段音乐", AgentProfile.GENERAL)
    assert "jianpu_to_staff" not in recommended_tool_names("生成一段音乐", AgentProfile.GENERAL)
    assert "jianpu_omr" not in recommended_tool_names("识别图片里的文字", AgentProfile.GENERAL)


def test_printed_score_title_prefers_large_centered_heading() -> None:
    result = select_title(
        [
            {
                "text": "104 九级 练习曲",
                "confidence": 0.93,
                "box": [[106, 139], [488, 143], [488, 189], [106, 185]],
            },
            {
                "text": "3. 第 三 首★",
                "confidence": 0.84,
                "box": [[761, 328], [1210, 318], [1211, 383], [762, 392]],
            },
            {
                "text": "Letellier",
                "confidence": 1.0,
                "box": [[1699, 483], [1864, 483], [1864, 527], [1699, 527]],
            },
        ],
        1962,
        2780,
    )

    assert clean_title("3. 第 三 首★") == "第三首"
    assert result["title"] == "第三首"
    assert result["confidence_label"] in {"medium", "high"}
    assert result["needs_confirmation"] is (result["confidence_label"] != "high")
    assert any("练习曲" in value for value in result["context"])


def test_printed_score_tempo_recognizes_words_and_explicit_bpm() -> None:
    result = select_tempo(
        [
            {
                "text": "Allegro = 120",
                "confidence": 0.9552,
                "box": [[255, 562], [552, 565], [552, 631], [255, 628]],
            }
        ],
        2780,
    )

    assert result["tempo_words"] == "Allegro"
    assert result["tempo"] == 120
    assert result["tempo_beat_unit"] is None
    assert result["tempo_needs_confirmation"] is True
    assert result["tempo_confidence_label"] == "high"


def test_printed_score_tempo_stays_empty_without_explicit_marking() -> None:
    result = select_tempo(
        [{"text": "104 九级练习曲", "confidence": 0.98, "box": [[1, 1], [10, 10]]}],
        1000,
    )

    assert result["tempo"] is None
    assert result["tempo_words"] == ""
    assert result["tempo_confidence_label"] == "unresolved"


def test_musicxml_uses_browser_export_companion_image_for_title(tmp_path: Path) -> None:
    image = tmp_path / "第三首原谱.jpg"
    image.write_bytes(b"image")
    musicxml = tmp_path / "第三首原谱(MusicXML).mxl"
    musicxml.write_bytes(b"musicxml")

    assert _companion_title_image(musicxml) == image.resolve()


def test_musicxml_without_companion_image_does_not_guess_title(tmp_path: Path) -> None:
    musicxml = tmp_path / "unrelated.musicxml"
    musicxml.write_bytes(b"musicxml")

    assert _companion_title_image(musicxml) is None


def test_companion_image_title_overrides_unreliable_musicxml_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "score.jpg"
    image.write_bytes(b"image")
    musicxml = tmp_path / "score.jpg(MusicXML).mxl"
    musicxml.write_bytes(b"musicxml")
    score = {
        "title": "误识别页眉",
        "title_source": "musicxml-credit-words",
    }
    payload = {
        "ok": True,
        "title": "第三首",
        "source": "paddleocr-local-title",
        "confidence": 0.8724,
        "confidence_label": "medium",
        "needs_confirmation": True,
        "context": ["九级练习曲"],
        "raw_text": "3. 第 三 首★",
    }
    monkeypatch.setattr(
        jianpu_omr_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False)),
    )

    result = jianpu_omr_module._identify_source_score(musicxml, score)

    assert result["title"] == "第三首"
    assert result["needs_confirmation"] is True
    assert score["title"] == "第三首"
    assert score["title_source"] == "paddleocr-local-title"


def test_source_image_ocr_fills_missing_tempo_without_overriding_explicit_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "score.jpg"
    image.write_bytes(b"image")
    score = {
        "title": "明确曲名",
        "title_source": "musicxml-work-title",
        "tempo": None,
    }
    payload = {
        "ok": True,
        "title": "错误候选标题",
        "tempo": 120,
        "tempo_words": "Allegro",
        "tempo_beat_unit": "quarter",
        "tempo_confidence": 0.9552,
        "tempo_source": "paddleocr-local-tempo",
    }
    monkeypatch.setattr(
        jianpu_omr_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False)),
    )

    result = jianpu_omr_module._identify_source_score(image, score)

    assert result["title"] == "明确曲名"
    assert score["title"] == "明确曲名"
    assert score["tempo"]["words"] == "Allegro"
    assert score["tempo"]["tempo"] == 120
    assert score["tempo"]["source"] == "paddleocr-local-tempo"


def test_musicxml_explicit_tempo_is_preserved_and_rendered() -> None:
    xml = """<score-partwise version="4.0">
    <work><work-title>速度测试</work-title></work>
    <part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
    <part id="P1"><measure number="1">
      <attributes><divisions>1</divisions><key><fifths>0</fifths></key>
      <time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <direction><direction-type><words>Allegro</words><metronome>
      <beat-unit>quarter</beat-unit><per-minute>120</per-minute>
      </metronome></direction-type><sound tempo="120"/></direction>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <voice>1</voice><type>quarter</type></note>
    </measure></part></score-partwise>"""

    score = _parse_musicxml(xml)
    source = _score_to_jianpu_source(score)
    rendered = jianpu_omr_module._render_html(score)

    assert score["tempo"]["words"] == "Allegro"
    assert score["tempo"]["tempo"] == 120
    assert source.split().count("4=120") == 1
    assert "piece=Allegro" in source.splitlines()
    assert "Allegro" in rendered
    assert "♩=120" in rendered


def test_generated_omr_metadata_is_verified_against_original_image(tmp_path, monkeypatch):
    image = tmp_path / "score.jpg"
    image.write_bytes(b"image")
    initial = {"words": "Allegro", "tempo": 2, "onset": 0.25, "beat_unit": "quarter"}
    later = {"words": "Andante", "tempo": 90, "onset": 0}
    score = {"title": "3 笪节 三", "title_source": "musicxml-movement-title",
             "tempo": {"tempo": 2}, "parts": [{"measures": [
                 {"directions": [initial]}, {"directions": [later]}]}]}
    payload = {"ok": True, "title": "第三首", "source": "paddleocr-local-title",
               "confidence": 0.87, "needs_confirmation": True,
               "tempo": 120, "tempo_words": "Allegro", "tempo_confidence": 0.95}
    monkeypatch.setattr(jianpu_omr_module.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(stdout=json.dumps(payload)))
    result = jianpu_omr_module._identify_source_score(image, score, omr_generated=True)
    assert result["title"] == score["title"] == "第三首"
    assert result["needs_confirmation"] is True
    assert score["tempo"]["tempo"] == initial["tempo"] == 120
    assert initial["omr_original_tempo"] == 2
    assert initial["omr_original_onset"] == 0.25
    assert initial["onset"] == 0
    assert later["tempo"] == 90
    assert score["warnings"]


def test_failed_image_verification_does_not_promote_omr_title_to_certainty(tmp_path, monkeypatch):
    image = tmp_path / "score.jpg"
    image.write_bytes(b"image")
    score = {"title": "OMR guess", "title_source": "musicxml-movement-title"}
    monkeypatch.setattr(jianpu_omr_module.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(stdout='{"ok":false}'))
    result = jianpu_omr_module._identify_source_score(image, score, omr_generated=True)
    assert result["needs_confirmation"] is True
    assert result["confidence"] < 1


def test_frozen_mac_discovers_native_runtime_under_resources_bundle(tmp_path, monkeypatch):
    resources = tmp_path / "Elren.app/Contents/Resources"
    executable = resources / "backend/ElrenBackend"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(jianpu_omr_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(jianpu_omr_module.sys, "platform", "darwin")
    monkeypatch.setattr(jianpu_omr_module.sys, "executable", str(executable))
    assert JianpuOMRTool._application_root() == resources.resolve() / "bundle"


def test_lilypond_portable_cache_is_newer_than_extracted_guile_source(tmp_path: Path) -> None:
    root = tmp_path / "lilypond-2.26.0"
    executable = root / "bin" / "lilypond.exe"
    source = root / "share" / "guile" / "3.0" / "ice-9" / "eval.scm"
    compiled = root / "lib" / "guile" / "3.0" / "ccache" / "ice-9" / "eval.go"
    executable.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    compiled.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    source.write_bytes(b"source")
    compiled.write_bytes(b"compiled")
    now = time.time()
    os.utime(compiled, (now - 20, now - 20))
    os.utime(source, (now, now))
    jianpu_omr_module._PREPARED_LILYPOND_ROOTS.discard(root.resolve())

    JianpuOMRTool._prepare_lilypond_cache(executable)

    assert compiled.stat().st_mtime_ns > source.stat().st_mtime_ns


def test_sixteenth_notes_render_two_beams_and_explicit_octave_dots() -> None:
    notes = []
    for index, step in enumerate(["C", "D", "E", "F", "G", "A", "B", "C"]):
        octave = 6 if index == 7 else 5
        beam = "begin" if index == 0 else "end" if index == 7 else "continue"
        slur = '<slur type="start" number="1"/>' if index == 0 else '<slur type="stop" number="1"/>' if index == 7 else ""
        notes.append(
            f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            f"<duration>1</duration><voice>1</voice><type>16th</type>"
            f"<beam number=\"1\">{beam}</beam><beam number=\"2\">{beam}</beam>"
            f"<notations>{slur}</notations></note>"
        )
    xml = (
        '<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Voice</part-name>'
        '</score-part></part-list><part id="P1"><measure number="1"><attributes><divisions>4</divisions>'
        '<key><fifths>0</fifths></key><time><beats>2</beats><beat-type>4</beat-type></time></attributes>'
        + "".join(notes)
        + "</measure></part></score-partwise>"
    )

    score = _parse_musicxml(xml)
    source = _score_to_jianpu_source(score)

    assert score["stats"]["notes"] == 8
    assert all(note["beams"] == 2 for note in score["parts"][0]["measures"][0]["notes"])
    music_line = next(line for line in source.splitlines() if line.startswith("s"))
    assert len(re.findall(r"\bs[#b]?[1-7]'?", music_line)) == 8
    assert "s1''" in music_line
    assert " ( " in music_line and music_line.endswith(" )")


def test_short_false_ottava_from_omr_is_corrected_only_for_recognized_input() -> None:
    xml = """<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Voice</part-name></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>4</divisions><key><fifths>0</fifths></key>
<time><beats>2</beats><beat-type>4</beat-type></time></attributes>
<note><pitch><step>B</step><octave>5</octave></pitch><duration>1</duration><type>16th</type></note>
<direction><direction-type><octave-shift type="down" number="1"/></direction-type></direction>
<note><pitch><step>C</step><octave>7</octave></pitch><duration>1</duration><type>16th</type></note>
<note><pitch><step>D</step><octave>7</octave></pitch><duration>1</duration><type>16th</type></note>
<direction><direction-type><octave-shift type="stop" number="1"/></direction-type></direction>
<note><pitch><step>E</step><octave>5</octave></pitch><duration>5</duration><type>quarter</type></note>
</measure></part></score-partwise>"""

    direct = _parse_musicxml(xml, normalize_omr=False)
    recognized = _parse_musicxml(xml, normalize_omr=True)
    direct_notes = direct["parts"][0]["measures"][0]["notes"]
    recognized_notes = recognized["parts"][0]["measures"][0]["notes"]

    assert [note["source_pitch"] for note in direct_notes[1:3]] == ["C7", "D7"]
    assert [note["source_pitch"] for note in recognized_notes[1:3]] == ["C6", "D6"]
    assert recognized["stats"]["auto_octave_corrections"] == 2
    assert all(note.get("auto_corrected") for note in recognized_notes[1:3])
