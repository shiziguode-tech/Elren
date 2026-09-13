from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from deepdesk.subprocess_env import credential_safe_environment


class WindowsOCR:
    """Quota-free OCR backed by the Windows.Media.Ocr language packs."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.script = Path(__file__).with_name("windows_ocr.ps1")
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    async def status(self, probe: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if self._status_cache and (not probe or now - self._status_cache[0] < 60):
            return self._status_cache[1]
        if not probe:
            # Routine UI polling must stay cheap. Presence of the bundled
            # script on Windows is sufficient for the initial capability card;
            # the explicit runtime probe performs the slower language-pack test.
            return {
                "ready": os.name == "nt" and self.script.is_file(),
                "source": "Windows.Media.Ocr",
                "offline": True,
                "available_languages": [],
                "probed": False,
                "error": "",
            }
        try:
            payload = await asyncio.to_thread(self._run, ["-StatusOnly"])
            result = {
                "ready": bool(payload.get("ok")),
                "source": "Windows.Media.Ocr",
                "offline": True,
                "available_languages": payload.get("available_languages", []),
                "probed": True,
                "error": payload.get("error", ""),
            }
        except Exception as exc:
            result = {
                "ready": False,
                "source": "Windows.Media.Ocr",
                "offline": True,
                "available_languages": [],
                "probed": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self._status_cache = (now, result)
        return result

    async def recognize(
        self, image_path: Path, language_tags: list[str] | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._recognize_sync,
            image_path,
            language_tags or [],
        )

    def _recognize_sync(
        self, image_path: Path, language_tags: list[str]
    ) -> dict[str, Any]:
        arguments = ["-ImagePath", str(image_path)]
        if language_tags:
            arguments.extend(["-LanguageTags", *language_tags])
        payload = self._run(arguments)
        for candidate in payload.get("candidates", []):
            candidate["text"] = str(candidate.get("text") or "")[:20_000]
            candidate["lines"] = list(candidate.get("lines") or [])[:300]
            for line in candidate["lines"]:
                line["words"] = list(line.get("words") or [])[:100]
        payload["candidates"] = list(payload.get("candidates") or [])[:8]
        payload["text"] = str(payload.get("text") or "")[:20_000]
        return payload

    def _run(self, arguments: list[str]) -> dict[str, Any]:
        if os.name != "nt":
            raise RuntimeError("Windows.Media.Ocr is only available on Windows")
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            *arguments,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        run = subprocess.run(
            command,
            capture_output=True,
            timeout=self.timeout,
            creationflags=creationflags,
            check=False,
            env=credential_safe_environment(),
        )
        stdout = run.stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        stderr = run.stderr.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
        if run.returncode != 0:
            raise RuntimeError(stderr or stdout or f"Windows OCR exited {run.returncode}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Windows OCR returned invalid JSON: {stdout[-500:]}") from exc
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "Windows OCR failed"))
        return payload
