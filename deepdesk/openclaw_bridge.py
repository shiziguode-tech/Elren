from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx

from deepdesk.secret_redaction import redact_text
from deepdesk.secret_storage import (
    LocalSecretVault,
    SecretProtector,
    SecretStorageFormatError,
    SecretStorageUnavailable,
    atomic_write_secure,
    scrub_env_file,
)
from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.tool_runtime import ToolRuntimeManifestError, discover_tool_runtime

if TYPE_CHECKING:
    from deepdesk.provider_secrets import ProviderSecretsStore

DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789"
_OPENCLAW_CONFIG_THREAD_LOCK = threading.RLock()
_MANAGED_CREDENTIAL_SKILLS = {
    "github",
    "gh-issues",
    "goplaces",
    "trello",
    "sag",
    "notion",
    "sonoscli",
    "openai-whisper-api",
    "oracle",
    "gemini",
    "coding-agent",
    "1password",
    "gifgrep",
    "summarize",
    "eightctl",
    "ordercli",
    "things-mac",
}
_GATEWAY_TOKEN_ENV_NAMES = frozenset(
    {
        "ELREN_OPENCLAW_GATEWAY_TOKEN",
        "DEEPDESK_OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
    }
)
_OPENCLAW_PROVIDER_CREDENTIAL_FIELDS = (
    "apiKey",
    "api_key",
    "accessToken",
    "access_token",
    "secretKey",
    "secret_key",
)
_OPENCLAW_MANAGED_PATH_PATTERNS = (
    # Root Gateway configuration and its bounded recovery/atomic-write names.
    "openclaw.json",
    "openclaw.json.last-good",
    "openclaw.json.bak*",
    "openclaw.json.tmp",
    "openclaw.json.*.tmp",
    "openclaw.tmp",
    "openclaw-*.tmp",
    ".openclaw*.tmp",
    # Per-agent provider model configuration. Exactly one agent directory
    # component is accepted; no recursive workspace walk is permitted.
    "agents/*/agent/models.json",
    "agents/*/agent/models.json.last-good",
    "agents/*/agent/models.json.bak*",
    "agents/*/agent/models.json.tmp",
    "agents/*/agent/models.json.*.tmp",
    "agents/*/agent/models.tmp",
    "agents/*/agent/models-*.tmp",
    "agents/*/agent/.models.json.*.tmp",
    # Generated provider catalogs live directly under one plugin directory.
    "agents/*/agent/plugins/*/catalog.json",
    "agents/*/agent/plugins/*/catalog.json.last-good",
    "agents/*/agent/plugins/*/catalog.json.bak*",
    "agents/*/agent/plugins/*/catalog.json.tmp",
    "agents/*/agent/plugins/*/catalog.json.*.tmp",
    "agents/*/agent/plugins/*/catalog.tmp",
    "agents/*/agent/plugins/*/catalog-*.tmp",
    "agents/*/agent/plugins/*/.catalog.json.*.tmp",
)


@contextmanager
def _exclusive_config_lock(lock_path: Path) -> Iterator[None]:
    """Serialize OpenClaw config updates across threads and app processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _OPENCLAW_CONFIG_THREAD_LOCK, lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            acquired = False
            for _ in range(80):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.025)
            if not acquired:
                raise TimeoutError("Timed out waiting for the OpenClaw config lock")
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OpenClawBridge:
    """Compatibility bridge to an official OpenClaw CLI/Gateway runtime."""

    def __init__(
        self,
        enabled: bool,
        cli: str,
        gateway_url: str,
        gateway_token: str,
        auto_start: bool,
        workspace: Path,
        provider_env: dict[str, str] | None = None,
        browser_allowed_hosts: list[str] | None = None,
        secret_protector: SecretProtector | None = None,
        provider_secrets: ProviderSecretsStore | None = None,
    ) -> None:
        self.enabled = enabled
        self.configured_cli = cli
        self.gateway_url = self._isolated_gateway_url(
            gateway_url, gateway_token, workspace
        )
        self.configured_gateway_token = gateway_token
        self.auto_start = auto_start
        self.workspace = workspace
        self.runtime_root = workspace / "work" / "openclaw-runtime"
        self.openclaw_entry = self.runtime_root / "node_modules" / "openclaw" / "openclaw.mjs"
        self.state_dir = workspace / "data" / "openclaw"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.state_dir / "openclaw.json"
        self.gateway_token_path = self.state_dir / "gateway.token"
        self.gateway_log_path = self.state_dir / "gateway-startup.log"
        self._secret_protector = secret_protector
        self._provider_secrets = provider_secrets
        self.gateway_token = self._resolve_gateway_token()
        self.provider_env = {key: value for key, value in (provider_env or {}).items() if value}
        self._subprocess_secret_history: set[str] = set()
        self._gateway_log_thread: threading.Thread | None = None
        self._remove_stale_agent_message_files()
        self._sanitize_gateway_log()
        self._delegated_credential_skills: set[str] = set()
        # Kept in the constructor for compatibility with older package configs.
        # Public browsing is unrestricted by hostname; OpenClaw's private-network
        # SSRF guard remains active below.
        del browser_allowed_hosts
        self.browser_allowed_hosts: list[str] = []
        self.web_search_provider = ""
        self._ensure_browser_policy()
        self.process: subprocess.Popen | None = None
        self.process_ownership = "none"
        self._ready_lock = asyncio.Lock()
        self.last_error = ""
        self.tool_count: int | None = None
        self.plugin_count: int | None = None
        self.loaded_plugin_count: int | None = None
        self._last_gateway_ready = False
        self._catalog_cache: tuple[float, dict[str, Any]] | None = None
        self._plugins_cache: tuple[float, dict[str, Any]] | None = None

    def _package_cli(self) -> str | None:
        """Resolve the bundled entry while rejecting links outside this package."""
        try:
            runtime_root = self.runtime_root.resolve(strict=True)
            entry = self.openclaw_entry.resolve(strict=True)
            entry.relative_to(runtime_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return str(entry) if entry.is_file() else None

    def _resolved_cli(self) -> tuple[str | None, str]:
        """Prefer this package, then use an explicit/system fallback if absent."""
        package_cli = self._package_cli()
        if package_cli:
            return package_cli, "package-local"

        configured = ""
        if self.configured_cli:
            configured_path = Path(self.configured_cli).expanduser()
            if configured_path.is_file():
                configured = str(configured_path.resolve())
            else:
                configured = shutil.which(self.configured_cli) or ""
        configured = self._external_cli(configured)
        if configured:
            return configured, "configured-fallback"

        system_cli = self._external_cli(shutil.which("openclaw") or "")
        if system_cli:
            return system_cli, "system-fallback"
        return None, "missing"

    def _external_cli(self, candidate: str) -> str:
        if not candidate:
            return ""
        try:
            resolved = Path(candidate).expanduser().resolve(strict=True)
            resolved.relative_to(self.workspace.resolve())
            return ""
        except ValueError:
            return str(resolved) if resolved.is_file() else ""
        except (OSError, RuntimeError):
            return ""

    def resolve_cli(self) -> str | None:
        """Return the selected CLI using package-first fallback semantics.

        The local pnpm `.bin/openclaw.cmd` wrapper is deliberately bypassed
        because it can retain an absolute NODE_PATH after a package is moved.
        """
        return self._resolved_cli()[0]

    @property
    def http_base_url(self) -> str:
        parsed = urlparse(self.gateway_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunparse((scheme, parsed.netloc, "", "", "", "")).rstrip("/")

    async def status(
        self,
        probe_catalog: bool = False,
        probe_health: bool = True,
    ) -> dict[str, Any]:
        cli, cli_source = self._resolved_cli()
        ready = (
            await self._health()
            if probe_health
            else bool(
                self._last_gateway_ready
                or (self.process and self.process.poll() is None)
            )
        )
        if ready:
            self.process_ownership = (
                "owned"
                if self.process and self.process.poll() is None
                else "adopted_existing"
            )
            self.last_error = ""
        if ready and cli and probe_catalog:
            try:
                catalog = await self.catalog()
                self.tool_count = self._count_tools(catalog)
            except Exception as exc:
                self.last_error = str(exc)
        return {
            "enabled": self.enabled,
            "cli_installed": cli is not None,
            "cli_path": cli or "",
            "cli_source": cli_source,
            "runtime_root": str(self.runtime_root.resolve()),
            "using_fallback": cli_source in {"configured-fallback", "system-fallback"},
            "gateway_url": self.gateway_url,
            "gateway_auth": "configured" if self.configured_gateway_token else "project-vault",
            "gateway_ready": ready,
            "gateway_state": "ready" if ready else "stopped",
            "auto_start": self.auto_start,
            "managed_process_running": bool(self.process and self.process.poll() is None),
            "process_ownership": self.process_ownership if ready else "none",
            "isolated_state": str(self.state_dir),
            "tool_count": self.tool_count,
            "plugin_count": self.plugin_count,
            "loaded_plugin_count": self.loaded_plugin_count,
            "last_error": self.last_error,
            "browser_allowed_hosts": self.browser_allowed_hosts,
            "web_search_provider": self.web_search_provider,
            "delegated_skill_credentials": (
                "elren-shell-only"
                if self._delegated_credential_skills
                else "not-configured"
            ),
        }

    async def ensure_ready(self) -> None:
        async with self._ready_lock:
            if not self.enabled:
                raise RuntimeError("OpenClaw compatibility is disabled")
            if await self._health():
                self.process_ownership = (
                    "owned"
                    if self.process and self.process.poll() is None
                    else "adopted_existing"
                )
                self.last_error = ""
                return
            cli = self.resolve_cli()
            if not cli:
                raise RuntimeError(
                    "The package-local OpenClaw runtime is missing and no computer-wide fallback was found"
                )
            if not self.auto_start:
                raise RuntimeError("OpenClaw Gateway is not running")
            self._start_gateway(cli)
            for _ in range(30):
                await asyncio.sleep(1)
                if await self._health():
                    self.process_ownership = "owned"
                    self.last_error = ""
                    return
                if self.process and self.process.poll() is not None:
                    self.last_error = self._startup_log_tail()
                    break
            detail = f": {self.last_error}" if self.last_error else ""
            raise RuntimeError(f"OpenClaw Gateway did not become ready{detail}")

    async def stop(self) -> None:
        """Stop only the Gateway process tree created by this bridge instance."""
        process = self.process
        if not process or process.poll() is not None or self.process_ownership != "owned":
            await self._join_gateway_log_thread()
            self.process = None
            self.process_ownership = "none"
            self._last_gateway_ready = False
            return
        if os.name == "nt":
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await taskkill.communicate()
        else:
            process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except TimeoutError:
                process.kill()
        await self._join_gateway_log_thread()
        self.process = None
        self.process_ownership = "none"
        self._last_gateway_ready = False
        self._catalog_cache = None
        self._plugins_cache = None

    async def _join_gateway_log_thread(self) -> None:
        thread = self._gateway_log_thread
        if thread and thread.is_alive():
            await asyncio.to_thread(thread.join, 5)
        self._gateway_log_thread = None

    async def catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        if (
            not refresh
            and self._catalog_cache
            and time.monotonic() - self._catalog_cache[0] < 60
        ):
            return self._catalog_cache[1]
        await self.ensure_ready()
        catalog = await self._gateway_call("tools.catalog", {})
        self.tool_count = self._count_tools(catalog)
        self._catalog_cache = (time.monotonic(), catalog)
        return catalog

    async def invoke(self, tool: str, args: dict[str, Any], session_key: str = "main") -> Any:
        await self.ensure_ready()
        headers = {"Content-Type": "application/json"}
        if self.gateway_token:
            headers["Authorization"] = f"Bearer {self.gateway_token}"
        payload = {"tool": tool, "args": args, "sessionKey": session_key}
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            response = await client.post(
                f"{self.http_base_url}/tools/invoke", headers=headers, json=payload
            )
            if response.is_error:
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except ValueError:
                    detail = response.text
                raise RuntimeError(f"OpenClaw tool {tool} failed: {detail}")
            return response.json()

    async def plugins(self, *, refresh: bool = False) -> dict[str, Any]:
        if (
            not refresh
            and self._plugins_cache
            and time.monotonic() - self._plugins_cache[0] < 60
        ):
            return self._plugins_cache[1]
        cli = self.resolve_cli()
        if not cli:
            raise RuntimeError("OpenClaw CLI is not installed")
        process = await asyncio.create_subprocess_exec(
            *self._cli_command(cli, "plugins", "list", "--json"),
            cwd=self.workspace,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await self._communicate_process(
            process,
            timeout=120,
            operation="OpenClaw plugin discovery",
            known_secrets=self._subprocess_secret_values(),
        )
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-4000:])
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
        plugins = payload.get("plugins", [])
        self.plugin_count = len(plugins)
        self.loaded_plugin_count = sum(item.get("status") == "loaded" for item in plugins)
        result = {
            "plugins": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "enabled": item.get("enabled"),
                    "tools": (item.get("contracts") or {}).get("tools", []),
                    "channels": item.get("channelIds", []),
                    "providers": item.get("providerIds", []),
                }
                for item in plugins
            ],
            "total": self.plugin_count,
            "loaded": self.loaded_plugin_count,
        }
        self._plugins_cache = (time.monotonic(), result)
        return result

    async def agent_exec(self, prompt: str) -> dict[str, Any]:
        cli = self.resolve_cli()
        if not cli:
            raise RuntimeError("OpenClaw CLI is not installed")
        if redact_text(prompt, self._subprocess_secret_values()) != prompt:
            raise PermissionError(
                "Refusing to send a credential-bearing prompt to OpenClaw"
            )
        message_path = self.state_dir / f".agent-message-{secrets.token_hex(16)}.txt"
        atomic_write_secure(message_path, prompt.encode("utf-8"))
        try:
            process = await asyncio.create_subprocess_exec(
                *self._cli_command(
                    cli,
                    "agent",
                    "--local",
                    "--agent",
                    "main",
                    "--message-file",
                    str(message_path),
                    "--json",
                    "--timeout",
                    "90",
                ),
                cwd=self.workspace,
                env=self._environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await self._communicate_process(
                process,
                timeout=100,
                operation="OpenClaw agent exec",
                known_secrets=self._subprocess_secret_values(),
            )
            if process.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace")[-4000:])
            return json.loads(stdout.decode("utf-8", errors="replace"))
        finally:
            message_path.unlink(missing_ok=True)

    async def _gateway_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        cli = self.resolve_cli()
        if not cli:
            raise RuntimeError("OpenClaw CLI is not installed")
        args = self._cli_command(
            cli, "gateway", "call", method, "--params", json.dumps(params), "--json"
        )
        if self.gateway_url != DEFAULT_GATEWAY_URL or self.gateway_token:
            args.extend(["--url", self.gateway_url])
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.workspace,
            env=self._environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await self._communicate_process(
            process,
            timeout=120,
            operation=f"OpenClaw gateway call {method}",
            known_secrets=self._subprocess_secret_values(),
        )
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-4000:])
        return json.loads(stdout.decode("utf-8", errors="replace"))

    @staticmethod
    async def _terminate_process(process: Any) -> None:
        """Best-effort reap of a task-owned asyncio subprocess."""
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            # The process has already received an unconditional kill.  Do not
            # mask the caller's cancellation or timeout if the platform cannot
            # report final exit promptly.
            pass

    @classmethod
    async def _communicate_process(
        cls,
        process: Any,
        *,
        timeout: float,
        operation: str,
        known_secrets: tuple[str, ...] = (),
    ) -> tuple[bytes, bytes]:
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            return (
                cls._redact_subprocess_bytes(stdout, known_secrets),
                cls._redact_subprocess_bytes(stderr, known_secrets),
            )
        except TimeoutError as exc:
            await cls._terminate_process(process)
            raise RuntimeError(
                f"{operation} timed out after {int(timeout)} seconds"
            ) from exc
        except asyncio.CancelledError:
            await cls._terminate_process(process)
            raise

    async def _health(self) -> bool:
        if not self.enabled:
            self._last_gateway_ready = False
            return False
        try:
            # Loopback traffic must never be routed through a machine-wide HTTP
            # proxy. A configured proxy previously made a healthy Gateway look
            # offline and triggered a duplicate startup attempt.
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(f"{self.http_base_url}/healthz")
            self._last_gateway_ready = response.status_code == 200
            return self._last_gateway_ready
        except httpx.HTTPError:
            self._last_gateway_ready = False
            return False

    def _start_gateway(self, cli: str) -> None:
        if self.process and self.process.poll() is None:
            return
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        parsed = urlparse(self.gateway_url)
        command = self._cli_command(cli, "gateway", "run", "--allow-unconfigured")
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
            command.extend(["--bind", "loopback", "--port", str(parsed.port)])
        self._sanitize_gateway_log()
        if not self.gateway_log_path.exists():
            atomic_write_secure(self.gateway_log_path, b"")
        self.process = subprocess.Popen(
            command,
            cwd=self.workspace,
            env=self._environment(),
            creationflags=flags,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        stream = getattr(self.process, "stdout", None)
        if stream is not None:
            self._gateway_log_thread = threading.Thread(
                target=self._redacting_gateway_log_pump,
                args=(stream,),
                name="elren-openclaw-log-redactor",
                daemon=True,
            )
            self._gateway_log_thread.start()
        self.process_ownership = "owned"

    def _startup_log_tail(self, limit: int = 4000) -> str:
        try:
            raw = self.gateway_log_path.read_bytes()
        except OSError:
            return ""
        redacted = self._redact_subprocess_bytes(raw, self._subprocess_secret_values())
        if redacted != raw and not (self.process and self.process.poll() is None):
            atomic_write_secure(self.gateway_log_path, redacted)
        return redacted[-limit:].decode("utf-8", errors="replace").strip()

    def _subprocess_secret_values(self) -> tuple[str, ...]:
        """Return exact secrets intentionally visible to OpenClaw processes."""

        values = {
            str(value)
            for value in (self.gateway_token, *self.provider_env.values())
            if len(str(value)) >= 4
        }
        self._subprocess_secret_history.update(values)
        return tuple(
            sorted(self._subprocess_secret_history, key=len, reverse=True)
        )

    @staticmethod
    def _redact_subprocess_bytes(
        payload: bytes, known_secrets: tuple[str, ...]
    ) -> bytes:
        if not payload:
            return payload
        decoded = payload.decode("utf-8", errors="replace")
        return redact_text(decoded, known_secrets).encode("utf-8")

    def _sanitize_gateway_log(self) -> None:
        """Remove configured exact secrets from a prior persistent log."""

        if not self.gateway_log_path.is_file():
            return
        raw = self.gateway_log_path.read_bytes()
        redacted = self._redact_subprocess_bytes(raw, self._subprocess_secret_values())
        if redacted != raw:
            atomic_write_secure(self.gateway_log_path, redacted)

    def _redacting_gateway_log_pump(self, stream: Any) -> None:
        """Persist child output one complete line at a time after redaction."""

        try:
            with self.gateway_log_path.open("ab", buffering=0) as log:
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    log.write(
                        self._redact_subprocess_bytes(
                            line, self._subprocess_secret_values()
                        )
                    )
        finally:
            try:
                stream.close()
            except (AttributeError, OSError):
                pass

    def _remove_stale_agent_message_files(self) -> None:
        """Remove prompt files left only by an unclean prior process exit."""

        candidates = set(self.state_dir.glob(".agent-message-*.txt"))
        candidates.update(self.state_dir.glob("..agent-message-*.txt.*.tmp"))
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink()

    def _environment(self) -> dict[str, str]:
        # Do not expose unrelated provider credentials to the Node gateway.
        # Credentials selected for this isolated runtime are added explicitly.
        environment = credential_safe_environment(self.provider_env)
        environment["OPENCLAW_STATE_DIR"] = str(self.state_dir)
        environment["OPENCLAW_CONFIG_PATH"] = str(self.config_path)
        if self.gateway_token:
            environment["OPENCLAW_GATEWAY_TOKEN"] = self.gateway_token
        return environment

    def _package_node(self) -> str:
        """Resolve Node from this movable package before consulting the host."""

        candidates = [
            self.workspace / "work" / "node-runtime" / ("node.exe" if os.name == "nt" else "bin/node"),
        ]
        try:
            runtime = discover_tool_runtime(self.workspace)
        except ToolRuntimeManifestError:
            runtime = None
        if runtime:
            for tool in runtime.tools:
                resolved_tool = Path(str(tool.get("resolved_executable") or ""))
                if (
                    tool.get("id") == "node"
                    and tool.get("present")
                    and resolved_tool.suffix.casefold() in {".exe", ""}
                ):
                    candidates.append(resolved_tool)
        workspace = self.workspace.resolve()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(workspace)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.is_file():
                return str(resolved)
        return shutil.which("node") or ""

    def _resolve_gateway_token(self) -> str:
        parsed = urlparse(self.gateway_url)
        is_local = parsed.scheme == "ws" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if not is_local and not self.configured_gateway_token:
            return ""
        vault = LocalSecretVault(self.gateway_token_path, self._secret_protector)
        lock_path = self.gateway_token_path.with_suffix(".lock")
        with _exclusive_config_lock(lock_path):
            stored_token = ""
            encrypted = False
            if self.gateway_token_path.is_file():
                raw = self.gateway_token_path.read_bytes()
                encrypted = LocalSecretVault.is_encrypted_bytes(raw)
                if encrypted:
                    values = vault.read().values
                    stored_token = str(values.get("gateway_token") or "")
                else:
                    try:
                        stored_token = raw.decode("utf-8").strip()
                    except UnicodeDecodeError as exc:
                        raise SecretStorageFormatError(
                            "The legacy OpenClaw Gateway token is not UTF-8"
                        ) from exc
                if len(stored_token) < 32:
                    raise SecretStorageFormatError(
                        "The OpenClaw Gateway token store is truncated or invalid"
                    )

            token = self.configured_gateway_token or stored_token
            if not token:
                token = secrets.token_urlsafe(48)
            if len(token) < 32:
                raise SecretStorageFormatError(
                    "The configured OpenClaw Gateway token is too short"
                )
            if not encrypted or token != stored_token:
                vault.write_verified({"gateway_token": token})

            # The vault has been authenticated or verified above. Only now may
            # a legacy .env copy be cleared; os.environ remains untouched for
            # the already-running process.
            scrub_env_file(self.workspace / ".env", _GATEWAY_TOKEN_ENV_NAMES)
            return token

    @staticmethod
    def _isolated_gateway_url(gateway_url: str, gateway_token: str, workspace: Path) -> str:
        """Give each movable local package a stable loopback port of its own."""
        if gateway_url.rstrip("/") != DEFAULT_GATEWAY_URL or gateway_token:
            return gateway_url
        identity = str(workspace.resolve()).casefold().encode("utf-8")
        offset = int.from_bytes(hashlib.sha256(identity).digest()[:4], "big") % 10000
        return f"ws://127.0.0.1:{20000 + offset}"

    def _update_config(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        """Apply a read-modify-write transaction without shared temp-file races."""
        lock_path = self.state_dir / "openclaw.config.lock"
        with _exclusive_config_lock(lock_path):
            try:
                config = (
                    json.loads(self.config_path.read_text(encoding="utf-8"))
                    if self.config_path.is_file()
                    else {}
                )
            except (OSError, ValueError):
                config = {}
            if not isinstance(config, dict):
                config = {}
            mutate(config)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="openclaw-", suffix=".tmp", dir=self.state_dir
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(5):
                    try:
                        os.replace(temporary, self.config_path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)
            try:
                self.config_path.chmod(0o600)
            except OSError:
                pass

    def _ensure_browser_policy(self) -> None:
        """Bind isolated OpenClaw state to this movable app folder and keep browsing safe."""
        def mutate(config: dict[str, Any]) -> None:
            browser = config.setdefault("browser", {})
            policy = browser.setdefault("ssrfPolicy", {})
            policy["dangerouslyAllowPrivateNetwork"] = False
            policy.pop("allowedHostnames", None)
            agents = config.setdefault("agents", {})
            defaults = agents.setdefault("defaults", {})
            # Never retain the absolute workspace from the machine/folder where a
            # package was built. Each EXE process makes its own package root the
            # active OpenClaw workspace, whose `skills/` directory is watched.
            defaults["workspace"] = str(self.workspace.resolve())
            skills = config.setdefault("skills", {})
            skill_loading = skills.setdefault("load", {})
            skill_loading["watch"] = True
            tools = config.setdefault("tools", {})
            web = tools.setdefault("web", {})
            search = web.setdefault("search", {})
            search.setdefault("enabled", True)
            search.setdefault("provider", "duckduckgo")
            search.setdefault("timeoutSeconds", 20)
            search.setdefault("maxResults", 8)
            self.web_search_provider = str(search.get("provider") or "")
            if self.web_search_provider == "duckduckgo":
                plugins = config.setdefault("plugins", {})
                entries = plugins.setdefault("entries", {})
                duckduckgo = entries.setdefault("duckduckgo", {})
                duckduckgo.setdefault("enabled", True)
                plugin_config = duckduckgo.setdefault("config", {})
                web_search = plugin_config.setdefault("webSearch", {})
                web_search.setdefault("safeSearch", "moderate")

        self._update_config(mutate)

    def set_skill_credentials(self, credentials: dict[str, dict[str, str]]) -> None:
        """Remove legacy OpenClaw plaintext and record Elren-only delegation.

        Arbitrary multi-variable skill ``env`` values cannot use OpenClaw's
        single-value SecretRef contract. Copying all keys into the persistent
        Gateway environment would let any delegated skill/exec read every key.
        Those credentials therefore remain available only through Elren's
        per-skill ShellTool injection; the Gateway receives none of them.
        """
        configured = {
            skill
            for skill, values in credentials.items()
            if skill in _MANAGED_CREDENTIAL_SKILLS
            and any(str(value) for value in values.values())
        }

        # Provider JSON is handled as a separate transaction: capture every
        # recognized DeepSeek credential, complete an authenticated vault
        # write/read, and only then rewrite any legacy plaintext file.
        self.migrate_provider_credentials()

        def mutate(config: dict[str, Any]) -> None:
            self._remove_managed_skill_plaintext(config)

        self._update_config(mutate)
        self._scrub_openclaw_recovery_copies()
        self._delegated_credential_skills = configured

    def migrate_provider_credentials(self) -> None:
        """Vault legacy OpenClaw DeepSeek keys before removing JSON copies."""

        self._scrub_openclaw_managed_files()
        self._sanitize_gateway_log()

    @staticmethod
    def _remove_managed_skill_plaintext(config: dict[str, Any]) -> None:
        skills = config.get("skills")
        if not isinstance(skills, dict):
            return
        entries = skills.get("entries")
        if not isinstance(entries, dict):
            return
        for skill in _MANAGED_CREDENTIAL_SKILLS:
            entry = entries.get(skill)
            if not isinstance(entry, dict):
                continue
            entry.pop("env", None)
            entry.pop("apiKey", None)
            if not entry:
                entries.pop(skill, None)

    def _scrub_openclaw_recovery_copies(self) -> None:
        """Scrub root-config recovery copies without deleting failed migrations."""

        lock_path = self.state_dir / "openclaw.config.lock"
        with _exclusive_config_lock(lock_path):
            recovery_files = {self.state_dir / "openclaw.json.last-good"}
            recovery_files.update(self.state_dir.glob("openclaw.json.bak*"))
            for recovery in recovery_files:
                if not recovery.is_file():
                    continue
                try:
                    config = json.loads(recovery.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    # A recovery copy may be the only copy of user settings.
                    # Preserve it when parsing or migration fails so the next
                    # startup can retry rather than causing data loss.
                    continue
                if not isinstance(config, dict):
                    continue
                self._remove_managed_skill_plaintext(config)
                atomic_write_secure(
                    recovery,
                    (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(
                        "utf-8"
                    ),
                )

            stale_temps = set(self.state_dir.glob("openclaw-*.tmp"))
            stale_temps.update(self.state_dir.glob(".openclaw*.tmp"))
            stale_temps.update(self.state_dir.glob("openclaw.json.*.tmp"))
            for temporary in stale_temps:
                if temporary.is_file() and not temporary.is_symlink():
                    self._scrub_managed_json_file(temporary)

    def _scrub_openclaw_managed_files(self) -> None:
        """Migrate credentials from the bounded OpenClaw JSON surface.

        Generated provider catalogs live outside ``openclaw.json`` and older
        releases wrote ``providers.*.apiKey`` there.  Only known catalog/model
        and root-config filenames are considered; arbitrary plugin JSON and
        binary state are not touched.  Each rewrite is atomic and protected by
        the same cross-thread/process lock as config updates.
        """

        lock_path = self.state_dir / "openclaw.config.lock"
        with _exclusive_config_lock(lock_path):
            if not self.state_dir.is_dir():
                return

            parsed: list[tuple[Path, dict[str, Any]]] = []
            credentials: set[str] = set()
            for candidate in sorted(
                self._managed_openclaw_json_files(), key=lambda item: str(item).casefold()
            ):
                try:
                    config = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    # Preserve malformed or unreadable recovery copies. They
                    # may be the only remaining copy and cannot be migrated
                    # without guessing at their structure.
                    continue
                if not isinstance(config, dict):
                    continue
                parsed.append((candidate, config))
                credentials.update(self._deepseek_provider_credentials(config))

            if len(credentials) > 1:
                # A single runtime environment variable cannot faithfully
                # represent conflicting active/recovery credentials. Leave all
                # source files untouched for an explicit user resolution.
                raise SecretStorageFormatError(
                    "Conflicting legacy OpenClaw DeepSeek credentials; migration stopped"
                )

            if credentials:
                if self._provider_secrets is None:
                    raise SecretStorageUnavailable(
                        "The provider credential vault is unavailable; OpenClaw migration stopped"
                    )
                credential = next(iter(credentials))
                updated = self._provider_secrets.update(
                    openclaw_deepseek_api_key=credential
                )
                if updated.openclaw_deepseek_api_key != credential:
                    raise SecretStorageFormatError(
                        "The OpenClaw provider credential was not verified in the vault"
                    )

            if self._provider_secrets is not None:
                runtime_key = self._provider_secrets.value.openclaw_deepseek_api_key
                if runtime_key:
                    self.provider_env["DEEPSEEK_API_KEY"] = runtime_key

            # Parsing and the verified vault transaction above complete before
            # the first plaintext rewrite. A failed rewrite keeps that file's
            # original bytes, while the already-vaulted key remains retryable.
            for candidate, config in parsed:
                self._scrub_parsed_managed_json_file(candidate, config)

    def _managed_openclaw_json_files(self) -> Iterator[Path]:
        """Yield a fixed-depth allowlist without recursively walking state."""

        try:
            state_root = self.state_dir.resolve()
        except (OSError, RuntimeError):
            return
        seen: set[Path] = set()
        for pattern in _OPENCLAW_MANAGED_PATH_PATTERNS:
            try:
                candidates = self.state_dir.glob(pattern)
                for candidate in candidates:
                    if candidate in seen or not candidate.is_file() or candidate.is_symlink():
                        continue
                    try:
                        candidate.resolve().relative_to(state_root)
                    except (OSError, RuntimeError, ValueError):
                        continue
                    seen.add(candidate)
                    yield candidate
            except OSError:
                # A concurrently removed agent/plugin directory is harmless;
                # the next startup or settings transaction retries the scan.
                continue

    def _scrub_managed_json_file(self, path: Path) -> None:
        """Scrub one allowlisted JSON file, retaining it on any failure."""

        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return
        if not isinstance(config, dict):
            return
        self._scrub_parsed_managed_json_file(path, config)

    def _scrub_parsed_managed_json_file(
        self, path: Path, config: dict[str, Any]
    ) -> None:
        """Scrub one already-parsed allowlisted file after vault verification."""

        changed = self._remove_deepseek_provider_credentials(config)
        if path.name.casefold().startswith("openclaw.json"):
            before = json.dumps(config, ensure_ascii=False, sort_keys=True)
            self._remove_managed_skill_plaintext(config)
            changed = changed or before != json.dumps(config, ensure_ascii=False, sort_keys=True)
        if not changed:
            return
        payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        # If the atomic write fails, the original file remains available for a
        # later retry; do not unlink or replace it before the write succeeds.
        atomic_write_secure(path, payload)

    @classmethod
    def _deepseek_provider_credentials(cls, config: dict[str, Any]) -> set[str]:
        """Return non-empty DeepSeek credentials without exposing their values."""

        credentials: set[str] = set()
        for provider in cls._deepseek_provider_entries(config):
            containers = [provider]
            auth = provider.get("auth")
            if isinstance(auth, dict):
                containers.append(auth)
            for container in containers:
                for field in _OPENCLAW_PROVIDER_CREDENTIAL_FIELDS:
                    if field not in container:
                        continue
                    value = container[field]
                    if value is None or value == "":
                        continue
                    if not isinstance(value, str):
                        raise SecretStorageFormatError(
                            "An OpenClaw DeepSeek credential has an invalid type"
                        )
                    normalized = value.strip()
                    if normalized:
                        credentials.add(normalized)
        return credentials

    @staticmethod
    def _deepseek_provider_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
        provider_maps: list[dict[str, Any]] = []
        providers = config.get("providers")
        if isinstance(providers, dict):
            provider_maps.append(providers)
        models = config.get("models")
        if isinstance(models, dict) and isinstance(models.get("providers"), dict):
            provider_maps.append(models["providers"])
        return [
            provider
            for provider_map in provider_maps
            for name, provider in provider_map.items()
            if str(name).casefold() == "deepseek" and isinstance(provider, dict)
        ]

    @classmethod
    def _remove_deepseek_provider_credentials(cls, config: dict[str, Any]) -> bool:
        """Remove only the credential now represented by the dedicated vault field."""

        changed = False
        for provider in cls._deepseek_provider_entries(config):
            containers = [provider]
            auth = provider.get("auth")
            if isinstance(auth, dict):
                containers.append(auth)
            for container in containers:
                for field in _OPENCLAW_PROVIDER_CREDENTIAL_FIELDS:
                    if field in container:
                        container.pop(field, None)
                        changed = True
        return changed

    def _cli_command(self, cli: str, *args: str) -> list[str]:
        expected = self.resolve_cli()
        try:
            matches_package_entry = bool(
                expected and Path(cli).resolve(strict=True) == Path(expected).resolve(strict=True)
            )
        except (OSError, RuntimeError):
            matches_package_entry = False
        if not matches_package_entry:
            raise RuntimeError("Refusing to execute an OpenClaw path that was not selected by Elren")
        suffix = Path(expected).suffix.lower()
        if suffix in {".cmd", ".bat"}:
            return ["cmd.exe", "/d", "/s", "/c", expected, *args]
        if suffix in {".mjs", ".js", ".cjs"}:
            node = self._package_node()
            if not node:
                raise RuntimeError("Node.js is required to run OpenClaw")
            return [node, expected, *args]
        return [expected, *args]

    @staticmethod
    def _count_tools(catalog: dict[str, Any]) -> int:
        if isinstance(catalog.get("tools"), list):
            return len(catalog["tools"])
        return sum(
            len(group.get("tools", []))
            for group in catalog.get("groups", [])
            if isinstance(group, dict)
        )
