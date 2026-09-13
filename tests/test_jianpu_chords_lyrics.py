"""Independent chord sounding times and separate, real compiled lyric contexts."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import subprocess

import pytest
from test_jianpu_fidelity_midi import ROOT, _midi, _mod, _note, _xml
from test_jianpu_structure_midi import repeat
from test_jianpu_timeline_midi import attrs, document

from deepdesk.lilypond_windows import windows_engraver_arguments
from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    JianpuToStaffTool,
    _parse_musicxml,
    _score_lines,
    _score_to_jianpu_source,
    _with_western_staff,
)
from deepdesk.subprocess_env import credential_safe_environment


def chord(step="E", duration=8, kind="half", **kwargs):
    return _note(step, duration, kind, **kwargs).replace("<note>", "<note><chord/>", 1)


START = '<tie type="start"/>'
STOP = '<tie type="stop"/>'

CASES = {
    "short_member": (_xml(_note() + chord()), [(60, 0, 4), (64, 0, 2)]),
    "partial_tie": (_xml(_note(duration=8, kind="half", extra=START) + chord()
                         + _note(duration=8, kind="half", extra=STOP) + chord()),
                    [(60, 0, 4), (64, 0, 2), (64, 2, 2)]),
    "upper_tie": (_xml(_note(duration=8, kind="half") + chord(extra=START)
                       + _note(duration=8, kind="half") + chord(extra=STOP)),
                  [(60, 0, 2), (64, 0, 4), (60, 2, 2)]),
    "reordered_tie": (_xml(_note(duration=8, kind="half", extra=START) + chord()
                           + _note("E", 8, "half") + chord("C", extra=STOP)),
                      [(60, 0, 4), (64, 0, 2), (64, 2, 2)]),
    "three_lengths": (_xml(_note() + chord() + chord("G", 4, "quarter")),
                      [(60, 0, 4), (64, 0, 2), (67, 0, 1)]),
    "ordinary_stacked": (_xml(_note() + chord(duration=16, kind="whole")),
                         [(60, 0, 4), (64, 0, 4)]),
    "all_tied_stacked": (_xml(_note(duration=8, kind="half", extra=START) + chord(extra=START)
                              + _note(duration=8, kind="half", extra=STOP) + chord(extra=STOP)),
                          [(60, 0, 4), (64, 0, 4)]),
    "across_barline": (document(attrs() + _note(extra=START) + chord(),
                                 _note(extra=STOP) + chord("G", 16, "whole")),
                        [(60, 0, 8), (64, 0, 2), (67, 4, 4)]),
    "repeat_short": (document(attrs() + repeat("forward") + _note() + chord() + repeat("backward")),
                     [(60, 0, 4), (64, 0, 2), (60, 4, 4), (64, 4, 2)]),
    "other_voice": (_xml(_note() + chord() + '<backup><duration>16</duration></backup>'
                          + _note("G").replace('<voice>1</voice>', '<voice>2</voice>')),
                    [(60, 0, 4), (64, 0, 2), (67, 0, 4)]),
    "voice_name_collision": (_xml(_note() + chord() + '<backup><duration>16</duration></backup>'
                                   + _note("G").replace('<voice>1</voice>', '<voice>1.chord1</voice>')),
                             [(60, 0, 4), (64, 0, 2), (67, 0, 4)]),
    "late_chord": (document(attrs() + _note(), _note("D") + chord()),
                   [(60, 0, 4), (62, 4, 4), (64, 4, 2)]),
    "unison_independent": (_xml(_note() + chord("C")), [(60, 0, 4), (60, 0, 2)]),
    "tie_across_key": (document(attrs() + _note(extra=START) + chord(),
                                 attrs(1) + _note(extra=STOP) + chord("G", 16, "whole")),
                        [(60, 0, 8), (64, 0, 2), (67, 4, 4)]),
    "tuplets_short": (_xml("".join(_note(step, 8, "quarter", extra=_mod())
                                    + chord("G", 4, "eighth", extra=_mod()) for step in "CDE")
                            + _note("A", 24, "half"), divisions=12),
                      [(60, 0, 2/3), (67, 0, 1/3), (62, 2/3, 2/3), (67, 2/3, 1/3),
                       (64, 4/3, 2/3), (67, 4/3, 1/3), (69, 2, 2)]),
}


def lyric(text, number="1", *, name="", extra=""):
    identity = f' number="{number}"' if number else f' name="{name}"'
    return f'<lyric{identity}>{extra}<text>{text}</text></lyric>'


LYRIC_CASES = {
    "two_verses": _xml("".join(_note(step, 4, "quarter", extra=lyric(a) + lyric(b, "2"))
                                for step, a, b in zip("CDEF", "春天来了", "秋天到了"))),
    "named_and_missing": _xml(_note(duration=4, kind="quarter", extra=lyric("hello", "", name="Chorus"))
                              + '<note><rest/><duration>4</duration><voice>1</voice><type>quarter</type></note>'
                              + _note("D", 4, "quarter", extra=lyric("world", "", name="Chorus") + lyric("later", "10"))
                              + _note("E", 4, "quarter", extra=lyric("again", "10"))),
    "same_syllable_fragments": _xml(_note(extra='<lyric number="1"><text>春</text><text>天</text></lyric>'
                                          + lyric("autumn", "2"))),
    "syllabic_verses": _xml(_note(duration=8, kind="half", extra=lyric("hel", extra="<syllabic>begin</syllabic>")
                                 + lyric("bon", "2"))
                            + _note("D", 8, "half", extra=lyric("lo", extra="<syllabic>end</syllabic>")
                                    + lyric("jour", "2"))),
    "chord_verses": _xml(_note(extra=lyric("春天") + lyric("秋天", "2")) + chord()),
    "chord_changing_melody": _xml(_note(duration=8, kind="half", extra=lyric("春") + lyric("秋", "2"))
                                  + chord("D", 4, "quarter")
                                  + _note("D", 8, "half", extra=lyric("天") + lyric("夜", "2"))
                                  + chord("E", 4, "quarter")),
}


@pytest.fixture(scope="module")
def converted(tmp_path_factory):
    runtime = JianpuOMRTool._lilypond_runtime(ROOT)
    assert runtime and runtime[0].is_relative_to(ROOT / "work/tool-runtime")
    JianpuOMRTool._prepare_lilypond_cache(runtime[0])
    directory = tmp_path_factory.mktemp("chords-lyrics")
    books = ['\\version "2.26.0"']
    results = {}
    full_lyrics = []
    for name, xml in {**{name: case[0] for name, case in CASES.items()}, **LYRIC_CASES}.items():
        score = _parse_musicxml(xml)
        snapshot = copy.deepcopy(score)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
        source = JianpuToStaffTool._source_from_input(path)
        assert _score_to_jianpu_source(score) == source
        assert score == snapshot, "Serialization must not alter the editable score"
        with contextlib.redirect_stderr(io.StringIO()):
            lily, _ = JianpuOMRTool._compile_jianpu_source(_with_western_staff(source), ROOT)
        (directory / f"{name}.jly").write_text(source, encoding="utf-8")
        (directory / f"{name}.ly").write_text(lily, encoding="utf-8")
        staffs = re.findall(r"BEGIN MIDI STAFF ===(.*?)% === END MIDI STAFF", lily, re.DOTALL)
        assert staffs
        books.append('\\book { \\bookOutputName "' + name + '"\n\\score { \\unfoldRepeats <<\n'
                     + "\n".join(staffs) + '\n>> \\midi {} } }')
        results[name] = {"score": score, "source": source, "lily": lily, "text": _score_lines(score)}
        if name in LYRIC_CASES:
            full_lyrics.append(directory / f"{name}.ly")
    program = directory / "chords.ly"
    program.write_text("\n".join(books), encoding="utf-8")
    result = subprocess.run([str(runtime[0]), *windows_engraver_arguments(), "-dno-print-pages", "-dno-point-and-click", str(program),
                             *map(str, full_lyrics)], cwd=directory,
                            env=credential_safe_environment({"GUILE_AUTO_COMPILE": "0", "ELREN_ENGRAVER_OFFLINE": "1"}),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                            check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (directory / "lilypond.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, str(directory / "lilypond.log") + "\n" + result.stderr[-3000:]
    for name, case in results.items():
        case["midi"] = _midi(directory / f"{name}.mid")
    (directory / "observations.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


@pytest.mark.parametrize("name", CASES)
def test_individual_chord_midi_attacks_and_durations(name, converted):
    actual = converted[name]["midi"]["notes"]
    expected = sorted(CASES[name][1], key=lambda note: (note[1], note[0]))
    assert len(actual) == len(expected), (actual, expected)
    for observed, reference in zip(actual, expected):
        assert observed == pytest.approx(reference, abs=.002)


@pytest.mark.parametrize("name", ["ordinary_stacked", "all_tied_stacked"])
def test_homogeneous_chords_remain_stacked(name, converted):
    assert "NextPart" not in converted[name]["source"]


def contexts(case):
    return re.findall(r'\\new Lyrics = "[^"]+" \{ \\lyricsto "([^"]+)" \{ (.*?)\} \}', case["lily"], re.DOTALL)


@pytest.mark.parametrize("name", LYRIC_CASES)
def test_all_verses_have_real_independent_lyrics_contexts(name, converted):
    rows = contexts(converted[name])
    assert len(rows) == 2, rows
    assert rows[0][0] == rows[1][0], "Both verses must attach to the same melody voice"
    assert len([line for line in converted[name]["source"].splitlines() if line.startswith("L:")]) == 2


def test_two_verses_are_not_interleaved_in_json_text_or_compiled_lyrics(converted):
    case = converted["two_verses"]
    rows = contexts(case)
    assert re.findall(r'"([^"\n]*)"', rows[0][1]) == ["1.", "春", "天", "来", "了"]
    assert re.findall(r'"([^"\n]*)"', rows[1][1]) == ["2.", "秋", "天", "到", "了"]
    assert any("春 天 来 了" in line for line in case["text"])
    assert any("秋 天 到 了" in line for line in case["text"])
    assert case["score"]["parts"][0]["measures"][0]["notes"][0]["lyrics"] == "春"


def test_missing_verse_slots_and_named_or_two_digit_labels_survive(converted):
    rows = contexts(converted["named_and_missing"])
    assert re.findall(r'"([^"\n]*)"', rows[0][1]) == ["Chorus.", "hello", "world", ""]
    assert re.findall(r'"([^"\n]*)"', rows[1][1]) == ["10.", "", "later", "again"]


def test_multiple_text_fragments_keep_one_syllable(converted):
    rows = contexts(converted["same_syllable_fragments"])
    assert re.findall(r'"([^"\n]*)"', rows[0][1]) == ["1.", "春天"]
    assert "春 天" not in rows[0][1]


def test_syllabic_hyphens_stay_with_own_verse(converted):
    rows = contexts(converted["syllabic_verses"])
    assert '"hel" --' in rows[0][1] and '"lo"' in rows[0][1]
    assert "--" not in rows[1][1]


def test_legacy_editable_json_without_onsets_preserves_chord_durations():
    score = _parse_musicxml(CASES["partial_tie"][0])
    original = _score_to_jianpu_source(score)
    for part in score["parts"]:
        for measure in part["measures"]:
            measure.pop("onset_unit", None)
            for note in measure["notes"]:
                note.pop("onset", None)
                note.pop("lyric_verses", None)
    assert _score_to_jianpu_source(score) == original
