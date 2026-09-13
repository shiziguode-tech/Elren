from __future__ import annotations

import json
import re
from typing import Any

# Some OpenAI-compatible relays have historically returned an otherwise normal
# assistant answer with the whole text JSON-encoded one extra time.  Do not use
# a general ``unicode_escape`` decode: it would also reinterpret source code,
# regular expressions and Windows paths.
_LITERAL_LINE_BREAK = re.compile(r"(?<!\\)\\(?:r\\n|n|r)")
_WINDOWS_PATH = re.compile(
    r"(?i)(?:^|[\s\"'(<>=])(?:[a-z]:\\|\\\\[^\\\s]+\\)"
)
_ESCAPE_EXPLANATION = re.compile(
    r"(?i)(?:escape|escaped|literal|regex|regexp|转义|字面量|正则)"
    r".{0,80}\\[nr]|\\[nr].{0,80}"
    r"(?:escape|escaped|literal|regex|regexp|转义|字面量|正则)"
)
_REGEX_OR_ESCAPE_SYNTAX = re.compile(
    r"\\[AbBdDsSwWZzG]|\\x[0-9a-fA-F]{2}|\\u[0-9a-fA-F]{4}"
    r"|\\N\{|(?:^|\s)\^[^\s]{1,160}\$|\(\?[aiLmsux-]"
)


def normalize_assistant_display_text(value: str) -> str:
    """Decode only an exact, whole-field JSON string envelope.

    This function is intentionally for human-display copies.  The original
    model message, tool arguments/results and durable task payload must remain
    untouched.  Parsing and re-encoding must round-trip byte-for-byte, and the
    decoded value must contain a real line break.  Ambiguous unquoted text,
    structured JSON, code, regexes, paths and double escapes are preserved.
    """

    if not value or "\n" in value or "\r" in value:
        return value
    if value != value.strip() or not _LITERAL_LINE_BREAK.search(value):
        return value

    # Reject syntax-bearing forms before JSON decoding can reinterpret them.
    if "```" in value or "~~~" in value or "`" in value:
        return value
    if _WINDOWS_PATH.search(value):
        return value
    if _ESCAPE_EXPLANATION.search(value) or _REGEX_OR_ESCAPE_SYNTAX.search(value):
        return value

    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return value
    if not isinstance(decoded, str) or not ("\n" in decoded or "\r" in decoded):
        return value
    if json.dumps(decoded, ensure_ascii=False, separators=(",", ":")) != value:
        return value

    # A JSON string can itself contain a source-code or serialized-data value.
    # Even an exact envelope is not enough evidence to reinterpret that payload.
    if "```" in decoded or "~~~" in decoded or "`" in decoded:
        return value
    if _WINDOWS_PATH.search(decoded):
        return value
    if _ESCAPE_EXPLANATION.search(decoded) or _REGEX_OR_ESCAPE_SYNTAX.search(decoded):
        return value
    try:
        nested = json.loads(decoded)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    else:
        if isinstance(nested, (dict, list, str)):
            return value
    return decoded


def add_task_display_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    """Add sparse API-only display fields while retaining every raw field."""

    result = payload.get("result")
    if isinstance(result, str):
        display_result = normalize_assistant_display_text(result)
        if display_result != result:
            payload["display_result"] = display_result

    events = payload.get("events")
    if not isinstance(events, list):
        return payload
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in {
            "assistant",
            "team_member_report",
        }:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if not isinstance(content, str):
            continue
        display_content = normalize_assistant_display_text(content)
        if display_content != content:
            data["display_content"] = display_content
    return payload
