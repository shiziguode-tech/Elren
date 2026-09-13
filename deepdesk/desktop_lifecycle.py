"""Local, per-launch shutdown handshake; never expose a network stop endpoint."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path


def service_instance(arguments: list[str]) -> str | None:
    for index, argument in enumerate(arguments):
        value = None
        if argument == "--elren-service-id" and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif argument.startswith("--elren-service-id="):
            value = argument.split("=", 1)[1]
        if value and re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    return None


async def serve_desktop(server, data_dir: Path, instance: str | None) -> None:
    """Let Uvicorn run its normal lifespan shutdown before acknowledging exit."""
    if instance is not None and not re.fullmatch(r"[0-9a-f]{32}", instance):
        raise ValueError("Invalid desktop service instance")
    request = data_dir / f"service-exit-request-{instance}" if instance else None
    stopped = data_dir / f"service-exit-complete-{instance}" if instance else None
    failed = data_dir / f"service-exit-failed-{instance}" if instance else None
    requested = False

    # Uvicorn otherwise waits indefinitely for an open streaming response before
    # it even calls the lifespan cleanup that stops application-owned workers.
    config = getattr(server, "config", None)
    if config is not None and getattr(config, "timeout_graceful_shutdown", None) is None:
        config.timeout_graceful_shutdown = 5.0

    def record_failure(reason: str) -> None:
        if failed is None:
            return
        if stopped is not None:
            stopped.unlink(missing_ok=True)
        failed.write_text(reason, encoding="utf-8")

    async def watch() -> None:
        nonlocal requested
        while True:
            if request is not None and request.is_file():
                # This distinct marker is not the replacement diagnostic marker:
                # upgrades and other instances must never trigger this watcher.
                requested = True
                server.should_exit = True
                return
            await asyncio.sleep(0.2)

    watcher = asyncio.create_task(watch()) if instance else None
    try:
        await server.serve()
        # Real Uvicorn consumes ASGI lifespan exceptions instead of raising them
        # from serve(). Its explicit failure flags are part of the result.
        lifespan = getattr(server, "lifespan", None)
        if any(bool(getattr(lifespan, name, False)) for name in (
            "startup_failed", "shutdown_failed", "error_occurred",
        )):
            raise RuntimeError("Desktop application lifecycle cleanup failed")
        if requested and stopped is not None:
            if failed is not None:
                failed.unlink(missing_ok=True)
            stopped.write_text("stopped", encoding="utf-8")
    except BaseException as exc:
        try:
            record_failure("shutdown_cancelled" if isinstance(exc, asyncio.CancelledError) else "shutdown_failed")
        except OSError:
            # Never expose a local path through the fallback exception. The
            # bootstrap retains the current marker for this nonzero exit.
            raise RuntimeError("Desktop shutdown result could not be recorded") from None
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
