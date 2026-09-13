from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import tempfile
import unicodedata
import warnings
from importlib.util import find_spec
from pathlib import Path
from typing import Any

TITLE_KEYWORDS = re.compile(
    r"(?:曲|首|歌|谣|舞|奏鸣|协奏|前奏|练习|变奏|赋格|小品|进行曲|幻想曲|圆舞曲|夜曲)"
)
NON_TITLE = re.compile(
    r"(?i)(?:allegro|andante|adagio|moderato|presto|largo|letellier|composer|op\.?\s*\d|"
    r"\b[0-9]{3,4}\s*[-–—]\s*[0-9]{0,4}\b|[♩♪]=?\s*\d+)"
)
CONTEXT_HINT = re.compile(
    r"(?:[一二三四五六七八九十0-9]+级|练习曲|教程|曲集|第[一二三四五六七八九十0-9]+册)"
)
TEMPO_WORD = re.compile(
    r"(?i)\b(?:larghissimo|grave|largo|larghetto|lento|adagio|adagietto|andante|"
    r"andantino|marcia\s+moderato|moderato|allegretto|allegro|vivace|vivacissimo|"
    r"presto|prestissimo)\b|最急板|急板|快板|小快板|中板|小行板|行板|柔板|慢板|广板"
)
BPM_MARK = re.compile(r"=\s*(?P<bpm>[1-9]\d{0,2}(?:[.,]\d{1,3})?)(?![\d.,])")
TEMPO_UNIT = re.compile(r"(?P<note>[♩♪𝅝𝅗𝅥𝅘𝅥𝅘𝅥𝅮𝅘𝅥𝅯𝅘𝅥𝅰𝅘𝅥𝅱])(?P<dots>(?:\s*[.·•𝅭]){0,3})\s*$")
TEMPO_UNITS = {"𝅝": "whole", "𝅗𝅥": "half", "♩": "quarter", "𝅘𝅥": "quarter",
               "♪": "eighth", "𝅘𝅥𝅮": "eighth", "𝅘𝅥𝅯": "16th", "𝅘𝅥𝅰": "32nd", "𝅘𝅥𝅱": "64th"}
# RapidOCR 3.9.2 default_models.yaml, verified against the shipped local bytes.
# Its downloader otherwise replaces corrupt files automatically, so metadata
# recognition must fail before constructing the engine if a model is damaged.
OFFLINE_OCR_MODEL_SHA256 = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}


def _bounds(box: Any) -> tuple[float, float, float, float] | None:
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def clean_title(value: Any) -> str:
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    text = re.sub(r"^\s*(?:第\s*)?[0-9一二三四五六七八九十]+\s*[.、．:]\s*", "", text)
    text = re.sub(r"^\s*[0-9]+\s*[.、．:]\s*", "", text)
    text = re.sub(r"[★☆*※]+$", "", text).strip(" -—–·•|（）()[]【】")
    # OCR often inserts spaces between individually recognized Han characters.
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    return text[:120]


def select_title(
    lines: list[dict[str, Any]], image_width: float, image_height: float
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    contexts: list[str] = []
    for line in lines:
        raw = str(line.get("text") or "").strip()
        text = clean_title(raw)
        bounds = _bounds(line.get("box"))
        if not text or bounds is None:
            continue
        left, top, right, bottom = bounds
        if top > image_height * 0.38:
            continue
        if CONTEXT_HINT.search(text):
            contexts.append(text)
        compact = re.sub(r"\s+", "", text)
        if len(compact) < 2 or len(compact) > 60:
            continue
        if re.fullmatch(r"[0-9\W_]+", compact) or NON_TITLE.search(text):
            continue
        center = (left + right) / 2 / max(1.0, image_width)
        centeredness = max(0.0, 1.0 - abs(center - 0.5) * 2)
        height_ratio = max(0.0, bottom - top) / max(1.0, image_height)
        top_preference = max(0.0, 1.0 - top / max(1.0, image_height * 0.38))
        confidence = float(line.get("confidence") or 0.0)
        score = (
            centeredness * 2.0
            + min(0.65, height_ratio * 18)
            + top_preference * 0.45
            + confidence
            + (0.2 if TITLE_KEYWORDS.search(text) else 0.0)
        )
        candidates.append(
            {
                "title": text,
                "raw_text": raw,
                "ocr_confidence": round(confidence, 4),
                "spatial_score": round(score, 4),
                "centeredness": round(centeredness, 4),
                "box": [left, top, right, bottom],
            }
        )
    if not candidates:
        return {
            "title": None,
            "confidence": 0.0,
            "confidence_label": "unresolved",
            "context": contexts[:3],
            "needs_confirmation": True,
        }
    selected = max(candidates, key=lambda item: float(item["spatial_score"]))
    confidence = min(
        0.98,
        float(selected["ocr_confidence"]) * 0.75
        + float(selected["centeredness"]) * 0.2
        + min(1.0, (selected["box"][3] - selected["box"][1]) / max(1.0, image_height * 0.03))
        * 0.05,
    )
    label = "high" if confidence >= 0.9 else "medium" if confidence >= 0.72 else "low"
    return {
        **selected,
        "confidence": round(confidence, 4),
        "confidence_label": label,
        "context": [item for item in contexts if item != selected["title"]][:3],
        "needs_confirmation": label != "high",
    }


def select_tempo(lines: list[dict[str, Any]], image_height: float) -> dict[str, Any]:
    """Select an explicitly printed tempo marking without inventing a default BPM."""

    candidates: list[dict[str, Any]] = []
    for line in lines:
        text = re.sub(r"\s+", " ", str(line.get("text") or "")).strip()
        bounds = _bounds(line.get("box"))
        if not text or bounds is None or bounds[1] > image_height * 0.55:
            continue
        word_match = TEMPO_WORD.search(text)
        bpm_match = BPM_MARK.search(text)
        if word_match is None and bpm_match is None:
            continue
        unit_match = TEMPO_UNIT.search(text[:bpm_match.start()]) if bpm_match else None
        beat_unit = TEMPO_UNITS[unit_match.group("note")] if unit_match else None
        unit_dots = len(re.sub(r"\s", "", unit_match.group("dots"))) if unit_match else 0
        confidence = float(line.get("confidence") or 0.0)
        bpm = float(bpm_match.group("bpm").replace(",", ".")) if bpm_match else None
        candidates.append(
            {
                "tempo": bpm,
                "tempo_words": word_match.group(0).strip() if word_match else "",
                "tempo_beat_unit": beat_unit,
                "tempo_beat_unit_dots": unit_dots,
                "tempo_needs_confirmation": bpm is not None and beat_unit is None,
                "tempo_raw_text": text[:120],
                "tempo_confidence": round(confidence, 4),
                "tempo_confidence_label": (
                    "high" if confidence >= 0.9 else "medium" if confidence >= 0.72 else "low"
                ),
                "tempo_source": "paddleocr-local-tempo",
                "_rank": confidence
                + (0.35 if bpm is not None else 0.0)
                + (0.15 if word_match else 0.0),
            }
        )
    if not candidates:
        return {
            "tempo": None,
            "tempo_words": "",
            "tempo_beat_unit": None,
            "tempo_beat_unit_dots": 0,
            "tempo_needs_confirmation": False,
            "tempo_raw_text": "",
            "tempo_confidence": 0.0,
            "tempo_confidence_label": "unresolved",
            "tempo_source": "unresolved",
        }
    selected = max(candidates, key=lambda item: float(item["_rank"]))
    selected.pop("_rank", None)
    return selected


def recognize_score_title(image_path: Path) -> dict[str, Any]:
    if image_path.suffix.casefold() == ".pdf":
        return recognize_pdf_score_metadata(image_path)
    from PIL import Image

    from deepdesk.paddle_ocr import PaddleOCR

    # The packaged PP-OCR models must already exist. Never turn a metadata
    # fallback into a model download when an incomplete installation is used.
    spec = find_spec("rapidocr")
    if spec is None or not spec.origin:
        raise RuntimeError("The bundled offline score OCR models are unavailable")
    model_root = Path(spec.origin).parent / "models"
    for name, expected in OFFLINE_OCR_MODEL_SHA256.items():
        path = model_root / name
        if not path.is_file():
            raise RuntimeError("The bundled offline score OCR models are unavailable")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError("A bundled offline score OCR model failed integrity verification")
    with Image.open(image_path) as image:
        width, height = image.size
    payload = PaddleOCR()._recognize_sync(image_path.resolve())
    lines = list(payload.get("lines") or [])
    selected = select_title(lines, float(width), float(height))
    tempo = select_tempo(lines, float(height))
    return {
        "ok": True,
        "source": "paddleocr-local-title",
        "model": str(payload.get("model") or "Baidu PaddleOCR PP-OCR"),
        "offline": True,
        **selected,
        **tempo,
    }


def _pdf_text_lines(page: Any) -> tuple[list[dict[str, Any]], float, float]:
    """Read first-page text geometry; PDF actions, JavaScript and URLs are data only."""
    if page.rotation:
        from pypdf import PdfWriter

        # pypdf requires writer-owned pages when normalizing content streams.
        # Clone only the first page and never modify the uploaded PDF on disk.
        owner = PdfWriter()
        page = owner.add_page(page)
        page.transfer_rotation_to_content()
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    if not all(math.isfinite(value) and 0 < value <= 14400 for value in (width, height)):
        raise ValueError("Unsupported PDF page dimensions")
    fragments: list[dict[str, Any]] = []

    def visit(text: str, cm: list, tm: list, _font: Any, size: float) -> None:
        if len(fragments) >= 300:
            return
        # Music fonts often expose glyph names instead of words. They must
        # never become a title candidate or be interpreted as a command.
        text = re.sub(
            r"/(?:noteheads|clefs|rests|flags|scripts|accidentals|timesig|dots)\.[A-Za-z0-9_.]+",
            " ",
            text,
        )
        text = unicodedata.normalize("NFKC", text)
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        font_size = abs(float(size)) * max(math.hypot(cm[0], cm[1]), 0.01)
        if not all(math.isfinite(value) for value in (x, y, font_size)) or not 0 < font_size <= 300:
            return
        for offset, raw in enumerate(text.splitlines()[:10]):
            value = re.sub(r"\s+", " ", raw).strip()[:180]
            baseline = height - y + offset * font_size * 1.2
            if not value or not 0 <= baseline <= height * 0.6:
                continue
            fragments.append({"text": value, "x": x, "baseline": baseline, "size": font_size})

    page.extract_text(visitor_text=visit)
    groups: list[list[dict[str, Any]]] = []
    for fragment in sorted(fragments, key=lambda item: (item["baseline"], item["x"])):
        group = next(
            (
                items
                for items in groups
                if abs(items[0]["baseline"] - fragment["baseline"])
                <= max(2.0, min(items[0]["size"], fragment["size"]) * 0.3)
            ),
            None,
        )
        if group is None:
            groups.append([fragment])
        else:
            group.append(fragment)
    lines = []
    for group in groups:
        group.sort(key=lambda item: item["x"])
        left = min(item["x"] for item in group)
        top = min(item["baseline"] - item["size"] for item in group)
        right = min(
            width, max(item["x"] + len(item["text"]) * item["size"] * 0.55 for item in group)
        )
        bottom = max(item["baseline"] + item["size"] * 0.2 for item in group)
        lines.append(
            {
                "text": " ".join(item["text"] for item in group),
                "confidence": 0.99,
                "box": [[left, top], [right, top], [right, bottom], [left, bottom]],
            }
        )
    return lines, width, height


def recognize_pdf_score_metadata(pdf_path: Path) -> dict[str, Any]:
    """Local first-page extraction, with embedded scan OCR and no PDF execution.

    Reuse the shipped pypdf/Pillow/PP-OCR stack. No browser, converter download,
    embedded attachment, document action or PDF metadata title is executed or
    trusted. Text-only/vector outlines that cannot be read remain unresolved.
    """
    from PIL import Image
    from pypdf import PdfReader

    if not pdf_path.is_file() or not 0 < pdf_path.stat().st_size <= 50 * 1024 * 1024:
        raise ValueError("PDF metadata input exceeds the local size limit")
    reader = PdfReader(pdf_path, strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF metadata requires an unlocked local copy")
    if not reader.pages:
        raise ValueError("PDF has no readable page")
    page = reader.pages[0]
    page_rotation = int(page.rotation) % 360
    lines, width, height = _pdf_text_lines(page)
    selected = select_title(lines, width, height)
    tempo = select_tempo(lines, height)
    if tempo.get("tempo") is not None or tempo.get("tempo_words"):
        tempo["tempo_source"] = "pdf-local-text-tempo"
    result = {
        "ok": True,
        "source": "pdf-local-text-title",
        "model": "pypdf first-page text",
        "offline": True,
        **selected,
        **tempo,
    }
    if selected.get("title") and tempo.get("tempo") is not None:
        return result
    # Scanned scores normally have one full-page image. Decode at most eight
    # first-page images, reject decompression bombs, and OCR only the largest
    # page-shaped scan; small logos are not credible score-title evidence.
    candidate = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            for index, item in enumerate(page.images):
                if index >= 8:
                    break
                image = item.image
                if image is None:
                    continue
                iw, ih = image.size
                scan_width, scan_height = (ih, iw) if page_rotation in {90, 270} else (iw, ih)
                ratio = (scan_width / max(1, scan_height)) / (width / height)
                if iw * ih > 16_000_000 or min(iw, ih) < 400 or not 0.7 <= ratio <= 1.4:
                    continue
                if candidate is None or iw * ih > candidate.width * candidate.height:
                    if candidate is not None:
                        candidate.close()
                    candidate = (
                        image.rotate(-page_rotation, expand=True) if page_rotation else image.copy()
                    )
        if candidate is None:
            return result
        if candidate is not None:
            with tempfile.TemporaryDirectory(prefix="elren-score-metadata-") as temporary:
                image_path = Path(temporary) / "first-page-scan.png"
                candidate.convert("RGB").save(image_path)
                recognized = recognize_score_title(image_path)
    except Exception:
        # Preserve any explicitly extracted text when an embedded image is
        # broken, exceeds resource limits or local OCR is unavailable.
        return result
    finally:
        if candidate is not None:
            candidate.close()
    if candidate is not None:
        if not selected.get("title") and recognized.get("title"):
            for key in (
                "title",
                "confidence",
                "confidence_label",
                "context",
                "needs_confirmation",
                "raw_text",
                "box",
            ):
                if key in recognized:
                    result[key] = recognized[key]
            result["source"] = "pdf-local-scan-title"
        if result.get("tempo") is None and recognized.get("tempo") is not None:
            for key, value in recognized.items():
                if key.startswith("tempo"):
                    result[key] = value
        elif not result.get("tempo_words") and recognized.get("tempo_words"):
            result["tempo_words"] = recognized["tempo_words"]
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(json.dumps({"ok": False, "error": "one image path is required"}))
        return 2
    try:
        result = recognize_score_title(Path(arguments[0]))
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # ASCII-only JSON keeps Chinese metadata intact across Windows code pages
    # when this helper is invoked as a captured subprocess.
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
