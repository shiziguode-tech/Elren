from __future__ import annotations

import json
import math
import os
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepdesk.config import Settings
from deepdesk.deepseek import _StreamAccumulator
from deepdesk.engine import AgentEngine
from deepdesk.main import create_app
from deepdesk.models import AgentTask, TaskStatus
from deepdesk.openclaw_bridge import OpenClawBridge
from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch, RuntimeSettingsStore
from deepdesk.secret_storage import LocalSecretVault, SecretStorageFormatError
from deepdesk.task_store import TaskStore


def _run_parallel(count: int, worker) -> list[Exception]:
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(worker, index) for index in range(count)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - asserted empty below
                errors.append(exc)
    return errors


def test_runtime_settings_survive_128_simultaneous_channel_updates(tmp_path: Path) -> None:
    path = tmp_path / "runtime-settings.json"
    store = RuntimeSettingsStore(path, RuntimeSettings())

    def update(index: int) -> None:
        store.update(
            RuntimeSettingsPatch(
                theme_accent=f"#{index % 256:02x}{index * 3 % 256:02x}{index * 7 % 256:02x}",
                request_timeout=30 + index % 90,
            )
        )

    assert _run_parallel(128, update) == []
    persisted = RuntimeSettings.model_validate_json(path.read_text(encoding="utf-8"))
    assert persisted.theme_accent.startswith("#")
    assert 30 <= persisted.request_timeout <= 119
    assert not path.with_suffix(".tmp").exists()


def test_runtime_settings_write_failure_does_not_publish_ephemeral_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-settings.json"
    store = RuntimeSettingsStore(path, RuntimeSettings(theme_accent="#010203"))

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated disk failure")

    monkeypatch.setattr("deepdesk.runtime_settings.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated disk failure"):
        store.update(RuntimeSettingsPatch(theme_accent="#abcdef"))

    assert store.value.theme_accent == "#010203"
    assert not path.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_runtime_settings_retries_transient_windows_replace_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-settings.json"
    store = RuntimeSettingsStore(path, RuntimeSettings(theme_accent="#010203"))
    original_replace = os.replace
    attempts = 0

    def transient_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise PermissionError(13, "simulated transient Windows denial", str(destination))
        original_replace(source, destination)

    monkeypatch.setattr("deepdesk.runtime_settings.os.replace", transient_replace)
    monkeypatch.setattr("deepdesk.runtime_settings.time.sleep", lambda _seconds: None)

    updated = store.update(RuntimeSettingsPatch(theme_accent="#abcdef"))

    assert attempts == 4
    assert updated.theme_accent == "#abcdef"
    assert RuntimeSettings.model_validate_json(path.read_text(encoding="utf-8")) == updated
    assert list(tmp_path.glob(".*.tmp")) == []


def test_runtime_settings_exhausted_replace_denials_preserve_old_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-settings.json"
    initial = RuntimeSettings(theme_accent="#010203")
    path.write_text(initial.model_dump_json(indent=2), encoding="utf-8")
    store = RuntimeSettingsStore(path, RuntimeSettings())
    attempts = 0

    def denied_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        del source, destination
        attempts += 1
        raise PermissionError(13, "simulated persistent Windows denial")

    monkeypatch.setattr("deepdesk.runtime_settings.os.replace", denied_replace)
    monkeypatch.setattr("deepdesk.runtime_settings.time.sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistent Windows denial"):
        store.update(RuntimeSettingsPatch(theme_accent="#abcdef"))

    assert attempts == 6
    assert store.value.theme_accent == "#010203"
    assert RuntimeSettings.model_validate_json(path.read_text(encoding="utf-8")) == initial
    assert list(tmp_path.glob(".*.tmp")) == []


def test_provider_secrets_survive_128_simultaneous_remote_and_ui_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-secrets.vault"
    store = ProviderSecretsStore(path, ProviderSecrets())

    def update(index: int) -> None:
        store.update(
            trello_api_key=f"key-{index}",
            trello_token=f"token-{index}",
        )

    assert _run_parallel(128, update) == []
    persisted = ProviderSecretsStore(path, ProviderSecrets()).value
    key_index = int(persisted.trello_api_key.removeprefix("key-"))
    token_index = int(persisted.trello_token.removeprefix("token-"))
    assert key_index == token_index
    assert LocalSecretVault.is_encrypted_bytes(path.read_bytes())
    assert b"key-" not in path.read_bytes()
    assert b"token-" not in path.read_bytes()
    assert not path.with_suffix(".tmp").exists()


def test_openclaw_config_survives_parallel_policy_and_credential_updates(
    tmp_path: Path,
) -> None:
    def update(index: int) -> None:
        bridge = OpenClawBridge(
            True,
            "",
            "ws://127.0.0.1:18789",
            "",
            False,
            tmp_path,
        )
        bridge.set_skill_credentials(
            {"github": {"GH_TOKEN": f"test-token-{index}"}}
        )

    assert _run_parallel(64, update) == []
    config = json.loads(
        (tmp_path / "data" / "openclaw" / "openclaw.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["agents"]["defaults"]["workspace"] == str(tmp_path.resolve())
    assert config["browser"]["ssrfPolicy"]["dangerouslyAllowPrivateNetwork"] is False
    assert config["tools"]["web"]["search"]["provider"] == "duckduckgo"
    assert "github" not in config.get("skills", {}).get("entries", {})
    assert "test-token-" not in json.dumps(config)


def test_live_settings_api_survives_parallel_reads_and_writes_without_secret_echo(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            deepdesk_workspace=tmp_path,
            deepdesk_openclaw_enabled=False,
            deepdesk_feishu_enabled=False,
            deepseek_api_key="",
            deepseek_backup_api_key="",
            deepdesk_vision_api_key="",
            deepdesk_telegram_bot_token="",
        )
    )
    responses = []
    with TestClient(app) as client:
        def request(index: int):
            if index % 3 == 0:
                return client.get("/api/status")
            if index % 3 == 1:
                return client.get("/api/settings")
            return client.patch(
                "/api/settings",
                json={
                    "theme_accent": f"#{index % 256:02x}{index * 3 % 256:02x}{index * 7 % 256:02x}",
                    "trello_api_key": f"api-secret-{index}",
                    "trello_token": f"token-secret-{index}",
                },
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = [future.result() for future in as_completed(
                [pool.submit(request, index) for index in range(48)]
            )]

    assert {response.status_code for response in responses} == {200}
    serialized = "".join(response.text for response in responses)
    assert "api-secret-" not in serialized
    assert "token-secret-" not in serialized
    secret_path = tmp_path / "data" / "provider-secrets.vault"
    stored = ProviderSecretsStore(secret_path, ProviderSecrets()).value
    assert stored.trello_api_key.removeprefix("api-secret-") == stored.trello_token.removeprefix(
        "token-secret-"
    )
    assert b"api-secret-" not in secret_path.read_bytes()
    assert b"token-secret-" not in secret_path.read_bytes()


def test_corrupted_settings_files_fall_back_and_are_atomically_repairable(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime-settings.json"
    secret_path = tmp_path / "provider-secrets.vault"
    runtime_path.write_bytes(b"{not-json\xff")
    secret_path.write_text('["wrong-shape"]', encoding="utf-8")

    runtime = RuntimeSettingsStore(runtime_path, RuntimeSettings(theme_accent="#010203"))
    assert runtime.value.theme_accent == "#010203"
    with pytest.raises(SecretStorageFormatError):
        ProviderSecretsStore(secret_path, ProviderSecrets(gemini="fallback-key"))
    runtime.update(RuntimeSettingsPatch(theme_accent="#abcdef"))
    assert RuntimeSettings.model_validate_json(runtime_path.read_text("utf-8")).theme_accent == "#abcdef"
    assert secret_path.read_text("utf-8") == '["wrong-shape"]'


def test_task_store_survives_parallel_writes_and_deleted_tasks_cannot_resurrect(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.db")

    def save(index: int) -> None:
        store.save(
            AgentTask(
                id=f"stress-{index:03d}",
                prompt=f"parallel prompt {index}",
                status=TaskStatus.COMPLETED,
                result=f"result {index}",
            )
        )

    assert _run_parallel(128, save) == []
    page, total = store.list_page(limit=100)
    assert total == 128
    assert len(page) == 100

    stale = store.get("stress-042")
    assert stale is not None
    assert store.delete(stale.id) is True
    stale.title = "late automatic title"
    store.save(stale)
    assert store.get(stale.id) is None
    assert store.rename(stale.id, "late user title") is None


def test_streaming_tool_calls_preserve_exact_unicode_for_80_random_chunkings() -> None:
    literal = json.dumps(
        {
            "path": r"C:\复杂 path\Project-Δ-100%.docx",
            "content": "05:30 / gas / 💡 / line1\nline2 / quote: \"literal\"",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    for seed in range(80):
        randomizer = random.Random(seed)
        cuts = sorted({randomizer.randrange(1, len(literal)) for _ in range(30)})
        chunks = [
            literal[start:end]
            for start, end in zip([0, *cuts], [*cuts, len(literal)], strict=True)
            if start < end
        ]
        accumulator = _StreamAccumulator(retain_final_reasoning=False)
        try:
            for index, chunk in enumerate(chunks):
                call: dict[str, object] = {
                    "index": 0,
                    "function": {"arguments": chunk},
                }
                if index == 0:
                    call.update({
                        "id": "call_exact",
                        "type": "function",
                        "function": {"name": "write", "arguments": chunk},
                    })
                accumulator.feed(
                    {"choices": [{"delta": {"tool_calls": [call]}}]}
                )
            result = accumulator.result()
        finally:
            accumulator.close()
        function = result["choices"][0]["message"]["tool_calls"][0]["function"]
        assert function == {"name": "write", "arguments": literal}


def test_tool_argument_validator_fuzzes_nested_finite_and_cardinality_boundaries() -> None:
    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "number", "minimum": -10, "maximum": 10},
            },
            "name": {"type": "string", "minLength": 1, "maxLength": 12},
        },
        "required": ["values", "name"],
        "additionalProperties": False,
    }
    randomizer = random.Random(20260816)
    invalid_numbers = [math.nan, math.inf, -math.inf, -11, 11]
    for _ in range(300):
        number = randomizer.choice(invalid_numbers)
        issues = AgentEngine._validate_tool_arguments(
            schema,
            {"values": [number] * randomizer.randint(1, 6), "name": "x" * randomizer.randint(0, 20)},
        )
        assert issues


@pytest.mark.skipif(os.name != "nt", reason="Windows Gradle wrapper contract")
def test_android_gradle_wrapper_fails_once_and_cleanly_when_java_is_missing() -> None:
    project = Path(__file__).resolve().parents[1] / "mobile" / "ElrenMobile"
    environment = dict(os.environ)
    environment["JAVA_HOME"] = ""
    environment["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "gradlew.bat", "--version"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}".casefold()
    # Gradle's official Windows 8.9 launcher preserves cmd.exe's 9009 code
    # when java.exe is absent; the public contract is a clean non-zero failure,
    # not a wrapper-specific normalization to 1.
    assert result.returncode != 0
    assert output.count("java_home is not set") == 1
    assert "invalid directory" not in output
    assert "not recognized" not in output
