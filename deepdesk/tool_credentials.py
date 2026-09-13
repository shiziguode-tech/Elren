from __future__ import annotations

from collections.abc import Callable, Mapping

# This is deliberately a closed allowlist.  A skill cannot request an arbitrary
# setting name or inherit every credential held by the Elren process.
SKILL_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "github": ("GH_TOKEN",),
    "gh-issues": ("GH_TOKEN",),
    "goplaces": ("GOOGLE_PLACES_API_KEY",),
    "trello": ("TRELLO_API_KEY", "TRELLO_TOKEN"),
    "sag": ("ELEVENLABS_API_KEY", "SAG_API_KEY"),
    "notion": ("NOTION_API_TOKEN",),
    "sonoscli": ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"),
    "openai-whisper-api": ("OPENAI_API_KEY",),
    "oracle": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "coding-agent": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"),
    "1password": ("OP_SERVICE_ACCOUNT_TOKEN",),
    "gifgrep": ("GIPHY_API_KEY", "TENOR_API_KEY"),
    "summarize": (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "XAI_API_KEY",
        "APIFY_API_TOKEN", "FIRECRAWL_API_KEY",
    ),
    "eightctl": ("EIGHTCTL_EMAIL", "EIGHTCTL_PASSWORD"),
    "ordercli": ("DELIVEROO_BEARER_TOKEN", "DELIVEROO_COOKIE"),
    "things-mac": ("THINGS_AUTH_TOKEN",),
}


class ToolCredentialResolver:
    """Return only the credential variables allowlisted for one named skill."""

    def __init__(self, values: Callable[[], Mapping[str, str]]) -> None:
        self._values = values

    def environment_for(self, skill: str) -> dict[str, str]:
        normalized = str(skill or "").strip().casefold()
        allowed = SKILL_CREDENTIAL_ENV.get(normalized, ())
        values = self._values()
        return {name: str(values.get(name) or "") for name in allowed if values.get(name)}

    def status_for(self, skill: str) -> dict[str, bool]:
        normalized = str(skill or "").strip().casefold()
        environment = self.environment_for(normalized)
        return {
            name: name in environment
            for name in SKILL_CREDENTIAL_ENV.get(normalized, ())
        }
