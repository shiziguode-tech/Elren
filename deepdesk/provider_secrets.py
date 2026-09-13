from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from deepdesk.custom_providers import (
    is_custom_provider,
    normalize_custom_provider,
    provider_selector,
)
from deepdesk.secret_storage import (
    LocalSecretVault,
    SecretProtector,
    SecretStorageFormatError,
    atomic_write_secure,
    scrub_env_file,
)

MODEL_PROVIDERS = {"openai", "anthropic", "google", "xai"}


def _stored_text(data: dict[str, object], name: str, default: str) -> str:
    value = data.get(name, default)
    return str(default if value is None else value)


@dataclass(slots=True, repr=False)
class ProviderSecrets:
    """Local provider credentials; values never appear in the public API or repr."""

    deepseek_primary: str = ""
    deepseek_backup: str = ""
    gemini: str = ""
    pollinations: str = ""
    huggingface: str = ""
    aicodemirror: str = ""
    aicodemirror_fable: str = ""
    model_providers: list[dict[str, Any]] = field(default_factory=list)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_open_id: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    github_token: str = ""
    google_places_api_key: str = ""
    trello_api_key: str = ""
    trello_token: str = ""
    elevenlabs_api_key: str = ""
    notion_token: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    op_service_account_token: str = ""
    giphy_api_key: str = ""
    tenor_api_key: str = ""
    apify_api_token: str = ""
    firecrawl_api_key: str = ""
    eightctl_email: str = ""
    eightctl_password: str = ""
    deliveroo_bearer_token: str = ""
    deliveroo_cookie: str = ""
    things_auth_token: str = ""
    sag_api_key: str = ""
    # Kept separate from the primary/backup chat-model keys because older
    # OpenClaw provider catalogs could contain an independently managed key.
    # This value is injected only into the isolated OpenClaw process.
    openclaw_deepseek_api_key: str = ""

    def __repr__(self) -> str:
        return "ProviderSecrets(<redacted>)"


_RUNTIME_RENAMES = {
    "deepseek_api_key": "deepseek_primary",
    "deepseek_backup_api_key": "deepseek_backup",
    "gemini_api_key": "gemini",
    "pollinations_api_key": "pollinations",
    "huggingface_token": "huggingface",
    "aicodemirror_api_key": "aicodemirror",
    "aicodemirror_fable_api_key": "aicodemirror_fable",
}
_SECRET_FIELD_NAMES = {item.name for item in fields(ProviderSecrets)}
_RUNTIME_SECRET_FIELDS = {
    **{name: name for name in _SECRET_FIELD_NAMES},
    **_RUNTIME_RENAMES,
}
_ENV_BASE_NAMES = {
    name: f"DEEPDESK_{name.upper()}" for name in _SECRET_FIELD_NAMES
}
_ENV_BASE_NAMES.update(
    {
        "deepseek_primary": "DEEPSEEK_API_KEY",
        "deepseek_backup": "DEEPSEEK_BACKUP_API_KEY",
        "gemini": "DEEPDESK_VISION_API_KEY",
        "pollinations": "DEEPDESK_POLLINATIONS_API_KEY",
        "huggingface": "DEEPDESK_HUGGINGFACE_TOKEN",
        "aicodemirror": "DEEPDESK_AICODEMIRROR_API_KEY",
        "aicodemirror_fable": "DEEPDESK_AICODEMIRROR_FABLE_API_KEY",
    }
)
_FIELD_ENV_EXTRAS: dict[str, frozenset[str]] = {
    "gemini": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    "huggingface": frozenset({"HUGGINGFACE_TOKEN", "HF_TOKEN"}),
    "github_token": frozenset({"ELREN_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}),
    "google_places_api_key": frozenset({"GOOGLE_PLACES_API_KEY"}),
    "trello_api_key": frozenset({"TRELLO_API_KEY"}),
    "trello_token": frozenset({"TRELLO_TOKEN"}),
    "elevenlabs_api_key": frozenset({"ELEVENLABS_API_KEY"}),
    "notion_token": frozenset({"NOTION_TOKEN", "NOTION_API_TOKEN"}),
    "spotify_client_id": frozenset({"SPOTIFY_CLIENT_ID"}),
    "spotify_client_secret": frozenset({"SPOTIFY_CLIENT_SECRET"}),
    "op_service_account_token": frozenset({"OP_SERVICE_ACCOUNT_TOKEN"}),
    "giphy_api_key": frozenset({"GIPHY_API_KEY"}),
    "tenor_api_key": frozenset({"TENOR_API_KEY"}),
    "apify_api_token": frozenset({"APIFY_API_TOKEN"}),
    "firecrawl_api_key": frozenset({"FIRECRAWL_API_KEY"}),
    "eightctl_email": frozenset({"EIGHTCTL_EMAIL"}),
    "eightctl_password": frozenset({"EIGHTCTL_PASSWORD"}),
    "deliveroo_bearer_token": frozenset({"DELIVEROO_BEARER_TOKEN"}),
    "deliveroo_cookie": frozenset({"DELIVEROO_COOKIE"}),
    "things_auth_token": frozenset({"THINGS_AUTH_TOKEN"}),
    "sag_api_key": frozenset({"SAG_API_KEY"}),
}
_MODEL_PROVIDER_ENV = {
    "openai": frozenset({"OPENAI_API_KEY"}),
    "anthropic": frozenset({"ANTHROPIC_API_KEY"}),
    "google": frozenset(
        {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"}
    ),
    "xai": frozenset({"XAI_API_KEY"}),
}


class ProviderSecretsStore:
    """Crash-safe OS-protected provider credential persistence and migration."""

    def __init__(
        self,
        path: Path,
        defaults: ProviderSecrets,
        *,
        legacy_path: Path | None = None,
        legacy_env_path: Path | None = None,
        protector: SecretProtector | None = None,
    ) -> None:
        self.path = Path(path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.legacy_env_path = Path(legacy_env_path) if legacy_env_path else None
        self.runtime_settings_path = self.path.parent / "runtime-settings.json"
        self._update_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._vault = LocalSecretVault(self.path, protector)

        base = asdict(defaults)
        runtime_values = self._legacy_runtime_secret_data()
        self._overlay(base, runtime_values, authoritative=False)

        legacy_values: dict[str, Any] = {}
        if self.legacy_path and self.legacy_path.is_file():
            legacy_values = self._read_mapping(self.legacy_path)
            self._overlay(base, legacy_values, authoritative=True)

        canonical_encrypted = False
        canonical_values: dict[str, Any] = {}
        if self.path.is_file():
            raw = self.path.read_bytes()
            canonical_encrypted = LocalSecretVault.is_encrypted_bytes(raw)
            canonical_values = (
                self._vault.read().values
                if canonical_encrypted
                else self._parse_legacy_json(raw, self.path)
            )
            self._overlay(base, canonical_values, authoritative=True)

        loaded = self._from_mapping(base, defaults)
        serialized = asdict(loaded)
        needs_persist = (
            self.path.is_file()
            or bool(legacy_values)
            or bool(runtime_values)
            or self._contains_persistable_values(loaded)
        )
        if needs_persist and (
            not canonical_encrypted or canonical_values != serialized
        ):
            self._vault.write_verified(serialized)

        # Cleanup comes only after a verified vault write/read. Any failure
        # leaves every plaintext only-copy intact for a safe next-start retry.
        if needs_persist:
            self._scrub_legacy_env(loaded)
            self._scrub_legacy_runtime_settings()
            self._remove_legacy_files()
        self.value = loaded

    @property
    def protector(self) -> SecretProtector:
        """Return the selected current-user protector for sibling local stores."""

        return self._vault.protector

    @staticmethod
    def _overlay(
        target: dict[str, Any],
        values: dict[str, Any],
        *,
        authoritative: bool,
    ) -> None:
        for name, value in values.items():
            if name not in _SECRET_FIELD_NAMES or value is None:
                continue
            if name == "model_providers":
                if isinstance(value, list) and (authoritative or value):
                    target[name] = value
            # Empty strings in an older/canonical store are not meaningful
            # clears: write-only settings updates deliberately preserve a
            # secret when their input is blank.  Keeping a non-empty default
            # here also lets a legacy .env value migrate into an otherwise
            # empty encrypted vault before that plaintext copy is scrubbed.
            elif str(value).strip():
                target[name] = value

    @staticmethod
    def _parse_legacy_json(raw: bytes, source: Path) -> dict[str, Any]:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStorageFormatError(
                f"Legacy credential store {source.name} is malformed"
            ) from exc
        if not isinstance(decoded, dict):
            raise SecretStorageFormatError(
                f"Legacy credential store {source.name} must contain an object"
            )
        return decoded

    def _read_mapping(self, source: Path) -> dict[str, Any]:
        raw = source.read_bytes()
        if LocalSecretVault.is_encrypted_bytes(raw):
            return LocalSecretVault(source, self._vault.protector).read().values
        return self._parse_legacy_json(raw, source)

    def _from_mapping(
        self, data: dict[str, Any], defaults: ProviderSecrets
    ) -> ProviderSecrets:
        scalar_values: dict[str, Any] = {}
        for item in fields(ProviderSecrets):
            if item.name != "model_providers":
                scalar_values[item.name] = _stored_text(
                    data, item.name, getattr(defaults, item.name)
                )
        scalar_values["aicodemirror"] = str(
            data.get("aicodemirror")
            or data.get("aicodemirror_claude")
            or data.get("aicodemirror_openai")
            or data.get("aicodemirror_gemini")
            or defaults.aicodemirror
        )
        scalar_values["aicodemirror_fable"] = str(
            data.get("aicodemirror_fable") or defaults.aicodemirror_fable
        )
        scalar_values["model_providers"] = self._normalize_model_providers(
            data.get("model_providers", defaults.model_providers),
            defaults.model_providers,
        )
        return ProviderSecrets(**scalar_values)

    @staticmethod
    def _contains_persistable_values(value: ProviderSecrets) -> bool:
        values = asdict(value)
        return any(bool(item) for item in values.values())

    def _legacy_runtime_secret_data(self) -> dict[str, Any]:
        if not self.runtime_settings_path.is_file():
            return {}
        try:
            decoded = json.loads(self.runtime_settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStorageFormatError(
                "runtime-settings.json is malformed; credential migration stopped"
            ) from exc
        if not isinstance(decoded, dict):
            raise SecretStorageFormatError("runtime-settings.json must contain an object")
        return {
            target: decoded[source]
            for source, target in _RUNTIME_SECRET_FIELDS.items()
            if source in decoded and decoded[source] is not None
        }

    def _scrub_legacy_runtime_settings(self) -> None:
        if not self.runtime_settings_path.is_file():
            return
        try:
            decoded = json.loads(self.runtime_settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStorageFormatError(
                "runtime-settings.json is malformed; plaintext cleanup stopped"
            ) from exc
        if not isinstance(decoded, dict):
            raise SecretStorageFormatError("runtime-settings.json must contain an object")
        changed = False
        for name in _RUNTIME_SECRET_FIELDS:
            if name in decoded:
                decoded.pop(name, None)
                changed = True
        if changed:
            atomic_write_secure(
                self.runtime_settings_path,
                (json.dumps(decoded, ensure_ascii=False, indent=2) + "\n").encode(),
            )

    def _scrub_legacy_env(self, value: ProviderSecrets) -> None:
        if not self.legacy_env_path:
            return
        names: set[str] = set()
        for field_name, env_name in _ENV_BASE_NAMES.items():
            if field_name != "model_providers" and getattr(value, field_name):
                names.add(env_name)
                names.update(_FIELD_ENV_EXTRAS.get(field_name, ()))
        for provider in value.model_providers:
            if provider.get("api_key"):
                names.update(_MODEL_PROVIDER_ENV.get(provider.get("provider", ""), ()))
        scrub_env_file(self.legacy_env_path, frozenset(names))

    def _remove_legacy_files(self) -> None:
        if not self.legacy_path or self.legacy_path == self.path:
            return
        candidates = {
            self.legacy_path,
            self.legacy_path.with_suffix(".tmp"),
            self.legacy_path.with_suffix(f"{self.legacy_path.suffix}.tmp"),
            self.legacy_path.with_name(f"{self.legacy_path.name}.bak"),
        }
        candidates.update(self.legacy_path.parent.glob(f"{self.legacy_path.name}.bak*"))
        candidates.update(self.legacy_path.parent.glob(f".{self.legacy_path.name}.*.tmp"))
        for candidate in candidates:
            if candidate.is_file():
                candidate.unlink()

    def _persist_unlocked(self, value: ProviderSecrets) -> None:
        self._vault.write_verified(asdict(value))

    def _update_unlocked(self, **changes: Any) -> ProviderSecrets:
        unknown = set(changes) - _SECRET_FIELD_NAMES
        if unknown:
            raise TypeError(f"Unknown provider secret fields: {', '.join(sorted(unknown))}")
        updated = replace(
            self.value,
            model_providers=deepcopy(self.value.model_providers),
        )
        model_providers = changes.pop("model_providers", None)
        for name, candidate in changes.items():
            if candidate is not None and str(candidate).strip():
                setattr(updated, name, str(candidate).strip())
        if model_providers is not None:
            updated.model_providers = self._normalize_model_providers(
                model_providers, self.value.model_providers
            )
        self._persist_unlocked(updated)
        self.value = updated
        return self.value

    def update(self, **changes: Any) -> ProviderSecrets:
        with self._update_lock:
            return self._update_unlocked(**changes)

    def preview_model_providers(
        self, values: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        with self._update_lock:
            if values is None:
                return deepcopy(self.value.model_providers)
            return self._normalize_model_providers(values, self.value.model_providers)

    @staticmethod
    def _normalize_model_providers(
        values: object, previous: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(values, list):
            return list(previous)
        old_by_id = {str(item.get("id") or ""): item for item in previous}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in values[:25]:
            if not isinstance(raw, dict):
                continue
            raw = normalize_custom_provider(raw)
            provider = str(raw.get("provider") or "").strip().lower()
            model = str(raw.get("model") or "").strip()
            if provider not in MODEL_PROVIDERS or not model or len(model) > 200:
                continue
            entry_id = str(raw.get("id") or "").strip() or uuid4().hex
            if not re_fullmatch_identifier(entry_id):
                entry_id = uuid4().hex
            old = old_by_id.get(entry_id, {})
            same_endpoint = (
                str(old.get("provider") or "") == provider
                and str(old.get("model") or "") == model
                and old.get("base_url") == raw.get("base_url")
                and is_custom_provider(old) == is_custom_provider(raw)
            )
            api_key = str(raw.get("api_key") or "").strip()
            if not api_key and same_endpoint:
                api_key = str(old.get("api_key") or "")
            selector = provider_selector({**raw, "id": entry_id})
            if selector.casefold() in seen:
                continue
            seen.add(selector.casefold())
            normalized.append(
                {
                    "id": entry_id,
                    "provider": provider,
                    "model": model,
                    "api_key": api_key,
                    **({"source": "custom", "base_url": raw["base_url"],
                        "reasoning_levels": list(raw["reasoning_levels"])}
                       if is_custom_provider(raw) else {}),
                }
            )
        return normalized

    def model_provider_status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item["id"],
                "provider": item["provider"],
                "model": item["model"],
                "selector": provider_selector(item),
                "configured": bool(item.get("api_key")),
                **({"source": "custom", "base_url": item["base_url"],
                    "reasoning_levels": list(item.get("reasoning_levels", []))}
                   if is_custom_provider(item) else {}),
            }
            for item in self.value.model_providers
        ]

    def status(self) -> dict[str, bool | int]:
        return {
            "primary_key_configured": bool(self.value.deepseek_primary),
            "backup_key_configured": bool(self.value.deepseek_backup),
            "openclaw_deepseek_key_configured": bool(
                self.value.openclaw_deepseek_api_key
            ),
            "gemini_key_configured": bool(self.value.gemini),
            "pollinations_key_configured": bool(self.value.pollinations),
            "huggingface_token_configured": bool(self.value.huggingface),
            "aicodemirror_key_configured": bool(self.value.aicodemirror),
            "aicodemirror_fable_key_configured": bool(self.value.aicodemirror_fable),
            "model_provider_count": (
                sum(bool(item.get("api_key")) for item in self.value.model_providers)
                + int(bool(self.value.aicodemirror))
                + int(bool(self.value.aicodemirror_fable) and not self.value.aicodemirror)
            ),
            "feishu_configured": bool(
                self.value.feishu_app_id and self.value.feishu_app_secret
            ),
            "feishu_open_id_configured": bool(self.value.feishu_open_id),
            "telegram_configured": bool(self.value.telegram_bot_token),
            "telegram_chat_id_configured": bool(self.value.telegram_chat_id),
            "github_token_configured": bool(self.value.github_token),
            "google_places_key_configured": bool(self.value.google_places_api_key),
            "trello_configured": bool(
                self.value.trello_api_key and self.value.trello_token
            ),
            "elevenlabs_key_configured": bool(self.value.elevenlabs_api_key),
            "notion_token_configured": bool(self.value.notion_token),
            "spotify_configured": bool(
                self.value.spotify_client_id and self.value.spotify_client_secret
            ),
            "onepassword_configured": bool(self.value.op_service_account_token),
            "giphy_key_configured": bool(self.value.giphy_api_key),
            "gifgrep_configured": bool(self.value.giphy_api_key),
            "tenor_key_configured": bool(self.value.tenor_api_key),
            "apify_token_configured": bool(self.value.apify_api_token),
            "firecrawl_key_configured": bool(self.value.firecrawl_api_key),
            "summarize_web_configured": bool(
                self.value.apify_api_token or self.value.firecrawl_api_key
            ),
            "eightctl_email_configured": bool(self.value.eightctl_email),
            "eightctl_password_configured": bool(self.value.eightctl_password),
            "eightctl_configured": bool(
                self.value.eightctl_email and self.value.eightctl_password
            ),
            "deliveroo_configured": bool(self.value.deliveroo_bearer_token),
            "deliveroo_token_configured": bool(self.value.deliveroo_bearer_token),
            "deliveroo_cookie_configured": bool(self.value.deliveroo_cookie),
            "things_token_configured": bool(self.value.things_auth_token),
            "sag_alt_key_configured": bool(self.value.sag_api_key),
            "sag_key_configured": bool(
                self.value.elevenlabs_api_key or self.value.sag_api_key
            ),
        }


def re_fullmatch_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(
        character.isalnum() or character in "_-" for character in value
    )
