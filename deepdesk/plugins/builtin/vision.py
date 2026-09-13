from __future__ import annotations

import asyncio
import mimetypes
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepdesk.macos_ocr import MacOSOCR
from deepdesk.models import Risk
from deepdesk.paddle_ocr import PaddleOCR
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.semantic_vision import VisionRequestError, request_semantic_vision
from deepdesk.vision_runtime import DEEPSEEK_VISION_MODEL, VisionEndpoint, VisionRuntime
from deepdesk.windows_ocr import WindowsOCR

SENSITIVE_SCREEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAQ\.[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(?:bearer|authorization)\s+[A-Za-z0-9._-]{12,}\b"),
)

TEXT_ONLY_HINTS = (
    "ocr", "text", "heading", "title", "read", "transcribe",
    "文字", "文本", "标题", "抄录", "读取", "识别文字",
)
MARKDOWN_HINTS = (
    "markdown", "document structure", "formula", "handwriting",
    "文档结构", "公式", "手写", "转成 markdown",
)
FIGURE_HINTS = ("table", "chart", "figure", "表格", "图表", "曲线图")
SEMANTIC_HINTS = (
    "layout", "overlap", "clipping", "contrast", "color", "appearance", "visual",
    "spacing", "responsive", "black screen", "brightness", "alignment", "position",
    "cropped", "obscured", "readable", "what does", "what is shown", "screen mean",
    "布局", "遮挡", "重叠", "裁切", "对比度", "颜色", "观感", "视觉", "间距",
    "响应式", "黑屏", "亮度", "对齐", "位置", "错位", "溢出", "是否清晰", "画面",
)


class VisionTool(ToolPlugin):
    name = "vision"
    description = (
        "OCR or understand a screenshot through a privacy-aware provider chain. Native local OCR "
        "(Windows OCR or Apple Vision) runs first with no quota; local PaddleOCR AI handles harder text; the configured cloud "
        "Google vision is followed by official DeepSeek primary/backup, then other configured "
        "fallbacks when recognition fails. When the current conversation uses DeepSeek vision, "
        "its official primary/backup route is preferred instead. Local credential screening and "
        "cloud-upload restrictions always apply. Use computer.screenshot first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Absolute screenshot path returned by computer",
            },
            "question": {
                "type": "string",
                "description": "What to inspect; ask for controls and coordinates",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "local_ocr", "markdown", "figure", "semantic"],
                "description": "Processing mode; auto chooses the cheapest sufficient provider",
            },
            "language_tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Optional installed native OCR language tags, e.g. zh-Hans-CN",
            },
            "cloud_fallback": {
                "type": "boolean",
                "description": "Allow image upload when local OCR is insufficient; default true",
            },
        },
        "required": ["image_path", "question"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        runtime: VisionRuntime,
        screenshot_dir: Path,
        windows_ocr: WindowsOCR | MacOSOCR | None = None,
        paddle_ocr: PaddleOCR | None = None,
        active_model: Callable[[], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.screenshot_dir = screenshot_dir.resolve()
        self.windows_ocr = windows_ocr or (MacOSOCR() if sys.platform == "darwin" else WindowsOCR())
        self.paddle_ocr = paddle_ocr or PaddleOCR()
        self.active_model = active_model
        # A single application can run several Agent tasks concurrently, while
        # free semantic-vision routes commonly allow only a very small request
        # burst.  Serialize only the cloud semantic stage: local OCR and all
        # other task work remain parallel.
        self._semantic_request_lock = asyncio.Lock()
        self._last_semantic_request_at = 0.0

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE

    @staticmethod
    def _select_mode(arguments: dict[str, Any]) -> str:
        requested = str(arguments.get("mode") or "auto")
        if requested != "auto":
            return requested
        question = str(arguments.get("question") or "").casefold()
        # Visual-quality questions frequently contain words such as "text" or
        # "readable". They still require pixel semantics; OCR alone cannot prove
        # layout, luminance, overlap, clipping, or responsive behavior.
        if any(token in question for token in SEMANTIC_HINTS):
            return "semantic"
        if any(token in question for token in FIGURE_HINTS):
            return "figure"
        if any(token in question for token in MARKDOWN_HINTS):
            return "markdown"
        if any(token in question for token in TEXT_ONLY_HINTS):
            return "local_ocr"
        return "semantic"

    @staticmethod
    def _contains_sensitive_text(text: str) -> bool:
        return any(pattern.search(text) for pattern in SENSITIVE_SCREEN_PATTERNS)

    @staticmethod
    def _has_usable_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        if len(compact) < 3:
            return False
        # OCR engines can occasionally return replacement characters for CJK
        # screenshots. Treat visibly corrupted output as unusable evidence.
        replacements = compact.count("\ufffd")
        return replacements < 3 or replacements / len(compact) < 0.015

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        from deepdesk.task_files import is_project_context, resolve_project_read

        project_context = is_project_context(context)
        image_path = (
            resolve_project_read(arguments["image_path"], context) if project_context
            else Path(arguments["image_path"]).resolve()
        )
        if not project_context and image_path != self.screenshot_dir and self.screenshot_dir not in image_path.parents:
            raise PermissionError("Vision can only read images created in the screenshot directory")
        if project_context:
            from PIL import Image

            if image_path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                raise PermissionError("Project vision accepts raster images only")
            # A model cannot route arbitrary repository text/binaries into a
            # cloud image request by naming them image_path.
            with Image.open(image_path) as image:
                image.verify()
        mode = self._select_mode(arguments)
        cloud_allowed = bool(arguments.get("cloud_fallback", True))
        # Task-local ContextVar in the app callback, not the mutable global
        # default. Capture it before any await so concurrent chats cannot leak
        # their preferred model into this invocation.
        preferred_model = str(self.active_model() if self.active_model else "")
        prefer_deepseek = cloud_allowed and preferred_model == DEEPSEEK_VISION_MODEL
        provider_chain = []
        fallback_reasons = []
        local: dict[str, Any] | None = None
        native_provider = getattr(self.windows_ocr, "provider_id", "windows-local-ocr")
        native_name = getattr(self.windows_ocr, "display_name", "Windows OCR")
        try:
            local = await self.windows_ocr.recognize(
                image_path,
                [str(item) for item in arguments.get("language_tags", [])],
            )
            provider_chain.append(f"{native_provider}:ok")
        except Exception as exc:
            provider_chain.append(f"{native_provider}:failed")
            fallback_reasons.append(f"{native_name} failed: {type(exc).__name__}: {exc}")

        local_text = str((local or {}).get("text") or "").strip()
        local_model = str((local or {}).get("model") or "Native local OCR")
        local_source = str((local or {}).get("source") or "native-local-offline")
        local_has_text = self._has_usable_text(local_text)
        mainland_china = context.egress_country == "CN"
        if mode == "local_ocr" and local_has_text and not mainland_china and not prefer_deepseek:
            return {
                "model": local_model,
                "source": local_source,
                "offline": True,
                "cloud_used": False,
                "provider_chain": provider_chain,
                "description": local_text,
                "ocr": local,
            }

        # PaddleOCR is local, quota-free AI OCR, so it is safe to use in every
        # region. It is the sole second-stage OCR engine after native OCR.
        paddle: dict[str, Any] | None = None
        paddle_text = ""
        if mainland_china or not local_has_text or mode in {"markdown", "figure", "semantic"}:
            try:
                paddle = await self.paddle_ocr.recognize(image_path)
                paddle_text = str(paddle.get("text") or "").strip()
                if self._has_usable_text(paddle_text):
                    provider_chain.append("paddleocr-local-ai:ok")
                else:
                    paddle_text = ""
                    provider_chain.append("paddleocr-local-ai:no-text")
                    fallback_reasons.append("PaddleOCR returned no usable text")
            except Exception as exc:
                provider_chain.append("paddleocr-local-ai:failed")
                fallback_reasons.append(f"PaddleOCR failed: {type(exc).__name__}: {exc}")

        best_local_text = paddle_text or (local_text if local_has_text else "")
        best_local_ocr = paddle or local
        if mode in {"local_ocr", "markdown"} and best_local_text and not prefer_deepseek:
            return {
                "model": (paddle or {}).get("model", local_model),
                "source": (paddle or {}).get("source", local_source),
                "offline": True,
                "cloud_used": False,
                "egress_route": "mainland-china" if mainland_china else "local",
                "provider_chain": provider_chain,
                "fallback_reason": "; ".join(fallback_reasons),
                "description": best_local_text,
                "ocr": best_local_ocr,
            }

        # Local OCR still wins when sufficient. If it failed, an allowed cloud
        # fallback (including official DeepSeek) must work in every region.
        if mainland_china and not cloud_allowed:
            return {
                "model": (paddle or {}).get("model", local_model),
                "source": (paddle or {}).get("source", local_source),
                "offline": True,
                "cloud_used": False,
                "egress_route": "mainland-china",
                "provider_chain": provider_chain,
                "fallback_reason": "; ".join(fallback_reasons),
                "description": best_local_text,
                "ocr": best_local_ocr,
                "semantic_limited": mode in {"semantic", "figure"},
                "semantic_verified": False,
            }

        if not cloud_allowed:
            return {
                "model": local_model,
                "source": local_source,
                "offline": True,
                "cloud_used": False,
                "provider_chain": provider_chain,
                "fallback_reason": "Cloud fallback disabled",
                "description": best_local_text,
                "ocr": best_local_ocr,
                "semantic_verified": False,
            }

        if self._contains_sensitive_text(local_text + "\n" + paddle_text):
            return {
                "model": local_model,
                "source": local_source,
                "offline": True,
                "cloud_used": False,
                "cloud_blocked": True,
                "provider_chain": provider_chain,
                "fallback_reason": "Potential credential detected by local OCR; image upload blocked",
                "description": best_local_text,
                "ocr": best_local_ocr,
                "semantic_verified": False,
            }

        async def endpoints():
            # Discover other routes lazily: a selected DeepSeek vision chat
            # should not wait for Google's catalog before its first OCR call.
            if prefer_deepseek:
                direct = getattr(self.runtime, "deepseek_endpoints", None)
                if callable(direct):
                    for endpoint in direct():
                        yield endpoint
            try:
                yield await self.runtime.ensure_ready()
            except Exception as exc:
                # Discovery errors may contain server text; keep only their class.
                fallback_reasons.append(f"Semantic vision unavailable: {type(exc).__name__}")
            fallback_provider = getattr(self.runtime, "semantic_fallback_endpoints", None)
            if callable(fallback_provider):
                for endpoint in fallback_provider():
                    yield endpoint
        raw = image_path.read_bytes()
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        prompt = (
            "Analyze this image for the user's request. Transcribe relevant text faithfully, "
            "preserving numbers, punctuation and line structure. For screenshots include relevant "
            "controls, states, layout and approximate center coordinates when requested. "
            "Clearly mark uncertainty; never invent missing text. If nothing can be recognized "
            "respond exactly NO_READABLE_TEXT. " + arguments["question"]
        )
        if best_local_text:
            prompt += "\nLocal OCR evidence (may contain recognition errors):\n" + best_local_text[:12_000]
        attempts = []
        seen = set()
        cloud_attempted = False
        async with self._semantic_request_lock:
            loop = asyncio.get_running_loop()
            since_last = loop.time() - self._last_semantic_request_at
            if since_last < 1.25:
                await asyncio.sleep(1.25 - since_last)
            self._last_semantic_request_at = loop.time()
            async for endpoint in endpoints():
                semantic_is_local = str(endpoint.base_url).startswith("http://127.0.0.1:")
                endpoint_key = str(getattr(endpoint, "api_key", "") or "")
                if not endpoint_key and not semantic_is_local:
                    endpoint_key = str(getattr(self.runtime, "api_key", "") or "")
                identity = (endpoint.base_url, endpoint.model, endpoint_key)
                if identity in seen:
                    continue
                seen.add(identity)
                endpoint = VisionEndpoint(
                    base_url=str(endpoint.base_url), model=str(endpoint.model),
                    source=str(endpoint.source),
                    protocol=str(getattr(endpoint, "protocol", "openai") or "openai"),
                    api_key=endpoint_key,
                )
                semantic_provider = (
                    "local-visual-model" if semantic_is_local else endpoint.source
                    if endpoint.protocol == "gemini" or endpoint.source.startswith("deepseek-official-")
                    else "google-vision-fallback"
                )
                cloud_attempted = cloud_attempted or not semantic_is_local
                try:
                    description, route_attempts = await request_semantic_vision(endpoint, raw, mime, prompt)
                except VisionRequestError as exc:
                    attempts.extend(exc.attempts)
                    provider_chain.append(f"{semantic_provider}:failed")
                    fallback_reasons.append(f"{semantic_provider}: {exc}")
                    continue
                attempts.extend(route_attempts)
                return {
                    "model": endpoint.model, "source": endpoint.source,
                    "description": description, "attempts": attempts,
                    "retried": len(attempts) > 1, "offline": not cloud_attempted,
                    "cloud_used": cloud_attempted, "semantic_verified": True,
                    "egress_route": "local" if semantic_is_local else (
                        "mainland-china-semantic-cloud" if mainland_china else "cloud"
                    ),
                    "provider_chain": [*provider_chain, f"{semantic_provider}:ok"],
                    "fallback_reason": "; ".join(fallback_reasons), "ocr": best_local_ocr,
                }
        if best_local_text:
            return {
                "model": (paddle or {}).get("model", local_model),
                "source": (paddle or {}).get("source", local_source),
                "offline": not cloud_attempted, "cloud_used": cloud_attempted,
                "semantic_verified": False, "semantic_limited": True,
                "egress_route": "mainland-china" if mainland_china else "local",
                "attempts": attempts, "retried": len(attempts) > 1,
                "provider_chain": provider_chain,
                "fallback_reason": "; ".join(fallback_reasons),
                "description": best_local_text, "ocr": best_local_ocr,
            }
        raise RuntimeError("All vision recognition methods failed: " + "; ".join(fallback_reasons))
