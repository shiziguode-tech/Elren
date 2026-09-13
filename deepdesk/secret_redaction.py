from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# A field name is stronger evidence than the value's shape.  Deliberately do
# not classify a bare ``key`` or ``id`` as secret: both are common in harmless
# tool results.  Provider-specific names such as ``telegram_bot_token`` and
# ``aws_secret_access_key`` are covered by the component boundaries below.
_SECRET_FIELD = re.compile(
    r"(?:^|[\s_-])(?:"
    r"api[\s_-]?key|access[\s_-]?key(?:[\s_-]?id)?|secret(?:[\s_-]?key)?|"
    r"client[\s_-]?secret|token|password|passphrase|authorization|cookie|"
    r"session[\s_-]?lease|private[\s_-]?key"
    r")(?:$|[\s_-])",
    re.IGNORECASE,
)
_CONFIGURED_STATUS_FIELD = re.compile(r"(?:^|[\s_-])configured$", re.IGNORECASE)

_CREDENTIAL_LABEL = (
    r"(?:api[\s_-]?key|access[\s_-]?key(?:[\s_-]?id)?|secret(?:[\s_-]?key)?|"
    r"client[\s_-]?secret|token|password|passphrase|authorization|cookie|"
    r"session[\s_-]?lease|private[\s_-]?key|密钥|密码|令牌)"
)

_AUTHORIZATION = re.compile(
    r"(?i)(\b(?:Authorization|Proxy-Authorization)\s*:\s*"
    r"(?:Bearer|Basic|Token|Digest)\s+)[^\s,;]+"
)
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?key|client_secret|secret|token|"
    r"password|passphrase|authorization|cookie|session[_-]?lease)=)[^&#\s]+"
)
_URL_USERINFO = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@:\s]+:)([^/@\s]+)(@)"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_SESSION_LEASE_FIELD = re.compile(r"^session[\s_-]*lease$", re.IGNORECASE)
_SESSION_LEASE_TEXT = re.compile(
    r'''(?ix)
    ["']?session[\s_-]*lease["']?\s*[:=：]\s*
    (?:["'][^"']*["']|[^\s,，;；}\]&]+)
    '''
)

# The assignment expressions below intentionally support permissive field
# names, but that makes them expensive on long arbitrary text: their ``*``
# portions can retry at every character before concluding that no credential
# assignment exists.  Keep the expressive expressions for positive matches,
# while using cheap linear hints to avoid invoking them for ordinary tool
# output (which is by far the common path).
_CREDENTIAL_TEXT_HINT = re.compile(
    r"(?i)(?:api|access|secret|client|token|password|passphrase|"
    r"authorization|cookie|session|private|密钥|密码|令牌)"
)
_ASSIGNMENT_TEXT_HINT = re.compile(r"[:=：]")

_ASSIGNMENT_BOUNDARIES = frozenset("\r\n,;，；{}[]&")
_ASSIGNMENT_FIELD_MAX = 512


def _assignment_field_is_secret(field: str, *, include_session_leases: bool) -> bool:
    """Classify a bounded assignment lhs without invoking a backtracking regex."""

    normalized = field.strip().strip("\"'").strip()
    if not normalized:
        return False
    # Text assignments historically accepted the broader credential label
    # set than mapping keys (including Chinese labels and embedded labels such
    # as ``provider_api_key``), so retain that behavior here.
    if not re.search(_CREDENTIAL_LABEL, normalized, re.IGNORECASE):
        return False
    return include_session_leases or not _SESSION_LEASE_FIELD.fullmatch(normalized)


def _redact_assignments_linear(
    value: str, *, include_session_leases: bool
) -> str:
    """Redact credential assignments with a bounded, single forward scan.

    The former permissive regexes could backtrack quadratically on strings
    such as ``api:xxxx...``.  This parser only scans backwards at most one
    field width and advances past each consumed value, so malformed or huge
    values remain bounded while JSON, quoted, unquoted, and escaped values are
    handled deterministically.
    """

    length = len(value)
    pieces: list[str] = []
    emitted = 0
    index = 0
    while index < length:
        if value[index] not in ":=：":
            index += 1
            continue

        field_end = index
        field_start = max(0, index - _ASSIGNMENT_FIELD_MAX)
        while field_start < field_end:
            if value[field_start] in _ASSIGNMENT_BOUNDARIES:
                field_start += 1
            else:
                break
        # Trim the candidate at the nearest structural boundary from the
        # right.  This keeps the field bounded even when prose precedes it.
        candidate = value[field_start:field_end]
        right_boundary = max(
            (candidate.rfind(boundary) for boundary in _ASSIGNMENT_BOUNDARIES),
            default=-1,
        )
        if right_boundary >= 0:
            field_start += right_boundary + 1
        if not _assignment_field_is_secret(
            value[field_start:field_end],
            include_session_leases=include_session_leases,
        ):
            index += 1
            continue

        value_start = index + 1
        while value_start < length and value[value_start].isspace():
            value_start += 1
        if value_start >= length:
            index = value_start
            continue
        # Earlier redaction passes (query strings, authorization headers,
        # private-key blocks) may already have replaced this value.  Treat the
        # marker as immutable; otherwise the unquoted scanner would stop at
        # its closing bracket and append a second ``]``.
        if value.startswith(REDACTED, value_start):
            index = value_start + len(REDACTED)
            continue
        if re.match(
            r"(?i)(?:bearer|basic|token|digest)\s+\[REDACTED\]",
            value[value_start:],
        ):
            index = value_start + len(REDACTED)
            continue

        quote = value[value_start] if value[value_start] in "\"'" else None
        if quote is not None:
            content_start = value_start + 1
            cursor = content_start
            while cursor < length:
                if value[cursor] == "\\":
                    cursor += 2
                elif value[cursor] == quote:
                    break
                else:
                    cursor += 1
            content_end = cursor
            next_index = cursor + 1 if cursor < length else length
        else:
            content_start = value_start
            cursor = content_start
            while cursor < length and not (
                value[cursor].isspace() or value[cursor] in ",，;；}]&"
            ):
                cursor += 1
            content_end = cursor
            next_index = cursor

        if content_end > content_start and value[content_start:content_end] != REDACTED:
            pieces.append(value[emitted:content_start])
            pieces.append(REDACTED)
            emitted = content_end
        index = max(next_index, index + 1)

    if not pieces:
        return value
    pieces.append(value[emitted:])
    return "".join(pieces)

# High-confidence bearer credential formats.  Matching these even without a
# label protects secrets pasted into prose, exception messages and provider
# payloads while avoiding broad entropy guesses that would hide ordinary IDs.
_VENDOR_CREDENTIALS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])gsk_[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])xai-[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])ya29\.[A-Za-z0-9._-]{10,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])AQ\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])hf_[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])npm_[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"),
    re.compile(
        r"(?<![A-Za-z0-9_-])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"
        r"(?![A-Za-z0-9_-])"
    ),
    re.compile(r"(?<![A-Za-z0-9_-])AKIA[0-9A-Z]{16}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])ASIA[0-9A-Z]{16}(?![A-Za-z0-9_-])"),
    re.compile(
        r"(?<![A-Za-z0-9_-])SG\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        r"(?![A-Za-z0-9_-])"
    ),
    re.compile(r"(?<![A-Za-z0-9_-])dop_v1_[A-Fa-f0-9]{16,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?<![A-Za-z0-9_-])\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
    ),
)


def is_secret_field(name: Any, *, include_session_leases: bool = True) -> bool:
    """Return whether a mapping key structurally identifies credential data."""

    field = str(name or "").strip()
    if not include_session_leases and _SESSION_LEASE_FIELD.fullmatch(field):
        return False
    return bool(_SECRET_FIELD.search(field)) and not bool(
        _CONFIGURED_STATUS_FIELD.search(field)
    )


def _known_secret_values(values: Iterable[Any] | None) -> list[str]:
    if values is None:
        return []
    # Values shorter than four characters cannot be real supported provider
    # keys and are dangerously ambiguous in normal prose.  Everything else is
    # replaced exactly, including credentials whose vendor has no known shape.
    return sorted(
        {
            text
            for item in values
            if len(text := str(item or "")) >= 4 and text != REDACTED
        },
        key=len,
        reverse=True,
    )


def persistence_secret_needles(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Return exact and JSON-escaped forms used for cheap persistence preflights.

    The result is intentionally ephemeral: callers use it for ``instr`` or
    byte-presence checks and never write it to a marker or log.  JSON may
    represent quotes, control characters, non-ASCII text, and forward slashes
    differently, so checking only the raw value could produce a false negative.
    """

    needles: set[str] = set()
    for secret in _known_secret_values(values):
        needles.add(secret)
        for ensure_ascii in (False, True):
            encoded = json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1]
            needles.add(encoded)
            needles.add(encoded.replace("/", r"\/"))
    needles.discard("")
    return tuple(sorted(needles, key=len, reverse=True))


def redact_text(
    value: str,
    known_secrets: Iterable[Any] | None = None,
    *,
    include_session_leases: bool = True,
) -> str:
    """Remove exact configured secrets and high-confidence credential shapes."""

    redacted = str(value)
    protected_session_leases: list[str] = []
    if not include_session_leases and _CREDENTIAL_TEXT_HINT.search(redacted):

        def protect_session_lease(match: re.Match[str]) -> str:
            index = len(protected_session_leases)
            protected_session_leases.append(match.group(0))
            return f"ELREN_RUNTIME_LEASE_PLACEHOLDER_{index}"

        redacted = _SESSION_LEASE_TEXT.sub(protect_session_lease, redacted)
    for secret in _known_secret_values(known_secrets):
        redacted = redacted.replace(secret, REDACTED)
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, redacted)
    redacted = _AUTHORIZATION.sub(r"\1[REDACTED]", redacted)
    redacted = _SENSITIVE_QUERY.sub(r"\1[REDACTED]", redacted)
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]\3", redacted)
    # Do not scan ordinary text at all.  Credential-like text takes the
    # bounded single-pass parser; the old permissive assignment regexes are
    # intentionally retained above only as documentation of the supported
    # shape and are no longer on the runtime path.
    if (
        _CREDENTIAL_TEXT_HINT.search(redacted)
        and _ASSIGNMENT_TEXT_HINT.search(redacted)
    ):
        redacted = _redact_assignments_linear(
            redacted,
            include_session_leases=include_session_leases,
        )
    for pattern in _VENDOR_CREDENTIALS:
        redacted = pattern.sub(REDACTED, redacted)
    for index, original in enumerate(protected_session_leases):
        redacted = redacted.replace(f"ELREN_RUNTIME_LEASE_PLACEHOLDER_{index}", original)
    return redacted


def redact_sensitive(
    value: Any,
    known_secrets: Iterable[Any] | None = None,
    *,
    include_session_leases: bool = True,
) -> Any:
    """Return a recursively redacted copy without changing runtime objects.

    The function intentionally preserves collection shapes because callers use
    it both for model events and for serialized persistence boundaries.
    """

    secrets = _known_secret_values(known_secrets)
    if isinstance(value, str):
        return redact_text(
            value,
            secrets,
            include_session_leases=include_session_leases,
        )
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if is_secret_field(
                    key,
                    include_session_leases=include_session_leases,
                )
                and item not in (None, "")
                else redact_sensitive(
                    item,
                    secrets,
                    include_session_leases=include_session_leases,
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            redact_sensitive(
                item,
                secrets,
                include_session_leases=include_session_leases,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive(
                item,
                secrets,
                include_session_leases=include_session_leases,
            )
            for item in value
        )
    if isinstance(value, set):
        return {
            redact_sensitive(
                item,
                secrets,
                include_session_leases=include_session_leases,
            )
            for item in value
        }
    if isinstance(value, frozenset):
        return frozenset(
            redact_sensitive(
                item,
                secrets,
                include_session_leases=include_session_leases,
            )
            for item in value
        )
    return value
