from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from deepdesk.subprocess_env import credential_safe_environment

VISION_MODEL_HINTS = ("gemini", "qwen", "llava", "minicpm-v")
DISCOVERY_URLS = (
    "http://127.0.0.1:11434/v1",  # Ollama OpenAI compatibility
    "http://127.0.0.1:1234/v1",  # LM Studio
    "http://127.0.0.1:8080/v1",  # llama.cpp / LocalAI
    "http://127.0.0.1:8001/v1",  # conventional vLLM/SGLang sidecar
    "http://127.0.0.1:1337/v1",  # Jan
)
AICODEMIRROR_GEMINI_BASE_URL = "https://api.aicodemirror.ai/api/gemini"
AICODEMIRROR_VISION_MODEL = "gemini-3.8-flash"
DEEPSEEK_VISION_MODEL = "deepseek-v4-flash-vision-exp"
DEEPSEEK_VISION_BASE_URL = "https://api.deepseek.com"


@dataclass(slots=True)
class VisionEndpoint:
    base_url: str
    model: str
    source: str
    protocol: str = "openai"
    api_key: str = field(default="", repr=False)


class VisionRuntime:
    """Discovers and lazily starts an OpenAI-compatible visual model service."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        auto_discover: bool,
        start_command: str,
        start_timeout: float,
        workspace: Path,
        relay_api_key: str = "",
        relay_base_url: str = AICODEMIRROR_GEMINI_BASE_URL,
        relay_model: str = AICODEMIRROR_VISION_MODEL,
        deepseek_keys: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.configured_url = base_url.rstrip("/")
        self.api_key = api_key
        self.configured_model = model
        self.auto_discover = auto_discover
        self.start_command = start_command.strip()
        self.start_timeout = start_timeout
        self.workspace = workspace
        self.relay_api_key = relay_api_key.strip()
        self.relay_base_url = relay_base_url.rstrip("/")
        self.relay_model = relay_model.strip()
        self.deepseek_keys = deepseek_keys
        self.endpoint: VisionEndpoint | None = None
        self.process: subprocess.Popen | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""
        # A configured endpoint is intentionally probed lazily so that a slow
        # cloud connection never delays the local UI.  Keep that state separate
        # from a real failed probe; otherwise the UI incorrectly reports a
        # healthy, configured Google fallback as "not ready" after every boot.
        self.probed = False
        self.reachable_urls: set[str] = set()
        self.observed_models: dict[str, list[str]] = {}
        self.probe_errors: dict[str, str] = {}
        self.confirmed_local_vision_models: dict[str, set[str]] = {}

    def set_local_model_capabilities(self, models: list[dict[str, Any]]) -> None:
        """Accept vision routes only when the local runtime explicitly says so."""
        confirmed: dict[str, set[str]] = {}
        for item in models:
            if item.get("supports_vision") is not True:
                continue
            base_url = str(item.get("base_url") or "").rstrip("/")
            model = str(item.get("model") or "").strip()
            if base_url.startswith("http://127.0.0.1:") and model:
                confirmed.setdefault(base_url, set()).add(model)
        self.confirmed_local_vision_models = confirmed
        if (
            self.endpoint
            and self.endpoint.base_url.startswith("http://127.0.0.1:")
            and self.endpoint.model
            not in confirmed.get(self.endpoint.base_url.rstrip("/"), set())
        ):
            self.endpoint = None

    def set_api_key(self, api_key: str) -> None:
        """Update the configured cloud-vision credential for future probes."""
        self.set_api_keys(api_key, self.relay_api_key)

    def set_api_keys(self, api_key: str, relay_api_key: str) -> None:
        """Update direct and relay vision credentials without exposing either."""
        updated = api_key.strip()
        updated_relay = relay_api_key.strip()
        if updated != self.api_key or updated_relay != self.relay_api_key:
            self.api_key = updated
            self.relay_api_key = updated_relay
            self.endpoint = None
            self.last_error = ""
            self.probed = False
            self.probe_errors = {}

    @property
    def configured(self) -> bool:
        """Whether the explicit cloud fallback has enough data to be probed."""
        direct = self.configured_url and self.configured_model and self.api_key
        relay = self.relay_base_url and self.relay_model and self.relay_api_key
        return bool(direct or relay or self.deepseek_endpoints())

    def deepseek_endpoints(self) -> list[VisionEndpoint]:
        """Read current official credentials on every request, never relay keys."""
        keys = self.deepseek_keys() if self.deepseek_keys else ("", "")
        endpoints = []
        seen = set()
        for slot, key in zip(("primary", "backup"), keys, strict=True):
            key = str(key or "").strip()
            if key and key not in seen:
                seen.add(key)
                endpoints.append(VisionEndpoint(
                    base_url=DEEPSEEK_VISION_BASE_URL,
                    model=DEEPSEEK_VISION_MODEL,
                    source=f"deepseek-official-{slot}", api_key=key,
                ))
        return endpoints

    def semantic_fallback_endpoints(self) -> list[VisionEndpoint]:
        endpoints = self.deepseek_endpoints()
        if self.relay_api_key and self.relay_base_url and self.relay_model:
            endpoints.append(VisionEndpoint(
                base_url=self.relay_base_url, model=self.relay_model,
                source="aicodemirror-gemini", protocol="gemini",
                api_key=self.relay_api_key,
            ))
        return endpoints

    @property
    def known_runtime(self) -> str:
        return "Ollama" if shutil.which("ollama") else ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def headers_for(endpoint: VisionEndpoint) -> dict[str, str]:
        if not endpoint.api_key:
            return {}
        if endpoint.protocol == "gemini":
            return {"x-goog-api-key": endpoint.api_key}
        return {"Authorization": f"Bearer {endpoint.api_key}"}

    async def status(self, probe: bool = True) -> dict[str, Any]:
        official = self.deepseek_endpoints()
        if (
            self.endpoint
            and self.endpoint.source.startswith("deepseek-official-")
            and self.endpoint not in official
        ):
            # Status must not keep reporting a deleted/rotated credential
            # as ready until the next OCR call happens to refresh it.
            self.endpoint = None
            self.probed = False
        if probe and not self.endpoint:
            await self.discover()
        pending = None
        if self.configured_url and self.configured_model and self.api_key:
            pending = VisionEndpoint(self.configured_url, self.configured_model, "configured")
        elif official:
            pending = official[0]
        elif self.relay_base_url and self.relay_model and self.relay_api_key:
            pending = VisionEndpoint(self.relay_base_url, self.relay_model, "aicodemirror-gemini")
        displayed = self.endpoint or pending
        local_endpoint = bool(
            self.endpoint
            and self.endpoint.base_url.startswith("http://127.0.0.1:")
        )
        return {
            "ready": self.endpoint is not None,
            "configured": self.configured,
            "probed": self.probed,
            "base_url": displayed.base_url if displayed else "",
            "model": displayed.model if displayed else "",
            "source": (
                self.endpoint.source
                if self.endpoint
                else "configured" if self.configured else "unavailable"
            ),
            "local": local_endpoint,
            "auto_discover": self.auto_discover,
            "auto_start_configured": bool(self.start_command or self.known_runtime),
            "known_runtime": self.known_runtime,
            "managed_process_running": bool(self.process and self.process.poll() is None),
            "last_error": self.last_error,
        }

    async def ensure_ready(self) -> VisionEndpoint:
        async with self._lock:
            if self.endpoint and (
                self.endpoint.source.startswith("deepseek-official-")
                or (self.endpoint.source == "aicodemirror-gemini" and self.deepseek_endpoints())
            ):
                # Do not retain a rotated/deleted key, and do not require a
                # catalog probe before a documented model's actual request.
                self.endpoint = None
            if self.endpoint and await self._probe(self.endpoint):
                return self.endpoint
            self.endpoint = None
            discovered = await self.discover()
            if discovered:
                return discovered
            start_command: str | list[str] | None = (
                self.start_command or self._known_start_command()
            )
            ollama_reachable = "http://127.0.0.1:11434/v1" in self.reachable_urls
            if start_command and not (self.known_runtime == "Ollama" and ollama_reachable):
                self._start_process(start_command)
                deadline = asyncio.get_running_loop().time() + self.start_timeout
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(1)
                    discovered = await self.discover()
                    if discovered:
                        return discovered
                    if self.process and self.process.poll() is not None:
                        break
            if self.known_runtime == "Ollama" and ollama_reachable:
                raise RuntimeError("Ollama is running but no compatible vision model is installed")
            raise RuntimeError(self.last_error or "No compatible vision service found")

    async def discover(self) -> VisionEndpoint | None:
        self.probed = True
        self.reachable_urls = set()
        self.observed_models = {}
        self.probe_errors = {}
        candidates: list[tuple[str, str]] = []
        # A runtime-verified local visual model is private and quota-free, so
        # prefer it ahead of cloud semantic fallbacks.
        if self.auto_discover:
            candidates.extend((url, "auto-discovered") for url in DISCOVERY_URLS)
        if self.configured_url and self.api_key:
            candidates.append((self.configured_url, "configured"))
        unique_candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate[0] not in seen:
                unique_candidates.append(candidate)
                seen.add(candidate[0])
        results = await asyncio.gather(
            *(self._models(base_url) for base_url, _ in unique_candidates)
        )
        for (base_url, source), models in zip(unique_candidates, results, strict=True):
            if models is None:
                continue
            self.reachable_urls.add(base_url)
            self.observed_models[base_url] = models
            selected = self._select_model(models, base_url=base_url)
            if selected:
                self.endpoint = VisionEndpoint(
                    base_url=base_url,
                    model=selected,
                    source=source,
                    protocol="openai",
                    api_key=self.api_key if base_url == self.configured_url else "",
                )
                self.last_error = ""
                return self.endpoint
        deepseek = self.deepseek_endpoints()
        if deepseek:
            self.endpoint = deepseek[0]
            self.last_error = ""
            return self.endpoint
        if self.relay_api_key and self.relay_base_url and self.relay_model:
            relay_models = await self._gemini_models(self.relay_base_url, self.relay_api_key)
            if relay_models is not None:
                self.reachable_urls.add(self.relay_base_url)
                self.observed_models[self.relay_base_url] = relay_models
                selected = self._select_model_for(self.relay_model, relay_models)
                if selected:
                    self.endpoint = VisionEndpoint(
                        base_url=self.relay_base_url,
                        model=selected,
                        source="aicodemirror-gemini",
                        protocol="gemini",
                        api_key=self.relay_api_key,
                    )
                    self.last_error = ""
                    return self.endpoint
        configured_error = self.probe_errors.get(self.configured_url, "")
        relay_error = self.probe_errors.get(self.relay_base_url, "")
        errors = [error for error in (configured_error, relay_error) if error]
        if self.configured and errors:
            self.last_error = "Semantic vision fallback unavailable: " + "; ".join(errors)
        elif self.configured:
            self.last_error = "Semantic vision fallback returned no compatible visual model"
        else:
            self.last_error = (
                "No running Gemini, Qwen-VL, LLaVA, or MiniCPM-V compatible "
                "visual endpoint was found"
            )
        return None

    def _select_model(
        self, models: list[str], *, base_url: str = ""
    ) -> str | None:
        if base_url.startswith("http://127.0.0.1:"):
            confirmed = self.confirmed_local_vision_models.get(
                base_url.rstrip("/"), set()
            )
            return next((model for model in models if model in confirmed), None)
        configured = self._select_model_for(self.configured_model, models)
        if configured:
            return configured
        return next(
            (
                model
                for model in models
                if any(hint in model.lower() for hint in VISION_MODEL_HINTS)
            ),
            None,
        )

    @staticmethod
    def _select_model_for(configured_model: str, models: list[str]) -> str | None:
        return (
            configured_model
            if any(
                model == configured_model
                or model.removeprefix("models/") == configured_model
                for model in models
            )
            else None
        )

    async def _models(self, base_url: str) -> list[str] | None:
        try:
            timeout = 12.0 if base_url.startswith("https://") else 2.5
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers=self.headers if base_url == self.configured_url else {},
                )
                response.raise_for_status()
                data = response.json()
            return [item["id"] for item in data.get("data", []) if item.get("id")]
        except httpx.HTTPStatusError as exc:
            message = ""
            try:
                body = exc.response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("status") or "")
            except (ValueError, AttributeError):
                pass
            clean = " ".join(message.split())[:240]
            self.probe_errors[base_url] = clean or f"HTTP {exc.response.status_code}"
            return None
        except httpx.HTTPError as exc:
            self.probe_errors[base_url] = type(exc).__name__
            return None
        except (ValueError, KeyError, TypeError):
            self.probe_errors[base_url] = "invalid model catalog response"
            return None

    async def _gemini_models(self, base_url: str, api_key: str) -> list[str] | None:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.get(
                    f"{base_url}/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                )
                response.raise_for_status()
                data = response.json()
            return [
                str(item["name"])
                for item in data.get("models", [])
                if isinstance(item, dict) and item.get("name")
            ]
        except httpx.HTTPStatusError as exc:
            message = ""
            try:
                body = exc.response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("status") or "")
            except (ValueError, AttributeError):
                pass
            self.probe_errors[base_url] = " ".join(message.split())[:240] or (
                f"AI Code Mirror HTTP {exc.response.status_code}"
            )
            return None
        except httpx.HTTPError as exc:
            self.probe_errors[base_url] = f"AI Code Mirror {type(exc).__name__}"
            return None
        except (ValueError, KeyError, TypeError):
            self.probe_errors[base_url] = "AI Code Mirror returned an invalid model catalog"
            return None

    async def _probe(self, endpoint: VisionEndpoint | str) -> bool:
        if isinstance(endpoint, str):
            return await self._models(endpoint) is not None
        if endpoint.protocol == "gemini":
            return await self._gemini_models(endpoint.base_url, endpoint.api_key) is not None
        return await self._models(endpoint.base_url) is not None

    def _known_start_command(self) -> list[str] | None:
        ollama = shutil.which("ollama")
        return [ollama, "serve"] if ollama else None

    def _start_process(self, command: str | list[str]) -> None:
        if self.process and self.process.poll() is None:
            return
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self.process = subprocess.Popen(
            command,
            cwd=self.workspace,
            shell=False,
            env=credential_safe_environment(),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
