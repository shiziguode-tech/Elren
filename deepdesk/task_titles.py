from __future__ import annotations

import re
import unicodedata

SCRIPT_MARKERS = (
    "LATIN",
    "CJK",
    "HIRAGANA",
    "KATAKANA",
    "HANGUL",
    "CYRILLIC",
    "GREEK",
    "ARABIC",
    "HEBREW",
    "DEVANAGARI",
    "BENGALI",
    "GURMUKHI",
    "GUJARATI",
    "ORIYA",
    "TAMIL",
    "TELUGU",
    "KANNADA",
    "MALAYALAM",
    "THAI",
    "ARMENIAN",
    "GEORGIAN",
)


def _character_script(character: str) -> str | None:
    name = unicodedata.name(character, "")
    if not name:
        return None
    if name.startswith("CJK ") or "IDEOGRAPH" in name:
        return "CJK"
    for marker in SCRIPT_MARKERS:
        if marker in name:
            return marker
    return None


def text_scripts(value: str) -> set[str]:
    return {
        script
        for character in str(value or "")
        if (script := _character_script(character)) is not None
    }


def title_scripts_compatible(prompt: str, title: str) -> bool:
    """Reject stray writing systems while preserving legitimate multilingual titles."""
    title = str(title or "")
    if not title.strip() or any(
        unicodedata.category(character).startswith("C") and not character.isspace()
        for character in title
    ):
        return False
    prompt_scripts = text_scripts(prompt)
    title_scripts = text_scripts(title)
    # Product and model names commonly use Latin characters in every language.
    allowed = (prompt_scripts or {"LATIN"}) | {"LATIN"}
    return title_scripts <= allowed


def clean_generated_title(prompt: str, raw_title: str) -> str:
    raw = str(raw_title or "").strip()
    # A title-model call can occasionally answer the underlying task instead
    # of the title instruction, especially when that task requires strict JSON.
    # Reject machine-readable payloads and long multi-line answers rather than
    # truncating them into misleading sidebar labels such as ``{"task_id":``.
    compact_raw = re.sub(r"\s+", " ", raw)
    if (
        len(raw) > 180
        or raw.startswith(("{", "[", "```"))
        or re.search(r'"(?:task_id|tool_calls|evidence|collect|normalize|summarize)"\s*:', raw)
        or raw.count("\n") > 1
    ):
        raise ValueError("generated task title is a response payload, not a title")
    title = compact_raw
    title = re.sub(r"^[#*`\s\"'“”‘’]+|[#*`\s\"'“”‘’]+$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -—:：，。！？；,.!?;")
    title = title[:60].rstrip("，。！？；：,.!?;:")
    visible = [character for character in title if character.isalnum()]
    prompt_scripts = text_scripts(prompt)
    minimum = 4 if "CJK" in prompt_scripts else 3
    if (
        not title
        or len(visible) < minimum
        or not title_scripts_compatible(prompt, title)
    ):
        raise ValueError("generated task title contains an unrelated writing system")
    return title
