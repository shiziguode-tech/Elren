from __future__ import annotations

import asyncio
import inspect
import mimetypes
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

_SECRET_PATTERN = re.compile(
    r"(?:sk|pk)[-_][A-Za-z0-9_-]{16,}|(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S{8,}",
    re.IGNORECASE,
)


class FreeMediaGenerator:
    """Media generation locked to proven free endpoints/models.

    The public image gateway is keyless. Catalog-backed video and music models
    must declare zero pricing and must not be marked ``paid_only``. Catalog checks
    are repeated periodically because provider availability and pricing can
    change. Official Hugging Face ZeroGPU Spaces and anonymous ModelScope
    Studios provide independent shared-compute pools without a paid API key.
    When one free pool is busy or exhausted, automatic selection tries the next
    verified-free provider before reporting failure. Public shared compute is
    still quota-limited; paid APIs are never used as a hidden fallback.
    The Pollinations API key is sent only in an Authorization header and is never
    placed in a generated URL, task event, or result.
    """

    base_url = "https://gen.pollinations.ai"
    legacy_image_url = "https://image.pollinations.ai"
    _music_names = {
        "elevenmusic",
        "lyria-3-clip",
        "stable-audio-3-medium",
        "stable-audio-3-large",
    }
    _space_models: dict[str, tuple[dict[str, Any], ...]] = {
        "video": (
            {
                "name": "ltx-video-distilled-zerogpu",
                "title": "LTX-Video Distilled (official ZeroGPU Space)",
                "category": "video",
                "output_modalities": ["video"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "Lightricks/ltx-video-distilled",
                "api_name": "/text_to_video",
                "public_shared": True,
            },
            {
                "name": "ltx-video-2.3-zerogpu",
                "title": "LTX 2.3 Distilled (official ZeroGPU Space)",
                "category": "video",
                "output_modalities": ["video"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "Lightricks/LTX-2-3",
                "api_name": "/generate_video",
                "public_shared": True,
            },
            {
                "name": "wan-2.1-public-demo",
                "title": "Wan 2.1 text-to-video (official public demo)",
                "category": "video",
                "output_modalities": ["video"],
                "pricing": {"currency": "shared_public_demo", "request": 0},
                "provider": "Wan public demo",
                "space_id": "Wan-AI/Wan2.1",
                "api_name": "/t2v_generation_async",
                "public_shared": True,
                "public_demo": True,
            },
        ),
        "music": (
            {
                "name": "stable-audio-3-zerogpu",
                "title": "Stable Audio 3 (official ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "stabilityai/stable-audio-3",
                "api_name": "/infer",
                "public_shared": True,
            },
            {
                "name": "ace-step-zerogpu",
                "title": "ACE-Step (official ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "ACE-Step/ACE-Step",
                "api_name": "/__call__",
                "public_shared": True,
            },
            {
                "name": "musicgen-zerogpu",
                "title": "MusicGen (official Meta ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "facebook/MusicGen",
                "api_name": "/predict_batched",
                "public_shared": True,
            },
            {
                "name": "stable-audio-open-zerogpu",
                "title": "Stable Audio Open Zero (public ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "artificialguybr/Stable-Audio-Open-Zero",
                "api_name": "/predict",
                "public_shared": True,
            },
            {
                "name": "diffrhythm-zerogpu",
                "title": "DiffRhythm full-song generator (official ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "ASLP-lab/DiffRhythm",
                "api_name": "/infer_music",
                "public_shared": True,
            },
            {
                "name": "heartmula-zerogpu",
                "title": "HeartMuLa song generator (public ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "projectlosangeles/HeartMuLa",
                "api_name": "/generate_music",
                "public_shared": True,
            },
            {
                "name": "ezaudio-zerogpu",
                "title": "EzAudio text-to-audio (official ZeroGPU Space)",
                "category": "audio",
                "output_modalities": ["audio"],
                "pricing": {"currency": "shared_zerogpu", "request": 0},
                "provider": "Hugging Face ZeroGPU",
                "space_id": "OpenSound/EzAudio",
                "api_name": "/generate_audio",
                "public_shared": True,
            },
        ),
    }
    _mainland_china_models: dict[str, tuple[dict[str, Any], ...]] = {
        "image": (
            {
                "name": "easyanimate-image-modelscope",
                "title": "EasyAnimate image generation (Alibaba PAI ModelScope Studio)",
                "provider": "ModelScope shared Studio",
                "studio_id": "PAI/EasyAnimate",
                "studio_url": "https://pai-easyanimate.ms.show",
                "pricing": {"currency": "shared_modelscope", "request": 0},
            },
        ),
        "video": (
            {
                "name": "easyanimate-video-modelscope",
                "title": "EasyAnimate text-to-video (Alibaba PAI ModelScope Studio)",
                "provider": "ModelScope shared Studio",
                "studio_id": "PAI/EasyAnimate",
                "studio_url": "https://pai-easyanimate.ms.show",
                "pricing": {"currency": "shared_modelscope", "request": 0},
            },
        ),
        "music": (
            {
                "name": "inspiremusic-modelscope",
                "title": "InspireMusic (Tongyi official ModelScope Studio)",
                "provider": "ModelScope shared Studio",
                "studio_id": "iic/InspireMusic",
                "studio_url": "https://iic-inspiremusic.ms.show",
                "pricing": {"currency": "shared_modelscope", "request": 0},
            },
        ),
    }

    def __init__(
        self, api_key: str, output_dir: Path, *, huggingface_token: str = ""
    ) -> None:
        self.api_key = api_key.strip()
        self.huggingface_token = huggingface_token.strip()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._space_status_cache: dict[str, tuple[float, bool, str]] = {}
        self._studio_endpoint_cache: dict[str, tuple[float, bool, str]] = {}
        self._provider_cooldowns: dict[str, tuple[float, str]] = {}

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._catalog_cache.clear()

    def set_huggingface_token(self, token: str) -> None:
        self.huggingface_token = str(token or "").strip()
        self._space_status_cache.clear()
        self._provider_cooldowns.pop("huggingface-zerogpu", None)

    @classmethod
    def _official_space_models(cls, kind: str) -> list[dict[str, Any]]:
        return [dict(item) for item in cls._space_models.get(kind, ())]

    @classmethod
    def _china_models(cls, kind: str) -> list[dict[str, Any]]:
        return [dict(item) for item in cls._mainland_china_models.get(kind, ())]

    async def _modelscope_studio_status(self, studio_id: str) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True, trust_env=True) as client:
                response = await client.get(f"https://modelscope.cn/api/v1/studio/{studio_id}")
            response.raise_for_status()
            payload = response.json()
            data = payload.get("Data") or payload.get("data") or payload
            running = str(data.get("Status") or "").casefold() == "running"
            anonymous = not bool(data.get("NeedLogin"))
            if running and anonymous:
                return True, ""
            return False, "ModelScope Studio is offline or requires login"
        except Exception as exc:
            return False, f"ModelScope Studio check failed: {type(exc).__name__}: {exc}"

    async def _modelscope_model_status(
        self, model: dict[str, Any], *, refresh: bool = False
    ) -> tuple[bool, str]:
        """Check both ModelScope metadata and the actual Gradio endpoint.

        A Studio can report Running on modelscope.cn while its independent
        ``*.ms.show`` generation domain is blocked by the current proxy, TLS
        path, or regional network. Reporting only metadata created a false
        positive in the capability map.
        """
        studio_id = str(model.get("studio_id") or "")
        studio_url = str(model.get("studio_url") or "").rstrip("/")
        online, reason = await self._modelscope_studio_status(studio_id)
        if not online or not studio_url:
            return online, reason or "ModelScope Studio generation URL is missing"
        cached = self._studio_endpoint_cache.get(studio_url)
        if cached and not refresh and time.monotonic() - cached[0] < 60:
            return cached[1], cached[2]
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=True, trust_env=True
            ) as client:
                response = await client.get(f"{studio_url}/config")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not payload:
                raise RuntimeError("invalid Gradio configuration")
            available, detail = True, ""
        except Exception as exc:
            available = False
            detail = (
                "Studio reports Running, but its generation endpoint is unreachable "
                f"from the current network: {type(exc).__name__}"
            )
        self._studio_endpoint_cache[studio_url] = (
            time.monotonic(), available, detail
        )
        return available, detail

    @staticmethod
    def _is_free_tier_model(model: dict[str, Any], kind: str) -> bool:
        if model.get("paid_only") is True or model.get("disabled") is True:
            return False
        pricing = model.get("pricing")
        if not isinstance(pricing, dict):
            return False
        prices: list[float] = []
        for key, value in pricing.items():
            if key == "currency":
                continue
            try:
                prices.append(float(value))
            except (TypeError, ValueError):
                return False
        # Strict free means a declared zero price. A model that merely omits
        # paid_only can still consume free or purchased Pollen and is rejected.
        if not prices or any(price != 0 for price in prices):
            return False
        outputs = {str(value).lower() for value in model.get("output_modalities") or []}
        name = str(model.get("name") or "").lower()
        text = " ".join(
            str(model.get(field) or "").lower()
            for field in ("name", "title", "description")
        )
        if kind == "image":
            return "image" in outputs and str(model.get("category") or "image") == "image"
        if kind == "video":
            return "video" in outputs
        if kind == "music":
            return "audio" in outputs and (name in FreeMediaGenerator._music_names or "music" in text)
        return False

    async def models(self, kind: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        if kind == "image":
            # The official legacy image gateway remains public and keyless.
            # It is the only backend we can prove cannot consume account credit.
            return [
                {
                    "name": "legacy-flux",
                    "title": "Pollinations public image generator",
                    "category": "image",
                    "output_modalities": ["image"],
                    "pricing": {"currency": "pollen", "request": 0},
                    "public_keyless": True,
                }
            ]
        official_spaces = self._official_space_models(kind)
        category = "audio" if kind == "music" else kind
        cached = self._catalog_cache.get(category)
        if cached and not refresh and time.monotonic() - cached[0] < 300:
            catalog = cached[1]
        else:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    response = await client.get(f"{self.base_url}/{category}/models")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise RuntimeError("Media provider returned an invalid model catalog")
                catalog = [item for item in payload if isinstance(item, dict)]
                self._catalog_cache[category] = (time.monotonic(), catalog)
            except Exception:
                # Official public Spaces remain usable when Pollinations' optional
                # catalog is unreachable. Generation still reports Space failures.
                catalog = []
        pollinations = []
        for model in catalog:
            if self._is_free_tier_model(model, kind):
                copy = dict(model)
                copy.setdefault("provider", "Pollinations")
                pollinations.append(copy)
        return [*official_spaces, *self._china_models(kind), *pollinations]

    @staticmethod
    def _provider_pool(model: dict[str, Any]) -> str:
        if model.get("public_demo"):
            return "wan-public-demo"
        if model.get("space_id"):
            return "huggingface-zerogpu"
        if model.get("studio_id"):
            return "modelscope-studio"
        return "pollinations"

    @staticmethod
    def _is_shared_capacity_error(exc: Exception) -> bool:
        detail = str(exc).casefold()
        return any(
            marker in detail
            for marker in (
                "quota",
                "zero gpu",
                "zerogpu",
                "rate-limit",
                "rate limit",
                "429",
                "queue is busy",
                "shared free media queue",
            )
        )

    def _remember_provider_failure(self, model: dict[str, Any], exc: Exception) -> None:
        if not self._is_shared_capacity_error(exc):
            return
        # A provider-wide quota error normally affects its other public models
        # too. Avoid wasting several minutes retrying the same exhausted pool.
        self._provider_cooldowns[self._provider_pool(model)] = (
            time.monotonic() + 300,
            str(exc),
        )

    def _provider_cooldown_reason(self, model: dict[str, Any]) -> str:
        pool = self._provider_pool(model)
        cooldown = self._provider_cooldowns.get(pool)
        if cooldown is None:
            return ""
        expires, reason = cooldown
        if time.monotonic() >= expires:
            self._provider_cooldowns.pop(pool, None)
            return ""
        return reason

    async def _space_status(self, space_id: str, *, refresh: bool = False) -> tuple[bool, str]:
        cached = self._space_status_cache.get(space_id)
        if cached and not refresh and time.monotonic() - cached[0] < 120:
            return cached[1], cached[2]
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(f"https://huggingface.co/api/spaces/{space_id}")
                response.raise_for_status()
                payload = response.json() or {}
                runtime = payload.get("runtime") or {}
                stage = str(runtime.get("stage") or "").upper()
                hardware = str((runtime.get("hardware") or {}).get("current") or "")
                public_demo = any(
                    str(item.get("space_id") or "") == space_id and item.get("public_demo")
                    for models in self._space_models.values()
                    for item in models
                )
                available = stage == "RUNNING" and (
                    hardware.startswith("zero-") or public_demo
                )
                reason = "" if available else f"Space state is {stage or 'unknown'} on {hardware or 'unknown hardware'}"
                if available:
                    subdomain = str(payload.get("subdomain") or "").strip()
                    if not subdomain:
                        raise RuntimeError("Space generation subdomain is missing")
                    config_response = await client.get(f"https://{subdomain}.hf.space/config")
                    config_response.raise_for_status()
                    config = config_response.json()
                    if not isinstance(config, dict) or not config:
                        raise RuntimeError("Space generation endpoint returned invalid configuration")
        except Exception as exc:
            available = False
            reason = f"Space check failed: {type(exc).__name__}: {exc}"
        self._space_status_cache[space_id] = (time.monotonic(), available, reason)
        return available, reason

    async def status(self, *, egress_country: str = "UNKNOWN") -> dict[str, Any]:
        if egress_country == "CN":
            availability: dict[str, Any] = {}
            for kind in ("image", "video", "music"):
                models = self._china_models(kind)
                online, reason = await self._modelscope_model_status(models[0])
                availability[kind] = {
                    "available": online,
                    "models": [str(models[0]["name"])] if online else [],
                    "reason": reason,
                    "shared_quota": True,
                }
            return {
                "provider": "ModelScope mainland-China shared Studios",
                "configured": True,
                "free_only": True,
                "egress_route": "mainland-china",
                "availability": availability,
            }
        availability: dict[str, Any] = {}
        kinds = ("image", "video", "music")
        model_results = await asyncio.gather(
            *(self.models(kind) for kind in kinds), return_exceptions=True
        )
        space_ids = {
            str(model.get("space_id") or "")
            for result in model_results
            if isinstance(result, list)
            for model in result
            if model.get("space_id")
        }
        space_results = await asyncio.gather(
            *(self._space_status(space_id) for space_id in space_ids),
            return_exceptions=True,
        )
        space_statuses = dict(zip(space_ids, space_results, strict=True))
        studio_ids = {
            str(model.get("studio_id") or "")
            for result in model_results
            if isinstance(result, list)
            for model in result
            if model.get("studio_id")
        }
        studio_results = await asyncio.gather(
            *(
                self._modelscope_model_status(
                    next(
                        model
                        for result in model_results
                        if isinstance(result, list)
                        for model in result
                        if str(model.get("studio_id") or "") == studio_id
                    )
                )
                for studio_id in studio_ids
            ),
            return_exceptions=True,
        )
        studio_statuses = dict(zip(studio_ids, studio_results, strict=True))
        for kind, model_result in zip(kinds, model_results, strict=True):
            try:
                if isinstance(model_result, BaseException):
                    raise model_result
                models = model_result
                online_models: list[str] = []
                reasons: list[str] = []
                for model in models:
                    space_id = str(model.get("space_id") or "")
                    if space_id:
                        outcome = space_statuses.get(space_id)
                        if isinstance(outcome, BaseException) or outcome is None:
                            online, reason = False, f"Space check failed: {outcome}"
                        else:
                            online, reason = outcome
                        if online:
                            online_models.append(str(model.get("name") or ""))
                        elif reason:
                            reasons.append(reason)
                    elif model.get("studio_id"):
                        outcome = studio_statuses.get(str(model["studio_id"]))
                        if isinstance(outcome, BaseException) or outcome is None:
                            online, reason = False, f"Studio check failed: {outcome}"
                        else:
                            online, reason = outcome
                        if online:
                            online_models.append(str(model.get("name") or ""))
                        elif reason:
                            reasons.append(reason)
                    elif kind == "image" or self.api_key:
                        online_models.append(str(model.get("name") or ""))
                availability[kind] = {
                    "available": bool(online_models),
                    "models": online_models,
                    "reason": "" if online_models else (
                        "; ".join(dict.fromkeys(reasons))
                        or ("Pollinations API key not configured" if not self.api_key else "No free model is currently online")
                    ),
                    "shared_quota": kind in {"video", "music"},
                }
            except Exception as exc:
                availability[kind] = {
                    "available": False,
                    "models": [],
                    "reason": f"Catalog check failed: {type(exc).__name__}: {exc}",
                }
        return {
            "provider": "Hugging Face ZeroGPU + Wan public demo + ModelScope Studios + Pollinations zero-price catalog",
            "configured": bool(self.api_key),
            "free_only": True,
            "availability": availability,
        }

    @staticmethod
    def _default_extension(kind: str, content_type: str) -> str:
        content_type = content_type.split(";", 1)[0].strip().lower()
        known = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/opus": ".opus",
            "audio/ogg": ".ogg",
            "audio/wav": ".wav",
            "audio/flac": ".flac",
            "audio/aac": ".aac",
        }
        return known.get(content_type) or mimetypes.guess_extension(content_type) or {
            "image": ".jpg",
            "video": ".mp4",
            "music": ".mp3",
        }[kind]

    @staticmethod
    def _validate_content_type(kind: str, content_type: str) -> None:
        normalized = content_type.split(";", 1)[0].strip().lower()
        expected = {"image": "image/", "video": "video/", "music": "audio/"}[kind]
        if not normalized.startswith(expected):
            raise RuntimeError(
                f"Media provider returned {normalized or 'an unknown content type'} instead of {kind} data"
            )

    @staticmethod
    def _gradio_media_path(value: Any, kind: str) -> Path | None:
        extensions = {
            "image": {".jpg", ".jpeg", ".png", ".webp", ".bmp"},
            "video": {".mp4", ".webm", ".mov", ".mkv"},
            "music": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"},
        }[kind]
        if isinstance(value, (str, Path)):
            candidate = Path(value)
            return candidate if candidate.is_file() and candidate.suffix.lower() in extensions else None
        if isinstance(value, dict):
            for key in ("path", "video", "audio", "file"):
                if key in value:
                    found = FreeMediaGenerator._gradio_media_path(value[key], kind)
                    if found:
                        return found
            for item in value.values():
                found = FreeMediaGenerator._gradio_media_path(item, kind)
                if found:
                    return found
        if isinstance(value, (list, tuple)):
            for item in value:
                found = FreeMediaGenerator._gradio_media_path(item, kind)
                if found:
                    return found
        return None

    @staticmethod
    def _find_gradio_endpoint(client: Any, input_count: int) -> int:
        candidates = [
            endpoint
            for endpoint in client.endpoints.values()
            if len(getattr(endpoint, "input_component_types", ())) == input_count
            and bool(getattr(endpoint, "backend_fn", True))
        ]
        if not candidates:
            raise RuntimeError("ModelScope Studio generation endpoint was not found")
        endpoint = candidates[-1]
        # Some public Studios intentionally hide the API documentation while
        # their own public UI calls the same Gradio queue. Enabling the client
        # endpoint locally does not bypass authentication or provider limits.
        endpoint.is_valid = True
        return int(endpoint.fn_index)

    def _run_modelscope_generation(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        seed: int,
    ) -> Any:
        from gradio_client import Client

        client = Client(str(selected["studio_url"]), verbose=False)
        safe_seed = int(seed) if int(seed) >= 0 else 43
        if kind in {"image", "video"}:
            fn_index = self._find_gradio_endpoint(client, 22)
            safe_width = max(384, min(int(width), 704))
            safe_height = max(384, min(int(height), 704))
            safe_width -= safe_width % 32
            safe_height -= safe_height % 32
            args = (
                "models/Diffusion_Transformer/EasyAnimateV5.1-12b-zh-InP",
                "none",
                "none",
                "none",
                0.55,
                prompt,
                "Twisted body, limb deformities, text captions, comic, ugly, error, watermark.",
                "Flow",
                30,
                "Generate by",
                safe_width,
                safe_height,
                512,
                "Image Generation" if kind == "image" else "Video Generation",
                1 if kind == "image" else max(5, min(21, int(duration) * 4 + 1)),
                6.0,
                None,
                None,
                None,
                None,
                0.7,
                str(safe_seed),
            )
            return client.submit(*args, fn_index=fn_index).result(timeout=1800)
        fn_index = self._find_gradio_endpoint(client, 5)
        args = (
            prompt,
            "InspireMusic-1.5B-24kHz",
            "intro",
            24000,
            max(10, min(60, int(duration))),
        )
        return client.submit(*args, fn_index=fn_index).result(timeout=1800)

    async def _generate_with_modelscope(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        seed: int,
        egress_country: str = "UNKNOWN",
    ) -> dict[str, Any]:
        online, reason = await self._modelscope_studio_status(str(selected["studio_id"]))
        if not online:
            raise RuntimeError(f"{reason}. No overseas paid fallback will be used")
        try:
            result = await asyncio.to_thread(
                self._run_modelscope_generation,
                selected,
                kind,
                prompt,
                width,
                height,
                duration,
                seed,
            )
        except Exception as exc:
            raise RuntimeError(
                "The free ModelScope Studio generation endpoint is unreachable from the "
                f"current network: {type(exc).__name__}. No paid fallback will be used"
            ) from exc
        source = self._gradio_media_path(result, kind)
        if source is None:
            raise RuntimeError("The ModelScope Studio returned no usable media file")
        maximum = {"image": 20, "video": 29, "music": 25}[kind] * 1024 * 1024
        size = source.stat().st_size
        if not 0 < size <= maximum:
            raise RuntimeError(f"Generated {kind} exceeds the cross-channel delivery size limit")
        extension = source.suffix.lower() or {"image": ".png", "video": ".mp4", "music": ".wav"}[kind]
        target = self.output_dir / f"generated-{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}{extension}"
        shutil.copy2(source, target)
        content_type = mimetypes.guess_type(target.name)[0] or {
            "image": "image/png",
            "video": "video/mp4",
            "music": "audio/wav",
        }[kind]
        self._validate_content_type(kind, content_type)
        return {
            "ok": True,
            "provider": "ModelScope shared Studio",
            "free_only": True,
            "egress_route": "mainland-china" if egress_country == "CN" else "global-free-fallback",
            "kind": kind,
            "model": str(selected.get("name") or ""),
            "path": str(target.resolve()),
            "size": size,
            "content_type": content_type,
            "pricing": selected.get("pricing") or {},
            "delivery_compatible": {"desktop": True, "feishu": True, "telegram": True},
            "notice": (
                "Used a free shared ModelScope Studio selected for the current mainland-China egress."
                if egress_country == "CN"
                else "Used an anonymous free ModelScope Studio after another free provider was unavailable."
            ),
        }

    @staticmethod
    def _space_error(exc: Exception) -> RuntimeError:
        detail = str(exc).strip()
        lowered = detail.lower()
        if "quota" in lowered or "zero gpu" in lowered or "zerogpu" in lowered:
            message = "The shared free ZeroGPU quota is temporarily exhausted; retry later"
        elif "queue" in lowered or "rate" in lowered or "429" in lowered:
            message = "The shared free media queue is busy; retry later"
        elif "timeout" in lowered or "timed out" in lowered:
            message = "The shared free media generator timed out; retry later"
        elif isinstance(exc, TypeError):
            # A TypeError raised before a request reaches the Space usually
            # means our installed Gradio client and call adapter disagree. Do
            # not misreport this local compatibility defect as a provider-wide
            # Hugging Face outage.
            message = (
                "The local Gradio client rejected the media adapter call"
                + (f": {detail[:240]}" if detail else "")
            )
        else:
            message = f"The official free media Space is unavailable: {type(exc).__name__}"
        return RuntimeError(f"{message}. Paid fallback is disabled")

    def _run_space_generation(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        seed: int,
    ) -> Any:
        from gradio_client import Client

        # gradio_client 1.x calls this parameter ``hf_token``; newer Gradio
        # client builds are migrating it to ``token``. Detect the installed
        # signature so portable packages work with either compatible runtime.
        auth_parameter = (
            "hf_token"
            if "hf_token" in inspect.signature(Client.__init__).parameters
            else "token"
        )
        client = Client(
            str(selected["space_id"]),
            **{auth_parameter: self.huggingface_token or None, "verbose": False},
        )
        api_name = str(selected["api_name"])
        model_name = str(selected["name"])
        safe_seed = max(int(seed), 0)
        if model_name == "wan-2.1-public-demo":
            ratio = max(0.1, float(width) / max(1.0, float(height)))
            size = "720*1280" if ratio < 0.8 else ("960*960" if ratio < 1.25 else "1280*720")
            client.predict(
                prompt=prompt,
                size=size,
                watermark_wan=False,
                seed=safe_seed if int(seed) >= 0 else -1,
                api_name=api_name,
            )
            for _ in range(180):
                result = client.predict(api_name="/status_refresh")
                if self._gradio_media_path(result, "video") is not None:
                    return result
                time.sleep(5)
            raise TimeoutError("Wan public demo did not finish within 15 minutes")
        if kind == "video":
            safe_width = max(256, min(int(width), 768))
            safe_height = max(256, min(int(height), 768))
            safe_width -= safe_width % 32
            safe_height -= safe_height % 32
            if model_name == "ltx-video-2.3-zerogpu":
                job = client.submit(
                    input_image=None,
                    prompt=prompt,
                    duration=max(1.0, min(float(duration), 8.0)),
                    enhance_prompt=False,
                    seed=safe_seed,
                    randomize_seed=int(seed) < 0,
                    height=safe_height,
                    width=safe_width,
                    api_name=api_name,
                )
                return job.result(timeout=1200)
            job = client.submit(
                prompt=prompt,
                negative_prompt="worst quality, blurry, jittery, distorted, watermark",
                input_image_filepath=None,
                input_video_filepath=None,
                height_ui=safe_height,
                width_ui=safe_width,
                mode="text-to-video",
                duration_ui=max(1, min(int(duration), 8)),
                ui_frames_to_use=9,
                seed_ui=safe_seed,
                randomize_seed=int(seed) < 0,
                ui_guidance_scale=1,
                improve_texture_flag=True,
                api_name=api_name,
            )
            return job.result(timeout=1200)
        if model_name == "stable-audio-3-zerogpu":
            job = client.submit(
                variant_key="medium",
                prompt=prompt,
                duration=max(3, min(int(duration), 60)),
                steps=8,
                cfg_scale=1.0,
                sampler_type="pingpong",
                seed=safe_seed,
                api_name=api_name,
            )
            return job.result(timeout=1200)
        if model_name == "musicgen-zerogpu":
            # The official MusicGen demo exposes a fixed-duration two-input API.
            # Passing no melody performs ordinary text-to-music generation.
            job = client.submit(prompt, None, api_name=api_name)
            return job.result(timeout=1200)
        if model_name == "stable-audio-open-zerogpu":
            job = client.submit(
                prompt=prompt,
                seconds_total=max(3, min(int(duration), 47)),
                steps=8,
                cfg_scale=1.0,
                api_name=api_name,
            )
            return job.result(timeout=1200)
        if model_name == "diffrhythm-zerogpu":
            job = client.submit(
                lrc="[instrumental]",
                ref_audio_path=None,
                text_prompt=prompt,
                seed=safe_seed,
                randomize_seed=int(seed) < 0,
                steps=32,
                cfg_strength=4.0,
                file_type="mp3",
                odeint_method="euler",
                preference_infer="speed first",
                Music_Duration=95,
                api_name=api_name,
            )
            return job.result(timeout=1800)
        if model_name == "heartmula-zerogpu":
            job = client.submit(
                lyrics="[Instrumental]",
                tags=prompt,
                max_duration_seconds=max(30, min(int(duration), 60)),
                temperature=1.0,
                topk=50,
                cfg_scale=3.0,
                api_name=api_name,
            )
            return job.result(timeout=1800)
        if model_name == "ezaudio-zerogpu":
            job = client.submit(
                text=prompt,
                length=max(1, min(int(duration), 10)),
                guidance_scale=5.0,
                guidance_rescale=0.75,
                ddim_steps=50,
                eta=1.0,
                random_seed=max(0, min(safe_seed, 100)),
                randomize_seed=int(seed) < 0,
                api_name=api_name,
            )
            return job.result(timeout=1200)
        job = client.submit(
            audio_duration=max(5, min(int(duration), 120)),
            prompt=prompt,
            lyrics="[instrumental]",
            infer_step=27,
            api_name=api_name,
        )
        return job.result(timeout=1200)

    async def _generate_with_space(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        seed: int,
    ) -> dict[str, Any]:
        online, reason = await self._space_status(str(selected["space_id"]), refresh=True)
        if not online:
            raise RuntimeError(f"{reason or 'The official free media Space is offline'}. Paid fallback is disabled")
        try:
            result = await asyncio.to_thread(
                self._run_space_generation,
                selected,
                kind,
                prompt,
                width,
                height,
                duration,
                seed,
            )
        except Exception as exc:
            raise self._space_error(exc) from exc
        source = self._gradio_media_path(result, kind)
        if source is None:
            raise RuntimeError("The official free media Space returned no usable media file")
        maximum = {"video": 29, "music": 25}[kind] * 1024 * 1024
        size = source.stat().st_size
        if size <= 0:
            raise RuntimeError("The official free media Space returned an empty file")
        if size > maximum:
            raise RuntimeError(
                f"Generated {kind} exceeds the cross-channel delivery size limit; try a shorter duration"
            )
        extension = source.suffix.lower() or {"video": ".mp4", "music": ".wav"}[kind]
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.output_dir / f"generated-{kind}-{timestamp}-{uuid4().hex[:8]}{extension}"
        shutil.copy2(source, target)
        content_type = mimetypes.guess_type(target.name)[0] or {
            "video": "video/mp4",
            "music": "audio/wav",
        }[kind]
        self._validate_content_type(kind, content_type)
        return {
            "ok": True,
            "provider": str(selected.get("provider") or "Hugging Face ZeroGPU"),
            "free_only": True,
            "kind": kind,
            "model": str(selected.get("name") or ""),
            "path": str(target.resolve()),
            "size": size,
            "content_type": content_type,
            "pricing": selected.get("pricing") or {},
            "delivery_compatible": {"desktop": True, "feishu": True, "telegram": True},
            "notice": (
                "Used the official Wan public demo pool without caller billing. Its shared host quota applies; "
                "paid fallback is disabled."
                if selected.get("public_demo")
                else (
                    "Used an official public ZeroGPU Space with the configured free-account token. "
                    "The account's included daily quota applies; paid fallback is disabled."
                    if self.huggingface_token
                    else "Used an official public ZeroGPU Space anonymously. It is free but shared, "
                    "so stricter per-IP quota limits apply; paid fallback is disabled."
                )
            ),
        }

    async def _generate_with_pollinations(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        aspect_ratio: str,
        seed: int,
    ) -> dict[str, Any]:
        model_name = str(selected.get("name") or "")
        if kind != "image" and not self.api_key:
            raise RuntimeError("Pollinations free-tier key is not configured in Settings")
        params: dict[str, Any] = {"model": model_name, "safe": "true"}
        if kind == "image":
            params.update({"width": max(256, min(int(width), 2048)), "height": max(256, min(int(height), 2048)), "seed": int(seed)})
        elif kind == "video":
            params.update({"duration": max(1, min(int(duration), 120)), "aspectRatio": aspect_ratio, "seed": int(seed)})
        else:
            params.update({"response_format": "mp3", "duration": max(3, min(int(duration), 300))})

        if kind == "image" and model_name == "legacy-flux":
            url = f"{self.legacy_image_url}/prompt/{quote(prompt, safe='')}"
            headers: dict[str, str] = {}
            params["nologo"] = "true"
            params.pop("model", None)
        else:
            endpoint = "audio" if kind == "music" else kind
            url = f"{self.base_url}/{endpoint}/{quote(prompt, safe='')}"
            headers = {"Authorization": f"Bearer {self.api_key}"}
        maximum = {"image": 20, "video": 150, "music": 50}[kind] * 1024 * 1024
        temporary = self.output_dir / f".media-{uuid4().hex}.tmp"
        content_type = ""
        total = 0
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(600, connect=30), follow_redirects=True
                ) as client,
                client.stream("GET", url, params=params, headers=headers) as response,
            ):
                if response.status_code == 402:
                    raise RuntimeError("Free media quota is exhausted; paid fallback is disabled")
                if response.status_code == 429:
                    raise RuntimeError("Free media service is rate-limited; retry later")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                self._validate_content_type(kind, content_type)
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > maximum:
                            raise RuntimeError(f"Generated {kind} exceeds the local size limit")
                        handle.write(chunk)
            if total <= 0:
                raise RuntimeError("Media provider returned an empty file")
            extension = self._default_extension(kind, content_type)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            target = self.output_dir / f"generated-{kind}-{timestamp}-{uuid4().hex[:8]}{extension}"
            temporary.replace(target)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

        return {
            "ok": True,
            "provider": "Pollinations",
            "free_only": True,
            "kind": kind,
            "model": model_name,
            "path": str(target.resolve()),
            "size": total,
            "content_type": content_type.split(";", 1)[0],
            "pricing": selected.get("pricing") or {},
            "delivery_compatible": {"desktop": True, "feishu": True, "telegram": True},
            "notice": "Only a proven zero-price endpoint/model was used; credit-consuming fallback is disabled.",
        }

    async def _generate_candidate(
        self,
        selected: dict[str, Any],
        kind: str,
        prompt: str,
        width: int,
        height: int,
        duration: int,
        aspect_ratio: str,
        seed: int,
        egress_country: str,
    ) -> dict[str, Any]:
        if selected.get("space_id"):
            return await self._generate_with_space(
                selected, kind, prompt, width, height, duration, seed
            )
        if selected.get("studio_id"):
            return await self._generate_with_modelscope(
                selected,
                kind,
                prompt,
                width,
                height,
                duration,
                seed,
                egress_country,
            )
        return await self._generate_with_pollinations(
            selected, kind, prompt, width, height, duration, aspect_ratio, seed
        )

    async def generate(
        self,
        kind: str,
        prompt: str,
        *,
        model: str = "",
        width: int = 1024,
        height: int = 1024,
        duration: int = 6,
        aspect_ratio: str = "16:9",
        seed: int = -1,
        egress_country: str = "UNKNOWN",
    ) -> dict[str, Any]:
        if kind not in {"image", "video", "music"}:
            raise ValueError("kind must be image, video, or music")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if len(prompt) > 4000:
            raise ValueError("prompt must not exceed 4000 characters")
        if _SECRET_PATTERN.search(prompt):
            raise PermissionError("Credentials and secrets cannot be sent to a media provider")

        if egress_country == "CN":
            candidates = self._china_models(kind)
        elif kind == "image":
            candidates = await self.models(kind)
        else:
            # Try known public pools immediately. The optional live Pollinations
            # catalog is fetched only if these providers fail, avoiding a catalog
            # network round-trip on the common path.
            candidates = [
                *self._official_space_models(kind),
                *self._china_models(kind),
            ]

        if model:
            by_name = {str(item.get("name") or ""): item for item in candidates}
            selected = by_name.get(model)
            if selected is None and egress_country != "CN":
                all_models = await self.models(kind, refresh=True)
                selected = next(
                    (item for item in all_models if str(item.get("name") or "") == model),
                    None,
                )
            if selected is None:
                raise RuntimeError(
                    f"Model {model!r} is unavailable on the provider's free tier; paid fallback is disabled"
                )
            candidates = [selected]

        failures: list[dict[str, str]] = []
        attempted_names: set[str] = set()
        attempted_order: list[str] = []

        async def try_candidates(items: list[dict[str, Any]]) -> dict[str, Any] | None:
            for selected in items:
                model_name = str(selected.get("name") or "")
                if not model_name or model_name in attempted_names:
                    continue
                attempted_names.add(model_name)
                attempted_order.append(model_name)
                cooldown_reason = "" if model else self._provider_cooldown_reason(selected)
                if cooldown_reason:
                    failures.append(
                        {
                            "model": model_name,
                            "provider": str(selected.get("provider") or ""),
                            "error": "provider free quota is cooling down after a recent failure",
                        }
                    )
                    continue
                try:
                    result = await self._generate_candidate(
                        selected,
                        kind,
                        prompt,
                        width,
                        height,
                        duration,
                        aspect_ratio,
                        seed,
                        egress_country,
                    )
                    result["attempted_models"] = list(attempted_order)
                    result["fallback_attempts"] = failures
                    result["automatic_free_fallback"] = not bool(model)
                    return result
                except Exception as exc:
                    self._remember_provider_failure(selected, exc)
                    failures.append(
                        {
                            "model": model_name,
                            "provider": str(selected.get("provider") or ""),
                            "error": str(exc),
                        }
                    )
                    if model:
                        raise
            return None

        result = await try_candidates(candidates)
        if result is not None:
            return result

        if not model and egress_country != "CN" and kind in {"video", "music"}:
            # A live zero-price catalog may expose additional Pollinations models.
            # Only entries with explicit zero pricing pass models().
            result = await try_candidates(await self.models(kind, refresh=True))
            if result is not None:
                return result

        if not failures:
            raise RuntimeError(
                f"No free-tier {kind} model is currently available; paid fallback is disabled"
            )
        summary = "; ".join(
            f"{item['provider'] or 'unknown'} / {item['model']}: {item['error']}"
            for item in failures
        )
        raise RuntimeError(
            f"All verified-free {kind} providers are currently unavailable. {summary}. "
            "Paid fallback is disabled"
        )
