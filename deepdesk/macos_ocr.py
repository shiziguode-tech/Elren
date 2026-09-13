from __future__ import annotations

import asyncio
import math
import sys
import time
from pathlib import Path
from typing import Any


def normalized_rect_to_image_box(rect: list[float], width: int, height: int) -> list[float]:
    """Apple Vision bottom-left normalized XYWH -> top-left image-pixel XYWH."""
    if len(rect) != 4 or width <= 0 or height <= 0 or not all(math.isfinite(v) for v in rect):
        raise ValueError("Invalid OCR image dimensions or bounding box")
    x, y, w, h = rect
    if w < 0 or h < 0:
        raise ValueError("Negative OCR bounding box size")
    left, right = max(0.0, min(1.0, x)), max(0.0, min(1.0, x + w))
    top, bottom = max(0.0, min(1.0, 1 - y - h)), max(0.0, min(1.0, 1 - y))
    return [round(left * width, 2), round(top * height, 2),
            round((right - left) * width, 2), round((bottom - top) * height, 2)]


class MacOSOCR:
    """Offline OCR backed by Apple's Vision framework on macOS 13+."""

    provider_id = "macos-local-ocr"
    display_name = "Apple Vision OCR"

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    async def status(self, probe: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if self._status_cache and (not probe or now - self._status_cache[0] < 60):
            return self._status_cache[1]
        ready = False
        error = ""
        if sys.platform == "darwin":
            try:
                import Quartz  # noqa: F401
                import Vision  # noqa: F401

                ready = True
            except ImportError as exc:
                error = f"{type(exc).__name__}: {exc}"
        else:
            error = "Apple Vision OCR is only available on macOS"
        result = {
            "ready": ready,
            "source": "Apple Vision VNRecognizeTextRequest",
            "offline": True,
            "available_languages": [],
            "probed": probe,
            "error": error,
        }
        self._status_cache = (now, result)
        return result

    async def recognize(
        self, image_path: Path, language_tags: list[str] | None = None
    ) -> dict[str, Any]:
        return await asyncio.wait_for(
            asyncio.to_thread(self._recognize_sync, image_path, language_tags or []),
            timeout=self.timeout,
        )

    @staticmethod
    def _recognize_sync(image_path: Path, language_tags: list[str]) -> dict[str, Any]:
        if sys.platform != "darwin":
            raise RuntimeError("Apple Vision OCR is only available on macOS")
        import Quartz
        import Vision

        image_url = Quartz.CFURLCreateFromFileSystemRepresentation(
            None, str(image_path).encode("utf-8"), len(str(image_path).encode("utf-8")), False
        )
        source = Quartz.CGImageSourceCreateWithURL(image_url, None)
        if source is None:
            raise ValueError(f"Unable to open image: {image_path}")
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            raise ValueError(f"Unable to decode image: {image_path}")
        width, height = int(Quartz.CGImageGetWidth(image)), int(Quartz.CGImageGetHeight(image))

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        if language_tags:
            request.setRecognitionLanguages_(language_tags[:8])
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(str(error or "Apple Vision OCR failed"))

        lines: list[dict[str, Any]] = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = str(candidates[0].string())
            box = observation.boundingBox()
            normalized = [float(box.origin.x), float(box.origin.y), float(box.size.width), float(box.size.height)]
            lines.append(
                {
                    "text": text,
                    "confidence": float(candidates[0].confidence()),
                    "normalized_rect": normalized,
                    "box": normalized_rect_to_image_box(normalized, width, height),
                }
            )
        return {
            "ok": True,
            "model": "Apple Vision",
            "source": "macos-vision-offline",
            "offline": True,
            "image": {"width": width, "height": height},
            "coordinate_system": {
                "box": "image-pixels-top-left",
                "normalized_rect": "image-normalized-bottom-left",
                "format": "x,y,width,height",
            },
            "coordinate_note": "Image pixels are not desktop points. Map through the current screenshot/display dimensions before clicking.",
            "text": "\n".join(item["text"] for item in lines)[:20_000],
            "lines": lines[:500],
        }
