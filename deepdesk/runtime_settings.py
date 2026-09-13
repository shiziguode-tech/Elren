from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deepdesk.custom_providers import normalize_custom_provider
from deepdesk.model_capabilities import is_retired_deepseek_selector
from deepdesk.models import AgentProfile, ApprovalPolicy, DiscussionTeamMember
from deepdesk.speech_transcription import SpeechLanguage


class RuntimeSettings(BaseModel):
    model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=240)
    reasoning_effort: Literal["auto", "minimal", "low", "medium", "high", "xhigh", "max"] = "high"
    default_agent: AgentProfile = AgentProfile.GENERAL
    default_policy: ApprovalPolicy = ApprovalPolicy.AUTONOMOUS
    compact_trace: bool = True
    cross_conversation_context: bool = True
    custom_system_prompt_suffix: str = Field(default="", max_length=20_000)
    discussion_team_enabled: bool = False
    discussion_team: list[DiscussionTeamMember] = Field(default_factory=list)
    max_steps: int | None = Field(default=None, ge=1, le=10_000)
    max_output_tokens: int | None = Field(default=None, ge=1024, le=384_000)
    request_timeout: float = Field(default=180, ge=10, le=600)
    voice_language: SpeechLanguage = "auto"
    voice_name: str = Field(default="", max_length=240)
    voice_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    voice_auto_speak: bool = True
    voice_hands_free: bool = True
    voice_auto_continue: bool = True
    theme_mode: Literal["dark", "light", "system"] = "light"
    theme_accent: str = Field(default="#a6533f", pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("model")
    @classmethod
    def migrate_retired_model(cls, value):
        return "auto" if is_retired_deepseek_selector(value) else value

    @model_validator(mode="after")
    def validate_discussion_team(self) -> RuntimeSettings:
        for member in self.discussion_team:
            if is_retired_deepseek_selector(member.model):
                member.model = "auto"
        if self.discussion_team:
            if len(self.discussion_team) < 2:
                raise ValueError("Discussion team requires at least two participants")
            leaders = [member for member in self.discussion_team if member.role == "leader"]
            if len(leaders) != 1:
                raise ValueError("Discussion team requires exactly one leader")
            ids = [member.id for member in self.discussion_team]
            if len(ids) != len(set(ids)):
                raise ValueError("Discussion team participant IDs must be unique")
        # A valid saved roster is the configuration flag.  Keep the old field
        # only for data/API compatibility; the UI must not require a second
        # opt-in after the user has already configured a team.
        self.discussion_team_enabled = bool(self.discussion_team)
        return self


class RuntimeSettingsPatch(BaseModel):
    # A newer UI must never receive a false "saved" response from an older or
    # mismatched backend that silently discards fields it does not understand.
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=240)

    @field_validator("model")
    @classmethod
    def migrate_retired_model(cls, value):
        return "auto" if is_retired_deepseek_selector(value) else value
    reasoning_effort: Literal["auto", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    # Retired UI field retained as a no-op transport compatibility field for
    # older clients. RuntimeSettingsStore deliberately discards it.
    default_agent: AgentProfile | None = None
    # Retired user-facing field. Older clients may still send it, but every
    # current task uses autonomous execution and the store discards the value.
    default_policy: ApprovalPolicy | None = None
    compact_trace: bool | None = None
    cross_conversation_context: bool | None = None
    custom_system_prompt_suffix: str | None = Field(default=None, max_length=20_000)
    discussion_team_enabled: bool | None = None
    discussion_team: list[DiscussionTeamMember] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=10_000)
    max_output_tokens: int | None = Field(default=None, ge=1024, le=384_000)
    request_timeout: float | None = Field(default=None, ge=10, le=600)
    voice_language: SpeechLanguage | None = None
    voice_name: str | None = Field(default=None, max_length=240)
    voice_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    voice_auto_speak: bool | None = None
    voice_hands_free: bool | None = None
    voice_auto_continue: bool | None = None
    theme_mode: Literal["dark", "light", "system"] | None = None
    theme_accent: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    # Optional write-only provider credentials. They are consumed by the API
    # route and stored separately; RuntimeSettings never serializes them.
    deepseek_api_key: str | None = None
    deepseek_backup_api_key: str | None = None
    gemini_api_key: str | None = None
    pollinations_api_key: str | None = None
    huggingface_token: str | None = None
    aicodemirror_api_key: str | None = None
    aicodemirror_fable_api_key: str | None = None
    model_providers: list[dict[str, Any]] | None = Field(default=None, max_length=25)

    @field_validator("model_providers")
    @classmethod
    def validate_model_providers(cls, entries):
        if entries is None:
            return None
        ids = [entry.get("id") for entry in entries if entry.get("id")]
        if any(not isinstance(value, str) for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("Model configuration IDs must be unique strings")
        return [normalize_custom_provider(entry) for entry in entries]

    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_open_id: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    github_token: str | None = None
    google_places_api_key: str | None = None
    trello_api_key: str | None = None
    trello_token: str | None = None
    elevenlabs_api_key: str | None = None
    notion_token: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    op_service_account_token: str | None = None
    giphy_api_key: str | None = None
    tenor_api_key: str | None = None
    apify_api_token: str | None = None
    firecrawl_api_key: str | None = None
    eightctl_email: str | None = None
    eightctl_password: str | None = None
    deliveroo_bearer_token: str | None = None
    deliveroo_cookie: str | None = None
    things_auth_token: str | None = None
    sag_api_key: str | None = None

    @model_validator(mode="after")
    def validate_discussion_team_patch(self) -> RuntimeSettingsPatch:
        if self.discussion_team is not None:
            if not self.discussion_team:
                self.discussion_team_enabled = False
                return self
            if len(self.discussion_team) < 2:
                raise ValueError("Discussion team requires at least two participants")
            leaders = [member for member in self.discussion_team if member.role == "leader"]
            if len(leaders) != 1:
                raise ValueError("Discussion team requires exactly one leader")
            ids = [member.id for member in self.discussion_team]
            if len(ids) != len(set(ids)):
                raise ValueError("Discussion team participant IDs must be unique")
            self.discussion_team_enabled = True
        return self


class RuntimeSettingsStore:
    def __init__(self, path: Path, defaults: RuntimeSettings) -> None:
        self.path = path
        self._update_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value = defaults
        if path.is_file():
            try:
                self.value = RuntimeSettings.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        # Agent specialization is now an internal routing concern.  The user
        # surface is always the general controller, including older settings
        # files that previously selected a specialist profile.
        self.value = self.value.model_copy(
            update={
                "default_agent": AgentProfile.GENERAL,
                "default_policy": ApprovalPolicy.AUTONOMOUS,
                "max_steps": None,
            }
        )

    def update(self, patch: RuntimeSettingsPatch) -> RuntimeSettings:
        # UI, voice, and remote-channel setting changes can arrive together.
        # Serialize the read-modify-write transaction so they cannot race over
        # the shared temporary file or publish a partially stale snapshot.
        with self._update_lock:
            return self._update_unlocked(patch)

    def _update_unlocked(self, patch: RuntimeSettingsPatch) -> RuntimeSettings:
        updated = self.preview(patch)
        self._persist_unlocked(updated)
        self.value = updated
        return updated

    def preview(self, patch: RuntimeSettingsPatch) -> RuntimeSettings:
        """Validate a detached candidate without changing disk or live state."""
        data = self.value.model_dump()
        changes = patch.model_dump(exclude_unset=True)
        changes.pop("default_agent", None)
        changes.pop("default_policy", None)
        # Numeric step limits are a retired compatibility field.  Agent work
        # continues until completion or an explicit user stop.
        changes.pop("max_steps", None)
        # ``max_output_tokens=null`` means the provider maximum. Other nullable patch
        # fields are transport conveniences; treating an explicit JSON null as a
        # stored value would turn a harmless partial update into a server error.
        for field in (
            "model",
            "reasoning_effort",
            "default_agent",
            "compact_trace",
            "cross_conversation_context",
            "custom_system_prompt_suffix",
            "discussion_team_enabled",
            "discussion_team",
            "request_timeout",
            "voice_language",
            "voice_name",
            "voice_rate",
            "voice_auto_speak",
            "voice_hands_free",
            "voice_auto_continue",
            "theme_mode",
            "theme_accent",
        ):
            if changes.get(field) is None:
                changes.pop(field, None)
        runtime_fields = {
            "model",
            "reasoning_effort",
            "compact_trace",
            "cross_conversation_context",
            "custom_system_prompt_suffix",
            "discussion_team_enabled",
            "discussion_team",
            "max_output_tokens",
            "request_timeout",
            "voice_language",
            "voice_name",
            "voice_rate",
            "voice_auto_speak",
            "voice_hands_free",
            "voice_auto_continue",
            "theme_mode",
            "theme_accent",
        }
        data.update({key: value for key, value in changes.items() if key in runtime_fields})
        data["default_agent"] = AgentProfile.GENERAL
        data["default_policy"] = ApprovalPolicy.AUTONOMOUS
        data["max_steps"] = None
        # Validate and persist a detached candidate before publishing it to
        # readers.  Assigning ``self.value`` first made a disk-full/permission
        # error return HTTP 500 while the running process nevertheless used
        # settings that would disappear after restart.
        return RuntimeSettings.model_validate(data)

    def _persist_unlocked(self, updated: RuntimeSettings) -> None:
        payload = json.dumps(
            updated.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows can briefly deny an atomic replace while Defender,
            # indexing, or another reader is closing the destination.  The
            # unique transaction file removes writer collisions; this short,
            # bounded retry absorbs only that transient OS condition.
            for attempt in range(6):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)
