from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


def _normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _label(option: dict[str, Any]) -> str:
    model = str(option.get("model") or option.get("selector") or "").strip()
    provider = str(option.get("provider") or "").strip()
    return f"{model} ({provider})" if provider else model


class ModelSelectionError(ValueError):
    def __init__(
        self,
        requested: str,
        *,
        code: str,
        suggestions: list[dict[str, str]] | None = None,
    ) -> None:
        self.requested = requested
        self.code = code
        self.suggestions = suggestions or []
        if self.suggestions:
            names = ", ".join(item["label"] for item in self.suggestions)
            message = (
                f'Model name "{requested}" is not an exact match. Possible intended model: '
                f"{names}. No setting was changed; ask the user to confirm the intended model first."
            )
        else:
            message = (
                f'Unsupported model selection "{requested}". No setting was changed; '
                "ask the user for the exact configured model name."
            )
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "requested": self.requested,
            "suggestions": self.suggestions,
            "message": str(self),
        }


def resolve_model_selection(
    requested: str,
    options: list[dict[str, Any]],
) -> str:
    """Resolve an exact configured model without silently falling back.

    Punctuation, spacing, and letter case are ignored, so spoken
    ``GPT 5.6 Sol`` resolves normally. A near miss such as ``GPT 5.6 Sou``
    raises a structured confirmation request instead of becoming ``auto``.
    """
    raw = str(requested or "").strip()
    normalized = _normalized(raw)
    if normalized in {"auto", "automatic", "\u81ea\u52a8", "\u81ea\u52a8\u6a21\u578b"}:
        return "auto"

    aliases: list[tuple[str, dict[str, Any]]] = []
    for option in options:
        selector = str(option.get("selector") or "").strip()
        model = str(option.get("model") or "").strip()
        provider = str(option.get("provider") or "").strip()
        if not selector:
            continue
        values = {
            selector,
            model,
            f"{provider}:{model}" if provider and model else "",
            f"{provider} {model}" if provider and model else "",
        }
        for value in values:
            alias = _normalized(value)
            if alias:
                aliases.append((alias, option))

    exact = {
        str(option.get("selector"))
        for alias, option in aliases
        if alias == normalized
    }
    if len(exact) == 1:
        return exact.pop()
    if len(exact) > 1:
        suggestions = []
        for selector in sorted(exact):
            option = next(item for item in options if item.get("selector") == selector)
            suggestions.append({"selector": selector, "label": _label(option)})
        raise ModelSelectionError(
            raw, code="ambiguous_model_selection", suggestions=suggestions
        )

    best_by_selector: dict[str, tuple[float, dict[str, Any]]] = {}
    for alias, option in aliases:
        score = SequenceMatcher(None, normalized, alias).ratio()
        selector = str(option.get("selector"))
        if score > best_by_selector.get(selector, (0.0, option))[0]:
            best_by_selector[selector] = (score, option)
    ranked = sorted(best_by_selector.values(), key=lambda item: item[0], reverse=True)
    suggestions = [
        {"selector": str(option["selector"]), "label": _label(option)}
        for score, option in ranked[:3]
        if score >= 0.68
    ]
    raise ModelSelectionError(
        raw,
        code="model_confirmation_required" if suggestions else "unsupported_model_selection",
        suggestions=suggestions,
    )
