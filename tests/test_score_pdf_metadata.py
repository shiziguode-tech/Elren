from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from deepdesk import score_title_ocr as metadata
from deepdesk.plugins.builtin import jianpu_omr
from deepdesk.plugins.builtin.jianpu_omr import _companion_title_image, _identify_source_score


def make_pdf(
    path: Path, *, title: str = "Offline QA", tempo: bool = True, scan: bool = False
) -> Path:
    document = canvas.Canvas(str(path), pagesize=(400, 600))
    document.setTitle("Invisible PDF metadata must not become a score title")
    if scan:
        with Image.new("RGB", (800, 1200), "white") as image:
            document.drawImage(ImageReader(image), 0, 0, width=400, height=600)
    if title:
        document.setFont("Helvetica-Bold", 22)
        document.drawCentredString(200, 540, title)
    if tempo:
        document.setFont("Helvetica", 13)
        document.drawString(35, 470, "Moderato (")
        document.drawString(105, 470, "= 96)")
    document.showPage()
    document.save()
    return path


def test_pdf_visible_text_fills_title_and_split_tempo_without_ocr(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "score.pdf")
    monkeypatch.setattr(
        metadata,
        "recognize_score_title",
        lambda _path: pytest.fail("digital PDF should not need OCR"),
    )
    result = metadata.recognize_pdf_score_metadata(path)
    assert result["title"] == "Offline QA"
    assert result["tempo"] == 96
    assert result["tempo_words"] == "Moderato"
    assert result["source"] == "pdf-local-text-title"
    assert result["tempo_source"] == "pdf-local-text-tempo"
    assert result["offline"] is True


def test_pdf_companion_and_real_metadata_subprocess(tmp_path):
    path = make_pdf(tmp_path / "score.pdf")
    assert _companion_title_image(path) == path
    score = {"title": "未命名乐谱", "tempo": None}
    result = _identify_source_score(path, score)
    assert result["title"] == score["title"] == "Offline QA"
    # The fixture prints "Moderato (= 96)" without a note glyph. Keep the
    # observed number as evidence, not an invented quarter-note tempo.
    assert score["tempo"].get("tempo") is None
    assert score["tempo"]["unresolved_metronome"]["value"] == 96
    assert score["tempo"]["words"] == "Moderato"


def test_frozen_backend_uses_metadata_mode_instead_of_starting_server(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "score.pdf")
    monkeypatch.setattr(jianpu_omr.sys, "frozen", True, raising=False)
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(stdout='{"ok": true, "title": "Offline QA"}')

    monkeypatch.setattr(jianpu_omr.subprocess, "run", run)
    score = {"title": "未命名乐谱", "tempo": None}
    assert _identify_source_score(path, score)["title"] == "Offline QA"
    assert observed[0][0] == [jianpu_omr.sys.executable, "--score-metadata", str(path)]
    assert observed[0][1]["timeout"] == 75
    assert observed[0][1].get("shell", False) is False


def test_pdf_missing_printed_metadata_ignores_document_title_and_actions(tmp_path):
    source = make_pdf(tmp_path / "blank.pdf", title="", tempo=False)
    target = tmp_path / "actions.pdf"
    writer = PdfWriter()
    writer.append(PdfReader(source))
    writer.add_metadata({"/Title": "Unprinted title"})
    writer.add_js('app.launchURL("https://invalid.example/never-open", true);')
    with target.open("wb") as output:
        writer.write(output)
    result = metadata.recognize_pdf_score_metadata(target)
    assert result["title"] is None
    assert result["tempo"] is None
    assert result["tempo_words"] == ""
    assert result["needs_confirmation"] is True


def test_pdf_reads_only_first_page_and_rejects_encryption(tmp_path):
    first = make_pdf(tmp_path / "first.pdf", title="", tempo=False)
    second = make_pdf(tmp_path / "second.pdf", title="Second page is not the heading")
    writer = PdfWriter()
    writer.append(first)
    writer.append(second)
    combined = tmp_path / "combined.pdf"
    with combined.open("wb") as output:
        writer.write(output)
    assert metadata.recognize_pdf_score_metadata(combined)["title"] is None
    writer.encrypt("not-a-real-secret")
    encrypted = tmp_path / "encrypted.pdf"
    with encrypted.open("wb") as output:
        writer.write(output)
    with pytest.raises(ValueError, match="Encrypted PDF"):
        metadata.recognize_pdf_score_metadata(encrypted)


def test_scanned_pdf_uses_only_local_temporary_embedded_image(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "scan.pdf", title="", tempo=False, scan=True)
    observed = []

    def recognize(path):
        assert path.suffix == ".png" and path.is_file()
        observed.append(path)
        return {
            "title": "第三首",
            "confidence": 0.95,
            "confidence_label": "high",
            "needs_confirmation": False,
            "tempo": 120,
            "tempo_words": "Allegro",
            "tempo_beat_unit": "quarter",
            "tempo_source": "paddleocr-local-tempo",
        }

    monkeypatch.setattr(metadata, "recognize_score_title", recognize)
    result = metadata.recognize_pdf_score_metadata(source)
    assert result["title"] == "第三首" and result["tempo"] == 120
    assert result["source"] == "pdf-local-scan-title"
    assert observed and not observed[0].exists()


def test_pdf_keeps_visible_title_if_optional_scan_ocr_fails(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "partial.pdf", tempo=False, scan=True)

    def unavailable(_path):
        raise RuntimeError("local OCR unavailable")

    monkeypatch.setattr(metadata, "recognize_score_title", unavailable)
    result = metadata.recognize_pdf_score_metadata(source)
    assert result["title"] == "Offline QA"
    assert result["tempo"] is None


def test_score_ocr_does_not_download_missing_local_models(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "find_spec", lambda _name: None)
    with pytest.raises(RuntimeError, match="bundled offline score OCR models"):
        metadata.recognize_score_title(tmp_path / "unused.png")


def test_score_ocr_rejects_damaged_models_before_engine_can_redownload(tmp_path, monkeypatch):
    package = tmp_path / "rapidocr"
    models = package / "models"
    models.mkdir(parents=True)
    for name in metadata.OFFLINE_OCR_MODEL_SHA256:
        (models / name).write_bytes(b"damaged-model-fixture")
    monkeypatch.setattr(
        metadata, "find_spec", lambda _name: SimpleNamespace(origin=str(package / "__init__.py"))
    )
    from deepdesk import paddle_ocr

    monkeypatch.setattr(
        paddle_ocr,
        "PaddleOCR",
        lambda: pytest.fail("corrupt models must not initialize the engine"),
    )
    with pytest.raises(RuntimeError, match="integrity verification"):
        metadata.recognize_score_title(tmp_path / "unused.png")


def test_title_normalizes_pdf_ligatures_without_inventing_text():
    assert metadata.clean_title("Oﬄine QA") == "Offline QA"


def test_pdf_title_preserves_ordinary_slash_words(tmp_path):
    path = make_pdf(tmp_path / "slash-title.pdf", title="AC/DC Prelude")
    assert metadata.recognize_pdf_score_metadata(path)["title"] == "AC/DC Prelude"


def test_rotated_scanned_pdf_normalizes_the_ocr_image(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "scan.pdf", title="", tempo=False, scan=True)
    writer = PdfWriter()
    writer.add_page(PdfReader(source).pages[0].rotate(90))
    rotated = tmp_path / "rotated.pdf"
    with rotated.open("wb") as output:
        writer.write(output)

    def recognize(path):
        with Image.open(path) as image:
            assert image.size == (1200, 800)
        return {"title": "Rotated study", "tempo": None}

    monkeypatch.setattr(metadata, "recognize_score_title", recognize)
    assert metadata.recognize_pdf_score_metadata(rotated)["title"] == "Rotated study"


def test_chinese_readme_matches_private_history_packaging():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "但仍不打包聊天历史和日志" not in readme
    assert "聊天数据库没有被密钥保险库加密" in readme
    assert "请勿分享个人私有资料包或其解压目录" in readme
