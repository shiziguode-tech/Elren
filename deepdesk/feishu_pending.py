from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from deepdesk.plugins.base import run_owned_async

logger = logging.getLogger(__name__)


class PendingFeishuAttachments:
    """Briefly join Feishu attachment events with the user's next text event."""

    def __init__(self, delay_seconds: float = 15.0) -> None:
        self.delay_seconds = max(0.01, float(delay_seconds))
        self._pending: dict[str, dict[str, Any]] = {}
        self._timers: set[asyncio.Task] = set()
        self._closed = False

    async def stage(
        self,
        key: str,
        attachments: list[str],
        metadata: dict[str, Any],
        on_expire: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        if self._closed:
            raise RuntimeError("Pending attachment queue is closed")
        previous = self._pending.get(key)
        first = previous is None
        if previous:
            previous["timer"].cancel()
            attachments = [*previous["attachments"], *attachments]
            metadata = {
                **previous["metadata"],
                **metadata,
                "image_count": int(previous["metadata"].get("image_count") or 0)
                + int(metadata.get("image_count") or 0),
            }
        entry: dict[str, Any] = {
            "attachments": list(dict.fromkeys(attachments)),
            "metadata": metadata,
        }
        self._pending[key] = entry
        entry["timer"] = asyncio.create_task(self._expire(key, entry, on_expire))
        self._timers.add(entry["timer"])
        entry["timer"].add_done_callback(self._timers.discard)
        return first

    async def _expire(
        self,
        key: str,
        entry: dict[str, Any],
        on_expire: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            await asyncio.sleep(self.delay_seconds)
            if self._pending.get(key) is not entry:
                return
            self._pending.pop(key, None)
            await run_owned_async(on_expire, entry)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Timer tasks are intentionally fire-and-forget.  Observe callback
            # failures here so attachment expiry cannot produce an unhandled
            # task exception with no channel diagnostic.
            logger.error("Pending channel attachment flush failed (%s)", type(exc).__name__)

    def take(self, key: str) -> dict[str, Any] | None:
        entry = self._pending.pop(key, None)
        if entry:
            entry["timer"].cancel()
        return entry

    async def close(self) -> None:
        self._closed = True
        await run_owned_async(self._drain_timers)

    async def _drain_timers(self) -> None:
        # Expired entries have left _pending, but their callbacks may still be
        # committing a task. Retain every timer until that work is observed.
        timers = list(self._timers)
        self._pending.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
