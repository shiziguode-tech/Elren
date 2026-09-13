import asyncio
import io
import json
import os
import time
from pathlib import Path

import pytest

from deepdesk.openclaw_bridge import OpenClawBridge
from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.secret_storage import (
    LocalSecretVault,
    SecretStorageFormatError,
    SecretStorageUnavailable,
)


def _assert_owner_only(path: Path) -> None:
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        return
    import win32api
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    current_user = win32security.GetTokenInformation(
        token, win32security.TokenUser
    )[0]
    assert dacl.GetAceCount() == 1
    assert dacl.GetAce(0)[2] == current_user


def test_openclaw_skill_credentials_are_removed_from_config_and_recovery_copies(tmp_path):
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
    )
    legacy_config = json.loads(bridge.config_path.read_text(encoding="utf-8"))
    legacy_entries = legacy_config.setdefault("skills", {}).setdefault("entries", {})
    legacy_entries["github"] = {"env": {"GH_TOKEN": "github-secret"}}
    legacy_entries["trello"] = {
        "env": {"TRELLO_API_KEY": "trello-key", "TRELLO_TOKEN": "trello-token"}
    }
    bridge.config_path.write_text(json.dumps(legacy_config), encoding="utf-8")
    last_good = bridge.state_dir / "openclaw.json.last-good"
    backup = bridge.state_dir / "openclaw.json.bak"
    stale_temp = bridge.state_dir / "openclaw-stale.tmp"
    for copy in (last_good, backup):
        copy.write_text(json.dumps(legacy_config), encoding="utf-8")
    stale_temp.write_text("github-secret", encoding="utf-8")

    bridge.set_skill_credentials(
        {
            "github": {"GH_TOKEN": "github-secret"},
            "trello": {"TRELLO_API_KEY": "trello-key", "TRELLO_TOKEN": "trello-token"},
            "not-allowlisted": {"EVERY_SECRET": "bad"},
            "summarize": {"XAI_API_KEY": "xai-secret", "APIFY_API_TOKEN": "apify-secret"},
        }
    )

    config = json.loads(bridge.config_path.read_text(encoding="utf-8"))
    entries = config["skills"]["entries"]
    assert "github" not in entries
    assert "trello" not in entries
    assert "not-allowlisted" not in entries
    assert "summarize" not in entries
    assert "GH_TOKEN" not in bridge.provider_env
    assert "GH_TOKEN" not in bridge._environment()
    assert bridge._delegated_credential_skills == {"github", "trello", "summarize"}
    for copy in (bridge.config_path, last_good, backup):
        content = copy.read_text(encoding="utf-8")
        for secret in (
            "github-secret",
            "trello-key",
            "trello-token",
            "xai-secret",
            "apify-secret",
        ):
            assert secret not in content
    # An unparsable temporary file is retained: it can be the only recovery
    # copy, and migration must never trade a failed parse for data loss.
    assert stale_temp.read_text(encoding="utf-8") == "github-secret"


def test_openclaw_provider_catalog_credentials_are_scrubbed_across_recovery_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider_store = ProviderSecretsStore(
        tmp_path / "data" / "provider-secrets.vault",
        ProviderSecrets(deepseek_primary="chat-model-secret"),
    )
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_secrets=provider_store,
    )
    catalog = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True)
    payload = {
        "providers": {
            "deepseek": {
                "baseUrl": "https://example.invalid",
                "apiKey": "catalog-secret",
                "auth": {"accessToken": "catalog-secret"},
            }
        },
        "metadata": {"apiKey": "this unrelated plugin metadata is retained"},
    }
    for path in (
        catalog,
        catalog.with_name("catalog.json.last-good"),
        catalog.with_name("catalog.json.bak.1"),
        catalog.with_name("catalog-stale.tmp"),
        catalog.with_name(".catalog.json.interrupted.tmp"),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")

    bridge.set_skill_credentials({})

    for path in (
        catalog,
        catalog.with_name("catalog.json.last-good"),
        catalog.with_name("catalog.json.bak.1"),
        catalog.with_name("catalog-stale.tmp"),
        catalog.with_name(".catalog.json.interrupted.tmp"),
    ):
        scrubbed = json.loads(path.read_text(encoding="utf-8"))
        assert "apiKey" not in scrubbed["providers"]["deepseek"]
        assert "accessToken" not in scrubbed["providers"]["deepseek"]["auth"]
        assert "apiKey" in scrubbed["metadata"]

    vault_bytes = provider_store.path.read_bytes()
    assert LocalSecretVault.is_encrypted_bytes(vault_bytes)
    assert b"catalog-secret" not in vault_bytes
    assert provider_store.value.openclaw_deepseek_api_key == "catalog-secret"
    assert bridge.provider_env == {"DEEPSEEK_API_KEY": "catalog-secret"}
    assert bridge._environment()["DEEPSEEK_API_KEY"] == "catalog-secret"

    captured: dict[str, object] = {}

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(bridge, "_cli_command", lambda _cli, *args: ["openclaw", *args])
    monkeypatch.setattr("deepdesk.openclaw_bridge.subprocess.Popen", fake_popen)
    bridge._start_gateway("selected-cli")

    assert "catalog-secret" not in " ".join(captured["command"])
    assert captured["env"]["DEEPSEEK_API_KEY"] == "catalog-secret"
    assert not bridge.gateway_log_path.exists() or (
        b"catalog-secret" not in bridge.gateway_log_path.read_bytes()
    )


def test_openclaw_nested_models_atomic_temp_is_vaulted_before_scrub(tmp_path: Path):
    provider_store = ProviderSecretsStore(
        tmp_path / "data" / "provider-secrets.vault", ProviderSecrets()
    )
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_secrets=provider_store,
    )
    recovery = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / ".models.json.interrupted.tmp"
    )
    recovery.parent.mkdir(parents=True, exist_ok=True)
    recovery.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "deepseek": {"api_key": "models-recovery-secret"}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    bridge.set_skill_credentials({})

    scrubbed = json.loads(recovery.read_text(encoding="utf-8"))
    assert "api_key" not in scrubbed["models"]["providers"]["deepseek"]
    assert (
        provider_store.value.openclaw_deepseek_api_key
        == "models-recovery-secret"
    )
    assert b"models-recovery-secret" not in provider_store.path.read_bytes()


def test_openclaw_managed_scan_is_fixed_depth_and_candidate_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
    )
    agent_root = bridge.state_dir / "agents" / "main" / "agent"
    plugin_root = agent_root / "plugins" / "deepseek"
    plugin_root.mkdir(parents=True)
    managed = {
        agent_root / "models.json",
        agent_root / ".models.json.interrupted.tmp",
        plugin_root / "catalog.json",
        plugin_root / "catalog.json.bak.2",
        plugin_root / ".catalog.json.interrupted.tmp",
    }
    for path in managed:
        path.write_text("{}", encoding="utf-8")

    # A large unrelated cache tree previously amplified state_dir.rglob and
    # Path.resolve work despite containing no managed OpenClaw credential file.
    noise_root = bridge.state_dir / "cache"
    for bucket in range(20):
        folder = noise_root / f"bucket-{bucket}"
        folder.mkdir(parents=True)
        for index in range(50):
            (folder / f"unrelated-{index}.json").write_text("{}", encoding="utf-8")
    deep_false_positive = noise_root / "nested" / "plugins" / "deepseek" / "catalog.json"
    deep_false_positive.parent.mkdir(parents=True)
    deep_false_positive.write_text("{}", encoding="utf-8")

    def reject_recursive_walk(*_args, **_kwargs):
        raise AssertionError("managed credential discovery must never use rglob")

    monkeypatch.setattr(Path, "rglob", reject_recursive_walk)
    original_resolve = Path.resolve
    resolve_calls = 0

    def counted_resolve(path: Path, *args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted_resolve)
    started = time.perf_counter()
    discovered = set(bridge._managed_openclaw_json_files())
    elapsed = time.perf_counter() - started

    assert managed <= discovered
    assert bridge.config_path in discovered
    assert deep_false_positive not in discovered
    assert resolve_calls <= len(discovered) + 1
    assert elapsed < 1.0


def test_openclaw_conflicting_provider_keys_fail_closed_without_scrub(tmp_path: Path):
    provider_store = ProviderSecretsStore(
        tmp_path / "data" / "provider-secrets.vault", ProviderSecrets()
    )
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_secrets=provider_store,
    )
    catalog = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps({"providers": {"deepseek": {"apiKey": "first-secret"}}}),
        encoding="utf-8",
    )
    recovery = catalog.with_name(".catalog.json.interrupted.tmp")
    recovery.write_text(
        json.dumps({"providers": {"deepseek": {"apiKey": "second-secret"}}}),
        encoding="utf-8",
    )

    with pytest.raises(SecretStorageFormatError, match="Conflicting legacy"):
        bridge.set_skill_credentials({})

    assert "first-secret" in catalog.read_text(encoding="utf-8")
    assert "second-secret" in recovery.read_text(encoding="utf-8")
    assert provider_store.value.openclaw_deepseek_api_key == ""


def test_openclaw_catalog_migration_failure_keeps_unique_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider_store = ProviderSecretsStore(
        tmp_path / "data" / "provider-secrets.vault", ProviderSecrets()
    )
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_secrets=provider_store,
    )
    catalog = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True)
    secret = "only-catalog-copy-secret"
    catalog.write_text(
        json.dumps({"providers": {"deepseek": {"apiKey": secret}}}),
        encoding="utf-8",
    )

    def fail_write(_path: Path, _payload: bytes) -> None:
        raise OSError("simulated catalog migration failure")

    monkeypatch.setattr("deepdesk.openclaw_bridge.atomic_write_secure", fail_write)
    with pytest.raises(OSError, match="simulated catalog migration failure"):
        bridge.set_skill_credentials({})

    assert catalog.is_file()
    assert secret in catalog.read_text(encoding="utf-8")
    assert provider_store.value.openclaw_deepseek_api_key == secret
    assert secret.encode() not in provider_store.path.read_bytes()


def test_openclaw_vault_failure_preserves_json_and_runtime_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider_store = ProviderSecretsStore(
        tmp_path / "data" / "provider-secrets.vault",
        ProviderSecrets(deepseek_primary="primary-fallback"),
    )
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_env={"DEEPSEEK_API_KEY": "primary-fallback"},
        provider_secrets=provider_store,
    )
    catalog = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps({"providers": {"deepseek": {"apiKey": "only-copy-secret"}}}),
        encoding="utf-8",
    )

    def fail_vault_write(_value: ProviderSecrets) -> None:
        raise OSError("simulated vault failure")

    monkeypatch.setattr(provider_store, "_persist_unlocked", fail_vault_write)
    with pytest.raises(OSError, match="simulated vault failure"):
        bridge.set_skill_credentials({})

    assert "only-copy-secret" in catalog.read_text(encoding="utf-8")
    assert provider_store.value.openclaw_deepseek_api_key == ""
    assert bridge.provider_env == {"DEEPSEEK_API_KEY": "primary-fallback"}


def test_openclaw_provider_plaintext_is_not_scrubbed_without_vault(tmp_path: Path):
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
    )
    catalog = (
        bridge.state_dir
        / "agents"
        / "main"
        / "agent"
        / "plugins"
        / "deepseek"
        / "catalog.json"
    )
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        json.dumps({"providers": {"deepseek": {"apiKey": "unique-secret"}}}),
        encoding="utf-8",
    )

    with pytest.raises(SecretStorageUnavailable, match="vault is unavailable"):
        bridge.set_skill_credentials({})

    assert "unique-secret" in catalog.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_openclaw_agent_prompt_uses_owner_only_file_not_argv_and_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    prompt = "private user prompt that must not enter argv"
    exact_secret = "configured-openclaw-canary"
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_env={"DEEPSEEK_API_KEY": exact_secret},
    )
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": exact_secret}).encode(), b""

    async def create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        message_index = args.index("--message-file") + 1
        message_path = Path(args[message_index])
        captured["message_path"] = message_path
        assert message_path.read_text(encoding="utf-8") == prompt
        _assert_owner_only(message_path)
        return Process()

    monkeypatch.setattr(bridge, "resolve_cli", lambda: "selected-openclaw")
    monkeypatch.setattr(
        bridge,
        "_cli_command",
        lambda _cli, *args: ["selected-openclaw", *args],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    result = await bridge.agent_exec(prompt)

    assert prompt not in captured["args"]
    assert "--message" not in captured["args"]
    assert "--message-file" in captured["args"]
    assert not captured["message_path"].exists()
    assert captured["env"]["DEEPSEEK_API_KEY"] == exact_secret
    assert result == {"result": "[REDACTED]"}


@pytest.mark.asyncio
async def test_openclaw_agent_prompt_file_is_deleted_on_spawn_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    prompt = "spawn-error-prompt-canary"
    bridge = OpenClawBridge(False, "", "ws://127.0.0.1:18789", "", False, tmp_path)
    captured: dict[str, Path] = {}

    async def create_subprocess_exec(*args, **_kwargs):
        path = Path(args[args.index("--message-file") + 1])
        captured["path"] = path
        assert path.read_text(encoding="utf-8") == prompt
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(bridge, "resolve_cli", lambda: "selected-openclaw")
    monkeypatch.setattr(
        bridge,
        "_cli_command",
        lambda _cli, *args: ["selected-openclaw", *args],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    with pytest.raises(OSError, match="simulated spawn failure"):
        await bridge.agent_exec(prompt)

    assert not captured["path"].exists()


@pytest.mark.asyncio
async def test_openclaw_agent_rejects_credential_prompt_before_file_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    exact_secret = "configured-prompt-secret-canary"
    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_env={"DEEPSEEK_API_KEY": exact_secret},
    )
    spawned = False

    async def create_subprocess_exec(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("credential prompt must be rejected before spawn")

    monkeypatch.setattr(bridge, "resolve_cli", lambda: "selected-openclaw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    with pytest.raises(PermissionError, match="credential-bearing"):
        await bridge.agent_exec(f"please use {exact_secret}")

    assert spawned is False
    assert not list(bridge.state_dir.glob(".agent-message-*.txt"))

    with pytest.raises(PermissionError, match="credential-bearing"):
        await bridge.agent_exec("api_key=sk-test-12345678901234567890")
    assert spawned is False
    assert not list(bridge.state_dir.glob(".agent-message-*.txt"))


@pytest.mark.asyncio
async def test_openclaw_agent_prompt_file_is_deleted_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bridge = OpenClawBridge(False, "", "ws://127.0.0.1:18789", "", False, tmp_path)
    captured: dict[str, Path] = {}

    class Process:
        returncode = None

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def create_subprocess_exec(*args, **_kwargs):
        captured["path"] = Path(args[args.index("--message-file") + 1])
        return Process()

    monkeypatch.setattr(bridge, "resolve_cli", lambda: "selected-openclaw")
    monkeypatch.setattr(
        bridge,
        "_cli_command",
        lambda _cli, *args: ["selected-openclaw", *args],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    with pytest.raises(RuntimeError, match="timed out"):
        await bridge.agent_exec("timeout-prompt-canary")

    assert not captured["path"].exists()


def test_openclaw_persistent_gateway_logs_are_exact_secret_redacted(tmp_path: Path):
    state_dir = tmp_path / "data" / "openclaw"
    state_dir.mkdir(parents=True)
    log_path = state_dir / "gateway-startup.log"
    canary = "configured-log-secret-canary"
    log_path.write_text(f"before {canary} after\n", encoding="utf-8")

    bridge = OpenClawBridge(
        enabled=False,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
        provider_env={"DEEPSEEK_API_KEY": canary},
    )

    assert canary not in log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in log_path.read_text(encoding="utf-8")
    bridge._redacting_gateway_log_pump(
        io.BytesIO(f"child stdout {canary}\nclean line\n".encode())
    )
    persisted = log_path.read_text(encoding="utf-8")
    assert canary not in persisted
    assert "clean line" in persisted
    assert canary not in bridge._startup_log_tail()


@pytest.mark.asyncio
async def test_openclaw_gateway_token_migrates_to_vault_and_never_enters_argv(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "data" / "openclaw"
    state_dir.mkdir(parents=True)
    token_path = state_dir / "gateway.token"
    token = "legacy-gateway-token-" + "x" * 48
    token_path.write_text(token, encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"ELREN_OPENCLAW_GATEWAY_TOKEN={token}\nELREN_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    bridge = OpenClawBridge(
        enabled=True,
        cli="",
        gateway_url="ws://127.0.0.1:18789",
        gateway_token="",
        auto_start=False,
        workspace=tmp_path,
    )

    assert bridge.gateway_token == token
    encrypted = token_path.read_bytes()
    assert LocalSecretVault.is_encrypted_bytes(encrypted)
    assert token.encode() not in encrypted
    assert LocalSecretVault(token_path).read().values == {"gateway_token": token}
    env_text = env_path.read_text(encoding="utf-8")
    assert "ELREN_OPENCLAW_GATEWAY_TOKEN=\n" in env_text
    assert "ELREN_HOST=127.0.0.1" in env_text
    assert token not in bridge.config_path.read_text(encoding="utf-8")

    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"{}", b""

    async def create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return Process()

    monkeypatch.setattr(bridge, "resolve_cli", lambda: "selected-openclaw")
    monkeypatch.setattr(
        bridge,
        "_cli_command",
        lambda _cli, *args: ["selected-openclaw", *args],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    assert await bridge._gateway_call("tools.catalog", {}) == {}
    assert token not in captured["args"]
    assert captured["env"]["OPENCLAW_GATEWAY_TOKEN"] == token


def test_gateway_token_vault_replace_failure_keeps_only_legacy_copy_for_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "data" / "openclaw"
    state_dir.mkdir(parents=True)
    token_path = state_dir / "gateway.token"
    token = "only-gateway-copy-" + "z" * 48
    token_path.write_text(token, encoding="utf-8")
    original_replace = os.replace

    def fail_token_replace(source, target):
        if Path(target) == token_path:
            raise OSError("simulated gateway vault failure")
        return original_replace(source, target)

    monkeypatch.setattr("deepdesk.secret_storage.os.replace", fail_token_replace)

    with pytest.raises(OSError, match="simulated gateway vault failure"):
        OpenClawBridge(
            enabled=True,
            cli="",
            gateway_url="ws://127.0.0.1:18789",
            gateway_token="",
            auto_start=False,
            workspace=tmp_path,
        )

    assert token_path.read_text(encoding="utf-8") == token
    assert not list(state_dir.glob(f".{token_path.name}.*.tmp"))
