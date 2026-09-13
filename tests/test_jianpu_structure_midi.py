"""Music form and pickup regression against the actual bundled open-source chain."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import re
import subprocess

import pytest
from test_jianpu_fidelity_midi import ROOT, _midi, _note
from test_jianpu_timeline_midi import attrs, document, meta_events, tempo

from deepdesk.jianpu_compiler_compat import compiler_ast
from deepdesk.lilypond_windows import windows_engraver_arguments
from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    JianpuToStaffTool,
    _parse_musicxml,
    _score_to_jianpu_source,
    _with_western_staff,
)
from deepdesk.subprocess_env import credential_safe_environment


def repeat(direction, count=2):
    return (f'<barline location="{"left" if direction == "forward" else "right"}">'
            f'<repeat direction="{direction}" times="{count}"/></barline>')


def ending(numbers, kind, backward=False):
    return (f'<barline location="{"left" if kind == "start" else "right"}">'
            f'<ending number="{numbers}" type="{kind}"/>'
            + ('<repeat direction="backward"/>' if backward else '') + '</barline>')


def notes(pitches, length=4):
    return [(pitch, index * length, length) for index, pitch in enumerate(pitches)]


XML_CASES = {
    "repeat_twice": (document(attrs() + repeat("forward") + _note(), _note("D") + repeat("backward")),
                     notes([60, 62, 60, 62])),
    "repeat_three_times": (document(attrs() + repeat("forward") + _note(), _note("D") + repeat("backward", 3)),
                           notes([60, 62, 60, 62, 60, 62])),
    "implicit_repeat_start": (document(attrs() + _note(), _note("D") + repeat("backward")),
                              notes([60, 62, 60, 62])),
    "first_second_endings": (document(attrs() + repeat("forward") + _note(),
                                      ending("1", "start") + _note("D") + ending("1", "stop", True),
                                      ending("2", "start") + _note("E") + ending("2", "discontinue")),
                             notes([60, 62, 60, 64])),
    "grouped_first_endings": (document(attrs() + repeat("forward") + _note(),
                                       ending("1,2", "start") + _note("D") + ending("1,2", "stop", True),
                                       ending("3", "start") + _note("E") + ending("3", "discontinue")),
                              notes([60, 62, 60, 62, 60, 64])),
    "three_endings": (document(attrs() + repeat("forward") + _note(),
                               ending("1", "start") + _note("D") + ending("1", "stop", True),
                               ending("2", "start") + _note("E") + ending("2", "stop", True),
                               ending("3", "start") + _note("F") + ending("3", "discontinue")),
                      notes([60, 62, 60, 64, 60, 65])),
    "multibar_endings": (document(attrs() + repeat("forward") + _note(),
                                   ending("1", "start") + _note("D"), _note("E") + ending("1", "stop", True),
                                   ending("2", "start") + _note("F"), _note("G") + ending("2", "discontinue")),
                          notes([60, 62, 64, 60, 65, 67])),
    "nested_repeats": (document(attrs() + repeat("forward") + _note(),
                                 repeat("forward") + _note("D") + repeat("backward"),
                                 _note("E") + repeat("backward")),
                        notes([60, 62, 62, 64, 60, 62, 62, 64])),
    "consecutive_repeats": (document(attrs() + repeat("forward") + _note() + repeat("backward"),
                                      repeat("forward") + _note("D") + repeat("backward")),
                             notes([60, 60, 62, 62])),
    "pickup_quarter": (document(attrs() + _note("G", 4, "quarter"), _note()).replace('<measure number="1">', '<measure number="0" implicit="yes">', 1),
                       [(67, 0, 1), (60, 1, 4)]),
    "pickup_eighth": (document(attrs() + _note("G", 2, "eighth"), _note()).replace('<measure number="1">', '<measure number="0" implicit="yes">', 1),
                      [(67, 0, .5), (60, .5, 4)]),
    "pickup_dotted": (document(attrs() + _note("G", 6, "quarter", dots=1), _note()).replace('<measure number="1">', '<measure number="0" implicit="yes">', 1),
                      [(67, 0, 1.5), (60, 1.5, 4)]),
    "pickup_complementary_end": (document(attrs() + _note("G", 4, "quarter"), _note(), _note("D", 12, "half", dots=1))
                                  .replace('<measure number="1">', '<measure number="0" implicit="yes">', 1)
                                  .replace('<measure number="3">', '<measure number="2" implicit="yes">', 1),
                                 [(67, 0, 1), (60, 1, 4), (62, 5, 3)]),
    "pickup_repeat_complement": (document(attrs() + repeat("forward") + _note("G", 4, "quarter"),
                                          _note("C", 12, "half", dots=1) + repeat("backward"))
                                  .replace('<measure number="1">', '<measure number="0" implicit="yes">', 1)
                                  .replace('<measure number="2">', '<measure number="1" implicit="yes">', 1),
                                 [(67, 0, 1), (60, 1, 3), (67, 4, 1), (60, 5, 3)]),
    "implicit_full_is_not_pickup": (document(attrs() + _note(), _note("D"))
                                    .replace('<measure number="1">', '<measure number="0" implicit="yes">', 1),
                                   notes([60, 62])),
    "repeat_tempo_changes": (document(attrs() + repeat("forward") + tempo(120) + _note(), tempo(90) + _note("D") + repeat("backward")),
                             notes([60, 62, 60, 62])),
    "repeat_key_changes": (document(attrs() + repeat("forward") + _note(), attrs(1) + _note("G") + repeat("backward")),
                           notes([60, 67, 60, 67])),
    "nested_repeat_key_changes": (document(attrs() + repeat("forward") + _note(),
                                            attrs(1) + repeat("forward") + _note("G") + repeat("backward"),
                                            attrs(2) + _note("D") + repeat("backward")),
                                  notes([60, 67, 67, 62, 60, 67, 67, 62])),
    "volta_key_changes": (document(attrs() + repeat("forward") + _note(),
                                    attrs(1) + ending("1", "start") + _note("G") + ending("1", "stop", True),
                                    attrs(2) + ending("2", "start") + _note("D") + ending("2", "discontinue")),
                          notes([60, 67, 60, 62])),
    "polyphonic_pickup": (document(attrs() + _note("G", 4, "quarter")
                                    + '<backup><duration>4</duration></backup><forward><duration>2</duration></forward>'
                                    + _note("C", 2, "eighth").replace('<voice>1</voice>', '<voice>2</voice>'),
                                    _note("D") + '<backup><duration>16</duration></backup>'
                                    + _note("E").replace('<voice>1</voice>', '<voice>2</voice>'))
                           .replace('<measure number="1">', '<measure number="0" implicit="yes">', 1),
                          [(67, 0, 1), (60, .5, .5), (62, 1, 4), (64, 1, 4)]),
}


BASE = "1=C\n4/4\n1 - - -"
SECOND = "1=C\n4/4\n3 - - -"
JLY_CASES = {
    "single_newline": (BASE + "\nNextPart\n" + SECOND, [(60, 0, 4), (64, 0, 4)]),
    "inline_separator": (BASE + " NextPart\n" + SECOND, [(60, 0, 4), (64, 0, 4)]),
    "tabs_separator": (BASE + "\tNextPart\t" + SECOND, [(60, 0, 4), (64, 0, 4)]),
    "existing_inline_withstaff": (BASE.replace("1 -", "WithStaff 1 -", 1) + " NextPart\n" + SECOND, [(60, 0, 4), (64, 0, 4)]),
    "commented_separator": (BASE + "\n% NextPart\n", [(60, 0, 4)]),
}


@pytest.fixture(scope="module")
def structure_midi(tmp_path_factory):
    runtime = JianpuOMRTool._lilypond_runtime(ROOT)
    if runtime is None or JianpuOMRTool._jianpu_runtime(ROOT) is None:
        pytest.skip("Bundled notation runtime unavailable")
    executable, _ = runtime
    if not executable.is_relative_to(ROOT / "work" / "tool-runtime"):
        pytest.skip("Requires package-local notation runtime")
    output = tmp_path_factory.mktemp("structure-midi")
    books = ['\\version "2.26.0"']
    results = {}
    for name, (input_text, _) in {**XML_CASES, **JLY_CASES}.items():
        path = output / (name + (".json" if name in XML_CASES else ".jly"))
        score = _parse_musicxml(input_text) if name in XML_CASES else None
        path.write_text(json.dumps(score) if score else input_text, encoding="utf-8")
        source = JianpuToStaffTool._source_from_input(path)
        wrapped = _with_western_staff(source)
        with contextlib.redirect_stderr(io.StringIO()):
            lily, _ = JianpuOMRTool._compile_jianpu_source(wrapped, ROOT)
        staffs = re.findall(r"BEGIN MIDI STAFF ===(.*?)% === END MIDI STAFF", lily, re.DOTALL)
        assert staffs
        books.append('\\book { \\bookOutputName "' + name + '"\n\\score { \\unfoldRepeats <<\n'
                     + "\n".join(staffs) + '\n>> \\midi {} } }')
        results[name] = {"score": score, "source": source, "wrapped": wrapped, "lily": lily}
    program = output / "structure.ly"
    program.write_text("\n".join(books), encoding="utf-8")
    result = subprocess.run([str(executable), *windows_engraver_arguments(), "-dno-print-pages", "-dno-point-and-click", str(program)],
                            cwd=output, env=credential_safe_environment({"GUILE_AUTO_COMPILE": "0", "ELREN_ENGRAVER_OFFLINE": "1"}),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (output / "lilypond.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, output / "lilypond.log"
    for name, case in results.items():
        case["midi"] = _midi(output / f"{name}.mid")
        case["events"] = meta_events(output / f"{name}.mid")
    (output / "observations.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


@pytest.mark.parametrize("name", [*XML_CASES, *JLY_CASES])
def test_actual_repeats_pickup_and_reverse_input(name, structure_midi):
    assert structure_midi[name]["midi"]["notes"] == {**XML_CASES, **JLY_CASES}[name][1]


@pytest.mark.parametrize("name,partial", [("pickup_quarter", "4"), ("pickup_eighth", "8"), ("pickup_dotted", "4.")])
def test_pickup_preserves_actual_compiled_bar_structure(name, partial, structure_midi):
    case = structure_midi[name]
    assert f"4/4,{partial}" in case["source"]
    staff = re.search(r"BEGIN MIDI STAFF ===(.*?)% === END MIDI STAFF", case["lily"], re.DOTALL)[1]
    assert f"\\partial {partial}" in staff
    assert re.search(r"\|\s+c'1", re.sub(r"%\{.*?%\}", "", staff)), staff


@pytest.mark.parametrize("name", JLY_CASES)
def test_every_real_reverse_part_receives_staff(name, structure_midi):
    case = structure_midi[name]
    expected = len(JLY_CASES[name][1])
    assert case["lily"].count("BEGIN 5-LINE STAFF") == expected
    assert case["wrapped"].count("WithStaff") == expected


def test_nextscore_is_wrapped_without_merging_movements():
    source = BASE + " NextScore\n" + SECOND
    with contextlib.redirect_stderr(io.StringIO()):
        lily, _ = JianpuOMRTool._compile_jianpu_source(_with_western_staff(source), ROOT)
    assert lily.count("BEGIN 5-LINE STAFF") == 2
    assert lily.count("\\unfoldRepeats") == 2


def test_full_implicit_measure_does_not_become_pickup(structure_midi):
    assert "\\partial" not in structure_midi["implicit_full_is_not_pickup"]["lily"]


def test_repeat_restores_initial_tempo_on_every_pass(structure_midi):
    assert structure_midi["repeat_tempo_changes"]["events"][0] == [(0, 120), (4, 90), (8, 120), (12, 90)]


def test_compatibility_does_not_modify_pinned_open_source_file():
    path = ROOT / "work/tool-runtime/native/jianpu-ly/jianpu_ly.py"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == "0890ed37035cf61a218bcf259153079ff12add7a6be8cde6a3dc493ecdab807c"
    compiler_ast(path.read_text(encoding="utf-8"), str(path))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_compatibility_refuses_an_unknown_upstream_shape():
    with pytest.raises(RuntimeError, match="not recognized"):
        compiler_ast("def getLY(score):\n    return score\n", "synthetic.py")


def test_mac_font_fallback_uses_lilypond_226_property_syntax():
    path = ROOT / "deepdesk/vendor/jianpu_ly/__init__.py"
    tree = compiler_ast(path.read_text(encoding="utf-8"), str(path))
    strings = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not any("(set-global-fonts" in value for value in strings)
    assert any('property-defaults.fonts.serif = "Source Serif Pro,Source Han Serif SC,Times New Roman,Arial Unicode MS"'
               in value for value in strings)


def test_zero_length_ending_fails_instead_of_looping():
    xml = document(attrs() + repeat("forward") + _note(),
                   ending("1", "start") + _note("D") + ending("1", "stop", True),
                   ending("2", "start")
                   + '<barline location="left"><ending number="2" type="discontinue"/></barline>' + _note("E"))
    with pytest.raises(ValueError, match="positive measure extent"):
        _score_to_jianpu_source(_parse_musicxml(xml))


def test_matching_shape_with_modified_release_is_refused():
    path = ROOT / "work/tool-runtime/native/jianpu-ly/jianpu_ly.py"
    with pytest.raises(RuntimeError, match="release hash"):
        compiler_ast(path.read_text(encoding="utf-8") + "\n# different source\n", "changed.py")
