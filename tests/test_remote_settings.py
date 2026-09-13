from __future__ import annotations

from pathlib import Path

import pytest

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.remote_settings import CHANGE_PROPERTIES, RemoteSettingsTool


@pytest.mark.parametrize("prefix", ["//", "／／", "/／", "／/"])
def test_remote_settings_accepts_all_supported_slash_prefixes(prefix: str, tmp_path: Path):
    tool = RemoteSettingsTool({}, tmp_path / "outputs")
    assert tool._has_prefix(prefix + " 修改设置") is True
    assert tool.risk({"changes": {"request_timeout": 240}}) == Risk.SAFE


def test_remote_settings_omits_retired_no_op_controls():
    assert {
        "default_policy",
        "discussion_team_enabled",
        "max_steps",
    }.isdisjoint(CHANGE_PROPERTIES)


def test_remote_settings_exposes_preferences_but_never_credentials_or_ai_controls():
    expected = {
        "model", "cross_conversation_context",
        "max_output_tokens", "request_timeout",
        "voice_language", "voice_name",
        "voice_rate", "voice_auto_speak", "voice_hands_free", "voice_auto_continue",
    }
    sensitive = {
        "custom_system_prompt_suffix", "discussion_team", "model_providers", "deepseek_api_key",
        "deepseek_backup_api_key", "gemini_api_key", "pollinations_api_key",
        "huggingface_token", "aicodemirror_api_key", "aicodemirror_fable_api_key", "feishu_app_id",
        "feishu_app_secret", "feishu_open_id", "telegram_bot_token",
        "telegram_chat_id", "okx_api_key", "okx_secret_key", "okx_passphrase",
        "binance_api_key", "binance_secret_key",
    }
    assert expected <= set(CHANGE_PROPERTIES)
    assert sensitive.isdisjoint(CHANGE_PROPERTIES)
    assert {"compact_trace", "theme_mode", "theme_accent"}.isdisjoint(CHANGE_PROPERTIES)




@pytest.mark.asyncio
async def test_remote_settings_requires_prefix_and_creates_sanitized_evidence(tmp_path: Path):
    calls: list[dict[str, object]] = []

    async def apply(changes):
        calls.append(changes)
        return {
            "changed": {
                "request_timeout": {"before": 180, "after": 240},
            }
        }

    tool = RemoteSettingsTool({"apply": apply}, tmp_path / "outputs")
    no_prefix = ToolContext(
        task_id="remote-1", workspace=str(tmp_path), source="telegram",
        user_prompt="把请求超时改为 240 秒",
    )
    with pytest.raises(PermissionError, match="begin with"):
        await tool.execute({"changes": {"request_timeout": 240}}, no_prefix)

    voice_context = ToolContext(
        task_id="remote-voice", workspace=str(tmp_path), source="telegram",
        user_prompt="把请求超时改为 240 秒", voice_request=True,
    )
    voice_result = await tool.execute(
        {"changes": {"request_timeout": 240}}, voice_context,
    )
    assert Path(voice_result["txt_log"]).is_file()

    context = ToolContext(
        task_id="remote-2", workspace=str(tmp_path), source="feishu",
        user_prompt="／/ 把请求超时改为 240 秒",
    )
    with pytest.raises(ValueError, match="Unsupported settings"):
        await tool.execute(
            {"changes": {"request_timeout": 240, "binance_api_key": "secret"}},
            context,
        )
    result = await tool.execute({"changes": {"request_timeout": 240}}, context)

    assert calls == [
        {"request_timeout": 240},
        {"request_timeout": 240},
    ]
    for key in ("before_screenshot", "after_screenshot", "txt_log"):
        assert Path(result[key]).is_file()
    audit = Path(result["txt_log"]).read_text(encoding="utf-8")
    assert "secret" not in audit
    assert "request_timeout" in audit


