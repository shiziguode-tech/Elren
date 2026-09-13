from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from deepdesk.config import Settings
from deepdesk.main import create_app
from deepdesk.models import AgentTask, TaskEvent

NO_STORE_HEADERS = {
    "cache-control": "no-store, max-age=0",
    "pragma": "no-cache",
    "expires": "0",
}


@pytest.mark.parametrize("extension,content", [
    ("html", b"<!doctype html><title>inert QA</title>"),
    ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
])
def test_inline_user_documents_cannot_share_app_origin(tmp_path, extension, content):
    app = _isolated_app(tmp_path)
    target = tmp_path / "screenshots" / "uploads" / f"qa.{extension}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    client = TestClient(app)
    for route in (f"/api/uploads/qa.{extension}", f"/screenshots/uploads/qa.{extension}"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.content == content
        policy = response.headers["content-security-policy"]
        assert "sandbox" in policy
        assert "allow-scripts" in policy  # interactive previews still work
        assert "allow-same-origin" not in policy
        assert "form-action 'none'" in policy
    assert "sandbox" not in client.get("/").headers["content-security-policy"]


def _isolated_app(tmp_path: Path):
    return create_app(
        Settings(
            _env_file=None,
            deepdesk_workspace=tmp_path,
            deepdesk_openclaw_enabled=False,
            deepdesk_feishu_enabled=False,
            deepseek_api_key="",
            deepseek_backup_api_key="",
            deepdesk_vision_api_key="",
            deepdesk_pollinations_api_key="",
            deepdesk_huggingface_token="",
            deepdesk_feishu_app_id="",
            deepdesk_feishu_app_secret="",
            deepdesk_feishu_open_id="",
            deepdesk_telegram_bot_token="",
            deepdesk_telegram_chat_id="",
        )
    )


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("filename,body,declared,status,code,limit", [
    ("", b"x", None, 422, "upload_missing_filename", None),
    ("qa.exe", b"x", None, 415, "upload_unsupported_format", None),
    ("qa.txt", b"", None, 422, "upload_empty_file", None),
    ("qa.txt", b"x", 26 * 1024 * 1024, 413, "upload_too_large", 25),
    ("qa.mp4", b"x", 101 * 1024 * 1024, 413, "upload_too_large", 100),
])
def test_upload_errors_follow_ui_language_and_actual_limit(
    tmp_path, language, filename, body, declared, status, code, limit,
):
    headers = {"X-Filename": filename, "Accept-Language": language}
    if declared is not None:
        headers["Content-Length"] = str(declared)
    response = TestClient(_isolated_app(tmp_path)).post("/api/uploads", content=body, headers=headers)
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    if language == "en":
        assert not re.search(r"[\u4e00-\u9fff]", detail["message"])
    else:
        assert re.search(r"[\u4e00-\u9fff]", detail["message"])
    if limit is not None:
        assert f"{limit} MB" in detail["message"]
        assert detail["limit_bytes"] == limit * 1024 * 1024


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Origin": "https://attacker.example"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "null", "Sec-Fetch-Site": "cross-site"}, 403),
        (
            {
                "Origin": "http://127.0.0.1:8765",
                "Sec-Fetch-Site": "same-origin",
            },
            200,
        ),
        (
            {
                "Origin": "http://localhost:8799",
                "Sec-Fetch-Site": "cross-site",
            },
            200,
        ),
        ({"Origin": "null", "Sec-Fetch-Site": "none"}, 200),
        ({}, 200),
    ],
)
def test_write_boundary_blocks_browser_csrf_before_side_effect(
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
):
    app = _isolated_app(tmp_path)
    app.state.scheduler.run_now = MagicMock(return_value={"ok": True, "id": "victim"})

    response = TestClient(app).post(
        "/api/schedules/victim/run",
        headers=headers,
        data={},
    )

    assert response.status_code == expected_status
    assert app.state.scheduler.run_now.call_count == (1 if expected_status == 200 else 0)


def test_no_origin_server_callback_reaches_feishu_route(tmp_path: Path):
    app = _isolated_app(tmp_path)
    client = TestClient(app)

    webhook = client.post("/api/feishu/events", json={})
    worker = client.post("/api/feishu/long-connection", json={})

    # The disabled webhook's own 410 proves the no-Origin request passed the
    # browser boundary instead of being rejected by the CSRF middleware.
    assert webhook.status_code == 410
    # The local worker's route-specific token error likewise proves that its
    # no-Origin transport remains reachable and keeps its existing auth gate.
    assert worker.status_code == 403
    assert worker.json()["detail"] == "飞书长连接工作进程校验失败"


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.example",
        "http://127.0.0.1.attacker.example",
        "http://localhost@attacker.example",
        "file://localhost/unsafe.html",
        "not a valid origin",
    ],
)
def test_origin_parser_rejects_lookalikes(tmp_path: Path, origin: str):
    app = _isolated_app(tmp_path)
    app.state.scheduler.run_now = MagicMock(return_value={"ok": True})

    response = TestClient(app).post(
        "/api/schedules/victim/run",
        headers={"Origin": origin},
    )

    assert response.status_code == 403
    app.state.scheduler.run_now.assert_not_called()


def test_every_registered_state_changing_route_has_the_same_boundary(tmp_path: Path):
    app = _isolated_app(tmp_path)
    client = TestClient(app)
    checked: list[tuple[str, str]] = []

    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = set(getattr(route, "methods", set()) or set())
        if not path.startswith("/api/"):
            continue
        concrete_path = re.sub(r"\{[^}]+\}", "boundary-probe", path)
        for method in sorted(methods & {"POST", "PUT", "PATCH", "DELETE"}):
            response = client.request(
                method,
                concrete_path,
                headers={"Origin": "https://attacker.example"},
            )
            assert response.status_code == 403, (method, path, response.text)
            checked.append((method, path))

    assert len(checked) >= 20


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8765", "localhost:8799", "[::1]:8765", "ui.localhost:8765"],
)
def test_loopback_host_variants_are_allowed(tmp_path: Path, host: str):
    app = _isolated_app(tmp_path)

    response = TestClient(app).get("/", headers={"Host": host})

    assert response.status_code == 200


def test_non_loopback_host_is_rejected_before_api_access(tmp_path: Path):
    app = _isolated_app(tmp_path)

    response = TestClient(app).get(
        "/api/settings",
        headers={"Host": "rebound.attacker.example"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Elren only accepts loopback Host headers"


@pytest.mark.parametrize("path", ["/", "/api/settings", "/missing"])
def test_security_headers_cover_pages_apis_and_errors(tmp_path: Path, path: str):
    app = _isolated_app(tmp_path)

    response = TestClient(app).get(path)

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "object-src 'none'" in csp


def _assert_no_store(response) -> None:
    for name, value in NO_STORE_HEADERS.items():
        assert response.headers[name] == value


def test_dynamic_and_private_responses_are_never_written_to_webview_cache(
    tmp_path: Path,
):
    app = _isolated_app(tmp_path)
    (tmp_path / "screenshots" / "private.png").write_bytes(b"private-screenshot")
    upload = tmp_path / "screenshots" / "uploads" / "private.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("private-upload", encoding="utf-8")
    artifact = tmp_path / "outputs" / "private.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("private-output", encoding="utf-8")

    # Prove the security boundary overrides an unsafe route-specific policy,
    # including FileResponse/mounted-static response classes used by real data.
    app.add_api_route(
        "/api/cache-policy-probe",
        lambda: Response(
            "private-api",
            headers={"Cache-Control": "private, max-age=86400"},
        ),
        methods=["GET"],
    )
    client = TestClient(app)
    paths = [
        "/",
        "/openapi.json",
        "/api/settings",
        "/api/tasks",
        "/api/cache-policy-probe",
        "/api/uploads/private.txt",
        "/api/artifacts/private.txt",
        "/screenshots/private.png",
        "/missing",
    ]

    for path in paths:
        response = client.get(path)
        assert response.status_code in {200, 404}, (path, response.text)
        _assert_no_store(response)


def test_every_registered_api_path_inherits_no_store_even_on_errors(tmp_path: Path):
    app = _isolated_app(tmp_path)
    client = TestClient(app)
    checked: list[str] = []

    for route in app.routes:
        path = str(getattr(route, "path", ""))
        if not path.startswith("/api/"):
            continue
        concrete_path = re.sub(r"\{[^}]+\}", "cache-probe", path)
        # OPTIONS avoids route side effects while exercising the real middleware
        # and FastAPI's error response for every current API path.
        response = client.options(concrete_path)
        assert response.status_code == 405, (path, response.status_code, response.text)
        _assert_no_store(response)
        checked.append(path)

    assert len(checked) >= 30


def test_versioned_static_assets_retain_normal_etag_caching(tmp_path: Path):
    app = _isolated_app(tmp_path)

    response = TestClient(app).get("/static/app.js?v=195")

    assert response.status_code == 200
    assert response.headers.get("etag")
    assert "no-store" not in response.headers.get("cache-control", "").casefold()


def test_task_json_never_exposes_remote_route_or_retired_privacy_metadata(tmp_path: Path):
    app = _isolated_app(tmp_path)
    task = AgentTask(
        prompt="private routing probe",
        source="telegram",
        remote_recipient_id="recipient-must-remain-server-side",
        remote_recipient_type="chat_id",
    )
    app.state.manager.tasks[task.id] = task

    response = TestClient(app).get(f"/api/tasks/{task.id}")

    assert response.status_code == 200
    payload = response.json()
    assert "remote_recipient_id" not in payload
    assert "remote_recipient_type" not in payload
    assert "private_chat" not in payload
    assert "privacy_session" not in payload
    assert "recipient-must-remain-server-side" not in response.text
    _assert_no_store(response)


def test_task_poll_can_return_an_inclusive_event_suffix_without_losing_updates(
    tmp_path: Path,
):
    app = _isolated_app(tmp_path)
    events = [
        TaskEvent(id="event-a", type="thinking", data={"step": 1}),
        TaskEvent(id="event-b", type="model_stream", data={"elapsed_seconds": 4}),
        TaskEvent(id="event-c", type="tool_call", data={"tool": "shell"}),
    ]
    task = AgentTask(prompt="delta probe", events=events)
    app.state.manager.tasks[task.id] = task
    client = TestClient(app)

    delta = client.get(f"/api/tasks/{task.id}?after_event_id=event-b")
    assert delta.status_code == 200
    payload = delta.json()
    assert payload["event_delta"] is True
    assert payload["event_delta_includes_cursor"] is True
    assert [event["id"] for event in payload["events"]] == ["event-b", "event-c"]
    assert payload["events"][0]["data"]["elapsed_seconds"] == 4

    # An evicted/stale cursor must recover with a complete snapshot, never a
    # misleading partial response.
    recovered = client.get(f"/api/tasks/{task.id}?after_event_id=missing-event")
    assert recovered.status_code == 200
    recovered_payload = recovered.json()
    assert "event_delta" not in recovered_payload
    assert [event["id"] for event in recovered_payload["events"]] == [
        "event-a",
        "event-b",
        "event-c",
    ]


def test_task_poll_event_suffix_avoids_resending_large_history(tmp_path: Path):
    app = _isolated_app(tmp_path)
    task = AgentTask(
        prompt="large polling payload",
        events=[
            TaskEvent(
                id=f"event-{index}",
                type="assistant",
                data={"content": f"chunk-{index}-" + ("x" * 1024)},
            )
            for index in range(200)
        ],
    )
    app.state.manager.tasks[task.id] = task
    client = TestClient(app)

    full = client.get(f"/api/tasks/{task.id}")
    delta = client.get(f"/api/tasks/{task.id}?after_event_id=event-198")

    assert full.status_code == 200
    assert delta.status_code == 200
    assert [event["id"] for event in delta.json()["events"]] == [
        "event-198",
        "event-199",
    ]
    assert len(delta.content) * 20 < len(full.content)


def test_web_client_forces_api_fetches_to_bypass_legacy_cache() -> None:
    source = Path("deepdesk/static/app.js").read_text(encoding="utf-8")

    assert source.count('cache: "no-store"') >= 2
