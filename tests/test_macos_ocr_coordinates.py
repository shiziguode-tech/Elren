import sys
from types import SimpleNamespace

import pytest

from deepdesk import macos_ocr
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin import vision


@pytest.mark.parametrize("rect,expected", [
    ([0.1, 0.7, 0.2, 0.1], [100, 100, 200, 50]),
    ([0, 0, 1, 1], [0, 0, 1000, 500]),
    ([-0.1, -0.1, 0.3, 0.3], [0, 400, 200, 100]),
    ([0.9, 0.9, 0.3, 0.3], [900, 0, 100, 50]),
])
def test_native_bottom_left_coordinates_become_bounded_top_left_pixels(rect, expected):
    assert macos_ocr.normalized_rect_to_image_box(rect, 1000, 500) == expected


@pytest.mark.parametrize("rect,width", [([0, 0, -1, 1], 100), ([float("nan"), 0, 1, 1], 100),
                                       ([0, 0, 1], 100), ([0, 0, 1, 1], 0)])
def test_invalid_ocr_geometry_is_not_exported_as_click_coordinates(rect, width):
    with pytest.raises(ValueError):
        macos_ocr.normalized_rect_to_image_box(rect, width, 500)


def test_native_result_preserves_raw_coordinates_and_labels_both_origins(tmp_path, monkeypatch):
    candidate = SimpleNamespace(string=lambda: "QA text", confidence=lambda: 0.99)
    box = SimpleNamespace(origin=SimpleNamespace(x=0.1, y=0.7), size=SimpleNamespace(width=0.2, height=0.1))
    observation = SimpleNamespace(topCandidates_=lambda _: [candidate], boundingBox=lambda: box)
    request = SimpleNamespace(setRecognitionLevel_=lambda _: None, setUsesLanguageCorrection_=lambda _: None,
                              results=lambda: [observation])
    handler = SimpleNamespace(performRequests_error_=lambda *_: (True, None))
    quartz = SimpleNamespace(CFURLCreateFromFileSystemRepresentation=lambda *_: "url",
                             CGImageSourceCreateWithURL=lambda *_: "source",
                             CGImageSourceCreateImageAtIndex=lambda *_: "image",
                             CGImageGetWidth=lambda _: 1000, CGImageGetHeight=lambda _: 500)
    framework = SimpleNamespace(
        VNRequestTextRecognitionLevelAccurate=1,
        VNRecognizeTextRequest=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: request)),
        VNImageRequestHandler=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithCGImage_options_=lambda *_: handler)),
    )
    monkeypatch.setattr(macos_ocr, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "Vision", framework)
    result = macos_ocr.MacOSOCR._recognize_sync(tmp_path / "第三首.png", [])
    assert result["image"] == {"width": 1000, "height": 500}
    assert result["lines"][0]["box"] == [100, 100, 200, 50]
    assert result["lines"][0]["normalized_rect"] == [0.1, 0.7, 0.2, 0.1]
    assert result["coordinate_system"]["normalized_rect"] == "image-normalized-bottom-left"
    assert result["coordinate_system"]["box"] == "image-pixels-top-left"


def test_mac_vision_defaults_to_native_apple_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "sys", SimpleNamespace(platform="darwin"))
    tool = vision.VisionTool(SimpleNamespace(), tmp_path)
    assert isinstance(tool.windows_ocr, macos_ocr.MacOSOCR)


@pytest.mark.asyncio
async def test_mac_ocr_provenance_is_not_labeled_as_windows(tmp_path):
    class Native:
        provider_id = "macos-local-ocr"
        display_name = "Apple Vision OCR"

        async def recognize(self, *_):
            return {"text": "Native QA", "model": "Apple Vision", "source": "macos-vision-offline"}

    image = tmp_path / "qa.png"
    image.write_bytes(b"mocked OCR input")
    tool = vision.VisionTool(SimpleNamespace(), tmp_path, windows_ocr=Native())
    result = await tool.execute({"image_path": str(image), "question": "read text", "mode": "local_ocr",
                                 "cloud_fallback": False},
                                ToolContext(task_id="qa-native", workspace=str(tmp_path), egress_country="US"))
    assert result["provider_chain"] == ["macos-local-ocr:ok"]
    assert result["cloud_used"] is False


@pytest.mark.asyncio
async def test_mac_ocr_failure_retains_correct_provider_and_offline_fallback(tmp_path):
    class Native:
        provider_id = "macos-local-ocr"
        display_name = "Apple Vision OCR"

        async def recognize(self, *_):
            raise RuntimeError("fixture native unavailable")

    class Paddle:
        async def recognize(self, *_):
            return {"text": "Offline fallback text", "source": "paddle-local", "model": "PaddleOCR"}

    image = tmp_path / "qa.png"
    image.write_bytes(b"mocked OCR input")
    tool = vision.VisionTool(SimpleNamespace(), tmp_path, windows_ocr=Native(), paddle_ocr=Paddle())
    result = await tool.execute({"image_path": str(image), "question": "read text", "mode": "local_ocr",
                                 "cloud_fallback": False},
                                ToolContext(task_id="qa-native-fallback", workspace=str(tmp_path), egress_country="US"))
    assert result["provider_chain"] == ["macos-local-ocr:failed", "paddleocr-local-ai:ok"]
    assert "Apple Vision OCR failed" in result["fallback_reason"]
    assert "Windows OCR" not in result["fallback_reason"]
    assert result["description"] == "Offline fallback text"
    assert result["cloud_used"] is False
