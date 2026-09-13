from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from deepdesk.models import Risk

logger = logging.getLogger(__name__)


async def wait_owned_result(worker: asyncio.Future) -> Any:
    """Wait without cancelling a separately owned worker or logging its error.

    Unlike shield's cancelled proxy on bundled Python 3.14, asyncio.wait does
    not install a later exception-logging callback. The owner remains responsible
    for joining/observing the worker on timeout or cancellation. Never pass an
    unowned coroutine here: callers must retain its Task/Future reference.
    """
    await asyncio.wait({worker})
    return worker.result()


async def finish_owned_work(worker: asyncio.Task) -> Any:
    """Join already-owned work despite repeated cancellation of its owner."""
    while not worker.done():
        try:
            await wait_owned_result(worker)
        except asyncio.CancelledError:
            continue
    return worker.result()


async def run_owned_thread(function: Callable, *args: Any, on_cancel: Callable | None = None) -> Any:
    """Keep ownership until synchronous work has reached its safe boundary.

    Cancelling to_thread alone only abandons its result, not its OS thread. A
    cancellable worker may be signalled; otherwise (e.g. an atomic filesystem
    commit) we join it before propagating cancellation. Repeated Stop/shutdown
    cancellation must not detach work that can still change task outputs.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await wait_owned_result(worker)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        try:
            await finish_owned_work(worker)
        except Exception as exc:
            # Cancellation wins, but retrieving this result also observes any
            # synchronous failure before relinquishing ownership.
            logger.warning("Owned synchronous work failed while stopping (%s)", type(exc).__name__)
        raise


async def run_owned_async(function: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    """Finish an admitted async transaction before propagating owner cancellation.

    Use only for bounded acceptance/commit operations that cannot be abandoned
    after creating work. This does not shield the long-running agent itself.
    Pass a factory, not an already-created coroutine, so cancellation before
    entry cannot leak an unawaited coroutine.
    """
    async def invoke():
        return await function(*args)

    worker = asyncio.create_task(invoke())
    try:
        return await wait_owned_result(worker)
    except asyncio.CancelledError:
        try:
            await finish_owned_work(worker)
        except Exception as exc:
            logger.warning("Owned async acceptance failed while stopping (%s)", type(exc).__name__)
        raise


@dataclass(slots=True)
class ToolContext:
    task_id: str
    workspace: str
    filesystem_scope: str | None = None
    egress_country: str = "UNKNOWN"
    user_prompt: str = ""
    source: str = "web"
    voice_request: bool = False
    approval_policy: str = "autonomous"
    agent_profile: str = ""
    application_workspace: str = ""
    authorized_read_paths: tuple[str, ...] = ()


class ToolPlugin(ABC):
    """One DeepSeek function tool. Third-party plugins subclass this class."""

    name: str
    description: str
    parameters: dict[str, Any]

    def api_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def risk(self, arguments: dict[str, Any]) -> Risk:
        raise NotImplementedError

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"执行 {self.name}: {arguments}"

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        raise NotImplementedError

    async def cleanup(self, context: ToolContext) -> None:
        """Release per-task resources. Built-ins may override this; default is a no-op."""
        return
