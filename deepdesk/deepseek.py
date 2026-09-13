from __future__ import annotations

import ast
import asyncio
import contextvars
import copy
import inspect
import json
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from deepdesk.custom_providers import (
    is_custom_provider,
    normalize_custom_provider,
    provider_selector,
)
from deepdesk.model_capabilities import (
    RETIRED_DEEPSEEK_MODEL,
    ReasoningCapability,
    is_retired_deepseek_selector,
    reasoning_capability,
)
from deepdesk.task_titles import clean_generated_title

logger = logging.getLogger(__name__)

Observer = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _notify_callback(callback: Callable, value: dict[str, Any]) -> None:
    """Preserve synchronous observers and await async persistence observers."""
    pending = callback(value)
    if inspect.isawaitable(pending):
        await pending

DEEPSEEK_V4_MAX_OUTPUT_TOKENS = 384_000
DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS = 1_000_000
ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS = 32_768
PROVIDER_WEB_SEARCH_TIMEOUT_SECONDS = 30.0
AICODEMIRROR_MODEL_COOLDOWN_SECONDS = 30 * 60

# Native provider adapters retain opaque state on normalized assistant messages
# so a tool-use continuation can be replayed faithfully. Those fields are not
# part of the OpenAI Chat Completions message schema and strict compatible
# endpoints reject them during cross-provider failover. Keep this deny-list
# deliberately exact: standard message/tool-call fields must pass unchanged.
PROVIDER_PRIVATE_MESSAGE_KEYS = frozenset(
    {
        "_anthropic_thinking_blocks",
        "_openai_reasoning_items",
        "_gemini_model_parts",
        "_provider_state_selector",
        "provider_state",
    }
)

# Safety fallbacks for providers that do not publish a machine-readable model
# context limit. They are kept separate from model metadata so the UI never
# presents them as an official per-model claim. A provider overflow response
# remains authoritative and triggers immediate context compaction.
CONSERVATIVE_CONTEXT_WINDOW_TOKENS = {
    "anthropic": 200_000,
    "google": 1_000_000,
    "openai": 128_000,
    "xai": 128_000,
}

PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "xai": "https://api.x.ai/v1",
}

AICODEMIRROR_BASE_URLS = {
    # Anthropic's SDK appends /v1/messages to the documented Base URL. This
    # client appends /messages itself, so retain /v1 in the internal endpoint.
    "anthropic": "https://api.aicodemirror.ai/api/claudecode/v1",
    "openai": "https://api.aicodemirror.ai/api/codex/backend-api/codex/v1",
    "google": "https://api.aicodemirror.ai/api/gemini",
}

AICODEMIRROR_MODELS = {
    "openai": (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-5.5", "gpt-5.4",
    ),
    "anthropic": (
        "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5",
        "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    ),
    "google": (
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
        "gemini-3.1-pro-preview", "gemini-3-flash-preview",
    ),
}

AICODEMIRROR_FABLE_MODELS = frozenset({
    "claude-fable-5",
    "claude-fable-5-1",
})

# Models removed by a provider must stay unavailable even when an older local
# settings file or a user-defined direct-provider row still references them.
# Historical task records are intentionally left untouched for auditability.
RETIRED_MODEL_IDS = frozenset({"gpt-5.6-luna", RETIRED_DEEPSEEK_MODEL})

# The relay preserves these vendors' exact public API model IDs, so their
# context windows can be sourced from the original vendors' model specs rather
# than guessed from the relay. Unknown user-entered IDs still use the explicit
# conservative fallback below.
OFFICIAL_MODEL_CONTEXT_WINDOWS = {
    **{
        ("openai", model): 1_050_000
        for model in AICODEMIRROR_MODELS["openai"]
    },
    **{
        ("anthropic", model): (
            200_000 if model == "claude-haiku-4-5-20251001" else 1_000_000
        )
        for model in AICODEMIRROR_MODELS["anthropic"]
    },
    **{
        ("google", model): 1_048_576
        for model in AICODEMIRROR_MODELS["google"]
    },
}


@dataclass(slots=True)
class ProviderEndpoint:
    selector: str
    provider: str
    model: str
    base_url: str
    keys: list[str]
    source: str = "direct"
    context_window_tokens: int | None = None
    context_window_source: str = "provider_not_declared"
    capabilities: tuple[str, ...] = ()
    reasoning_levels: tuple[str, ...] = ()
    runtime: str = ""


class ProviderError(RuntimeError):
    """Base request failure raised by the configured model provider."""


class DeepSeekError(ProviderError):
    pass


# Keep these subclasses compatible with older callers that catch
# ``DeepSeekError`` around the multi-provider client, while exposing the real
# failing provider in task errors instead of labelling everything DeepSeek.
class OpenAIError(DeepSeekError):
    pass


class AnthropicError(DeepSeekError):
    pass


class GeminiError(DeepSeekError):
    pass


class XAIError(DeepSeekError):
    pass


_PROVIDER_ERROR_TYPES = {
    "deepseek": DeepSeekError,
    "openai": OpenAIError,
    "anthropic": AnthropicError,
    "google": GeminiError,
    "xai": XAIError,
}


def _provider_error(provider: str, message: str) -> ProviderError:
    error_type = _PROVIDER_ERROR_TYPES.get(str(provider or "").casefold(), ProviderError)
    return error_type(message)


class ContextWindowError(DeepSeekError):
    """The provider rejected a request because its input context was too large."""


def _looks_like_context_window_error(status: int, detail: str) -> bool:
    if status not in {400, 413, 422}:
        return False
    lowered = str(detail or "").casefold()
    context_markers = (
        "context length", "context window", "maximum context", "max context",
        "too many tokens", "input is too long", "prompt is too long",
        "request too large", "token limit", "max_tokens",
    )
    return any(marker in lowered for marker in context_markers)


def _aicodemirror_terminal_error(response: httpx.Response) -> str:
    """Return a stable relay error code that should not be blindly retried."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    code = str(payload.get("code") or "").strip().upper()
    # These responses describe relay account/catalog configuration rather
    # than transient upstream load. Retrying the same request three times only
    # delays failover and consumes user time without any chance of recovery.
    if code in {
        "SETTLEMENT_PRICING_NOT_FOUND",
        "MODEL_NOT_FOUND",
        "MODEL_NOT_SUPPORTED",
        "CHANNEL_NOT_AVAILABLE",
    }:
        return code
    return ""


def _sse_data_payload(raw_line: str) -> str | None:
    """Return one SSE data payload while tolerating a UTF-8 BOM.

    Some OpenAI-compatible relays prefix the first streamed line with a BOM.
    Ignoring that line can discard the first (and sometimes only) assistant
    delta, which later appears as an empty assistant response.
    """

    line = raw_line.strip().lstrip("\ufeff")
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None
    return line[5:].strip()


@dataclass(slots=True)
class DeepSeekReply:
    message: dict[str, Any]
    usage: dict[str, Any]
    active_key: str
    finish_reason: str | None = None
    provider_model: str = ""


class _StreamAccumulator:
    """Incrementally merge SSE deltas without retaining every provider event.

    Hidden reasoning can be hundreds of thousands of tokens in V4 Max mode. It
    is therefore spooled to disk after a small memory threshold and is only
    materialized when a tool call must be replayed to the provider. Final text
    replies do not need their hidden reasoning in the agent conversation.
    """

    def __init__(self, *, retain_final_reasoning: bool) -> None:
        self.retain_final_reasoning = retain_final_reasoning
        self.event_count = 0
        self.reasoning_chars = 0
        self.content_chars = 0
        self.message: dict[str, Any] = {"role": "assistant", "content": ""}
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish_reason: str | None = None
        self.provider_model = ""
        self._reasoning = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - closed by close()
            max_size=1_048_576, mode="w+", encoding="utf-8", newline=""
        )
        self._content = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - closed by close()
            max_size=1_048_576, mode="w+", encoding="utf-8", newline=""
        )

    def feed(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        if event.get("model"):
            self.provider_model = str(event["model"])
        if event.get("usage"):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta") or choice.get("message") or {}
            if delta.get("role"):
                self.message["role"] = delta["role"]
            if delta.get("content"):
                part = str(delta["content"])
                self._content.write(part)
                self.content_chars += len(part)
            if delta.get("reasoning_content"):
                part = str(delta["reasoning_content"])
                self._reasoning.write(part)
                self.reasoning_chars += len(part)
            calls = delta.get("tool_calls") or []
            for ordinal, call in enumerate(calls):
                raw_index = call.get("index")
                if raw_index is not None:
                    index = int(raw_index)
                elif len(calls) > 1:
                    # Compatible relays occasionally omit index on every chunk
                    # but preserve the array position for parallel calls.
                    index = ordinal
                elif call.get("id"):
                    incoming_id = str(call["id"])
                    index = next(
                        (
                            existing_index
                            for existing_index, existing in self.tool_calls.items()
                            if existing.get("id") == incoming_id
                        ),
                        len(self.tool_calls),
                    )
                elif self.tool_calls:
                    # A single anonymous fragment is a continuation of the most
                    # recently opened call, not a new empty tool call.
                    index = max(self.tool_calls)
                else:
                    index = 0
                target = self.tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call.get("id"):
                    incoming_id = str(call["id"])
                    # Compatible relays sometimes repeat complete metadata on
                    # each argument delta. Preserve fragmented identifiers but
                    # do not turn `call_1` into `call_1call_1`.
                    if incoming_id != target["id"]:
                        target["id"] += incoming_id
                if call.get("type"):
                    target["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    incoming_name = str(function["name"])
                    if incoming_name != target["function"]["name"]:
                        target["function"]["name"] += incoming_name
                if function.get("arguments"):
                    target["function"]["arguments"] += str(function["arguments"])
                extra_content = call.get("extra_content")
                if isinstance(extra_content, dict):
                    incoming_google = extra_content.get("google") or {}
                    incoming_signature = (
                        incoming_google.get("thought_signature")
                        if isinstance(incoming_google, dict)
                        else None
                    )
                    if incoming_signature:
                        target_extra = target.setdefault("extra_content", {})
                        target_google = target_extra.setdefault("google", {})
                        existing_signature = str(
                            target_google.get("thought_signature") or ""
                        )
                        signature_part = str(incoming_signature)
                        if not existing_signature:
                            target_google["thought_signature"] = signature_part
                        elif signature_part == existing_signature:
                            pass
                        elif signature_part.startswith(existing_signature):
                            # Some compatible streams repeat the accumulated
                            # opaque value rather than send only its delta.
                            target_google["thought_signature"] = signature_part
                        else:
                            target_google["thought_signature"] = (
                                existing_signature + signature_part
                            )

    def progress(self, elapsed_seconds: float, *, final: bool = False) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "reasoning_chars": self.reasoning_chars,
            "content_chars": self.content_chars,
            "tool_call_count": len(self.tool_calls),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 1),
            "provider_model": self.provider_model,
            "usage": dict(self.usage),
            "final": final,
        }

    def result(self) -> dict[str, Any]:
        if not self.event_count:
            raise DeepSeekError("DeepSeek streaming response contained no data events")
        self._content.seek(0)
        self.message["content"] = self._content.read()
        if self.reasoning_chars and (self.retain_final_reasoning or self.tool_calls):
            self._reasoning.seek(0)
            self.message["reasoning_content"] = self._reasoning.read()
        if self.tool_calls:
            self.message["tool_calls"] = [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ]
        return {
            "choices": [
                {"message": self.message, "finish_reason": self.finish_reason}
            ],
            "usage": self.usage,
            "model": self.provider_model,
        }

    def close(self) -> None:
        self._reasoning.close()
        self._content.close()


class _AnthropicStreamAccumulator:
    """Translate native Anthropic Messages SSE events to the agent's chat shape."""

    _STOP_REASONS = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }

    def __init__(self) -> None:
        self.event_count = 0
        self.content_chars = 0
        self.reasoning_chars = 0
        self.provider_model = ""
        self.finish_reason: str | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._thinking_blocks: dict[int, dict[str, Any]] = {}

    def feed(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        kind = str(event.get("type") or "")
        if kind == "message_start":
            message = event.get("message") or {}
            self.provider_model = str(message.get("model") or "")
            usage = message.get("usage") or {}
            self.prompt_tokens = int(usage.get("input_tokens") or 0)
            return
        if kind == "content_block_start":
            index = int(event.get("index") or 0)
            block = event.get("content_block") or {}
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                part = str(block["text"])
                self._text.append(part)
                self.content_chars += len(part)
            elif block_type == "tool_use":
                raw_input = block.get("input")
                arguments = "" if raw_input in (None, {}) else json.dumps(
                    raw_input, ensure_ascii=False
                )
                self._tool_calls[index] = {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": arguments,
                    },
                }
            elif block_type in {"thinking", "redacted_thinking"}:
                # Claude requires the complete signed thinking block to be
                # replayed unchanged with a tool result. Keep provider state
                # separate from visible assistant text.
                self._thinking_blocks[index] = dict(block)
                if block.get("thinking"):
                    part = str(block["thinking"])
                    self._reasoning.append(part)
                    self.reasoning_chars += len(part)
            return
        if kind == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                part = str(delta.get("text") or "")
                self._text.append(part)
                self.content_chars += len(part)
            elif delta_type == "input_json_delta":
                call = self._tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                call["function"]["arguments"] += str(delta.get("partial_json") or "")
            elif delta_type == "thinking_delta":
                part = str(delta.get("thinking") or "")
                self._reasoning.append(part)
                self.reasoning_chars += len(part)
                thinking = self._thinking_blocks.setdefault(
                    index, {"type": "thinking", "thinking": ""}
                )
                thinking["thinking"] = str(thinking.get("thinking") or "") + part
            elif delta_type == "signature_delta":
                thinking = self._thinking_blocks.setdefault(
                    index, {"type": "thinking", "thinking": ""}
                )
                thinking["signature"] = str(thinking.get("signature") or "") + str(
                    delta.get("signature") or ""
                )
            return
        if kind == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = str(delta.get("stop_reason") or "")
            if stop_reason:
                self.finish_reason = self._STOP_REASONS.get(
                    stop_reason, stop_reason
                )
            usage = event.get("usage") or {}
            self.completion_tokens = int(usage.get("output_tokens") or 0)

    def progress(self, elapsed_seconds: float, *, final: bool = False) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "reasoning_chars": self.reasoning_chars,
            "content_chars": self.content_chars,
            "tool_call_count": len(self._tool_calls),
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 1),
            "provider_model": self.provider_model,
            "usage": self.usage,
            "final": final,
        }

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "reasoning_chars": self.reasoning_chars,
        }

    def result(self) -> dict[str, Any]:
        if not self.event_count:
            raise DeepSeekError("Anthropic streaming response contained no data events")
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self._text),
        }
        if self._reasoning and self._tool_calls:
            message["reasoning_content"] = "".join(self._reasoning)
        if self._thinking_blocks:
            message["_anthropic_thinking_blocks"] = [
                self._thinking_blocks[index]
                for index in sorted(self._thinking_blocks)
            ]
        if self._tool_calls:
            message["tool_calls"] = [
                self._tool_calls[index] for index in sorted(self._tool_calls)
            ]
        return {
            "choices": [
                {"message": message, "finish_reason": self.finish_reason}
            ],
            "usage": self.usage,
            "model": self.provider_model,
        }


class DeepSeekClient:
    """Multi-provider agent client with DeepSeek key failover and native adapters."""

    def __init__(
        self,
        base_url: str,
        model: str,
        primary_key: str,
        backup_key: str,
        timeout: float = 180,
        max_output_tokens: int | None = None,
        on_key_change: Callable[[str], None] | None = None,
    ) -> None:
        if is_retired_deepseek_selector(model):
            model = "deepseek-v4-flash"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keys = [key for key in [primary_key, backup_key] if key]
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.active_index = 0
        self.on_key_change = on_key_change
        self._task_model: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "deepdesk_task_model", default=None
        )
        self._task_max_output_tokens: contextvars.ContextVar[int | None] = (
            contextvars.ContextVar("deepdesk_task_max_output_tokens", default=None)
        )
        self._task_reasoning_effort: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar("deepdesk_task_reasoning_effort", default=None)
        )
        self._task_stream_progress: contextvars.ContextVar[
            Observer | None
        ] = contextvars.ContextVar("deepdesk_task_stream_progress", default=None)
        self._task_model_failover: contextvars.ContextVar[
            Observer | None
        ] = contextvars.ContextVar("deepdesk_task_model_failover", default=None)
        self._task_identity_override: contextvars.ContextVar[str | None] = (
            contextvars.ContextVar("deepdesk_task_identity_override", default=None)
        )
        self.provider_models: list[ProviderEndpoint] = []
        self.local_models: list[ProviderEndpoint] = []
        self._inactive_local_models: dict[str, ProviderEndpoint] = {}
        self._active_indices: dict[str, int] = {}
        self._temporarily_unavailable_models: dict[str, tuple[float, str]] = {}
        self.default_model_preference = model

    def _all_provider_models(self) -> list[ProviderEndpoint]:
        return [*self.provider_models, *self.local_models]

    def set_keys(self, primary_key: str, backup_key: str) -> None:
        """Replace credentials for future requests without exposing them to callers."""
        self.keys = [key for key in [primary_key, backup_key] if key]
        self.active_index = 0

    def set_provider_models(
        self,
        entries: list[dict[str, Any]],
        aicodemirror_keys: dict[str, str] | None = None,
        aicodemirror_fable_key: str = "",
    ) -> None:
        previous_keysets = {
            endpoint.selector: (endpoint.provider, endpoint.model, endpoint.base_url, tuple(endpoint.keys))
            for endpoint in self.provider_models
        }
        models: list[ProviderEndpoint] = []
        seen: set[str] = set()
        for entry in entries:
            entry = normalize_custom_provider(entry)
            custom = is_custom_provider(entry)
            provider = str(entry.get("provider") or "").strip().lower()
            model = str(entry.get("model") or "").strip()
            key = str(entry.get("api_key") or "").strip()
            if (
                provider not in PROVIDER_BASE_URLS
                or not model
                or not key
                or (not custom and model.casefold() in RETIRED_MODEL_IDS)
            ):
                continue
            selector = provider_selector(entry)
            if selector.casefold() in seen:
                continue
            seen.add(selector.casefold())
            models.append(
                ProviderEndpoint(
                    selector=selector,
                    provider=provider,
                    model=model,
                    base_url=entry["base_url"] if custom else PROVIDER_BASE_URLS[provider].rstrip("/"),
                    keys=[key],
                    source="custom" if custom else "direct",
                    reasoning_levels=tuple(entry.get("reasoning_levels", [])) if custom else (),
                    context_window_tokens=None if custom else OFFICIAL_MODEL_CONTEXT_WINDOWS.get(
                        (provider, model)
                    ),
                    context_window_source=(
                        "official_model_spec"
                        if not custom and (provider, model) in OFFICIAL_MODEL_CONTEXT_WINDOWS
                        else "provider_not_declared"
                    ),
                    capabilities=("tools",),
                )
            )
        relay_keys = aicodemirror_keys or {}
        dedicated_fable_key = str(aicodemirror_fable_key or "").strip()
        for provider, catalog in AICODEMIRROR_MODELS.items():
            normalized_key = str(relay_keys.get(provider) or "").strip()
            if not normalized_key and not (
                provider == "anthropic" and dedicated_fable_key
            ):
                continue
            for model in catalog:
                model_key = (
                    dedicated_fable_key
                    if provider == "anthropic"
                    and model in AICODEMIRROR_FABLE_MODELS
                    else normalized_key
                )
                if not model_key:
                    continue
                selector = f"aicodemirror-{provider}:{model}"
                if selector.casefold() in seen:
                    continue
                seen.add(selector.casefold())
                models.append(
                    ProviderEndpoint(
                        selector=selector,
                        provider=provider,
                        model=model,
                        base_url=AICODEMIRROR_BASE_URLS[provider],
                        keys=[model_key],
                        source="aicodemirror",
                        context_window_tokens=OFFICIAL_MODEL_CONTEXT_WINDOWS.get(
                            (provider, model)
                        ),
                        context_window_source="official_model_spec",
                        capabilities=("tools",),
                    )
                )
        self.provider_models = models
        valid = {
            endpoint.selector for endpoint in [*models, *self.local_models]
        }
        current_keysets = {
            endpoint.selector: (endpoint.provider, endpoint.model, endpoint.base_url, tuple(endpoint.keys))
            for endpoint in models
        }
        self._active_indices = {
            selector: index
            for selector, index in self._active_indices.items()
            if selector in valid
        }
        self._temporarily_unavailable_models = {
            selector: state
            for selector, state in self._temporarily_unavailable_models.items()
            if selector in valid
            and previous_keysets.get(selector)
            == current_keysets.get(selector)
        }

    def set_local_models(self, entries: list[dict[str, Any]]) -> None:
        """Replace the live local catalog with the latest loopback probe."""
        models: list[ProviderEndpoint] = []
        seen: set[str] = set()
        for entry in entries:
            selector = str(entry.get("selector") or "").strip()
            model = str(entry.get("model") or "").strip()
            base_url = str(entry.get("base_url") or "").rstrip("/")
            if (
                not selector.startswith("local-")
                or not model
                or not base_url.startswith("http://127.0.0.1:")
                or selector.casefold() in seen
            ):
                continue
            seen.add(selector.casefold())
            raw_capabilities = entry.get("capabilities")
            capabilities = tuple(
                str(item).strip().casefold()
                for item in raw_capabilities
                if str(item).strip()
            ) if isinstance(raw_capabilities, list) else ()
            raw_levels = entry.get("reasoning_levels")
            declared_levels = {
                str(item).strip().casefold()
                for item in raw_levels
                if str(item).strip().casefold()
                in {"minimal", "low", "medium", "high", "xhigh", "max"}
            } if isinstance(raw_levels, list) else set()
            reasoning_levels = tuple(
                level
                for level in ("minimal", "low", "medium", "high", "xhigh", "max")
                if level in declared_levels
            )
            raw_context = entry.get("context_window_tokens")
            try:
                context_tokens = int(raw_context) if raw_context else None
            except (TypeError, ValueError):
                context_tokens = None
            if context_tokens is not None and not 1_024 <= context_tokens <= 100_000_000:
                context_tokens = None
            models.append(
                ProviderEndpoint(
                    selector=selector,
                    provider="local",
                    model=model,
                    base_url=base_url,
                    # A non-empty sentinel lets the common OpenAI-compatible
                    # request path run. It is sent only to fixed loopback URLs.
                    keys=["local-runtime"],
                    source=str(entry.get("source") or "local-runtime"),
                    context_window_tokens=context_tokens,
                    context_window_source=str(
                        entry.get("context_window_source")
                        or "local_runtime_not_declared"
                    ),
                    capabilities=capabilities,
                    reasoning_levels=reasoning_levels,
                    runtime=str(entry.get("runtime") or "Local runtime"),
                )
            )
        self._inactive_local_models.update(
            {endpoint.selector: endpoint for endpoint in self.local_models}
        )
        self.local_models = models
        for endpoint in models:
            self._inactive_local_models.pop(endpoint.selector, None)
        while len(self._inactive_local_models) > 256:
            self._inactive_local_models.pop(next(iter(self._inactive_local_models)))
        valid = {endpoint.selector for endpoint in self._all_provider_models()}
        self._active_indices = {
            selector: index
            for selector, index in self._active_indices.items()
            if selector in valid
        }
        self._temporarily_unavailable_models = {
            selector: state
            for selector, state in self._temporarily_unavailable_models.items()
            if selector in valid
        }

    def set_default_model_preference(self, selector: str) -> None:
        """Set the model used when a task's send-bar choice is Automatic.

        ``auto`` preserves capability-aware multi-provider routing. A concrete
        selector makes Automatic behave as "use my configured default" while
        the send bar can still override it for one turn.
        """
        preference = str(selector or "auto").strip() or "auto"
        options = self.model_options()
        available = {item["selector"] for item in options}
        if preference != "auto" and preference not in available:
            preference = "auto"
        self.default_model_preference = preference
        if preference == "auto":
            # The settings/status interface must still boot when no key exists;
            # task creation performs the strict credential preflight.
            if options:
                try:
                    self.model = self.select_automatic_model(False)
                except DeepSeekError:
                    # All cloud routes may be in a temporary circuit-open
                    # state. Keep status/settings available, while task routing
                    # will fail fast rather than call a known-dead endpoint.
                    available = {item["selector"] for item in options}
                    if self.model not in available:
                        self.model = str(options[0]["selector"])
            else:
                self.model = "deepseek-v4-flash"
        else:
            self.model = preference

    @property
    def effective_selector(self) -> str:
        return self._task_model.get() or self.model

    @property
    def effective_model(self) -> str:
        return self._endpoint_for(self.effective_selector).model

    def bind_task_model(self, model: str) -> contextvars.Token[str | None]:
        self._task_identity_override.set(None)
        return self._task_model.set(model)

    def reset_task_model(self, token: contextvars.Token[str | None]) -> None:
        self._task_model.reset(token)
        self._task_identity_override.set(None)

    def bind_task_max_output_tokens(
        self, max_tokens: int
    ) -> contextvars.Token[int | None]:
        """Bound one child/task response without mutating provider settings."""

        bounded = max(128, min(32_768, int(max_tokens)))
        return self._task_max_output_tokens.set(bounded)

    def reset_task_max_output_tokens(
        self, token: contextvars.Token[int | None]
    ) -> None:
        self._task_max_output_tokens.reset(token)

    def bind_task_reasoning_effort(
        self, effort: str
    ) -> contextvars.Token[str | None]:
        """Bind provider-neutral reasoning depth for one task/context only."""
        if effort not in {
            "auto", "minimal", "low", "medium", "high", "xhigh", "max"
        }:
            raise ValueError("Unsupported reasoning effort")
        return self._task_reasoning_effort.set(effort)

    def _reasoning_effort(self, *, recovery: bool = False) -> str | None:
        effort = self._task_reasoning_effort.get() or "high"
        if effort == "auto":
            return None
        return "low" if recovery else effort

    def reasoning_capability(
        self, selector: str | None = None
    ) -> ReasoningCapability:
        endpoint = self._endpoint_for(selector or self.effective_selector)
        if endpoint.source == "custom":
            return ReasoningCapability(
                levels=endpoint.reasoning_levels,
                control=(
                    ("openai_reasoning_effort" if endpoint.provider == "openai"
                     else "anthropic_output_config_effort")
                    if endpoint.reasoning_levels else "none"
                ),
                verification="user_declared",
            )
        if endpoint.provider == "local":
            return ReasoningCapability(
                levels=endpoint.reasoning_levels,
                control=(
                    "local_declared_reasoning_effort"
                    if endpoint.reasoning_levels
                    else "none"
                ),
                verification=(
                    "local_runtime_declaration"
                    if endpoint.reasoning_levels
                    else "local_runtime_did_not_declare_levels"
                ),
            )
        if endpoint.provider == "google" and endpoint.source != "aicodemirror":
            # The direct Google route intentionally uses Google's OpenAI-
            # compatible Chat endpoint, not native generateContent. Its exact
            # translation of thinkingLevel/budget has not been established, so
            # do not advertise the native control contract for that route.
            return ReasoningCapability(
                verification="google_openai_compatibility_reasoning_unverified"
            )
        return reasoning_capability(endpoint.provider, endpoint.model)

    def _effective_reasoning_effort(
        self,
        *,
        endpoint: ProviderEndpoint | None = None,
        recovery: bool = False,
    ) -> str | None:
        endpoint = endpoint or self._endpoint_for(self.effective_selector)
        capability = self.reasoning_capability(endpoint.selector)
        effort = self._reasoning_effort(recovery=recovery)
        if effort in capability.levels:
            return effort
        # Never invent an API control for an unknown model and never silently
        # remap an unsupported level. Omitting the field lets the provider use
        # its documented default.
        return None

    def _openai_reasoning_effort(
        self,
        *,
        endpoint: ProviderEndpoint | None = None,
        recovery: bool = False,
    ) -> str | None:
        endpoint = endpoint or self._endpoint_for(self.effective_selector)
        if endpoint.provider != "openai":
            return None
        return self._effective_reasoning_effort(
            endpoint=endpoint, recovery=recovery
        )

    def reset_task_reasoning_effort(
        self, token: contextvars.Token[str | None]
    ) -> None:
        self._task_reasoning_effort.reset(token)

    def bind_task_stream_progress(
        self, callback: Observer
    ) -> contextvars.Token[Observer | None]:
        """Bind a metadata-only stream progress callback to the current task."""
        return self._task_stream_progress.set(callback)

    def reset_task_stream_progress(
        self,
        token: contextvars.Token[Observer | None],
    ) -> None:
        self._task_stream_progress.reset(token)

    def bind_task_model_failover(
        self, callback: Observer
    ) -> contextvars.Token[Observer | None]:
        """Enable task-scoped model failover and report every route change."""
        return self._task_model_failover.set(callback)

    def reset_task_model_failover(
        self,
        token: contextvars.Token[Observer | None],
    ) -> None:
        self._task_model_failover.reset(token)

    def _model_failover_candidates(
        self, current: ProviderEndpoint, excluded: set[str]
    ) -> list[ProviderEndpoint]:
        """Return configured alternatives, preferring a different AI company."""
        endpoints: list[ProviderEndpoint] = []
        if self.keys:
            endpoints.extend(
                self._endpoint_for(selector)
                for selector in ("deepseek-v4-flash", "deepseek-v4-pro")
            )
        endpoints.extend(
            endpoint
            for endpoint in self._all_provider_models()
            if endpoint.provider != "local" or "tools" in endpoint.capabilities
        )
        unique: dict[str, ProviderEndpoint] = {}
        for endpoint in endpoints:
            if endpoint.keys and endpoint.selector not in excluded:
                unique.setdefault(endpoint.selector, endpoint)
        difficult = self._capability_score(
            {"model": current.model, "provider": current.provider}
        ) >= 70

        def rank(endpoint: ProviderEndpoint) -> tuple[int, int]:
            option = {
                "selector": endpoint.selector,
                "provider": endpoint.provider,
                "model": endpoint.model,
            }
            return (
                1 if endpoint.provider != current.provider else 0,
                self._automatic_route_score(option, difficult),
            )

        return sorted(unique.values(), key=rank, reverse=True)

    def _model_failover_identity(
        self, endpoint: ProviderEndpoint, *, previous_selector: str
    ) -> str:
        identity = self.model_identity_info(endpoint.selector)
        return (
            "MODEL FAILOVER OVERRIDE FOR THE CURRENT TASK: the previous model became "
            "unavailable and the host has switched this task. This instruction supersedes "
            "every earlier runtime-identity statement in the conversation. Exact current "
            f"model ID: {identity['model']}. Developer/provider: "
            f"{identity['provider_name']}. Connection route: {identity['route']}. Host "
            f"selector: {identity['selector']}. Identify yourself using this new model and "
            "provider if asked; do not claim to still be the previous model. "
            f"Host-recorded previous selector: {previous_selector}. "
            "This was an actual automatic model switch, not merely a retry of "
            "the same model. In your final answer, briefly disclose this switch "
            "in the user's language. Never claim that no other model was used "
            "or that a same-model-only requirement was satisfied. A successful "
            "fallback does not prove the originally requested model worked. "
            "This event does not establish whether subagents were used; report "
            "that separately using actual tool activity."
        )

    @property
    def active_label(self) -> str:
        endpoint = self._endpoint_for(self.effective_selector)
        if not endpoint.keys:
            return "未配置"
        index = self._active_indices.get(endpoint.selector, 0)
        if endpoint.provider == "deepseek" and endpoint.selector in {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }:
            return "主密钥" if index == 0 else "备用密钥"
        if endpoint.source == "aicodemirror":
            return "AICodeMirror"
        return endpoint.provider.upper()

    def _endpoint_for(self, selector: str) -> ProviderEndpoint:
        if is_retired_deepseek_selector(selector):
            raise DeepSeekError("This model has been removed; select an available model")
        for endpoint in self._all_provider_models():
            if endpoint.selector == selector:
                return endpoint
        if selector.startswith("custom:"):
            raise DeepSeekError("This custom API route is no longer configured; select an available model")
        if selector.startswith("local-") and selector in self._inactive_local_models:
            # Keep an in-flight task pinned to its original loopback route long
            # enough for the ordinary transport failure/failover path to run.
            # The inactive route is not returned by model_options(), so no new
            # task can select it and it can never be reinterpreted as DeepSeek.
            return self._inactive_local_models[selector]
        if selector.startswith("deepseek:"):
            model = selector.split(":", 1)[1]
        else:
            model = selector
        return ProviderEndpoint(
            selector=selector,
            provider="deepseek",
            model=model,
            base_url=(
                "https://api.deepseek.com"
                if model == "deepseek-v4-flash-vision-exp" else self.base_url
            ),
            keys=list(self.keys),
        )

    def model_identity_info(self, selector: str | None = None) -> dict[str, str]:
        """Return authoritative, host-side identity metadata for one model route.

        Models cannot reliably infer the API route that invoked them.  Keep this
        metadata outside conversation history and derive it from the endpoint
        selected by the host for the current task.
        """
        endpoint = self._endpoint_for(selector or self.effective_selector)
        provider_names = {
            "deepseek": "DeepSeek",
            "openai": "OpenAI (GPT / ChatGPT family)",
            "anthropic": "Anthropic (Claude family)",
            "google": "Google (Gemini family)",
            "xai": "xAI (Grok family)",
            "local": "Local model",
        }
        return {
            "selector": endpoint.selector,
            "model": endpoint.model,
            "provider": endpoint.provider,
            "provider_name": "Third-party compatible API" if endpoint.source == "custom" else provider_names.get(
                endpoint.provider, endpoint.provider or "unknown provider"
            ),
            "route": (
                "Third-party compatible API" if endpoint.source == "custom" else "AI Code Mirror relay"
                if endpoint.source == "aicodemirror"
                else (
                    f"local runtime ({endpoint.runtime})"
                    if endpoint.provider == "local"
                    else "direct provider API"
                )
            ),
        }

    def model_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        if self.keys:
            options.extend(
                [
                    {
                        "selector": "deepseek-v4-flash",
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "context_window_tokens": DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS,
                        "context_window_source": "built_in_model_limit",
                        "reasoning": reasoning_capability(
                            "deepseek", "deepseek-v4-flash"
                        ).public(),
                        "capabilities": ["tools"],
                        "supports_vision": False,
                        "supports_tools": True,
                        "supports_documents": False,
                    },
                    {
                        "selector": "deepseek-v4-pro",
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "context_window_tokens": DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS,
                        "context_window_source": "built_in_model_limit",
                        "reasoning": reasoning_capability(
                            "deepseek", "deepseek-v4-pro"
                        ).public(),
                        "capabilities": ["tools"],
                        "supports_vision": False,
                        "supports_tools": True,
                        "supports_documents": False,
                    },
                    {
                        "selector": "deepseek-v4-flash-vision-exp",
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash-vision-exp",
                        "context_window_tokens": DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS,
                        "context_window_source": "built_in_model_limit",
                        "reasoning": reasoning_capability(
                            "deepseek", "deepseek-v4-flash-vision-exp"
                        ).public(),
                        "capabilities": ["tools", "vision"],
                        "supports_vision": True,
                        "supports_tools": True,
                        "supports_documents": False,
                    },
                ]
            )
        options.extend(
            {
                "selector": endpoint.selector,
                "provider": (
                    f"aicodemirror-{endpoint.provider}"
                    if endpoint.source == "aicodemirror"
                    else endpoint.provider
                ),
                "model": endpoint.model,
                **({"source": "custom", "base_url": endpoint.base_url}
                   if endpoint.source == "custom" else {}),
                **self.context_window_info(endpoint.selector),
                "reasoning": self.reasoning_capability(endpoint.selector).public(),
                "capabilities": list(endpoint.capabilities),
                "local_runtime": endpoint.runtime,
                "supports_vision": "vision" in endpoint.capabilities,
                "supports_tools": "tools" in endpoint.capabilities,
                "supports_documents": endpoint.provider == "local",
            }
            for endpoint in self._all_provider_models()
            if endpoint.selector not in {item["selector"] for item in options}
        )
        return options

    def has_model_credentials(self, selector: str = "auto") -> bool:
        """Return whether a requested route can run without creating a task.

        Keep this check host-side so an unconfigured UI, remote channel, or API
        client cannot create a doomed task and wait for provider retries.
        """
        available = {item["selector"] for item in self.model_options()}
        requested = str(selector or "auto").strip() or "auto"
        if requested == "auto":
            default = str(self.default_model_preference or "auto")
            return bool(available) if default == "auto" else default in available
        return requested in available

    def context_window_info(self, selector: str | None = None) -> dict[str, Any]:
        """Return the effective safety window together with transparent provenance."""
        endpoint = self._endpoint_for(selector or self.effective_selector)
        if endpoint.context_window_tokens:
            return {
                "context_window_tokens": endpoint.context_window_tokens,
                "context_window_source": endpoint.context_window_source,
            }
        if endpoint.provider == "deepseek" and endpoint.selector in {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }:
            return {
                "context_window_tokens": DEEPSEEK_V4_CONTEXT_WINDOW_TOKENS,
                "context_window_source": "built_in_model_limit",
            }
        if endpoint.provider == "local":
            return {
                "context_window_tokens": None,
                "context_window_source": (
                    endpoint.context_window_source
                    or "local_runtime_active_context_not_exposed"
                ),
            }
        tokens = CONSERVATIVE_CONTEXT_WINDOW_TOKENS.get(endpoint.provider, 128_000)
        return {
            "context_window_tokens": tokens,
            "context_window_source": (
                "relay_not_declared_conservative_fallback"
                if endpoint.source == "aicodemirror"
                else "provider_not_declared_conservative_fallback"
            ),
        }

    def context_window_tokens(self) -> int | None:
        """Return a safe working window for automatic context compaction.

        DeepSeek V4 has a product-level window known to this runtime.  Extra
        provider model names are user supplied and may vary, so their values are
        deliberately conservative; a provider's explicit overflow response is
        still authoritative and triggers immediate recovery compaction.
        """
        value = self.context_window_info()["context_window_tokens"]
        return int(value) if value is not None else None

    @staticmethod
    def _capability_score(option: dict[str, str]) -> int:
        name = str(option.get("model") or "").casefold()
        score = 50
        for marker, delta in {
            "pro": 24, "opus": 28, "sonnet": 18, "gpt-5": 24,
            "o3": 22, "o4": 22, "grok-4": 22, "max": 16,
            "flash": -22, "mini": -18, "nano": -25, "haiku": -22,
            "lite": -18, "fast": -14,
        }.items():
            if marker in name:
                score += delta
        return score

    @classmethod
    def _automatic_route_score(
        cls,
        option: dict[str, str],
        difficult: bool,
        prompt: str = "",
    ) -> int:
        """Balance capability, likely cost, and task fit across configured APIs.

        The old easy-task route selected the *lowest* capability score. Since
        DeepSeek options are listed first, ties made automatic mode look like a
        DeepSeek-only feature even when other providers were configured. This
        utility score instead considers every available endpoint, while
        demanding tasks retain a strong preference for top capability tiers
        such as Sol and Opus.
        """
        name = str(option.get("model") or "").casefold()
        provider = str(option.get("provider") or "").casefold()
        text = str(prompt or "").casefold()
        score = cls._capability_score(option)

        if difficult:
            for marker, bonus in {
                "gpt-5.6-sol": 12,
                "claude-opus-5": 10,
                "gpt-5.6-terra": 5,
                "sonnet-5": 4,
                "pro": 3,
            }.items():
                if marker in name:
                    score += bonus
        else:
            for marker, bonus in {
                "flash": 8,
                "haiku": 7,
                "mini": 7,
                "nano": 5,
                "opus": -18,
                "gpt-5.6-sol": -12,
                "pro": -12,
                # Terra is the closest supported successor for ordinary work
                # after the relay retired Luna. Keep it slightly ahead of the
                # older generic GPT-5.5 route without affecting hard-task Sol.
                "gpt-5.6-terra": 5,
                "sonnet": -5,
                "fable": -4,
            }.items():
                if marker in name:
                    score += bonus

        # International direct and relay models should genuinely participate
        # instead of losing list-order ties to the built-in DeepSeek entries.
        if provider != "deepseek":
            score += 3

        coding_markers = ("code", "coding", "debug", "program", "软件", "代码", "编程", "调试")
        visual_markers = ("image", "video", "vision", "picture", "图片", "图像", "视频", "视觉")
        writing_markers = ("write", "draft", "rewrite", "文案", "写作", "润色", "起草")
        if any(marker in text for marker in coding_markers):
            if "gpt-5.6-sol" in name:
                score += 7
            elif "opus" in name:
                score += 3
        if any(marker in text for marker in visual_markers) and provider.endswith("google"):
            score += 8
        if any(marker in text for marker in writing_markers) and (
            "sonnet" in name or "fable" in name
        ):
            score += 7
        return score

    def select_automatic_model(self, difficult: bool, prompt: str = "") -> str:
        options = self.model_options()
        cloud_options = [
            option for option in options if option.get("provider") != "local"
        ]
        now = time.monotonic()
        available_cloud_options: list[dict[str, Any]] = []
        for option in cloud_options:
            selector = str(option.get("selector") or "")
            unavailable = self._temporarily_unavailable_models.get(selector)
            if unavailable and unavailable[0] <= now:
                self._temporarily_unavailable_models.pop(selector, None)
                unavailable = None
            if unavailable is None:
                available_cloud_options.append(option)
        # Merely installing a local model must not silently replace the user's
        # established Automatic cloud route. A local model is used when it is
        # explicitly selected/defaulted, or when it is the only available route.
        if available_cloud_options:
            options = available_cloud_options
        elif cloud_options:
            # Every cloud endpoint is already known to be dead for the active
            # circuit window. Re-selecting one creates the same doomed request
            # on every Automatic task and wastes latency/rate budget. Do not
            # silently replace the configured cloud route with a local model;
            # fail fast with explicit transient state instead.
            remaining = min(
                state[0] - now
                for option in cloud_options
                if (state := self._temporarily_unavailable_models.get(
                    str(option.get("selector") or "")
                ))
            )
            raise DeepSeekError(
                "所有已配置的云模型路由当前均暂不可用；"
                f"最早约 {max(1, int(remaining))} 秒后重试"
            )
        selected = max(
            options,
            key=lambda option: self._automatic_route_score(option, difficult, prompt),
        )
        return selected["selector"]

    def model_temporarily_unavailable(self, selector: str) -> bool:
        """Return live circuit state without exposing provider error details."""

        normalized = str(selector or "").strip()
        state = self._temporarily_unavailable_models.get(normalized)
        if state and state[0] <= time.monotonic():
            self._temporarily_unavailable_models.pop(normalized, None)
            return False
        return state is not None

    def routing_model(self) -> str:
        return self.select_automatic_model(False)

    async def generate_task_title(
        self, prompt: str, model_selector: str | None = None
    ) -> str:
        """Create a short title with the model that actually runs the task.

        Direct callers retain V4 Flash as the compatibility fallback. The task
        manager passes the concrete ``model_selected`` value, so a configured
        non-DeepSeek model names its own conversation.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "Create a concise chat title from the user's first request. "
                    "If the request is Chinese, return 4-12 Chinese characters; "
                    "otherwise return 3-8 words in the request language. Preserve "
                    "the actual goal, omit politeness, punctuation, quotes, markdown, "
                    "status words, and explanations. Return only the title."
                ),
            },
            {"role": "user", "content": str(prompt or "")[:8_000]},
        ]
        title_model = str(model_selector or "deepseek-v4-flash").strip()
        model_token = self.bind_task_model(title_model)
        output_token = self._task_max_output_tokens.set(96)
        try:
            reply = await self._chat(messages, [], recovery=True)
        finally:
            self._task_max_output_tokens.reset(output_token)
            self.reset_task_model(model_token)
        try:
            return clean_generated_title(
                prompt, str(reply.message.get("content") or "")
            )
        except ValueError as exc:
            raise DeepSeekError(
                f"{title_model} returned an invalid task title: {exc}"
            ) from exc

    async def chat(self, messages: list[dict], tools: list[dict]) -> DeepSeekReply:
        return await self._chat(messages, tools, recovery=False)

    async def chat_recovery(
        self, messages: list[dict], tools: list[dict]
    ) -> DeepSeekReply:
        """Retry a reasoning-only response with hidden thinking disabled."""
        return await self._chat(messages, tools, recovery=True)

    async def classify_task_difficulty(self, prompt: str) -> dict[str, Any]:
        """Use Flash non-thinking mode to route one conversation deterministically."""
        classifier = [
            {
                "role": "system",
                "content": (
                    "Classify the user's entire task for model routing. Return JSON only: "
                    '{"use_pro":true|false,"reason":"short reason"}. Use Pro for difficult '
                    "software construction/debugging, multi-step agent/tool workflows, complex "
                    "research or analysis, long instructions, high uncertainty, or tasks where a "
                    "mistake is costly. Use Flash for simple chat, stable facts, or one-step actions."
                ),
            },
            {"role": "user", "content": prompt[:12_000]},
        ]
        token = self.bind_task_model(self.routing_model())
        output_token = self._task_max_output_tokens.set(512)
        try:
            reply = await self._chat(classifier, [], recovery=True)
        finally:
            self._task_max_output_tokens.reset(output_token)
            self.reset_task_model(token)
        content = str(reply.message.get("content") or "").strip()
        match = re.search(r"\{[\s\S]*\}", content)
        data: dict[str, Any] | None = None
        if match:
            candidate = match.group(0)
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                # Providers occasionally wrap the JSON in markdown or emit a
                # Python-literal dict (single quotes/True). Accept that safe
                # representation instead of turning a successful route into a
                # request failure.
                try:
                    parsed = ast.literal_eval(candidate)
                    if isinstance(parsed, dict):
                        data = parsed
                except (SyntaxError, ValueError):
                    data = None
        if data is None:
            # Last-resort deterministic routing keeps the agent usable when a
            # classifier response is malformed. Complex/long tasks go Pro;
            # short stable prompts stay Flash. This is explicitly surfaced as
            # an automatic-fallback reason in the task event.
            lowered = prompt.casefold()
            complex_markers = (
                "code", "python", "debug", "multi-step", "agent", "browser",
                "math", "proof", "gpqa", "humaneval", "mbpp", "mmlu",
            )
            use_pro = len(prompt) > 1_500 or any(marker in lowered for marker in complex_markers)
            return {"use_pro": use_pro, "reason": "Automatic heuristic fallback after malformed classifier output"}
        return {
            "use_pro": data.get("use_pro") is True,
            "reason": str(data.get("reason") or "AI difficulty judgment")[:300],
        }

    async def _chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool,
        _failed_selectors: set[str] | None = None,
        _failure_history: list[str] | None = None,
    ) -> DeepSeekReply:
        endpoint = self._endpoint_for(self.effective_selector)
        identity_override = self._task_identity_override.get()
        request_messages = (
            [*messages, {"role": "system", "content": identity_override}]
            if identity_override
            else messages
        )
        if not endpoint.keys:
            raise _provider_error(
                endpoint.provider,
                "当前模型没有配置 API 密钥，请在设置中添加供应商、模型调用名和密钥。",
            )
        attempts: list[str] = []
        unavailable = self._temporarily_unavailable_models.get(endpoint.selector)
        if unavailable and unavailable[0] <= time.monotonic():
            self._temporarily_unavailable_models.pop(endpoint.selector, None)
            unavailable = None
        active_index = self._active_indices.get(endpoint.selector, 0)
        candidate_indices = (
            [] if unavailable else list(range(active_index, len(endpoint.keys)))
        )
        if active_index > 0 and not unavailable:
            candidate_indices += list(range(active_index))
        if unavailable:
            attempts.append(
                f"AICodeMirror: model route temporarily unavailable ({unavailable[1]})"
            )
        terminal_route_failure = False
        for index in candidate_indices:
            key = endpoint.keys[index]
            for retry in range(3):
                try:
                    result = (
                        await self._request(
                            endpoint, key, request_messages, tools, recovery=True
                        )
                        if recovery
                        else await self._request(endpoint, key, request_messages, tools)
                    )
                    # A tool-call turn commonly has no visible assistant text.
                    # It is valid as long as the provider returned at least one
                    # tool call. Only retry when both content and tool calls are
                    # absent; otherwise legitimate actions such as sending a
                    # Feishu message would be misreported as an empty response.
                    message = (result.get("choices") or [{}])[0].get("message") or {}
                    has_content = bool(str(message.get("content") or "").strip())
                    has_tool_calls = bool(message.get("tool_calls"))
                    if not has_content and not has_tool_calls:
                        attempts.append(f"{self._label(index, endpoint)}: empty assistant response")
                        if retry < 2:
                            await asyncio.sleep(1.0 * (2**retry))
                            continue
                        # AI Code Mirror exposes both protocols. Its Chat
                        # Completions stream can intermittently complete with
                        # no assistant delta even though the key/model remain
                        # healthy. Switch protocol once after the bounded chat
                        # retries; do not simply repeat the same failing shape.
                        if (
                            endpoint.source == "aicodemirror"
                            and endpoint.provider == "openai"
                        ):
                            result = await self._request_openai_responses(
                                endpoint, key, request_messages, tools
                            )
                            message = (result.get("choices") or [{}])[0].get(
                                "message"
                            ) or {}
                            has_content = bool(
                                str(message.get("content") or "").strip()
                            )
                            has_tool_calls = bool(message.get("tool_calls"))
                            if has_content or has_tool_calls:
                                attempts.append(
                                    "AICodeMirror Responses API fallback succeeded"
                                )
                            else:
                                attempts.append(
                                    "AICodeMirror Responses API: empty assistant response"
                                )
                                break
                        else:
                            break
                    if active_index != index:
                        self._active_indices[endpoint.selector] = index
                        if endpoint.provider == "deepseek":
                            self.active_index = index
                        if self.on_key_change:
                            self.on_key_change(self.active_label)
                    self._temporarily_unavailable_models.pop(endpoint.selector, None)
                    return DeepSeekReply(
                        message=result["choices"][0]["message"],
                        usage=result.get("usage", {}),
                        active_key=self.active_label,
                        finish_reason=result["choices"][0].get("finish_reason"),
                        provider_model=str(result.get("model") or ""),
                    )
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    relay_code = (
                        _aicodemirror_terminal_error(exc.response)
                        if endpoint.source == "aicodemirror"
                        else ""
                    )
                    attempts.append(
                        f"{self._label(index, endpoint)}: HTTP {status}"
                        + (f" ({relay_code})" if relay_code else "")
                    )
                    if relay_code:
                        self._temporarily_unavailable_models[endpoint.selector] = (
                            time.monotonic() + AICODEMIRROR_MODEL_COOLDOWN_SECONDS,
                            relay_code,
                        )
                        terminal_route_failure = True
                        break
                    # Cloudflare uses 524 when the relay accepted the request
                    # but its upstream model did not produce an HTTP response
                    # before the proxy deadline.  AI Code Mirror supports both
                    # OpenAI protocols, so recover immediately through the
                    # Responses endpoint instead of failing the whole Agent
                    # task (or blindly repeating the same slow chat request).
                    if (
                        status == 524
                        and endpoint.source == "aicodemirror"
                        and endpoint.provider == "openai"
                    ):
                        try:
                            result = await self._request_openai_responses(
                                endpoint, key, request_messages, tools
                            )
                            message = (result.get("choices") or [{}])[0].get(
                                "message"
                            ) or {}
                            has_content = bool(
                                str(message.get("content") or "").strip()
                            )
                            has_tool_calls = bool(message.get("tool_calls"))
                            if has_content or has_tool_calls:
                                attempts.append(
                                    "AICodeMirror Responses API recovered HTTP 524"
                                )
                                return DeepSeekReply(
                                    message=message,
                                    usage=result.get("usage", {}),
                                    active_key=self.active_label,
                                    finish_reason=(result.get("choices") or [{}])[
                                        0
                                    ].get("finish_reason"),
                                    provider_model=str(result.get("model") or ""),
                                )
                            attempts.append(
                                "AICodeMirror Responses API: empty assistant response"
                            )
                        except ContextWindowError:
                            raise
                        except httpx.HTTPStatusError as fallback_exc:
                            attempts.append(
                                "AICodeMirror Responses API: HTTP "
                                f"{fallback_exc.response.status_code}"
                            )
                        except httpx.TransportError as fallback_exc:
                            attempts.append(
                                "AICodeMirror Responses API: "
                                f"{type(fallback_exc).__name__}"
                            )
                        except DeepSeekError as fallback_exc:
                            attempts.append(
                                f"AICodeMirror Responses API: {fallback_exc}"
                            )
                        except (
                            AttributeError,
                            IndexError,
                            KeyError,
                            TypeError,
                            ValueError,
                        ) as fallback_exc:
                            attempts.append(
                                "AICodeMirror Responses API: malformed provider response "
                                f"({type(fallback_exc).__name__})"
                            )
                    if status in {401, 402}:
                        break
                    if status in {429, 500, 502, 503, 504, 524} and retry < 2:
                        await asyncio.sleep(1.5 * (2**retry))
                        continue
                    break
                except httpx.TransportError as exc:
                    attempts.append(f"{self._label(index, endpoint)}: {type(exc).__name__}")
                    if retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
                except ContextWindowError:
                    raise
                except (
                    AttributeError,
                    IndexError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as exc:
                    # JSON/SSE bodies are controlled by the remote provider. A
                    # truncated proxy response or malformed event must follow
                    # the bounded retry and cross-provider failover path rather
                    # than terminate the Agent task as a decoder exception.
                    attempts.append(
                        f"{self._label(index, endpoint)}: malformed provider response "
                        f"({type(exc).__name__})"
                    )
                    if retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
                except DeepSeekError as exc:
                    attempts.append(f"{self._label(index, endpoint)}: {exc}")
                    if retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
            if terminal_route_failure:
                break
        failed_selectors = set(_failed_selectors or ())
        failed_selectors.add(endpoint.selector)
        failure_history = list(_failure_history or ())
        failure_history.append(f"{endpoint.selector}: " + "; ".join(attempts))
        failover_callback = self._task_model_failover.get()
        if failover_callback:
            candidates = self._model_failover_candidates(endpoint, failed_selectors)
            if candidates:
                fallback = candidates[0]
                self._task_model.set(fallback.selector)
                self._task_identity_override.set(
                    self._model_failover_identity(
                        fallback, previous_selector=endpoint.selector
                    )
                )
                event = {
                    "from_model": endpoint.selector,
                    "to_model": fallback.selector,
                    "from_provider": endpoint.provider,
                    "to_provider": fallback.provider,
                    "different_provider": fallback.provider != endpoint.provider,
                    "reason": "; ".join(attempts[-4:]),
                }
                try:
                    await _notify_callback(failover_callback, event)
                except Exception:
                    # Reporting is observational; a UI/event-store problem
                    # must not prevent the provider recovery itself.
                    logger.debug("Model failover callback failed", exc_info=True)
                sanitized_messages = self._strip_provider_private_state(
                    messages, strip_reasoning_content=True
                )
                # The engine owns this history list across later tool turns.
                # Persist the selector-bound cleanup there; passing only a
                # sanitized recursive copy would let the old model's opaque
                # state reappear on the very next Agent step.
                messages[:] = sanitized_messages
                return await self._chat(
                    messages,
                    tools,
                    recovery=recovery,
                    _failed_selectors=failed_selectors,
                    _failure_history=failure_history,
                )
        # Preserve the complete bounded route history.  Without this, a task
        # that failed over OpenAI -> Anthropic -> Google surfaced only the last
        # provider's error, hiding the actual recovery path and making a
        # configuration problem look provider-specific.
        history = failure_history[-8:]
        raise _provider_error(
            endpoint.provider,
            "所有已配置的故障转移路由均不可用：" + " | ".join(history),
        )

    def _label(self, index: int, endpoint: ProviderEndpoint | None = None) -> str:
        if endpoint and endpoint.source == "aicodemirror":
            return "AICodeMirror"
        if endpoint and endpoint.provider != "deepseek":
            return endpoint.provider.upper()
        return "主密钥" if index == 0 else "备用密钥"

    async def native_web_search(self, query: str, max_uses: int = 5) -> dict[str, Any]:
        """Use DeepSeek's Anthropic-compatible, server-side Web Search tool."""
        if not self.keys:
            raise DeepSeekError("DeepSeek API key is not configured")
        attempts: list[str] = []
        candidate_indices = list(range(self.active_index, len(self.keys)))
        if self.active_index > 0:
            candidate_indices += list(range(self.active_index))
        for index in candidate_indices:
            for retry in range(3):
                try:
                    result = await self._web_search_request(
                        self.keys[index], query, max_uses
                    )
                    if self.active_index != index:
                        self.active_index = index
                        if self.on_key_change:
                            self.on_key_change(self.active_label)
                    return self._normalize_web_search(result)
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    attempts.append(f"{self._label(index)}: HTTP {status}")
                    if status in {401, 402}:
                        break
                    if status in {429, 500, 502, 503, 504} and retry < 2:
                        await asyncio.sleep(1.5 * (2**retry))
                        continue
                    break
                except httpx.TransportError as exc:
                    attempts.append(f"{self._label(index)}: {type(exc).__name__}")
                    if retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
        raise DeepSeekError(
            "DeepSeek native web search failed after primary/backup attempts: "
            + "; ".join(attempts)
        )

    async def provider_web_search(
        self, query: str, max_uses: int = 5
    ) -> dict[str, Any]:
        """Use the currently selected model provider's native search tool.

        This deliberately resolves the task-local selector, so a send-bar model
        override and Automatic's chosen route use the same provider for both the
        answer and its search.  Credentials are sent only to that endpoint:
        direct keys remain on the official vendor host while AI Code Mirror keys
        remain on the configured relay host.
        """
        endpoint = self._endpoint_for(self.effective_selector)
        if endpoint.provider == "deepseek":
            result = await self.native_web_search(query, max_uses)
            result.update(
                {
                    "model": endpoint.model,
                    "provider": "deepseek",
                    "route": "direct",
                }
            )
            return result
        if endpoint.source == "custom" or endpoint.provider not in {"openai", "anthropic", "google"}:
            raise DeepSeekError(
                f"{endpoint.provider} does not expose a configured native web-search adapter"
            )
        if not endpoint.keys:
            raise DeepSeekError(f"{endpoint.provider} API key is not configured")

        request = {
            "openai": self._openai_web_search_request,
            "anthropic": self._anthropic_web_search_request,
            "google": self._google_web_search_request,
        }[endpoint.provider]
        attempts: list[str] = []
        active_index = self._active_indices.get(endpoint.selector, 0)
        candidate_indices = list(range(active_index, len(endpoint.keys)))
        if active_index > 0:
            candidate_indices += list(range(active_index))
        for index in candidate_indices:
            key = endpoint.keys[index]
            for retry in range(3):
                try:
                    result = await request(endpoint, key, query, max_uses)
                    if endpoint.provider in {"openai", "google"} and not (
                        result.get("sources") or result.get("searches")
                    ):
                        raise DeepSeekError(
                            f"{endpoint.provider} returned a response but did not execute "
                            "a grounded web search"
                        )
                    self._active_indices[endpoint.selector] = index
                    result.update(
                        {
                            "model": endpoint.model,
                            "provider": endpoint.provider,
                            "route": (
                                "aicodemirror"
                                if endpoint.source == "aicodemirror"
                                else "direct"
                            ),
                        }
                    )
                    return result
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    attempts.append(f"{self._label(index, endpoint)}: HTTP {status}")
                    if status in {401, 402, 403, 404, 405, 422}:
                        break
                    if status in {408, 409, 425, 429, 500, 502, 503, 504} and retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
                except httpx.TimeoutException:
                    # A relay can accept the native-search payload but never
                    # finish its hosted tool. Do not repeat the same long wait;
                    # return control to the Agent so it can use the isolated
                    # browser or a bounded local crawler instead.
                    attempts.append(
                        f"{self._label(index, endpoint)}: native search timed out "
                        f"after {PROVIDER_WEB_SEARCH_TIMEOUT_SECONDS:g}s"
                    )
                    break
                except httpx.TransportError as exc:
                    attempts.append(
                        f"{self._label(index, endpoint)}: {type(exc).__name__}"
                    )
                    if retry < 2:
                        await asyncio.sleep(1.0 * (2**retry))
                        continue
                    break
        route = "AI Code Mirror" if endpoint.source == "aicodemirror" else "official"
        raise DeepSeekError(
            f"{endpoint.provider} native web search via {route} failed: "
            + "; ".join(attempts)
        )

    async def _openai_web_search_request(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        query: str,
        max_uses: int,
    ) -> dict[str, Any]:
        # The Responses API lets the model decide whether search is useful.
        # ``max_uses`` is retained in host metadata because OpenAI's web_search
        # tool does not currently expose a per-request search-count parameter.
        payload = {
            "model": endpoint.model,
            "input": query,
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=min(float(self.timeout), PROVIDER_WEB_SEARCH_TIMEOUT_SECONDS)
        ) as client:
            response = await client.post(
                f"{endpoint.base_url}/responses", headers=headers, json=payload
            )
            response.raise_for_status()
            result = response.json()
        normalized = self._normalize_openai_web_search(result)
        normalized["requested_max_uses"] = max(1, min(int(max_uses), 10))
        return normalized

    async def _anthropic_web_search_request(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        query: str,
        max_uses: int,
    ) -> dict[str, Any]:
        payload = {
            "model": endpoint.model,
            "max_tokens": ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max(1, min(int(max_uses), 10)),
                }
            ],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=min(float(self.timeout), PROVIDER_WEB_SEARCH_TIMEOUT_SECONDS)
        ) as client:
            response = await client.post(
                f"{endpoint.base_url}/messages", headers=headers, json=payload
            )
            response.raise_for_status()
            result = response.json()
            # Server-side tools may pause while their loop is still active.
            # Anthropic requires replaying the assistant blocks unchanged.
            for _ in range(3):
                if result.get("stop_reason") != "pause_turn":
                    break
                payload["messages"] = [
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": result.get("content", [])},
                ]
                response = await client.post(
                    f"{endpoint.base_url}/messages", headers=headers, json=payload
                )
                response.raise_for_status()
                result = response.json()
        return self._normalize_web_search(result)

    @staticmethod
    def _google_native_generate_url(endpoint: ProviderEndpoint) -> str:
        model = quote(endpoint.model, safe="-_.")
        if endpoint.source == "aicodemirror":
            root = endpoint.base_url.rstrip("/")
            return f"{root}/v1beta/models/{model}:generateContent"
        # Official provider entries use Google's OpenAI-compat base for normal
        # chat. Native grounding lives one level above its trailing /openai.
        root = endpoint.base_url.rstrip("/").removesuffix("/openai")
        return f"{root}/models/{model}:generateContent"

    async def _google_web_search_request(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        query: str,
        max_uses: int,
    ) -> dict[str, Any]:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
        }
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            timeout=min(float(self.timeout), PROVIDER_WEB_SEARCH_TIMEOUT_SECONDS)
        ) as client:
            response = await client.post(
                self._google_native_generate_url(endpoint),
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
        normalized = self._normalize_google_web_search(result)
        normalized["requested_max_uses"] = max(1, min(int(max_uses), 10))
        return normalized

    @staticmethod
    def _normalize_openai_web_search(result: dict[str, Any]) -> dict[str, Any]:
        texts: list[str] = []
        sources: list[dict[str, str]] = []
        searches: list[dict[str, Any]] = []
        for item in result.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                searches.append(item)
                for source in (item.get("action") or {}).get("sources") or []:
                    if not isinstance(source, dict):
                        continue
                    url = str(source.get("url") or "")
                    if url:
                        sources.append(
                            {"title": str(source.get("title") or url), "url": url}
                        )
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                text = str(block.get("text") or block.get("output_text") or "")
                if text:
                    texts.append(text)
                for annotation in block.get("annotations") or []:
                    if not isinstance(annotation, dict):
                        continue
                    citation = annotation.get("url_citation") or annotation
                    url = str(citation.get("url") or "")
                    if url:
                        sources.append(
                            {"title": str(citation.get("title") or url), "url": url}
                        )
        if not texts and result.get("output_text"):
            texts.append(str(result["output_text"]))
        unique_sources = list({item["url"]: item for item in sources}.values())
        return {
            "answer": "\n\n".join(texts),
            "sources": unique_sources,
            "searches": searches,
            "stop_reason": result.get("status"),
            "usage": result.get("usage") or {},
        }

    @staticmethod
    def _normalize_google_web_search(result: dict[str, Any]) -> dict[str, Any]:
        candidate = (result.get("candidates") or [{}])[0]
        texts = [
            str(part.get("text"))
            for part in ((candidate.get("content") or {}).get("parts") or [])
            if isinstance(part, dict) and part.get("text")
        ]
        metadata = candidate.get("groundingMetadata") or {}
        sources: list[dict[str, str]] = []
        for chunk in metadata.get("groundingChunks") or []:
            if not isinstance(chunk, dict):
                continue
            web = chunk.get("web") or {}
            url = str(web.get("uri") or web.get("url") or "")
            if url:
                sources.append({"title": str(web.get("title") or url), "url": url})
        searches = [
            {"query": str(query)}
            for query in metadata.get("webSearchQueries") or []
            if query
        ]
        return {
            "answer": "\n\n".join(texts),
            "sources": list({item["url"]: item for item in sources}.values()),
            "searches": searches,
            "stop_reason": candidate.get("finishReason"),
            "usage": result.get("usageMetadata") or {},
        }

    async def _web_search_request(self, key: str, query: str, max_uses: int) -> dict:
        root = self.base_url.removesuffix("/v1")
        if not root.endswith("/anthropic"):
            root += "/anthropic"
        payload = {
            "model": self.effective_model,
            "max_tokens": 8192,
            "temperature": 0,
            "thinking": {"type": "enabled", "budget_tokens": 4096},
            "messages": [{"role": "user", "content": query}],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max(1, min(max_uses, 10)),
                }
            ],
        }
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{root}/v1/messages", headers=headers, json=payload
            )
            response.raise_for_status()
            result = response.json()
        for _ in range(3):
            if result.get("stop_reason") != "pause_turn":
                break
            payload["messages"] = [
                {"role": "user", "content": query},
                {"role": "assistant", "content": result.get("content", [])},
            ]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{root}/v1/messages", headers=headers, json=payload
                )
                response.raise_for_status()
                result = response.json()
        return result

    @staticmethod
    def _normalize_web_search(result: dict[str, Any]) -> dict[str, Any]:
        texts: list[str] = []
        sources: list[dict[str, str]] = []
        searches: list[dict[str, Any]] = []
        for block in result.get("content", []):
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                texts.append(str(block["text"]))
                for citation in block.get("citations") or []:
                    url = str(citation.get("url") or "")
                    if url:
                        sources.append(
                            {"title": str(citation.get("title") or url), "url": url}
                        )
            elif kind == "server_tool_use":
                searches.append(block)
            elif kind == "web_search_tool_result":
                for item in block.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "")
                    if url:
                        sources.append(
                            {"title": str(item.get("title") or url), "url": url}
                        )
        unique_sources = list({item["url"]: item for item in sources}.values())
        return {
            "answer": "\n\n".join(texts),
            "sources": unique_sources,
            "searches": searches,
            "stop_reason": result.get("stop_reason"),
            "usage": result.get("usage", {}),
            "provider": "deepseek-native-web-search",
        }

    @staticmethod
    def _openai_responses_payload(
        endpoint: ProviderEndpoint,
        messages: list[dict],
        tools: list[dict],
        max_output_tokens: int | None,
        reasoning_effort: str | None = "high",
    ) -> dict[str, Any]:
        """Translate the Agent chat transcript to OpenAI's Responses shape.

        AI Code Mirror documents both Chat Completions and Responses.  The
        latter is the primary relay protocol and is required for GPT-6 Astra
        tool calling, including direct-provider routes. Tool calls and outputs must be
        preserved so a fallback in the middle of an Agent run can continue the
        same tool loop instead of restarting it.
        """

        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []

        def content_text(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("input_text") or item.get(
                            "output_text"
                        )
                        if text:
                            parts.append(str(text))
                    elif item is not None:
                        parts.append(str(item))
                return "\n".join(parts)
            return "" if value is None else str(value)

        for message in messages:
            role = str(message.get("role") or "user")
            text = content_text(message.get("content"))
            if role in {"system", "developer"}:
                if text:
                    instructions.append(text)
                continue
            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": text,
                    }
                )
                continue
            normalized_role = "assistant" if role == "assistant" else "user"
            if normalized_role == "assistant":
                # When history is managed manually, the Responses API requires
                # every reasoning output item to be replayed before the
                # corresponding function call/output continuation.
                for item in message.get("_openai_reasoning_items") or []:
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        input_items.append(dict(item))
            content: Any = text
            raw_content = message.get("content")
            if normalized_role == "user" and isinstance(raw_content, list) and any(
                isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
                for part in raw_content
            ):
                # Preserve multimodal input through Chat -> Responses fallback.
                # Flattening everything to text silently made the model blind.
                content = []
                for part in raw_content:
                    if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                        image = {"type": "input_image"}
                        if part["type"] == "image_url":
                            source = part.get("image_url")
                            source = {"url": source} if isinstance(source, str) else source
                            if not isinstance(source, dict) or not isinstance(source.get("url"), str) or not source["url"].strip():
                                raise ValueError("Invalid image input for Responses")
                            image["image_url"] = source["url"]
                            detail = source.get("detail")
                        else:
                            for field in ("image_url", "file_id"):
                                if part.get(field) is not None:
                                    if not isinstance(part[field], str) or not part[field].strip():
                                        raise ValueError("Invalid image input for Responses")
                                    image[field] = part[field]
                            if ("image_url" in image) == ("file_id" in image):
                                raise ValueError("Image input requires one URL or file ID")
                            detail = part.get("detail")
                        if detail is not None:
                            if detail not in ("auto", "low", "high", "original"):
                                raise ValueError("Invalid image detail for Responses")
                            image["detail"] = detail
                        content.append(image)
                    else:
                        part_text = content_text([part])
                        if part_text:
                            content.append({"type": "input_text", "text": part_text})
            if content:
                input_items.append({"role": normalized_role, "content": content})
            if normalized_role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    if not function.get("name"):
                        continue
                    call_id = str(call.get("id") or "")
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": str(function["name"]),
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )

        if not input_items:
            input_items.append({"role": "user", "content": "Continue."})
        response_tools: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") or {}
            if not function.get("name"):
                continue
            response_tools.append(
                {
                    "type": "function",
                    "name": str(function["name"]),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "input": input_items,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        # Elren manages Responses history manually, so request opaque reasoning
        # state even when the user leaves effort on the model default.
        payload["include"] = ["reasoning.encrypted_content"]
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    @staticmethod
    def _normalize_openai_response(
        result: dict[str, Any], model: str
    ) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        reasoning_items: list[dict[str, Any]] = []
        for index, item in enumerate(result.get("output") or []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "reasoning":
                # Preserve the provider item, including opaque encrypted state,
                # without exposing it as assistant-visible reasoning text.
                reasoning_items.append(
                    {
                        key: item[key]
                        for key in (
                            "type",
                            "id",
                            "summary",
                            "content",
                            "encrypted_content",
                            "status",
                        )
                        if key in item
                    }
                )
            elif kind == "message":
                for block in item.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text") or block.get("output_text")
                    if text:
                        text_parts.append(str(text))
                    elif block.get("refusal"):
                        text_parts.append(str(block["refusal"]))
            elif kind == "function_call" and item.get("name"):
                tool_calls.append(
                    {
                        "id": str(
                            item.get("call_id")
                            or item.get("id")
                            or f"call_response_{index}"
                        ),
                        "type": "function",
                        "function": {
                            "name": str(item["name"]),
                            "arguments": str(item.get("arguments") or "{}"),
                        },
                    }
                )
        if not text_parts and result.get("output_text"):
            text_parts.append(str(result["output_text"]))
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning_items:
            message["_openai_reasoning_items"] = reasoning_items
        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else (
                        "stop" if result.get("status") == "completed" else result.get("status")
                    ),
                }
            ],
            "usage": {
                **usage,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": int(
                    usage.get("total_tokens") or prompt_tokens + completion_tokens
                ),
            },
            "model": str(result.get("model") or model),
        }

    async def _request_openai_responses(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool = False,
    ) -> dict[str, Any]:
        max_tokens = self._task_max_output_tokens.get()
        if max_tokens is None:
            max_tokens = self.max_output_tokens
        payload = self._openai_responses_payload(
            endpoint,
            messages,
            tools,
            max_tokens,
            self._openai_reasoning_effort(
                endpoint=endpoint, recovery=recovery
            ),
        )
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{endpoint.base_url}/responses", headers=headers, json=payload
            )
            if response.is_error and _looks_like_context_window_error(
                response.status_code, response.text[:2000]
            ):
                raise ContextWindowError(f"{endpoint.provider} context window exceeded")
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("output"), list):
            raise _provider_error(
                endpoint.provider, "OpenAI Responses API returned an invalid response"
            )
        return self._normalize_openai_response(result, endpoint.model)

    async def _request(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool = False,
    ) -> dict:
        # AI Code Mirror documents Responses as its advanced OpenAI protocol.
        # Use it as the primary path for GPT reasoning and tool turns instead
        # of first waiting on an intermittently empty Chat Completions stream.
        # Astra does not support tool calling through Chat Completions. Keep
        # all its turns on Responses so continuations retain reasoning state.
        if endpoint.provider == "openai" and (
            endpoint.source == "aicodemirror" or (endpoint.source != "custom" and endpoint.model == "gpt-6-astra")
        ):
            return await self._request_openai_responses(
                endpoint, key, messages, tools, recovery=recovery
            )
        if endpoint.provider == "anthropic":
            return await self._request_anthropic(
                endpoint, key, messages, tools, recovery=recovery
            )
        if endpoint.provider == "google" and endpoint.source == "aicodemirror":
            return await self._request_gemini(
                endpoint, key, messages, tools, recovery=recovery
            )
        payload = self._chat_payload(messages, tools, recovery=recovery, endpoint=endpoint)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        accumulator = _StreamAccumulator(retain_final_reasoning=False)
        started = time.monotonic()
        last_progress = started
        callback = self._task_stream_progress.get()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=endpoint.provider != "local" and endpoint.source != "custom",
            ) as client, client.stream(
                "POST",
                f"{endpoint.base_url}/chat/completions", headers=headers, json=payload
            ) as response:
                if response.is_error:
                    raw_error = await response.aread()
                    detail = raw_error.decode("utf-8", errors="replace")[:2000]
                    if _looks_like_context_window_error(response.status_code, detail):
                        raise ContextWindowError(
                            f"{endpoint.provider} context window exceeded"
                        )
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "text/event-stream" not in content_type:
                    raw = await response.aread()
                    result = json.loads(raw)
                    if not isinstance(result, dict) or not result.get("choices"):
                        raise DeepSeekError(
                            f"{endpoint.provider} returned an invalid chat response"
                        )
                    if endpoint.provider == "google":
                        self._tag_google_compatibility_state(
                            result, endpoint.selector
                        )
                    return result
                async for raw_line in response.aiter_lines():
                    data = _sse_data_payload(raw_line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break
                    accumulator.feed(json.loads(data))
                    now = time.monotonic()
                    if callback and now - last_progress >= 10:
                        try:
                            await _notify_callback(callback, accumulator.progress(now - started))
                        except Exception:
                            # Progress reporting is observational and must never
                            # turn a valid provider response into a failed task.
                            callback = None
                        last_progress = now
            if callback:
                try:
                    await _notify_callback(callback, accumulator.progress(time.monotonic() - started, final=True))
                except Exception:
                    callback = None
            result = accumulator.result()
            if endpoint.provider == "google":
                self._tag_google_compatibility_state(result, endpoint.selector)
            return result
        finally:
            accumulator.close()

    async def _request_gemini(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool = False,
    ) -> dict[str, Any]:
        """Call AICodeMirror's native Gemini generateContent endpoint."""
        payload = self._gemini_payload(
            messages, tools, endpoint=endpoint, recovery=recovery
        )
        headers = {
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        }
        model = quote(endpoint.model, safe="-_.")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{endpoint.base_url}/v1beta/models/{model}:generateContent",
                headers=headers,
                json=payload,
            )
            if response.is_error:
                detail = response.text[:2000]
                if _looks_like_context_window_error(response.status_code, detail):
                    raise ContextWindowError("Gemini context window exceeded")
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or not result.get("candidates"):
            raise DeepSeekError("Gemini returned an invalid generateContent response")
        return self._normalize_gemini_response(result, endpoint.model)

    def _gemini_payload(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        endpoint: ProviderEndpoint | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        endpoint = endpoint or self._endpoint_for(self.effective_selector)
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        tool_names: dict[str, tuple[str, str]] = {}

        def text_from(content: Any) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    str(item.get("text") or "") if isinstance(item, dict) else str(item)
                    for item in content
                ]
                return "\n".join(part for part in texts if part)
            return "" if content is None else str(content)

        for message in messages:
            role = str(message.get("role") or "user")
            if role in {"system", "developer"}:
                text = text_from(message.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "")
                name, provider_call_id = tool_names.get(
                    tool_call_id, ("tool", "")
                )
                raw_result = message.get("content")
                try:
                    parsed_result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
                except json.JSONDecodeError:
                    parsed_result = {"result": str(raw_result or "")}
                if not isinstance(parsed_result, dict):
                    parsed_result = {"result": parsed_result}
                function_response: dict[str, Any] = {
                    "name": name,
                    "response": parsed_result,
                }
                if provider_call_id:
                    function_response["id"] = provider_call_id
                response_part = {"functionResponse": function_response}
                if (
                    contents
                    and contents[-1].get("role") == "user"
                    and contents[-1].get("parts")
                    and all(
                        isinstance(part, dict) and "functionResponse" in part
                        for part in contents[-1]["parts"]
                    )
                ):
                    # Parallel Gemini calls return their function responses as
                    # multiple Parts in one following user Content.
                    contents[-1]["parts"].append(response_part)
                else:
                    contents.append({"role": "user", "parts": [response_part]})
                continue

            target_role = "model" if role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            provider_parts = message.get("_gemini_model_parts")
            if target_role == "model" and isinstance(provider_parts, list):
                # Gemini validates opaque thought signatures on the exact Part
                # that originally carried them. Replaying the complete model
                # Part list also preserves mixed text/function-call ordering.
                parts = copy.deepcopy(
                    [part for part in provider_parts if isinstance(part, dict)]
                )
                normalized_calls = [
                    call
                    for call in message.get("tool_calls") or []
                    if isinstance(call, dict)
                ]
                call_index = 0
                for part in parts:
                    function_call = part.get("functionCall") or {}
                    if not isinstance(function_call, dict) or not function_call.get(
                        "name"
                    ):
                        continue
                    call = (
                        normalized_calls[call_index]
                        if call_index < len(normalized_calls)
                        else {}
                    )
                    call_id = str(call.get("id") or "")
                    if call_id:
                        tool_names[call_id] = (
                            str(function_call["name"]),
                            str(function_call.get("id") or ""),
                        )
                    call_index += 1
            else:
                text = text_from(message.get("content"))
                if text:
                    parts.append({"text": text})
            if target_role == "model" and not isinstance(provider_parts, list):
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "tool")
                    call_id = str(call.get("id") or "")
                    if call_id:
                        tool_names[call_id] = (name, "")
                    raw_arguments = function.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw_arguments)
                    except (TypeError, json.JSONDecodeError):
                        arguments = {"_raw_arguments": str(raw_arguments)}
                    if not isinstance(arguments, dict):
                        arguments = {"value": arguments}
                    function_part: dict[str, Any] = {
                        "functionCall": {"name": name, "args": arguments}
                    }
                    provider_state = call.get("provider_state") or {}
                    if isinstance(provider_state, dict) and provider_state.get(
                        "thought_signature"
                    ):
                        # Gemini 3 validates this opaque signature on the next
                        # function-response turn. It must stay on the exact
                        # functionCall part where the provider returned it.
                        function_part["thoughtSignature"] = str(
                            provider_state["thought_signature"]
                        )
                    parts.append(function_part)
            if parts:
                contents.append({"role": target_role, "parts": parts})

        if not contents:
            contents.append({"role": "user", "parts": [{"text": "Continue."}]})
        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        declarations = []
        for tool in tools:
            function = tool.get("function") or {}
            if not function.get("name"):
                continue
            declarations.append(
                {
                    "name": str(function["name"]),
                    "description": str(function.get("description") or ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        if declarations:
            payload["tools"] = [{"functionDeclarations": declarations}]
        max_tokens = self._task_max_output_tokens.get()
        if max_tokens is None:
            max_tokens = self.max_output_tokens
        generation_config: dict[str, Any] = {}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        effort = self._effective_reasoning_effort(
            endpoint=endpoint, recovery=recovery
        )
        if effort:
            generation_config["thinkingConfig"] = {"thinkingLevel": effort}
        if generation_config:
            payload["generationConfig"] = generation_config
        _ = endpoint
        return payload

    @staticmethod
    def _normalize_gemini_response(
        result: dict[str, Any], model: str
    ) -> dict[str, Any]:
        candidate = (result.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            if part.get("text"):
                text_parts.append(str(part["text"]))
            function_call = part.get("functionCall") or {}
            if function_call.get("name"):
                tool_call: dict[str, Any] = {
                        "id": str(function_call.get("id") or f"call_gemini_{index}"),
                        "type": "function",
                        "function": {
                            "name": str(function_call["name"]),
                            "arguments": json.dumps(
                                function_call.get("args") or {}, ensure_ascii=False
                            ),
                        },
                    }
                tool_calls.append(tool_call)
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        if tool_calls or any(part.get("thoughtSignature") for part in parts):
            # Preserve the complete native model turn. A thoughtSignature is
            # opaque and must be sent back unmodified in the same Part/order;
            # reconstructing from normalized text/tool_calls is not equivalent.
            message["_gemini_model_parts"] = copy.deepcopy(parts)
        reason = str(candidate.get("finishReason") or "")
        finish_reason = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
        }.get(reason, reason.casefold() or None)
        if tool_calls:
            finish_reason = "tool_calls"
        usage = result.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        completion_tokens = int(usage.get("candidatesTokenCount") or 0)
        return {
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": int(usage.get("totalTokenCount") or prompt_tokens + completion_tokens),
                "completion_tokens_details": {
                    "reasoning_tokens": int(usage.get("thoughtsTokenCount") or 0)
                },
            },
            "model": model,
        }

    async def _request_anthropic(
        self,
        endpoint: ProviderEndpoint,
        key: str,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool = False,
    ) -> dict[str, Any]:
        """Call Anthropic's production Messages API instead of its compatibility shim."""
        payload = self._anthropic_payload(
            messages, tools, endpoint=endpoint, recovery=recovery
        )
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        accumulator = _AnthropicStreamAccumulator()
        started = time.monotonic()
        last_progress = started
        callback = self._task_stream_progress.get()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=endpoint.source != "custom") as client, client.stream(
                "POST",
                f"{endpoint.base_url}/messages",
                headers=headers,
                json=payload,
            ) as response:
                if response.is_error:
                    raw_error = await response.aread()
                    detail = raw_error.decode("utf-8", errors="replace")[:2000]
                    if _looks_like_context_window_error(response.status_code, detail):
                        raise ContextWindowError("Anthropic context window exceeded")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "text/event-stream" not in content_type:
                    raw = await response.aread()
                    result = json.loads(raw)
                    if not isinstance(result, dict) or not result.get("content"):
                        raise DeepSeekError("Anthropic returned an invalid Messages response")
                    return self._normalize_anthropic_message(result)
                async for raw_line in response.aiter_lines():
                    data = _sse_data_payload(raw_line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("type") == "error":
                        error = event.get("error") or {}
                        detail = json.dumps(error, ensure_ascii=False, default=str)
                        if _looks_like_context_window_error(400, detail):
                            raise ContextWindowError("Anthropic context window exceeded")
                        raise DeepSeekError(
                            f"Anthropic stream error: {error.get('type', 'unknown')}"
                        )
                    accumulator.feed(event)
                    now = time.monotonic()
                    if callback and now - last_progress >= 10:
                        try:
                            await _notify_callback(callback, accumulator.progress(now - started))
                        except Exception:
                            callback = None
                        last_progress = now
            if callback:
                try:
                    await _notify_callback(callback,
                        accumulator.progress(time.monotonic() - started, final=True)
                    )
                except Exception:
                    callback = None
            return accumulator.result()
        finally:
            # The Anthropic accumulator retains only bounded response metadata
            # and final content, so it has no temporary resources to close.
            pass

    def _anthropic_payload(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        endpoint: ProviderEndpoint | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        endpoint = endpoint or self._endpoint_for(self.effective_selector)
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        def append_turn(role: str, blocks: list[dict[str, Any]]) -> None:
            if not blocks:
                return
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})

        for message in messages:
            role = str(message.get("role") or "user")
            if role in {"system", "developer"}:
                content = message.get("content")
                if content:
                    system_parts.append(str(content))
                continue
            if role == "tool":
                append_turn(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                )
                continue

            target_role = "assistant" if role == "assistant" else "user"
            blocks: list[dict[str, Any]] = []
            if target_role == "assistant":
                for thinking_block in message.get(
                    "_anthropic_thinking_blocks"
                ) or []:
                    if (
                        isinstance(thinking_block, dict)
                        and thinking_block.get("type")
                        in {"thinking", "redacted_thinking"}
                    ):
                        # Anthropic verifies signed thinking blocks during a
                        # tool-use loop. Replay each complete block unchanged.
                        blocks.append(dict(thinking_block))
            blocks.extend(self._anthropic_content_blocks(message.get("content")))
            if target_role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    try:
                        tool_input = json.loads(arguments)
                    except (TypeError, json.JSONDecodeError):
                        tool_input = {"_raw_arguments": str(arguments)}
                    if not isinstance(tool_input, dict):
                        tool_input = {"value": tool_input}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "input": tool_input,
                        }
                    )
            append_turn(target_role, blocks)

        if not converted:
            converted.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})

        max_tokens = self._task_max_output_tokens.get()
        if max_tokens is None:
            max_tokens = self.max_output_tokens
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": converted,
            "max_tokens": max_tokens or ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        native_tools = self._anthropic_tools(tools)
        if native_tools:
            payload["tools"] = native_tools
        effort = self._effective_reasoning_effort(
            endpoint=endpoint, recovery=recovery
        )
        if endpoint.source == "custom":
            # Do not infer thinking modes from a relay's arbitrary model alias.
            if effort:
                payload["output_config"] = {"effort": effort}
            return payload
        model_name = endpoint.model.casefold()
        adaptive_model = bool(
            re.search(
                r"claude-(?:fable|mythos|opus|sonnet)-(?:5(?:$|-)|4-(?:6|7|8)(?:$|-))",
                model_name,
            )
        )
        capability = reasoning_capability(endpoint.provider, endpoint.model)
        if adaptive_model:
            payload["thinking"] = {"type": "adaptive"}
            if effort and capability.control == "anthropic_output_config_effort":
                payload["output_config"] = {"effort": effort}
        elif any(
            marker in model_name
            for marker in (
                "claude-haiku-4-5",
                "claude-sonnet-4-5",
                "claude-opus-4-5",
            )
        ):
            budget = {
                None: 4_096,
                "low": 1_024,
                "medium": 4_096,
                "high": 8_192,
                "xhigh": 16_384,
                "max": 24_576,
            }[effort]
            payload["max_tokens"] = max(int(payload["max_tokens"]), budget + 4_096)
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if (
                "claude-opus-4-5" in model_name
                and effort
                and capability.control == "anthropic_output_config_effort"
            ):
                payload["output_config"] = {"effort": effort}
        return payload

    @staticmethod
    def _anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
        if content is None or content == "":
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]
        blocks: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                blocks.append({"type": "text", "text": str(item)})
                continue
            if item.get("type") == "text":
                blocks.append({"type": "text", "text": str(item.get("text") or "")})
                continue
            if item.get("type") == "image_url":
                image = item.get("image_url") or {}
                url = str(image.get("url") or "")
                match = re.fullmatch(r"data:([^;,]+);base64,(.+)", url, re.DOTALL)
                if match:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": match.group(1),
                                "data": match.group(2),
                            },
                        }
                    )
                elif url:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "url", "url": url},
                        }
                    )
        return blocks

    @staticmethod
    def _anthropic_tools(tools: list[dict]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") or {}
            name = str(function.get("name") or "")
            if not name:
                continue
            converted.append(
                {
                    "name": name,
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        return converted

    @staticmethod
    def _normalize_anthropic_message(result: dict[str, Any]) -> dict[str, Any]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        thinking_blocks: list[dict[str, Any]] = []
        for block in result.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(str(block.get("text") or ""))
            elif kind in {"thinking", "redacted_thinking"}:
                thinking_blocks.append(dict(block))
                reasoning_parts.append(str(block.get("thinking") or ""))
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(
                                block.get("input") or {}, ensure_ascii=False
                            ),
                        },
                    }
                )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if reasoning_parts and tool_calls:
            message["reasoning_content"] = "".join(reasoning_parts)
        if thinking_blocks:
            message["_anthropic_thinking_blocks"] = thinking_blocks
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        stop_reason = str(result.get("stop_reason") or "")
        return {
            "choices": [
                {
                    "message": message,
                    "finish_reason": _AnthropicStreamAccumulator._STOP_REASONS.get(
                        stop_reason, stop_reason or None
                    ),
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "model": str(result.get("model") or ""),
        }

    def _chat_payload(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        recovery: bool = False,
        endpoint: ProviderEndpoint | None = None,
    ) -> dict[str, Any]:
        endpoint = endpoint or self._endpoint_for(self.effective_selector)
        payload = {
            "model": endpoint.model,
            "messages": self._strip_provider_private_state(
                messages,
                # DeepSeek uses reasoning_content to continue its own tool
                # loop. It is not a standard Chat Completions field, so never
                # forward it to another compatible provider.
                strip_reasoning_content=endpoint.provider != "deepseek",
                allowed_google_selector=(
                    endpoint.selector
                    if endpoint.provider == "google"
                    and endpoint.source != "aicodemirror"
                    else None
                ),
            ),
            "tools": tools,
            "stream": True,
        }
        max_tokens = self._task_max_output_tokens.get()
        if max_tokens is None:
            max_tokens = self.max_output_tokens
        if endpoint.provider == "deepseek":
            payload["stream_options"] = {"include_usage": True}
            payload["temperature"] = 0
            requested_effort = self._effective_reasoning_effort(
                endpoint=endpoint
            ) or "high"
            payload["thinking"] = {"type": "disabled" if recovery else "enabled"}
            if not recovery:
                # DeepSeek V4 expects effort at the top level, not inside ``thinking``.
                payload["reasoning_effort"] = requested_effort
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            else:
                payload["max_tokens"] = DEEPSEEK_V4_MAX_OUTPUT_TOKENS
        elif max_tokens is not None:
            # Provider limits vary and users enter arbitrary model IDs. In
            # automatic mode omit a guessed cap; only forward an explicit cap.
            payload["max_tokens"] = max_tokens
        effort = self._effective_reasoning_effort(
            endpoint=endpoint, recovery=recovery
        )
        if endpoint.provider == "openai" and effort:
            openai_effort = self._openai_reasoning_effort(
                endpoint=endpoint, recovery=recovery
            )
            if openai_effort:
                payload["reasoning_effort"] = openai_effort
        elif effort and endpoint.provider in {"google", "xai", "local"}:
            payload["reasoning_effort"] = effort
        if not tools or (
            endpoint.provider == "local" and "tools" not in endpoint.capabilities
        ):
            # Some OpenAI-compatible endpoints reject an empty tools array.
            # A local catalog that did not explicitly advertise tool calling
            # must also stay text-only; sending schemas based on a model-name
            # guess makes conservative local routes fail before answering.
            payload.pop("tools", None)
        return payload

    @staticmethod
    def _tag_google_compatibility_state(
        result: dict[str, Any], selector: str
    ) -> None:
        """Bind Google's opaque Chat-compat signature to its producing model."""

        for choice in result.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                extra_content = call.get("extra_content") or {}
                google = (
                    extra_content.get("google")
                    if isinstance(extra_content, dict)
                    else None
                )
                if isinstance(google, dict) and google.get("thought_signature"):
                    message["_provider_state_selector"] = selector
                    break

    @staticmethod
    def _strip_provider_private_state(
        value: Any,
        *,
        strip_reasoning_content: bool = False,
        allowed_google_selector: str | None = None,
        _allow_google_extra: bool = False,
    ) -> Any:
        """Deep-copy a Chat payload value without native-provider state.

        Normalized history is shared across failover routes. Native Anthropic,
        Gemini, and Responses adapters consume the private fields themselves;
        generic Chat Completions routes must never receive them.
        """

        if isinstance(value, dict):
            producer_selector = str(value.get("_provider_state_selector") or "")
            allow_google_extra = _allow_google_extra or bool(
                allowed_google_selector
                and producer_selector == allowed_google_selector
            )
            is_tool_call = "function" in value and (
                value.get("type") == "function" or "id" in value
            )
            return {
                key: DeepSeekClient._strip_provider_private_state(
                    item,
                    strip_reasoning_content=strip_reasoning_content,
                    allowed_google_selector=allowed_google_selector,
                    _allow_google_extra=allow_google_extra,
                )
                for key, item in value.items()
                if key not in PROVIDER_PRIVATE_MESSAGE_KEYS
                and not (strip_reasoning_content and key == "reasoning_content")
                and not (
                    key == "extra_content"
                    and is_tool_call
                    and not allow_google_extra
                )
            }
        if isinstance(value, list):
            return [
                DeepSeekClient._strip_provider_private_state(
                    item,
                    strip_reasoning_content=strip_reasoning_content,
                    allowed_google_selector=allowed_google_selector,
                    _allow_google_extra=_allow_google_extra,
                )
                for item in value
            ]
        if isinstance(value, tuple):
            return [
                DeepSeekClient._strip_provider_private_state(
                    item,
                    strip_reasoning_content=strip_reasoning_content,
                    allowed_google_selector=allowed_google_selector,
                    _allow_google_extra=_allow_google_extra,
                )
                for item in value
            ]
        return value

    @staticmethod
    def _merge_stream_events(events: list[dict[str, Any]]) -> dict[str, Any]:
        accumulator = _StreamAccumulator(retain_final_reasoning=True)
        try:
            for event in events:
                accumulator.feed(event)
            return accumulator.result()
        finally:
            accumulator.close()

    @staticmethod
    def _merge_anthropic_stream_events(
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        accumulator = _AnthropicStreamAccumulator()
        for event in events:
            accumulator.feed(event)
        return accumulator.result()
