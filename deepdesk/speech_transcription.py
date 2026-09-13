from __future__ import annotations

import ctypes
import locale
import mimetypes
import os
from pathlib import Path
from typing import Literal

import httpx

SUPPORTED_SPEECH_LANGUAGES = (
    "zh-CN", "en-US", "ja-JP", "ko-KR", "fr-FR", "de-DE", "es-ES", "it-IT",
    "pt-BR", "ru-RU", "ar-SA", "hi-IN", "th-TH", "vi-VN", "id-ID", "ms-MY",
    "tr-TR", "pl-PL", "nl-NL", "uk-UA",
)
SpeechLanguage = Literal[
    "auto", "zh-CN", "en-US", "ja-JP", "ko-KR", "fr-FR", "de-DE", "es-ES",
    "it-IT", "pt-BR", "ru-RU", "ar-SA", "hi-IN", "th-TH", "vi-VN", "id-ID",
    "ms-MY", "tr-TR", "pl-PL", "nl-NL", "uk-UA",
]


def _normalize_speech_language(value: str) -> str:
    value = str(value or "").strip().replace("_", "-")
    exact = next(
        (item for item in SUPPORTED_SPEECH_LANGUAGES if item.casefold() == value.casefold()),
        "",
    )
    if exact:
        return exact
    prefix = value.split("-", 1)[0].casefold()
    return next(
        (item for item in SUPPORTED_SPEECH_LANGUAGES if item.split("-", 1)[0].casefold() == prefix),
        "",
    )


def system_speech_language() -> str:
    """Return the Windows system locale, independent of Elren's UI language."""

    candidates: list[str] = []
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(85)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if kernel32.GetSystemDefaultLocaleName(buffer, len(buffer)):
                candidates.append(buffer.value)
        except (AttributeError, OSError):
            pass
    try:
        language, _encoding = locale.getlocale()
        if language:
            candidates.append(language)
    except (ValueError, TypeError):
        pass
    for candidate in candidates:
        normalized = _normalize_speech_language(candidate)
        if normalized:
            return normalized
    return "en-US"


class SpeechTranscriber:
    """Transcribe remote voice notes before DeepSeek reasons about the text.

    DeepSeek's public API has no speech endpoint. The included free path uses a
    user-supplied Hugging Face Read token and Whisper; no transcript is stored
    by this class after it is returned to the task pipeline.
    """

    ENDPOINTS = (
        "https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3-turbo",
        "https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3",
    )

    def __init__(self, token_provider) -> None:
        self.token_provider = token_provider

    async def transcribe(self, path: Path) -> str:
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Voice note exceeds the 20 MB transcription limit")
        token = str(self.token_provider() or "").strip()
        if not token:
            raise RuntimeError("Hugging Face Read token is required for Telegram voice transcription")
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "audio/ogg"
        errors: list[str] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15)) as client:
            for endpoint in self.ENDPOINTS:
                try:
                    response = await client.post(
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": content_type,
                            "Accept": "application/json",
                        },
                        content=content,
                    )
                    if response.status_code >= 400:
                        errors.append(f"{response.status_code}: {response.text[:180]}")
                        continue
                    payload = response.json()
                    text = str(payload.get("text") if isinstance(payload, dict) else "").strip()
                    if text:
                        return text
                    errors.append("empty transcript")
                except (httpx.HTTPError, ValueError) as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        raise RuntimeError("Free Whisper transcription failed: " + "; ".join(errors[-2:]))
