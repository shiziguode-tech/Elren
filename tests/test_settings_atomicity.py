"""Local ASGI settings transactions: synthetic credentials, no server/lifespan/network."""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from deepdesk.config import Settings
from deepdesk.control_files import deactivate_control_file_guard
from deepdesk.main import create_app
from deepdesk.secret_storage import AesGcmProtector, LocalSecretVault
from deepdesk.settings_transaction import SettingsRecoveryError, SettingsTransaction


@pytest_asyncio.fixture
async def isolated(tmp_path, monkeypatch, request):
    def deny(*_args, **_kwargs):
        raise AssertionError("Real network is forbidden in settings atomicity tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    settings = Settings(_env_file=None, deepdesk_workspace=tmp_path,
                        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False,
                        deepseek_api_key="", deepseek_backup_api_key="")
    app = create_app(settings, persistent_control_baseline=bool(getattr(request, "param", False)))
    app.state.vision_runtime.discover = AsyncMock(return_value=None)
    app.state.feishu_bridge.restart_long_connection = AsyncMock(return_value=True)
    app.state.telegram_bridge.restart_polling = AsyncMock(return_value=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        baseline = await http.patch("/api/settings", json={"deepseek_api_key": "qa-old-credential", "theme_mode": "light"})
        assert baseline.status_code == 200, baseline.text
        try:
            yield app, http
        finally:
            deactivate_control_file_guard(tmp_path)


def disk_snapshot(app):
    return {path: path.read_bytes() if path.exists() else None for path in (
        app.state.provider_secrets.path, app.state.runtime_settings.path,
    )}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["vault-before", "vault-after", "runtime-before", "runtime-after", "live-before", "live-after", "commit"])
async def test_failure_restores_files_credentials_preferences_and_live_client(isolated, monkeypatch, stage):
    app, http = isolated
    before = (await http.get("/api/settings")).json()
    files = disk_snapshot(app)
    client = app.state.manager.engine.client
    target, method = (
        (app.state.provider_secrets, "update") if stage.startswith("vault") else
        (app.state.runtime_settings, "update") if stage.startswith("runtime") else
        (SettingsTransaction, "commit") if stage == "commit" else (client, "set_keys")
    )
    original = getattr(target, method)

    def failure(*args, **kwargs):
        if stage.endswith("after"):
            original(*args, **kwargs)
        raise OSError("Synthetic transaction failure; no real credential")

    with monkeypatch.context() as patch:
        patch.setattr(target, method, failure)
        result = await http.patch("/api/settings", json={"deepseek_api_key": "qa-new-credential", "theme_mode": "dark"})
    assert result.status_code == 500
    after = (await http.get("/api/settings")).json()
    assert after["theme_mode"] == before["theme_mode"] == "light"
    assert client.keys == ["qa-old-credential"]
    assert app.state.provider_secrets.value.deepseek_primary == "qa-old-credential"
    assert disk_snapshot(app) == files
    assert not (app.state.settings.data_dir / "settings-transaction.vault").exists()
    assert "qa-new-credential" not in result.text


@pytest.mark.asyncio
async def test_external_activation_failure_reports_committed_save(isolated, monkeypatch):
    app, http = isolated
    monkeypatch.setattr(app.state.telegram_bridge, "restart_polling", AsyncMock(side_effect=OSError("qa secret must not echo")))
    result = await http.patch("/api/settings", json={"telegram_bot_token": "synthetic-bot-token", "theme_mode": "dark"})
    assert result.status_code == 200
    assert result.json()["saved"] is True
    assert {"component": "telegram", "code": "activation_failed"} in result.json()["activation_warnings"]
    assert (await http.get("/api/settings")).json()["theme_mode"] == "dark"
    assert app.state.telegram_bridge.bot_token == "synthetic-bot-token"
    assert "qa secret" not in result.text and "synthetic-bot-token" not in result.text


@pytest.mark.asyncio
async def test_prospective_provider_and_model_commit_together(isolated):
    app, http = isolated
    response = await http.patch("/api/settings", json={
        "model_providers": [{"id": "qa-provider", "provider": "openai", "model": "gpt-qa-only", "api_key": "qa-provider-secret"}],
        "model": "openai:gpt-qa-only", "max_output_tokens": None, "voice_rate": 1.5,
    })
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["model"] == "openai:gpt-qa-only"
    assert response.json()["active_model"] == "openai:gpt-qa-only"
    assert app.state.runtime_settings.value.max_output_tokens is None
    assert app.state.manager.engine.client.has_model_credentials("openai:gpt-qa-only")
    assert "qa-provider-secret" not in response.text


@pytest.mark.asyncio
async def test_late_response_build_failure_restores_every_credential_consumer(isolated, monkeypatch):
    app, http = isolated
    files = disk_snapshot(app)
    client = app.state.manager.engine.client

    def failure(*_args, **_kwargs):
        raise RuntimeError("synthetic response assembly failure")

    with monkeypatch.context() as patch:
        patch.setattr(client, "context_window_info", failure)
        response = await http.patch("/api/settings", json={
            "deepseek_api_key": "qa-new", "gemini_api_key": "qa-google",
            "telegram_bot_token": "qa-telegram",
            "theme_mode": "dark",
        })
    assert response.status_code == 500
    assert disk_snapshot(app) == files
    assert client.keys == ["qa-old-credential"]
    assert app.state.vision_runtime.api_key == ""
    assert app.state.telegram_bridge.bot_token == ""
    app.state.telegram_bridge.restart_polling.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_model_never_changes_any_settings_file(isolated):
    app, http = isolated
    files = disk_snapshot(app)
    result = await http.patch("/api/settings", json={"model": "not-a-real-model-731", "deepseek_api_key": "qa-new", "theme_mode": "dark"})
    assert result.status_code == 422
    assert disk_snapshot(app) == files
    assert app.state.manager.engine.client.keys == ["qa-old-credential"]


@pytest.mark.asyncio
async def test_invalid_model_is_rejected_before_journal_creation(isolated, monkeypatch):
    _app, http = isolated

    def must_not_begin(_transaction):
        raise AssertionError("Invalid settings must not even start a journal")

    monkeypatch.setattr(SettingsTransaction, "begin", must_not_begin)
    result = await http.patch("/api/settings", json={
        "model": "invalid-qa-model", "deepseek_api_key": "qa-new",
    })
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_full_control_candidate_size_checked_before_any_write(isolated, monkeypatch):
    app, http = isolated
    files = disk_snapshot(app)

    def must_not_begin(_transaction):
        raise AssertionError("Oversized roster must not even start a journal")

    monkeypatch.setattr(SettingsTransaction, "begin", must_not_begin)
    roster = [{
        "id": f"qa-member-{index}", "name": f"Member {index}",
        "role": "leader" if index == 0 else "member",
        "assignment": "a" * 4000, "system_prompt": "s" * 12000,
    } for index in range(34)]
    result = await http.patch("/api/settings", json={
        "discussion_team": roster, "deepseek_api_key": "qa-new",
    }, headers={"origin": "http://127.0.0.1", "sec-fetch-site": "same-origin"})
    assert result.status_code == 422
    assert result.json()["detail"] == "AI control settings exceed the supported storage size"
    assert disk_snapshot(app) == files


@pytest.mark.asyncio
async def test_queued_vision_probe_is_not_reported_as_already_activated(isolated):
    _app, http = isolated
    result = await http.patch("/api/settings", json={"theme_mode": "dark"})
    assert result.status_code == 200
    assert result.json()["saved"] is True
    assert result.json()["activation_pending"] == ["vision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("isolated", [True], indirect=True)
async def test_persistent_control_and_both_settings_files_rollback_together(isolated, monkeypatch):
    app, http = isolated
    guard = app.state.control_guard
    files = {**disk_snapshot(app), guard.state_path: guard.state_path.read_bytes()}
    old_trusted = dict(guard._trusted_runtime_control)
    original = guard.accept_runtime_ai_control_settings

    def write_then_fail(**changes):
        original(**changes)
        assert guard.state_path.read_bytes() != files[guard.state_path]
        raise OSError("synthetic failure after authenticated baseline replacement")

    with monkeypatch.context() as patch:
        patch.setattr(guard, "accept_runtime_ai_control_settings", write_then_fail)
        response = await http.patch("/api/settings", json={
            "custom_system_prompt_suffix": "Synthetic changed control instruction",
            "deepseek_api_key": "qa-new", "theme_mode": "dark",
        }, headers={"origin": "http://127.0.0.1", "sec-fetch-site": "same-origin"})
    assert response.status_code == 500
    assert guard._trusted_runtime_control == old_trusted
    assert {path: path.read_bytes() if path.exists() else None for path in files} == files
    assert guard._trusted_state_bytes == files[guard.state_path]


@pytest.mark.asyncio
async def test_cancelling_external_activation_does_not_undo_committed_settings(isolated, monkeypatch):
    app, http = isolated
    entered = asyncio.Event()

    async def delayed_restart():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.telegram_bridge, "restart_polling", delayed_restart)
    saving = asyncio.create_task(http.patch("/api/settings", json={
        "telegram_bot_token": "qa-bot", "theme_mode": "dark",
    }))
    await asyncio.wait_for(entered.wait(), 5)
    saving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await saving
    assert (await http.get("/api/settings")).json()["theme_mode"] == "dark"
    assert app.state.provider_secrets.value.telegram_bot_token == "qa-bot"
    assert not (app.state.settings.data_dir / "settings-transaction.vault").exists()
    # Cancellation must also release the request-level serializing lock.
    assert (await http.patch("/api/settings", json={"voice_rate": 1.25})).status_code == 200


@pytest.mark.asyncio
async def test_control_acceptance_failure_restores_trusted_values(isolated, monkeypatch):
    app, http = isolated
    files = disk_snapshot(app)
    guard = app.state.control_guard
    previous = dict(guard._trusted_runtime_control)
    original = guard.accept_runtime_ai_control_settings

    def failure(**changes):
        original(**changes)
        raise OSError("synthetic control baseline failure")

    with monkeypatch.context() as patch:
        patch.setattr(guard, "accept_runtime_ai_control_settings", failure)
        response = await http.patch("/api/settings", json={"custom_system_prompt_suffix": "New synthetic instruction", "theme_mode": "dark"},
                                    headers={"origin": "http://127.0.0.1", "sec-fetch-site": "same-origin"})
    assert response.status_code == 500
    assert guard._trusted_runtime_control == previous
    assert disk_snapshot(app) == files


@pytest.mark.asyncio
async def test_failed_rollback_blocks_further_writes_until_recovered(isolated, monkeypatch):
    app, http = isolated
    files = disk_snapshot(app)
    original_update = app.state.runtime_settings.update

    def failed_update(*args, **kwargs):
        original_update(*args, **kwargs)
        raise OSError("synthetic post-write failure")

    def failed_restore(*_args, **_kwargs):
        raise OSError("synthetic unavailable disk")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.runtime_settings, "update", failed_update)
        patch.setattr(SettingsTransaction, "_restore", failed_restore)
        response = await http.patch("/api/settings", json={"deepseek_api_key": "qa-new", "theme_mode": "dark"})
        assert response.status_code == 503
        assert app.state.manager.engine.client.keys == ["qa-old-credential"]
        next_response = await http.patch("/api/settings", json={"theme_mode": "system"})
        assert next_response.status_code == 503
    targets = {"credentials": app.state.provider_secrets.path, "preferences": app.state.runtime_settings.path}
    SettingsTransaction(app.state.settings.data_dir, targets, app.state.provider_secrets.protector).recover()
    assert disk_snapshot(app) == files


@pytest.mark.asyncio
async def test_startup_recovers_before_loading_live_settings(isolated):
    app, _http = isolated
    files = disk_snapshot(app)
    targets = {"credentials": app.state.provider_secrets.path, "preferences": app.state.runtime_settings.path}
    pending = SettingsTransaction(app.state.settings.data_dir, targets, app.state.provider_secrets.protector)
    pending.begin()
    app.state.provider_secrets.update(deepseek_primary="qa-half-commit")
    # Do not commit or rollback: a new create_app must repair the durable pair
    # before either ProviderSecretsStore or control-plane startup reads it.
    restarted = create_app(app.state.settings)
    assert disk_snapshot(restarted) == files
    assert restarted.state.provider_secrets.value.deepseek_primary == "qa-old-credential"
    assert restarted.state.runtime_settings.value.theme_mode == "light"


@pytest.mark.asyncio
async def test_parallel_partial_patches_merge_after_activation_wait(isolated, monkeypatch):
    app, http = isolated
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_restart():
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(app.state.telegram_bridge, "restart_polling", delayed_restart)
    first = asyncio.create_task(http.patch("/api/settings", json={"telegram_bot_token": "qa-bot", "theme_mode": "dark"}))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(http.patch("/api/settings", json={"voice_rate": 1.25}))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    results = await asyncio.gather(first, second)
    assert all(result.status_code == 200 for result in results)
    final = (await http.get("/api/settings")).json()
    assert final["theme_mode"] == "dark" and final["voice_rate"] == 1.25


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,code", [("conflict", 409), ("missing", 404), ("created", 200)])
async def test_manual_schedule_run_maps_business_conflict(isolated, monkeypatch, outcome, code):
    app, http = isolated

    def run(_schedule_id):
        if outcome == "conflict":
            raise ValueError("Schedule already claimed or result is uncertain")
        return None if outcome == "missing" else {"task_id": "synthetic-only"}

    monkeypatch.setattr(app.state.scheduler, "run_now", run)
    response = await http.post("/api/schedules/synthetic/run")
    assert response.status_code == code


def test_authenticated_pending_journal_recovers_both_files_and_absence(tmp_path):
    protector = AesGcmProtector("qa-transaction", b"x" * 32)
    first, second = tmp_path / "provider.vault", tmp_path / "runtime.json"
    first.write_bytes(b"old ciphertext only")
    targets = {"credentials": first, "preferences": second}
    transaction = SettingsTransaction(tmp_path, targets, protector)
    transaction.begin()
    assert LocalSecretVault.is_encrypted_bytes(transaction.path.read_bytes())
    assert b"old ciphertext only" not in transaction.path.read_bytes()
    first.write_bytes(b"new ciphertext only")
    second.write_bytes(b"new preferences")
    # Simulate a crash by discarding the live transaction without rollback.
    SettingsTransaction(tmp_path, targets, protector).recover()
    assert first.read_bytes() == b"old ciphertext only"
    assert not second.exists()
    assert not transaction.path.exists()


def test_committed_receipt_never_rolls_back_new_files(tmp_path, monkeypatch):
    protector = AesGcmProtector("qa-transaction", b"x" * 32)
    target = tmp_path / "preferences.json"
    target.write_bytes(b"old")
    transaction = SettingsTransaction(tmp_path, {"preferences": target}, protector)
    transaction.begin()
    target.write_bytes(b"new")
    original = type(target).unlink

    def cannot_clean(path, *args, **kwargs):
        if path == transaction.path:
            raise OSError("synthetic cleanup failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(target), "unlink", cannot_clean)
        transaction.commit()
    assert transaction.path.exists()
    SettingsTransaction(tmp_path, {"preferences": target}, protector).recover()
    assert target.read_bytes() == b"new"


def test_malformed_recovery_fails_closed(tmp_path):
    target = tmp_path / "preferences.json"
    target.write_bytes(b"keep")
    transaction = SettingsTransaction(tmp_path, {"preferences": target}, AesGcmProtector("qa-transaction", b"x" * 32))
    transaction.path.write_bytes(b"not authenticated")
    with pytest.raises(SettingsRecoveryError):
        transaction.recover()
    assert target.read_bytes() == b"keep"
    assert transaction.path.read_bytes() == b"not authenticated"


def test_commit_writer_failure_after_replacement_uses_authenticated_receipt(tmp_path, monkeypatch):
    protector = AesGcmProtector("qa-transaction", b"x" * 32)
    target = tmp_path / "preferences.json"
    target.write_bytes(b"old")
    transaction = SettingsTransaction(tmp_path, {"preferences": target}, protector)
    transaction.begin()
    target.write_bytes(b"new")
    original = LocalSecretVault.write_verified

    def write_then_fail(vault, values):
        original(vault, values)
        if values.get("phase") == "committed":
            raise OSError("synthetic failure after committed receipt replacement")

    monkeypatch.setattr(LocalSecretVault, "write_verified", write_then_fail)
    transaction.commit()
    assert target.read_bytes() == b"new"
    assert transaction.snapshot is None
    assert not transaction.path.exists()


@pytest.mark.parametrize("malformation", ["foreign-workspace", "unknown-target", "invalid-base64"])
def test_authenticated_but_invalid_recovery_never_partially_restores(tmp_path, malformation):
    protector = AesGcmProtector("qa-transaction", b"x" * 32)
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    transaction = SettingsTransaction(tmp_path, {"first": first, "second": second}, protector)
    transaction.begin()
    first.write_bytes(b"new first")
    second.write_bytes(b"new second")
    record = LocalSecretVault(transaction.path, protector).read().values
    if malformation == "foreign-workspace":
        record["workspace"] = "unrelated-workspace"
    elif malformation == "unknown-target":
        record["before"]["unexpected"] = None
    else:
        record["before"]["second"] = "not base64!"
    LocalSecretVault(transaction.path, protector).write_verified(record)
    with pytest.raises(SettingsRecoveryError):
        transaction.recover()
    assert first.read_bytes() == b"new first" and second.read_bytes() == b"new second"
    assert transaction.path.exists()
