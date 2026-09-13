from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, AliasGenerator, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from deepdesk.model_capabilities import is_retired_deepseek_selector


def _package_workspace() -> Path:
    """Anchor runtime state to the installed package, not the caller's CWD.

    Launchers set ``ELREN_WORKSPACE`` explicitly, so this is primarily the
    safe fallback for direct ``python -m deepdesk.main`` starts.  Using
    ``Path.cwd()`` here caused stray data/work/output directories whenever a
    shortcut, test runner, or service started the process from a parent folder.
    """

    return Path(__file__).resolve().parents[1]


def _workspace_env_file() -> Path:
    """Resolve .env beside the configured workspace, never beside the caller."""

    for name in ("ELREN_WORKSPACE", "MILO_WORKSPACE", "DEEPDESK_WORKSPACE"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser().resolve() / ".env"
    return _package_workspace() / ".env"


def _environment_alias(field_name: str) -> str | AliasChoices:
    """Use Elren-native environment names while accepting legacy installs."""

    if field_name.startswith("deepdesk_"):
        suffix = field_name.removeprefix("deepdesk_").upper()
        return AliasChoices(f"ELREN_{suffix}", f"MILO_{suffix}", field_name.upper())
    return field_name.upper()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # __init__ supplies an absolute, workspace-relative path. Keeping no
        # relative fallback here prevents direct starts from reading an
        # unrelated .env in the terminal's current directory.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        alias_generator=AliasGenerator(validation_alias=_environment_alias),
    )

    def __init__(self, **values: Any) -> None:
        if "_env_file" not in values:
            values["_env_file"] = _workspace_env_file()
        super().__init__(**values)

    deepseek_api_key: str = ""
    deepseek_backup_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    @field_validator("deepseek_model")
    @classmethod
    def migrate_retired_model(cls, value):
        return "deepseek-v4-flash" if is_retired_deepseek_selector(value) else value
    deepdesk_vision_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    deepdesk_vision_api_key: str = ""
    deepdesk_pollinations_api_key: str = ""
    deepdesk_huggingface_token: str = ""
    deepdesk_aicodemirror_api_key: str = ""
    deepdesk_aicodemirror_fable_api_key: str = ""
    deepdesk_vision_model: str = "gemini-3.6-flash"
    deepdesk_vision_auto_discover: bool = False
    deepdesk_vision_start_command: str = ""
    deepdesk_vision_start_timeout: float = 45.0
    deepdesk_openclaw_enabled: bool = True
    deepdesk_openclaw_cli: str = ""
    deepdesk_openclaw_gateway_url: str = "ws://127.0.0.1:18789"
    deepdesk_openclaw_gateway_token: str = ""
    deepdesk_openclaw_auto_start: bool = True
    deepdesk_feishu_enabled: bool = True
    deepdesk_feishu_base_url: str = "https://open.feishu.cn"
    deepdesk_feishu_app_id: str = ""
    deepdesk_feishu_app_secret: str = ""
    deepdesk_feishu_open_id: str = ""
    deepdesk_telegram_bot_token: str = ""
    deepdesk_telegram_chat_id: str = ""
    deepdesk_mobile_port: int = 8768
    deepdesk_github_token: str = ""
    deepdesk_google_places_api_key: str = ""
    deepdesk_trello_api_key: str = ""
    deepdesk_trello_token: str = ""
    deepdesk_elevenlabs_api_key: str = ""
    deepdesk_notion_token: str = ""
    deepdesk_spotify_client_id: str = ""
    deepdesk_spotify_client_secret: str = ""
    deepdesk_op_service_account_token: str = ""
    deepdesk_giphy_api_key: str = ""
    deepdesk_tenor_api_key: str = ""
    deepdesk_apify_api_token: str = ""
    deepdesk_firecrawl_api_key: str = ""
    deepdesk_eightctl_email: str = ""
    deepdesk_eightctl_password: str = ""
    deepdesk_deliveroo_bearer_token: str = ""
    deepdesk_deliveroo_cookie: str = ""
    deepdesk_things_auth_token: str = ""
    deepdesk_sag_api_key: str = ""
    deepdesk_host: str = "127.0.0.1"
    deepdesk_port: int = 8765
    deepdesk_workspace: Path = Field(default_factory=_package_workspace)
    deepdesk_max_output_tokens: int = 0
    deepdesk_request_timeout: float = 180.0

    @property
    def workspace(self) -> Path:
        return self.deepdesk_workspace.expanduser().resolve()

    @property
    def data_dir(self) -> Path:
        path = self.workspace / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def screenshot_dir(self) -> Path:
        path = self.workspace / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path
