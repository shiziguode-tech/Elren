from pathlib import Path

from deepdesk.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_matches_runtime_defaults_and_documents_operational_knobs() -> None:
    source = (ROOT / ".env.example").read_text(encoding="utf-8")
    values = {}
    for line in source.splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value

    settings = Settings(_env_file=None)
    assert values["ELREN_VISION_BASE_URL"] == settings.deepdesk_vision_base_url
    assert values["ELREN_VISION_MODEL"] == settings.deepdesk_vision_model
    assert values["ELREN_VISION_AUTO_DISCOVER"].lower() == str(
        settings.deepdesk_vision_auto_discover
    ).lower()
    for required in (
        "ELREN_POLLINATIONS_API_KEY",
        "ELREN_MOBILE_PORT",
        "ELREN_MAX_OUTPUT_TOKENS",
        "ELREN_REQUEST_TIMEOUT",
    ):
        assert required in values
    assert "ELREN_MAX_STEPS" not in values
    assert not any(token in name for name in values for token in ('OKX', 'BINANCE', 'FINANCE'))
    assert not hasattr(settings, "deepdesk_max_steps")


def test_default_vision_chain_contains_no_removed_deepseek_ocr_model() -> None:
    settings = Settings(_env_file=None)
    assert "deepseek" not in settings.deepdesk_vision_model.casefold()


def test_default_workspace_is_package_root_not_process_cwd(
    monkeypatch, tmp_path: Path
) -> None:
    for name in ("ELREN_WORKSPACE", "MILO_WORKSPACE", "DEEPDESK_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    assert Settings(_env_file=None).workspace == ROOT
    assert not (tmp_path / "data").exists()


def test_default_dotenv_follows_workspace_instead_of_process_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "portable folder"
    unrelated = tmp_path / "unrelated cwd"
    workspace.mkdir()
    unrelated.mkdir()
    (workspace / ".env").write_text("ELREN_PORT=9543\n", encoding="utf-8")
    (unrelated / ".env").write_text("ELREN_PORT=9999\n", encoding="utf-8")
    monkeypatch.setenv("ELREN_WORKSPACE", str(workspace))
    monkeypatch.delenv("ELREN_PORT", raising=False)
    monkeypatch.chdir(unrelated)

    settings = Settings()

    assert settings.workspace == workspace.resolve()
    assert settings.deepdesk_port == 9543


def test_elren_environment_identity_precedes_legacy_alias(monkeypatch) -> None:
    monkeypatch.setenv("DEEPDESK_PORT", "9666")
    monkeypatch.setenv("MILO_PORT", "9711")
    monkeypatch.setenv("ELREN_PORT", "9777")
    assert Settings(_env_file=None).deepdesk_port == 9777

    monkeypatch.delenv("ELREN_PORT")
    assert Settings(_env_file=None).deepdesk_port == 9711

    monkeypatch.delenv("MILO_PORT")
    assert Settings(_env_file=None).deepdesk_port == 9666
