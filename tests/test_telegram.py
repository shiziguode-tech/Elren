import asyncio
from pathlib import Path

import pytest

from deepdesk.telegram import TELEGRAM_LOCAL_UPDATE_MAX_ATTEMPTS, TelegramBridge


def test_telegram_credentials_can_clear_stale_default_chat_id():
    bridge = TelegramBridge("token", "previous-recipient")

    bridge.set_credentials("token", "")

    assert bridge.default_chat_id == ""


def test_telegram_token_only_update_preserves_default_chat_id():
    bridge = TelegramBridge("old-token", "recipient")

    bridge.set_credentials("new-token")

    assert bridge.default_chat_id == "recipient"


@pytest.mark.asyncio
async def test_polling_retries_update_when_local_callback_fails(monkeypatch):
    bridge = TelegramBridge("token", "123")
    update = {
        "update_id": 41,
        "message": {"message_id": 1, "chat": {"id": 123}, "text": "retry me"},
    }
    polls = 0
    deliveries = 0

    async def fake_api(method, **_kwargs):
        nonlocal polls
        assert method == "getUpdates"
        polls += 1
        return [update]

    async def callback(_event):
        nonlocal deliveries
        deliveries += 1
        if deliveries == 1:
            raise RuntimeError("temporary local failure")
        bridge._stop.set()

    async def no_delay(_seconds):
        return None

    bridge._callback = callback
    monkeypatch.setattr(bridge, "_api", fake_api)
    monkeypatch.setattr(asyncio, "sleep", no_delay)

    await bridge._poll_loop()

    assert polls == 2
    assert deliveries == 2
    assert bridge._offset == 42


@pytest.mark.asyncio
async def test_polling_retries_negative_ack_without_skipping_later_updates(monkeypatch):
    bridge = TelegramBridge("token", "123")
    first = {
        "update_id": 41,
        "message": {"message_id": 1, "chat": {"id": 123}, "text": "retry me"},
    }
    second = {
        "update_id": 42,
        "message": {"message_id": 2, "chat": {"id": 123}, "text": "after me"},
    }
    polls = 0
    deliveries: list[str] = []

    async def fake_api(method, **_kwargs):
        nonlocal polls
        assert method == "getUpdates"
        polls += 1
        return [first, second]

    async def callback(event):
        deliveries.append(event["prompt"])
        if deliveries == ["retry me"]:
            return {
                "ok": False,
                "reason": "attachment_download_failed",
                "ack_update": False,
            }
        if event["prompt"] == "after me":
            bridge._stop.set()
        return {"ok": True, "ack_update": True}

    async def no_delay(_seconds):
        return None

    bridge._callback = callback
    monkeypatch.setattr(bridge, "_api", fake_api)
    monkeypatch.setattr(asyncio, "sleep", no_delay)

    await bridge._poll_loop()

    assert polls == 2
    assert deliveries == ["retry me", "retry me", "after me"]
    assert bridge._offset == 43


@pytest.mark.asyncio
async def test_polling_consumes_explicit_permanent_negative_ack(monkeypatch):
    bridge = TelegramBridge("token", "123")
    update = {
        "update_id": 41,
        "message": {"message_id": 1, "chat": {"id": 123}, "text": "needs key"},
    }

    async def fake_api(_method, **_kwargs):
        return [update]

    async def callback(_event):
        bridge._stop.set()
        return {"ok": False, "reason": "model_api_key_required", "ack_update": True}

    bridge._callback = callback
    monkeypatch.setattr(bridge, "_api", fake_api)

    await bridge._poll_loop()

    assert bridge._offset == 42


@pytest.mark.asyncio
async def test_polling_bounds_negative_ack_and_unblocks_later_updates(monkeypatch):
    bridge = TelegramBridge("token", "123")
    first = {
        "update_id": 41,
        "message": {"message_id": 1, "chat": {"id": 123}, "text": "always fails"},
    }
    second = {
        "update_id": 42,
        "message": {"message_id": 2, "chat": {"id": 123}, "text": "must run"},
    }
    deliveries: list[tuple[str, int]] = []

    async def fake_api(_method, **_kwargs):
        return [first, second]

    async def callback(event):
        deliveries.append((event["prompt"], event["_delivery_attempt"]))
        assert event["_delivery_max_attempts"] == TELEGRAM_LOCAL_UPDATE_MAX_ATTEMPTS
        if event["prompt"] == "always fails":
            return {"ok": False, "reason": "transient", "ack_update": False}
        bridge._stop.set()
        return {"ok": True, "ack_update": True}

    async def no_delay(_seconds):
        return None

    bridge._callback = callback
    monkeypatch.setattr(bridge, "_api", fake_api)
    monkeypatch.setattr(asyncio, "sleep", no_delay)

    await bridge._poll_loop()

    assert deliveries == [
        ("always fails", 1),
        ("always fails", 2),
        ("always fails", 3),
        ("must run", 1),
    ]
    assert bridge._offset == 43
    assert bridge.failed_update_count == 1
    assert "failed after 3 local deliveries" in bridge.last_update_error


@pytest.mark.asyncio
async def test_polling_bounds_callback_exceptions_and_unblocks_queue(monkeypatch):
    bridge = TelegramBridge("token", "123")
    first = {
        "update_id": 51,
        "message": {"message_id": 1, "chat": {"id": 123}, "text": "raises"},
    }
    second = {
        "update_id": 52,
        "message": {"message_id": 2, "chat": {"id": 123}, "text": "after"},
    }
    deliveries: list[str] = []

    async def fake_api(_method, **_kwargs):
        return [first, second]

    async def callback(event):
        deliveries.append(event["prompt"])
        if event["prompt"] == "raises":
            raise RuntimeError("still broken")
        bridge._stop.set()

    async def no_delay(_seconds):
        return None

    bridge._callback = callback
    monkeypatch.setattr(bridge, "_api", fake_api)
    monkeypatch.setattr(asyncio, "sleep", no_delay)

    await bridge._poll_loop()

    assert deliveries == ["raises", "raises", "raises", "after"]
    assert bridge._offset == 53
    assert bridge.failed_update_count == 1
    assert "callback_RuntimeError" in bridge.last_update_error


@pytest.mark.asyncio
async def test_concurrent_polling_restarts_leave_exactly_one_worker(monkeypatch):
    bridge = TelegramBridge("token", "123")
    active = 0
    maximum_active = 0

    async def callback(_event):
        return None

    async def fake_poll_loop():
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(bridge, "_poll_loop", fake_poll_loop)
    await bridge.start_polling(callback)
    await asyncio.gather(*(bridge.restart_polling() for _ in range(12)))

    assert bridge._poll_task is not None
    assert not bridge._poll_task.done()
    assert maximum_active == 1

    await bridge.stop_polling()
    assert active == 0


def test_telegram_parses_text_photo_and_office_attachment():
    event = TelegramBridge.parse_update(
        {
            "update_id": 99,
            "message": {
                "message_id": 7,
                "chat": {"id": -123, "type": "supergroup"},
                "from": {"id": 456},
                "message_thread_id": 789,
                "caption": "Please inspect these files",
                "photo": [
                    {"file_id": "small", "file_size": 100},
                    {"file_id": "large", "file_size": 200},
                ],
                "document": {
                    "file_id": "ppt-file",
                    "file_name": "legacy-slides.ppt",
                    "file_size": 1234,
                },
            },
        }
    )

    assert event["chat_id"] == "-123"
    assert event["chat_type"] == "supergroup"
    assert event["sender_id"] == "456"
    assert event["message_thread_id"] == "789"
    assert event["prompt"] == "Please inspect these files"
    assert event["resources"][0]["file_id"] == "large"
    assert event["resources"][1]["file_name"] == "legacy-slides.ppt"


def test_telegram_photo_selection_uses_resolution_when_file_size_is_omitted():
    event = TelegramBridge.parse_update(
        {
            "message": {
                "message_id": 8,
                "chat": {"id": 123},
                "photo": [
                    {"file_id": "thumbnail", "width": 160, "height": 90},
                    {"file_id": "ocr-quality", "width": 1600, "height": 900},
                ],
            }
        }
    )

    assert event["resources"][0]["file_id"] == "ocr-quality"


def test_telegram_anonymous_group_sender_uses_sender_chat_identity():
    event = TelegramBridge.parse_update(
        {
            "message": {
                "message_id": 9,
                "chat": {"id": -123, "type": "supergroup"},
                "sender_chat": {"id": -456},
                "document": {"file_id": "file", "file_name": "note.txt"},
            }
        }
    )

    assert event["sender_id"] == "-456"


def test_telegram_photo_selection_uses_last_variant_without_size_metadata():
    event = TelegramBridge.parse_update(
        {
            "message": {
                "message_id": 9,
                "chat": {"id": 123},
                "photo": [
                    {"file_id": "first"},
                    {"file_id": "last"},
                ],
            }
        }
    )

    assert event["resources"][0]["file_id"] == "last"


@pytest.mark.asyncio
async def test_telegram_sends_plain_text_in_chunks_and_native_media(tmp_path: Path, monkeypatch):
    bridge = TelegramBridge("token", "123")
    calls = []

    async def fake_api(method, *, data=None, files=None, timeout=40):
        calls.append((method, data, tuple((files or {}).keys()), timeout))
        return {"message_id": len(calls)}

    monkeypatch.setattr(bridge, "_api", fake_api)
    sent = await bridge.send_text("123", "x" * 8100)
    assert sent["parts"] == 3
    assert [call[0] for call in calls] == ["sendMessage"] * 3
    assert all("parse_mode" not in call[1] for call in calls)

    image = tmp_path / "result.png"
    image.write_bytes(b"not-a-real-image-but-upload-routing-is-extension-based")
    document = tmp_path / "slides.ppt"
    document.write_bytes(b"legacy-ppt")
    audio = tmp_path / "generated-music.mp3"
    audio.write_bytes(b"audio")
    video = tmp_path / "generated-video.mp4"
    video.write_bytes(b"video")
    await bridge.send_file("123", image)
    await bridge.send_file("123", document)
    await bridge.send_file("123", audio)
    await bridge.send_file("123", video)
    assert calls[-4][0:3] == ("sendPhoto", {"chat_id": "123"}, ("photo",))
    assert calls[-3][0:3] == ("sendDocument", {"chat_id": "123"}, ("document",))
    assert calls[-2][0:3] == ("sendAudio", {"chat_id": "123"}, ("audio",))
    assert calls[-1][0:3] == ("sendVideo", {"chat_id": "123"}, ("video",))


def test_telegram_ui_and_task_pipeline_are_registered():
    root = Path(__file__).resolve().parents[1]
    index = (root / "deepdesk" / "static" / "index.html").read_text("utf-8")
    app = (root / "deepdesk" / "static" / "app.js").read_text("utf-8")
    main = (root / "deepdesk" / "main.py").read_text("utf-8")
    assert "settingTelegramBotToken" in index
    assert 'source="telegram"' in main
    assert "download_file(" in main
    assert "send_file(chat_id" in main
    assert "telegram_bot_token" in app
    assert '\"timeout\": \"3\"' in (root / "deepdesk" / "telegram.py").read_text("utf-8")
    assert "max_keepalive_connections=5" in (root / "deepdesk" / "telegram.py").read_text("utf-8")
    assert "已收到你的远程要求。附件正在接收并等待后续说明" in main
