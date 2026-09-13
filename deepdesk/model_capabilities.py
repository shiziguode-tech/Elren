from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReasoningCapability:
    """Verified API-level reasoning controls for one concrete model ID.

    Unknown user-supplied IDs deliberately get no controls.  A name that merely
    looks like a reasoning model is not evidence that its endpoint accepts a
    reasoning parameter.
    """

    levels: tuple[str, ...] = ()
    control: str = "none"
    verification: str = "unverified"

    def public(self) -> dict[str, Any]:
        return {
            "supported": bool(self.levels),
            "levels": list(self.levels),
            "control": self.control,
            "verification": self.verification,
        }


NONE = ReasoningCapability()

RETIRED_DEEPSEEK_MODEL = "deepseek-v4.1-flash-expires-on-0910"


def is_retired_deepseek_selector(selector: str) -> bool:
    # Only built-in/direct selectors; do not rewrite explicit third-party routes.
    return str(selector or "").strip().casefold() in {
        RETIRED_DEEPSEEK_MODEL, "deepseek:" + RETIRED_DEEPSEEK_MODEL,
    }


def reasoning_capability(provider: str, model: str) -> ReasoningCapability:
    """Return a conservative, exact API capability description.

    The matrix is based on the vendors' published API schemas; GPT-6 Astra was
    checked against https://developers.openai.com/api/docs/models/gpt-6-astra
    and the latest-model guide on 2026-09-05.
    Only exact documented families are recognized; arbitrary configured model
    strings remain unsupported until a vendor document or a successful probe
    establishes their request contract.
    """

    provider = str(provider or "").casefold()
    model = str(model or "").casefold().strip()

    if provider == "deepseek" and model == "deepseek-v4-flash-vision-exp":
        # Official schema + both saved-key OCR/chat probes, 2026-09-05.
        # medium/xhigh map to high; accepted none does NOT disable thinking.
        return ReasoningCapability(
            levels=("low", "high", "max"),
            control="deepseek_reasoning_effort",
            verification="deepseek_api_docs_and_live_probe_20260905",
        )

    if provider == "deepseek" and model in {
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    }:
        return ReasoningCapability(
            levels=("high", "max"),
            control="deepseek_reasoning_effort",
            verification="deepseek_api_docs",
        )

    if provider == "openai":
        if model in {"gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"}:
            return ReasoningCapability(
                levels=("low", "medium", "high", "xhigh", "max"),
                control="openai_reasoning_effort",
                verification="openai_model_spec",
            )
        if re.fullmatch(r"gpt-5\.(?:4|5)(?:-(?:mini|nano))?", model):
            return ReasoningCapability(
                levels=("low", "medium", "high", "xhigh"),
                control="openai_reasoning_effort",
                verification="openai_model_spec",
            )
        if model == "gpt-5.1":
            return ReasoningCapability(
                levels=("low", "medium", "high"),
                control="openai_reasoning_effort",
                verification="openai_model_spec",
            )
        if model in {"gpt-5.4-pro", "gpt-5.5-pro"}:
            return ReasoningCapability(
                levels=("medium", "high", "xhigh"),
                control="openai_reasoning_effort",
                verification="openai_model_spec",
            )

    if provider == "anthropic":
        if model in {
            "claude-opus-5",
            "claude-fable-5-1",
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
        }:
            return ReasoningCapability(
                levels=("low", "medium", "high", "xhigh", "max"),
                control="anthropic_output_config_effort",
                verification="anthropic_effort_docs",
            )
        if model in {"claude-opus-4-6", "claude-sonnet-4-6"}:
            return ReasoningCapability(
                levels=("low", "medium", "high", "max"),
                control="anthropic_output_config_effort",
                verification="anthropic_effort_docs",
            )
        if model in {
            "claude-haiku-4-5-20251001",
            "claude-opus-4-5-20251101",
            "claude-sonnet-4-5-20250929",
        }:
            return ReasoningCapability(
                levels=("low", "medium", "high", "xhigh", "max"),
                control="anthropic_budget_tokens",
                verification="anthropic_extended_thinking_docs",
            )

    if provider == "google":
        if model in {"gemini-3.8-flash", "gemini-3.7-flash"}:
            return ReasoningCapability(
                levels=("low", "medium", "high"),
                control="gemini_thinking_level",
                verification="google_thinking_docs",
            )
        if model in {
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
        }:
            return ReasoningCapability(
                levels=("minimal", "low", "medium", "high"),
                control="gemini_thinking_level",
                verification="google_thinking_docs",
            )
        if model in {"gemini-3.1-pro-preview", "gemini-2.5-pro"}:
            return ReasoningCapability(
                levels=("low", "medium", "high"),
                control="gemini_thinking_level",
                verification="google_thinking_docs",
            )
        if model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            return ReasoningCapability(
                levels=("low", "medium", "high"),
                control="gemini_thinking_budget",
                verification="google_thinking_docs",
            )

    if provider == "xai":
        if model == "grok-4.5":
            return ReasoningCapability(
                levels=("low", "medium", "high"),
                control="xai_reasoning_effort",
                verification="xai_reasoning_docs",
            )
        if model == "grok-4.20-multi-agent":
            return ReasoningCapability(
                levels=("low", "medium", "high", "xhigh"),
                control="xai_agent_count_effort",
                verification="xai_multi_agent_docs",
            )

    return NONE
