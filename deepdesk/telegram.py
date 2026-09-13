from __future__ import annotations

import asyncio
import mimetypes
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from deepdesk.plugins.base import run_owned_async

TELEGRAM_LOCAL_UPDATE_MAX_ATTEMPTS = 3


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _TelegramUpdateDeferred(RuntimeError):
    """Keep a Telegram update pending after a transient local negative ack."""


class TelegramBridge:
    """Telegram Bot API channel with long polling and native media transfer."""

    def __init__(
        self,
        bot_token: str = "",
        default_chat_id: str = "",
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self.bot_token = bot_token.strip()
        self.default_chat_id = default_chat_id.strip()
        self.base_url = base_url.rstrip("/")
        self.last_error = ""
        self.bot_username = ""
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._offset = 0
        self._callback: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
        self._client: httpx.AsyncClient | None = None
        self._poll_lifecycle_lock = asyncio.Lock()
        self._negative_ack_attempts: dict[int, int] = {}
        self.failed_update_count = 0
        self.last_update_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def set_credentials(
        self, bot_token: str, default_chat_id: str | None = None
    ) -> None:
        changed = bot_token.strip() != self.bot_token
        self.bot_token = bot_token.strip()
        # An empty value is meaningful here: it is how the settings UI clears
        # a previously configured default recipient. Keeping the old value
        # could otherwise route a later proactive message to the wrong chat.
        # ``None`` preserves compatibility for callers only rotating the token.
        if default_chat_id is not None:
            self.default_chat_id = default_chat_id.strip()
        if changed:
            self._offset = 0
            self.bot_username = ""
            self._negative_ack_attempts.clear()

    def remember_default_chat_id(self, chat_id: str) -> None:
        if chat_id.strip():
            self.default_chat_id = chat_id.strip()

    def _method_url(self, method: str) -> str:
        if not self.bot_token:
            raise RuntimeError("Telegram bot token is not configured")
        return f"{self.base_url}/bot{self.bot_token}/{method}"

    async def _api(
        self,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 40,
    ) -> Any:
        client = self._client
        temporary_client = client is None or client.is_closed
        if temporary_client:
            client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            )
        try:
            response = await client.post(
                self._method_url(method),
                data=data or {},
                files=files,
                timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            )
            payload = response.json()
            if response.status_code >= 400 or not payload.get("ok"):
                description = str(payload.get("description") or f"HTTP {response.status_code}")
                raise RuntimeError(f"Telegram {method} failed: {description}")
            self.last_error = ""
            return payload.get("result")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Telegram {method} failed: {type(exc).__name__}") from exc
        finally:
            if temporary_client:
                await client.aclose()

    async def status(self, *, probe: bool = False) -> dict[str, Any]:
        ready = bool(self.configured and self._poll_task and not self._poll_task.done())
        if probe and self.configured:
            try:
                me = await self._api("getMe")
                self.bot_username = str((me or {}).get("username") or "")
                ready = True
            except Exception as exc:
                self.last_error = str(exc)
                ready = False
        return {
            "configured": self.configured,
            "ready": ready,
            "polling": bool(self._poll_task and not self._poll_task.done()),
            "bot_username": self.bot_username,
            "default_chat_id_configured": bool(self.default_chat_id),
            "last_error": self.last_error,
            "failed_update_count": self.failed_update_count,
            "last_update_error": self.last_update_error,
        }

    async def start_polling(
        self, callback: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> bool:
        async with self._poll_lifecycle_lock:
            return await self._start_polling_unlocked(callback)

    async def _start_polling_unlocked(
        self, callback: Callable[[dict[str, Any]], Awaitable[Any]]
    ) -> bool:
        self._callback = callback
        if not self.configured:
            return False
        if self._poll_task and not self._poll_task.done():
            return True
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(12, connect=8),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        self._stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())
        return True

    async def restart_polling(self) -> bool:
        # Settings updates, startup recovery and health repair can request a
        # restart at the same time.  Keep stop/start in one critical section so
        # two callers cannot each create a live long-poll worker.
        async with self._poll_lifecycle_lock:
            callback = self._callback
            await self._stop_polling_unlocked()
            return bool(callback and await self._start_polling_unlocked(callback))

    async def stop_polling(self) -> None:
        async with self._poll_lifecycle_lock:
            await self._stop_polling_unlocked()

    async def _stop_polling_unlocked(self) -> None:
        self._stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set() and self.configured:
            try:
                updates = await self._api(
                    "getUpdates",
                    data={
                        "offset": str(self._offset),
                        # Some consumer proxies buffer Telegram's long-poll
                        # response until the requested timeout. A three-second
                        # window bounds message pickup latency while the shared
                        # keep-alive client avoids repeated TLS handshakes.
                        "timeout": "3",
                        "allowed_updates": '["message"]',
                    },
                    timeout=10,
                )
                self.last_error = ""
                for update in updates or []:
                    if self._stop.is_set():
                        break
                    await run_owned_async(self._accept_update, update)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _accept_update(self, update: dict[str, Any]) -> None:
        """Own callback processing and local acknowledgement as one operation."""
        update_id = int(update.get("update_id") or 0)
        next_offset = update_id + 1
        event = self.parse_update(update)
        if event and self._callback:
            attempt = self._negative_ack_attempts.get(update_id, 0) + 1
            # Internal delivery provenance lets the application avoid
            # repeating user notifications during a bounded retry.
            event["_delivery_attempt"] = attempt
            event["_delivery_max_attempts"] = (
                TELEGRAM_LOCAL_UPDATE_MAX_ATTEMPTS
            )
            try:
                callback_result = await self._callback(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                callback_result = {
                    "ok": False,
                    "ack_update": False,
                    "reason": f"callback_{type(exc).__name__}",
                }
            if isinstance(callback_result, dict):
                acknowledged = callback_result.get("ack_update")
                if acknowledged is False or (
                    callback_result.get("ok") is False
                    and acknowledged is not True
                ):
                    reason = str(
                        callback_result.get("reason")
                        or "transient_local_negative_ack"
                    )[:300]
                    if attempt < TELEGRAM_LOCAL_UPDATE_MAX_ATTEMPTS:
                        self._negative_ack_attempts[update_id] = attempt
                        raise _TelegramUpdateDeferred(reason)
                    # A permanently failing update must not create an
                    # infinite notification loop or block every later
                    # message in the Telegram queue. Preserve a visible
                    # diagnostic, then consume it after the bounded
                    # retry budget is exhausted.
                    self._negative_ack_attempts.pop(update_id, None)
                    self.failed_update_count += 1
                    self.last_update_error = (
                        f"update {update_id} failed after {attempt} "
                        f"local deliveries: {reason}"
                    )
                else:
                    self._negative_ack_attempts.pop(update_id, None)
            else:
                self._negative_ack_attempts.pop(update_id, None)
        # Telegram discards updates older than ``offset``. Advancing
        # before the callback made a transient local exception lose
        # the user's request permanently. Commit only after the
        # normalized event has been handled successfully.
        self._offset = max(self._offset, next_offset)

    @staticmethod
    def parse_update(update: dict[str, Any]) -> dict[str, Any] | None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        # Anonymous group administrators may be represented by ``sender_chat``
        # instead of ``from``. Preserve either identity for attachment batching.
        sender = message.get("from") or message.get("sender_chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return None
        prompt = str(message.get("text") or message.get("caption") or "").strip()
        resources: list[dict[str, Any]] = []
        photos = [item for item in (message.get("photo") or []) if isinstance(item, dict)]
        if photos:
            # Telegram orders photo sizes from small to large, but ``file_size``
            # is optional. Prefer actual image area, then byte size, while the
            # index preserves Telegram's ordering as a final fallback. This
            # prevents OCR/vision tasks from receiving the thumbnail.
            photo = max(
                enumerate(photos),
                key=lambda pair: (
                    _safe_int(pair[1].get("width")) * _safe_int(pair[1].get("height")),
                    _safe_int(pair[1].get("file_size")),
                    pair[0],
                ),
            )[1]
            resources.append(
                {
                    "type": "image",
                    "file_id": str(photo.get("file_id") or ""),
                    "file_name": f"telegram-photo-{message.get('message_id', 'image')}.jpg",
                    "file_size": _safe_int(photo.get("file_size")),
                }
            )
        for field, kind, fallback in (
            ("document", "file", "telegram-file"),
            ("audio", "audio", "telegram-audio.mp3"),
            ("voice", "audio", "telegram-voice.ogg"),
            ("video", "video", "telegram-video.mp4"),
            ("video_note", "video", "telegram-video-note.mp4"),
            ("animation", "video", "telegram-animation.mp4"),
        ):
            value = message.get(field)
            if not isinstance(value, dict):
                continue
            resources.append(
                {
                    "type": kind,
                    "telegram_type": field,
                    "file_id": str(value.get("file_id") or ""),
                    "file_name": str(value.get("file_name") or fallback),
                    "file_size": int(value.get("file_size") or 0),
                }
            )
        return {
            "update_id": str(update.get("update_id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "chat_id": chat_id,
            "chat_type": str(chat.get("type") or ""),
            "sender_id": str(sender.get("id") or ""),
            "message_thread_id": str(message.get("message_thread_id") or ""),
            "prompt": prompt,
            "resources": resources,
        }

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        file_name: str,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> Path:
        if not file_id:
            raise ValueError("Telegram file_id is missing")
        file_info = await self._api("getFile", data={"file_id": file_id})
        file_path = str((file_info or {}).get("file_path") or "")
        file_size = int((file_info or {}).get("file_size") or 0)
        if not file_path or file_size > max_bytes:
            raise ValueError("Telegram attachment is unavailable or exceeds the 20 MB download limit")
        safe_name = Path(file_name.replace("\x00", "")).name or Path(file_path).name
        safe_name = re.sub(r"[^\w.()\- ]+", "_", safe_name)[:180] or "telegram-file"
        destination.mkdir(parents=True, exist_ok=True)
        target = (destination / safe_name).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError as exc:
            raise PermissionError("Telegram attachment path escapes the destination") from exc
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120, connect=20), follow_redirects=False
            ) as client, client.stream(
                "GET", f"{self.base_url}/file/bot{self.bot_token}/{file_path}"
            ) as response:
                response.raise_for_status()
                total = 0
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Telegram attachment exceeds the local download limit")
                        handle.write(chunk)
            if not target.stat().st_size:
                raise ValueError("Telegram returned an empty attachment")
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    async def send_text(self, chat_id: str, text: str) -> dict[str, Any]:
        messages: list[Any] = []
        value = str(text or "") or " "
        for start in range(0, len(value), 4000):
            messages.append(
                await self._api(
                    "sendMessage",
                    data={"chat_id": chat_id, "text": value[start : start + 4000]},
                )
            )
        last = messages[-1] if messages else {}
        return {"message_id": str((last or {}).get("message_id") or ""), "parts": len(messages)}

    async def send_file(self, chat_id: str, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ValueError("Telegram Bot API file upload limit is 50 MB")
        extension = path.suffix.lower()
        if extension in {".jpg", ".jpeg", ".png", ".webp"} and path.stat().st_size <= 10 * 1024 * 1024:
            method, field = "sendPhoto", "photo"
        elif extension in {".mp3", ".m4a"}:
            method, field = "sendAudio", "audio"
        elif extension in {".mp4", ".mov", ".webm", ".mkv"}:
            method, field = "sendVideo", "video"
        else:
            method, field = "sendDocument", "document"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            result = await self._api(
                method,
                data={"chat_id": chat_id},
                files={field: (path.name, handle, content_type)},
                timeout=180,
            )
        return {"message_id": str((result or {}).get("message_id") or ""), "method": method}
