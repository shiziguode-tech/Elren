from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import logging
import re
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from deepdesk.subprocess_env import credential_safe_environment

# ``edge_tts`` pulls in its HTTP and websocket stack. Importing it with the web
# service added noticeable latency even when the user never opened voice mode.
# Keep a patchable module slot for tests/integrations, but resolve the optional
# provider only on the first synthesis request.
edge_tts = None


def _edge_tts_available() -> bool:
    if edge_tts is not None:
        return True
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _load_edge_tts():
    global edge_tts
    if edge_tts is not None:
        return edge_tts
    try:
        edge_tts = importlib.import_module("edge_tts")
    except ImportError:  # Windows/macOS local voices remain available.
        return None
    return edge_tts


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeechAudio:
    path: Path
    media_type: str
    provider: str
    voice: str


class SpeechSynthesizer:
    """Create reply audio without relying on Web Speech support in WebView2."""

    VOICES = (
        {"name": "zh-CN-XiaoxiaoNeural", "language": "zh-CN", "label": "晓晓 · 中文女声"},
        {"name": "zh-CN-YunxiNeural", "language": "zh-CN", "label": "云希 · 中文男声"},
        {"name": "en-US-AvaNeural", "language": "en-US", "label": "Ava · English female"},
        {"name": "en-US-AndrewNeural", "language": "en-US", "label": "Andrew · English male"},
        {"name": "ja-JP-NanamiNeural", "language": "ja-JP", "label": "Nanami · 日本語"},
        {"name": "ko-KR-SunHiNeural", "language": "ko-KR", "label": "SunHi · 한국어"},
        {"name": "fr-FR-DeniseNeural", "language": "fr-FR", "label": "Denise · Français"},
        {"name": "de-DE-KatjaNeural", "language": "de-DE", "label": "Katja · Deutsch"},
        {"name": "es-ES-ElviraNeural", "language": "es-ES", "label": "Elvira · Español"},
        {"name": "it-IT-ElsaNeural", "language": "it-IT", "label": "Elsa · Italiano"},
        {"name": "pt-BR-FranciscaNeural", "language": "pt-BR", "label": "Francisca · Português"},
        {"name": "ru-RU-SvetlanaNeural", "language": "ru-RU", "label": "Svetlana · Русский"},
        {"name": "ar-SA-ZariyahNeural", "language": "ar-SA", "label": "Zariyah · العربية"},
        {"name": "hi-IN-SwaraNeural", "language": "hi-IN", "label": "Swara · हिन्दी"},
        {"name": "th-TH-PremwadeeNeural", "language": "th-TH", "label": "Premwadee · ไทย"},
        {"name": "vi-VN-HoaiMyNeural", "language": "vi-VN", "label": "HoaiMy · Tiếng Việt"},
        {"name": "id-ID-GadisNeural", "language": "id-ID", "label": "Gadis · Indonesia"},
        {"name": "ms-MY-YasminNeural", "language": "ms-MY", "label": "Yasmin · Bahasa Melayu"},
        {"name": "tr-TR-EmelNeural", "language": "tr-TR", "label": "Emel · Türkçe"},
        {"name": "pl-PL-ZofiaNeural", "language": "pl-PL", "label": "Zofia · Polski"},
        {"name": "nl-NL-ColetteNeural", "language": "nl-NL", "label": "Colette · Nederlands"},
        {"name": "uk-UA-PolinaNeural", "language": "uk-UA", "label": "Polina · Українська"},
    )

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        self._macos_say = shutil.which("say") if sys.platform == "darwin" else None
        self._windows_script = Path(__file__).with_name("windows_tts.ps1")

    def status(self) -> dict:
        online_neural = _edge_tts_available()
        return {
            "ready": bool(
                online_neural
                or (self._powershell and self._windows_script.is_file())
                or self._macos_say
            ),
            "online_neural": online_neural,
            "windows_fallback": bool(self._powershell and self._windows_script.is_file()),
            "macos_fallback": bool(self._macos_say),
            "voices": list(self.VOICES),
        }

    @staticmethod
    def _plain_text(value: str) -> str:
        text = str(value or "")
        text = re.sub(r"```[\s\S]*?```", " 代码内容 ", text)
        text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[`*_#>|~]", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:1800]

    @staticmethod
    def _language(text: str, requested: str) -> str:
        known = {item["language"] for item in SpeechSynthesizer.VOICES}
        if requested in known:
            return requested
        if re.search(r"[\u3040-\u30ff]", text):
            return "ja-JP"
        if re.search(r"[\uac00-\ud7af]", text):
            return "ko-KR"
        if re.search(r"[\u0600-\u06ff]", text):
            return "ar-SA"
        if re.search(r"[\u0900-\u097f]", text):
            return "hi-IN"
        if re.search(r"[\u0e00-\u0e7f]", text):
            return "th-TH"
        if re.search(r"[іїєґІЇЄҐ]", text):
            return "uk-UA"
        if re.search(r"[\u0400-\u04ff]", text):
            return "ru-RU"
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        return "zh-CN" if cjk >= max(1, len(text) // 12) else "en-US"

    @classmethod
    def _voice(cls, language: str, requested: str) -> str:
        known = {item["name"] for item in cls.VOICES}
        if requested in known:
            return requested
        return next(
            (item["name"] for item in cls.VOICES if item["language"] == language),
            "en-US-AvaNeural",
        )

    @staticmethod
    def _edge_rate(rate: float) -> str:
        percent = max(-50, min(100, round((float(rate) - 1.0) * 100)))
        return f"{percent:+d}%"

    def _cache_key(self, text: str, voice: str, rate: float) -> str:
        payload = f"v1\0{voice}\0{rate:.2f}\0{text}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _trim_cache(self, keep: int = 80) -> None:
        files = sorted(
            (path for path in self.cache_dir.glob("reply-*.*") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[keep:]:
            try:
                path.unlink()
            except OSError:
                pass

    async def synthesize(
        self,
        text: str,
        *,
        language: str = "auto",
        voice_name: str = "",
        rate: float = 1.0,
    ) -> SpeechAudio:
        clean = self._plain_text(text)
        if not clean:
            raise ValueError("There is no readable text in this reply")
        language = self._language(clean, language)
        voice = self._voice(language, voice_name)
        rate = max(0.5, min(2.0, float(rate)))
        digest = self._cache_key(clean, voice, rate)
        mp3_path = self.cache_dir / f"reply-{digest}.mp3"
        wav_path = self.cache_dir / f"reply-{digest}.wav"
        aiff_path = self.cache_dir / f"reply-{digest}.aiff"

        async with self._lock:
            if mp3_path.is_file() and mp3_path.stat().st_size > 512:
                return SpeechAudio(mp3_path, "audio/mpeg", "edge-neural", voice)
            if wav_path.is_file() and wav_path.stat().st_size > 512:
                return SpeechAudio(wav_path, "audio/wav", "windows-local", voice)
            if aiff_path.is_file() and aiff_path.stat().st_size > 512:
                return SpeechAudio(aiff_path, "audio/aiff", "macos-local", voice)

            neural_provider = _load_edge_tts()
            if neural_provider is not None:
                try:
                    await asyncio.wait_for(
                        neural_provider.Communicate(
                            clean,
                            voice,
                            rate=self._edge_rate(rate),
                        ).save(str(mp3_path)),
                        timeout=35,
                    )
                    if mp3_path.is_file() and mp3_path.stat().st_size > 512:
                        self._trim_cache()
                        return SpeechAudio(mp3_path, "audio/mpeg", "edge-neural", voice)
                except Exception as exc:
                    logger.warning("Online neural speech failed; using Windows voice: %s", exc)
                    mp3_path.unlink(missing_ok=True)

            if self._macos_say:
                await self._synthesize_macos(clean, aiff_path, voice_name, rate)
                self._trim_cache()
                return SpeechAudio(
                    aiff_path, "audio/aiff", "macos-local", voice_name or "system-default"
                )
            await self._synthesize_windows(clean, wav_path, voice_name, rate)
            self._trim_cache()
            return SpeechAudio(wav_path, "audio/wav", "windows-local", voice_name or "system-default")

    async def stream_mp3(
        self,
        text: str,
        *,
        language: str = "auto",
        voice_name: str = "",
        rate: float = 1.0,
    ) -> AsyncIterator[bytes]:
        """Yield neural MP3 bytes as soon as Edge TTS produces them.

        The regular ``synthesize`` method intentionally waits for a complete,
        cacheable file. Live voice needs lower time-to-first-audio, so this path
        streams the same neural voice to the browser while atomically building
        the cache in the background.
        """

        neural_provider = _load_edge_tts()
        if neural_provider is None:
            raise RuntimeError("Online neural speech streaming is unavailable")
        clean = self._plain_text(text)
        if not clean:
            raise ValueError("There is no readable text in this reply")
        language = self._language(clean, language)
        voice = self._voice(language, voice_name)
        rate = max(0.5, min(2.0, float(rate)))
        digest = self._cache_key(clean, voice, rate)
        mp3_path = self.cache_dir / f"reply-{digest}.mp3"
        if mp3_path.is_file() and mp3_path.stat().st_size > 512:
            with mp3_path.open("rb") as cached:
                while chunk := cached.read(64 * 1024):
                    yield chunk
            return

        temporary = self.cache_dir / f".reply-{digest}-{uuid4().hex}.mp3"
        produced = 0
        try:
            with temporary.open("wb") as output:
                async with asyncio.timeout(35):
                    communicate = neural_provider.Communicate(
                        clean,
                        voice,
                        rate=self._edge_rate(rate),
                    )
                    async for event in communicate.stream():
                        if event.get("type") != "audio" or not event.get("data"):
                            continue
                        chunk = bytes(event["data"])
                        output.write(chunk)
                        produced += len(chunk)
                        yield chunk
            if produced <= 512:
                raise RuntimeError("Neural speech stream returned no playable audio")
            if not mp3_path.exists():
                temporary.replace(mp3_path)
            else:
                temporary.unlink(missing_ok=True)
            self._trim_cache()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def _synthesize_windows(
        self,
        text: str,
        output_path: Path,
        voice_name: str,
        rate: float,
    ) -> None:
        if not self._powershell or not self._windows_script.is_file():
            raise RuntimeError("No speech engine is available")
        input_path = output_path.with_suffix(".txt")
        input_path.write_text(text, encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                self._powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self._windows_script),
                "-InputPath",
                str(input_path),
                "-OutputPath",
                str(output_path),
                "-VoiceName",
                voice_name,
                "-Rate",
                str(rate),
                env=credential_safe_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
            if process.returncode != 0 or not output_path.is_file():
                detail = (stderr or stdout or b"Windows speech synthesis failed").decode(
                    "utf-8", errors="replace"
                )
                raise RuntimeError(detail.strip()[:500])
        except Exception:
            output_path.unlink(missing_ok=True)
            raise
        finally:
            input_path.unlink(missing_ok=True)

    async def _synthesize_macos(
        self,
        text: str,
        output_path: Path,
        voice_name: str,
        rate: float,
    ) -> None:
        if not self._macos_say:
            raise RuntimeError("macOS speech synthesis is unavailable")
        command = [self._macos_say, "-o", str(output_path), "-r", str(round(180 * rate))]
        if voice_name and "Neural" not in voice_name:
            command.extend(["-v", voice_name])
        command.append(text)
        process = await asyncio.create_subprocess_exec(
            *command,
            env=credential_safe_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
        if process.returncode != 0 or not output_path.is_file():
            output_path.unlink(missing_ok=True)
            detail = (stderr or stdout or b"macOS speech synthesis failed").decode(
                "utf-8", errors="replace"
            )
            raise RuntimeError(detail.strip()[:500])
