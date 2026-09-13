from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from deepdesk.audiveris_environment import audiveris_bootstrap_args, audiveris_environment
from deepdesk.audiveris_tessdata import LANGUAGES, validate_tessdata
from deepdesk.jianpu_compiler_compat import compiler_ast
from deepdesk.lilypond_windows import WINDOWS_XML_READY, windows_engraver_arguments
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread
from deepdesk.score_engraving_style import apply_reading_style
from deepdesk.score_title_ocr import TEMPO_WORD, select_tempo
from deepdesk.subprocess_env import credential_safe_environment

SUPPORTED_INPUTS = {
    ".bmp",
    ".jpeg",
    ".jpg",
    ".mxl",
    ".musicxml",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".xml",
}
MUSICXML_INPUTS = {".mxl", ".musicxml", ".xml"}
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_MUSICXML_BYTES = 32 * 1024 * 1024
MAX_MXL_ENTRIES = 4_000
MAX_MXL_EXPANDED_BYTES = 128 * 1024 * 1024
MAJOR_NAMES = ["C♭", "G♭", "D♭", "A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯"]
MINOR_NAMES = ["A♭", "E♭", "B♭", "F", "C", "G", "D", "A", "E", "B", "F♯", "C♯", "G♯", "D♯", "A♯"]
STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SHARP_DEGREES = ["1", "♯1", "2", "♭3", "3", "4", "♯4", "5", "♯5", "6", "♭7", "7"]
FLAT_DEGREES = ["1", "♭2", "2", "♭3", "3", "4", "♭5", "5", "♭6", "6", "♭7", "7"]
JIANPU_LY_VERSION = "1.889"
LILYPOND_VERSION = "2.26.0"
_JIANPU_LY_LOCK = threading.Lock()
_LILYPOND_CACHE_LOCK = threading.Lock()
_PREPARED_LILYPOND_ROOTS: set[Path] = set()
UNKNOWN_SCORE_TITLES = {"", "未命名乐谱", "untitled", "unknown", "unknown score"}
TITLE_OCR_INPUTS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
JIANPU_STAFF_INPUTS = {".jly", ".txt", ".json"}
MAX_JIANPU_SOURCE_BYTES = 4 * 1024 * 1024
# Match fix_fullwidth in the pinned jianpu-ly compiler before inspecting input.
# NFKC is deliberately not used: it changes additional musical/text characters
# that the compiler itself does not normalize.
_JIANPU_CHARACTER_TRANSLATION = {
    **{codepoint: codepoint - 0xFEE0 for codepoint in range(0xFF01, 0xFF5F)},
    0x201A: ord(","),
    0xFF61: ord("."),
}
UNSAFE_JIANPU_SOURCE = re.compile(
    r"(?im)^\s*(?:LPH?|:LPH?)\s*:|\\(?:include|sourcefilename|bookOutputName)\b"
    # LilyPond accepts $ as well as #, plus splicing/reader prefixes. Keep
    # ordinary pitch sharps and quoted stanza strings, not executable forms.
    r"|[#$]\s*(?:[@'`,;!#]|[\(\[])"
)


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return node.find(f"{{*}}{name}")


def _children(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return list(node.findall(f"{{*}}{name}"))


def _text(node: ET.Element | None, name: str | None = None, default: str = "") -> str:
    target = _child(node, name) if name else node
    return str(target.text or "").strip() if target is not None else default


def _number(node: ET.Element | None, name: str, default: float) -> float:
    try:
        return float(_text(node, name, str(default)))
    except (TypeError, ValueError):
        return default


def _safe_stem(value: str) -> str:
    normalized = re.sub(r"[^\w\-\u3400-\u9fff]+", "-", value, flags=re.UNICODE).strip("-_")
    return normalized[:64] or "score"


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_musicxml(path: Path) -> str:
    if path.suffix.casefold() != ".mxl":
        if path.stat().st_size > MAX_MUSICXML_BYTES:
            raise ValueError("MusicXML exceeds the 32 MB parsing limit")
        return path.read_text(encoding="utf-8-sig", errors="strict")
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_MXL_ENTRIES:
                raise ValueError("Compressed MusicXML contains too many entries")
            if sum(item.file_size for item in entries) > MAX_MXL_EXPANDED_BYTES:
                raise ValueError("Compressed MusicXML expands beyond the 128 MB safety limit")
            entry_name = ""
            try:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                rootfile = container.find(".//{*}rootfile")
                entry_name = str(rootfile.attrib.get("full-path") or "") if rootfile is not None else ""
            except KeyError:
                pass
            candidates = [
                item.filename
                for item in entries
                if item.filename.casefold().endswith((".xml", ".musicxml"))
                and not item.filename.casefold().startswith("meta-inf/")
            ]
            selected = entry_name if entry_name in archive.namelist() else (candidates[0] if candidates else "")
            if not selected:
                raise ValueError("Compressed MusicXML has no score document")
            data = archive.read(selected)
            if len(data) > MAX_MUSICXML_BYTES:
                raise ValueError("MusicXML score exceeds the 32 MB parsing limit")
            return data.decode("utf-8-sig", errors="strict")
    except BadZipFile as exc:
        raise ValueError("The .mxl file is not valid compressed MusicXML") from exc


def _key_info(fifths: int, mode: str) -> dict[str, Any]:
    safe_fifths = max(-7, min(7, int(fifths)))
    minor = mode.casefold() == "minor"
    major_pc = (7 * safe_fifths) % 12
    tonic_pc = (major_pc + 9) % 12 if minor else major_pc
    names = MINOR_NAMES if minor else MAJOR_NAMES
    return {
        "fifths": safe_fifths,
        "mode": "minor" if minor else "major",
        "tonic_pc": tonic_pc,
        "tonic": names[safe_fifths + 7],
    }


def _jianpu_one_midi(key: dict[str, Any]) -> int:
    """Sound of an unmarked 1 in pinned jianpu-ly's 1= / 6= coordinate system.

    jianpu-ly transposes C3 for major and A3 for minor; its target octave depends
    on the spelled tonic (including C-flat/B-sharp boundaries). In a minor key
    the tonic is degree 6, not degree 1. Mirror that documented source transform,
    rather than choosing a new octave convention for the downstream engine.
    """
    tonic = str(key["tonic"]).replace("♯", "#").replace("♭", "b")
    step = tonic[0].upper()
    alteration = tonic.count("#") - tonic.count("b")
    minor = key["mode"] == "minor"
    target_octave = (4 if step in "CD" else 3) if minor else (2 if step in "GAB" else 3)
    target_midi = (target_octave + 1) * 12 + STEP_PC[step] + alteration
    return 60 + target_midi - (57 if minor else 48)


def _midi_token(midi: int, key: dict[str, Any], *, validate_range: bool = True) -> str:
    if not 0 <= midi <= 127:
        raise ValueError("Pitch is outside the supported MIDI range")
    reference = _jianpu_one_midi(key)
    relative = (midi - reference) % 12
    token = (FLAT_DEGREES if int(key["fifths"]) < 0 else SHARP_DEGREES)[relative]
    octave_shift = (midi - reference) // 12
    if validate_range and abs(octave_shift) > 3:
        raise ValueError("Pitch is outside the bundled jianpu-ly three-octave-mark range; refusing to clip its octave")
    if octave_shift > 0:
        token += "\u0307" * octave_shift
    elif octave_shift < 0:
        token += "\u0323" * abs(octave_shift)
    return token


def _pitch_components(
    step: str, alter: int, octave: int, key: dict[str, Any]
) -> tuple[str, str, int]:
    step = str(step or "C").upper()
    if abs(alter) > 2:
        raise ValueError("Pitch alterations beyond double accidentals are not supported")
    midi = (octave + 1) * 12 + STEP_PC.get(step, 0) + alter
    source = f"{step}{'♯' * max(0, alter)}{'♭' * max(0, -alter)}{octave}"
    # The written pitch can be outside the engraver's range while an octave-
    # transposing instrument's sounding pitch is inside it. Validate after that
    # instrument transform, not by clipping/rejecting the written intermediate.
    return _midi_token(midi, key, validate_range=False), source, midi


def _instrument_transposition(node: ET.Element) -> dict[str, int | None]:
    chromatic = Fraction(_text(node, "chromatic", "0"))
    if chromatic.denominator != 1 or _child(node, "double") is not None:
        raise ValueError("Microtonal or doubled instrument transposition cannot be represented faithfully by this converter")
    return {
        "chromatic": int(chromatic),
        "diatonic": int(_number(node, "diatonic", 0)) if _child(node, "diatonic") is not None else None,
        "octave_change": int(_number(node, "octave-change", 0)),
    }


def _concert_key(key: dict[str, Any], transposition: dict[str, Any]) -> dict[str, Any]:
    chromatic = int(transposition.get("chromatic") or 0)
    if not chromatic % 12:
        return dict(key)
    target_pc = (int(key["tonic_pc"]) + chromatic) % 12
    candidates = [_key_info(fifths, str(key["mode"])) for fifths in range(-7, 8)]
    candidates = [item for item in candidates if item["tonic_pc"] == target_pc]
    letters = "CDEFGAB"
    diatonic = transposition.get("diatonic")
    target_step = letters[(letters.index(str(key["tonic"])[0]) + int(diatonic)) % 7] if diatonic is not None else None
    return min(candidates, key=lambda item: (
        bool(target_step and str(item["tonic"])[0] != target_step),
        abs(int(item["fifths"]) - int(key["fifths"])), abs(int(item["fifths"])),
    ))


def _pitch_token(pitch: ET.Element, key: dict[str, Any]) -> tuple[str, str, int]:
    alteration = Fraction(_text(pitch, "alter", "0"))
    if alteration.denominator != 1:
        raise ValueError("Microtonal note pitches cannot be represented faithfully by bundled jianpu-ly; refusing to round pitch")
    return _pitch_components(
        _text(pitch, "step", "C"),
        int(alteration),
        int(_number(pitch, "octave", 4)),
        key,
    )


def _beam_count(quarter_length: float, note_type: str) -> int:
    normalized = str(note_type or "").casefold()
    by_type = {"eighth": 1, "16th": 2, "32nd": 3, "64th": 4}
    if normalized in by_type:
        return by_type[normalized]
    if not normalized:
        length = Fraction(str(quarter_length)).limit_denominator(65_536)
        for beams in range(5):
            for dots in range(5):
                if length == Fraction(1, 2 ** beams) * (2 - Fraction(1, 2 ** dots)):
                    return beams
    if 0 < quarter_length <= 0.13:
        return 3
    if 0 < quarter_length <= 0.26:
        return 2
    if 0 < quarter_length <= 0.51:
        return 1
    return 0


def _direction_payload(direction: ET.Element) -> dict[str, Any] | None:
    words = " ".join(
        value for item in direction.findall(".//{*}words") if (value := _text(item))
    )
    metronome = direction.find(".//{*}metronome")
    sound = direction if direction.tag.rsplit("}", 1)[-1] == "sound" else _child(direction, "sound")
    tempo = _number(metronome, "per-minute", 0) if metronome is not None else 0
    from_metronome = bool(tempo)
    if not tempo and sound is not None:
        try:
            tempo = float(sound.attrib.get("tempo") or 0)
        except (TypeError, ValueError):
            tempo = 0
    dynamics = direction.find(".//{*}dynamics")
    dynamic = ""
    if dynamics is not None:
        for item in list(dynamics):
            dynamic = item.tag.rsplit("}", 1)[-1]
            if dynamic:
                break
    if not words and not tempo and not dynamic:
        return None
    payload = {
        "words": words,
        "tempo": float(tempo) if tempo else None,
        "beat_unit": _text(metronome, "beat-unit", "quarter") if from_metronome else "quarter",
        "beat_unit_dots": len(_children(metronome, "beat-unit-dot")) if from_metronome else 0,
        "dynamic": dynamic,
    }
    if not tempo and words:
        # OMR sometimes emits the printed metronome as plain words, not a
        # MusicXML metronome. Preserve its evidence without guessing a beat.
        recognized = select_tempo([{"text": words, "confidence": 0.0,
                                    "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}], 10)
        if recognized.get("tempo") is not None:
            if recognized.get("tempo_beat_unit"):
                payload.update(tempo=recognized["tempo"], beat_unit=recognized["tempo_beat_unit"],
                               beat_unit_dots=recognized["tempo_beat_unit_dots"],
                               words=recognized["tempo_words"])
            else:
                payload["beat_unit"] = None
                payload["unresolved_metronome"] = {"value": recognized["tempo"], "raw_text": words}
    return payload


def _tempo_source(tempo: dict[str, Any]) -> str:
    if not tempo.get("tempo"):
        return ""
    denominator = {"whole": 1, "half": 2, "quarter": 4, "eighth": 8,
                   "16th": 16, "32nd": 32, "64th": 64}.get(str(tempo.get("beat_unit") or "quarter").casefold())
    value = Fraction(str(tempo["tempo"]))
    dots = int(tempo.get("beat_unit_dots") or 0)
    if denominator is None or not 0 <= dots <= 4 or value <= 0 or value.denominator != 1:
        raise ValueError("This metronome marking cannot be represented exactly by bundled jianpu-ly")
    return f"{denominator}{'.' * dots}={int(value)}"


def _tempo_text(tempo: dict[str, Any]) -> str:
    if tempo.get("tempo") is None:
        return ""
    symbol = {"whole": "𝅝", "half": "𝅗𝅥", "quarter": "♩", "eighth": "♪",
              "16th": "𝅘𝅥𝅯", "32nd": "𝅘𝅥𝅰", "64th": "𝅘𝅥𝅱"}.get(str(tempo.get("beat_unit") or "quarter").casefold(), "♩")
    return f"{symbol}{'·' * int(tempo.get('beat_unit_dots') or 0)}={float(tempo['tempo']):g}"


def _normalize_short_ottava_spans(
    part_notes: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    warnings: list[str],
) -> int:
    """Undo tiny OMR-created octave spans only when continuity strongly proves an error."""

    positions = {id(note): index for index, note in enumerate(part_notes)}
    corrected = 0
    for span in spans:
        notes = [note for note in span.get("notes", []) if id(note) in positions]
        if not span.get("closed") or not notes or len(notes) > 4:
            continue
        start = positions[id(notes[0])]
        end = positions[id(notes[-1])]
        before = part_notes[start - 1] if start > 0 else None
        after = part_notes[end + 1] if end + 1 < len(part_notes) else None
        delta = -12 if span.get("type") == "down" else 12 if span.get("type") == "up" else 0
        if not delta or (before is None and after is None):
            continue
        raw_edges: list[int] = []
        adjusted_edges: list[int] = []
        if before is not None:
            raw_edges.append(abs(int(notes[0]["midi"]) - int(before["midi"])))
            adjusted_edges.append(abs(int(notes[0]["midi"]) + delta - int(before["midi"])))
        if after is not None:
            raw_edges.append(abs(int(after["midi"]) - int(notes[-1]["midi"])))
            adjusted_edges.append(abs(int(after["midi"]) - (int(notes[-1]["midi"]) + delta)))
        if max(raw_edges, default=0) < 12 or sum(raw_edges) - sum(adjusted_edges) < 8:
            continue
        for note in notes:
            new_octave = int(note["_pitch_octave"]) + delta // 12
            token, source, midi = _pitch_components(
                str(note["_pitch_step"]), int(note["_pitch_alter"]), new_octave, note["_key"]
            )
            note.update(
                {
                    "token": token,
                    "source_pitch": source,
                    "midi": midi,
                    "_pitch_octave": new_octave,
                    "risk": "medium",
                    "auto_corrected": "short_ottava_continuity",
                }
            )
        corrected += len(notes)
        warnings.append(
            f"第 {span.get('measure', '?')} 小节检测到仅覆盖 {len(notes)} 个音且造成八度跳进的识别标记，"
            "已按旋律连续性回正一个八度；请对照原谱复核。"
        )
    return corrected


def _normalize_omr_slash_lyrics(part: dict[str, Any], warnings: list[str]) -> None:
    # Audiveris can read an instrumental mark as a single slash lyric. Only
    # suppress a verse made entirely of slash marks; keep real lyric verses,
    # melisma/extender markup, and every original entry for reversible review.
    notes = [note for measure in part["measures"] for note in measure["notes"]]
    verses: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        for identity, entry in _verse_entries(note).items():
            verses.setdefault(identity, []).append(entry)
    suppressed = set()
    for identity, entries in verses.items():
        text = "".join(str(entry.get("text") or "") for entry in entries)
        if (text.strip() and set(text) <= set("/\\﹨＼／ \t\r\n")
                and not any(entry.get("extend") for entry in entries)):
            suppressed.add(identity)
    if not suppressed:
        return
    for note in notes:
        entries = _verse_entries(note)
        if not suppressed.intersection(entries):
            continue
        note["omr_original_lyrics"] = [dict(entry) for entry in note["lyric_verses"]]
        note["lyric_verses"] = [entry for identity, entry in entries.items() if identity not in suppressed]
        note["lyrics"] = next((entry["text"] for entry in note["lyric_verses"]), "")
    warnings.append("识别到仅含斜线的可疑歌词行，已从谱面隐藏；原值保留在 omr_original_lyrics，请对照原谱复核。")


def _parse_musicxml(xml_text: str, *, normalize_omr: bool = False) -> dict[str, Any]:
    # MusicXML commonly carries a PUBLIC doctype. ElementTree does not fetch the
    # external DTD, so it is safe to retain; custom entity declarations remain
    # unnecessary for scores and are rejected.
    if "<!ENTITY" in xml_text.upper():
        raise ValueError("MusicXML declarations with custom entities are not accepted")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("MusicXML is malformed") from exc
    if root.tag.rsplit("}", 1)[-1] != "score-partwise":
        raise ValueError("Only score-partwise MusicXML is supported")

    part_names: dict[str, str] = {}
    part_list = _child(root, "part-list")
    for item in _children(part_list, "score-part"):
        part_names[str(item.attrib.get("id") or "")] = _text(item, "part-name", "声部")
    work_title = _text(_child(root, "work"), "work-title")
    movement_title = _text(root, "movement-title")
    credit_titles = [
        value
        for credit in _children(root, "credit")
        for item in _children(credit, "credit-words")
        if (value := _text(item))
        and not re.fullmatch(r"[\d\W_]+", value)
        and len(value) <= 120
    ]
    title = work_title or movement_title or (credit_titles[0] if credit_titles else "未命名乐谱")
    title_source = (
        "musicxml-work-title"
        if work_title
        else "musicxml-movement-title"
        if movement_title
        else "musicxml-credit-words"
        if credit_titles
        else "unresolved"
    )
    creators = [value for item in _children(_child(root, "identification"), "creator") if (value := _text(item))]
    score: dict[str, Any] = {
        "title": title,
        "title_source": title_source,
        "creators": creators,
        "parts": [],
        "warnings": [],
        "tempo": None,
        "pitch_basis": "concert",
        "pitch_reference": f"jianpu-ly-{JIANPU_LY_VERSION}",
    }
    if normalize_omr:
        # Image OMR sometimes classifies a page number or isolated punctuation
        # as a composer. Do not engrave these as names, but retain the original
        # evidence for correction. Explicit user-supplied MusicXML is untouched.
        plausible_creators = [value for value in creators if any(char.isalpha() for char in value)]
        if plausible_creators != creators:
            score["omr_original_creators"] = list(creators)
            score["creators"] = plausible_creators
            score["warnings"].append(
                "作者识别包含纯数字或孤立符号，已从排版署名中留空或移除；原值保留在 omr_original_creators 中，请对照原谱复核。"
            )
    auto_octave_corrections = 0

    for part_index, part in enumerate(_children(root, "part"), 1):
        part_id = str(part.attrib.get("id") or f"P{part_index}")
        state = {"divisions": 1.0, "fifths": 0, "mode": "major", "beats": 4, "beat_type": 4}
        transpositions: dict[str, dict[str, Any]] = {}
        output_part: dict[str, Any] = {
            "id": part_id,
            "name": part_names.get(part_id) or f"声部 {part_index}",
            "measures": [],
        }
        active_octave_shifts: dict[str, dict[str, Any]] = {}
        octave_spans: list[dict[str, Any]] = []
        part_pitched_notes: list[dict[str, Any]] = []
        for measure_index, measure in enumerate(_children(part, "measure"), 1):
            def apply_attributes(attributes: ET.Element, state=state, transpositions=transpositions) -> None:
                state["divisions"] = max(0.001, _number(attributes, "divisions", state["divisions"]))
                key_node = _child(attributes, "key")
                if key_node is not None:
                    state["fifths"] = int(_number(key_node, "fifths", state["fifths"]))
                    state["mode"] = _text(key_node, "mode", str(state["mode"]))
                time_node = _child(attributes, "time")
                if time_node is not None:
                    state["beats"] = int(_number(time_node, "beats", state["beats"]))
                    state["beat_type"] = int(_number(time_node, "beat-type", state["beat_type"]))
                for transpose_node in _children(attributes, "transpose"):
                    number = str(transpose_node.attrib.get("number") or "*")
                    if number == "*":
                        transpositions.clear()
                    transpositions[number] = _instrument_transposition(transpose_node)
            def keys(state=state, transpositions=transpositions) -> tuple[dict[str, Any], dict[str, Any]]:
                written = _key_info(int(state["fifths"]), str(state["mode"]))
                return written, _concert_key(written, transpositions.get("1", transpositions.get("*", {})))

            key, concert_key = keys()
            initial_key, initial_concert_key = key, concert_key
            initial_time = f"{state['beats']}/{state['beat_type']}"
            timeline: list[dict[str, Any]] = []
            pitch_states = [(Fraction(0), key, concert_key, dict(transpositions))]
            notes: list[dict[str, Any]] = []
            directions: list[dict[str, Any]] = []
            document_cursor = Fraction(0)
            measure_extent = Fraction(0)
            previous_onsets: dict[str, Fraction] = {}
            note_index = 0
            for child_node in list(measure):
                child_name = child_node.tag.rsplit("}", 1)[-1]
                if child_name == "attributes":
                    old_concert_key = concert_key
                    old_time = f"{state['beats']}/{state['beat_type']}"
                    apply_attributes(child_node)
                    key, concert_key = keys()
                    pitch_states.append((document_cursor, key, concert_key, dict(transpositions)))
                    current_time = f"{state['beats']}/{state['beat_type']}"
                    if not notes and document_cursor == 0:
                        initial_key, initial_concert_key, initial_time = key, concert_key, current_time
                    elif concert_key != old_concert_key or current_time != old_time:
                        event = {"onset": float(document_cursor)}
                        if concert_key != old_concert_key:
                            event["key"] = concert_key
                        if current_time != old_time:
                            event["time"] = current_time
                        timeline.append(event)
                    continue
                if child_name in {"backup", "forward"}:
                    shift = Fraction(_text(child_node, "duration", "0")) / Fraction(str(state["divisions"]))
                    if shift < 0:
                        raise ValueError("MusicXML backup/forward duration must not be negative")
                    document_cursor += -shift if child_name == "backup" else shift
                    measure_extent = max(measure_extent, document_cursor)
                    if document_cursor < 0:
                        raise ValueError("MusicXML backup moves before the start of its measure")
                    continue
                if child_name in {"direction", "sound"}:
                    payload = _direction_payload(child_node)
                    if payload:
                        offset = _child(child_node, "offset")
                        shift = Fraction(_text(offset) or "0") / Fraction(str(state["divisions"]))
                        payload["onset"] = float(document_cursor + shift)
                        if payload["onset"] < 0:
                            raise ValueError("MusicXML direction offset moves before its measure")
                        sound = _child(child_node, "sound")
                        if sound is not None and shift and (offset is None or offset.attrib.get("sound") != "yes"):
                            raise ValueError("Separate visual and playback tempo offsets are not supported by bundled jianpu-ly")
                        directions.append(payload)
                        if score["tempo"] is None and measure_index == 1 and payload["onset"] == 0 and (
                            payload["tempo"] or payload.get("unresolved_metronome")
                            or TEMPO_WORD.search(str(payload["words"]))
                        ):
                            score["tempo"] = payload
                        if payload.get("unresolved_metronome"):
                            warning = "速度数值已识别，但未确认拍值，未补写节拍单位；请核对原谱。"
                            if warning not in score["warnings"]:
                                score["warnings"].append(warning)
                    for shift in child_node.findall(".//{*}octave-shift"):
                        number = str(shift.attrib.get("number") or "1")
                        shift_type = str(shift.attrib.get("type") or "").casefold()
                        if shift_type == "stop":
                            span = active_octave_shifts.pop(number, None)
                            if span is not None:
                                span["closed"] = True
                                octave_spans.append(span)
                        elif shift_type in {"up", "down"}:
                            previous = active_octave_shifts.pop(number, None)
                            if previous is not None:
                                octave_spans.append(previous)
                            active_octave_shifts[number] = {
                                "number": number,
                                "type": shift_type,
                                "measure": str(measure.attrib.get("number") or measure_index),
                                "notes": [],
                                "closed": False,
                            }
                    continue
                if child_name != "note":
                    continue
                note = child_node
                note_index += 1
                voice = _text(note, "voice", "1")
                chord = _child(note, "chord") is not None
                onset = previous_onsets.get(voice, document_cursor) if chord else document_cursor
                # backup rewinds musical time, not the document's attribute
                # declarations. Resolve the key at this note's actual onset.
                applicable = [item for item in pitch_states if item[0] <= onset]
                _, note_key, note_concert_key, note_transpositions = max(enumerate(applicable), key=lambda pair: (pair[1][0], pair[0]))[1]
                rest = _child(note, "rest") is not None
                pitch = _child(note, "pitch")
                if rest:
                    token, source, midi = "0", "休止符", None
                    pitch_step, pitch_alter, pitch_octave = "", 0, 0
                elif pitch is not None:
                    pitch_step = _text(pitch, "step", "C").upper()
                    pitch_alter = int(_number(pitch, "alter", 0))
                    pitch_octave = int(_number(pitch, "octave", 4))
                    token, source, midi = _pitch_token(pitch, note_key)
                else:
                    token, source, midi = "?", "未知", None
                    pitch_step, pitch_alter, pitch_octave = "", 0, 0
                duration = _number(note, "duration", 0)
                quarter_length = duration / float(state["divisions"]) if duration else 0
                note_type = _text(note, "type", "")
                staff = _text(note, "staff", "1")
                time_modification = _child(note, "time-modification")
                duration_modification = None
                if time_modification is not None:
                    duration_modification = {
                        "actual_notes": int(_number(time_modification, "actual-notes", 0)),
                        "normal_notes": int(_number(time_modification, "normal-notes", 0)),
                        "normal_type": _text(time_modification, "normal-type", ""),
                        "normal_dots": len(_children(time_modification, "normal-dot")),
                    }
                grace = _child(note, "grace") is not None
                beams = [
                    {
                        "number": str(item.attrib.get("number") or "1"),
                        "type": _text(item).casefold(),
                    }
                    for item in _children(note, "beam")
                ]
                notations = _child(note, "notations")
                slurs = [
                    {
                        "type": str(item.attrib.get("type") or "").casefold(),
                        "number": str(item.attrib.get("number") or "1"),
                        "placement": str(item.attrib.get("placement") or "above"),
                    }
                    for item in _children(notations, "slur")
                    if str(item.attrib.get("type") or "").casefold() in {"start", "stop"}
                ]
                tie_types = {
                    str(item.attrib.get("type") or "").casefold()
                    for item in [*_children(note, "tie"), *_children(notations, "tied")]
                }
                risk = "high" if token == "?" else "medium" if not duration and _child(note, "grace") is None else "low"
                parsed_note = {
                        "id": f"{part_id}-m{measure_index}-n{note_index}",
                        "token": token,
                        "source_pitch": source,
                        "midi": midi,
                        "rest": rest,
                        "voice": voice,
                        "staff": staff,
                        "chord": chord,
                        "grace": grace,
                        "onset": float(onset),
                        "duration": round(duration, 3),
                        "quarter_length": quarter_length,
                        "note_type": note_type,
                        "beams": _beam_count(quarter_length, note_type),
                        "beam_states": beams,
                        "dots": len(_children(note, "dot")),
                        # Keep independent verse identity and same-syllable text
                        # fragments. The legacy field is the first line only.
                        "lyrics": "".join(_text(item) for item in _children(_child(note, "lyric"), "text")),
                        "lyric_verses": [
                            {"number": str(item.attrib.get("number") or ""),
                             "name": str(item.attrib.get("name") or ""),
                             "text": "".join(_text(fragment) for fragment in list(item)
                                             if fragment.tag.rsplit("}", 1)[-1] in {"text", "elision"}),
                             "syllabic": _text(item, "syllabic", "single"),
                             "extend": (str(extension.attrib.get("type") or "start")
                                        if (extension := _child(item, "extend")) is not None else "")}
                            for item in _children(note, "lyric")],
                        "tie_start": "start" in tie_types,
                        "tie_stop": "stop" in tie_types,
                        "slurs": slurs,
                        "time_modification": duration_modification,
                        "tuplet_marks": [str(item.attrib.get("type") or "") for item in _children(notations, "tuplet")],
                        "risk": risk,
                        "_pitch_step": pitch_step,
                        "_pitch_alter": pitch_alter,
                        "_pitch_octave": pitch_octave,
                        "_key": note_key,
                        "_concert_key": note_concert_key,
                        "_transposition": dict(note_transpositions.get(staff, note_transpositions.get("*", {}))),
                    }
                notes.append(parsed_note)
                previous_onsets[voice] = onset
                if not chord and not grace:
                    document_cursor += Fraction(_text(note, "duration", "0")) / Fraction(str(state["divisions"]))
                    measure_extent = max(measure_extent, document_cursor)
                if midi is not None:
                    part_pitched_notes.append(parsed_note)
                    for span in active_octave_shifts.values():
                        span["notes"].append(parsed_note)
            voices = sorted({str(note["voice"]) for note in notes})
            if len(voices) > 1:
                score["warnings"].append(f"第 {measure_index} 小节包含 {len(voices)} 个声部，已分行导出。")
            expected_quarters = float(state["beats"]) * 4 / max(1, float(state["beat_type"]))
            for voice in voices:
                actual_quarters = max((
                    float(note["onset"]) + float(note["quarter_length"])
                    for note in notes
                    if str(note["voice"]) == voice and not note["chord"] and not note["grace"]
                ), default=0)
                if (
                    actual_quarters
                    and str(measure.attrib.get("implicit") or "").casefold() != "yes"
                    and abs(actual_quarters - expected_quarters) > 0.13
                ):
                    score["warnings"].append(
                        f"第 {measure_index} 小节声部 {voice} 的识别时值为 {actual_quarters:g} 拍，"
                        f"与拍号要求的 {expected_quarters:g} 拍不一致。"
                    )
                    for item in notes:
                        if str(item["voice"]) == voice and item["risk"] == "low":
                            item["risk"] = "medium"
            output_part["measures"].append(
                {
                    "number": str(measure.attrib.get("number") or measure_index),
                    "implicit": str(measure.attrib.get("implicit") or "").casefold() == "yes",
                    "extent": float(measure_extent),
                    "barlines": [
                        {"location": str(barline.attrib.get("location") or "right"),
                         "repeat": dict(repeat.attrib) if (repeat := _child(barline, "repeat")) is not None else None,
                         "ending": dict(ending.attrib) if (ending := _child(barline, "ending")) is not None else None}
                        for barline in _children(measure, "barline")
                    ],
                    "key": initial_concert_key,
                    "written_key": initial_key,
                    "instrument_transpositions": {number: dict(value) for number, value in transpositions.items()},
                    "time": initial_time,
                    "state_changes": timeline,
                    "onset_unit": "quarter",
                    "divisions": float(state["divisions"]),
                    "beat_duration": float(state["divisions"]) * 4 / max(1, float(state["beat_type"])),
                    "voices": voices or ["1"],
                    "notes": notes,
                    "directions": directions,
                }
            )
        octave_spans.extend(active_octave_shifts.values())
        if normalize_omr:
            _normalize_omr_slash_lyrics(output_part, score["warnings"])
            auto_octave_corrections += _normalize_short_ottava_spans(
                part_pitched_notes, octave_spans, score["warnings"]
            )
        for note in part_pitched_notes:
            # Keep the source's written spelling and MIDI value for review. The
            # exported numbers, WithStaff score and MIDI deliberately use concert
            # pitch, including octave-transposing instruments. Normalize OMR's
            # spurious ottava in written coordinates before applying this offset.
            transposition = note.pop("_transposition", {})
            semitones = int(transposition.get("chromatic") or 0) + 12 * int(transposition.get("octave_change") or 0)
            note["written_midi"] = note["midi"]
            note["written_pitch"] = note["source_pitch"]
            note["instrument_transposition"] = transposition
            note["midi"] = int(note["midi"]) + semitones
            note["token"] = _midi_token(note["midi"], note.pop("_concert_key"))
            note.pop("_pitch_step", None)
            note.pop("_pitch_alter", None)
            note.pop("_pitch_octave", None)
            note.pop("_key", None)
        for measure in output_part["measures"]:
            for note in measure["notes"]:
                # Rest/unpitched notes are not members of part_pitched_notes.
                note.pop("_transposition", None)
                note.pop("_concert_key", None)
        score["parts"].append(output_part)
    if not score["parts"]:
        raise ValueError("MusicXML contains no score parts")
    all_notes = [note for part in score["parts"] for measure in part["measures"] for note in measure["notes"]]
    score["stats"] = {
        "parts": len(score["parts"]),
        "measures": sum(len(part["measures"]) for part in score["parts"]),
        "notes": sum(1 for note in all_notes if not note["rest"]),
        "review_count": sum(1 for note in all_notes if note["risk"] != "low"),
        "auto_octave_corrections": auto_octave_corrections,
    }
    return score


def _score_title_is_known(score: dict[str, Any]) -> bool:
    return str(score.get("title") or "").strip().casefold() not in UNKNOWN_SCORE_TITLES


def _companion_title_image(input_path: Path) -> Path | None:
    """Use an original image/PDF, or an image paired with exported MusicXML."""

    if input_path.suffix.casefold() in TITLE_OCR_INPUTS or input_path.suffix.casefold() == ".pdf":
        return input_path
    if input_path.suffix.casefold() not in MUSICXML_INPUTS:
        return None

    stem = input_path.stem
    candidate_names: list[str] = []
    # Browsers and score tools commonly save ``score.jpg(MusicXML).mxl``.
    without_label = re.sub(
        r"(?i)\s*[\(\[]?\s*music\s*xml\s*[\)\]]?\s*$", "", stem
    ).strip()
    if without_label and without_label != stem:
        candidate_names.append(without_label)
        if Path(without_label).suffix.casefold() not in TITLE_OCR_INPUTS:
            candidate_names.extend(
                f"{without_label}{suffix}" for suffix in sorted(TITLE_OCR_INPUTS)
            )
    # Also support ``score.jpg.musicxml`` and ``score.png.xml``.
    candidate_names.append(stem)

    seen: set[str] = set()
    for name in candidate_names:
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidate = input_path.with_name(name)
        if (
            candidate.suffix.casefold() in TITLE_OCR_INPUTS
            and candidate.is_file()
            and candidate.stat().st_size <= MAX_INPUT_BYTES
        ):
            return candidate.resolve()
    return None


def _merge_recognized_tempo(
    score: dict[str, Any], metadata: dict[str, Any] | None, *, omr_generated: bool = False
) -> None:
    """Merge only explicitly OCR-recognized tempo data; never insert a default speed."""

    if not metadata:
        return
    recognized_value = metadata.get("tempo")
    recognized_words = str(metadata.get("tempo_words") or "").strip()
    if recognized_value is None and not recognized_words:
        return
    current = dict(score.get("tempo") or {})
    valid_units = {"whole", "half", "quarter", "eighth", "16th", "32nd", "64th"}
    recognized_unit = metadata.get("tempo_beat_unit")
    unit_evidence = current
    if recognized_unit not in valid_units and current.get("beat_unit") not in valid_units and omr_generated:
        # Audiveris may place the opening mark just after the first note. Use
        # only its matching opening direction, never a later tempo change.
        for part in score.get("parts", []):
            measures = part.get("measures") or []
            for item in (measures[0].get("directions", []) if measures else []):
                if (item.get("tempo") is not None and float(item.get("onset") or 0) <= 0.5
                        and recognized_words
                        and str(item.get("words") or "").strip().casefold() == recognized_words.casefold()
                        and item.get("beat_unit") in valid_units):
                    unit_evidence = item
                    break
            if unit_evidence.get("beat_unit") in valid_units:
                break
    chosen_unit = recognized_unit if recognized_unit in valid_units else unit_evidence.get("beat_unit")
    can_merge_number = recognized_value is not None and chosen_unit in valid_units
    if (current.get("tempo") is None or omr_generated) and can_merge_number:
        current["tempo"] = float(recognized_value)
        current["beat_unit"] = chosen_unit
        current["beat_unit_dots"] = int(
            metadata.get("tempo_beat_unit_dots") or 0
            if recognized_unit in valid_units else unit_evidence.get("beat_unit_dots") or 0
        )
        current["beat_unit_source"] = "local-tempo-ocr" if recognized_unit in valid_units else "musicxml"
    elif recognized_value is not None and not can_merge_number:
        current["unresolved_metronome"] = {
            "value": recognized_value, "raw_text": str(metadata.get("tempo_raw_text") or ""),
        }
        warning = "速度数值已识别，但未确认拍值，未补写节拍单位；请核对原谱。"
        if warning not in score.setdefault("warnings", []):
            score["warnings"].append(warning)
    current_words = str(current.get("words") or "").strip()
    if recognized_words and (not current_words or TEMPO_WORD.search(current_words) is None):
        current["words"] = recognized_words
    current.setdefault("dynamic", "")
    current["source"] = str(metadata.get("tempo_source") or "local-tempo-ocr")
    current["confidence"] = float(metadata.get("tempo_confidence") or 0.0)
    score["tempo"] = current
    if omr_generated and can_merge_number and recognized_words:
        # Audiveris can misread the printed initial metronome mark and place it
        # after the first note. Reconcile only its first, matching tempo label;
        # preserve all later tempo changes and retain the original OCR evidence.
        for part in score.get("parts", []):
            measures = part.get("measures") or []
            if not measures:
                continue
            initial = next((item for item in measures[0].get("directions", [])
                            if item.get("tempo") is not None), None)
            if (initial is not None and float(initial.get("onset") or 0) <= 0.5
                    and str(initial.get("words") or "").strip().casefold() == recognized_words.casefold()
                    and (float(initial["tempo"]) != float(recognized_value)
                         or initial.get("beat_unit") != current.get("beat_unit")
                         or int(initial.get("beat_unit_dots") or 0) != current.get("beat_unit_dots", 0)
                         or float(initial.get("onset") or 0) != 0)):
                initial["omr_original_tempo"] = initial["tempo"]
                initial["omr_original_onset"] = initial.get("onset", 0)
                initial["omr_original_beat_unit"] = initial.get("beat_unit")
                initial["omr_original_beat_unit_dots"] = initial.get("beat_unit_dots", 0)
                initial["onset"] = 0
                for name in ("tempo", "beat_unit", "beat_unit_dots", "source", "confidence"):
                    initial[name] = current[name]
                score.setdefault("warnings", []).append(
                    "原谱开头的 OMR 速度与标题区域 OCR 不一致，已按原图速度标记修正；原识别值保留在 omr_original_tempo 中，请复核。"
                )


def _identify_source_score(
    input_path: Path, score: dict[str, Any], *, omr_generated: bool = False
) -> dict[str, Any]:
    """Identify the printed title without letting OCR text influence agent control."""

    title_image = _companion_title_image(input_path)
    title_source = str(score.get("title_source") or "")
    title_known = _score_title_is_known(score)
    credit_needs_image_verification = (
        (title_source == "musicxml-credit-words" or omr_generated) and title_image is not None
    )
    # User-supplied MusicXML title fields are authoritative. Fields generated by
    # OMR from an image are still OCR guesses, even when called movement-title.
    identification: dict[str, Any] | None = None
    if title_image is not None and (
        not title_known or credit_needs_image_verification or not score.get("tempo")
    ):
        # A frozen macOS backend is not a Python interpreter: its dedicated
        # metadata mode must dispatch before importing or starting the server.
        command = (
            [sys.executable, "--score-metadata", str(title_image)]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "deepdesk.score_title_ocr", str(title_image)]
        )
        try:
            completed = subprocess.run(
                command,
                cwd=JianpuOMRTool._application_root(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=75,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=credential_safe_environment({"ELREN_TITLE_OCR_OFFLINE": "1"}),
            )
            for line in reversed(completed.stdout.splitlines()):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("ok"):
                    identification = payload
                break
        except (OSError, subprocess.TimeoutExpired):
            identification = None
    _merge_recognized_tempo(score, identification, omr_generated=omr_generated)
    if title_known and not credit_needs_image_verification:
        return {
            "title": str(score["title"]),
            "confidence": 1.0,
            "confidence_label": "high",
            "source": title_source or "musicxml-title",
            "context": [],
            "needs_confirmation": False,
            "assistant_reply_required": f"在最终回复中明确告诉用户：原谱曲名是《{score['title']}》。",
        }
    if identification and identification.get("title"):
        title = str(identification["title"])
        score["title"] = title
        score["title_source"] = str(identification.get("source") or "local-title-ocr")
        needs_confirmation = bool(identification.get("needs_confirmation", True))
        reply = (
            f"在最终回复中明确告诉用户：原谱曲名可能是《{title}》，标题 OCR 置信度不足，请用户确认。"
            if needs_confirmation
            else f"在最终回复中明确告诉用户：原谱曲名是《{title}》。"
        )
        return {
            "title": title,
            "confidence": float(identification.get("confidence") or 0),
            "confidence_label": str(identification.get("confidence_label") or "low"),
            "source": str(identification.get("source") or "local-title-ocr"),
            "context": list(identification.get("context") or [])[:3],
            "raw_text": str(identification.get("raw_text") or "")[:160],
            "needs_confirmation": needs_confirmation,
            "assistant_reply_required": reply,
        }
    if _score_title_is_known(score):
        title = str(score["title"])
        return {
            "title": title,
            "confidence": 0.55,
            "confidence_label": "low",
            "source": title_source or "musicxml-credit-words",
            "context": [],
            "needs_confirmation": True,
            "assistant_reply_required": (
                f"在最终回复中明确告诉用户：MusicXML 页眉文字可能是《{title}》，但原图标题识别失败，请用户确认。"
            ),
        }
    return {
        "title": None,
        "confidence": 0.0,
        "confidence_label": "unresolved",
        "source": "unresolved",
        "context": [],
        "needs_confirmation": True,
        "assistant_reply_required": "在最终回复中明确告诉用户：未能从原谱可靠识别曲名，请提供更清晰的标题区域或手动确认；不得猜测。",
    }


def _duration_text(note: dict[str, Any]) -> str:
    token = str(note["token"])
    length = float(note["quarter_length"])
    if note["grace"]:
        token = f"({token})"
    elif 0 < length <= 0.26:
        token += "__"
    elif 0 < length <= 0.51:
        token += "_"
    elif length >= 1.75:
        token += " " + "-" * min(3, max(1, round(length) - 1))
    if int(note["dots"]):
        token += "." * int(note["dots"])
    return token


def _score_lines(score: dict[str, Any]) -> list[str]:
    lines = [str(score["title"])]
    if score["creators"]:
        lines.append(" / ".join(score["creators"]))
    first_measure = score["parts"][0]["measures"][0] if score["parts"][0]["measures"] else None
    if first_measure:
        mode = "小调" if first_measure["key"]["mode"] == "minor" else "大调"
        lines.append(f"1={first_measure['key']['tonic']} {mode}    拍号 {first_measure['time']}")
    lines.append("说明：数字后的上点/下点表示高低八度；_ 表示减时线；- 表示增时线。")
    for original_part in score["parts"]:
        part = _expand_independent_chords(original_part)
        lines.extend(["", f"【{part['name']}】"])
        voices = sorted({voice for measure in part["measures"] for voice in measure["voices"]})
        for voice in voices:
            if len(voices) > 1:
                lines.append(f"声部 {voice}")
            measures: list[str] = []
            for measure in part["measures"]:
                tokens = [_duration_text(note) for note in measure["notes"] if str(note["voice"]) == voice]
                measures.append(" ".join(tokens) if tokens else "")
            lines.append("| " + " | ".join(measures) + " |")
            verse_ids = dict.fromkeys(verse for measure in part["measures"] for note in measure["notes"]
                                     if str(note["voice"]) == voice for verse in _verse_entries(note))
            for verse in verse_ids:
                lyric_measures = [" ".join(str(_verse_entries(note).get(verse, {}).get("text") or "")
                                          for note in measure["notes"] if str(note["voice"]) == voice
                                          and not note.get("rest")) for measure in part["measures"]]
                if any(item.strip() for item in lyric_measures):
                    label = f"歌词 {verse}" if len(verse_ids) > 1 else "歌词"
                    lines.append(label + "：| " + " | ".join(lyric_measures) + " |")
    if score["warnings"]:
        lines.extend(["", "复核提示：", *[f"- {warning}" for warning in score["warnings"]]])
    return lines


def _safe_engraving_header(value: Any, fallback: str = "") -> str:
    """Keep untrusted MusicXML metadata out of the generated LilyPond program."""

    cleaned = re.sub(r"[\x00-\x1f\x7f{}\\%\"]+", " ", str(value or fallback))
    return re.sub(r"\s+", " ", cleaned).strip()[:180] or fallback


def _jianpu_pitch(token: Any) -> str:
    raw = str(token or "?")
    upper = raw.count("\u0307")
    lower = raw.count("\u0323")
    pitch = raw.replace("\u0307", "").replace("\u0323", "")
    pitch = pitch.replace("♯", "#").replace("♭", "b")
    if not re.fullmatch(r"[#b]?[1-7]|0", pitch):
        return "0"
    return pitch + ("'" * upper) + ("," * lower)


def _jianpu_tuplet(note: dict[str, Any]) -> tuple[int, int] | None:
    modification = note.get("time_modification")
    if not modification or note.get("grace"):
        return None
    actual = int(modification.get("actual_notes") or 0)
    normal = int(modification.get("normal_notes") or 0)
    if 0 < actual == normal <= 64:
        return None
    if not (1 < actual <= 64 and 0 < normal <= 64):
        raise ValueError("Invalid or unsupported MusicXML time-modification ratio")
    power = 2
    while power < actual:
        power *= 2
    supported_normal = actual * 3 // 2 if power == actual else power // 2
    if normal != supported_normal:
        raise ValueError(f"Bundled jianpu-ly cannot faithfully express the {actual}:{normal} tuplet ratio; refusing to change its timing")
    return actual, normal


def _jianpu_duration(note: dict[str, Any], pitch: str) -> list[str]:
    """Encode measured MusicXML time with standard jianpu-ly notes/ties.

    quarter_length is the sounding duration; type/dots are source hints, never a
    substitute for that time. Keep enough precision for ratios such as 1/3, then
    undo the supported tuplet scale before writing its normal note values.
    """
    if note.get("grace"):
        return [f"g[{pitch}]"]
    length = Fraction(str(note.get("quarter_length") or 0)).limit_denominator(65_536)
    ratio = _jianpu_tuplet(note)
    if ratio:
        length *= Fraction(ratio[0], ratio[1])
    if not 0 < length <= 256:
        raise ValueError("A sounding note duration is missing or outside the supported range")
    for denominator, prefix in ((1, ""), (2, "q"), (4, "s"), (8, "d"), (16, "h")):
        for dots in range(5):
            if length == Fraction(1, denominator) * (2 - Fraction(1, 2 ** dots)):
                return [f"{prefix}{pitch}{'.' * dots}"]
    if length.denominator == 1:
        return [pitch, *(["-"] * (int(length) - 1))]
    if length > 1:
        whole = int(length)
        tail_note = {"quarter_length": float(length - whole)}
        tail = _jianpu_duration(tail_note, pitch)
        return [pitch, *(["-"] * (whole - 1)), *([] if pitch == "0" else ["~"]), *tail]
    for denominator, prefix in ((2, "q"), (4, "s"), (8, "d"), (16, "h")):
        unit = Fraction(1, denominator)
        if length > unit:
            tail = _jianpu_duration({"quarter_length": float(length - unit)}, pitch)
            return [f"{prefix}{pitch}", *([] if pitch == "0" else ["~"]), *tail]
    raise ValueError(f"Cannot faithfully express duration {length} with bundled jianpu-ly; provide an explicit supported tuplet")


def _jianpu_rest_fill(quarter_length: float) -> list[str]:
    """Pad a non-pickup OMR measure without moving every later barline."""

    remaining = round(max(0.0, quarter_length), 4)
    values = [
        (4.0, ["0", "-", "-", "-"]),
        (3.0, ["0", "-", "-"]),
        (2.0, ["0", "-"]),
        (1.5, ["0."]),
        (1.0, ["0"]),
        (0.75, ["q0."]),
        (0.5, ["q0"]),
        (0.375, ["s0."]),
        (0.25, ["s0"]),
        (0.1875, ["d0."]),
        (0.125, ["d0"]),
        (0.09375, ["h0."]),
        (0.0625, ["h0"]),
    ]
    result: list[str] = []
    for value, tokens in values:
        while remaining + 0.0001 >= value:
            result.extend(tokens)
            remaining = round(remaining - value, 4)
    return result


def _measure_length(measure: dict[str, Any]) -> Fraction:
    if "extent" in measure:
        return Fraction(str(measure["extent"])).limit_denominator(65_536)
    cursors: dict[str, Fraction] = {}
    for note in measure["notes"]:
        if note.get("chord") or note.get("grace"):
            continue
        voice = str(note.get("voice") or "1")
        duration = Fraction(str(note.get("quarter_length") or 0)).limit_denominator(65_536)
        if measure.get("onset_unit") == "quarter":
            cursors[voice] = max(cursors.get(voice, Fraction(0)), Fraction(str(note.get("onset") or 0)) + duration)
        else:
            cursors[voice] = cursors.get(voice, Fraction(0)) + duration
    return max(cursors.values(), default=Fraction(0))


def _pickup_suffix(measure: dict[str, Any]) -> str:
    numerator, denominator = (int(item) for item in str(measure["time"]).split("/"))
    length = _measure_length(measure)
    if not measure.get("implicit") or not 0 < length < Fraction(numerator * 4, denominator):
        return ""
    for unit in (1, 2, 4, 8, 16, 32, 64, 128):
        for dotted in (False, True):
            if length == Fraction(4, unit) * (Fraction(3, 2) if dotted else 1):
                return f",{unit}{'.' if dotted else ''}"
    raise ValueError("This pickup length is not expressible in bundled jianpu-ly's single/dotted pickup syntax")


def _repeat_source(measures: list[dict[str, Any]], blocks: list[str]) -> str:
    """Map MusicXML barline control flow to upstream repeat/alternative syntax."""
    events = []
    for index, measure in enumerate(measures):
        for barline in measure.get("barlines", []):
            location = barline.get("location", "right")
            if not barline.get("repeat") and not barline.get("ending"):
                continue
            if location not in {"left", "right"}:
                raise ValueError("Mid-measure repeat barlines require split measures before conversion")
            events.append((index + (location == "right"), barline))
    events.sort(key=lambda item: item[0])
    stack: list[int] = []
    regions: list[dict[str, Any]] = []
    active_endings: dict[str, int] = {}
    endings: list[dict[str, Any]] = []
    for boundary, event in events:
        ending = event.get("ending") or {}
        label = str(ending.get("number") or "")
        if ending.get("type") == "start":
            if label in active_endings:
                raise ValueError("MusicXML ending is opened twice")
            active_endings[label] = boundary
        elif ending.get("type") in {"stop", "discontinue"}:
            if label not in active_endings:
                raise ValueError("MusicXML ending has no matching start")
            numbers = [int(value.strip()) for value in label.split(",")]
            start = active_endings.pop(label)
            if start >= boundary:
                raise ValueError("MusicXML ending must have positive measure extent")
            endings.append({"start": start, "end": boundary, "numbers": numbers})
        repeat = event.get("repeat") or {}
        if repeat.get("direction") == "forward":
            stack.append(boundary)
        elif repeat.get("direction") == "backward":
            # Additional backward marks on later endings belong to the already
            # opened alternative group, not an implicit new whole-piece repeat.
            if not stack and label and min(int(value.strip()) for value in label.split(",")) > 1:
                continue
            start = stack.pop() if stack else 0
            count = int(repeat.get("times") or 2)
            if count < 1 or count > 128 or start >= boundary:
                raise ValueError("Invalid or excessive MusicXML repeat count/extent")
            regions.append({"start": start, "end": boundary, "count": count, "alternatives": []})
    if stack or active_endings:
        raise ValueError("MusicXML contains an unclosed repeat or ending")
    claimed_endings: set[int] = set()
    for region in regions:
        first = next((item for item in endings if item["end"] == region["end"] and item["start"] >= region["start"]), None)
        if first is None:
            continue
        alternatives = [first]
        visited = {id(first)}
        while following := next((item for item in endings if item["start"] == alternatives[-1]["end"]), None):
            if id(following) in visited or following["end"] <= alternatives[-1]["end"]:
                raise ValueError("MusicXML alternative boundaries do not advance")
            visited.add(id(following))
            alternatives.append(following)
        flattened = [number for item in alternatives for number in item["numbers"]]
        if (flattened != list(range(1, max(flattened) + 1))
                or any(len(item["numbers"]) != 1 for item in alternatives[1:])):
            raise ValueError("Nonsequential volta numbers cannot be represented by bundled jianpu-ly alternatives")
        region.update(common_end=first["start"], end=alternatives[-1]["end"],
                      count=max(flattened), alternatives=alternatives)
        claimed_endings.update(id(item) for item in alternatives)
    if len(claimed_endings) != len(endings):
        raise ValueError("MusicXML ending is not associated with a repeat")

    def sequence(start: int, end: int, excluded: frozenset[int] = frozenset()) -> str:
        output = []
        index = start
        while index < end:
            candidates = [item for item in regions if item["start"] == index and item["end"] <= end and id(item) not in excluded]
            region = max(candidates, key=lambda item: item["end"]) if candidates else None
            if region:
                nested_excluded = excluded | {id(region)}
                common = sequence(index, region.get("common_end", region["end"]), nested_excluded)
                marker = "R{" if region["count"] == 2 else f"R{region['count']}{{"
                output.append(f"{marker} {common} }}")
                if region["alternatives"]:
                    alternatives = [sequence(item["start"], item["end"], nested_excluded) for item in region["alternatives"]]
                    output.append("A{ " + " | ".join(alternatives) + " }")
                index = region["end"]
            else:
                output.append(blocks[index])
                index += 1
        return " ".join(output)

    return sequence(0, len(measures))


def _note_groups(measure: dict[str, Any], voice: str) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    cursor = Fraction(0)
    for original in measure["notes"]:
        if str(original["voice"]) != voice:
            continue
        note = dict(original)
        if note.get("chord") and groups:
            note["onset"] = groups[-1][0]["onset"]
            groups[-1].append(note)
        else:
            if measure.get("onset_unit") != "quarter":
                note["onset"] = float(cursor)
            groups.append([note])
            if not note.get("grace"):
                cursor = Fraction(str(note.get("onset") or 0)) + Fraction(str(note.get("quarter_length") or 0))
    return groups


def _expand_independent_chords(part: dict[str, Any]) -> dict[str, Any]:
    """Use standard parallel parts when a simple chord would lose note semantics.

    Stream assignment follows a pending tie by pitch, not MusicXML chord order.
    The source JSON is untouched. Ordinary homogeneous chords stay stacked.
    """
    voices = sorted({str(voice) for measure in part["measures"] for voice in measure["voices"]})
    output = {**part, "measures": [{**measure, "notes": list(measure["notes"])} for measure in part["measures"]]}
    for voice in voices:
        measures = [_note_groups(measure, voice) for measure in part["measures"]]

        def signature(note: dict[str, Any]) -> tuple[Any, ...]:
            return (Fraction(str(note.get("quarter_length") or 0)).limit_denominator(65_536),
                    bool(note.get("tie_start")), bool(note.get("tie_stop")), _jianpu_tuplet(note),
                    bool(note.get("grace")))

        if not any(len({signature(note) for note in group}) > 1 for groups in measures for group in groups):
            continue
        lane_prefix = f"{voice}.chord"
        while any(existing.startswith(lane_prefix) for existing in voices):
            lane_prefix += "_"
        lanes: list[dict[str, Any]] = []
        assigned: list[list[list[dict[str, Any]]]] = []
        offset = Fraction(0)
        for measure, groups in zip(part["measures"], measures):
            current: list[list[dict[str, Any]]] = [[] for _ in lanes]
            for group in groups:
                onset = Fraction(str(group[0].get("onset") or 0)).limit_denominator(65_536)
                absolute = offset + onset
                selected: dict[int, int] = {}
                used: set[int] = set()

                def pitch_of(note: dict[str, Any]) -> Any:
                    return note.get("midi") if note.get("midi") is not None else note.get("token")

                # Reserve continuations first, even if the tied pitch changes
                # its position within the next chord.
                for index, note in enumerate(group):
                    if note.get("tie_stop"):
                        match = next((lane for lane, state in enumerate(lanes)
                                      if lane not in used and state["tie"] and state["end"] == absolute
                                      and state["pitch"] == pitch_of(note)), None)
                        if match is not None:
                            selected[index] = match
                            used.add(match)
                for index, note in enumerate(group):
                    if index not in selected:
                        available = [lane for lane, state in enumerate(lanes)
                                     if lane not in used and state["end"] <= absolute]
                        matching = [lane for lane in available if lanes[lane]["pitch"] == pitch_of(note)]
                        # Preserve the source's melody/member ordering unless
                        # a pending tie has reserved that lane for another note.
                        preferred = [index] if index in available else []
                        lane = (preferred or matching or available or [len(lanes)])[0]
                        if lane == len(lanes):
                            lanes.append({})
                            current.append([])
                        selected[index] = lane
                        used.add(lane)
                    lane = selected[index]
                    duration = Fraction(str(note.get("quarter_length") or 0)).limit_denominator(65_536)
                    lanes[lane] = {"pitch": pitch_of(note), "tie": bool(note.get("tie_start")),
                                   "end": absolute + (0 if note.get("grace") else duration)}
                    current[lane].append({**note, "chord": False, "voice": f"{lane_prefix}{lane + 1}"})
            assigned.append(current)
            numerator, denominator = map(int, str(measure["time"]).split("/"))
            offset += max(_measure_length(measure), Fraction(0) if measure.get("implicit") else Fraction(numerator * 4, denominator))
        for measure, groups, current in zip(output["measures"], measures, assigned):
            measure["notes"] = [note for note in measure["notes"] if str(note["voice"]) != voice]
            for lane in range(len(lanes)):
                lane_notes = current[lane] if lane < len(current) else []
                # Fill the missing tail/member of each chord using its original
                # tuplet scale, so e.g. a 1/3 beat gap isn't rounded to binary rests.
                for group in groups:
                    base = group[0]
                    if base.get("grace"):
                        continue
                    start = Fraction(str(base.get("onset") or 0)).limit_denominator(65_536)
                    end = start + Fraction(str(base.get("quarter_length") or 0)).limit_denominator(65_536)
                    covering = [note for note in lane_notes if not note.get("grace")
                                and Fraction(str(note.get("onset") or 0)).limit_denominator(65_536) == start]
                    if covering:
                        start += Fraction(str(covering[0].get("quarter_length") or 0)).limit_denominator(65_536)
                    if start < end:
                        lane_notes.append({**base, "id": str(base.get("id", "")) + f"-rest{lane}",
                                           "voice": f"{lane_prefix}{lane + 1}", "chord": False,
                                           "onset": float(start), "quarter_length": float(end - start),
                                           "token": "0", "rest": True, "midi": None, "lyrics": "",
                                           "lyric_verses": [], "tie_start": False, "tie_stop": False,
                                           "slurs": [], "tuplet_marks": []})
                measure["notes"].extend(sorted(lane_notes, key=lambda note: float(note.get("onset") or 0)))
            measure["onset_unit"] = "quarter"
            measure["voices"] = sorted({str(note["voice"]) for note in measure["notes"]})
    return output


def _verse_entries(note: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = note.get("lyric_verses")
    if entries is None:
        entries = [{"number": "1", "text": str(note.get("lyrics") or "")}]
    return {str(item.get("number") or item.get("name") or "1"): item for item in entries}


def _score_to_jianpu_source(score: dict[str, Any]) -> str:
    """Translate the validated score model to jianpu-ly's stable text notation."""

    streams: list[str] = []
    for original_part in score["parts"]:
        part = _expand_independent_chords(original_part)
        voices = sorted({str(voice) for measure in part["measures"] for voice in measure["voices"]})
        for voice in voices:
            first = part["measures"][0] if part["measures"] else None
            lines: list[str] = ["SeparateTimesig"]
            if first:
                key = first["key"]
                tonic = str(key["tonic"]).replace("♯", "#").replace("♭", "b")
                lines.extend([f"{'6' if key['mode'] == 'minor' else '1'}={tonic}", str(first["time"]) + _pickup_suffix(first)])
            tempo = score.get("tempo") or {}
            # The first explicit event already establishes playback speed and
            # must remain inside a possible repeat. Do not engrave it twice.
            initial_tempo_event = bool(first and any(
                item.get("tempo") and not item.get("onset")
                and _tempo_source(item) == _tempo_source(tempo)
                for item in first.get("directions", [])
            ))
            if tempo.get("tempo") and not initial_tempo_event:
                lines.append(_tempo_source(tempo))
            if str(tempo.get("words") or "").strip():
                lines.append(
                    f"piece={_safe_engraving_header(tempo.get('words'))}"
                )
            lines.extend(
                [
                    f"title={_safe_engraving_header(score.get('title'), '未命名乐谱')}",
                    f"composer={_safe_engraving_header(' / '.join(score.get('creators') or []))}",
                    "NoIndent",
                    "RaggedLast",
                ]
            )
            part_name = _safe_engraving_header(part.get("name"), "声部")
            if (len(score["parts"]) > 1 or len(voices) > 1
                    or part_name.casefold() not in {"voice", "part", "music", "声部"}):
                lines.append(f"instrument={part_name}" + (f" {voice}" if len(voices) > 1 else ""))
            music: list[str] = []
            lyric_slots: list[dict[str, dict[str, Any]]] = []
            active_slur_marker: int | None = None
            emitted_key = first["key"] if first else None
            emitted_time = first["time"] if first else None
            emitted_tempo = _tempo_source(tempo)
            measure_blocks: list[tuple[int, int]] = []
            for measure in part["measures"]:
                block_start = len(music)
                active_tuplet: tuple[int, int] | None = None
                voice_notes = [note for note in measure["notes"] if str(note["voice"]) == voice]
                events = [{"onset": 0, "key": measure["key"], "time": measure["time"]},
                          *measure.get("state_changes", []),
                          *[item for item in measure.get("directions", []) if item.get("tempo")]]
                events.sort(key=lambda item: float(item.get("onset") or 0))
                event_index = 0
                cursor = Fraction(0)

                def emit_events(at: Fraction, events=events, music=music) -> None:
                    nonlocal event_index, emitted_key, emitted_time, emitted_tempo
                    while event_index < len(events) and Fraction(str(events[event_index].get("onset") or 0)).limit_denominator(65_536) <= at:
                        event = events[event_index]
                        event_key = event.get("key")
                        if event_key and event_key != emitted_key:
                            tonic = str(event_key["tonic"]).replace("♯", "#").replace("♭", "b")
                            music.append(f"{'6' if event_key['mode'] == 'minor' else '1'}={tonic}")
                            emitted_key = event_key
                        if event.get("time") and event["time"] != emitted_time:
                            if at:
                                raise ValueError("A time-signature change inside a measure cannot be represented faithfully by bundled jianpu-ly")
                            music.append(str(event["time"]))
                            emitted_time = event["time"]
                        if event.get("tempo"):
                            marking = _tempo_source(event)
                            # Retain an explicit marking even when it equals the
                            # initial tempo: a repeat can return here after a later
                            # speed change, and must restore this value on replay.
                            music.append(marking)
                            emitted_tempo = marking
                        event_index += 1

                def fill_to(target: Fraction, events=events, music=music, emit_events=emit_events) -> None:
                    nonlocal cursor, event_index
                    # Synchronous local closures; neither escapes this measure.
                    while event_index < len(events):  # noqa: B023
                        position = Fraction(str(events[event_index].get("onset") or 0)).limit_denominator(65_536)  # noqa: B023
                        if position > target:
                            break
                        if position > cursor:
                            music.extend(_jianpu_rest_fill(float(position - cursor)))
                            cursor = position
                        emit_events(cursor)
                    if target > cursor:
                        music.extend(_jianpu_rest_fill(float(target - cursor)))
                        cursor = target

                index = 0
                while index < len(voice_notes):
                    note = voice_notes[index]
                    onset = Fraction(str(note.get("onset") or 0)).limit_denominator(65_536) if measure.get("onset_unit") == "quarter" else cursor
                    if onset < cursor:
                        raise ValueError("Overlapping notes within one voice cannot be flattened faithfully")
                    if onset > cursor and active_tuplet:
                        music.append("]")
                        active_tuplet = None
                    fill_to(onset)
                    chord = [note]
                    scan = index + 1
                    while scan < len(voice_notes) and voice_notes[scan].get("chord"):
                        chord.append(voice_notes[scan])
                        scan += 1
                    pitches = [_jianpu_pitch(item.get("token")) for item in chord]
                    pitch = "".join(pitches) if len(pitches) > 1 else pitches[0]
                    duration = Fraction(str(note.get("quarter_length") or 0)).limit_denominator(65_536)
                    end = onset + (0 if note.get("grace") else duration)
                    intervening = [event for event in events[event_index:]
                                   if onset < Fraction(str(event.get("onset") or 0)).limit_denominator(65_536) < end]
                    if intervening and (_jianpu_tuplet(note) or any(event.get("key") or event.get("time") for event in intervening)):
                        raise ValueError("State changes within a sustained tuplet or across a sustained key change cannot be represented faithfully")
                    token_parts = _jianpu_duration(note, pitch)
                    tuplet = _jianpu_tuplet(note)
                    if tuplet != active_tuplet or (active_tuplet and "start" in note.get("tuplet_marks", [])):
                        if active_tuplet:
                            music.append("]")
                        if tuplet:
                            music.append(f"{tuplet[0]}[")
                        active_tuplet = tuplet
                    starts = {
                        str(slur.get("number") or "1")
                        for item in chord for slur in item.get("slurs", []) if slur.get("type") == "start"
                    }
                    stops = {
                        str(slur.get("number") or "1")
                        for item in chord for slur in item.get("slurs", []) if slur.get("type") == "stop"
                    }
                    if intervening:
                        segment_start = onset
                        for event in intervening:
                            position = Fraction(str(event["onset"])).limit_denominator(65_536)
                            if position <= segment_start:
                                continue
                            music.extend(_jianpu_duration({**note, "quarter_length": float(position - segment_start)}, pitch))
                            if pitch != "0":
                                music.append("~")
                            emit_events(position)
                            segment_start = position
                        music.extend(_jianpu_duration({**note, "quarter_length": float(end - segment_start)}, pitch))
                    else:
                        music.extend(token_parts)
                    cursor = end
                    if any(item.get("tie_start") for item in chord):
                        music.append("~")
                    if stops and active_slur_marker is not None:
                        music.append(")")
                        active_slur_marker = None
                    if starts and active_slur_marker is None:
                        music.append("(")
                        active_slur_marker = len(music) - 1
                    verses = {verse: value for item in chord for verse, value in _verse_entries(item).items()}
                    # Automatic lyric-to-voice alignment skips tied
                    # continuations; absent lyrics on rests consume no slot.
                    if (not note.get("grace") and not note.get("rest")
                            and (not note.get("tie_stop") or any(value.get("text") for value in verses.values()))):
                        lyric_slots.append(verses)
                    if active_tuplet and "stop" in note.get("tuplet_marks", []):
                        music.append("]")
                        active_tuplet = None
                    index = scan
                if active_tuplet:
                    music.append("]")
                if not measure.get("implicit"):
                    beats, beat_type = (int(item) for item in str(measure["time"]).split("/", 1))
                    expected = beats * 4 / max(1, beat_type)
                    fill_to(max(cursor, Fraction(str(expected))))
                else:
                    fill_to(max(cursor, _measure_length(measure)))
                if event_index < len(events):
                    raise ValueError("MusicXML direction lies beyond the end of its measure")
                measure_blocks.append((block_start, len(music)))
            if active_slur_marker is not None:
                music[active_slur_marker] = ""
            lines.append(_repeat_source(part["measures"], [" ".join(music[start:end]) for start, end in measure_blocks]))
            verse_ids = dict.fromkeys(verse for slot in lyric_slots for verse in slot)
            for verse in verse_ids:
                if not any(slot.get(verse, {}).get("text") for slot in lyric_slots):
                    continue
                syllables = []
                for slot in lyric_slots:
                    value = slot.get(verse, {})
                    text = _safe_engraving_header(value.get("text"))
                    # L: preserves one MusicXML syllable per note, including
                    # multiple Hanzi/text fragments. H: would auto-split it.
                    syllables.append('"' + text + '"')
                    if text and value.get("syllabic") in {"begin", "middle"}:
                        syllables.append("--")
                label = _safe_engraving_header(verse)
                stanza = ('\\set stanza = #"' + label + '." ') if len(verse_ids) > 1 or verse != "1" else ""
                lines.append("L: " + stanza + " ".join(syllables))
            streams.append("\n".join(lines))
    if not streams:
        raise ValueError("MusicXML contains no renderable score streams")
    return "\n\nNextPart\n\n".join(streams).rstrip() + "\n"


def _validate_jianpu_source(source: str) -> str:
    normalized = str(source or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("简谱文本为空。")
    if len(normalized.encode("utf-8")) > MAX_JIANPU_SOURCE_BYTES:
        raise ValueError("简谱文本超过 4 MB 安全限制。")
    # Raw LilyPond/Scheme directives must not become executable only after the
    # upstream compiler converts their fullwidth spelling to ASCII.
    normalized = normalized.translate(_JIANPU_CHARACTER_TRANSLATION)
    if UNSAFE_JIANPU_SOURCE.search(normalized):
        raise ValueError("简谱包含不允许的 LilyPond 注入指令；请只使用标准 jianpu-ly 记谱语法。")
    if not re.search(r"(?m)^\s*[16]\s*=\s*[A-Ga-g]", normalized):
        raise ValueError("未找到简谱调号（例如 1=C 或 6=A）；请提供标准 jianpu-ly 文本。")
    if not re.search(r"(?m)^\s*\d+\s*/\s*\d+", normalized):
        raise ValueError("未找到拍号（例如 4/4）；请提供标准 jianpu-ly 文本。")
    return normalized + "\n"


def _with_western_staff(source: str) -> str:
    """Enable jianpu-ly's mature Western-staff doubling for every part."""

    validated = _validate_jianpu_source(source)
    # The upstream grammar separates movements and parts by whitespace tokens,
    # not whole lines. Preserve comments as text, but never interpret a commented
    # NextPart as a new part (the compiler ignores those too).
    pieces: list[str] = []
    start = 0
    for match in re.finditer(r"(?<!\S)(NextPart|NextScore)(?!\S)", validated):
        line_start = validated.rfind("\n", 0, match.start()) + 1
        if "%" in validated[line_start:match.start()]:
            continue
        pieces.extend([validated[start:match.start()], match.group(1)])
        start = match.end()
    pieces.append(validated[start:])
    for index in range(0, len(pieces), 2):
        commands = re.sub(r"%[^\n]*", "", pieces[index])
        if commands.strip() and not re.search(r"(?<!\S)WithStaff(?!\S)", commands):
            pieces[index] = "WithStaff\n" + pieces[index].lstrip()
    return "\n\n".join(piece.strip() for piece in pieces).rstrip() + "\n"


def _jianpu_title(source: str, fallback: str) -> str:
    match = re.search(r"(?m)^\s*title\s*=\s*(.+?)\s*$", source)
    value = match.group(1).strip() if match else fallback
    return re.sub(r"[{}\\]", "", value).strip()[:120] or fallback


def _render_engraved_html(score: dict[str, Any], svg_paths: list[Path], pdf_path: Path) -> str:
    pages = "".join(
        f'<figure><img src="{quote(path.name)}" alt="简谱第 {index} 页"></figure>'
        for index, path in enumerate(svg_paths, 1)
    )
    title = html.escape(str(score.get("title") or "简谱"))
    tempo = score.get("tempo") or {}
    tempo_bits = [str(tempo.get("words") or "").strip()]
    if tempo.get("tempo") is not None:
        tempo_bits.append(_tempo_text(tempo))
    tempo_text = " · ".join(bit for bit in tempo_bits if bit)
    tempo_markup = f"<p>{html.escape(tempo_text)}</p>" if tempo_text else ""
    review_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in score.get("warnings", []))
    review_markup = f"<aside><strong>复核提示</strong><ul>{review_items}</ul></aside>" if review_items else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · 专业简谱</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#eeeae3;color:#2e2924;font-family:system-ui,-apple-system,"Microsoft YaHei UI",sans-serif}}header{{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:10px max(18px,calc((100vw - 1100px)/2));background:rgba(250,247,241,.94);border-bottom:1px solid #d8d0c5;backdrop-filter:blur(12px)}}.heading{{min-width:0}}h1{{overflow:hidden;margin:0;font-size:15px;white-space:nowrap;text-overflow:ellipsis}}p{{margin:3px 0 0;color:#756a60;font-size:12px}}a{{flex:none;border:1px solid #c9bfb2;border-radius:8px;padding:7px 11px;color:inherit;text-decoration:none;background:#fff}}main{{max-width:1120px;margin:22px auto 48px;padding:0 14px}}figure{{margin:0 0 20px;background:#fff;box-shadow:0 4px 22px rgba(66,52,38,.10)}}img{{display:block;width:100%;height:auto}}aside{{margin-top:18px;padding:12px 14px;border-left:3px solid #a84d3f;background:#f7f0e8;color:#6f6257;font-size:13px;line-height:1.65}}@media print{{header,aside{{display:none}}body,main{{background:#fff;margin:0;padding:0}}figure{{box-shadow:none;break-after:page;margin:0}}}}</style>
</head><body><header><div class="heading"><h1>{title}</h1>{tempo_markup}</div><a href="{quote(pdf_path.name)}">下载 PDF</a></header><main>{pages}{review_markup}<aside>本页由 jianpu-ly {JIANPU_LY_VERSION} 与 GNU LilyPond {LILYPOND_VERSION} 专业排版。光学乐谱识别仍可能误判音高或节奏，演奏、教学或出版前请对照原谱复核。</aside></main></body></html>"""


def _beam_state(note: dict[str, Any], number: str = "1") -> str:
    for state in note.get("beam_states", []):
        if str(state.get("number")) == number:
            return str(state.get("type") or "").casefold()
    return ""


def _beam_runs(notes: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], int]]:
    runs: list[tuple[list[dict[str, Any]], int]] = []
    active: list[dict[str, Any]] = []
    for note in notes:
        state = _beam_state(note)
        if state == "begin":
            if active:
                runs.extend(([item], 0) for item in active)
            active = [note]
        elif state == "continue" and active:
            active.append(note)
        elif state == "end" and active:
            active.append(note)
            common = min((int(item.get("beams") or 0) for item in active), default=0)
            runs.append((active, common if len(active) > 1 else 0))
            active = []
        else:
            if active:
                runs.extend(([item], 0) for item in active)
                active = []
            runs.append(([note], 0))
    if active:
        runs.extend(([item], 0) for item in active)
    return runs


def _arc_spans(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_slurs: dict[str, int] = {}
    arcs: list[dict[str, Any]] = []
    for index, note in enumerate(notes):
        for slur in note.get("slurs", []):
            number = str(slur.get("number") or "1")
            if slur.get("type") == "stop":
                start = active_slurs.pop(number, None)
                arcs.append(
                    {
                        "start": 0 if start is None else start,
                        "end": index,
                        "lane": min(2, max(0, int(number) - 1 if number.isdigit() else 0)),
                        "class": "from-previous" if start is None else "",
                    }
                )
            elif slur.get("type") == "start":
                active_slurs[number] = index
    for number, start in active_slurs.items():
        arcs.append(
            {
                "start": start,
                "end": max(start + 0.5, len(notes) - 0.1),
                "lane": min(2, max(0, int(number) - 1 if number.isdigit() else 0)),
                "class": "to-next",
            }
        )
    tie_start: int | None = None
    for index, note in enumerate(notes):
        if note.get("tie_stop"):
            arcs.append(
                {
                    "start": 0 if tie_start is None else tie_start,
                    "end": index,
                    "lane": 0,
                    "class": "tie" + (" from-previous" if tie_start is None else ""),
                }
            )
            tie_start = None
        if note.get("tie_start"):
            tie_start = index
    if tie_start is not None:
        arcs.append(
            {
                "start": tie_start,
                "end": max(tie_start + 0.5, len(notes) - 0.1),
                "lane": 0,
                "class": "tie to-next",
            }
        )
    return [arc for arc in arcs if float(arc["end"]) > float(arc["start"])]


def _render_note_html(note: dict[str, Any], shared_beams: int = 0) -> str:
    remaining = max(0, int(note.get("beams") or 0) - shared_beams)
    classes = ["note"]
    if remaining:
        classes.append(f"beam-{min(4, remaining)}")
    if note.get("grace"):
        classes.append("grace")
    if note.get("risk") != "low":
        classes.append("needs-review")
    raw_token = str(note["token"])
    upper_dots = raw_token.count("\u0307")
    lower_dots = raw_token.count("\u0323")
    token = html.escape(raw_token.replace("\u0307", "").replace("\u0323", ""))
    upper_markup = (
        '<span class="octave-marks upper" aria-hidden="true">'
        + "".join("<i></i>" for _ in range(upper_dots))
        + "</span>"
        if upper_dots
        else ""
    )
    lower_markup = (
        '<span class="octave-marks lower" aria-hidden="true">'
        + "".join("<i></i>" for _ in range(lower_dots))
        + "</span>"
        if lower_dots
        else ""
    )
    if int(note.get("dots") or 0):
        token += f'<span class="duration-dot">{"·" * int(note["dots"])}</span>'
    dashes = ""
    length = float(note.get("quarter_length") or 0)
    if length >= 1.75:
        dashes = f'<span class="dash">{"—" * min(3, max(1, round(length) - 1))}</span>'
    duration_label = str(note.get("note_type") or f"{length:g}")
    title = html.escape(
        f"{note.get('source_pitch', '')} · {duration_label}"
        + (" · 自动校正，需复核" if note.get("auto_corrected") else ""),
        quote=True,
    )
    local_beams = "".join(
        f'<i class="local-beam local-beam-{index}" aria-hidden="true"></i>'
        for index in range(1, remaining + 1)
    )
    return (
        f'<span class="note-unit"><span class="{" ".join(classes)}" title="{title}">'
        f'{token}{upper_markup}{lower_markup}</span>{dashes}{local_beams}</span>'
    )


def _render_voice_measure(notes: list[dict[str, Any]]) -> str:
    groups: list[str] = []
    for run, shared_beams in _beam_runs(notes):
        rendered = "".join(_render_note_html(note, shared_beams) for note in run)
        beam_lines = "".join(
            f'<i class="shared-beam shared-beam-{index}"></i>'
            for index in range(1, shared_beams + 1)
        )
        groups.append(
            f'<span class="rhythm-group shared-beams-{shared_beams}">{rendered}{beam_lines}</span>'
        )
    arc_markup: list[str] = []
    count = max(1, len(notes))
    for arc in _arc_spans(notes):
        left = max(0.0, (float(arc["start"]) + 0.25) / count * 100)
        right = min(100.0, (float(arc["end"]) + 0.75) / count * 100)
        arc_markup.append(
            f'<i class="slur-arc {html.escape(str(arc["class"]))}" '
            f'style="left:{left:.3f}%;width:{max(5.0, right-left):.3f}%;--slur-lane:{int(arc["lane"])}"></i>'
        )
    return f'<div class="voice-measure"><div class="voice-notes">{"".join(groups)}</div>{"".join(arc_markup)}</div>'


def _render_html(score: dict[str, Any]) -> str:
    first_measure = score["parts"][0]["measures"][0] if score["parts"][0]["measures"] else None
    key_line = ""
    if first_measure:
        mode = "小调" if first_measure["key"]["mode"] == "minor" else "大调"
        key_line = f"1={first_measure['key']['tonic']} {mode} · {first_measure['time']} 拍"
        tempo = score.get("tempo") or {}
        tempo_words = str(tempo.get("words") or "").strip()
        tempo_value = tempo.get("tempo")
        if tempo_words:
            key_line += f" · {tempo_words}"
        if tempo_value:
            key_line += f" · {_tempo_text(tempo)}"
    sections: list[str] = []
    for part in score["parts"]:
        voices = sorted({voice for measure in part["measures"] for voice in measure["voices"]})
        voice_blocks: list[str] = []
        for voice in voices:
            measures: list[str] = []
            for measure in part["measures"]:
                voice_notes = [note for note in measure["notes"] if str(note["voice"]) == voice]
                direction_bits: list[str] = []
                for direction in measure.get("directions", []):
                    if direction.get("words"):
                        direction_bits.append(str(direction["words"]))
                    if direction.get("tempo"):
                        direction_bits.append(_tempo_text(direction))
                    if direction.get("dynamic"):
                        direction_bits.append(str(direction["dynamic"]))
                direction_html = (
                    f'<span class="measure-direction">{html.escape(" · ".join(direction_bits))}</span>'
                    if direction_bits
                    else ""
                )
                measures.append(
                    f'<span class="measure" data-measure="{html.escape(str(measure["number"]))}">'
                    + direction_html
                    + _render_voice_measure(voice_notes)
                    + "</span>"
                )
            voice_label = f'<div class="voice-label">声部 {html.escape(voice)}</div>' if len(voices) > 1 else ""
            voice_blocks.append(f'{voice_label}<div class="score-line">{"".join(measures)}</div>')
        sections.append(
            f'<section class="part"><h2>{html.escape(str(part["name"]))}</h2>{"".join(voice_blocks)}</section>'
        )
    warning_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in score["warnings"])
    warning_block = f'<aside class="warnings"><strong>结构复核提示</strong><ul>{warning_items}</ul></aside>' if warning_items else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(str(score['title']))} · 简谱</title>
<style>
:root{{--paper:#fbf7ef;--ink:#2c2824;--muted:#756a60;--rule:#d9cfc2;--accent:#ae4c3b}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:"Noto Serif SC","Source Han Serif SC","Microsoft YaHei",serif}}
main{{max-width:1000px;margin:0 auto;padding:52px 46px 72px}} header{{border-bottom:1px solid var(--rule);padding-bottom:18px;margin-bottom:28px}}
h1{{font-size:32px;line-height:1.25;margin:0 0 8px;letter-spacing:.03em}} .meta{{color:var(--muted);font-size:14px}} .key{{margin-top:11px;font:600 15px/1.4 "Microsoft YaHei UI",sans-serif}}
.part{{margin:0 0 34px}} h2{{font:600 17px/1.4 "Microsoft YaHei UI",sans-serif;margin:0 0 15px;color:var(--muted)}}
.voice-label{{margin:16px 0 7px;color:var(--muted);font:13px/1.4 sans-serif}} .score-line{{display:flex;flex-wrap:wrap;align-items:flex-end;gap:18px 0}}
.measure{{display:inline-flex;position:relative;flex-direction:column;justify-content:flex-end;min-height:58px;padding:15px 15px 8px;border-right:1px solid var(--ink);font:600 22px/1.4 "Noto Sans Mono CJK SC","Microsoft YaHei UI",monospace;white-space:nowrap}}
.measure:first-child{{border-left:1px solid var(--ink)}} .measure-direction{{position:absolute;left:12px;top:-7px;color:var(--muted);font:italic 11px/1.3 sans-serif}}
.voice-measure{{position:relative;padding-top:12px}} .voice-notes{{display:flex;align-items:flex-end;gap:9px}} .rhythm-group{{display:inline-flex;position:relative;align-items:flex-end;gap:10px;padding-bottom:6px}}
.note-unit{{display:inline-flex;position:relative;align-items:center;min-width:.65em;padding-bottom:6px}} .note{{display:inline-block;min-width:.65em;text-align:center;position:relative}} .note.needs-review{{text-decoration:underline dotted var(--accent);text-underline-offset:5px}}
.octave-marks{{position:absolute;left:50%;display:flex;flex-direction:column;align-items:center;gap:1px;transform:translateX(-50%);pointer-events:none}} .octave-marks.upper{{bottom:calc(100% - 1px)}} .octave-marks.lower{{top:calc(100% - 1px)}} .octave-marks i{{display:block;width:3.5px;height:3.5px;border-radius:50%;background:currentColor}}
.local-beam{{position:absolute;left:1px;right:1px;height:1.5px;background:currentColor;bottom:4px}} .local-beam-2{{bottom:1px}} .local-beam-3{{bottom:-2px}} .local-beam-4{{bottom:-5px}}
.shared-beam{{position:absolute;left:1px;right:1px;height:1.5px;background:currentColor;bottom:4px}} .shared-beam-2{{bottom:1px}} .shared-beam-3{{bottom:-2px}} .shared-beam-4{{bottom:-5px}}
.slur-arc{{position:absolute;z-index:2;top:calc(3px - var(--slur-lane)*5px);height:12px;border-top:1.2px solid currentColor;border-radius:50% 50% 0 0;pointer-events:none}} .slur-arc.tie{{border-top-color:var(--muted)}} .slur-arc.from-previous{{border-top-left-radius:0}} .slur-arc.to-next{{border-top-right-radius:0}}
.grace{{font-size:.72em;transform:translateY(-.25em)}} .duration-dot{{font-size:.68em;margin-left:1px;vertical-align:.35em}} .dash{{font-size:.72em;letter-spacing:-.08em;color:var(--muted)}}
.notice,.warnings{{margin-top:28px;padding:14px 16px;border-left:3px solid var(--accent);background:#f5ede4;color:var(--muted);font:14px/1.7 sans-serif}} .warnings ul{{margin:6px 0 0;padding-left:20px}}
@media(max-width:640px){{main{{padding:28px 18px 46px}}h1{{font-size:25px}}.score-line{{gap:10px 0}}.measure{{font-size:19px;gap:9px;padding:7px 11px 8px}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:12mm}}.notice{{break-inside:avoid}}}}
</style></head><body><main><header><h1>{html.escape(str(score['title']))}</h1><div class="meta">Elren 本地五线谱转简谱</div><div class="key">{html.escape(key_line)}</div></header>
{"".join(sections)}{warning_block}<div class="notice">自动光学乐谱识别可能误判音高、节奏、连音或声部。演奏、教学或出版前请对照原谱复核。</div>
</main></body></html>"""


class JianpuOMRTool(ToolPlugin):
    name = "jianpu_omr"
    description = (
        "Convert staff notation (五线谱) images, scans, PDFs, MusicXML, or MXL into editable numbered notation "
        "(简谱) and professionally typeset SVG/PDF through bundled jianpu-ly and GNU LilyPond; also export "
        "editable JLY/LY, HTML preview, UTF-8 text, and structured JSON. For image/PDF input, recognition runs only in "
        "the package-local Docker Lite Audiveris runtime; the main AI process never loads the recognition model. "
        "MusicXML exports use concert (sounding) pitch; JSON retains written_pitch/written_midi and instrument transposition. "
        "Use this tool whenever the user asks for 五线谱转简谱, staff-to-jianpu, or numbered-notation conversion."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "convert"]},
            "input_path": {
                "type": "string",
                "description": "Absolute attachment path or workspace-relative score image, PDF, MusicXML, or MXL path",
            },
            "output_name": {
                "type": "string",
                "description": "Optional safe base name for exported artifacts",
                "maxLength": 80,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, attachment_root: Path | None = None) -> None:
        self.attachment_root = attachment_root.resolve() if attachment_root else None

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") == "status" else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        if arguments.get("action") == "status":
            return "检查本地五线谱识别运行时"
        return f"将五线谱转换并导出为简谱：{arguments.get('input_path', '')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments, context)

    def _resolve_input(self, requested: str, context: ToolContext) -> Path:
        workspace = Path(context.workspace).resolve()
        raw = Path(requested)
        path = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
        if not _inside(path, workspace) and not (
            self.attachment_root and _inside(path, self.attachment_root)
        ):
            raise PermissionError("Score input is outside the workspace and attachment directory")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.suffix.casefold() not in SUPPORTED_INPUTS:
            raise ValueError(f"Unsupported score format: {path.suffix or '[none]'}")
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("Score input exceeds the 50 MB local-processing limit")
        return path

    @staticmethod
    def _application_root() -> Path:
        """Return the Elren installation root, independent of the task workspace."""
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            resources = Path(sys.executable).resolve().parent.parent
            if resources.name == "Resources" and resources.parent.name == "Contents":
                return resources / "bundle"
        module_path = Path(__file__).resolve()
        try:
            return module_path.parents[3]
        except IndexError:
            return Path.cwd().resolve()

    @classmethod
    def _notation_is_packaged(
        cls, workspace: Path, notation: tuple[Path, str] | None, engraver: tuple[Path, str] | None,
    ) -> bool:
        """Discovery mode is not package membership, including explicit paths."""
        if notation is None or engraver is None:
            return False
        install_root = os.environ.get("ELREN_WORKSPACE", "").strip()
        roots = [cls._application_root(), workspace]
        if install_root:
            roots.append(Path(install_root).expanduser())
        executable_name = "lilypond.exe" if os.name == "nt" else "lilypond"
        for root in roots:
            canonical = root.resolve()
            native = canonical / "work/tool-runtime/native"
            expected_notation = (native / "jianpu-ly").resolve()
            expected_engraver = (native / "lilypond" / f"lilypond-{LILYPOND_VERSION}" / "bin" / executable_name).resolve()
            if (expected_notation.is_relative_to(canonical) and expected_engraver.is_relative_to(canonical)
                    and notation[0].resolve() == expected_notation and engraver[0].resolve() == expected_engraver):
                return True
        return False

    @classmethod
    def _audiveris_runtime(cls, workspace: Path) -> tuple[Path, str] | None:
        configured = os.environ.get("ELREN_AUDIVERIS_HOME", "").strip()
        install_root = os.environ.get("ELREN_WORKSPACE", "").strip()
        candidates: list[tuple[Path, str]] = []
        seen: set[str] = set()

        def add_candidate(root: Path, mode: str) -> None:
            resolved = root.expanduser().resolve()
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key not in seen:
                candidates.append((resolved, mode))
                seen.add(key)

        # A task may run against any user-selected project directory.  The
        # bundled OMR runtime belongs to the Elren installation, not that task
        # directory, so derive it from this module before considering fallbacks.
        add_candidate(
            cls._application_root() / "work" / "tool-runtime" / "native" / "audiveris",
            "docker-lite-package-local",
        )
        if install_root:
            add_candidate(
                Path(install_root) / "work" / "tool-runtime" / "native" / "audiveris",
                "docker-lite-package-local",
            )
        add_candidate(
            workspace / "work" / "tool-runtime" / "native" / "audiveris",
            "docker-lite-package-local",
        )
        if configured:
            add_candidate(Path(configured), "explicit-local-fallback")
        if os.name == "nt":
            add_candidate(Path("C:/Program Files/Audiveris"), "host-installed-fallback")
        else:
            add_candidate(Path("/Applications/Audiveris.app/Contents"), "host-installed-fallback")
            add_candidate(Path("/opt/audiveris"), "host-installed-fallback")
        for root, mode in candidates:
            java = root / "runtime" / "bin" / ("java.exe" if os.name == "nt" else "java")
            app = root / "app"
            if java.is_file() and (app / "audiveris.jar").is_file():
                return root.resolve(), mode
        return None

    @classmethod
    def _lilypond_runtime(cls, workspace: Path) -> tuple[Path, str] | None:
        configured = os.environ.get("ELREN_LILYPOND_HOME", "").strip()
        install_root = os.environ.get("ELREN_WORKSPACE", "").strip()
        executable_name = "lilypond.exe" if os.name == "nt" else "lilypond"
        candidates: list[tuple[Path, str]] = []
        if configured:
            configured_path = Path(configured).expanduser()
            candidates.append(
                (configured_path if configured_path.is_file() else configured_path / "bin" / executable_name, "explicit-package-runtime")
            )
        for root in [cls._application_root(), Path(install_root) if install_root else None, workspace]:
            if root is not None:
                candidates.append(
                    (
                        root / "work" / "tool-runtime" / "native" / "lilypond" / f"lilypond-{LILYPOND_VERSION}" / "bin" / executable_name,
                        "package-local-open-source",
                    )
                )
        system = shutil.which("lilypond")
        if system:
            candidates.append((Path(system), "host-installed-fallback"))
        seen: set[str] = set()
        for executable, mode in candidates:
            resolved = executable.resolve()
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key not in seen and resolved.is_file():
                return resolved, mode
            seen.add(key)
        return None

    @classmethod
    def _jianpu_runtime(cls, workspace: Path) -> tuple[Path, str] | None:
        configured = os.environ.get("ELREN_JIANPU_LY_HOME", "").strip()
        install_root = os.environ.get("ELREN_WORKSPACE", "").strip()
        candidates: list[tuple[Path, str]] = []
        if configured:
            candidates.append((Path(configured).expanduser(), "explicit-package-runtime"))
        for root in [cls._application_root(), Path(install_root) if install_root else None, workspace]:
            if root is not None:
                candidates.append(
                    (
                        root / "work" / "tool-runtime" / "native" / "jianpu-ly",
                        "docker-lite-package-local",
                    )
                )
        seen: set[str] = set()
        for root, mode in candidates:
            resolved = root.resolve()
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key in seen:
                continue
            seen.add(key)
            required = [resolved / "jianpu_ly.py", resolved / "LICENSE", resolved / "UPSTREAM.json"]
            if all(path.is_file() for path in required):
                try:
                    provenance = json.loads((resolved / "UPSTREAM.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                expected_hash = str(provenance.get("source_sha256") or "").casefold()
                if (
                    str(provenance.get("version") or "") == JIANPU_LY_VERSION
                    and expected_hash
                    and _sha256(resolved / "jianpu_ly.py").casefold() == expected_hash
                ):
                    return resolved, mode
        return None

    @classmethod
    def _compile_jianpu_source(cls, source_text: str, workspace: Path) -> tuple[str, str]:
        runtime = cls._jianpu_runtime(workspace)
        if runtime is None:
            raise RuntimeError("未找到 Docker Lite 中的 jianpu-ly 开源记谱运行时。")
        runtime_root, runtime_mode = runtime
        module_path = runtime_root / "jianpu_ly.py"
        module = types.ModuleType("_elren_jianpu_ly_runtime")
        module.__file__ = str(module_path)
        with _JIANPU_LY_LOCK:
            source_code = module_path.read_text(encoding="utf-8")
            # The runtime is package-local and accepted only after its pinned
            # source SHA-256 matches UPSTREAM.json above.
            exec(compile(compiler_ast(source_code, str(module_path)), str(module_path), "exec"), module.__dict__)  # noqa: S102
            module._lilypond_minor_version = 26
            previous_sloppy_bars = os.environ.get("j2ly_sloppy_bars")
            os.environ["j2ly_sloppy_bars"] = "1"
            try:
                lilypond_text = module.process_input(source_text)
            finally:
                if previous_sloppy_bars is None:
                    os.environ.pop("j2ly_sloppy_bars", None)
                else:
                    os.environ["j2ly_sloppy_bars"] = previous_sloppy_bars
        lilypond_text = re.sub(
            r"#\(ly:make-moment ([0-9]+) ([0-9]+)\)", r"#(/ \1 \2)", lilypond_text
        )
        return apply_reading_style(lilypond_text), runtime_mode

    @classmethod
    def _typeset_jianpu_source(
        cls,
        source_text: str,
        workspace: Path,
        export_root: Path,
        source_path: Path,
        lilypond_path: Path,
        output_prefix: Path,
    ) -> tuple[list[Path], Path, Path | None, dict[str, Any]]:
        runtime = cls._lilypond_runtime(workspace)
        if runtime is None:
            raise RuntimeError("未找到随包 GNU LilyPond 排版引擎，无法产生可信的专业乐谱。")
        executable, runtime_mode = runtime
        cls._prepare_lilypond_cache(executable)
        source_path.write_text(source_text, encoding="utf-8")
        lilypond_text, notation_runtime_mode = cls._compile_jianpu_source(source_text, workspace)
        lilypond_path.write_text(lilypond_text, encoding="utf-8")
        environment = credential_safe_environment(
            {"ELREN_ENGRAVER_OFFLINE": "1", "GUILE_AUTO_COMPILE": "0"}
        )
        bootstrap_arguments = windows_engraver_arguments()
        logs: list[str] = []
        for format_argument in ("--pdf", "--svg"):
            try:
                completed = subprocess.run(
                    [
                        str(executable),
                        *bootstrap_arguments,
                        format_argument,
                        "-dno-point-and-click",
                        "-o",
                        str(output_prefix),
                        str(lilypond_path),
                    ],
                    cwd=export_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=3 * 60,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("专业乐谱排版超过 3 分钟，已停止。") from exc
            full_log = f"{completed.stdout}\n{completed.stderr}"
            combined = full_log[-12_000:]
            logs.append(combined)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"GNU LilyPond 排版未完成（代码 {completed.returncode}）：{combined[-1200:]}"
                )
            if bootstrap_arguments and WINDOWS_XML_READY not in full_log:
                raise RuntimeError("Windows 乐谱排版引擎的系统 XML 库初始化未完成。")
        pdf_path = output_prefix.with_suffix(".pdf")
        svg_paths = sorted(
            export_root.glob(f"{output_prefix.name}*.svg"),
            key=lambda path: (len(path.name), path.name),
        )
        if not pdf_path.is_file() or not svg_paths:
            raise RuntimeError("专业排版引擎未生成完整的 PDF/SVG 产物。")
        midi_path = next((path for suffix in (".mid", ".midi")
                          if (path := output_prefix.with_suffix(suffix)).is_file()), None)
        return svg_paths, pdf_path, midi_path, {
            "notation_engine": f"jianpu-ly {JIANPU_LY_VERSION}",
            "typesetter": f"GNU LilyPond {LILYPOND_VERSION}",
            "notation_runtime_mode": notation_runtime_mode,
            "engraver_runtime_mode": runtime_mode,
            "windows_xml_lifetime_guard": bool(bootstrap_arguments),
            "network_access_requested": False,
            "log_tail": "\n".join(logs)[-2_000:],
        }

    @staticmethod
    def _prepare_lilypond_cache(executable: Path) -> None:
        """Keep extracted Guile bytecode newer than source without changing file contents."""

        runtime_root = executable.resolve().parent.parent
        with _LILYPOND_CACHE_LOCK:
            if runtime_root in _PREPARED_LILYPOND_ROOTS:
                return
            # The distribution contains two independent bytecode trees.
            # Correcting only Guile's stdlib still makes every launch interpret
            # extracted LilyPond sources instead of loading its bundled cache.
            trees = (
                (runtime_root / "share/guile/3.0", runtime_root / "lib/guile/3.0/ccache"),
                (runtime_root / f"share/lilypond/{LILYPOND_VERSION}/scm",
                 runtime_root / f"lib/lilypond/{LILYPOND_VERSION}/ccache"),
            )
            for source_root, cache_root in trees:
                source_files = list(source_root.rglob("*.scm")) if source_root.is_dir() else []
                compiled_files = list(cache_root.rglob("*.go")) if cache_root.is_dir() else []
                if source_files and compiled_files:
                    newest_source_ns = max(path.stat().st_mtime_ns for path in source_files)
                    stale = [path for path in compiled_files if path.stat().st_mtime_ns <= newest_source_ns]
                    target_seconds = newest_source_ns / 1_000_000_000 + 5
                    try:
                        for path in stale:
                            os.utime(path, (target_seconds, target_seconds))
                    except OSError as exc:
                        raise RuntimeError(
                            "LilyPond 便携运行时的 Guile 缓存时间戳无法校正，请确认安装目录可写。"
                        ) from exc
            _PREPARED_LILYPOND_ROOTS.add(runtime_root)

    @classmethod
    def _status(cls, workspace: Path) -> dict[str, Any]:
        runtime = cls._audiveris_runtime(workspace)
        engraver = cls._lilypond_runtime(workspace)
        notation = cls._jianpu_runtime(workspace)
        packaged_notation = cls._notation_is_packaged(workspace, notation, engraver)
        text_ocr_ready = False
        if runtime:
            try:
                validate_tessdata(runtime[0])
                text_ocr_ready = True
            except (OSError, ValueError):
                pass
        return {
            "ok": True,
            "ready": runtime is not None and engraver is not None and notation is not None,
            "engine": "Audiveris 5.11.0" if runtime else None,
            "runtime_mode": runtime[1] if runtime else "unavailable",
            "engraver_ready": engraver is not None,
            "notation_engine": f"jianpu-ly {JIANPU_LY_VERSION}" if notation else None,
            "notation_runtime_ready": notation is not None,
            "notation_runtime_mode": notation[1] if notation else "unavailable",
            "typesetter": f"GNU LilyPond {LILYPOND_VERSION}" if engraver else None,
            "engraver_runtime_mode": engraver[1] if engraver else "unavailable",
            "docker_lite_model_present": bool(
                runtime and runtime[1] == "docker-lite-package-local"
            ),
            "docker_lite_notation_present": packaged_notation,
            "offline_text_ocr_ready": text_ocr_ready,
            "offline_text_ocr_languages": list(LANGUAGES) if text_ocr_ready else [],
            "cloud_upload": False,
            "message": (
                "本地 Docker Lite 识别与开源专业简谱排版引擎已就绪。"
                if runtime and runtime[1] == "docker-lite-package-local" and packaged_notation
                else "本地识别与开源排版可用，但记谱引擎使用外部路径，并非完整随包配置。"
                if runtime and engraver and notation and not packaged_notation
                else "可使用本机 Audiveris 回退；发布包的 Docker Lite 识别组件尚未就绪。"
                if runtime
                else "未找到本地 Audiveris 识别引擎；MusicXML/MXL 可在排版引擎就绪时直接转简谱。"
            ) + (" 离线文字 OCR 语言数据不完整；歌词等文字可能缺失。" if runtime and not text_ocr_ready else ""),
        }

    @classmethod
    def _engrave(
        cls, score: dict[str, Any], workspace: Path, export_root: Path, base_name: str
    ) -> tuple[Path, Path, list[Path], Path, Path | None, dict[str, Any]]:
        source_path = export_root / f"{base_name}-专业简谱.jly"
        lilypond_path = export_root / f"{base_name}-专业简谱.ly"
        output_prefix = export_root / f"{base_name}-专业简谱"
        source_text = _score_to_jianpu_source(score)
        svg_paths, pdf_path, midi_path, runtime_info = cls._typeset_jianpu_source(
            source_text, workspace, export_root, source_path, lilypond_path, output_prefix
        )
        html_path = export_root / f"{base_name}-简谱.html"
        html_path.write_text(_render_engraved_html(score, svg_paths, pdf_path), encoding="utf-8")
        return source_path, lilypond_path, svg_paths, pdf_path, midi_path, runtime_info

    @staticmethod
    def _recognize(input_path: Path, workspace: Path, job_root: Path) -> tuple[Path, dict[str, Any]]:
        runtime = JianpuOMRTool._audiveris_runtime(workspace)
        if runtime is None:
            raise RuntimeError(
                "本地 Docker Lite Audiveris 识别引擎不可用。请安装发布包中的识别组件，或先提供 MusicXML/MXL。"
            )
        runtime_root, runtime_mode = runtime
        java = runtime_root / "runtime" / "bin" / ("java.exe" if os.name == "nt" else "java")
        text_ocr_warning = ""
        try:
            tessdata = validate_tessdata(runtime_root)
        except (OSError, ValueError):
            tessdata = None
            text_ocr_warning = "离线文字 OCR 语言数据缺失或未通过完整性校验；音符识别仍继续，但歌词等文字可能缺失。"
        environment, runtime_options = audiveris_environment(job_root, tessdata=tessdata)
        isolated_input = job_root / f"input{input_path.suffix.casefold()}"
        shutil.copy2(input_path, isolated_input)
        output_dir = job_root / "recognized"
        output_dir.mkdir(parents=True)
        arguments = [
            str(java),
            "-Dfile.encoding=UTF-8",
            *runtime_options,
            "--enable-native-access=ALL-UNNAMED",
            "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED",
            "-Xms256m",
            "-Xmx3G",
            *audiveris_bootstrap_args(runtime_root),
            "-batch",
            "-transcribe",
            "-export",
            "-save",
            *(["-constant", "org.audiveris.omr.text.Language.defaultSpecification=" + "+".join(LANGUAGES)]
              if tessdata is not None else []),
            "-output",
            str(output_dir),
            str(isolated_input),
        ]
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10 * 60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=environment,
                cwd=job_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("本地乐谱识别超过 10 分钟，已停止；请裁剪为清晰的单页后重试。") from exc
        full_log = f"{completed.stdout}\n{completed.stderr}"
        combined = full_log[-8_000:]
        text_ocr_ready = tessdata is not None and not any(marker in full_log.casefold() for marker in (
            "no installed ocr languages", "collection of supported languages is empty",
            "could not initialize tessbaseapi", "error in loading local ocr languages",
            "missing support for 'eng+chi_sim+chi_tra'",
        ))
        if tessdata is not None and not text_ocr_ready:
            text_ocr_warning = "离线文字 OCR 数据完整，但原生识别器未能加载语言模型；速度、歌词等文字可能缺失，请复核。"
        if completed.returncode != 0:
            raise RuntimeError(f"本地乐谱识别未完成（代码 {completed.returncode}）：{combined[-800:]}")
        candidates = [
            path
            for path in output_dir.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in MUSICXML_INPUTS
            and "meta-inf" not in {part.casefold() for part in path.parts}
        ]
        if not candidates:
            raise RuntimeError("识别引擎没有生成 MusicXML；请确认谱线完整、图像正对且清晰。")
        musicxml = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        return musicxml, {
            "engine": "Audiveris 5.11.0",
            "runtime_mode": runtime_mode,
            "network_access_requested": False,
            "offline_text_ocr_ready": text_ocr_ready,
            "offline_text_ocr_languages": list(LANGUAGES) if text_ocr_ready else [],
            "text_ocr_warning": text_ocr_warning,
            "log_tail": combined[-2_000:],
            "omr_log_tail": combined[-2_000:],
        }

    def _execute_sync(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = Path(context.workspace).resolve()
        action = str(arguments.get("action") or "")
        if action == "status":
            return self._status(workspace)
        if action != "convert":
            raise ValueError(f"Unsupported jianpu action: {action}")
        requested = str(arguments.get("input_path") or "").strip()
        if not requested:
            raise ValueError("input_path is required for conversion")
        input_path = self._resolve_input(requested, context)
        base_name = _safe_stem(str(arguments.get("output_name") or input_path.stem))
        export_root = workspace / "outputs" / "jianpu" / f"{base_name}-{context.task_id[:8] or uuid4().hex[:8]}"
        if export_root.exists():
            export_root = export_root.with_name(f"{export_root.name}-{uuid4().hex[:6]}")
        export_root.mkdir(parents=True, exist_ok=False)
        runtime_info: dict[str, Any] = {
            "engine": "MusicXML direct",
            "runtime_mode": "docker-lite-parser",
            "network_access_requested": False,
        }
        work_root = workspace / "work"
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="elren-jianpu-", dir=str(work_root)) as temporary:
            job_root = Path(temporary)
            musicxml_path = input_path
            recognized_input = input_path.suffix.casefold() not in MUSICXML_INPUTS
            if recognized_input:
                musicxml_path, runtime_info = self._recognize(input_path, workspace, job_root)
            score = _parse_musicxml(
                _read_musicxml(musicxml_path), normalize_omr=recognized_input
            )
            if runtime_info.get("text_ocr_warning"):
                score["warnings"].append(runtime_info["text_ocr_warning"])
            source_identification = _identify_source_score(input_path, score, omr_generated=recognized_input)
            source_target = export_root / f"{base_name}-识别结果{musicxml_path.suffix.casefold()}"
            shutil.copy2(musicxml_path, source_target)

        text_path = export_root / f"{base_name}-简谱.txt"
        json_path = export_root / f"{base_name}-简谱.json"
        text_path.write_text("\n".join(_score_lines(score)) + "\n", encoding="utf-8")
        json_path.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        jly_path, ly_path, svg_paths, pdf_path, midi_path, engraving_info = self._engrave(
            score, workspace, export_root, base_name
        )
        runtime_info.update(engraving_info)
        html_path = export_root / f"{base_name}-简谱.html"
        artifacts = [
            html_path,
            pdf_path,
            *svg_paths,
            *([midi_path] if midi_path else []),
            jly_path,
            ly_path,
            text_path,
            json_path,
            source_target,
        ]
        identified_title = source_identification.get("title")
        title_message = (
            f"原谱曲名可能是《{identified_title}》，请确认标题识别。"
            if identified_title and source_identification.get("needs_confirmation")
            else f"原谱曲名是《{identified_title}》。"
            if identified_title
            else "未能可靠识别原谱曲名，请提供更清晰的标题区域或手动确认。"
        )
        return {
            "ok": True,
            "message": (
                "五线谱已在本地转换，并由 jianpu-ly + GNU LilyPond 专业排版为 SVG/PDF；"
                f"{title_message} 自动识别结果需要对照原谱复核。"
            ),
            "source_identification": source_identification,
            "primary_path": str(html_path),
            "paths": [str(path) for path in artifacts],
            "artifacts": [
                {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256(path)}
                for path in artifacts
            ],
            "stats": score["stats"],
            "tempo": score.get("tempo"),
            "pitch_basis": score.get("pitch_basis", "concert"),
            "pitch_contract": "Exported Jianpu, staff reference and MIDI use sounding/concert pitch. JSON written_pitch, written_midi and written_key retain the input's written notation.",
            "warnings": score["warnings"],
            "quality": {
                "review_required": input_path.suffix.casefold() not in MUSICXML_INPUTS or bool(score["stats"]["review_count"]),
                "reason": "OMR is probabilistic; compare exported notation with the source score before performance or publication.",
            },
            "runtime": runtime_info,
        }


def _render_staff_html(title: str, svg_paths: list[Path], pdf_path: Path) -> str:
    pages = "".join(
        f'<figure><img src="{quote(path.name)}" alt="五线谱第 {index} 页"></figure>'
        for index, path in enumerate(svg_paths, 1)
    )
    safe_title = html.escape(title)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_title} · 五线谱</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#eeeae3;color:#2e2924;font-family:system-ui,-apple-system,"Microsoft YaHei UI",sans-serif}}header{{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:18px;padding:12px max(18px,calc((100vw - 1100px)/2));background:rgba(250,247,241,.94);border-bottom:1px solid #d8d0c5;backdrop-filter:blur(12px)}}h1{{overflow:hidden;margin:0;font-size:15px;white-space:nowrap;text-overflow:ellipsis}}a{{flex:none;border:1px solid #c9bfb2;border-radius:8px;padding:7px 11px;color:inherit;text-decoration:none;background:#fff}}main{{max-width:1120px;margin:22px auto 48px;padding:0 14px}}figure{{margin:0 0 20px;background:#fff;box-shadow:0 4px 22px rgba(66,52,38,.10)}}img{{display:block;width:100%;height:auto}}aside{{margin-top:18px;padding:12px 14px;border-left:3px solid #a84d3f;background:#f7f0e8;color:#6f6257;font-size:13px;line-height:1.65}}@media print{{header,aside{{display:none}}body,main{{background:#fff;margin:0;padding:0}}figure{{box-shadow:none;break-after:page;margin:0}}}}</style>
</head><body><header><h1>{safe_title}</h1><a href="{quote(pdf_path.name)}">下载 PDF</a></header><main>{pages}<aside>五线谱由 jianpu-ly {JIANPU_LY_VERSION} 的 WithStaff 转换并由 GNU LilyPond {LILYPOND_VERSION} 排版；页面保留原简谱作为逐音对照，便于发现与修正输入错误。</aside></main></body></html>"""


class JianpuToStaffTool(ToolPlugin):
    name = "jianpu_to_staff"
    description = (
        "Convert editable numbered notation (简谱) in jianpu-ly JLY/TXT syntax, or Elren's structured Jianpu JSON, "
        "into professionally engraved Western staff notation (五线谱). Uses package-local open-source jianpu-ly "
        "WithStaff and GNU LilyPond entirely offline, exporting HTML, PDF, SVG, MIDI, JLY, and LY. The engraved "
        "score retains the source Jianpu as a synchronized reference. Use for 简谱转五线谱 or numbered-notation-to-staff requests."
        " For JSON editing, token is the pitch in the declared Jianpu key; quarter_length is sounding duration and "
        "time_modification records supported tuplet ratios. Keep written/source metadata consistent when editing; "
        "changing midi or source_pitch alone does not replace the editable token."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "convert"]},
            "input_path": {
                "type": "string",
                "description": "Absolute attachment path or workspace-relative .jly, standard jianpu-ly .txt, or Elren Jianpu .json path",
            },
            "output_name": {
                "type": "string",
                "description": "Optional safe base name for exported staff-notation artifacts",
                "maxLength": 80,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, attachment_root: Path | None = None) -> None:
        self.attachment_root = attachment_root.resolve() if attachment_root else None

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") == "status" else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        if arguments.get("action") == "status":
            return "检查简谱转五线谱开源运行时"
        return f"将简谱转换并导出为五线谱：{arguments.get('input_path', '')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments, context)

    def _resolve_input(self, requested: str, context: ToolContext) -> Path:
        workspace = Path(context.workspace).resolve()
        raw = Path(requested)
        path = raw.resolve() if raw.is_absolute() else (workspace / raw).resolve()
        if not _inside(path, workspace) and not (
            self.attachment_root and _inside(path, self.attachment_root)
        ):
            raise PermissionError("Jianpu input is outside the workspace and attachment directory")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.suffix.casefold() not in JIANPU_STAFF_INPUTS:
            raise ValueError("简谱转五线谱支持 .jly、标准 jianpu-ly .txt 或 Elren 简谱 .json。")
        if path.stat().st_size > MAX_JIANPU_SOURCE_BYTES:
            raise ValueError("简谱输入超过 4 MB 安全限制。")
        return path

    @staticmethod
    def _source_from_input(input_path: Path) -> str:
        if input_path.suffix.casefold() != ".json":
            return _validate_jianpu_source(input_path.read_text(encoding="utf-8-sig", errors="strict"))
        score = json.loads(input_path.read_text(encoding="utf-8-sig", errors="strict"))
        if not isinstance(score, dict) or not isinstance(score.get("parts"), list):
            raise ValueError("JSON 不是 Elren 导出的结构化简谱。")
        return _validate_jianpu_source(_score_to_jianpu_source(score))

    @staticmethod
    def _status(workspace: Path) -> dict[str, Any]:
        notation = JianpuOMRTool._jianpu_runtime(workspace)
        engraver = JianpuOMRTool._lilypond_runtime(workspace)
        ready = notation is not None and engraver is not None
        packaged_notation = JianpuOMRTool._notation_is_packaged(workspace, notation, engraver)
        return {
            "ok": True,
            "ready": ready,
            "notation_engine": f"jianpu-ly {JIANPU_LY_VERSION}" if notation else None,
            "notation_runtime_mode": notation[1] if notation else "unavailable",
            "typesetter": f"GNU LilyPond {LILYPOND_VERSION}" if engraver else None,
            "engraver_runtime_mode": engraver[1] if engraver else "unavailable",
            "docker_lite_notation_present": packaged_notation,
            "cloud_upload": False,
            "message": (
                "Docker Lite 中的简谱转五线谱开源记谱与排版引擎已就绪。"
                if packaged_notation
                else "本地开源记谱与排版引擎已就绪；当前使用外部路径，并非完整随包配置。"
                if ready
                else "Docker Lite 缺少 jianpu-ly 或 GNU LilyPond，暂不能稳定导出五线谱。"
            ),
        }

    def _execute_sync(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        workspace = Path(context.workspace).resolve()
        action = str(arguments.get("action") or "")
        if action == "status":
            return self._status(workspace)
        if action != "convert":
            raise ValueError(f"Unsupported staff conversion action: {action}")
        requested = str(arguments.get("input_path") or "").strip()
        if not requested:
            raise ValueError("input_path is required for conversion")
        input_path = self._resolve_input(requested, context)
        source = self._source_from_input(input_path)
        source = _with_western_staff(source)
        base_name = _safe_stem(str(arguments.get("output_name") or input_path.stem))
        title = _jianpu_title(source, base_name)
        export_root = workspace / "outputs" / "staff" / f"{base_name}-{context.task_id[:8] or uuid4().hex[:8]}"
        if export_root.exists():
            export_root = export_root.with_name(f"{export_root.name}-{uuid4().hex[:6]}")
        export_root.mkdir(parents=True, exist_ok=False)
        source_path = export_root / f"{base_name}-五线谱对照版.jly"
        lilypond_path = export_root / f"{base_name}-五线谱对照版.ly"
        output_prefix = export_root / f"{base_name}-五线谱对照版"
        svg_paths, pdf_path, midi_path, runtime_info = JianpuOMRTool._typeset_jianpu_source(
            source, workspace, export_root, source_path, lilypond_path, output_prefix
        )
        html_path = export_root / f"{base_name}-五线谱.html"
        html_path.write_text(_render_staff_html(title, svg_paths, pdf_path), encoding="utf-8")
        artifacts = [
            html_path,
            pdf_path,
            *svg_paths,
            *([midi_path] if midi_path else []),
            source_path,
            lilypond_path,
        ]
        return {
            "ok": True,
            "message": (
                "简谱已在本地转换为专业五线谱，并保留逐音简谱对照；已导出 HTML、PDF、SVG、MIDI 与可编辑源文件。"
            ),
            "primary_path": str(html_path),
            "paths": [str(path) for path in artifacts],
            "artifacts": [
                {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256(path)}
                for path in artifacts
            ],
            "quality": {
                "review_required": True,
                "reason": "The staff is deterministic from the supplied Jianpu; verify the Jianpu source if a pitch or rhythm is wrong.",
            },
            "runtime": runtime_info,
        }
