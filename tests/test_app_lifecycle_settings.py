import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from deepdesk.config import Settings
from deepdesk.main import _referenced_output_paths, create_app
from deepdesk.models import AgentTask, TaskStatus
from deepdesk.secret_storage import LocalSecretVault
from deepdesk.speech_transcription import SUPPORTED_SPEECH_LANGUAGES
from deepdesk.task_store import TaskStore


def _isolated_app(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
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
    return create_app(settings)


def test_app_uses_lifespan_without_deprecated_event_handlers(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)

    assert app.router.on_startup == []
    assert app.router.on_shutdown == []
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        status = client.get("/api/status")

    assert status.status_code == 200
    assert status.json()["default_model"] == "auto"
    assert status.json()["model"]


def test_app_migrates_openclaw_provider_key_to_dedicated_vault_field(
    tmp_path: Path, monkeypatch
):
    catalog = (
        tmp_path
        / "data"
        / "openclaw"
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {"providers": {"deepseek": {"apiKey": "openclaw-only-secret"}}}
        ),
        encoding="utf-8",
    )

    app = _isolated_app(tmp_path, monkeypatch)

    vault = tmp_path / "data" / "provider-secrets.vault"
    assert LocalSecretVault.is_encrypted_bytes(vault.read_bytes())
    assert b"openclaw-only-secret" not in vault.read_bytes()
    assert (
        app.state.provider_secrets.value.openclaw_deepseek_api_key
        == "openclaw-only-secret"
    )
    assert app.state.openclaw_bridge.provider_env == {
        "DEEPSEEK_API_KEY": "openclaw-only-secret"
    }
    with TestClient(app) as client:
        payload = client.get("/api/settings").json()
        assert payload["openclaw_deepseek_key_configured"] is True
    scrubbed = json.loads(catalog.read_text(encoding="utf-8"))
    assert "apiKey" not in scrubbed["providers"]["deepseek"]


def test_artifact_folder_index_is_nonblocking_and_refreshes_in_background(
    tmp_path, monkeypatch
):
    folder = tmp_path / "outputs" / "large-result"
    folder.mkdir(parents=True)
    expected_size = 0
    for index in range(75):
        payload = f"artifact-{index}".encode()
        (folder / f"item-{index}.txt").write_bytes(payload)
        expected_size += len(payload)

    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        first = client.get("/api/artifacts")
        assert first.status_code == 200
        assert first.json()["summary_pending"] is True
        deadline = time.monotonic() + 5
        payload = first.json()
        while payload["summary_pending"] and time.monotonic() < deadline:
            time.sleep(0.02)
            payload = client.get("/api/artifacts").json()

        assert payload["summary_pending"] is False
        folder_entry = next(
            item for item in payload["artifacts"] if item["name"] == "large-result"
        )
        assert folder_entry["file_count"] == 75
        assert folder_entry["size"] == expected_size


def test_artifact_index_hides_internal_cache_folders(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    project = outputs / "project"
    cache = project / "__pycache__"
    cache.mkdir(parents=True)
    (project / "result.txt").write_text("visible", encoding="utf-8")
    (cache / "module.pyc").write_bytes(b"hidden-cache")
    (outputs / ".pytest_cache").mkdir(parents=True)

    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        payload = client.get("/api/artifacts").json()
        while payload["summary_pending"] and time.monotonic() < deadline:
            time.sleep(0.02)
            payload = client.get("/api/artifacts").json()

    names = {item["name"] for item in payload["artifacts"]}
    assert ".pytest_cache" not in names
    project_entry = next(item for item in payload["artifacts"] if item["name"] == "project")
    assert project_entry["file_count"] == 1
    assert project_entry["size"] == len("visible")


def test_desktop_mobile_unpair_is_idempotent_for_stale_settings_cards(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    bridge = app.state.mobile_bridge
    device_id = "android-stale-card"

    with TestClient(app) as client:
        bridge._records[device_id] = {
            "device_id": device_id,
            "name": "Phone",
            "paired_at": 1.0,
        }
        first = client.delete(f"/api/mobile/devices/{device_id}")
        second = client.delete(f"/api/mobile/devices/{device_id}")

    assert first.status_code == 200
    assert first.json()["revoked"] is True
    assert first.json()["already_unpaired"] is False
    assert second.status_code == 200
    assert second.json()["revoked"] is False
    assert second.json()["already_unpaired"] is True


def test_configured_workspace_owns_backend_runtime_paths(tmp_path, monkeypatch):
    host_cwd = tmp_path / "unrelated-current-directory"
    workspace = tmp_path / "package-workspace"
    host_cwd.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(host_cwd)
    app = create_app(
        Settings(
            _env_file=None,
            deepdesk_workspace=workspace,
            deepdesk_openclaw_enabled=False,
            deepdesk_feishu_enabled=False,
        )
    )

    assert app.state.openclaw_bridge.workspace.resolve() == workspace.resolve()
    assert app.state.mcp_runtime.workspace == workspace.resolve()
    assert app.state.feishu_bridge.workspace == workspace.resolve()
    assert (workspace / "plugins").is_dir()
    assert not (host_cwd / "plugins").exists()


def test_remote_result_attachments_are_limited_to_workspace_outputs(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "outputs" / "report.txt"
    private = workspace / "data" / "provider-keys.txt"
    outside = tmp_path / "outside-secret.txt"
    output.parent.mkdir(parents=True)
    private.parent.mkdir(parents=True)
    output.write_text("public artifact", encoding="utf-8")
    private.write_text("private", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    task = SimpleNamespace(
        result=f"完成：outputs/report.txt\n内部：{private}\n外部：{outside}",
        attachments=[],
    )

    found = _referenced_output_paths(task, workspace, ("txt",), max_bytes=1024)

    assert found == [output.resolve()]


def test_ordinary_settings_save_does_not_restart_message_channels(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    feishu_restart = AsyncMock(return_value=True)
    telegram_restart = AsyncMock(return_value=True)
    app.state.feishu_bridge.restart_long_connection = feishu_restart
    app.state.telegram_bridge.restart_polling = telegram_restart

    with TestClient(app) as client:
        response = client.patch("/api/settings", json={"compact_trace": False})

    assert response.status_code == 200
    feishu_restart.assert_not_awaited()
    telegram_restart.assert_not_awaited()


def test_retry_telegram_delivery_uses_decrypted_durable_route_without_revealing_it(
    tmp_path: Path,
    monkeypatch,
):
    app = _isolated_app(tmp_path, monkeypatch)
    recipient = "telegram-retry-private-route-811902"
    task = AgentTask(
        prompt="retry delivery",
        source="telegram",
        remote_recipient_id=recipient,
        remote_recipient_type="chat_id",
        status=TaskStatus.COMPLETED,
        result="completed result",
    )
    app.state.manager.store.save(task)
    app.state.manager.tasks.pop(task.id, None)
    app.state.telegram_bridge.send_text = AsyncMock(
        return_value={"message_id": "message-1"}
    )

    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{task.id}/retry-telegram-delivery")

    assert response.status_code == 200
    assert app.state.telegram_bridge.send_text.await_args.args[0] == recipient
    database = tmp_path / "data" / "deepdesk.db"
    for path in (
        database,
        database.with_name("deepdesk.db-wal"),
        database.with_name("deepdesk.db-shm"),
    ):
        if path.exists():
            assert recipient.encode() not in path.read_bytes()


def test_retry_feishu_delivery_keeps_complete_persisted_route(
    tmp_path: Path,
    monkeypatch,
):
    app = _isolated_app(tmp_path, monkeypatch)
    recipient = "ou_persisted_feishu_recipient"
    task = AgentTask(
        prompt="retry feishu delivery",
        source="feishu",
        remote_recipient_id=recipient,
        remote_recipient_type="open_id",
        status=TaskStatus.COMPLETED,
        result="completed result",
    )
    app.state.manager.store.save(task)
    app.state.manager.tasks.pop(task.id, None)
    app.state.feishu_bridge.send_text = AsyncMock(
        return_value={"ok": True, "message_id": "message-1"}
    )

    with TestClient(app) as client:
        response = client.post(f"/api/tasks/{task.id}/retry-feishu-delivery")

    assert response.status_code == 200
    call = app.state.feishu_bridge.send_text.await_args
    assert call.args[0] == recipient
    assert call.kwargs["receive_id_type"] == "open_id"


def test_remote_redelivery_never_falls_back_to_changed_channel_defaults(
    tmp_path: Path,
    monkeypatch,
):
    app = _isolated_app(tmp_path, monkeypatch)
    telegram = AgentTask(
        prompt="legacy telegram result",
        source="telegram",
        status=TaskStatus.COMPLETED,
        result="telegram result",
    )
    feishu = AgentTask(
        prompt="legacy feishu result",
        source="feishu",
        status=TaskStatus.COMPLETED,
        result="feishu result",
    )
    app.state.manager.tasks[telegram.id] = telegram
    app.state.manager.tasks[feishu.id] = feishu
    app.state.telegram_bridge.default_chat_id = "new-telegram-default"
    app.state.feishu_bridge.default_receive_id = "new-feishu-default"
    app.state.telegram_bridge.send_text = AsyncMock(return_value={"message_id": "wrong"})
    app.state.feishu_bridge.send_text = AsyncMock(return_value={"message_id": "wrong"})

    with TestClient(app) as client:
        telegram_response = client.post(
            f"/api/tasks/{telegram.id}/retry-telegram-delivery"
        )
        feishu_response = client.post(
            f"/api/tasks/{feishu.id}/retry-feishu-delivery"
        )

    assert telegram_response.status_code == 409
    assert feishu_response.status_code == 409
    app.state.telegram_bridge.send_text.assert_not_awaited()
    app.state.feishu_bridge.send_text.assert_not_awaited()


def test_restart_recovery_never_uses_changed_defaults_for_tasks_without_routes(
    tmp_path: Path,
    monkeypatch,
):
    app = _isolated_app(tmp_path, monkeypatch)
    interruption = "Elren 重启时任务仍在运行；已安全标记为中断"
    for source in ("telegram", "feishu"):
        app.state.manager.store.save(
            AgentTask(
                prompt=f"legacy interrupted {source}",
                source=source,
                status=TaskStatus.FAILED,
                error=interruption,
            )
        )
    app.state.telegram_bridge.default_chat_id = "new-telegram-default"
    app.state.feishu_bridge.default_receive_id = "new-feishu-default"
    app.state.telegram_bridge.send_text = AsyncMock(return_value={"message_id": "wrong"})
    app.state.feishu_bridge.send_text = AsyncMock(return_value={"message_id": "wrong"})
    original_list = app.state.manager.store.list_restart_interrupted
    recovery_sources: list[str] = []

    def track_recovery(source: str):
        recovery_sources.append(source)
        return original_list(source)

    app.state.manager.store.list_restart_interrupted = track_recovery
    app.state.manager.continue_from = Mock(side_effect=AssertionError("must fail closed"))

    with TestClient(app):
        deadline = time.monotonic() + 2
        while len(set(recovery_sources)) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert set(recovery_sources) == {"telegram", "feishu"}
    app.state.manager.continue_from.assert_not_called()
    app.state.telegram_bridge.send_text.assert_not_awaited()
    app.state.feishu_bridge.send_text.assert_not_awaited()


def test_changed_channel_credentials_restart_only_the_matching_channel(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    feishu_restart = AsyncMock(return_value=True)
    telegram_restart = AsyncMock(return_value=True)
    app.state.feishu_bridge.restart_long_connection = feishu_restart
    app.state.telegram_bridge.restart_polling = telegram_restart

    with TestClient(app) as client:
        response = client.patch(
            "/api/settings",
            json={"feishu_app_id": "cli_test", "feishu_app_secret": "secret"},
        )

    assert response.status_code == 200
    feishu_restart.assert_awaited_once()
    telegram_restart.assert_not_awaited()






def test_main_status_replaces_retired_speech_status_alias(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        unified = client.get("/api/status")
        retired = client.get("/api/speech/status")

    assert unified.status_code == 200
    assert isinstance(unified.json()["speech"], dict)
    assert retired.status_code == 404


def test_provider_search_is_the_only_registered_native_search_tool(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    names = set(app.state.registry.health()["names"])

    assert "provider_web_search" in names
    assert "deepseek_web_search" not in names


def test_voice_preferences_are_persisted_in_categorized_settings(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        updated = client.patch(
            "/api/settings",
            json={
                "voice_language": "zh-CN",
                "voice_name": "Microsoft Xiaoxiao",
                "voice_rate": 1.15,
                "voice_auto_speak": False,
                "voice_hands_free": False,
                "voice_auto_continue": True,
            },
        )
        loaded = client.get("/api/settings")

    assert updated.status_code == 200
    assert loaded.status_code == 200
    payload = loaded.json()
    assert payload["active_model"]
    assert payload["voice_language"] == "zh-CN"
    assert payload["voice_name"] == "Microsoft Xiaoxiao"
    assert payload["voice_rate"] == 1.15
    assert payload["voice_auto_speak"] is False
    assert payload["voice_hands_free"] is False
    assert payload["voice_auto_continue"] is True
    assert payload["system_voice_language"] in SUPPORTED_SPEECH_LANGUAGES


def test_custom_system_prompt_suffix_round_trips_through_settings_api(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    suffix = "When writing code, verify it with an executable test."

    with TestClient(app) as client:
        updated = client.patch(
            "/api/settings",
            json={"custom_system_prompt_suffix": suffix},
            headers={
                "Host": "127.0.0.1",
                "Origin": "http://127.0.0.1",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        loaded = client.get("/api/settings")

    assert updated.status_code == 200
    assert loaded.status_code == 200
    assert updated.json()["custom_system_prompt_suffix"] == suffix
    assert loaded.json()["custom_system_prompt_suffix"] == suffix


def test_ai_control_settings_reject_non_browser_and_cross_origin_requests(
    tmp_path, monkeypatch
):
    app = _isolated_app(tmp_path, monkeypatch)
    payload = {"custom_system_prompt_suffix": "untrusted remote prompt"}

    with TestClient(app) as client:
        no_origin = client.patch("/api/settings", json=payload)
        cross_origin = client.patch(
            "/api/settings",
            json=payload,
            headers={
                "Origin": "http://attacker.invalid",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        ordinary_setting = client.patch(
            "/api/settings",
            json={"theme_mode": "dark"},
        )
        loaded = client.get("/api/settings")

    assert no_origin.status_code == 403
    assert cross_origin.status_code == 403
    assert ordinary_setting.status_code == 200
    assert loaded.json()["custom_system_prompt_suffix"] == ""
    assert loaded.json()["theme_mode"] == "dark"


def test_settings_validation_errors_never_echo_write_only_credentials(
    tmp_path, monkeypatch
):
    app = _isolated_app(tmp_path, monkeypatch)
    canary = "opaque-new-api-key-7F3A91E6C2D84B50"

    with TestClient(app) as client:
        rejected = client.patch(
            "/api/settings",
            json={
                "deepseek_api_key": canary,
                "discussion_team": [
                    {
                        "id": "only-one",
                        "name": "Only one",
                        "role": "leader",
                        "model": "auto",
                    }
                ],
            },
            headers={
                "Host": "127.0.0.1",
                "Origin": "http://127.0.0.1",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert rejected.status_code == 422
    serialized = rejected.text
    assert canary not in serialized
    assert '"input"' not in serialized
    assert rejected.json()["detail"][0]["msg"] == "Request validation failed"


def test_tool_credentials_settings_never_echo_plaintext(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        updated = client.patch(
            "/api/settings",
            json={
                "github_token": "github-secret",
                "google_places_api_key": "places-secret",
                "trello_api_key": "trello-key",
                "trello_token": "trello-token",
                "elevenlabs_api_key": "eleven-secret",
                "notion_token": "notion-secret",
                "spotify_client_id": "spotify-id",
                "spotify_client_secret": "spotify-secret",
                "op_service_account_token": "op-secret",
                "giphy_api_key": "giphy-secret",
                "tenor_api_key": "tenor-secret",
                "apify_api_token": "apify-secret",
                "firecrawl_api_key": "firecrawl-secret",
                "eightctl_email": "sleep@example.com",
                "eightctl_password": "sleep-secret",
                "deliveroo_bearer_token": "deliveroo-secret",
                "deliveroo_cookie": "cookie-secret",
                "things_auth_token": "things-secret",
                "sag_api_key": "sag-secret",
            },
        )
        loaded = client.get("/api/settings")

    assert updated.status_code == 200
    assert loaded.status_code == 200
    payload = loaded.json()
    assert payload["github_token_configured"] is True
    assert payload["google_places_key_configured"] is True
    assert payload["trello_configured"] is True
    assert payload["elevenlabs_key_configured"] is True
    assert payload["notion_token_configured"] is True
    assert payload["spotify_configured"] is True
    assert payload["onepassword_configured"] is True
    assert payload["giphy_key_configured"] is True
    assert payload["gifgrep_configured"] is True
    assert payload["tenor_key_configured"] is True
    assert payload["summarize_web_configured"] is True
    assert payload["apify_token_configured"] is True
    assert payload["firecrawl_key_configured"] is True
    assert payload["eightctl_email_configured"] is True
    assert payload["eightctl_password_configured"] is True
    assert payload["eightctl_configured"] is True
    assert payload["deliveroo_configured"] is True
    assert payload["deliveroo_token_configured"] is True
    assert payload["deliveroo_cookie_configured"] is True
    assert payload["things_token_configured"] is True
    assert payload["sag_key_configured"] is True
    assert payload["sag_alt_key_configured"] is True
    serialized = loaded.text + updated.text
    for secret in ("github-secret", "places-secret", "trello-key", "trello-token", "eleven-secret", "notion-secret", "spotify-id", "spotify-secret"):
        assert secret not in serialized
    for secret in ("op-secret", "giphy-secret", "tenor-secret", "apify-secret", "firecrawl-secret", "sleep@example.com", "sleep-secret", "deliveroo-secret", "cookie-secret", "things-secret", "sag-secret"):
        assert secret not in serialized


def test_skill_model_credentials_stay_in_vault_and_elren_scoped_injection(tmp_path, monkeypatch):
    import json

    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.patch(
            "/api/settings",
            json={
                "model_providers": [
                    {"id": "oa", "provider": "openai", "model": "gpt-test", "api_key": "oa-secret"},
                    {"id": "google", "provider": "google", "model": "gemini-test", "api_key": "google-secret"},
                    {"id": "xai", "provider": "xai", "model": "grok-test", "api_key": "xai-secret"},
                ]
            },
        )

    assert response.status_code == 200
    assert "oa-secret" not in response.text
    assert "google-secret" not in response.text
    assert "xai-secret" not in response.text
    config = json.loads(
        (tmp_path / "data" / "openclaw" / "openclaw.json").read_text(encoding="utf-8")
    )
    entries = config.get("skills", {}).get("entries", {})
    for skill in ("oracle", "gemini", "summarize", "coding-agent"):
        assert "env" not in entries.get(skill, {})
        assert "apiKey" not in entries.get(skill, {})
    serialized_config = json.dumps(config)
    for secret in ("oa-secret", "google-secret", "xai-secret"):
        assert secret not in serialized_config
        assert secret.encode() not in (
            tmp_path / "data" / "provider-secrets.vault"
        ).read_bytes()
    assert {
        "oracle",
        "gemini",
        "summarize",
        "coding-agent",
    } <= app.state.openclaw_bridge._delegated_credential_skills
    gateway_environment = app.state.openclaw_bridge._environment()
    assert "OPENAI_API_KEY" not in gateway_environment
    assert "GEMINI_API_KEY" not in gateway_environment
    assert "XAI_API_KEY" not in gateway_environment


def test_every_supported_voice_language_can_be_selected(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        for language in SUPPORTED_SPEECH_LANGUAGES:
            updated = client.patch("/api/settings", json={"voice_language": language})
            assert updated.status_code == 200
            assert updated.json()["voice_language"] == language


def test_aicodemirror_key_is_write_only_and_enables_all_three_model_families(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        updated = client.patch(
            "/api/settings",
            json={
                "aicodemirror_api_key": "mirror-secret",
                "aicodemirror_fable_api_key": "fable-secret",
            },
        )
        loaded = client.get("/api/settings")
        automatic = client.patch("/api/settings", json={"model": "auto"})
        defaulted = client.patch(
            "/api/settings",
            json={"model": "aicodemirror-openai:gpt-5.6-terra"},
        )

    assert updated.status_code == 200
    assert loaded.status_code == 200
    payload = loaded.json()
    assert payload["aicodemirror_key_configured"] is True
    assert payload["aicodemirror_fable_key_configured"] is True
    assert "mirror-secret" not in str(payload)
    assert "fable-secret" not in str(payload)
    selectors = {item["selector"] for item in payload["available_models"]}
    assert "aicodemirror-openai:gpt-5.6-sol" in selectors
    assert "aicodemirror-openai:gpt-6-astra" in selectors
    assert "aicodemirror-anthropic:claude-opus-5" in selectors
    assert "aicodemirror-anthropic:claude-fable-5-1" in selectors
    assert "aicodemirror-anthropic:claude-fable-5" in selectors
    assert "aicodemirror-google:gemini-3.8-flash" in selectors
    assert "aicodemirror-google:gemini-3.7-flash" in selectors
    assert "aicodemirror-google:gemini-3.6-flash" in selectors

    assert automatic.status_code == 200
    assert automatic.json()["model"] == "auto"
    assert automatic.json()["active_model"]
    assert defaulted.status_code == 200
    assert defaulted.json()["model"] == "aicodemirror-openai:gpt-5.6-terra"
    assert defaulted.json()["active_model"] == "aicodemirror-openai:gpt-5.6-terra"


def test_new_provider_key_and_default_model_can_be_saved_atomically(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        relay = client.patch(
            "/api/settings",
            json={
                "aicodemirror_api_key": "mirror-secret",
                "model": "aicodemirror-openai:gpt-5.6-terra",
            },
        )
        direct = client.patch(
            "/api/settings",
            json={
                "model_providers": [
                    {
                        "id": "new-openai",
                        "provider": "openai",
                        "model": "gpt-test",
                        "api_key": "direct-secret",
                    }
                ],
                "model": "openai:gpt-test",
            },
        )
        loaded = client.get("/api/settings")

    assert relay.status_code == 200
    assert relay.json()["model"] == "aicodemirror-openai:gpt-5.6-terra"
    assert direct.status_code == 200
    assert direct.json()["model"] == "openai:gpt-test"
    assert loaded.json()["model"] == "openai:gpt-test"
    assert "mirror-secret" not in relay.text + direct.text + loaded.text
    assert "direct-secret" not in relay.text + direct.text + loaded.text


def test_invalid_spoken_model_never_overwrites_default_with_auto(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.patch("/api/settings", json={"aicodemirror_api_key": "mirror-secret"})
        selected = client.patch(
            "/api/settings", json={"model": "aicodemirror-openai:gpt-5.6-terra"}
        )
        rejected = client.patch("/api/settings", json={"model": "GPT5.6SOU"})
        loaded = client.get("/api/settings")

    assert selected.status_code == 200
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert detail["code"] == "model_confirmation_required"
    assert detail["suggestions"][0]["selector"] == "aicodemirror-openai:gpt-5.6-sol"
    assert loaded.json()["model"] == "aicodemirror-openai:gpt-5.6-terra"


def test_control_agent_is_fixed_and_chat_can_be_renamed(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.patch("/api/settings", json={"deepseek_api_key": "test-key"})
        settings = client.patch("/api/settings", json={"default_agent": "coder"})
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "检查当前工程并给出修复方案",
                "agent_profile": "planner",
                # Cached requests from pre-retirement builds remain loadable,
                # but these fields must no longer activate hidden behavior.
                "private_chat": True,
                "privacy_session": "stale-page-capability",
            },
        )
        task_id = created.json()["id"]
        renamed = client.patch(
            f"/api/tasks/{task_id}/title",
            json={"title": "工程修复方案"},
        )
        fetched = client.get(f"/api/tasks/{task_id}")

    assert settings.status_code == 200
    assert settings.json()["default_agent"] == "general"
    assert created.status_code == 200
    assert created.json()["agent_profile"] == "general"
    assert "private_chat" not in created.json()
    assert "privacy_session" not in created.json()
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "工程修复方案"
    assert fetched.json()["title_source"] == "user"


def test_missing_model_key_is_rejected_before_a_task_record_is_created(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        before = client.get("/api/tasks").json()["total"]
        response = client.post(
            "/api/tasks",
            json={"prompt": "This must not create a failed task."},
        )
        after = client.get("/api/tasks").json()["total"]

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_api_key_required"
    assert before == after == 0
    assert app.state.manager.tasks == {}


def test_startup_repairs_only_corrupt_auto_titles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = TaskStore(tmp_path / "data" / "deepdesk.db")
    automatic = AgentTask(
        prompt="语音 1 转写：是啊",
        title="语音转写一致性 പരിശോധന",
        title_source="auto",
        status=TaskStatus.COMPLETED,
        result="done",
    )
    manual = AgentTask(
        prompt="语音 1 转写：是啊",
        title="我的自定义标题 പരിശോധന",
        title_source="user",
        status=TaskStatus.COMPLETED,
        result="done",
    )
    store.save(automatic)
    store.save(manual)

    _isolated_app(tmp_path, monkeypatch)
    repaired = TaskStore(tmp_path / "data" / "deepdesk.db")

    assert repaired.get(automatic.id).title == "语音 1 转写：是啊"
    assert repaired.get(automatic.id).title_source == "auto"
    assert repaired.get(manual.id).title == "我的自定义标题 പരിശോധന"
    assert repaired.get(manual.id).title_source == "user"
