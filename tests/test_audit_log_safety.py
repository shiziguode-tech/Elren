from __future__ import annotations

import json

import pytest

from deepdesk.audit import AuditLog, sanitize_audit_value


def test_audit_sanitizer_omits_binary_and_bounds_large_text() -> None:
    sanitized = sanitize_audit_value(
        {
            "bytes": b"\x00\x01secret-binary",
            "binary_text": "\x00\x01" * 100,
            "invalid_encoding": "\ufffd" * 20,
            "large": "x" * 20_000,
        }
    )

    assert sanitized["bytes"] == "[binary content omitted: 15 bytes]"
    assert sanitized["binary_text"].startswith("[binary content omitted:")
    assert sanitized["invalid_encoding"].startswith("[text with invalid encoding omitted:")
    assert "audit text truncated" in sanitized["large"]
    assert len(sanitized["large"]) < 9_000


def test_audit_sanitizer_redacts_unsaved_credentials_and_authorization_headers() -> None:
    sanitized = sanitize_audit_value(
        {
            "api_key": "sk-new-secret-not-yet-configured",
            "nested": {"telegram_bot_token": "123456:ABCDEF"},
            "configured": {"gemini_key_configured": True},
            "header": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "url": "https://example.test/run?token=url-secret-value&mode=safe",
            "json": '{"password":"plain-text-secret"}',
        }
    )

    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert "sk-new-secret" not in serialized
    assert "123456:ABCDEF" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "url-secret-value" not in serialized
    assert "plain-text-secret" not in serialized
    assert sanitized["configured"]["gemini_key_configured"] is True


@pytest.mark.asyncio
async def test_audit_log_writes_valid_utf8_jsonl_without_raw_binary(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path)

    await audit.write(
        "tool_result",
        "task-1",
        {"result": {"content": "\x00\x01" * 1000}, "raw": b"payload"},
    )

    raw = path.read_bytes()
    assert b"\x00" not in raw
    record = json.loads(raw.decode("utf-8"))
    assert record["data"]["result"]["content"].startswith("[binary content omitted:")
    assert record["data"]["raw"] == "[binary content omitted: 7 bytes]"


def test_existing_audit_log_is_sanitized_once(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps({"event": "legacy", "data": {"content": "\x00\x01" * 200}})
        + "\nnot-json\n",
        encoding="utf-8",
    )

    AuditLog(path)

    records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert records[0]["data"]["content"].startswith("[binary content omitted:")
    assert records[1]["event"] == "legacy_log_line_omitted"
    assert path.with_suffix(".jsonl.sanitized-v2").is_file()
