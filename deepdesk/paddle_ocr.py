from __future__ import annotations

import asyncio
from importlib.util import find_spec
from pathlib import Path
from typing import Any


class PaddleOCR:
    """Lazy, CPU-only PP-OCR inference for mainland-China routing.

    RapidOCR packages Baidu PaddleOCR neural-network weights in ONNX form. The
    engine is loaded only on first use and guarded because task execution can be
    concurrent while the inference session is intentionally shared.
    """

    def __init__(self) -> None:
        self._engine: Any | None = None
        self._lock = asyncio.Lock()

    def _recognize_sync(self, image_path: Path) -> dict[str, Any]:
        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR(
                params={
                    "EngineConfig.onnxruntime.use_cuda": False,
                    "EngineConfig.onnxruntime.use_dml": False,
                }
            )
        result = self._engine(str(image_path))
        # RapidOCR returns NumPy arrays on some versions. NumPy deliberately
        # rejects implicit truth-value checks (``array or ()``), so normalize
        # by identity instead of boolean coercion.
        raw_texts = () if result.txts is None else result.txts
        raw_scores = () if result.scores is None else result.scores
        raw_boxes = () if result.boxes is None else result.boxes
        texts = [str(item) for item in raw_texts]
        scores = [float(item) for item in raw_scores]
        boxes = [
            [[float(value) for value in point] for point in polygon]
            for polygon in raw_boxes
        ]
        return {
            "ready": True,
            "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
            "source": "paddleocr-local-ai",
            "offline": True,
            "text": "\n".join(texts),
            "lines": [
                {
                    "text": text,
                    "confidence": scores[index] if index < len(scores) else None,
                    "box": boxes[index] if index < len(boxes) else None,
                }
                for index, text in enumerate(texts)
            ],
        }

    async def recognize(self, image_path: Path) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._recognize_sync, image_path.resolve())

    async def status(self, probe: bool = False) -> dict[str, Any]:
        if not probe:
            ready = find_spec("onnxruntime") is not None and find_spec("rapidocr") is not None
            return {
                "ready": ready,
                "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
                "source": "local-ai",
                "loaded": self._engine is not None,
                "gpu_required": False,
                "probed": False,
            }
        try:
            import onnxruntime  # noqa: F401
            import rapidocr  # noqa: F401

            return {
                "ready": True,
                "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
                "source": "local-ai",
                "loaded": self._engine is not None,
                "gpu_required": False,
                "probed": True,
            }
        except Exception as exc:
            return {
                "ready": False,
                "model": "Baidu PaddleOCR PP-OCR (ONNX CPU)",
                "source": "local-ai",
                "loaded": False,
                "gpu_required": False,
                "probed": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
