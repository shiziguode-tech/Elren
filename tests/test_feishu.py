import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from deepdesk.config import Settings
from deepdesk.engine import TaskManager
from deepdesk.feishu import FeishuBridge
from deepdesk.main import (
    _cancel_and_observe_background_task,
    _monitor_feishu_connection,
    create_app,
)
from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, TaskStatus


def _isolated_feishu_app(tmp_path: Path):
    app = create_app(
        Settings(
            _env_file=None,
            deepdesk_workspace=tmp_path,
            deepdesk_openclaw_enabled=False,
            deepdesk_feishu_enabled=True,
            deepdesk_feishu_app_id="cli_test",
            deepdesk_feishu_app_secret="secret",
            deepdesk_feishu_open_id="",
            deepseek_api_key="model-key",
            deepseek_backup_api_key="",
            deepdesk_vision_api_key="",
            deepdesk_pollinations_api_key="",
            deepdesk_huggingface_token="",
            deepdesk_telegram_bot_token="",
            deepdesk_telegram_chat_id="",
        )
    )
    bridge = app.state.feishu_bridge
    bridge.verification_token = "verify"
    bridge._listener_token = "listener-token-with-at-least-32-characters"
    bridge.send_text = AsyncMock(return_value={"ok": True, "message_id": "om_ack"})
    app.state.manager.on_task_created = None

    async def unexpected_execution(*args):
        raise AssertionError("Feishu ingress tests must install their synthetic task factory")

    # A future entry-point rename must not accidentally start a real provider
    # while an ingress-only test still mocks the old factory method.
    app.state.manager.engine.run = unexpected_execution
    return app


def _install_completed_task_factory(app, *, fail_first: bool = False) -> list[dict]:
    calls: list[dict] = []

    async def create(prompt, *_args, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        if fail_first and len(calls) == 1:
            raise RuntimeError("simulated task creation failure")
        task = AgentTask(
            prompt=prompt,
            source="feishu",
            status=TaskStatus.COMPLETED,
            result="done",
            attachments=list(kwargs.get("attachments") or []),
            remote_recipient_id=str(kwargs.get("remote_recipient_id") or ""),
            remote_recipient_type=str(kwargs.get("remote_recipient_type") or "open_id"),
        )
        app.state.manager.tasks[task.id] = task
        return task

    app.state.manager.create_async = create
    return calls


def _raw_feishu_event(
    event_id: str,
    *,
    with_resource: bool = False,
    prompt: str = "process this event",
) -> dict:
    content = {"text": prompt}
    message_type = "text"
    if with_resource:
        message_type = "file"
        content.update({"file_key": "file_key_1", "file_name": "report.pdf"})
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": "verify",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_event",
                "chat_id": "oc_chat",
                "message_type": message_type,
                "content": json.dumps(content),
            },
        },
    }


def _normalized_feishu_event(event_id: str, *, with_resource: bool = False) -> dict:
    event = {
        "event_id": event_id,
        "prompt": "process this event",
        "receive_id": "ou_user",
        "receive_id_type": "open_id",
        "message_id": "om_event",
    }
    if with_resource:
        event.update(
            {
                "message_type": "file",
                "resources": [
                    {
                        "type": "file",
                        "file_key": "file_key_1",
                        "file_name": "report.pdf",
                    }
                ],
            }
        )
    return event


def test_feishu_event_challenge_and_message_normalization(tmp_path: Path):
    bridge = FeishuBridge(
        enabled=True,
        base_url="https://open.feishu.cn",
        app_id="cli_test",
        app_secret="secret",
        verification_token="verify",
        workspace=tmp_path,
    )
    assert bridge.parse_event({"challenge": "abc"}) == {"challenge": "abc"}
    event = bridge.parse_event(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt_1",
                "event_type": "im.message.receive_v1",
                "token": "verify",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_chat",
                    "content": json.dumps({"text": "打开记事本"}, ensure_ascii=False),
                },
            },
        }
    )
    assert event == {
        "event_id": "evt_1",
        "prompt": "打开记事本",
        "receive_id": "ou_user",
        "receive_id_type": "open_id",
        "message_id": "om_1",
    }
    repeated = bridge.parse_event(
        {
            "header": {
                "event_id": "evt_1",
                "event_type": "im.message.receive_v1",
                "token": "verify",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_chat",
                    "content": json.dumps({"text": "打开记事本"}, ensure_ascii=False),
                },
            },
        }
    )
    assert repeated == event


def test_worker_does_not_consume_event_id_before_loopback_callback_succeeds(
    tmp_path: Path,
):
    bridge = FeishuBridge(
        True, "https://open.feishu.cn", "cli_test", "secret", workspace=tmp_path
    )
    payload = {
        "header": {"event_id": "evt_retry", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}},
            "message": {"content": json.dumps({"text": "retry me"})},
        },
    }

    first = bridge.parse_event(payload, mark_seen=False)
    second = bridge.parse_event(payload, mark_seen=False)

    assert first == second
    reservation = bridge.reserve_event_id("evt_retry")
    assert reservation
    assert bridge.rollback_event_id("evt_retry", reservation) is True
    retry_reservation = bridge.reserve_event_id("evt_retry")
    assert retry_reservation
    assert bridge.commit_event_id("evt_retry", retry_reservation) is True
    assert bridge.reserve_event_id("evt_retry") is None


def test_feishu_event_commit_survives_bridge_restart(tmp_path: Path):
    first = FeishuBridge(
        True, "https://open.feishu.cn", "cli_test", "secret", workspace=tmp_path
    )
    reservation = first.reserve_event_id("evt_persisted")

    assert reservation
    assert first.commit_event_id("evt_persisted", reservation) is True
    assert (tmp_path / "data" / "feishu-event-dedup.json").is_file()

    restarted = FeishuBridge(
        True, "https://open.feishu.cn", "cli_test", "secret", workspace=tmp_path
    )
    assert restarted.reserve_event_id("evt_persisted") is None


def test_feishu_event_reservation_rollback_and_ttl_release(
    tmp_path: Path,
    monkeypatch,
):
    clock = {"now": 100.0}
    monkeypatch.setattr("deepdesk.feishu.time.monotonic", lambda: clock["now"])
    bridge = FeishuBridge(
        True, "https://open.feishu.cn", "cli_test", "secret", workspace=tmp_path
    )

    first = bridge.reserve_event_id("evt_ttl", ttl_seconds=5)
    assert first
    assert bridge.reserve_event_id("evt_ttl", ttl_seconds=5) is None
    assert bridge.rollback_event_id("evt_ttl", "not-the-owner") is False
    assert bridge.rollback_event_id("evt_ttl", first) is True

    second = bridge.reserve_event_id("evt_ttl", ttl_seconds=5)
    assert second and second != first
    clock["now"] += 6
    third = bridge.reserve_event_id("evt_ttl", ttl_seconds=5)
    assert third and third != second
    assert bridge.rollback_event_id("evt_ttl", second) is False
    assert bridge.rollback_event_id("evt_ttl", third) is True


@pytest.mark.asyncio
async def test_feishu_http_download_failure_rolls_back_and_retry_commits(
    tmp_path: Path,
):
    app = _isolated_feishu_app(tmp_path)
    bridge = app.state.feishu_bridge
    task_calls = _install_completed_task_factory(app)
    download_calls = 0

    async def download(*_args, **_kwargs):
        nonlocal download_calls
        download_calls += 1
        if download_calls == 1:
            raise RuntimeError("temporary download failure")
        target = tmp_path / "downloaded-report.pdf"
        target.write_bytes(b"pdf")
        return target

    bridge.download_message_resource = download
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        first = await client.post(
            "/api/feishu/events",
            json=_raw_feishu_event("evt_download_retry", with_resource=True),
        )
        retry = await client.post(
            "/api/feishu/events",
            json=_raw_feishu_event("evt_download_retry", with_resource=True),
        )
        duplicate = await client.post(
            "/api/feishu/events",
            json=_raw_feishu_event("evt_download_retry", with_resource=True),
        )

    assert first.status_code == 200
    assert first.json() == {"ok": False, "reason": "attachment_download_failed"}
    assert retry.status_code == 200
    assert retry.json()["ok"] is True
    assert duplicate.json() == {
        "ok": True,
        "ignored": True,
        "reason": "duplicate_event",
    }
    assert download_calls == 2
    assert len(task_calls) == 1


@pytest.mark.asyncio
async def test_attachment_only_event_commits_after_delayed_task_creation(
    tmp_path: Path,
):
    app = _isolated_feishu_app(tmp_path)
    bridge = app.state.feishu_bridge
    app.state.pending_feishu_attachments.delay_seconds = 0.02
    task_calls = _install_completed_task_factory(app)

    async def download(*_args, **_kwargs):
        target = tmp_path / "pending-report.pdf"
        target.write_bytes(b"pdf")
        return target

    bridge.download_message_resource = download
    dedupe_path = tmp_path / "data" / "feishu-event-dedup.json"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        accepted = await client.post(
            "/api/feishu/events",
            json=_raw_feishu_event(
                "evt_pending_commit", with_resource=True, prompt=""
            ),
        )
        assert accepted.json()["pending_attachments"] is True
        assert not dedupe_path.exists()
        await asyncio.sleep(0.08)

    assert len(task_calls) == 1
    restarted = FeishuBridge(
        True, "https://open.feishu.cn", "cli_test", "secret", workspace=tmp_path
    )
    assert restarted.reserve_event_id("evt_pending_commit") is None


@pytest.mark.asyncio
async def test_feishu_websocket_task_creation_failure_rolls_back_for_retry(
    tmp_path: Path,
):
    app = _isolated_feishu_app(tmp_path)
    task_calls = _install_completed_task_factory(app, fail_first=True)
    headers = {"X-Elren-Feishu-Token": app.state.feishu_bridge.listener_token}
    event = _normalized_feishu_event("evt_create_retry")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        first = await client.post(
            "/api/feishu/long-connection", json=event, headers=headers
        )
        retry = await client.post(
            "/api/feishu/long-connection", json=event, headers=headers
        )
        duplicate = await client.post(
            "/api/feishu/long-connection", json=event, headers=headers
        )

    assert first.status_code == 500
    assert retry.status_code == 200
    assert retry.json()["ok"] is True
    assert duplicate.json() == {
        "ok": True,
        "ignored": True,
        "reason": "duplicate_event",
    }
    assert len(task_calls) == 2


@pytest.mark.asyncio
async def test_feishu_http_and_websocket_concurrency_creates_at_most_one_task(
    tmp_path: Path,
):
    app = _isolated_feishu_app(tmp_path)
    bridge = app.state.feishu_bridge
    task_calls = _install_completed_task_factory(app)
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    download_calls = 0

    async def delayed_download(*_args, **_kwargs):
        nonlocal download_calls
        download_calls += 1
        download_started.set()
        await release_download.wait()
        target = tmp_path / "concurrent-report.pdf"
        target.write_bytes(b"pdf")
        return target

    bridge.download_message_resource = delayed_download
    headers = {"X-Elren-Feishu-Token": bridge.listener_token}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        websocket_request = asyncio.create_task(
            client.post(
                "/api/feishu/long-connection",
                json=_normalized_feishu_event("evt_concurrent", with_resource=True),
                headers=headers,
            )
        )
        await asyncio.wait_for(download_started.wait(), timeout=2)
        http_duplicate = await client.post(
            "/api/feishu/events",
            json=_raw_feishu_event("evt_concurrent", with_resource=True),
        )
        release_download.set()
        websocket_result = await asyncio.wait_for(websocket_request, timeout=2)

    assert websocket_result.status_code == 200
    assert websocket_result.json()["ok"] is True
    assert http_duplicate.json() == {
        "ok": True,
        "ignored": True,
        "reason": "duplicate_event",
    }
    assert download_calls == 1
    assert len(task_calls) == 1


def test_feishu_image_message_is_normalized_as_a_downloadable_resource():
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    event = bridge.parse_event(
        {
            "header": {"event_id": "evt_image", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_image",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img_key_1"}),
                },
            },
        },
        mark_seen=False,
    )

    assert event["prompt"] == ""
    assert event["message_type"] == "image"
    assert event["resources"] == [
        {"type": "image", "file_key": "img_key_1", "file_name": ""}
    ]


@pytest.mark.parametrize("file_name", ["report.pdf", "report.doc", "report.docx", "slides.pptx", "data.xlsx", "notes.txt"])
def test_feishu_document_message_is_normalized_as_a_downloadable_resource(file_name: str):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    event = bridge.parse_event(
        {
            "header": {"event_id": f"evt_{file_name}", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_file",
                    "message_type": "file",
                    "content": json.dumps({"file_key": "file_key_1", "file_name": file_name}),
                },
            },
        },
        mark_seen=False,
    )

    assert event["prompt"] == ""
    assert event["message_type"] == "file"
    assert event["resources"] == [
        {"type": "file", "file_key": "file_key_1", "file_name": file_name}
    ]


@pytest.mark.parametrize(
    ("message_type", "content", "expected_name"),
    [
        ("audio", {"file_key": "audio_key", "duration": 2000}, "feishu-audio.opus"),
        ("media", {"file_key": "video_key", "file_name": "clip.mp4", "duration": 6000}, "clip.mp4"),
    ],
)
def test_feishu_audio_and_video_are_normalized_as_downloadable_resources(
    message_type: str, content: dict, expected_name: str
):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    event = bridge.parse_event(
        {
            "header": {"event_id": f"evt_{message_type}", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_media",
                    "message_type": message_type,
                    "content": json.dumps(content),
                },
            },
        },
        mark_seen=False,
    )

    assert event["message_type"] == message_type
    assert event["resources"] == [
        {"type": message_type, "file_key": content["file_key"], "file_name": expected_name}
    ]


@pytest.mark.asyncio
async def test_incoming_feishu_image_is_downloaded_inside_attachment_directory(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    captured = {}

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        content = b"fake-png"
        headers = {"content-type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield self.content

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs["params"]
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    target = await bridge.download_message_resource(
        "om/unsafe",
        "img key",
        "image",
        tmp_path / "attachments",
    )

    assert target.read_bytes() == b"fake-png"
    assert target.suffix == ".png"
    assert target.parent == (tmp_path / "attachments").resolve()
    assert "/om%2Funsafe/resources/img%20key" in captured["url"]
    assert captured["params"] == {"type": "image"}


@pytest.mark.asyncio
async def test_incoming_feishu_document_keeps_its_safe_file_name(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    captured = {}

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        content = b"document-bytes"
        headers = {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield self.content

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method, _url, **kwargs):
            captured["params"] = kwargs["params"]
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    target = await bridge.download_message_resource(
        "om_file",
        "file_key",
        "file",
        tmp_path / "attachments",
        file_name="季度报告.docx",
    )

    assert target.name == "季度报告.docx"
    assert target.read_bytes() == b"document-bytes"
    assert captured["params"] == {"type": "file"}


@pytest.mark.asyncio
async def test_incoming_feishu_attachment_stops_at_local_size_limit(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        headers = {"content-type": "application/octet-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield b"1234"
            yield b"5678"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    with pytest.raises(RuntimeError, match="download limit"):
        await bridge.download_message_resource(
            "om_file",
            "file_key",
            "file",
            tmp_path / "attachments",
            file_name="large.bin",
            max_bytes=6,
        )
    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
async def test_incoming_feishu_attachment_ignores_malformed_optional_size_header(
    monkeypatch, tmp_path: Path
):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        headers = {"content-type": "text/plain", "content-length": "not-a-number"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield b"valid body"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    target = await bridge.download_message_resource(
        "om_file", "file_key", "file", tmp_path / "attachments", file_name="note.txt"
    )

    assert target.read_bytes() == b"valid body"


def test_feishu_event_rejects_wrong_verification_token():
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret", "verify")
    with pytest.raises(PermissionError):
        bridge.parse_event({"header": {"event_type": "im.message.receive_v1", "token": "wrong"}})


@pytest.mark.asyncio
async def test_unconfigured_feishu_status_is_safe():
    bridge = FeishuBridge(True, "https://open.feishu.cn", "", "")
    status = await bridge.status(probe=True)
    assert status["configured"] is False
    assert status["ready"] is False


def test_long_connection_log_rotation_is_bounded_and_keeps_recent_tail(tmp_path: Path):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        workspace=tmp_path,
    )
    log_path = tmp_path / "data" / "feishu-long-connection.log"
    log_path.parent.mkdir(parents=True)
    recent = b"r" * (1024 * 1024)
    log_path.write_bytes(b"old" * (2 * 1024 * 1024) + recent)

    bridge._rotate_listener_log(log_path)

    assert log_path.read_bytes() == b""
    assert log_path.with_name(f"{log_path.name}.1").read_bytes() == recent


@pytest.mark.asyncio
async def test_long_connection_only_uses_app_credentials(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")

    class FakeProcess:
        returncode = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    async def fake_get_tenant_token():
        return "tenant-token"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge, "_get_tenant_token", fake_get_tenant_token)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    assert await bridge.start_long_connection(
        "http://127.0.0.1:8765/api/feishu/long-connection"
    )
    assert captured["args"][-1] == "deepdesk.feishu_ws_worker"
    assert captured["env"]["ELREN_FEISHU_APP_ID"] == "cli_test"
    assert captured["env"]["ELREN_FEISHU_APP_SECRET"] == "secret"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert bridge.validate_listener_token(captured["env"]["ELREN_FEISHU_LISTENER_TOKEN"])
    assert (await bridge.status())["long_connection_running"] is True

    await bridge.stop_long_connection()
    assert bridge.listener_token == ""


def test_stale_listener_cleanup_is_scoped_to_exact_workspace(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        workspace=tmp_path / "current",
    )
    expected = str(bridge._listener_status_path)

    class FakeProcess:
        def __init__(self, pid: int, status_path: str):
            self.pid = pid
            self.info = {
                "pid": pid,
                "cmdline": ["python", "-m", "deepdesk.feishu_ws_worker"],
            }
            self.status_path = status_path
            self.terminated = False
            self.killed = False

        def environ(self):
            return {"ELREN_FEISHU_STATUS_PATH": self.status_path}

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    matching = FakeProcess(101, expected)
    unrelated = FakeProcess(102, str(tmp_path / "other" / "status.json"))
    monkeypatch.setattr(
        "deepdesk.feishu.psutil.process_iter",
        lambda _attrs: [matching, unrelated],
    )
    monkeypatch.setattr(
        "deepdesk.feishu.psutil.wait_procs",
        lambda processes, timeout: (list(processes), []),
    )

    assert bridge._terminate_stale_listeners() == 1
    assert matching.terminated is True
    assert unrelated.terminated is False


@pytest.mark.asyncio
async def test_concurrent_listener_restarts_leave_only_one_worker(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        workspace=tmp_path,
    )
    processes = []

    class FakeProcess:
        returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    async def fake_get_tenant_token():
        return "tenant-token"

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_get_tenant_token)
    monkeypatch.setattr(bridge, "_terminate_stale_listeners", lambda: 0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    results = await asyncio.gather(
        bridge.start_long_connection("http://127.0.0.1:8765/callback"),
        bridge.start_long_connection("http://127.0.0.1:8765/callback"),
    )

    assert results == [True, True]
    assert len(processes) == 2
    assert processes[0].returncode == 0
    assert processes[1].returncode is None
    assert bridge._listener is processes[1]
    await bridge.stop_long_connection()


def test_long_connection_listener_token_survives_service_restart(tmp_path: Path):
    first = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    first._listener_token_path = tmp_path / "feishu-listener-token"
    token = first._persistent_listener_token()

    second = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    second._listener_token_path = tmp_path / "feishu-listener-token"

    assert len(token) >= 32
    assert second._persistent_listener_token() == token


def test_listener_token_migrates_to_os_vault_and_scrubs_legacy_file(tmp_path: Path):
    legacy = tmp_path / "feishu-listener-token"
    legacy.write_text("legacy-listener-token-012345678901234567890123\n", encoding="utf-8")
    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")
    bridge._listener_token_path = legacy

    token = bridge._persistent_listener_token()

    assert token.startswith("legacy-listener-token-")
    assert not legacy.exists()
    vault = legacy.with_name("feishu-listener-token.vault")
    assert vault.read_bytes().startswith(bytes.fromhex("454c52454e564c5401"))
    assert token.encode() not in vault.read_bytes()


def test_settings_ui_only_requests_feishu_app_id_and_secret():
    root = Path(__file__).resolve().parents[1]
    index = (root / "deepdesk" / "static" / "index.html").read_text(encoding="utf-8")
    app = (root / "deepdesk" / "static" / "app.js").read_text(encoding="utf-8")

    assert "settingFeishuAppId" in index
    assert "settingFeishuAppSecret" in index
    assert "settingFeishuOpenId" in index
    assert "settingFeishuVerificationToken" not in index + app
    assert "settingFeishuEncryptKey" not in index + app


def test_long_connection_loopback_callback_bypasses_system_proxy():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deepdesk" / "feishu_ws_worker.py").read_text(encoding="utf-8")

    assert "trust_env=False" in worker
    assert "callback_url" in worker
    assert "client._reconnect_interval = 5" in worker
    assert "feishu-status-heartbeat" in worker
    assert "mark_seen=False" in worker
    assert "timeout=90" in worker


def test_main_process_supervises_a_stale_or_crashed_feishu_worker():
    root = Path(__file__).resolve().parents[1]
    main = (root / "deepdesk" / "main.py").read_text(encoding="utf-8")

    assert "async def _monitor_feishu_connection(" in main
    assert "unhealthy_checks < threshold" in main
    assert "await bridge.restart_long_connection()" in main
    assert "Feishu connection monitor will retry (%s)" in main


@pytest.mark.asyncio
async def test_feishu_monitor_retries_after_restart_exception_without_leaking_it(caplog):
    recovered = asyncio.Event()

    class FlakyBridge:
        restart_calls = 0

        async def status(self, probe=False):
            assert probe is False
            return {"configured": True, "ready": False}

        async def restart_long_connection(self):
            self.restart_calls += 1
            if self.restart_calls == 1:
                raise RuntimeError(
                    "https://open.feishu.cn/open-apis/token?token=private-monitor-token"
                )
            recovered.set()
            return True

    bridge = FlakyBridge()
    task = asyncio.create_task(
        _monitor_feishu_connection(
            bridge,
            check_interval_seconds=0,
            unhealthy_threshold=1,
            failure_backoff_seconds=0.001,
            max_failure_backoff_seconds=0.002,
        )
    )
    await asyncio.wait_for(recovered.wait(), timeout=1)

    assert bridge.restart_calls >= 2
    assert not task.done()
    task.cancel()
    assert isinstance(
        (await asyncio.gather(task, return_exceptions=True))[0],
        asyncio.CancelledError,
    )
    assert "private-monitor-token" not in caplog.text
    assert "https://open.feishu.cn" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_normal_feishu_monitor_cancel_is_not_reported_as_a_failure(caplog):
    class IdleBridge:
        async def status(self, probe=False):
            return {"configured": False, "ready": False}

        async def restart_long_connection(self):
            raise AssertionError("an unconfigured bridge must not restart")

    task = asyncio.create_task(
        _monitor_feishu_connection(IdleBridge(), check_interval_seconds=60)
    )
    await asyncio.sleep(0)

    failure = await _cancel_and_observe_background_task(
        task,
        component="feishu-connection-monitor",
    )

    assert failure is None
    assert "Background task failed before shutdown" not in caplog.text


@pytest.mark.asyncio
async def test_failed_feishu_monitor_does_not_skip_remaining_shutdown_cleanup(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    failed = asyncio.Event()
    calls: list[str] = []
    sensitive_error = "https://open.feishu.cn/open-apis/token?token=private-shutdown-token"

    async def failed_monitor(_bridge):
        failed.set()
        raise RuntimeError(sensitive_error)

    async def no_start(*_args, **_kwargs):
        return False

    def cleanup(name):
        async def run():
            calls.append(name)

        return run

    monkeypatch.setattr("deepdesk.main._monitor_feishu_connection", failed_monitor)
    app = _isolated_feishu_app(tmp_path)
    app.state.scheduler.start = no_start
    app.state.feishu_bridge.start_long_connection = no_start
    app.state.telegram_bridge.start_polling = no_start
    app.state.feishu_bridge.stop_long_connection = cleanup("feishu")
    app.state.telegram_bridge.stop_polling = cleanup("telegram")
    app.state.scheduler.stop = cleanup("scheduler")
    app.state.manager.shutdown = cleanup("manager")
    app.state.mobile_bridge.stop = cleanup("mobile")
    app.state.openclaw_bridge.stop = cleanup("openclaw")

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(failed.wait(), timeout=1)
        await asyncio.sleep(0)

    assert calls == [
        "feishu",
        "telegram",
        "scheduler",
        "manager",
        "mobile",
        "openclaw",
    ]
    audit_text = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
    assert "shutdown_background_task_failed" in audit_text
    assert "feishu-connection-monitor" in audit_text
    assert "RuntimeError" in audit_text
    assert "private-shutdown-token" not in audit_text + caplog.text
    assert "https://open.feishu.cn" not in audit_text + caplog.text


@pytest.mark.asyncio
async def test_shutdown_cleanup_failure_is_audited_after_all_components_run(
    tmp_path: Path,
    caplog,
):
    calls: list[str] = []
    sensitive_error = "https://open.feishu.cn/open-apis/token?token=cleanup-private-token"

    async def no_start(*_args, **_kwargs):
        return False

    async def failed_feishu_cleanup():
        calls.append("feishu")
        raise RuntimeError(sensitive_error)

    def cleanup(name):
        async def run():
            calls.append(name)

        return run

    app = _isolated_feishu_app(tmp_path)
    app.state.scheduler.start = no_start
    app.state.feishu_bridge.start_long_connection = no_start
    app.state.telegram_bridge.start_polling = no_start
    app.state.feishu_bridge.stop_long_connection = failed_feishu_cleanup
    app.state.telegram_bridge.stop_polling = cleanup("telegram")
    app.state.scheduler.stop = cleanup("scheduler")
    app.state.manager.shutdown = cleanup("manager")
    app.state.mobile_bridge.stop = cleanup("mobile")
    app.state.openclaw_bridge.stop = cleanup("openclaw")

    with pytest.raises(RuntimeError, match="feishu-listener \\(RuntimeError\\)") as caught:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.01)

    assert calls == [
        "feishu",
        "telegram",
        "scheduler",
        "manager",
        "mobile",
        "openclaw",
    ]
    audit_text = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
    assert "shutdown_cleanup_failed" in audit_text
    assert "feishu-listener" in audit_text
    assert "RuntimeError" in audit_text
    combined = audit_text + caplog.text + str(caught.value)
    assert "cleanup-private-token" not in combined
    assert "https://open.feishu.cn" not in combined


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,recipient_type", [("feishu", "open_id"), ("telegram", "chat_id")])
async def test_interrupted_feishu_tasks_are_recovered_after_restart(channel, recipient_type):
    root = Path(__file__).resolve().parents[1]
    main = (root / "deepdesk" / "main.py").read_text(encoding="utf-8")

    assert "async def recover_interrupted_feishu_tasks()" in main
    assert "await recover_interrupted_feishu_tasks()" in main
    assert "服务重启后自动恢复" in main
    assert main.count(
        "discussion_team_enabled=previous.discussion_team_enabled"
    ) == 2
    assert main.count("for member in previous.discussion_team") >= 2

    class SyntheticEngine:
        async def run(self, task, cancel, queue):
            task.status = TaskStatus.COMPLETED

    manager = TaskManager(SyntheticEngine(), None)
    previous = AgentTask(prompt="Synthetic interrupted task", status=TaskStatus.FAILED,
                         source=channel, remote_recipient_id="synthetic-recipient",
                         remote_recipient_type=recipient_type)
    manager.tasks[previous.id] = previous
    resumed = await manager.continue_from_async(previous, "Recover", ApprovalPolicy.AUTONOMOUS,
                                               AgentProfile.GENERAL)
    await asyncio.gather(*list(manager.background.values()))
    assert resumed.source == channel and resumed.parent_task_id == previous.id
    assert manager.remote_delivery_route(resumed) == ("synthetic-recipient", recipient_type)


@pytest.mark.asyncio
async def test_saved_open_id_is_used_as_default_recipient(monkeypatch):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_default",
    )
    captured = {}

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"message_id": "om_test"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    literal_text = "**需要原样保留的字符** [链接](https://example.com)"
    result = await bridge.send_text("", literal_text)

    assert result == {"ok": True, "message_id": "om_test"}
    assert "receive_id_type=open_id" in captured["url"]
    assert captured["json"]["receive_id"] == "ou_default"
    assert json.loads(captured["json"]["content"])["text"] == literal_text


@pytest.mark.asyncio
async def test_feishu_send_surfaces_official_permission_error(monkeypatch):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_default",
    )

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 400
        reason_phrase = "Bad Request"

        def json(self):
            return {"code": 99991672, "msg": "required scope: im:message:send_as_bot"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    with pytest.raises(RuntimeError, match="99991672.*im:message:send_as_bot"):
        await bridge.send_text("", "hello")


@pytest.mark.asyncio
async def test_feishu_send_retries_transient_network_error_without_changing_text(monkeypatch):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_default",
    )
    calls = 0
    sent_texts = []

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        def json(self):
            return {"code": 0, "data": {"message_id": "om_retried"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            nonlocal calls
            calls += 1
            sent_texts.append(json.loads(kwargs["json"]["content"])["text"])
            if calls == 1:
                raise httpx.ConnectError("temporary disconnect")
            return FakeResponse()

    async def no_wait(_seconds):
        return None

    import httpx

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr("deepdesk.feishu.asyncio.sleep", no_wait)

    literal_text = "**不要转换** [原始内容](literal)"
    result = await bridge.send_text("", literal_text)

    assert result == {"ok": True, "message_id": "om_retried"}
    assert calls == 2
    assert sent_texts == [literal_text, literal_text]


@pytest.mark.asyncio
async def test_feishu_image_is_uploaded_then_sent_as_native_image_message(monkeypatch, tmp_path: Path):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_default",
    )
    image_path = tmp_path / "answer.png"
    image_path.write_bytes(b"fake-image")
    calls = []

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/open-apis/im/v1/images"):
                assert kwargs["data"] == {"image_type": "message"}
                assert kwargs["files"]["image"][0] == "answer.png"
                return FakeResponse({"code": 0, "data": {"image_key": "img_uploaded"}})
            assert kwargs["json"]["msg_type"] == "image"
            assert json.loads(kwargs["json"]["content"]) == {"image_key": "img_uploaded"}
            return FakeResponse({"code": 0, "data": {"message_id": "om_image_sent"}})

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    result = await bridge.send_image("", image_path)

    assert result == {"ok": True, "message_id": "om_image_sent"}
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "expected_type"),
    [
        ("answer.pdf", "pdf"),
        ("answer.doc", "doc"),
        ("answer.docx", "doc"),
        ("answer.pptx", "ppt"),
        ("answer.xlsx", "xls"),
        ("answer.txt", "stream"),
        ("answer.mp3", "stream"),
        ("answer.opus", "opus"),
        ("answer.mp4", "stream"),
    ],
)
async def test_feishu_document_is_uploaded_then_sent_as_native_file_message(
    monkeypatch, tmp_path: Path, file_name: str, expected_type: str
):
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_default",
    )
    file_path = tmp_path / file_name
    file_path.write_bytes(b"native-file-content")
    calls = []

    async def fake_token():
        return "tenant-token"

    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/open-apis/im/v1/files"):
                assert kwargs["data"] == {"file_type": expected_type, "file_name": file_name}
                assert kwargs["files"]["file"][0] == file_name
                return FakeResponse({"code": 0, "data": {"file_key": "file_uploaded"}})
            assert kwargs["json"]["msg_type"] == "file"
            assert json.loads(kwargs["json"]["content"]) == {"file_key": "file_uploaded"}
            return FakeResponse({"code": 0, "data": {"message_id": "om_file_sent"}})

    monkeypatch.setattr(bridge, "_get_tenant_token", fake_token)
    monkeypatch.setattr("deepdesk.feishu.httpx.AsyncClient", lambda **_kwargs: FakeClient())

    result = await bridge.send_file("", file_path)

    assert result == {"ok": True, "message_id": "om_file_sent"}
    assert len(calls) == 2


def test_feishu_task_pipeline_passes_downloaded_attachments_and_relays_result_files():
    root = Path(__file__).resolve().parents[1]
    main = (root / "deepdesk" / "main.py").read_text(encoding="utf-8")
    engine = (root / "deepdesk" / "engine.py").read_text(encoding="utf-8")

    assert "download_message_resource" in main
    assert "attachments=attachments" in main
    assert "feishu_result_image_paths(task)" in main
    assert "await feishu_bridge.send_image" in main
    assert "feishu_result_document_paths(task)" in main
    assert "await feishu_bridge.send_file" in main
    assert "PendingFeishuAttachments(delay_seconds=15)" in main
    assert "pending_feishu_attachments.take(conversation_key)" in main
    assert "已收到你的远程要求。附件正在接收并等待后续说明" in main
    assert "acknowledge=False" in main
    assert "本地图片或文档完整路径" in engine


def test_launcher_removes_orphaned_feishu_workers_before_starting():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "start.ps1").read_text(encoding="utf-8")

    assert "function Stop-StaleElrenWorkers" in launcher
    assert "deepdesk\\.feishu_ws_worker" in launcher
    assert "Stop-StaleElrenWorkers" in launcher


def test_authenticated_message_can_refresh_default_open_id():
    bridge = FeishuBridge(
        True,
        "https://open.feishu.cn",
        "cli_test",
        "secret",
        default_receive_id="ou_old_app",
    )

    bridge.remember_default_receive_id(" ou_current_app ")

    assert bridge.default_receive_id == "ou_current_app"

def test_feishu_default_workspace_does_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    bridge = FeishuBridge(True, "https://open.feishu.cn", "cli_test", "secret")

    assert bridge.workspace == Path(__file__).resolve().parents[1]
