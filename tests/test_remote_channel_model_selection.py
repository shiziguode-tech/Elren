import logging
from pathlib import Path

from deepdesk.main import (
    authorize_or_bind_telegram_chat,
    log_telegram_delivery_failure,
    remote_channel_task_snapshot,
)
from deepdesk.models import DiscussionTeamMember
from deepdesk.runtime_settings import RuntimeSettings
from deepdesk.telegram import TelegramBridge


def _configured_team() -> list[DiscussionTeamMember]:
    return [
        DiscussionTeamMember(
            id="leader",
            name="Leader",
            role="leader",
            model="aicodemirror-openai:gpt-5.6-sol",
            reasoning_effort="high",
        ),
        DiscussionTeamMember(
            id="member",
            name="Member",
            role="member",
            model="deepseek-v4-pro",
            reasoning_effort="high",
        ),
    ]


def test_remote_channels_use_saved_default_model_not_configured_team() -> None:
    runtime = RuntimeSettings(
        model="aicodemirror-openai:gpt-5.6-terra",
        reasoning_effort="medium",
        discussion_team=_configured_team(),
    )

    snapshot = remote_channel_task_snapshot(
        runtime,
        automatic_model="deepseek-v4-flash",
    )

    assert snapshot == {
        "model_preference": "aicodemirror-openai:gpt-5.6-terra",
        "active_model": "aicodemirror-openai:gpt-5.6-terra",
        "reasoning_effort": "medium",
        "discussion_team_enabled": False,
        "discussion_team": [],
    }


def test_remote_channels_resolve_automatic_without_enabling_team() -> None:
    runtime = RuntimeSettings(
        model="auto",
        discussion_team=_configured_team(),
    )

    snapshot = remote_channel_task_snapshot(
        runtime,
        automatic_model="deepseek-v4-flash",
    )

    assert snapshot["model_preference"] == "auto"
    assert snapshot["active_model"] == "deepseek-v4-flash"
    assert snapshot["discussion_team_enabled"] is False
    assert snapshot["discussion_team"] == []


def test_both_remote_launchers_share_the_non_team_snapshot() -> None:
    source = (Path(__file__).resolve().parents[1] / "deepdesk" / "main.py").read_text(
        encoding="utf-8"
    )
    feishu = source[source.index("async def launch_feishu_task(") : source.index("async def flush_pending_feishu_attachments(")]
    telegram = source[source.index("async def launch_telegram_task(") : source.index("def telegram_attachment_only_prompt(")]

    for launcher in (feishu, telegram):
        assert "remote_channel_task_snapshot(" in launcher
        assert "**task_snapshot" in launcher
        assert "**discussion_team_snapshot()" not in launcher


def test_telegram_launcher_explicitly_acknowledges_consumed_updates() -> None:
    source = (Path(__file__).resolve().parents[1] / "deepdesk" / "main.py").read_text(
        encoding="utf-8"
    )
    feishu = source[
        source.index("async def launch_feishu_task(") : source.index(
            "def attachment_only_prompt("
        )
    ]
    telegram = source[
        source.index("async def launch_telegram_task(") : source.index(
            "def telegram_attachment_only_prompt("
        )
    ]

    assert '"reason": "model_api_key_required",\n                "ack_update": True' in telegram
    assert '"attachment_count": len(attachments),\n            "ack_update": True' in telegram
    assert "ack_update" not in feishu


def test_telegram_failure_notifications_are_single_and_failures_are_classified() -> None:
    source = (Path(__file__).resolve().parents[1] / "deepdesk" / "main.py").read_text(
        encoding="utf-8"
    )
    callback = source[
        source.index("async def notify_telegram_failure_once(") : source.index(
            "async def retry_telegram_delivery("
        )
    ]

    assert "telegram_failure_notices" in callback
    assert '"attachment_download_failed"' in callback
    assert "isinstance(" in callback and "(ValueError, PermissionError)" in callback
    assert '"voice_transcription_failed"' in callback
    assert '"ack_update": notified' in callback
    assert "{exc}" not in callback
    assert "type(exc).__name__}:" not in callback


def test_telegram_delivery_failure_log_never_serializes_exception_or_token(caplog) -> None:
    token = "123456789:TEST_SECRET_SHOULD_NEVER_REACH_LOGS"
    secret_url = f"https://api.telegram.org/bot{token}/sendMessage"

    with caplog.at_level(logging.DEBUG, logger="deepdesk.main"):
        log_telegram_delivery_failure(
            "Failed to acknowledge Telegram task",
            RuntimeError(f"request failed for {secret_url}"),
        )

    rendered = caplog.text
    assert "Failed to acknowledge Telegram task (RuntimeError)" in rendered
    assert token not in rendered
    assert secret_url not in rendered
    assert "request failed" not in rendered


class _FakeSecretsStore:
    def __init__(self) -> None:
        self.updates: list[dict[str, str]] = []

    def update(self, **changes):
        self.updates.append(changes)


def test_telegram_bound_chat_rejects_takeover_without_rebinding() -> None:
    bridge = TelegramBridge("token", "123")
    secrets = _FakeSecretsStore()

    accepted = authorize_or_bind_telegram_chat(
        bridge, secrets, "999", chat_type="private"
    )

    assert accepted is False
    assert bridge.default_chat_id == "123"
    assert secrets.updates == []


def test_telegram_first_private_chat_binds_once_but_group_cannot_auto_claim() -> None:
    bridge = TelegramBridge("token")
    secrets = _FakeSecretsStore()

    assert not authorize_or_bind_telegram_chat(
        bridge, secrets, "-999", chat_type="supergroup"
    )
    assert bridge.default_chat_id == ""
    assert secrets.updates == []

    assert authorize_or_bind_telegram_chat(
        bridge, secrets, "123", chat_type="private"
    )
    assert bridge.default_chat_id == "123"
    assert secrets.updates == [{"telegram_chat_id": "123"}]


def test_telegram_attachment_batch_key_isolated_by_chat_thread_and_sender() -> None:
    source = (Path(__file__).resolve().parents[1] / "deepdesk" / "main.py").read_text(
        encoding="utf-8"
    )
    callback = source[
        source.index("async def accept_telegram_update(") : source.index(
            "async def retry_telegram_delivery("
        )
    ]

    assert 'sender_id = str(event.get("sender_id")' in callback
    assert 'sender_id = f"message-{message_scope}"' in callback
    assert 'thread_id = str(event.get("message_thread_id")' in callback
    assert 'f"telegram:{chat_id}:{thread_id}:{sender_id}"' in callback
    assert '"reason": "telegram_chat_not_authorized"' in callback
