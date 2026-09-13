from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from deepdesk.feishu import FeishuBridge


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required worker setting: {name}")
    return value


def run() -> None:
    # Importing the official SDK is intentionally isolated in this worker: its
    # blocking WebSocket event loop cannot interfere with FastAPI's event loop.
    import lark_oapi as lark

    app_id = _required_env("ELREN_FEISHU_APP_ID")
    app_secret = _required_env("ELREN_FEISHU_APP_SECRET")
    callback_url = _required_env("ELREN_FEISHU_CALLBACK_URL")
    listener_token = _required_env("ELREN_FEISHU_LISTENER_TOKEN")
    status_path = Path(_required_env("ELREN_FEISHU_STATUS_PATH"))
    parser = FeishuBridge(True, "https://open.feishu.cn", app_id, app_secret)
    state_lock = threading.Lock()
    state = {"value": "starting", "detail": "", "last_event_at": ""}

    def report(next_state: str | None = None, detail: str = "") -> None:
        with state_lock:
            if next_state:
                state["value"] = next_state
                state["detail"] = detail[:500]
            payload = {
                "pid": os.getpid(),
                "parent_pid": os.getppid(),
                "state": state["value"],
                "detail": state["detail"],
                "heartbeat_at": time.time(),
                "updated_at": datetime.now(UTC).isoformat(),
                "last_event_at": state["last_event_at"],
            }
            try:
                status_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = status_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, status_path)
            except OSError:
                pass

    def heartbeat() -> None:
        while True:
            report()
            time.sleep(5)

    threading.Thread(target=heartbeat, name="feishu-status-heartbeat", daemon=True).start()

    def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        payload = json.loads(lark.JSON.marshal(data))
        event = parser.parse_event(payload, mark_seen=False)
        if not event:
            return
        try:
            response = httpx.post(
                callback_url,
                headers={"X-Elren-Feishu-Token": listener_token},
                json=event,
                # Image/file events download their Feishu resource before the
                # local task starts. Keep this loopback request alive long
                # enough for a large attachment or a transient API retry.
                timeout=90,
                # The callback is always loopback. A desktop HTTP proxy must not
                # intercept this internal hop and turn a valid event into a 502.
                trust_env=False,
            )
            response.raise_for_status()
            with state_lock:
                state["last_event_at"] = datetime.now(UTC).isoformat()
            report("event_received")
            report("connected")
        except Exception as exc:
            report("callback_error", f"{type(exc).__name__}: {exc}")
            raise

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    while True:
        client = lark.ws.Client(
            app_id,
            app_secret,
            log_level=lark.LogLevel.WARNING,
            event_handler=event_handler,
        )
        # The official SDK defaults to a 120-second retry interval. A desktop
        # agent should recover promptly enough that a remote instruction is not
        # silently missed during a transient network interruption.
        client._reconnect_nonce = 1
        client._reconnect_interval = 5
        client._ping_interval = 30
        original_connect = client._connect

        async def monitored_connect(_connect=original_connect):
            report("connecting")
            try:
                await _connect()
            except Exception as exc:
                report("reconnecting", f"{type(exc).__name__}: {exc}")
                raise
            report("connected")

        client._connect = monitored_connect
        client.on_reconnecting = lambda: report("reconnecting")
        client.on_reconnected = lambda: report("connected")
        try:
            client.start()
        except KeyboardInterrupt:
            report("stopped")
            return
        except Exception as exc:
            report("error", f"{type(exc).__name__}: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    run()
