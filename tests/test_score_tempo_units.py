import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from deepdesk.plugins.builtin.jianpu_omr import (
    _direction_payload,
    _merge_recognized_tempo,
    _render_engraved_html,
)
from deepdesk.score_title_ocr import select_tempo


def recognize(text):
    return select_tempo([{
        "text": text, "confidence": 0.98,
        "box": [[0, 0], [100, 0], [100, 20], [0, 20]],
    }], 1000)


@pytest.mark.parametrize("text,unit,dots,bpm", [
    ("Allegro ♩ = 120", "quarter", 0, 120),
    ("Andante ♪· = 60", "eighth", 1, 60),
    ("𝅗𝅥. = 72.5", "half", 1, 72.5),
    ("𝅘𝅥𝅯 = 96", "16th", 0, 96),
    ("𝅘𝅥 · · = 80", "quarter", 2, 80),
    ("Largo ♩ = 18", "quarter", 0, 18),
])
def test_explicit_metronome_units_and_dots(text, unit, dots, bpm):
    result = recognize(text)
    assert result["tempo"] == bpm
    assert result["tempo_beat_unit"] == unit
    assert result["tempo_beat_unit_dots"] == dots


def test_missing_glyph_is_not_an_observed_quarter_note():
    result = recognize("Allegro = 120")
    assert result["tempo"] == 120
    assert result["tempo_beat_unit"] is None
    assert result["tempo_needs_confirmation"] is True


def test_unknown_unit_does_not_create_a_metronome_mark():
    score = {"tempo": None}
    _merge_recognized_tempo(score, recognize("Allegro = 120"))
    assert score["tempo"].get("tempo") is None
    assert score["tempo"].get("beat_unit") is None
    assert score["tempo"]["words"] == "Allegro"
    assert score["tempo"]["unresolved_metronome"]["value"] == 120
    assert score["warnings"]


def test_ocr_missing_glyph_preserves_existing_musicxml_unit_and_dots():
    score = {"tempo": {"tempo": 2, "beat_unit": "half", "beat_unit_dots": 1}}
    _merge_recognized_tempo(score, recognize("Allegro = 120"), omr_generated=True)
    assert score["tempo"]["tempo"] == 120
    assert score["tempo"]["beat_unit"] == "half"
    assert score["tempo"]["beat_unit_dots"] == 1
    assert score["tempo"]["beat_unit_source"] == "musicxml"


def test_initial_omr_direction_supplies_missing_glyph_without_losing_dots():
    initial = {"words": "Allegro", "tempo": 2, "beat_unit": "quarter",
               "beat_unit_dots": 1, "onset": 0.25}
    score = {"tempo": None, "parts": [{"measures": [{"directions": [initial]}]}]}
    _merge_recognized_tempo(score, recognize("Allegro = 120"), omr_generated=True)
    assert score["tempo"]["beat_unit_dots"] == initial["beat_unit_dots"] == 1
    assert initial["tempo"] == 120
    assert initial["omr_original_tempo"] == 2


def test_unrelated_later_direction_is_not_used_to_invent_a_unit():
    later = {"words": "Andante", "tempo": 90, "beat_unit": "quarter", "onset": 2}
    score = {"tempo": None, "parts": [{"measures": [{"directions": [later]}]}]}
    _merge_recognized_tempo(score, recognize("Allegro = 120"), omr_generated=True)
    assert score["tempo"].get("tempo") is None
    assert later["tempo"] == 90


def test_same_bpm_with_wrong_unit_is_reconciled_in_opening_direction():
    initial = {"words": "Allegro", "tempo": 120, "beat_unit": "quarter",
               "beat_unit_dots": 0, "onset": 0}
    score = {"tempo": dict(initial), "parts": [{"measures": [{"directions": [initial]}]}]}
    _merge_recognized_tempo(score, recognize("Allegro 𝅗𝅥· = 120"), omr_generated=True)
    assert initial["tempo"] == 120
    assert initial["beat_unit"] == "half"
    assert initial["beat_unit_dots"] == 1
    assert initial["omr_original_beat_unit"] == "quarter"


@pytest.mark.parametrize("words,unit,dots", [("♩ = 120", "quarter", 0), ("♪· = 120", "eighth", 1), ("= 120", None, 0)])
def test_plain_text_metronome_preserves_unit_evidence(words, unit, dots):
    direction = ET.fromstring(f"<direction><direction-type><words>{words}</words></direction-type></direction>")
    result = _direction_payload(direction)
    assert result["beat_unit"] == unit
    assert result["beat_unit_dots"] == dots
    if unit:
        assert result["tempo"] == 120
        assert result["words"] == ""
    else:
        assert result["tempo"] is None
        assert result["unresolved_metronome"] == {"value": 120, "raw_text": words}


def test_professional_html_exposes_escaped_review_warnings():
    result = _render_engraved_html({"title": "QA", "warnings": ["拍值未确认", "<script>bad</script>"]}, [], Path("qa.pdf"))
    assert "拍值未确认" in result
    assert "&lt;script&gt;bad&lt;/script&gt;" in result
    assert "<script>bad</script>" not in result
