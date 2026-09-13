"""Validated, user-declared compatible API routes (never official credentials)."""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def is_custom_provider(entry: dict[str, Any]) -> bool:
    return entry.get("source") == "custom" or "base_url" in entry


def normalize_custom_provider(entry: dict[str, Any]) -> dict[str, Any]:
    result = dict(entry)
    if not is_custom_provider(entry):
        return result  # Preserve existing official rows and their selectors.
    protocol = entry.get("provider")
    if protocol not in {"openai", "anthropic"}:
        raise ValueError("Custom API protocol must be OpenAI or Anthropic")
    entry_id = entry.get("id")
    if entry_id is not None and (not isinstance(entry_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry_id)):
        raise ValueError("Invalid custom API configuration ID")
    model = entry.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or re.search(r"[\x00-\x1f\x7f]", model):
        raise ValueError("Enter an exact API model name (1–200 characters)")
    address = entry.get("base_url")
    if not isinstance(address, str) or not address.strip() or len(address) > 2048:
        raise ValueError("Enter the third-party API base URL")
    address = address.strip()
    if re.search(r"[\s\\\x00-\x1f\x7f]", address):
        raise ValueError("API URL must not contain whitespace or backslashes")
    try:
        url = urlsplit(address)
        host = url.hostname
        port = url.port
    except ValueError:
        raise ValueError("Invalid API URL") from None
    if not host or url.username is not None or url.password is not None or url.query or url.fragment or "?" in address or "#" in address:
        raise ValueError("API URL must have a host and no credentials, query or fragment")
    loopback = host.casefold() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise ValueError("Use HTTPS for remote APIs (HTTP is allowed only on loopback)")
    if port == 0:
        raise ValueError("Invalid API port")
    path = url.path.rstrip("/")
    suffix = "/chat/completions" if protocol == "openai" else "/messages"
    if path.endswith(suffix):
        path = path[:-len(suffix)]
    elif path.endswith(("/chat/completions", "/messages", "/responses")):
        raise ValueError("API URL does not match the selected protocol")
    path = path or "/v1"
    levels = entry.get("reasoning_levels", [])
    allowed = REASONING_LEVELS if protocol == "openai" else REASONING_LEVELS[1:]
    if not isinstance(levels, list) or any(not isinstance(level, str) or level not in allowed for level in levels):
        raise ValueError("Invalid reasoning levels for this protocol")
    key = entry.get("api_key", "")
    if not isinstance(key, str) or re.search(r"[\x00-\x1f\x7f]", key):
        raise ValueError("Invalid API key format")
    result.update(source="custom", provider=protocol, model=model.strip(),
                  base_url=urlunsplit((url.scheme, url.netloc.lower(), path, "", "")),
                  reasoning_levels=[level for level in allowed if level in levels],
                  api_key=key.strip())
    return result


def provider_selector(entry: dict[str, Any]) -> str:
    return f"custom:{entry['id']}" if is_custom_provider(entry) else f"{entry['provider']}:{entry['model']}"
