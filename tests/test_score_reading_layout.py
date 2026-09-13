import copy
from types import SimpleNamespace

import pytest
from test_jianpu_fidelity_midi import ROOT, _note, _xml

from deepdesk.plugins.builtin.jianpu_omr import (
    JianpuOMRTool,
    _parse_musicxml,
    _score_to_jianpu_source,
    _with_western_staff,
)
from deepdesk.score_engraving_style import apply_reading_style


def _lyric(text, number="1", extra=""):
    return f'<lyric number="{number}"><text>{text}</text>{extra}</lyric>'


@pytest.mark.parametrize("symbol", ["﹨", "\\", "/", "＼", "／"])
def test_omr_slash_only_verse_hidden_reversibly(symbol):
    score = _parse_musicxml(_xml(_note(extra=_lyric(symbol))), normalize_omr=True)
    note = score["parts"][0]["measures"][0]["notes"][0]
    assert note["lyrics"] == "" and note["lyric_verses"] == []
    assert note["omr_original_lyrics"][0]["text"] == symbol
    assert "L:" not in _score_to_jianpu_source(score)
    assert any("omr_original_lyrics" in warning for warning in score["warnings"])


@pytest.mark.parametrize("normalize,extras", [
    (False, [_lyric("﹨")]),
    (True, [_lyric("﹨"), _lyric("hello")]),
    (True, [_lyric("﹨", extra='<extend type="start"/>')]),
    (True, [_lyric("啊！")]),
    (True, [_lyric("123")]),
    (True, [_lyric("…")]),
])
def test_real_or_user_supplied_lyrics_not_filtered(normalize, extras):
    score = _parse_musicxml(_xml("".join(_note(extra=extra) for extra in extras)), normalize_omr=normalize)
    assert all("omr_original_lyrics" not in note
               for note in score["parts"][0]["measures"][0]["notes"])


def test_filter_does_not_remove_other_verse():
    score = _parse_musicxml(_xml(_note(extra=_lyric("﹨") + _lyric("春天", "2"))), normalize_omr=True)
    note = score["parts"][0]["measures"][0]["notes"][0]
    assert note["lyrics"] == "春天"
    assert note["lyric_verses"][0]["number"] == "2"


def test_reading_style_is_presentation_only_and_idempotent():
    original = '\\version "2.26.0"\n\\score { { c16 d16 e16 f16 } \\midi {} }'
    styled = apply_reading_style(original)
    assert styled.endswith(original.split("\n", 1)[1])
    assert "set-global-staff-size 24" in styled
    assert apply_reading_style(styled) == styled


def test_generated_heading_avoids_generic_solo_voice_and_large_tempo_subtitle():
    score = _parse_musicxml(_xml(_note(), tempo=True))
    score["parts"][0]["name"] = "Voice"
    snapshot = copy.deepcopy(score)
    source = _score_to_jianpu_source(score)
    assert "instrument=Voice" not in source
    assert "piece=Allegro" in source and "subtitle=Allegro" not in source
    lily, _ = JianpuOMRTool._compile_jianpu_source(source, ROOT)
    assert "% Elren reading edition:" in lily
    assert score == snapshot


def test_named_instrument_preserved():
    score = _parse_musicxml(_xml(_note()))
    score["parts"][0]["name"] = "Flute"
    assert "instrument=Flute" in _score_to_jianpu_source(score)


def test_time_signature_is_declared_after_separate_style():
    source = _score_to_jianpu_source(_parse_musicxml(_xml(_note())))
    assert source.index("SeparateTimesig") < source.index("4/4")
    lily, _ = JianpuOMRTool._compile_jianpu_source(source, ROOT)
    assert r"\mark \markup{4/4}" in lily or "4/4" in lily.split("\\score", 1)[1]


def test_western_doubling_has_no_duplicate_separate_time_signature_mark():
    source = "SeparateTimesig\n1=C\n4/4\n1 2 3 4\n3/4\n5 6 7\n"
    lily, _ = JianpuOMRTool._compile_jianpu_source(_with_western_staff(source), ROOT)
    # One score-level mark per change, owned by the numbered staff. Western
    # notation and MIDI must still receive the real meter changes.
    assert lily.count(r"\mark \markup{1=C 4/4}") == 1
    assert lily.count(r"\mark \markup{3/4}") == 1
    assert r"\mark \markup{4/4}" not in lily
    assert lily.count(r"\time 4/4") == 3
    assert lily.count(r"\time 3/4") == 3


@pytest.mark.parametrize("suffix", [".mid", ".midi"])
def test_platform_specific_midi_extension_is_returned(monkeypatch, tmp_path, suffix):
    from deepdesk.plugins.builtin import jianpu_omr

    prefix = tmp_path / "score"
    monkeypatch.setattr(JianpuOMRTool, "_lilypond_runtime", lambda _: (tmp_path / "lilypond", "test"))
    monkeypatch.setattr(JianpuOMRTool, "_prepare_lilypond_cache", lambda _: None)
    monkeypatch.setattr(JianpuOMRTool, "_compile_jianpu_source", lambda *_: ("score", "test"))

    def typeset(*args, **kwargs):
        for extension in (".pdf", ".svg", suffix):
            prefix.with_suffix(extension).write_bytes(b"fixture")
        return SimpleNamespace(returncode=0, stdout=jianpu_omr.WINDOWS_XML_READY, stderr="")

    monkeypatch.setattr(jianpu_omr.subprocess, "run", typeset)
    result = JianpuOMRTool._typeset_jianpu_source(
        "1=C\n4/4\n1 - - -", ROOT, tmp_path,
        prefix.with_suffix(".jly"), prefix.with_suffix(".ly"), prefix,
    )
    assert result[2] == prefix.with_suffix(suffix)
