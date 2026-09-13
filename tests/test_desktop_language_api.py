from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepdesk.config import Settings
from deepdesk.main import create_app


@pytest.fixture
def desktop_client(tmp_path: Path):
    app = create_app(Settings(
        _env_file=None,
        deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False,
        deepdesk_feishu_enabled=False,
        deepseek_api_key="",
        deepseek_backup_api_key="",
        deepdesk_vision_api_key="",
        deepdesk_pollinations_api_key="",
        deepdesk_huggingface_token="",
        deepdesk_aicodemirror_api_key="",
        deepdesk_aicodemirror_fable_api_key="",
        deepdesk_feishu_app_id="",
        deepdesk_feishu_app_secret="",
        deepdesk_feishu_open_id="",
        deepdesk_telegram_bot_token="",
        deepdesk_telegram_chat_id="",
    ))
    # Do not enter the lifespan: these tests must not start channel listeners,
    # model discovery, a desktop shell, or any user-owned backend.
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({
        "Origin": "http://127.0.0.1:8765",
        "Sec-Fetch-Site": "same-origin",
    })
    try:
        yield client, tmp_path / "data" / "desktop-ui-language.txt"
    finally:
        client.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_desktop_language_stores_only_two_utf8_bytes(desktop_client, language):
    client, path = desktop_client
    settings_path = path.parent / "runtime-settings.json"
    settings_before = settings_path.read_bytes() if settings_path.exists() else None

    response = client.put("/api/desktop/language", json={"language": language})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "language": language}
    assert path.read_bytes() == language.encode("utf-8")
    assert (settings_path.read_bytes() if settings_path.exists() else None) == settings_before


@pytest.mark.parametrize("payload", [
    {"language": "fr"}, {"language": "EN"}, {"language": "en\n"},
    {"language": "../../anything"}, {"language": None}, {"language": 1}, {},
])
def test_invalid_language_does_not_modify_saved_preference(desktop_client, payload):
    client, path = desktop_client
    path.write_bytes(b"zh")

    response = client.put("/api/desktop/language", json=payload)

    assert response.status_code == 422
    assert path.read_bytes() == b"zh"


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "http://127.0.0.1:8765"},
    {"Sec-Fetch-Site": "same-origin"},
    {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://127.0.0.1:8766", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "https://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
])
def test_non_same_origin_callers_cannot_change_desktop_language(desktop_client, headers):
    client, path = desktop_client
    path.write_bytes(b"zh")
    client.headers.pop("Origin", None)
    client.headers.pop("Sec-Fetch-Site", None)

    response = client.put("/api/desktop/language", json={"language": "en"}, headers=headers)

    assert response.status_code == 403
    assert path.read_bytes() == b"zh"


def test_language_io_failure_has_safe_503_and_keeps_existing_value(desktop_client, monkeypatch):
    client, path = desktop_client
    path.write_bytes(b"zh")
    original_write_text = Path.write_text

    def fail_language_write(target, *args, **kwargs):
        if target == path:
            raise OSError("simulated sensitive local path " + str(path))
        return original_write_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_language_write)
    response = client.put("/api/desktop/language", json={"language": "en"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Desktop language preference could not be saved"}
    assert str(path) not in response.text
    assert path.read_bytes() == b"zh"
