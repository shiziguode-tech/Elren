from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import psutil
import uvicorn
from Crypto.Cipher import AES
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from deepdesk.secret_storage import (
    LocalSecretVault,
    SecretProtector,
    SecretStorageError,
    SecretStorageUnavailable,
    atomic_write_secure,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
PACKET_AAD = b"elren-mobile-v1"
PAIRING_TTL_SECONDS = 300
MAX_PACKET_BYTES = 16 * 1024 * 1024
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_VIRTUAL_INTERFACE_MARKERS = (
    "virtual",
    "vethernet",
    "hyper-v",
    "vmware",
    "virtualbox",
    "wsl",
    "docker",
    "tailscale",
    "zerotier",
    "vpn",
    "loopback",
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    text = str(value or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _legacy_mobile_quarantine_path(data_dir: Path) -> Path:
    """Return an OS-owned location outside the movable workspace."""

    workspace = data_dir.resolve().parent
    identity = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        root = base / "Elren" / "security" / "mobile-legacy"
    elif sys.platform == "darwin":
        root = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Elren"
            / "security"
            / "mobile-legacy"
        )
    else:
        state_home = os.environ.get("XDG_STATE_HOME", "").strip()
        root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
        root = root / "elren" / "security" / "mobile-legacy"
    return root / f"{identity}.vault"


def derive_device_key(pairing_secret: bytes, pairing_id: str, device_id: str) -> bytes:
    material = f"elren-mobile-device\0{pairing_id}\0{device_id}".encode()
    return hmac.new(pairing_secret, material, hashlib.sha256).digest()


def pairing_proof(
    pairing_secret: bytes, pairing_id: str, device_id: str, client_nonce: str
) -> str:
    material = f"pair\0{pairing_id}\0{device_id}\0{client_nonce}".encode()
    return _b64encode(hmac.new(pairing_secret, material, hashlib.sha256).digest())


def connection_signature(device_key: bytes, device_id: str, timestamp: int, nonce: str) -> str:
    material = f"connect\0{device_id}\0{timestamp}\0{nonce}".encode()
    return _b64encode(hmac.new(device_key, material, hashlib.sha256).digest())


def revocation_signature(device_key: bytes, device_id: str, timestamp: int, nonce: str) -> str:
    material = f"revoke\0{device_id}\0{timestamp}\0{nonce}".encode()
    return _b64encode(hmac.new(device_key, material, hashlib.sha256).digest())


def encrypt_packet(device_key: bytes, payload: dict[str, Any]) -> dict[str, str | int]:
    nonce = os.urandom(12)
    cipher = AES.new(device_key, AES.MODE_GCM, nonce=nonce)
    cipher.update(PACKET_AAD)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext, tag = cipher.encrypt_and_digest(plain)
    return {
        "v": PROTOCOL_VERSION,
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
        "tag": _b64encode(tag),
    }


def decrypt_packet(device_key: bytes, envelope: dict[str, Any]) -> dict[str, Any]:
    if int(envelope.get("v") or 0) != PROTOCOL_VERSION:
        raise ValueError("Unsupported mobile protocol version")
    nonce = _b64decode(str(envelope.get("nonce") or ""))
    ciphertext = _b64decode(str(envelope.get("ciphertext") or ""))
    tag = _b64decode(str(envelope.get("tag") or ""))
    if len(nonce) != 12 or len(tag) != 16 or len(ciphertext) > MAX_PACKET_BYTES:
        raise ValueError("Invalid encrypted mobile packet")
    cipher = AES.new(device_key, AES.MODE_GCM, nonce=nonce)
    cipher.update(PACKET_AAD)
    plain = cipher.decrypt_and_verify(ciphertext, tag)
    value = json.loads(plain.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Mobile packet must contain an object")
    return value


def _unprotect_legacy_dpapi(record: dict[str, Any]) -> bytes:
    """Read only the old Windows DPAPI field during one-way migration.

    ``key_local`` was an unsafe fallback and is handled separately by removing
    the whole device record.  If DPAPI is unavailable, the caller keeps this
    protected legacy field intact and refuses to use or save the device.
    """

    protected = str(record.get("key_dpapi") or "")
    if not protected or os.name != "nt":
        raise SecretStorageUnavailable("Legacy mobile device key requires Windows DPAPI")
    try:
        import win32crypt

        value = bytes(
            win32crypt.CryptUnprotectData(
                _b64decode(protected), None, None, None, 0
            )[1]
        )
    except Exception as exc:
        raise SecretStorageUnavailable(
            "Legacy mobile device key could not be authenticated by Windows DPAPI"
        ) from exc
    if len(value) != 32:
        raise ValueError("Invalid legacy mobile device key")
    return value


def _is_rfc1918(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and any(address in network for network in _RFC1918_NETWORKS)


def _default_route_address() -> str:
    """Return the IPv4 address selected by the OS default route, without sending data."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0] or "")
    except OSError:
        return ""


def preferred_lan_address() -> str:
    try:
        interface_stats = psutil.net_if_stats()
        interface_addresses = psutil.net_if_addrs()
    except (OSError, RuntimeError, NotImplementedError, psutil.Error):
        route_address = _default_route_address()
        return route_address if _is_rfc1918(route_address) else ""
    candidates: list[tuple[int, str, str]] = []
    for interface_name, addresses in interface_addresses.items():
        lowered_name = interface_name.casefold()
        stats = interface_stats.get(interface_name)
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            value = str(address.address or "")
            if not _is_rfc1918(value):
                continue
            score = 100 if stats is None or stats.isup else 0
            if any(marker in lowered_name for marker in ("wi-fi", "wifi", "wlan", "ethernet")):
                score += 20
            if any(marker in lowered_name for marker in _VIRTUAL_INTERFACE_MARKERS):
                score -= 200
            candidates.append((score, interface_name, value))
    route_address = _default_route_address()
    route_candidate = next(
        (item for item in candidates if item[2] == route_address),
        None,
    )
    if route_candidate is not None and route_candidate[0] >= 0:
        return route_candidate[2]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1].casefold(), item[2]))
    return candidates[0][2]


@dataclass(slots=True)
class PendingPairing:
    pairing_id: str
    secret: bytes
    endpoint: str
    created_at: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float:
        return self.created_at + PAIRING_TTL_SECONDS


@dataclass(slots=True)
class ConnectedDevice:
    websocket: WebSocket
    key: bytes
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    capabilities: list[str] = field(default_factory=list)


class MobileBridge:
    """Encrypted LAN bridge for the user-authorized Android companion app."""

    def __init__(
        self,
        data_dir: Path,
        screenshot_dir: Path,
        port: int = 8768,
        *,
        secret_protector: SecretProtector | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.screenshot_dir = screenshot_dir
        self.port = int(port)
        self.store_path = data_dir / "mobile-devices.json"
        # Device metadata remains inspectable JSON, while all per-device
        # authentication keys live in this OS-backed vault.  There is no
        # plaintext/base64 key fallback.
        self.secret_path = data_dir / "mobile-device-secrets.vault"
        self._secret_protector = secret_protector
        self._secret_vault: LocalSecretVault | None = None
        self._secret_values: dict[str, Any] = {}
        self._secret_error = ""
        self._legacy_devices_quarantined = 0
        self.pairings: dict[str, PendingPairing] = {}
        self.connections: dict[str, ConnectedDevice] = {}
        self.pending_commands: dict[
            str, tuple[str, asyncio.Future[dict[str, Any]]]
        ] = {}
        self._connection_nonces: dict[tuple[str, str], float] = {}
        self._records = self._load_records()
        self._migrate_legacy_keys()
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._closing_tasks: set[asyncio.Task[None]] = set()
        self._start_lock = asyncio.Lock()
        self._error = ""
        self._app = self._create_mobile_app()

    @property
    def has_devices(self) -> bool:
        return bool(self._records)

    def _load_records(self) -> dict[str, dict[str, Any]]:
        if not self.store_path.is_file():
            return {}
        try:
            payload = json.loads(self.store_path.read_text(encoding="utf-8"))
            devices = payload.get("devices") if isinstance(payload, dict) else []
            return {
                str(item["device_id"]): dict(item)
                for item in devices or []
                if isinstance(item, dict) and DEVICE_ID_RE.fullmatch(str(item.get("device_id") or ""))
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _ensure_secret_vault(self) -> LocalSecretVault:
        if self._secret_vault is not None:
            return self._secret_vault
        try:
            self._secret_vault = LocalSecretVault(
                self.secret_path,
                self._secret_protector,
            )
            if self.secret_path.is_file():
                values = self._secret_vault.read().values
                self._secret_values = {
                    str(key): value for key, value in values.items()
                    if isinstance(value, str)
                }
            else:
                self._secret_values = {}
            self._secret_error = ""
            return self._secret_vault
        except SecretStorageError as exc:
            self._secret_error = str(exc)
            raise

    def _persist_secret_values(self) -> None:
        vault = self._ensure_secret_vault()
        vault.write_verified(dict(self._secret_values))

    def _migrate_legacy_keys(self) -> None:
        """Migrate DPAPI records into the vault and scrub legacy JSON fields.

        A record containing the old plaintext ``key_local`` field is never
        trusted.  Preserve it in an encrypted, current-user-bound quarantine
        outside the workspace when possible, remove it from active metadata,
        and require the phone to pair again.  If quarantine is unavailable,
        discard the unusable record rather than retaining plaintext forever.
        """

        if not self._quarantine_plaintext_legacy_records():
            return
        legacy = {
            device_id: record
            for device_id, record in self._records.items()
            if record.get("key_dpapi")
        }
        if not legacy:
            return
        try:
            self._ensure_secret_vault()
            for device_id, record in legacy.items():
                self._secret_values[device_id] = _b64encode(
                    _unprotect_legacy_dpapi(record)
                )
            self._persist_secret_values()
            for record in legacy.values():
                record.pop("key_dpapi", None)
                record.pop("key_local", None)
            self._write_metadata()
        except (SecretStorageError, OSError, ValueError, TypeError) as exc:
            self._secret_error = str(exc)
            logger.warning("Mobile device credentials were not migrated; secure store required")

    def _quarantine_plaintext_legacy_records(self) -> bool:
        legacy = {
            device_id: dict(record)
            for device_id, record in self._records.items()
            if record.get("key_local")
        }
        if not legacy:
            return True

        quarantined = False
        quarantine_path = _legacy_mobile_quarantine_path(self.data_dir)
        try:
            vault = LocalSecretVault(quarantine_path, self._secret_protector)
            previous: dict[str, Any] = {}
            if quarantine_path.is_file():
                loaded = vault.read().values.get("devices")
                if isinstance(loaded, dict):
                    previous = {
                        str(device_id): dict(record)
                        for device_id, record in loaded.items()
                        if isinstance(record, dict)
                    }
            previous.update(legacy)
            vault.write_verified(
                {
                    "schema": 1,
                    "reason": "plaintext_mobile_key_removed_repair_required",
                    "devices": previous,
                }
            )
            quarantined = True
        except (SecretStorageError, OSError, ValueError, TypeError) as exc:
            self._secret_error = str(exc)
            logger.warning(
                "Legacy plaintext mobile credentials could not be quarantined; removing active records"
            )

        remaining = {
            device_id: record
            for device_id, record in self._records.items()
            if device_id not in legacy
        }
        try:
            self._write_metadata(remaining)
        except (SecretStorageError, OSError, ValueError, TypeError) as exc:
            self._secret_error = str(exc)
            logger.warning("Legacy plaintext mobile credentials could not be removed")
            return False
        self._records = remaining
        self._legacy_devices_quarantined += len(legacy) if quarantined else 0
        return True

    def _write_metadata(
        self,
        records: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "schema": 2,
            "devices": list((self._records if records is None else records).values()),
        }
        atomic_write_secure(
            self.store_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def _save_records(self) -> None:
        # Never persist a device record carrying either legacy secret field.
        if any(record.get("key_dpapi") or record.get("key_local") for record in self._records.values()):
            raise SecretStorageUnavailable("Legacy mobile device key must be migrated before saving")
        self._write_metadata()

    def _public_record(self, device_id: str, record: dict[str, Any]) -> dict[str, Any]:
        connection = self.connections.get(device_id)
        return {
            "device_id": device_id,
            "name": str(record.get("name") or "Android device"),
            "android_version": str(record.get("android_version") or ""),
            "app_version": str(record.get("app_version") or ""),
            "paired_at": float(record.get("paired_at") or 0),
            "connected": connection is not None,
            "last_seen": connection.last_seen if connection else float(record.get("last_seen") or 0),
            "capabilities": list(connection.capabilities if connection else record.get("capabilities") or []),
        }

    def status(self) -> dict[str, Any]:
        devices = [self._public_record(device_id, record) for device_id, record in self._records.items()]
        return {
            "ready": bool(self._server and self._server.started and not self._error),
            "port": self.port,
            "lan_address": preferred_lan_address(),
            "paired": len(devices),
            "connected": sum(bool(item["connected"]) for item in devices),
            "devices": devices,
            "error": self._error,
            "security": "AES-256-GCM + HMAC-SHA256; device keys protected by the OS credential vault",
            "credential_store_ready": not bool(self._secret_error),
            "legacy_devices_quarantined": self._legacy_devices_quarantined,
        }

    async def start(self) -> None:
        async with self._start_lock:
            if self._server_task and not self._server_task.done():
                return
            self._error = ""
            config = uvicorn.Config(
                self._app,
                # LAN binding is required for the paired Android companion.
                # Every non-health operation is protected by an expiring HMAC
                # pairing proof or a per-device signed/encrypted session.
                host="0.0.0.0",  # noqa: S104
                port=self.port,
                log_level="warning",
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._server.install_signal_handlers = lambda: None
            self._server_task = asyncio.create_task(self._server.serve())
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if self._server.started:
                    return
                if self._server_task.done():
                    break
                await asyncio.sleep(0.05)
            self._error = f"Unable to start the Android bridge on port {self.port}"
            if self._server_task.done():
                try:
                    await self._server_task
                except Exception as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
            else:
                self._server.should_exit = True
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
            self._server_task = None
            raise RuntimeError(self._error)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await asyncio.wait_for(self._server_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
            self._server_task = None
        for device_id, connection in list(self.connections.items()):
            self._fail_pending_for_device(device_id, "Android bridge stopped")
            try:
                await connection.websocket.close(code=1001)
            except Exception:
                logger.debug("Failed to close mobile websocket during shutdown", exc_info=True)
        self.connections.clear()
        closing = list(self._closing_tasks)
        self._closing_tasks.clear()
        if closing:
            await asyncio.gather(*closing, return_exceptions=True)

    async def create_pairing(self) -> dict[str, Any]:
        await self.start()
        address = preferred_lan_address()
        if not address:
            raise RuntimeError("No private LAN IPv4 address is available for Android pairing")
        now = time.time()
        self.pairings = {
            key: value for key, value in self.pairings.items() if value.expires_at > now
        }
        pairing_id = uuid4().hex
        pending = PendingPairing(
            pairing_id=pairing_id,
            secret=os.urandom(32),
            endpoint=f"http://{address}:{self.port}",
        )
        self.pairings[pairing_id] = pending
        return {
            "pairing_id": pairing_id,
            "expires_at": pending.expires_at,
            "endpoint": pending.endpoint,
            "qr_url": f"/api/mobile/pairing/{pairing_id}/qr.png",
        }

    def pairing_payload(self, pairing_id: str) -> str:
        pending = self.pairings.get(pairing_id)
        if not pending or pending.expires_at <= time.time():
            self.pairings.pop(pairing_id, None)
            raise KeyError(pairing_id)
        query = urlencode(
            {
                "v": PROTOCOL_VERSION,
                "endpoint": pending.endpoint,
                "id": pending.pairing_id,
                "secret": _b64encode(pending.secret),
            }
        )
        return f"elren://pair?{query}"

    def pairing_qr_png(self, pairing_id: str) -> bytes:
        import qrcode

        image = qrcode.make(self.pairing_payload(pairing_id))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def revoke(self, device_id: str) -> bool:
        if device_id not in self._records:
            return False
        removed_record = self._records.pop(device_id, None)
        removed_secret = self._secret_values.pop(device_id, None)
        try:
            if removed_secret is not None:
                self._persist_secret_values()
            self._save_records()
        except Exception:
            # Do not strand a paired device in memory if the secure store or
            # metadata write is temporarily unavailable.
            if removed_record is not None:
                self._records[device_id] = removed_record
            if removed_secret is not None:
                self._secret_values[device_id] = removed_secret
            raise
        self._fail_pending_for_device(device_id, "Android device was revoked")
        connection = self.connections.pop(device_id, None)
        if connection:
            closing = asyncio.create_task(
                connection.websocket.close(code=4003, reason="Device revoked"),
                name=f"mobile-revoke-{device_id}",
            )
            self._closing_tasks.add(closing)
            closing.add_done_callback(self._closing_tasks.discard)
        return True

    def _consume_connection_nonce(self, device_id: str, nonce: str) -> bool:
        """Accept one fresh signed WebSocket nonce and reject LAN replays."""
        now = time.time()
        self._connection_nonces = {
            key: expires_at
            for key, expires_at in self._connection_nonces.items()
            if expires_at > now
        }
        key = (device_id, nonce)
        if key in self._connection_nonces:
            return False
        self._connection_nonces[key] = now + 120
        return True

    def _fail_pending_for_device(self, device_id: str, reason: str) -> None:
        """Wake commands immediately when their phone connection disappears."""
        for pending_device, future in list(self.pending_commands.values()):
            if pending_device == device_id and not future.done():
                future.set_exception(ConnectionError(reason))

    def _device_key(self, device_id: str) -> bytes:
        record = self._records.get(device_id)
        if not record:
            raise KeyError(device_id)
        if record.get("key_local") or record.get("key_dpapi"):
            raise SecretStorageUnavailable("Legacy mobile device key requires migration")
        try:
            self._ensure_secret_vault()
            value = _b64decode(str(self._secret_values.get(device_id) or ""))
        except (SecretStorageError, ValueError) as exc:
            raise SecretStorageUnavailable(
                "Secure mobile device credential store is unavailable"
            ) from exc
        if len(value) != 32:
            raise ValueError("Invalid stored mobile device key")
        return value

    def _create_mobile_app(self) -> FastAPI:
        app = FastAPI(title="Elren Android bridge", docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "protocol": PROTOCOL_VERSION}

        @app.post("/pair")
        async def pair(request: Request) -> dict[str, Any]:
            payload = await request.json()
            pairing_id = str(payload.get("pairing_id") or "")
            device_id = str(payload.get("device_id") or "")
            device_name = str(payload.get("device_name") or "Android device").strip()[:80]
            client_nonce = str(payload.get("client_nonce") or "")
            proof = str(payload.get("proof") or "")
            pending = self.pairings.get(pairing_id)
            if (
                not pending
                or pending.expires_at <= time.time()
                or not DEVICE_ID_RE.fullmatch(device_id)
                or not (16 <= len(client_nonce) <= 180)
            ):
                raise HTTPException(status_code=401, detail="Pairing request is invalid or expired")
            expected = pairing_proof(pending.secret, pairing_id, device_id, client_nonce)
            if not hmac.compare_digest(expected, proof):
                raise HTTPException(status_code=401, detail="Pairing proof is invalid")
            key = derive_device_key(pending.secret, pairing_id, device_id)
            record = {
                "device_id": device_id,
                "name": device_name or "Android device",
                "android_version": str(payload.get("android_version") or "")[:40],
                "app_version": str(payload.get("app_version") or "")[:40],
                "paired_at": time.time(),
                "last_seen": 0,
                "capabilities": [],
            }
            try:
                self._ensure_secret_vault()
                self._secret_values[device_id] = _b64encode(key)
                self._persist_secret_values()
                self._records[device_id] = record
                self._save_records()
            except (SecretStorageError, OSError, ValueError, TypeError) as exc:
                self._secret_values.pop(device_id, None)
                self._secret_error = str(exc)
                raise HTTPException(
                    status_code=503,
                    detail="Secure device credential storage is unavailable; device was not paired",
                ) from exc
            self.pairings.pop(pairing_id, None)
            return {"ok": True, "device_id": device_id, "protocol": PROTOCOL_VERSION}

        @app.delete("/devices/{device_id}")
        async def device_unpair(device_id: str, request: Request) -> dict[str, Any]:
            try:
                key = self._device_key(device_id)
            except (KeyError, ValueError, OSError, SecretStorageError) as exc:
                raise HTTPException(status_code=404, detail="Android device is not paired") from exc
            timestamp_text = str(request.query_params.get("ts") or "0")
            nonce = str(request.query_params.get("nonce") or "")
            signature = str(request.query_params.get("sig") or "")
            try:
                timestamp = int(timestamp_text)
            except ValueError:
                timestamp = 0
            expected = revocation_signature(key, device_id, timestamp, nonce)
            if (
                abs(int(time.time()) - timestamp) > 120
                or not (16 <= len(nonce) <= 180)
                or not hmac.compare_digest(expected, signature)
            ):
                raise HTTPException(status_code=401, detail="Unpair request is invalid")
            self.revoke(device_id)
            return {"ok": True, "device_id": device_id}

        @app.websocket("/ws/{device_id}")
        async def device_socket(websocket: WebSocket, device_id: str) -> None:
            try:
                key = self._device_key(device_id)
            except (KeyError, ValueError, OSError, SecretStorageError):
                await websocket.close(code=4003)
                return
            timestamp_text = str(websocket.query_params.get("ts") or "0")
            nonce = str(websocket.query_params.get("nonce") or "")
            signature = str(websocket.query_params.get("sig") or "")
            try:
                timestamp = int(timestamp_text)
            except ValueError:
                timestamp = 0
            expected = connection_signature(key, device_id, timestamp, nonce)
            if (
                abs(int(time.time()) - timestamp) > 120
                or not (16 <= len(nonce) <= 180)
                or not hmac.compare_digest(expected, signature)
            ):
                await websocket.close(code=4003)
                return
            if not self._consume_connection_nonce(device_id, nonce):
                await websocket.close(code=4003)
                return
            await websocket.accept()
            previous = self.connections.pop(device_id, None)
            if previous:
                self._fail_pending_for_device(
                    device_id, "Android device reconnected before the command completed"
                )
                try:
                    await previous.websocket.close(code=4000, reason="Reconnected")
                except Exception:
                    logger.debug("Failed to close superseded mobile websocket", exc_info=True)
            connection = ConnectedDevice(websocket=websocket, key=key)
            self.connections[device_id] = connection
            try:
                await websocket.send_json(
                    encrypt_packet(key, {"type": "hello", "protocol": PROTOCOL_VERSION})
                )
                while True:
                    envelope = await websocket.receive_json()
                    payload = decrypt_packet(key, envelope)
                    connection.last_seen = time.time()
                    if payload.get("type") == "hello":
                        connection.capabilities = [
                            str(item)[:80] for item in payload.get("capabilities") or []
                        ][:40]
                        record = self._records.get(device_id)
                        if record:
                            record["last_seen"] = connection.last_seen
                            record["capabilities"] = connection.capabilities
                            self._save_records()
                    elif payload.get("type") == "result":
                        request_id = str(payload.get("id") or "")
                        pending = self.pending_commands.get(request_id)
                        future = pending[1] if pending and pending[0] == device_id else None
                        if future is not None and not future.done():
                            future.set_result(payload)
                    elif payload.get("type") == "ping":
                        await websocket.send_json(
                            encrypt_packet(key, {"type": "pong", "time": time.time()})
                        )
                    elif payload.get("type") == "unpair":
                        self.revoke(device_id)
                        await websocket.close(code=4003, reason="Device unpaired")
                        break
            except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
                pass
            finally:
                if self.connections.get(device_id) is connection:
                    self.connections.pop(device_id, None)
                    self._fail_pending_for_device(
                        device_id, "Android device disconnected before the command completed"
                    )

        return app

    def resolve_device_id(self, requested: str = "") -> str:
        requested = str(requested or "").strip()
        if requested:
            if requested not in self._records:
                raise KeyError(f"Android device is not paired: {requested}")
            return requested
        connected = list(self.connections)
        if len(connected) == 1:
            return connected[0]
        if not connected:
            raise RuntimeError("No paired Android device is currently connected")
        raise RuntimeError("Multiple Android devices are connected; specify device_id")

    async def command(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        *,
        device_id: str = "",
        autonomous: bool = False,
        timeout: float = 30,
    ) -> dict[str, Any]:
        resolved = self.resolve_device_id(device_id)
        connection = self.connections.get(resolved)
        if not connection:
            raise RuntimeError(f"Android device is offline: {resolved}")
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending_commands[request_id] = (resolved, future)
        payload = {
            "type": "command",
            "id": request_id,
            "action": action,
            "arguments": arguments or {},
            "autonomous": bool(autonomous),
            "created_at": time.time(),
        }
        try:
            await connection.websocket.send_json(encrypt_packet(connection.key, payload))
            result = await asyncio.wait_for(future, timeout=max(2, min(float(timeout), 120)))
        finally:
            self.pending_commands.pop(request_id, None)
        if not bool(result.get("ok")):
            raise RuntimeError(str(result.get("error") or "Android command failed"))
        value = result.get("result")
        return value if isinstance(value, dict) else {"value": value}
