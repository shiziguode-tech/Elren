from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class LocalRuntimeCandidate:
    id: str
    label: str
    catalog_url: str
    base_url: str
    protocol: str = "openai"
    runtime_catalog_url: str = ""


DEFAULT_LOCAL_RUNTIMES = (
    LocalRuntimeCandidate(
        "ollama",
        "Ollama",
        "http://127.0.0.1:11434/api/tags",
        "http://127.0.0.1:11434/v1",
        "ollama",
    ),
    LocalRuntimeCandidate(
        "lm-studio",
        "LM Studio",
        "http://127.0.0.1:1234/v1/models",
        "http://127.0.0.1:1234/v1",
        runtime_catalog_url="http://127.0.0.1:1234/api/v0/models",
    ),
    LocalRuntimeCandidate(
        "llama-cpp",
        "llama.cpp / LocalAI",
        "http://127.0.0.1:8080/v1/models",
        "http://127.0.0.1:8080/v1",
    ),
    LocalRuntimeCandidate(
        "vllm",
        "vLLM / SGLang",
        "http://127.0.0.1:8001/v1/models",
        "http://127.0.0.1:8001/v1",
    ),
    LocalRuntimeCandidate(
        "jan",
        "Jan",
        "http://127.0.0.1:1337/v1/models",
        "http://127.0.0.1:1337/v1",
    ),
)


def _clean_capabilities(value: Any) -> tuple[str, ...]:
    def enabled(flag: Any) -> bool:
        if isinstance(flag, dict) and "supported" in flag:
            return enabled(flag.get("supported"))
        if isinstance(flag, str):
            return flag.strip().casefold() not in {
                "", "0", "false", "no", "none", "unsupported", "disabled"
            }
        return bool(flag)

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    elif isinstance(value, dict):
        values = [key for key, flag in value.items() if enabled(flag)]
    else:
        values = []
    aliases = {
        "image": "vision",
        "images": "vision",
        "image_url": "vision",
        "visual": "vision",
        "tool": "tools",
        "tool_calling": "tools",
        "function_calling": "tools",
        "reasoning": "thinking",
    }
    normalized = {
        aliases.get(str(item).strip().casefold(), str(item).strip().casefold())
        for item in values
        if str(item).strip()
    }
    return tuple(sorted(normalized))


def _declared_reasoning_levels(*values: Any) -> tuple[str, ...]:
    """Return only reasoning levels explicitly advertised by the runtime."""
    order = ("minimal", "low", "medium", "high", "xhigh", "max")
    supported = set(order)
    declared: set[str] = set()
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = value
        else:
            candidates = []
        for item in candidates:
            level = str(item).strip().casefold()
            if level in supported:
                declared.add(level)
    # Runtime catalogs are not required to return slider levels in increasing
    # order and sometimes repeat them.  Keep the UI/API contract deterministic.
    return tuple(level for level in order if level in declared)


def _context_window(model_info: Any) -> int | None:
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if str(key).casefold().endswith(".context_length"):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if 1_024 <= parsed <= 100_000_000:
                return parsed
    return None


def _valid_context_length(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1_024 <= parsed <= 100_000_000 else None


def _ollama_app_context_length() -> int | None:
    """Read the user's Ollama allocation without assuming an install path.

    ``/api/show`` reports the model's architectural maximum, not the context
    currently allocated by the Ollama app.  The app stores its slider value in
    a per-user SQLite database.  This is an optional best-effort source: older
    Ollama builds and headless servers simply fall through to the environment
    or live ``/api/ps`` allocation.
    """

    environment_value = _valid_context_length(os.environ.get("OLLAMA_CONTEXT_LENGTH"))
    if environment_value:
        return environment_value

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Ollama" / "db.sqlite")
    home = Path.home()
    candidates.extend(
        (
            home / "Library" / "Application Support" / "Ollama" / "db.sqlite",
            home / ".local" / "share" / "Ollama" / "db.sqlite",
        )
    )
    for path in candidates:
        if not path.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro",
                uri=True,
                timeout=0.1,
            )
            row = connection.execute(
                "SELECT context_length FROM settings WHERE id = 1"
            ).fetchone()
            configured = _valid_context_length(row[0] if row else None)
            if configured:
                return configured
        except (OSError, sqlite3.Error):
            continue
        finally:
            if connection is not None:
                connection.close()
    return None


class LocalModelRegistry:
    """Discover model servers that are reachable on this computer right now.

    Discovery is intentionally limited to fixed loopback URLs. It never scans
    the LAN, never starts an external runtime, and never keeps a stale model in
    the selectable catalog after a failed probe.
    """

    def __init__(
        self,
        *,
        candidates: tuple[LocalRuntimeCandidate, ...] = DEFAULT_LOCAL_RUNTIMES,
        timeout: float = 1.25,
        transport: httpx.AsyncBaseTransport | None = None,
        ollama_context_reader: Callable[[], int | None] = _ollama_app_context_length,
    ) -> None:
        self.candidates = candidates
        self.timeout = timeout
        self.transport = transport
        self.ollama_context_reader = ollama_context_reader
        self.models: list[dict[str, Any]] = []
        self.runtimes: list[dict[str, Any]] = []
        self.last_probed_at = ""
        self._lock = asyncio.Lock()

    async def discover(self) -> list[dict[str, Any]]:
        async with self._lock:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                results = await asyncio.gather(
                    *(self._probe(client, candidate) for candidate in self.candidates)
                )
            models: list[dict[str, Any]] = []
            runtimes: list[dict[str, Any]] = []
            seen: set[str] = set()
            for runtime, discovered in results:
                runtimes.append(runtime)
                for item in discovered:
                    selector = str(item["selector"])
                    if selector.casefold() in seen:
                        continue
                    seen.add(selector.casefold())
                    models.append(item)
            self.models = models
            self.runtimes = runtimes
            self.last_probed_at = datetime.now(UTC).isoformat()
            return [dict(item) for item in models]

    async def _probe(
        self,
        client: httpx.AsyncClient,
        candidate: LocalRuntimeCandidate,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            response = await client.get(candidate.catalog_url)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("catalog root is not an object")
            if candidate.protocol == "ollama":
                active_contexts = await self._ollama_active_contexts(client)
                configured_context = await asyncio.to_thread(
                    self.ollama_context_reader
                )
                models = await self._ollama_models(
                    client,
                    candidate,
                    payload,
                    active_contexts=active_contexts,
                    configured_context=configured_context,
                )
            else:
                runtime_payload: dict[str, Any] | None = None
                if candidate.runtime_catalog_url:
                    try:
                        runtime_response = await client.get(candidate.runtime_catalog_url)
                        runtime_response.raise_for_status()
                        candidate_payload = runtime_response.json()
                        if isinstance(candidate_payload, dict):
                            runtime_payload = candidate_payload
                    except Exception:
                        # The OpenAI-compatible catalog remains usable.  Never
                        # promote a declared architectural maximum to an active
                        # allocation just because this optional probe failed.
                        runtime_payload = None
                models = self._openai_models(
                    candidate, payload, runtime_payload=runtime_payload
                )
            return (
                {
                    "id": candidate.id,
                    "label": candidate.label,
                    "ready": True,
                    "model_count": len(models),
                    "error": "",
                },
                models,
            )
        except Exception as exc:
            # Runtime adapters and custom HTTP transports are third-party
            # boundaries.  One broken local server must not abort discovery of
            # every other healthy server.  ``CancelledError`` remains
            # cooperative because it derives from ``BaseException``.
            return (
                {
                    "id": candidate.id,
                    "label": candidate.label,
                    "ready": False,
                    "model_count": 0,
                    "error": type(exc).__name__,
                },
                [],
            )

    @staticmethod
    async def _ollama_active_contexts(
        client: httpx.AsyncClient,
    ) -> dict[str, int]:
        """Return the allocations Ollama is actually using for loaded models."""

        try:
            response = await client.get("http://127.0.0.1:11434/api/ps")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return {}
        catalog = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(catalog, list):
            return {}
        active: dict[str, int] = {}
        for item in catalog:
            if not isinstance(item, dict):
                continue
            model = str(item.get("name") or item.get("model") or "").strip()
            context = _valid_context_length(item.get("context_length"))
            if model and context:
                active[model.casefold()] = context
        return active

    async def _ollama_models(
        self,
        client: httpx.AsyncClient,
        candidate: LocalRuntimeCandidate,
        payload: dict[str, Any],
        *,
        active_contexts: dict[str, int],
        configured_context: int | None,
    ) -> list[dict[str, Any]]:
        catalog = payload.get("models")
        if not isinstance(catalog, list):
            raise ValueError("Ollama catalog is missing models")

        async def details(item: Any) -> dict[str, Any] | None:
            if not isinstance(item, dict):
                return None
            model = str(item.get("name") or item.get("model") or "").strip()
            if not model or len(model) > 240:
                return None
            capabilities: tuple[str, ...] = ()
            reasoning_levels: tuple[str, ...] = ()
            declared_context: int | None = None
            try:
                response = await client.post(
                    "http://127.0.0.1:11434/api/show",
                    json={"model": model, "verbose": False},
                )
                response.raise_for_status()
                shown = response.json()
                if isinstance(shown, dict):
                    capabilities = _clean_capabilities(shown.get("capabilities"))
                    reasoning_levels = _declared_reasoning_levels(
                        shown.get("reasoning_efforts"),
                        shown.get("supported_reasoning_efforts"),
                    )
                    declared_context = _context_window(shown.get("model_info"))
            except (httpx.HTTPError, ValueError, TypeError):
                # The model remains a valid completion route when the optional
                # capability endpoint is unavailable. Unknown capabilities are
                # deliberately not guessed from its name.
                pass
            active_context = active_contexts.get(model.casefold())
            if active_context:
                context_tokens = min(
                    value for value in (active_context, declared_context) if value
                )
                context_source = "ollama_active_allocation"
            elif configured_context:
                context_tokens = min(
                    value for value in (configured_context, declared_context) if value
                )
                context_source = "ollama_app_setting"
            else:
                # /api/show is an architectural maximum, not the allocation
                # currently used by the loaded process. Unknown must remain
                # unknown so the engine can learn through provider overflow
                # recovery instead of presenting a fabricated fixed limit.
                context_tokens = None
                context_source = "ollama_active_allocation_not_exposed"
            return self._entry(
                candidate,
                model,
                capabilities=capabilities,
                reasoning_levels=reasoning_levels,
                context_window_tokens=context_tokens,
                context_window_source=context_source,
                maximum_context_window_tokens=declared_context,
            )

        results = await asyncio.gather(*(details(item) for item in catalog[:100]))
        return [item for item in results if item is not None]

    def _openai_models(
        self,
        candidate: LocalRuntimeCandidate,
        payload: dict[str, Any],
        *,
        runtime_payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        catalog = payload.get("data")
        if not isinstance(catalog, list):
            raise ValueError("OpenAI-compatible catalog is missing data")
        runtime_contexts = self._runtime_contexts(runtime_payload or payload)
        models: list[dict[str, Any]] = []
        for item in catalog[:100]:
            if not isinstance(item, dict):
                continue
            model = str(item.get("id") or "").strip()
            if not model or len(model) > 240:
                continue
            capabilities = tuple(
                sorted(
                    {
                        *_clean_capabilities(item.get("capabilities")),
                        *_clean_capabilities(item.get("modalities")),
                    }
                )
            )
            capability_metadata = (
                item.get("capabilities")
                if isinstance(item.get("capabilities"), dict)
                else {}
            )
            reasoning_levels = _declared_reasoning_levels(
                item.get("reasoning_efforts"),
                item.get("supported_reasoning_efforts"),
                capability_metadata.get("reasoning_effort"),
                capability_metadata.get("reasoning_efforts"),
            )
            runtime_context, runtime_source, runtime_maximum = runtime_contexts.get(
                model.casefold(), (None, "local_runtime_active_context_not_exposed", None)
            )
            declared_maximum = next(
                (
                    parsed
                    for key in ("max_context_length", "context_length")
                    if (parsed := _valid_context_length(item.get(key))) is not None
                ),
                None,
            )
            maximum_context = runtime_maximum or declared_maximum
            context_tokens = (
                min(runtime_context, maximum_context)
                if runtime_context and maximum_context
                else runtime_context
            )
            models.append(
                self._entry(
                    candidate,
                    model,
                    capabilities=capabilities,
                    reasoning_levels=reasoning_levels,
                    context_window_tokens=context_tokens,
                    context_window_source=(
                        runtime_source
                        if context_tokens
                        else "local_runtime_active_context_not_exposed"
                    ),
                    maximum_context_window_tokens=maximum_context,
                )
            )
        return models

    @staticmethod
    def _runtime_contexts(
        payload: dict[str, Any],
    ) -> dict[str, tuple[int | None, str, int | None]]:
        """Extract active allocations without confusing them with model maxima."""

        catalog = payload.get("data") or payload.get("models")
        if not isinstance(catalog, list):
            return {}
        contexts: dict[str, tuple[int | None, str, int | None]] = {}
        for item in catalog:
            if not isinstance(item, dict):
                continue
            model = str(
                item.get("id") or item.get("model") or item.get("name") or ""
            ).strip()
            if not model:
                continue
            maximum = next(
                (
                    parsed
                    for key in ("max_context_length", "maximum_context_length")
                    if (parsed := _valid_context_length(item.get(key))) is not None
                ),
                None,
            )
            active_values = [
                parsed
                for key in (
                    "loaded_context_length",
                    "active_context_length",
                    "configured_context_length",
                    "runtime_context_length",
                    "n_ctx",
                    # vLLM exposes the configured engine limit under this key.
                    "max_model_len",
                )
                if (parsed := _valid_context_length(item.get(key))) is not None
            ]
            instances = item.get("loaded_instances")
            if isinstance(instances, list):
                for instance in instances:
                    if not isinstance(instance, dict):
                        continue
                    config = instance.get("config")
                    config = config if isinstance(config, dict) else {}
                    for value in (
                        instance.get("context_length"),
                        instance.get("loaded_context_length"),
                        config.get("context_length"),
                        config.get("context_window"),
                        config.get("n_ctx"),
                    ):
                        parsed = _valid_context_length(value)
                        if parsed:
                            active_values.append(parsed)
            active = min(active_values) if active_values else None
            if active and maximum:
                active = min(active, maximum)
            contexts[model.casefold()] = (
                active,
                "local_runtime_loaded_instance" if active else "local_runtime_active_context_not_exposed",
                maximum,
            )
        return contexts

    @staticmethod
    def _entry(
        candidate: LocalRuntimeCandidate,
        model: str,
        *,
        capabilities: tuple[str, ...],
        reasoning_levels: tuple[str, ...],
        context_window_tokens: int | None,
        context_window_source: str,
        maximum_context_window_tokens: int | None,
    ) -> dict[str, Any]:
        return {
            "selector": f"local-{candidate.id}:{model}",
            "provider": "local",
            "model": model,
            "base_url": candidate.base_url,
            "source": f"local-{candidate.id}",
            "runtime": candidate.label,
            "capabilities": list(capabilities),
            "reasoning_levels": list(reasoning_levels),
            "supports_thinking": "thinking" in capabilities,
            "supports_vision": "vision" in capabilities,
            "supports_tools": "tools" in capabilities,
            "supports_documents": True,
            "context_window_tokens": context_window_tokens,
            "context_window_source": context_window_source,
            "maximum_context_window_tokens": maximum_context_window_tokens,
        }

    def status(self) -> dict[str, Any]:
        return {
            "ready": bool(self.models),
            "model_count": len(self.models),
            "vision_model_count": sum(
                1 for item in self.models if item.get("supports_vision") is True
            ),
            "models": [
                {
                    "selector": item["selector"],
                    "model": item["model"],
                    "runtime": item["runtime"],
                    "capabilities": list(item["capabilities"]),
                    "reasoning_levels": list(item["reasoning_levels"]),
                    "context_window_tokens": item.get("context_window_tokens"),
                    "context_window_source": item.get("context_window_source"),
                    "maximum_context_window_tokens": item.get(
                        "maximum_context_window_tokens"
                    ),
                }
                for item in self.models
            ],
            "runtimes": [dict(item) for item in self.runtimes],
            "last_probed_at": self.last_probed_at,
        }
