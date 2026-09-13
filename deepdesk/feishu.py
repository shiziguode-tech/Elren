from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import psutil

from deepdesk.secret_storage import LocalSecretVault, SecretStorageError
from deepdesk.subprocess_env import credential_safe_environment

logger = logging.getLogger(__name__)

_EVENT_RESERVATION_TTL_SECONDS = 2 * 60 * 60
_MAX_EVENT_RESERVATIONS = 2_048
_MAX_COMMITTED_EVENT_IDS = 4_096


def _normalized_event_id(value: Any) -> str:
    event_id = str(value or "").strip()
    if len(event_id) <= 512:
        return event_id
    return "sha256:" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()


class FeishuBridge:
    """Small Feishu bot bridge for optional remote task intake and replies.

    It uses the official tenant token and IM REST APIs, while incoming v2
    ``im.message.receive_v1`` events are accepted through the local callback
    route. The app can therefore receive remote prompts and send task results
    without making Feishu a mandatory dependency.
    """

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        app_id: str,
        app_secret: str,
        verification_token: str = "",
        encrypt_key: str = "",
        default_receive_id: str = "",
        workspace: Path | None = None,
    ) -> None:
        self.enabled = enabled
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.verification_token = verification_token.strip()
        self.encrypt_key = encrypt_key.strip()
        self.default_receive_id = default_receive_id.strip()
        # Direct construction (tests, worker entry points, third-party use)
        # must remain portable even when the process was launched from an
        # arbitrary current directory.  The main app still passes its explicit
        # configured workspace.
        package_workspace = Path(__file__).resolve().parents[1]
        self.workspace = (workspace or package_workspace).expanduser().resolve()
        self._event_dedupe_path = self.workspace / "data" / "feishu-event-dedup.json"
        self._event_lock = threading.RLock()
        self._event_reservations: dict[str, tuple[str, float]] = {}
        self._seen_events = self._load_committed_event_ids()
        self._tenant_token = ""
        self._tenant_token_expires_at = 0.0
        self._listener: asyncio.subprocess.Process | None = None
        self._listener_lock = asyncio.Lock()
        self._listener_token = ""
        self._listener_log = None
        self._listener_callback_url = ""
        self._listener_status_path = self.workspace / "data" / "feishu-worker-status.json"
        self._listener_token_path = self._listener_status_path.with_name("feishu-listener-token")
        self.last_error = ""

    def set_credentials(
        self,
        *,
        app_id: str,
        app_secret: str,
        verification_token: str = "",
        encrypt_key: str = "",
        default_receive_id: str = "",
    ) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.verification_token = verification_token.strip()
        self.encrypt_key = encrypt_key.strip()
        self.default_receive_id = default_receive_id.strip()
        self._tenant_token = ""
        self._tenant_token_expires_at = 0.0

    def remember_default_receive_id(self, receive_id: str) -> None:
        self.default_receive_id = receive_id.strip()

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.app_id and self.app_secret)

    async def status(self, probe: bool = False) -> dict[str, Any]:
        listener_running = bool(self._listener and self._listener.returncode is None)
        worker_status = self._read_worker_status()
        worker_state = str(worker_status.get("state") or "")
        heartbeat_at = float(worker_status.get("heartbeat_at") or 0)
        heartbeat_age = max(0.0, time.time() - heartbeat_at) if heartbeat_at else None
        worker_healthy = bool(
            listener_running
            and (
                not worker_status
                or (
                    worker_state in {"connected", "event_received"}
                    and heartbeat_age is not None
                    and heartbeat_age <= 20
                )
            )
        )
        worker_error = ""
        if listener_running and worker_status and not worker_healthy:
            detail = str(worker_status.get("detail") or "").strip()
            worker_error = f"飞书长连接状态异常：{worker_state or 'unknown'}"
            if detail:
                worker_error += f"（{detail}）"
        result = {
            "enabled": self.enabled,
            "configured": self.configured,
            "app_id_configured": bool(self.app_id),
            "app_secret_configured": bool(self.app_secret),
            "verification_token_configured": bool(self.verification_token),
            "encrypt_key_configured": bool(self.encrypt_key),
            "default_receive_id_configured": bool(self.default_receive_id),
            "callback_path": "/api/feishu/events",
            "connection_mode": "long_connection",
            "long_connection_running": listener_running,
            "long_connection_healthy": worker_healthy,
            "worker_state": worker_state or ("starting" if listener_running else "stopped"),
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "last_event_at": worker_status.get("last_event_at", ""),
            "send_api": f"{self.base_url}/open-apis/im/v1/messages",
            "ready": False,
            "last_error": worker_error or self.last_error,
        }
        if not self.configured:
            return result
        if probe:
            try:
                await self._get_tenant_token()
                result["ready"] = worker_healthy
                if not worker_error:
                    result["last_error"] = ""
                    self.last_error = ""
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                result["last_error"] = self.last_error
        else:
            result["ready"] = worker_healthy
        return result

    def _read_worker_status(self) -> dict[str, Any]:
        try:
            value = json.loads(self._listener_status_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            listener_pids = {
                int(value.get("pid") or 0),
                int(value.get("parent_pid") or 0),
            }
            listener_pids.discard(0)
            listener_pid = int(getattr(self._listener, "pid", 0) or 0)
            if listener_pid and listener_pids and listener_pid not in listener_pids:
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    def _load_committed_event_ids(self) -> deque[str]:
        committed: deque[str] = deque(maxlen=_MAX_COMMITTED_EVENT_IDS)
        if not self._event_dedupe_path.is_file():
            return committed
        try:
            payload = json.loads(self._event_dedupe_path.read_text(encoding="utf-8"))
            values = payload.get("committed_event_ids") if isinstance(payload, dict) else []
            if not isinstance(values, list):
                raise ValueError("committed_event_ids must be an array")
            seen: set[str] = set()
            for raw in list(values or [])[-_MAX_COMMITTED_EVENT_IDS:]:
                event_id = _normalized_event_id(raw)
                if event_id and event_id not in seen:
                    committed.append(event_id)
                    seen.add(event_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable Feishu event dedupe state: %s", exc)
        return committed

    def _persist_committed_event_ids_locked(self) -> None:
        payload = json.dumps(
            {
                "schema": 1,
                "committed_event_ids": list(self._seen_events),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._event_dedupe_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._event_dedupe_path.with_name(
            f".{self._event_dedupe_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._event_dedupe_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _prune_event_reservations_locked(self, now: float) -> None:
        expired = [
            event_id
            for event_id, (_token, expires_at) in self._event_reservations.items()
            if expires_at <= now
        ]
        for event_id in expired:
            self._event_reservations.pop(event_id, None)

    def reserve_event_id(
        self,
        event_id: str,
        *,
        ttl_seconds: float = _EVENT_RESERVATION_TTL_SECONDS,
    ) -> str | None:
        """Reserve one event without consuming its retry until work succeeds.

        ``None`` means another request already owns or committed the event. An
        empty string is an untracked lease for legacy payloads lacking an ID.
        """

        event_id = _normalized_event_id(event_id)
        if not event_id:
            return ""
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            ttl = _EVENT_RESERVATION_TTL_SECONDS
        if not math.isfinite(ttl):
            ttl = _EVENT_RESERVATION_TTL_SECONDS
        ttl = max(1.0, min(ttl, _EVENT_RESERVATION_TTL_SECONDS))
        now = time.monotonic()
        with self._event_lock:
            self._prune_event_reservations_locked(now)
            if event_id in self._seen_events or event_id in self._event_reservations:
                return None
            if len(self._event_reservations) >= _MAX_EVENT_RESERVATIONS:
                raise RuntimeError(
                    "Feishu event reservation capacity is temporarily exhausted"
                )
            token = secrets.token_urlsafe(24)
            self._event_reservations[event_id] = (token, now + ttl)
            return token

    def commit_event_id(self, event_id: str, reservation_token: str) -> bool:
        """Permanently consume an owned event after local task acceptance."""

        return self.commit_event_ids([(event_id, reservation_token)])

    def commit_event_ids(self, reservations: list[tuple[str, str]]) -> bool:
        """Atomically commit all reservations represented by one created task."""

        normalized = [
            (_normalized_event_id(event_id), str(token or ""))
            for event_id, token in reservations
        ]
        now = time.monotonic()
        with self._event_lock:
            self._prune_event_reservations_locked(now)
            for event_id, reservation_token in normalized:
                if not event_id:
                    if reservation_token != "":
                        return False
                    continue
                current = self._event_reservations.get(event_id)
                if current is None:
                    if event_id not in self._seen_events:
                        return False
                    continue
                token, expires_at = current
                if expires_at <= now or not secrets.compare_digest(
                    token, reservation_token
                ):
                    return False

            changed = False
            for event_id, _reservation_token in normalized:
                if not event_id:
                    continue
                self._event_reservations.pop(event_id, None)
                if event_id not in self._seen_events:
                    self._seen_events.append(event_id)
                    changed = True
            if changed:
                self._persist_committed_event_ids_locked()
            return True

    def rollback_event_id(self, event_id: str, reservation_token: str) -> bool:
        """Release only the caller's reservation so Feishu may safely retry."""

        return self.rollback_event_ids([(event_id, reservation_token)])

    def rollback_event_ids(self, reservations: list[tuple[str, str]]) -> bool:
        """Release every still-owned reservation after a shared failure."""

        normalized = [
            (_normalized_event_id(event_id), str(token or ""))
            for event_id, token in reservations
        ]
        released_all = True
        with self._event_lock:
            for event_id, reservation_token in normalized:
                if not event_id:
                    released_all = released_all and reservation_token == ""
                    continue
                current = self._event_reservations.get(event_id)
                if current is None or not secrets.compare_digest(
                    current[0], reservation_token
                ):
                    released_all = False
                    continue
                self._event_reservations.pop(event_id, None)
        return released_all

    def accept_event_id(self, event_id: str) -> bool:
        """Backward-compatible immediate consume; ingress uses two-phase APIs."""

        reservation = self.reserve_event_id(event_id)
        return bool(
            reservation is not None
            and self.commit_event_id(event_id, reservation)
        )

    @property
    def listener_token(self) -> str:
        return self._listener_token

    def validate_listener_token(self, candidate: str) -> bool:
        return bool(
            self._listener_token
            and candidate
            and secrets.compare_digest(self._listener_token, candidate)
        )

    def _persistent_listener_token(self) -> str:
        vault_path = self._listener_token_path.with_name("feishu-listener-token.vault")
        vault = LocalSecretVault(vault_path)
        if vault_path.is_file():
            existing = str(vault.read().values.get("listener_token") or "").strip()
            if len(existing) >= 32:
                return existing
        legacy = ""
        try:
            legacy = self._listener_token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        token = legacy if len(legacy) >= 32 else secrets.token_urlsafe(32)
        vault.write_verified({"listener_token": token})
        # Remove the legacy plaintext only after authenticated write/read.
        if legacy:
            self._listener_token_path.unlink(missing_ok=True)
        return token

    def _rotate_listener_log(self, log_path: Path) -> None:
        """Bound the exact workspace-owned long-connection log before append.

        The SDK worker can retry forever, so an append-only stdout/stderr file
        otherwise grows for the lifetime of the installation.  Keep the most
        recent MiB in a single backup and start a fresh active log.  Rotation
        is diagnostic-only and must never stop the message channel starting.
        """

        expected = self.workspace / "data" / "feishu-long-connection.log"
        if log_path != expected or log_path.is_symlink():
            return
        try:
            if log_path.stat().st_size <= 5 * 1024 * 1024:
                return
            with log_path.open("rb") as source:
                size = source.seek(0, os.SEEK_END)
                source.seek(max(0, size - 1024 * 1024))
                tail = source.read()
            backup = log_path.with_name(f"{log_path.name}.1")
            temporary = backup.with_suffix(f"{backup.suffix}.tmp")
            temporary.write_bytes(tail)
            os.replace(temporary, backup)
            log_path.write_bytes(b"")
        except OSError:
            # Logging must remain best-effort: credential validation and the
            # remote channel are more important than retaining old diagnostics.
            pass

    async def start_long_connection(self, callback_url: str) -> bool:
        """Start the official Feishu WebSocket listener as an isolated worker."""
        async with self._listener_lock:
            return await self._start_long_connection_unlocked(callback_url)

    async def _start_long_connection_unlocked(self, callback_url: str) -> bool:
        await self._stop_long_connection_unlocked()
        self._listener_callback_url = callback_url
        if not self.configured:
            return False
        try:
            await self._get_tenant_token()
        except Exception as exc:
            self.last_error = f"飞书凭据验证失败：{type(exc).__name__}: {exc}"
            return False
        # A launcher crash or forced restart can orphan the SDK's blocking
        # worker.  The new service has no process handle for it, so stopping
        # only ``self._listener`` leaves several WebSocket consumers receiving
        # and duplicating the same remote event.  Reap only workers whose
        # credential-safe environment names this exact workspace status file.
        await asyncio.to_thread(self._terminate_stale_listeners)
        # Keep the loopback token stable across app restarts. If an old worker
        # survives a forced launcher restart for a few seconds, it can still
        # hand the event to the new service instead of losing it with a 403.
        try:
            self._listener_token = self._persistent_listener_token()
        except (SecretStorageError, OSError, UnicodeError) as exc:
            self.last_error = f"飞书监听器安全存储不可用：{type(exc).__name__}"
            return False
        try:
            self._listener_status_path.unlink(missing_ok=True)
        except OSError:
            pass
        env = credential_safe_environment(
            {
                "ELREN_FEISHU_APP_ID": self.app_id,
                "ELREN_FEISHU_APP_SECRET": self.app_secret,
                "ELREN_FEISHU_CALLBACK_URL": callback_url,
                "ELREN_FEISHU_LISTENER_TOKEN": self._listener_token,
                "ELREN_FEISHU_STATUS_PATH": str(self._listener_status_path),
            }
        )
        log_path = self.workspace / "data" / "feishu-long-connection.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_listener_log(log_path)
        self._listener_log = log_path.open("ab", buffering=0)
        self._listener = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "deepdesk.feishu_ws_worker",
            cwd=str(self.workspace),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self._listener_log,
            stderr=self._listener_log,
        )
        await asyncio.sleep(0.15)
        if self._listener.returncode is not None:
            self.last_error = "飞书长连接进程启动失败，请查看 data/feishu-long-connection.log"
            await self._stop_long_connection_unlocked()
            return False
        self.last_error = ""
        return True

    async def restart_long_connection(self) -> bool:
        if not self._listener_callback_url:
            return False
        return await self.start_long_connection(self._listener_callback_url)

    async def stop_long_connection(self) -> None:
        async with self._listener_lock:
            await self._stop_long_connection_unlocked()

    async def _stop_long_connection_unlocked(self) -> None:
        process = self._listener
        self._listener = None
        self._listener_token = ""
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._listener_log:
            self._listener_log.close()
            self._listener_log = None

    def _terminate_stale_listeners(self) -> int:
        """Best-effort cleanup for orphaned workers from this workspace only."""

        expected_status = os.path.normcase(str(self._listener_status_path.resolve()))
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "cmdline"]):
            if process.pid == os.getpid():
                continue
            try:
                command = " ".join(process.info.get("cmdline") or ())
                if "deepdesk.feishu_ws_worker" not in command:
                    continue
                environment = process.environ()
                status_path = str(environment.get("ELREN_FEISHU_STATUS_PATH") or "")
                if not status_path:
                    continue
                candidate = os.path.normcase(str(Path(status_path).resolve()))
                if candidate == expected_status:
                    matches.append(process)
            except (OSError, psutil.Error):
                continue
        for process in matches:
            try:
                process.terminate()
            except psutil.Error:
                pass
        _gone, alive = psutil.wait_procs(matches, timeout=2) if matches else ([], [])
        for process in alive:
            try:
                process.kill()
            except psutil.Error:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=2)
        return len(matches)

    async def _get_tenant_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expires_at - 60:
            return self._tenant_token
        if not self.configured:
            raise RuntimeError("飞书尚未配置 app_id 和 app_secret")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            response.raise_for_status()
            data = response.json()
        if data.get("code", 0) != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"飞书获取 tenant_access_token 失败：{data.get('msg', 'unknown error')}")
        self._tenant_token = str(data["tenant_access_token"])
        self._tenant_token_expires_at = time.time() + int(data.get("expire", 7200))
        return self._tenant_token

    async def send_text(
        self,
        receive_id: str,
        text: str,
        *,
        receive_id_type: str = "open_id",
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        receive_id = (receive_id or self.default_receive_id).strip()
        text = text.strip()
        if not receive_id or not text:
            raise ValueError("receive_id and text are required")
        return await self._send_message(
            receive_id,
            "text",
            {"text": text},
            receive_id_type=receive_id_type,
            max_attempts=max_attempts,
        )

    async def _send_message(
        self,
        receive_id: str,
        message_type: str,
        content: dict[str, Any],
        *,
        receive_id_type: str = "open_id",
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        query = urlencode({"receive_id_type": receive_id_type})
        payload = {
            "receive_id": receive_id,
            "msg_type": message_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        attempts = max(1, min(int(max_attempts), 8))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                token = await self._get_tenant_token()
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(
                        f"{self.base_url}/open-apis/im/v1/messages?{query}",
                        headers={"Authorization": f"Bearer {token}"},
                        json=payload,
                    )
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                code = data.get("code", response.status_code)
                if response.status_code < 400 and data.get("code", 0) == 0:
                    self.last_error = ""
                    return {
                        "ok": True,
                        "message_id": data.get("data", {}).get("message_id", ""),
                    }

                message = data.get("msg") or response.reason_phrase or "unknown error"
                last_error = RuntimeError(f"飞书发送消息失败（{code}）：{message}")
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 401:
                    self._tenant_token = ""
                    self._tenant_token_expires_at = 0.0
                    retryable = True
                if not retryable:
                    raise last_error
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = RuntimeError(f"飞书发送消息网络异常：{type(exc).__name__}: {exc}")
            except RuntimeError as exc:
                last_error = exc
                if "飞书发送消息失败" in str(exc):
                    raise

            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

        if last_error is None:
            last_error = RuntimeError("Feishu message delivery failed without an error response")
        self.last_error = str(last_error)
        raise last_error

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        destination_dir: Path,
        *,
        file_name: str = "",
        max_bytes: int = 25 * 1024 * 1024,
    ) -> Path:
        """Download an image, file, audio, or video attached to a Feishu message."""
        message_id = str(message_id or "").strip()
        file_key = str(file_key or "").strip()
        resource_type = str(resource_type or "").strip().lower()
        if resource_type in {"audio", "media", "video"}:
            resource_type = "file"
        if not message_id or not file_key or resource_type not in {"image", "file"}:
            raise ValueError("message_id, file_key and a valid resource_type are required")

        url = (
            f"{self.base_url}/open-apis/im/v1/messages/{quote(message_id, safe='')}"
            f"/resources/{quote(file_key, safe='')}"
        )
        response_status: int | None = None
        response_reason = ""
        response_headers: dict[str, str] = {}
        response_content = b""
        for attempt in range(1, 4):
            try:
                token = await self._get_tenant_token()
                async with httpx.AsyncClient(timeout=60) as client, client.stream(
                    "GET",
                    url,
                    params={"type": resource_type},
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    response_status = response.status_code
                    response_reason = response.reason_phrase
                    response_headers = dict(response.headers)
                    try:
                        declared_size = max(
                            int(response.headers.get("content-length") or 0), 0
                        )
                    except (TypeError, ValueError):
                        # A malformed optional size header must not bypass the
                        # real streamed byte limit or make a valid file fail.
                        declared_size = 0
                    if response_status < 400 and declared_size > max_bytes:
                        raise RuntimeError(
                            "Feishu attachment exceeds the local download limit"
                        )
                    body_limit = max_bytes if response_status < 400 else 64 * 1024
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > body_limit:
                            if response_status < 400:
                                raise RuntimeError(
                                    "Feishu attachment exceeds the local download limit"
                                )
                            break
                        body.extend(chunk)
                    response_content = bytes(body)
                if response_status == 401:
                    self._tenant_token = ""
                    self._tenant_token_expires_at = 0.0
                if response_status not in {401, 429} and response_status < 500:
                    break
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 3:
                    raise
            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))
        if response_status is None:
            raise RuntimeError("飞书附件下载失败：没有收到服务响应")
        if response_status >= 400:
            try:
                data = json.loads(response_content)
            except (UnicodeDecodeError, ValueError):
                data = {}
            detail = data.get("msg") or response_reason or "unknown error"
            raise RuntimeError(f"飞书附件下载失败（{response_status}）：{detail}")

        raw_name = Path(str(file_name or "").replace("\\", "/")).name
        safe_name = re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", raw_name).strip("._")
        if not safe_name:
            extension = ""
            if resource_type == "image":
                content_type = response_headers.get("content-type", "").split(";", 1)[0]
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                    "image/bmp": ".bmp",
                    "image/tiff": ".tiff",
                }.get(content_type, mimetypes.guess_extension(content_type) or ".jpg")
            safe_message_id = re.sub(r"[^A-Za-z0-9_-]+", "_", message_id[-24:]).strip("_")
            safe_name = f"feishu-{safe_message_id or 'attachment'}{extension}"
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = (destination_dir / safe_name).resolve()
        target.relative_to(destination_dir.resolve())
        if target.exists():
            target = target.with_name(f"{target.stem}-{int(time.time() * 1000)}{target.suffix}")
        target.write_bytes(response_content)
        return target

    async def send_image(
        self,
        receive_id: str,
        image_path: str | Path,
        *,
        receive_id_type: str = "open_id",
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        """Upload a local image to Feishu and send it as a native image message."""
        receive_id = (receive_id or self.default_receive_id).strip()
        path = Path(image_path).resolve()
        supported = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tif", ".tiff", ".heic"}
        if not receive_id:
            raise ValueError("receive_id is required")
        if not path.is_file() or path.suffix.lower() not in supported:
            raise ValueError("image_path must be a supported local image")
        if path.stat().st_size <= 0 or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Feishu image uploads must be between 1 byte and 10 MB")

        attempts = max(1, min(int(max_attempts), 8))
        last_error: Exception | None = None
        image_key = ""
        for attempt in range(1, attempts + 1):
            try:
                token = await self._get_tenant_token()
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                async with httpx.AsyncClient(timeout=60) as client:
                    with path.open("rb") as handle:
                        response = await client.post(
                            f"{self.base_url}/open-apis/im/v1/images",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"image_type": "message"},
                            files={"image": (path.name, handle, mime)},
                        )
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                image_key = str(data.get("data", {}).get("image_key") or "")
                if response.status_code < 400 and data.get("code", 0) == 0 and image_key:
                    break
                code = data.get("code", response.status_code)
                message = data.get("msg") or response.reason_phrase or "unknown error"
                last_error = RuntimeError(f"飞书上传图片失败（{code}）：{message}")
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 401:
                    self._tenant_token = ""
                    self._tenant_token_expires_at = 0.0
                    retryable = True
                if not retryable:
                    raise last_error
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = RuntimeError(f"飞书上传图片网络异常：{type(exc).__name__}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
        if not image_key:
            if last_error is None:
                last_error = RuntimeError("Feishu image upload failed without an error response")
            self.last_error = str(last_error)
            raise last_error
        return await self._send_message(
            receive_id,
            "image",
            {"image_key": image_key},
            receive_id_type=receive_id_type,
            max_attempts=max_attempts,
        )

    async def send_file(
        self,
        receive_id: str,
        file_path: str | Path,
        *,
        receive_id_type: str = "open_id",
        max_attempts: int = 4,
    ) -> dict[str, Any]:
        """Upload a local document/audio/video and send it as a Feishu file message."""
        receive_id = (receive_id or self.default_receive_id).strip()
        path = Path(file_path).resolve()
        file_types = {
            ".pdf": "pdf",
            ".doc": "doc",
            ".docx": "doc",
            ".xls": "xls",
            ".xlsx": "xls",
            ".ppt": "ppt",
            ".pptx": "ppt",
            ".txt": "stream",
            ".mp3": "stream",
            ".wav": "stream",
            ".flac": "stream",
            ".m4a": "stream",
            ".aac": "stream",
            ".ogg": "stream",
            ".opus": "opus",
            ".mp4": "stream",
            ".webm": "stream",
            ".mov": "stream",
            ".mkv": "stream",
        }
        file_type = file_types.get(path.suffix.lower(), "")
        if not receive_id:
            raise ValueError("receive_id is required")
        if not path.is_file() or not file_type:
            raise ValueError("file_path must be a supported document, audio, or video file")
        if path.stat().st_size <= 0 or path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("Feishu file uploads must be between 1 byte and 30 MB")

        attempts = max(1, min(int(max_attempts), 8))
        last_error: Exception | None = None
        file_key = ""
        for attempt in range(1, attempts + 1):
            try:
                token = await self._get_tenant_token()
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                async with httpx.AsyncClient(timeout=90) as client:
                    with path.open("rb") as handle:
                        response = await client.post(
                            f"{self.base_url}/open-apis/im/v1/files",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"file_type": file_type, "file_name": path.name},
                            files={"file": (path.name, handle, mime)},
                        )
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                file_key = str(data.get("data", {}).get("file_key") or "")
                if response.status_code < 400 and data.get("code", 0) == 0 and file_key:
                    break
                code = data.get("code", response.status_code)
                message = data.get("msg") or response.reason_phrase or "unknown error"
                last_error = RuntimeError(f"飞书上传文件失败（{code}）：{message}")
                retryable = response.status_code == 429 or response.status_code >= 500
                if response.status_code == 401:
                    self._tenant_token = ""
                    self._tenant_token_expires_at = 0.0
                    retryable = True
                if not retryable:
                    raise last_error
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = RuntimeError(f"飞书上传文件网络异常：{type(exc).__name__}: {exc}")
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))
        if not file_key:
            if last_error is None:
                last_error = RuntimeError("Feishu file upload failed without an error response")
            self.last_error = str(last_error)
            raise last_error
        return await self._send_message(
            receive_id,
            "file",
            {"file_key": file_key},
            receive_id_type=receive_id_type,
            max_attempts=max_attempts,
        )

    def parse_event(
        self, payload: dict[str, Any], *, mark_seen: bool = False
    ) -> dict[str, Any] | None:
        """Validate and normalize a Feishu v2 message event.

        Feishu's long-connection SDK can be added later without changing this
        normalized shape. HTTP callback mode is intentionally token-checked.
        """
        if "challenge" in payload:
            return {"challenge": payload["challenge"]}
        if payload.get("encrypt"):
            raise ValueError("当前回调暂不接受加密事件，请在飞书事件订阅中使用明文或长连接")
        header = payload.get("header") or {}
        if self.verification_token and header.get("token") != self.verification_token:
            raise PermissionError("飞书事件 verification token 不匹配")
        event_id = _normalized_event_id(header.get("event_id") or payload.get("uuid"))
        # Parsing is non-consuming by default. Ingress reserves the normalized
        # event and commits only after attachment/task acceptance succeeds.
        # ``mark_seen`` remains solely for older direct callers that explicitly
        # request the former immediate-consume behavior.
        if event_id and mark_seen and not self.accept_event_id(event_id):
            return None
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        message = event.get("message") or {}
        content = message.get("content") or "{}"
        try:
            content_data = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            content_data = {}
        prompt = str(content_data.get("text") or "").strip()
        message_type = str(message.get("message_type") or "text").strip().lower()
        resources: list[dict[str, str]] = []
        if message_type == "image" and content_data.get("image_key"):
            resources.append(
                {
                    "type": "image",
                    "file_key": str(content_data["image_key"]),
                    "file_name": "",
                }
            )
        elif message_type in {"file", "audio", "media"} and content_data.get("file_key"):
            resources.append(
                {
                    # Preserve the message kind for the local task pipeline;
                    # download_message_resource maps audio/media to Feishu's
                    # required resource query type=file.
                    "type": message_type,
                    "file_key": str(content_data["file_key"]),
                    "file_name": str(
                        content_data.get("file_name")
                        or ("feishu-audio.opus" if message_type == "audio" else "feishu-video.mp4" if message_type == "media" else "")
                    ),
                }
            )
        sender = event.get("sender") or {}
        sender_id = (sender.get("sender_id") or {}).get("open_id") or ""
        chat_id = str(message.get("chat_id") or "").strip()
        if not (prompt or resources) or not (sender_id or chat_id):
            return None
        normalized = {
            "event_id": event_id,
            "prompt": prompt,
            "receive_id": sender_id or chat_id,
            "receive_id_type": "open_id" if sender_id else "chat_id",
            "message_id": message.get("message_id", ""),
        }
        if resources:
            normalized["resources"] = resources
            normalized["message_type"] = message_type
        return normalized
