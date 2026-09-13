import json
import os
from pathlib import Path

import pytest

from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch, RuntimeSettingsStore
from deepdesk.secret_storage import (
    AesGcmProtector,
    LocalSecretVault,
    SecretStorageFormatError,
    SecretStorageIntegrityError,
    SecretStorageUnavailable,
    WindowsDPAPIProtector,
    _macos_keychain_key,
    select_secret_protector,
)


def test_provider_secrets_load_and_update_without_exposing_status(tmp_path: Path):
    path = tmp_path / "data" / "provider-secrets.vault"
    store = ProviderSecretsStore(path, ProviderSecrets("primary", "backup", "gemini"))

    assert store.status() == {
        "primary_key_configured": True,
        "backup_key_configured": True,
        "openclaw_deepseek_key_configured": False,
        "gemini_key_configured": True,
        "pollinations_key_configured": False,
        "huggingface_token_configured": False,
        "aicodemirror_key_configured": False,
        "aicodemirror_fable_key_configured": False,
        "model_provider_count": 0,
        "feishu_configured": False,
        "feishu_open_id_configured": False,
        "telegram_configured": False,
        "telegram_chat_id_configured": False,
        "github_token_configured": False,
        "google_places_key_configured": False,
        "trello_configured": False,
        "elevenlabs_key_configured": False,
        "notion_token_configured": False,
        "spotify_configured": False,
        "onepassword_configured": False,
        "giphy_key_configured": False,
        "gifgrep_configured": False,
        "tenor_key_configured": False,
        "apify_token_configured": False,
        "firecrawl_key_configured": False,
        "summarize_web_configured": False,
        "eightctl_email_configured": False,
        "eightctl_password_configured": False,
        "eightctl_configured": False,
        "deliveroo_configured": False,
        "deliveroo_token_configured": False,
        "deliveroo_cookie_configured": False,
        "things_token_configured": False,
        "sag_alt_key_configured": False,
        "sag_key_configured": False,
    }
    store.update(
        deepseek_primary="new-primary",
        gemini="new-gemini",
        pollinations="media-key",
        huggingface="hf-test-token",
        aicodemirror="mirror-key",
        aicodemirror_fable="fable-key",
    )
    assert store.value.deepseek_primary == "new-primary"
    assert store.value.deepseek_backup == "backup"
    persisted = path.read_bytes()
    assert LocalSecretVault.is_encrypted_bytes(persisted)
    assert b"new-primary" not in persisted
    assert b"new-gemini" not in persisted

    reloaded = ProviderSecretsStore(path, ProviderSecrets())
    assert reloaded.value.deepseek_primary == "new-primary"
    assert reloaded.value.gemini == "new-gemini"
    assert reloaded.value.pollinations == "media-key"
    assert reloaded.value.huggingface == "hf-test-token"
    assert reloaded.value.aicodemirror == "mirror-key"
    assert reloaded.value.aicodemirror_fable == "fable-key"
    assert reloaded.status()["aicodemirror_key_configured"] is True
    assert reloaded.status()["aicodemirror_fable_key_configured"] is True


def test_tool_credentials_are_write_only_and_blank_updates_preserve_values(tmp_path: Path):
    path = tmp_path / "data" / "provider-secrets.vault"
    store = ProviderSecretsStore(path, ProviderSecrets())
    store.update(
        github_token="gh-secret",
        google_places_api_key="places-secret",
        trello_api_key="trello-key",
        trello_token="trello-token",
        elevenlabs_api_key="voice-secret",
        notion_token="notion-secret",
        spotify_client_id="spotify-id",
        spotify_client_secret="spotify-secret",
        op_service_account_token="op-secret",
        giphy_api_key="giphy-secret",
        tenor_api_key="tenor-secret",
        apify_api_token="apify-secret",
        firecrawl_api_key="firecrawl-secret",
        eightctl_email="sleep@example.com",
        eightctl_password="sleep-secret",
        deliveroo_bearer_token="deliveroo-secret",
        deliveroo_cookie="cookie-secret",
        things_auth_token="things-secret",
        sag_api_key="sag-secret",
    )
    store.update(github_token="", trello_token="")

    assert store.value.github_token == "gh-secret"
    assert store.value.trello_token == "trello-token"
    status = store.status()
    assert status["github_token_configured"] is True
    assert status["google_places_key_configured"] is True
    assert status["trello_configured"] is True
    assert status["elevenlabs_key_configured"] is True
    assert status["notion_token_configured"] is True
    assert status["spotify_configured"] is True
    assert status["onepassword_configured"] is True
    assert status["giphy_key_configured"] is True
    assert status["gifgrep_configured"] is True
    assert status["tenor_key_configured"] is True
    assert status["summarize_web_configured"] is True
    assert status["apify_token_configured"] is True
    assert status["firecrawl_key_configured"] is True
    assert status["eightctl_email_configured"] is True
    assert status["eightctl_password_configured"] is True
    assert status["eightctl_configured"] is True
    assert status["deliveroo_configured"] is True
    assert status["deliveroo_token_configured"] is True
    assert status["deliveroo_cookie_configured"] is True
    assert status["things_token_configured"] is True
    assert status["sag_key_configured"] is True
    assert status["sag_alt_key_configured"] is True


def test_tool_credential_resolver_enforces_per_skill_allowlist():
    from deepdesk.tool_credentials import ToolCredentialResolver

    resolver = ToolCredentialResolver(
        lambda: {
            "GH_TOKEN": "github-secret",
            "TRELLO_API_KEY": "trello-key",
            "TRELLO_TOKEN": "trello-token",
            "OPENAI_API_KEY": "openai-secret",
            "XAI_API_KEY": "xai-secret",
            "APIFY_API_TOKEN": "apify-secret",
            "FIRECRAWL_API_KEY": "firecrawl-secret",
        }
    )

    assert resolver.environment_for("github") == {"GH_TOKEN": "github-secret"}
    assert resolver.environment_for("trello") == {
        "TRELLO_API_KEY": "trello-key",
        "TRELLO_TOKEN": "trello-token",
    }
    assert resolver.environment_for("openai-whisper-api") == {
        "OPENAI_API_KEY": "openai-secret"
    }
    assert resolver.environment_for("unknown-skill") == {}
    assert resolver.environment_for("summarize") == {
        "OPENAI_API_KEY": "openai-secret",
        "XAI_API_KEY": "xai-secret",
        "APIFY_API_TOKEN": "apify-secret",
        "FIRECRAWL_API_KEY": "firecrawl-secret",
    }


def test_multiple_model_providers_preserve_hidden_keys_and_allow_row_removal(tmp_path: Path):
    path = tmp_path / "data" / "provider-secrets.vault"
    store = ProviderSecretsStore(path, ProviderSecrets())
    store.update(
        model_providers=[
            {"id": "duplicate-deepseek", "provider": "deepseek", "model": "deepseek-v4-pro", "api_key": "duplicate-secret"},
            {"id": "openai-main", "provider": "openai", "model": "gpt-test", "api_key": "oa-secret"},
            {"id": "xai-main", "provider": "xai", "model": "grok-test", "api_key": "xai-secret"},
        ],
        telegram_bot_token="bot-secret",
        telegram_chat_id="1234",
    )

    public = store.model_provider_status()
    assert public == [
        {"id": "openai-main", "provider": "openai", "model": "gpt-test", "selector": "openai:gpt-test", "configured": True},
        {"id": "xai-main", "provider": "xai", "model": "grok-test", "selector": "xai:grok-test", "configured": True},
    ]
    assert "oa-secret" not in str(public)
    assert all(item["provider"] != "deepseek" for item in store.value.model_providers)

    store.update(
        model_providers=[
            {"id": "openai-main", "provider": "openai", "model": "gpt-test", "api_key": ""},
        ]
    )
    assert len(store.value.model_providers) == 1
    assert store.value.model_providers[0]["api_key"] == "oa-secret"
    assert store.status()["telegram_configured"] is True


def test_runtime_settings_store_ignores_write_only_key_fields(tmp_path: Path):
    store = RuntimeSettingsStore(tmp_path / "runtime.json", RuntimeSettings())
    updated = store.update(RuntimeSettingsPatch(deepseek_api_key="secret", request_timeout=42))
    assert updated.request_timeout == 42
    assert "deepseek_api_key" not in store.path.read_text(encoding="utf-8")


def test_provider_secrets_null_values_fall_back_to_configured_defaults(tmp_path: Path):
    path = tmp_path / "data" / "provider-secrets.vault"
    legacy_path = tmp_path / "data" / "provider-keys.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "deepseek_primary": None,
                "telegram_bot_token": None,
            }
        ),
        encoding="utf-8",
    )

    defaults = ProviderSecrets(
        deepseek_primary="default-primary",
        telegram_bot_token="default-bot",
    )
    loaded = ProviderSecretsStore(path, defaults, legacy_path=legacy_path)

    assert loaded.value.deepseek_primary == "default-primary"
    assert loaded.value.telegram_bot_token == "default-bot"
    assert LocalSecretVault.is_encrypted_bytes(path.read_bytes())
    assert not legacy_path.exists()


def test_provider_secrets_migrate_env_and_legacy_json_only_after_verified_vault(
    tmp_path: Path,
):
    path = tmp_path / "data" / "provider-secrets.vault"
    legacy_path = tmp_path / "data" / "provider-keys.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "deepseek_primary": "legacy-primary",
                "github_token": "legacy-github",
                "model_providers": [
                    {
                        "id": "openai-main",
                        "provider": "openai",
                        "model": "gpt-test",
                        "api_key": "legacy-model-key",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep this comment\n"
        "DEEPSEEK_API_KEY=legacy-primary\n"
        "ELREN_GITHUB_TOKEN=legacy-github\n"
        "ELREN_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    legacy_path.with_suffix(".tmp").write_text("legacy-primary", encoding="utf-8")
    legacy_path.with_name(f"{legacy_path.name}.bak").write_text(
        "legacy-github", encoding="utf-8"
    )

    store = ProviderSecretsStore(
        path,
        ProviderSecrets(),
        legacy_path=legacy_path,
        legacy_env_path=env_path,
    )

    assert store.value.deepseek_primary == "legacy-primary"
    assert store.value.github_token == "legacy-github"
    assert store.value.model_providers[0]["api_key"] == "legacy-model-key"
    encrypted = path.read_bytes()
    assert LocalSecretVault.is_encrypted_bytes(encrypted)
    for secret in (b"legacy-primary", b"legacy-github", b"legacy-model-key"):
        assert secret not in encrypted
    env_text = env_path.read_text(encoding="utf-8")
    assert "# keep this comment" in env_text
    assert "DEEPSEEK_API_KEY=\n" in env_text
    assert "ELREN_GITHUB_TOKEN=\n" in env_text
    assert "ELREN_HOST=127.0.0.1" in env_text
    assert not legacy_path.exists()
    assert not legacy_path.with_suffix(".tmp").exists()
    assert not legacy_path.with_name(f"{legacy_path.name}.bak").exists()


def test_legacy_runtime_setting_credentials_migrate_without_losing_preferences(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runtime_path = data_dir / "runtime-settings.json"
    runtime_path.write_text(
        json.dumps(
            {
                "theme_mode": "dark",
                "deepseek_api_key": "runtime-primary-secret",
                "trello_token": "runtime-trello-secret",
                "model_providers": [
                    {
                        "id": "anthropic-main",
                        "provider": "anthropic",
                        "model": "claude-test",
                        "api_key": "runtime-model-secret",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    vault_path = data_dir / "provider-secrets.vault"

    store = ProviderSecretsStore(vault_path, ProviderSecrets())

    assert store.value.deepseek_primary == "runtime-primary-secret"
    assert store.value.trello_token == "runtime-trello-secret"
    assert store.value.model_providers[0]["api_key"] == "runtime-model-secret"
    persisted_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert persisted_runtime == {"theme_mode": "dark"}
    encrypted = vault_path.read_bytes()
    for secret in (
        b"runtime-primary-secret",
        b"runtime-trello-secret",
        b"runtime-model-secret",
    ):
        assert secret not in encrypted


def test_provider_secrets_replace_failure_keeps_legacy_and_env_plaintext_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "data" / "provider-secrets.vault"
    legacy_path = tmp_path / "data" / "provider-keys.json"
    legacy_path.parent.mkdir(parents=True)
    original = '{"deepseek_primary":"only-copy-secret"}'
    legacy_path.write_text(original, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=only-copy-secret\n", encoding="utf-8")

    original_replace = os.replace

    def fail_vault_replace(source, target):
        if Path(target) == path:
            raise OSError("simulated power loss")
        return original_replace(source, target)

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", fail_vault_replace)

    with pytest.raises(OSError, match="simulated power loss"):
        ProviderSecretsStore(
            path,
            ProviderSecrets(),
            legacy_path=legacy_path,
            legacy_env_path=env_path,
        )

    assert not path.exists()
    assert legacy_path.read_text(encoding="utf-8") == original
    assert "only-copy-secret" in env_path.read_text(encoding="utf-8")
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_provider_secrets_failed_encrypted_update_does_not_publish_memory_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "provider-secrets.vault"
    store = ProviderSecretsStore(path, ProviderSecrets(deepseek_primary="old-secret"))
    original_bytes = path.read_bytes()
    original_replace = os.replace

    def fail_vault_replace(source, target):
        if Path(target) == path:
            raise OSError("simulated disk failure")
        return original_replace(source, target)

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", fail_vault_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        store.update(deepseek_primary="new-secret")

    assert store.value.deepseek_primary == "old-secret"
    assert path.read_bytes() == original_bytes
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_env_replace_failure_happens_after_verified_vault_and_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "data" / "provider-secrets.vault"
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=env-only-secret\nELREN_PORT=8765\n",
        encoding="utf-8",
    )
    original_replace = os.replace

    def fail_env_replace(source, target):
        if Path(target) == env_path:
            raise OSError("simulated env replace failure")
        return original_replace(source, target)

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", fail_env_replace)
    with pytest.raises(OSError, match="simulated env replace failure"):
        ProviderSecretsStore(
            path,
            ProviderSecrets(deepseek_primary="env-only-secret"),
            legacy_env_path=env_path,
        )

    assert LocalSecretVault.is_encrypted_bytes(path.read_bytes())
    assert b"env-only-secret" not in path.read_bytes()
    assert "env-only-secret" in env_path.read_text(encoding="utf-8")

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", original_replace)
    reloaded = ProviderSecretsStore(path, ProviderSecrets(), legacy_env_path=env_path)
    assert reloaded.value.deepseek_primary == "env-only-secret"
    assert "DEEPSEEK_API_KEY=\n" in env_path.read_text(encoding="utf-8")


def test_empty_encrypted_field_accepts_legacy_env_default_then_scrubs_plaintext(
    tmp_path: Path,
):
    path = tmp_path / "data" / "provider-secrets.vault"
    path.parent.mkdir(parents=True)
    LocalSecretVault(path).write_verified({"deepseek_primary": ""})
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=env-migration-secret\nELREN_PORT=8765\n",
        encoding="utf-8",
    )

    store = ProviderSecretsStore(
        path,
        # Settings loads this value from the legacy .env before constructing
        # ProviderSecretsStore; the store owns the verified migration/scrub.
        ProviderSecrets(deepseek_primary="env-migration-secret"),
        legacy_env_path=env_path,
    )

    assert store.value.deepseek_primary == "env-migration-secret"
    assert b"env-migration-secret" not in path.read_bytes()
    assert "DEEPSEEK_API_KEY=\n" in env_path.read_text(encoding="utf-8")
    assert "ELREN_PORT=8765" in env_path.read_text(encoding="utf-8")
    assert ProviderSecretsStore(path, ProviderSecrets()).value.deepseek_primary == (
        "env-migration-secret"
    )


def test_nonempty_encrypted_field_remains_authoritative_over_legacy_env_default(
    tmp_path: Path,
):
    path = tmp_path / "data" / "provider-secrets.vault"
    ProviderSecretsStore(path, ProviderSecrets(deepseek_primary="vault-secret"))
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=stale-env-secret\n", encoding="utf-8")

    store = ProviderSecretsStore(
        path,
        ProviderSecrets(deepseek_primary="stale-env-secret"),
        legacy_env_path=env_path,
    )

    assert store.value.deepseek_primary == "vault-secret"
    assert "DEEPSEEK_API_KEY=\n" in env_path.read_text(encoding="utf-8")


def test_provider_secrets_tamper_and_malformed_legacy_fail_closed(tmp_path: Path):
    path = tmp_path / "provider-secrets.vault"
    ProviderSecretsStore(path, ProviderSecrets(deepseek_primary="secret"))
    tampered = bytearray(path.read_bytes())
    tampered[-1] ^= 0x01
    path.write_bytes(tampered)

    with pytest.raises(SecretStorageIntegrityError):
        ProviderSecretsStore(path, ProviderSecrets(deepseek_primary="env-fallback"))

    path.write_text("{broken-json", encoding="utf-8")
    with pytest.raises(SecretStorageFormatError):
        ProviderSecretsStore(path, ProviderSecrets(deepseek_primary="env-fallback"))


def test_provider_secret_repr_never_contains_values():
    value = ProviderSecrets(
        deepseek_primary="repr-primary-secret",
        model_providers=[
            {
                "id": "oa",
                "provider": "openai",
                "model": "gpt-test",
                "api_key": "repr-model-secret",
            }
        ],
    )

    rendered = repr(value)
    assert "repr-primary-secret" not in rendered
    assert "repr-model-secret" not in rendered


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_windows_dpapi_repeated_real_roundtrips_do_not_truncate_x64_pointers():
    protector = WindowsDPAPIProtector()
    for index in range(500):
        plaintext = f"dpapi-roundtrip-{index}".encode()
        assert protector.unprotect(protector.protect(plaintext)) == plaintext


def test_aes_gcm_test_backend_authenticates_ciphertext(tmp_path: Path):
    protector = AesGcmProtector("test-aes", b"k" * 32)
    path = tmp_path / "vault.bin"
    vault = LocalSecretVault(path, protector)
    vault.write_verified({"key": "never-plaintext"})
    payload = bytearray(path.read_bytes())
    assert b"never-plaintext" not in payload
    payload[-1] ^= 0x01
    path.write_bytes(payload)
    with pytest.raises(SecretStorageIntegrityError):
        vault.read()


def test_secret_vault_retries_transient_windows_replace_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    vault = LocalSecretVault(path, AesGcmProtector("test-aes", b"k" * 32))
    original_replace = os.replace
    attempts = 0

    def transient_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError(13, "simulated transient Windows denial", str(destination))
        original_replace(source, destination)

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", transient_replace)
    monkeypatch.setattr("deepdesk.secret_storage.time.sleep", lambda _seconds: None)

    vault.write_verified({"key": "encrypted-secret"})

    assert attempts == 4
    assert vault.read().values == {"key": "encrypted-secret"}
    assert b"encrypted-secret" not in path.read_bytes()
    assert not list(tmp_path.glob(".vault.bin.*.tmp"))


def test_secret_vault_exhausted_replace_denials_preserve_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vault.bin"
    vault = LocalSecretVault(path, AesGcmProtector("test-aes", b"k" * 32))
    vault.write_verified({"key": "old-secret"})
    original_payload = path.read_bytes()
    attempts = 0

    def denied_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        del source, destination
        attempts += 1
        raise PermissionError(13, "simulated persistent Windows denial")

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", denied_replace)
    monkeypatch.setattr("deepdesk.secret_storage.time.sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistent Windows denial"):
        vault.write_verified({"key": "new-secret"})

    assert attempts == 6
    assert path.read_bytes() == original_payload
    assert vault.read().values == {"key": "old-secret"}
    assert not list(tmp_path.glob(".vault.bin.*.tmp"))


def test_encrypted_development_envelope_is_authenticated_and_upgraded(tmp_path: Path):
    protector = AesGcmProtector("test-aes", b"k" * 32)
    path = tmp_path / "legacy-vault.bin"
    vault = LocalSecretVault(path, protector)
    plaintext = json.dumps(
        {"version": 1, "values": {"key": "encrypted-legacy-secret"}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    algorithm = protector.algorithm.encode()
    legacy = b"ELRENVLT\x01" + bytes([len(algorithm)]) + algorithm + protector.protect(plaintext)
    path.write_bytes(legacy)

    assert vault.read().values == {"key": "encrypted-legacy-secret"}
    canonical = path.read_bytes()
    assert canonical != legacy
    assert b"encrypted-legacy-secret" not in canonical
    assert vault.read().values == {"key": "encrypted-legacy-secret"}


def test_legacy_dpapi_identifier_is_upgraded_without_plaintext_fallback(tmp_path: Path):
    protector = AesGcmProtector("windows-dpapi-current-user", b"k" * 32)
    path = tmp_path / "legacy-dpapi-name.bin"
    vault = LocalSecretVault(path, protector)
    plaintext = json.dumps(
        {"version": 1, "values": {"key": "dpapi-name-secret"}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    legacy_algorithm = b"windows-dpapi"
    legacy = (
        b"ELRENVLT\x01"
        + bytes([len(legacy_algorithm)])
        + legacy_algorithm
        + protector.protect(plaintext)
    )
    path.write_bytes(legacy)

    assert vault.read().values == {"key": "dpapi-name-secret"}
    canonical = path.read_bytes()
    assert canonical != legacy
    assert b"dpapi-name-secret" not in canonical
    assert b"windows-dpapi-current-user" in canonical


def test_macos_keychain_master_key_uses_security_framework_not_process_argv(
    monkeypatch: pytest.MonkeyPatch,
):
    import ctypes

    captured: dict[str, bytes] = {}

    class Function:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    class Security:
        def __init__(self):
            self.SecKeychainFindGenericPassword = Function(lambda *_: -25300)

            def add(*args):
                captured["key"] = ctypes.string_at(args[6], args[5])
                return 0

            self.SecKeychainAddGenericPassword = Function(add)
            self.SecKeychainItemFreeContent = Function(lambda *_: 0)

    class CoreFoundation:
        def __init__(self):
            self.CFRelease = Function(lambda *_: None)

    security = Security()
    core_foundation = CoreFoundation()

    def cdll(path):
        return security if "Security.framework" in path else core_foundation

    monkeypatch.setattr("deepdesk.secret_storage.platform.system", lambda: "Darwin")
    monkeypatch.setattr(ctypes, "CDLL", cdll)
    monkeypatch.setattr(
        "deepdesk.secret_storage.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("Keychain master key entered process argv"),
    )

    key = _macos_keychain_key()

    assert len(key) == 32
    assert captured["key"]


def test_linux_without_secret_service_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("deepdesk.secret_storage.platform.system", lambda: "Linux")
    monkeypatch.setattr("deepdesk.secret_storage.shutil.which", lambda _name: None)

    with pytest.raises(SecretStorageUnavailable, match="Secret Service"):
        select_secret_protector()


@pytest.mark.skipif(os.name != "nt", reason="Windows owner DACL contract")
def test_windows_vault_dacl_contains_only_current_user(tmp_path: Path):
    import win32api
    import win32security

    path = tmp_path / "dacl-vault.bin"
    LocalSecretVault(path).write_verified({"key": "secret"})
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    current_user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]

    assert dacl.GetAceCount() == 1
    assert dacl.GetAce(0)[2] == current_user
