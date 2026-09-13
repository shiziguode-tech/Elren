"""Musical-event regression, using the pinned offline compiler and real MIDI."""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
from pathlib import Path

import pytest
from test_jianpu_fidelity_midi import ROOT, _midi, _note, _vlq

from deepdesk.lilypond_windows import windows_engraver_arguments
from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    JianpuToStaffTool,
    _parse_musicxml,
    _render_engraved_html,
    _render_html,
    _score_to_jianpu_source,
    _with_western_staff,
)
from deepdesk.subprocess_env import credential_safe_environment


def attrs(fifths=0, beats=4, unit=4, divisions=4, extra=""):
    return (f"<attributes><divisions>{divisions}</divisions><key><fifths>{fifths}</fifths></key>"
            f"<time><beats>{beats}</beats><beat-type>{unit}</beat-type></time>{extra}</attributes>")


def tempo(value=120, dots=0, unit="quarter", offset="", sound=""):
    return ("<direction><direction-type><metronome>"
            f"<beat-unit>{unit}</beat-unit>" + "<beat-unit-dot/>" * dots
            + (f"<per-minute>{value}</per-minute>" if value else "")
            + "</metronome></direction-type>" + offset + sound + "</direction>")


def document(*measures):
    return ('<score-partwise version="4.0"><part-list><score-part id="P1">'
            '<part-name>Timeline</part-name></score-part></part-list><part id="P1">'
            + "".join(f'<measure number="{i}">{body}</measure>' for i, body in enumerate(measures, 1))
            + "</part></score-partwise>")


def cases():
    return {
        "key_time_tempo": (document(attrs() + tempo() + _note(),
                                     attrs(2, 3) + tempo(90) + _note("D", 12, "half", dots=1)),
                           [(60, 0, 4), (62, 4, 3)], [(0, 120), (4, 90)], [(0, "4/4"), (4, "3/4")]),
        "key_return": (document(attrs() + _note(), attrs(1) + _note("G"), attrs() + _note()),
                       [(60, 0, 4), (67, 4, 4), (60, 8, 4)], [], [(0, "4/4")]),
        "mid_bar_key": (document(attrs() + _note("C", 8, "half")
                                 + '<attributes><key><fifths>2</fifths></key></attributes>' + _note("D", 8, "half")),
                        [(60, 0, 2), (62, 2, 2)], [], [(0, "4/4")]),
        "dotted_tempo": (document(attrs(beats=6, unit=8) + tempo(80, 1) + _note("C", 12, "half", dots=1)),
                         [(60, 0, 3)], [(0, 120)], [(0, "6/8")]),
        "double_dotted_tempo": (document(attrs() + tempo(80, 2) + _note()),
                                [(60, 0, 4)], [(0, 140)], [(0, "4/4")]),
        "sound_quarter_not_visual_dot": (document(attrs() + tempo(None, 1, "half", sound='<sound tempo="120"/>') + _note()),
                                         [(60, 0, 4)], [(0, 120)], [(0, "4/4")]),
        "direct_sound_tempo": (document(attrs() + '<sound tempo="120"/>' + _note("C", 8, "half")
                                         + '<sound tempo="90"/>' + _note("D", 8, "half")),
                               [(60, 0, 2), (62, 2, 2)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "slow_tempo_not_clamped": (document(attrs() + tempo(10) + _note()), [(60, 0, 4)], [(0, 10)], [(0, "4/4")]),
        "fast_tempo_not_clamped": (document(attrs() + tempo(450) + _note()), [(60, 0, 4)], [(0, 450)], [(0, "4/4")]),
        "tempo_between_notes": (document(attrs() + tempo() + _note("C", 8, "half") + tempo(90) + _note("D", 8, "half")),
                                [(60, 0, 2), (62, 2, 2)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "offset_inside_sustain": (document(attrs() + tempo() + tempo(90, offset='<offset>8</offset>') + _note()),
                                  [(60, 0, 4)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "negative_offset": (document(attrs() + tempo() + _note() + tempo(90, offset='<offset>-8</offset>')),
                            [(60, 0, 4)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "explicit_sound_offset": (document(attrs() + tempo() + tempo(90, offset='<offset sound="yes">8</offset>', sound='<sound tempo="90"/>') + _note()),
                                  [(60, 0, 4)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "forward_second_voice": (document(attrs(beats=2) + _note("C", 8, "half")
                                           + '<backup><duration>8</duration></backup><forward><duration>4</duration><voice>2</voice></forward>'
                                           + _note("G", 4, "quarter").replace('<voice>1</voice>', '<voice>2</voice>')),
                                 [(60, 0, 2), (67, 1, 1)], [], [(0, "2/4")]),
        "internal_gap": (document(attrs() + _note("C", 4, "quarter") + '<forward><duration>4</duration></forward>' + _note("D", 8, "half")),
                         [(60, 0, 1), (62, 2, 2)], [], [(0, "4/4")]),
        "tempo_in_gap": (document(attrs() + _note("C", 4, "quarter") + tempo(90, offset='<offset>4</offset>')
                                  + '<forward><duration>8</duration></forward>' + _note("D", 4, "quarter")),
                         [(60, 0, 1), (62, 3, 1)], [(0, 60), (2, 90)], [(0, "4/4")]),
        "polyphonic_tempo": (document(attrs() + tempo() + _note("C", 8, "half") + tempo(90) + _note("D", 8, "half")
                                       + '<backup><duration>16</duration></backup>'
                                       + _note("G").replace('<voice>1</voice>', '<voice>2</voice>')),
                             [(60, 0, 2), (67, 0, 4), (62, 2, 2)], [(0, 120), (2, 90)], [(0, "4/4")]),
        "divisions_change": (document(attrs() + _note("C", 8, "half")
                                       + '<attributes><divisions>8</divisions></attributes>' + _note("D", 16, "half")),
                             [(60, 0, 2), (62, 2, 2)], [], [(0, "4/4")]),
        "polyphonic_key": (document(attrs() + _note("C", 8, "half")
                                     + '<attributes><key><fifths>2</fifths></key></attributes>' + _note("D", 8, "half")
                                     + '<backup><duration>16</duration></backup>'
                                     + (_note("G", 8, "half") + _note("A", 8, "half")).replace('<voice>1</voice>', '<voice>2</voice>')),
                           [(60, 0, 2), (67, 0, 2), (62, 2, 2), (69, 2, 2)], [], [(0, "4/4")]),
    }


CASES = cases()


def meta_events(path):
    data = path.read_bytes()
    ppq = int.from_bytes(data[12:14], "big")
    cursor = 8 + int.from_bytes(data[4:8], "big")
    tempos, times = set(), set()
    while cursor < len(data):
        size = int.from_bytes(data[cursor + 4:cursor + 8], "big")
        chunk = data[cursor + 8:cursor + 8 + size]
        cursor += 8 + size
        offset, tick, running = 0, 0, None
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
                payload = chunk[offset:offset + length]
                offset += length
                if kind == 81:
                    tempos.add((tick / ppq, round(60_000_000 / int.from_bytes(payload, "big"), 3)))
                elif kind == 88:
                    times.add((tick / ppq, f"{payload[0]}/{2 ** payload[1]}"))
            elif status in (240, 247):
                length, offset = _vlq(chunk, offset)
                offset += length
            else:
                offset += 1 if status & 240 in (192, 208) else 2
    return sorted(tempos), sorted(times)


@pytest.fixture(scope="module")
def timelines(tmp_path_factory):
    runtime = JianpuOMRTool._lilypond_runtime(ROOT)
    if runtime is None or JianpuOMRTool._jianpu_runtime(ROOT) is None:
        pytest.skip("Bundled notation engine unavailable")
    executable, _ = runtime
    if not executable.is_relative_to(ROOT / "work" / "tool-runtime"):
        pytest.skip("Requires package-local engine")
    output = tmp_path_factory.mktemp("timeline-midi")
    books = ['\\version "2.26.0"']
    results = {}
    for name, (xml, *_) in CASES.items():
        score = _parse_musicxml(xml)
        path = output / f"{name}.json"
        path.write_text(json.dumps(score), encoding="utf-8")
        source = JianpuToStaffTool._source_from_input(path)
        with contextlib.redirect_stderr(io.StringIO()):
            lily, _ = JianpuOMRTool._compile_jianpu_source(_with_western_staff(source), ROOT)
        staffs = re.findall(r"BEGIN MIDI STAFF ===(.*?)% === END MIDI STAFF", lily, re.DOTALL)
        assert staffs
        books.append('\\book { \\bookOutputName "' + name + '"\n\\score { <<\n'
                     + "\n".join(staffs) + '\n>> \\midi {} } }')
        results[name] = {"score": score, "source": source, "lily": lily}
    program = output / "timeline.ly"
    program.write_text("\n".join(books), encoding="utf-8")
    result = subprocess.run([str(executable), *windows_engraver_arguments(), "-dno-print-pages", "-dno-point-and-click", str(program)],
                            cwd=output, env=credential_safe_environment({"GUILE_AUTO_COMPILE": "0", "ELREN_ENGRAVER_OFFLINE": "1"}),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (output / "lilypond.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, output / "lilypond.log"
    for name in CASES:
        path = output / f"{name}.mid"
        results[name].update(midi=_midi(path), events=meta_events(path))
    (output / "observations.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


@pytest.mark.parametrize("name", CASES)
def test_real_midi_preserves_timeline(name, timelines):
    _, notes, tempos, times = CASES[name]
    actual = timelines[name]
    assert actual["midi"]["notes"] == notes
    assert actual["events"][1] == times
    if tempos:
        assert len(actual["events"][0]) == len(tempos)
        for observed, expected in zip(actual["events"][0], tempos):
            assert observed[0] == expected[0]
            # Standard MIDI stores integral microseconds/quarter, not exact BPM.
            assert observed[1] == pytest.approx(expected[1], abs=expected[1] ** 2 / 60_000_000 + .00051)


def test_dotted_display_and_json_agree(timelines):
    score = timelines["dotted_tempo"]["score"]
    assert score["tempo"]["beat_unit_dots"] == 1
    assert "4.=80" in timelines["dotted_tempo"]["source"]
    assert "♩·=80" in _render_html(score)
    assert "♩·=80" in _render_engraved_html(score, [], Path("synthetic.pdf"))


def test_delayed_tempo_is_not_promoted_to_start(timelines):
    assert timelines["tempo_in_gap"]["score"]["tempo"] is None


@pytest.mark.parametrize("body,match", [
    (attrs() + '<backup><duration>4</duration></backup>' + _note(), "before the start"),
    (attrs() + tempo(90, offset='<offset>-4</offset>') + _note(), "before its measure"),
    (attrs() + tempo(90, offset='<offset>20</offset>') + _note(), "beyond the end"),
    (attrs() + tempo(90, offset='<offset>4</offset>', sound='<sound tempo="90"/>') + _note(), "Separate visual"),
    (attrs() + _note("C", 8, "half") + attrs(beats=3) + _note("D", 4, "quarter"), "time-signature change inside"),
    (attrs() + tempo("90.5") + _note(), "represented exactly"),
    (attrs() + tempo("90.0001") + _note(), "represented exactly"),
])
def test_unsupported_timeline_is_explicit_not_silent(body, match):
    with pytest.raises(ValueError, match=match):
        _score_to_jianpu_source(_parse_musicxml(document(body)))


def test_forward_gap_is_not_marked_as_missing_duration(timelines):
    notes = timelines["forward_second_voice"]["score"]["parts"][0]["measures"][0]["notes"]
    assert [note["onset"] for note in notes] == [0, 1]
    assert all(note["risk"] == "low" for note in notes)


def test_sustained_key_change_in_other_voice_is_not_silently_repitched():
    xml = document(attrs() + _note("C", 8, "half")
                   + '<attributes><key><fifths>2</fifths></key></attributes>' + _note("D", 8, "half")
                   + '<backup><duration>16</duration></backup>'
                   + _note("G").replace('<voice>1</voice>', '<voice>2</voice>'))
    with pytest.raises(ValueError, match="sustained key change"):
        _score_to_jianpu_source(_parse_musicxml(xml))
