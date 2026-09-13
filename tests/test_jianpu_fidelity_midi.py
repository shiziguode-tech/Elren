"""Semantic regression tests against the actual pinned jianpu-ly/LilyPond chain.

No OMR, API, app or fonts/rendering. The integration
fixture writes only pytest's isolated directory and emits MIDI-only score blocks.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import struct
import subprocess
from pathlib import Path

import pytest

from deepdesk.lilypond_windows import windows_engraver_arguments
from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    JianpuToStaffTool,
    _parse_musicxml,
    _score_to_jianpu_source,
    _with_western_staff,
)
from deepdesk.subprocess_env import credential_safe_environment

ROOT = Path(__file__).resolve().parents[1]


def _note(step="C", duration=16, kind="whole", *, octave=4, dots=0, extra="", staff=1):
    before_staff, notation_start, after_staff = extra.partition("<notations>")
    return (f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            f"<duration>{duration}</duration><voice>1</voice>"
            + (f"<type>{kind}</type>" if kind else "") + "<dot/>" * dots
            + before_staff + f"<staff>{staff}</staff>" + notation_start + after_staff + "</note>")


def _xml(notes, *, fifths=0, mode="major", divisions=4, beats=4, unit=4, transpose="", tempo=False):
    direction = ("<direction><direction-type><words>Allegro</words><metronome>"
                 "<beat-unit>quarter</beat-unit><per-minute>120</per-minute>"
                 "</metronome></direction-type></direction>") if tempo else ""
    return ('<score-partwise version="4.0"><work><work-title>第三首</work-title></work>'
            '<part-list><score-part id="P1"><part-name>Audit</part-name></score-part></part-list>'
            '<part id="P1"><measure number="1"><attributes>'
            f"<divisions>{divisions}</divisions><key><fifths>{fifths}</fifths><mode>{mode}</mode></key>"
            f"<time><beats>{beats}</beats><beat-type>{unit}</beat-type></time>"
            "<clef><sign>G</sign><line>2</line></clef>" + transpose + "</attributes>"
            + direction + notes + "</measure></part></score-partwise>")


def _mod(actual=3, normal=2):
    return (f"<time-modification><actual-notes>{actual}</actual-notes>"
            f"<normal-notes>{normal}</normal-notes></time-modification>")


def _transpose(chromatic, *, diatonic=None, octave=0, number=""):
    return ('<transpose' + (f' number="{number}"' if number else '') + '>'
            + (f"<diatonic>{diatonic}</diatonic>" if diatonic is not None else "")
            + f"<chromatic>{chromatic}</chromatic><octave-change>{octave}</octave-change></transpose>")


def _cases():
    cases = {}
    for mode in ("major", "minor"):
        for fifths in range(-7, 8):
            cases[f"key_{mode}_{fifths:+d}".replace("+", "p").replace("-", "m")] = (
                _xml(_note(), fifths=fifths, mode=mode), [(60, 0, 4)])
    cases["minor_tonic_a4"] = (_xml(_note("A"), mode="minor"), [(69, 0, 4)])
    cases["g_major_tonic_g4"] = (_xml(_note("G"), fifths=1), [(67, 0, 4)])
    cases["three_lower_octave_marks"] = (_xml(_note(octave=1)), [(24, 0, 4)])
    cases["three_upper_octave_marks"] = (_xml(_note(octave=7)), [(96, 0, 4)])
    cases["triplets"] = (_xml("".join(_note(step, 4, "eighth", extra=_mod()) for step in "CDE")
                                      + _note("F", 36, "half", dots=1), divisions=12),
                          [(60, 0, 1/3), (62, 1/3, 1/3), (64, 2/3, 1/3), (65, 1, 3)])
    cases["quintuplets"] = (_xml("".join(_note(step, 2, "eighth", extra=_mod(5, 4)) for step in "CDEFG"),
                                        divisions=5, beats=2), [(pitch, i * .4, .4) for i, pitch in enumerate((60, 62, 64, 65, 67))])
    cases["duplets"] = (_xml(_note("C", 3, "eighth", extra=_mod(2, 3))
                                  + _note("D", 3, "eighth", extra=_mod(2, 3)), beats=3, unit=8),
                         [(60, 0, .75), (62, .75, .75)])
    cases["omitted_type_dotted_sixteenths"] = (_xml("".join(_note(step, 3, None) for step in "CDEG"),
                                                      divisions=8, beats=3, unit=8),
                                              [(pitch, i * .375, .375) for i, pitch in enumerate((60, 62, 64, 67))])
    cases["double_dotted_half"] = (_xml(_note("C", 14, "half", dots=2) + _note("D", 2, "eighth")),
                                     [(60, 0, 3.5), (62, 3.5, .5)])
    cases["double_dotted_whole"] = (_xml(_note("C", 28, "whole", dots=2), beats=7), [(60, 0, 7)])
    cases["control_explicit_sixteenths_title_tempo"] = (
        _xml("".join(_note(step, 1, "16th") for step in "CDEG")
             + _note("A", 3, "eighth", dots=1) + _note("B", 1, "16th"), beats=2, tempo=True),
        [(60, 0, .25), (62, .25, .25), (64, .5, .25), (67, .75, .25), (69, 1, .75), (71, 1.75, .25)])
    cases["bflat_instrument"] = (_xml(_note(), transpose=_transpose(-2, diatonic=-1)), [(58, 0, 4)])
    cases["f_instrument"] = (_xml(_note(), transpose=_transpose(-7, diatonic=-4)), [(53, 0, 4)])
    cases["octave_instrument"] = (_xml(_note(), transpose=_transpose(0, octave=-1)), [(48, 0, 4)])
    cases["bflat_octave_instrument"] = (_xml(_note(), transpose=_transpose(-2, diatonic=-1, octave=-1)), [(46, 0, 4)])
    cases["written_outside_but_sounding_inside_range"] = (_xml(_note(octave=8), transpose=_transpose(0, octave=-1)), [(96, 0, 4)])
    cases["staff_specific_transposition"] = (_xml(_note(duration=8, kind="half", staff=1)
                                                    + _note(duration=8, kind="half", staff=2),
                                                    transpose=_transpose(-2, diatonic=-1, number="1")), [(58, 0, 2), (60, 2, 2)])
    return cases


CASES = _cases()


def _vlq(data, offset):
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value = value * 128 + (byte & 127)
        if byte < 128:
            return value, offset


def _midi(path):
    data = path.read_bytes()
    assert data[:4] == b"MThd"
    _, tracks, ppq = struct.unpack(">HHH", data[8:14])
    assert not ppq & 0x8000
    cursor = 8 + int.from_bytes(data[4:8], "big")
    notes, tempos = [], []
    for _ in range(tracks):
        assert data[cursor:cursor+4] == b"MTrk"
        size = int.from_bytes(data[cursor+4:cursor+8], "big")
        chunk = data[cursor+8:cursor+8+size]
        cursor += 8 + size
        offset, tick, running, active = 0, 0, None, {}
        while offset < len(chunk):
            delta, offset = _vlq(chunk, offset)
            tick += delta
            status = chunk[offset]
            if status & 128:
                offset += 1
                if status < 240:
                    running = status
            else:
                status = running
            if status == 255:
                kind = chunk[offset]
                length, offset = _vlq(chunk, offset + 1)
                payload = chunk[offset:offset+length]
                offset += length
                if kind == 81:
                    tempos.append(60_000_000 / int.from_bytes(payload, "big"))
            elif status in (240, 247):
                length, offset = _vlq(chunk, offset)
                offset += length
            else:
                command, channel = status & 240, status & 15
                count = 1 if command in (192, 208) else 2
                payload = chunk[offset:offset+count]
                offset += count
                if command == 144 and payload[1]:
                    active[(channel, payload[0])] = tick
                elif command == 128 or (command == 144 and not payload[1]):
                    start = active.pop((channel, payload[0]))
                    notes.append((payload[0], start / ppq, (tick-start) / ppq))
    return {"notes": sorted(notes, key=lambda note: (note[1], note[0])), "tempo": tempos, "ppq": ppq}


@pytest.fixture(scope="module")
def rendered_midi(tmp_path_factory):
    runtime = JianpuOMRTool._lilypond_runtime(ROOT)
    if runtime is None or JianpuOMRTool._jianpu_runtime(ROOT) is None:
        pytest.skip("Bundled open-source notation runtime is unavailable on this platform")
    executable, _mode = runtime
    if not executable.is_relative_to(ROOT / "work" / "tool-runtime"):
        pytest.skip("This regression requires the package-local LilyPond runtime")
    # Match the app's portable extraction preparation; ZIP timestamps can make
    # Guile ignore shipped bytecode and interpret its entire library otherwise.
    JianpuOMRTool._prepare_lilypond_cache(executable)
    output = tmp_path_factory.mktemp("music-midi")
    books = ['\\version "2.26.0"']
    compiled = {}
    for name, (document, _) in CASES.items():
        score = _parse_musicxml(document)
        input_path = output / f"{name}.json"
        input_path.write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
        source = JianpuToStaffTool._source_from_input(input_path)
        warnings = io.StringIO()
        with contextlib.redirect_stderr(warnings):
            lily, _ = JianpuOMRTool._compile_jianpu_source(_with_western_staff(source), ROOT)
        staffs = re.findall(r"BEGIN MIDI STAFF ===(.*?)% === END MIDI STAFF", lily, re.DOTALL)
        assert staffs, name
        body = "\n".join(staffs)
        books.append('\\book { \\bookOutputName "' + name + '"\n\\score { \\unfoldRepeats <<\n'
                     + body + '\n>> \\midi {} } }')
        compiled[name] = {"source": source, "lily": lily, "score": score, "compiler_warnings": warnings.getvalue()}
    program = output / "regression.ly"
    program.write_text("\n".join(books), encoding="utf-8")
    completed = subprocess.run([str(executable), *windows_engraver_arguments(), "-dno-print-pages", "-dno-point-and-click", str(program)],
                               cwd=output, env=credential_safe_environment({"GUILE_AUTO_COMPILE": "0", "ELREN_ENGRAVER_OFFLINE": "1"}),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    (output / "lilypond.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, f"LilyPond failed; isolated log: {output / 'lilypond.log'}"
    result = {name: {**compiled[name], "midi": _midi(output / f"{name}.mid")} for name in CASES}
    (output / "observations.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@pytest.mark.parametrize("name", CASES)
def test_actual_midi_preserves_musicxml_pitches_and_duration(name, rendered_midi):
    actual = rendered_midi[name]["midi"]
    expected = CASES[name][1]
    assert len(actual["notes"]) == len(expected)
    for observed, intended in zip(actual["notes"], expected):
        assert observed[0] == intended[0]
        assert observed[1:] == pytest.approx(intended[1:], abs=1 / actual["ppq"])


def test_title_tempo_and_explicit_sixteenths_remain_correct(rendered_midi):
    case = rendered_midi["control_explicit_sixteenths_title_tempo"]
    assert "title=第三首" in case["source"] and 'title="第三首"' in case["lily"]
    assert "s1 s2 s3 s5 q6. s7" in case["source"]
    assert case["midi"]["tempo"] == [120]


def test_transposing_instrument_preserves_written_and_concert_contract(rendered_midi):
    case = rendered_midi["bflat_instrument"]
    score = case["score"]
    measure = score["parts"][0]["measures"][0]
    note = measure["notes"][0]
    assert score["pitch_basis"] == "concert"
    assert note["written_midi"] == 60 and note["written_pitch"] == "C4" and note["source_pitch"] == "C4"
    assert note["midi"] == 58 and note["token"] == "1"
    assert measure["written_key"]["tonic"] == "C" and measure["key"]["tonic"] == "B♭"
    assert "1=Bb" in case["source"]


def test_missing_type_infers_dotted_sixteenth_not_eighth(rendered_midi):
    case = rendered_midi["omitted_type_dotted_sixteenths"]
    assert "s1. s2. s3. s5." in case["source"]
    assert all(note["beams"] == 2 for note in case["score"]["parts"][0]["measures"][0]["notes"])


def test_explicit_tuplet_boundaries_are_kept():
    notes = ""
    for group in range(2):
        for index, step in enumerate("CDE"):
            mark = "start" if index == 0 else "stop" if index == 2 else ""
            notation = f'<notations><tuplet type="{mark}"/></notations>' if mark else ""
            notes += _note(step, 4, "eighth", extra=_mod() + notation)
    source = _score_to_jianpu_source(_parse_musicxml(_xml(notes, divisions=12, beats=2)))
    assert source.count("3[") == 2 and source.count("]") == 2


def test_unsupported_duration_is_rejected_instead_of_silently_changed():
    with pytest.raises(ValueError, match="tuplet ratio"):
        _score_to_jianpu_source(_parse_musicxml(_xml(_note(duration=4, kind="quarter", extra=_mod(9, 4)), divisions=9)))


def test_unit_time_modification_does_not_change_plain_note():
    source = _score_to_jianpu_source(_parse_musicxml(_xml(_note(extra=_mod(1, 1)))))
    assert "1 - - -" in source and "1[" not in source


@pytest.mark.parametrize("transpose", [_transpose("0.5"), "<transpose><chromatic>0</chromatic><double/></transpose>"])
def test_unrepresentable_instrument_transposition_is_rejected(transpose):
    with pytest.raises(ValueError, match="Microtonal or doubled"):
        _parse_musicxml(_xml(_note(), transpose=transpose))


def test_unsupported_octave_is_not_clipped():
    with pytest.raises(ValueError, match="refusing to clip"):
        _parse_musicxml(_xml(_note(octave=8)))


@pytest.mark.parametrize("alteration", ["0.5", "-0.5", "1.5"])
def test_microtonal_note_is_not_silently_rounded(alteration):
    document = _xml(_note()).replace("<step>C</step>", f"<step>C</step><alter>{alteration}</alter>")
    with pytest.raises(ValueError, match="Microtonal note pitches"):
        _parse_musicxml(document)
