from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import psutil

from deepdesk.command_safety import SENSITIVE_PATTERNS
from deepdesk.plugins.base import finish_owned_work
from deepdesk.subprocess_env import credential_safe_environment

try:
    import resource
except ImportError:  # pragma: no cover - resource is a POSIX-only standard module
    resource = None


class PosixResourceSandbox:
    """Run zsh with process-group and POSIX resource limits on macOS.

    This is deliberate containment, not a filesystem or network namespace.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def status(self) -> dict[str, Any]:
        available = os.name == "posix" and resource is not None
        darwin = sys.platform == "darwin"
        return {
            "available": available,
            "backend": "macos-posix-resource-limits" if available else "unavailable",
            "os_enforced": available,
            "enforced": {
                "new_process_group": available,
                "kill_process_tree_on_timeout": available,
                "process_limit": available and not darwin,
                "address_space_limit": available and not darwin,
                "cpu_time_limit": available,
                "wall_clock_timeout": available,
                "credential_environment_filter": True,
            },
            "monitored": {"process_group_rss_and_count": available and darwin},
            "not_enforced": ["filesystem namespace isolation", "network namespace isolation"],
            "workspace": str(self.workspace),
        }

    @staticmethod
    def _group_usage(group: int) -> tuple[int, int]:
        """Count only the owned process group, including reparented descendants."""
        count, rss = 0, 0
        for process in psutil.process_iter():
            try:
                if os.getpgid(process.pid) == group:
                    count += 1
                    rss += process.memory_info().rss
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
        return count, rss

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: int = 30,
        memory_mb: int = 256,
        cpu_seconds: int = 20,
        max_processes: int = 4,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if SENSITIVE_PATTERNS.search(command):
            raise PermissionError("Commands that access credentials or process environment are blocked")
        timeout_seconds = min(max(timeout_seconds, 1), 120)
        memory_mb = min(max(memory_mb, 64), 2048)
        cpu_seconds = min(max(cpu_seconds, 1), 120)
        max_processes = min(max(max_processes, 1), 16)

        def limits() -> None:
            if resource is None:
                raise RuntimeError("POSIX resource limits are unavailable")
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
            # Darwin rejects finite RLIMIT_AS on current Apple silicon systems.
            # RLIMIT_NPROC is per user, not per task; a small value also prevents
            # ordinary shells from spawning on a user's already-running desktop.
            if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
                memory = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            if sys.platform != "darwin" and hasattr(resource, "RLIMIT_NPROC"):
                resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))

        environment = credential_safe_environment(
            {
                "ELREN_SANDBOX": "macos-posix-resource-limits",
                **dict(environment or {}),
            }
        )
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            "/bin/zsh",
            "-f",
            "-c",
            command,
            cwd=self.workspace,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=limits,
        ))
        process = None
        timed_out = False
        limit_exceeded = ""
        monitor = None

        async def monitor_group() -> None:
            nonlocal limit_exceeded
            while process.returncode is None:
                count, rss = await asyncio.to_thread(self._group_usage, process.pid)
                if process.returncode is not None:
                    return
                if count > max_processes or rss > memory_mb * 1024 * 1024:
                    limit_exceeded = "process_count" if count > max_processes else "memory_rss"
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return
                await asyncio.sleep(0.1)

        try:
            process = await asyncio.shield(creation)
            if sys.platform == "darwin":
                monitor = asyncio.create_task(monitor_group())
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await finish_owned_work(asyncio.create_task(process.communicate()))
        except asyncio.CancelledError:
            # Shield creation so cancellation cannot lose the process handle
            # between fork/exec and asyncio returning its Process object.
            if process is None:
                process = await finish_owned_work(creation)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await finish_owned_work(asyncio.create_task(process.communicate()))
            raise
        finally:
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
        return {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "limit_exceeded": limit_exceeded,
            "stdout": stdout.decode("utf-8", errors="replace")[-50_000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-20_000:],
            "sandbox": {
                "backend": "macos-posix-resource-limits",
                "pid": process.pid,
                "memory_mb": memory_mb,
                "cpu_seconds": cpu_seconds,
                "max_processes": max_processes,
                "timeout_seconds": timeout_seconds,
            },
        }
