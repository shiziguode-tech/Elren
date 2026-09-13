from __future__ import annotations

import locale
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from deepdesk.command_safety import SENSITIVE_PATTERNS
from deepdesk.plugins.base import run_owned_thread
from deepdesk.subprocess_env import credential_safe_environment

try:
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32process
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    pywintypes = win32api = win32con = win32event = None
    win32file = win32job = win32process = None


class WindowsJobSandbox:
    """Windows Job Object process sandbox with explicit, honest boundaries."""

    def __init__(self, workspace: Path, *, temporary_root: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        self.temp_dir = (temporary_root or self.workspace) / "work" / "sandbox"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        available = bool(os.name == "nt" and win32job and shutil.which("powershell.exe"))
        return {
            "available": available,
            "backend": "windows-job-object" if available else "unavailable",
            "os_enforced": available,
            "enforced": {
                "suspended_before_assignment": available,
                "kill_process_tree_on_close": available,
                "active_process_limit": available,
                "process_memory_limit": available,
                "per_process_cpu_time": available,
                "wall_clock_timeout": available,
                "credential_environment_filter": True,
            },
            "not_enforced": [
                "filesystem namespace isolation",
                "network namespace isolation",
                "Windows restricted token/AppContainer",
            ],
            "workspace": str(self.workspace),
        }

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
        cancelled = threading.Event()
        return await run_owned_thread(
            self._run_sync,
            command,
            min(max(timeout_seconds, 1), 120),
            min(max(memory_mb, 64), 2048),
            min(max(cpu_seconds, 1), 120),
            min(max(max_processes, 1), 16),
            dict(environment or {}),
            cancelled,
            on_cancel=cancelled.set,
        )

    def _run_sync(
        self,
        command: str,
        timeout_seconds: int,
        memory_mb: int,
        cpu_seconds: int,
        max_processes: int,
        extra_environment: dict[str, str],
        cancelled: threading.Event | None = None,
    ) -> dict[str, Any]:
        if not self.status()["available"]:
            raise RuntimeError("Windows Job Object sandbox is unavailable")
        cancelled = cancelled or threading.Event()
        if cancelled.is_set():
            return {"exit_code": -1, "cancelled": True, "timed_out": False}

        stdout_path = self._temp_path("stdout-")
        stderr_path = self._temp_path("stderr-")
        handles: list[Any] = []
        job = process_handle = thread_handle = None
        timed_out = False
        exit_code = -1
        try:
            security = pywintypes.SECURITY_ATTRIBUTES()
            security.bInheritHandle = True
            output_handle = win32file.CreateFile(
                str(stdout_path), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ,
                security, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
            )
            error_handle = win32file.CreateFile(
                str(stderr_path), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ,
                security, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
            )
            input_handle = win32file.CreateFile(
                "NUL", win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                security, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
            )
            handles.extend([output_handle, error_handle, input_handle])

            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32con.STARTF_USESTDHANDLES
            startup.hStdOutput = output_handle
            startup.hStdError = error_handle
            startup.hStdInput = input_handle

            executable = shutil.which("powershell.exe") or "powershell.exe"
            command_line = subprocess.list2cmdline(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    strict_powershell_command(command),
                ]
            )
            environment = self._clean_environment()
            environment.update(extra_environment)
            process_handle, thread_handle, process_id, _ = win32process.CreateProcess(
                executable,
                command_line,
                None,
                None,
                True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW,
                environment,
                str(self.workspace),
                startup,
            )

            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            basic = info["BasicLimitInformation"]
            basic["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | win32job.JOB_OBJECT_LIMIT_PROCESS_TIME
            )
            basic["ActiveProcessLimit"] = max_processes
            basic["PerProcessUserTimeLimit"] = cpu_seconds * 10_000_000
            info["ProcessMemoryLimit"] = memory_mb * 1024 * 1024
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info
            )
            win32job.AssignProcessToJobObject(job, process_handle)
            # Cancellation during process creation leaves the child suspended
            # until it belongs to this invocation's job. Never kill by name or
            # workspace prefix: only this owned Job is terminated.
            if not cancelled.is_set():
                win32process.ResumeThread(thread_handle)
            deadline = time.monotonic() + timeout_seconds
            while True:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    timed_out = not cancelled.is_set()
                    win32job.TerminateJobObject(job, 1460 if timed_out else 1223)
                    break
                if win32event.WaitForSingleObject(process_handle, 50) != win32con.WAIT_TIMEOUT:
                    break
            # A command may exit while its descendants still hold output files.
            # This run's Job owns those children too, for completion and Stop.
            win32job.TerminateJobObject(job, 1223 if cancelled.is_set() else 0)
            while win32job.QueryInformationJobObject(
                job, win32job.JobObjectBasicAccountingInformation
            )["ActiveProcesses"]:
                time.sleep(0.01)
            exit_code = win32process.GetExitCodeProcess(process_handle)
            return {
                "exit_code": exit_code,
                "timed_out": timed_out,
                "stdout": self._read_output(stdout_path, 50_000),
                "stderr": self._read_output(stderr_path, 20_000),
                "sandbox": {
                    "backend": "windows-job-object",
                    "pid": process_id,
                    "memory_mb": memory_mb,
                    "cpu_seconds": cpu_seconds,
                    "max_processes": max_processes,
                    "timeout_seconds": timeout_seconds,
                },
            }
        finally:
            # Also cover creation/assignment failures before ResumeThread.
            if job:
                job.Close()
            if process_handle and win32process.GetExitCodeProcess(process_handle) == 259:
                win32process.TerminateProcess(process_handle, 1223)
                win32event.WaitForSingleObject(process_handle, win32event.INFINITE)
            if thread_handle:
                thread_handle.Close()
            if process_handle:
                process_handle.Close()
            for handle in handles:
                handle.Close()
            # A just-exited Windows child can retain an inherited file handle
            # for a few milliseconds. Cleanup must never overwrite a valid
            # command result with WinError 32, especially when several sandbox
            # jobs finish concurrently.
            self._unlink_output(stdout_path)
            self._unlink_output(stderr_path)

    @staticmethod
    def _unlink_output(path: Path) -> None:
        for attempt in range(5):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.02 * (2**attempt))
        # The file is a bounded temporary log. Leaving one locked file for a
        # later cleanup is safer than turning a successful sandbox action into
        # an application-visible failure.

    @staticmethod
    def _read_output(path: Path, limit: int) -> str:
        data = path.read_bytes()[-limit:]
        return decode_windows_process_output(data)

    def _temp_path(self, prefix: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".log", dir=self.temp_dir)
        os.close(descriptor)
        return Path(name)

    @staticmethod
    def _clean_environment() -> dict[str, str]:
        return credential_safe_environment({"ELREN_SANDBOX": "windows-job-object"})


def strict_powershell_command(command: str) -> str:
    """Make PowerShell failures observable through the process exit code.

    Windows PowerShell normally reports exit code 0 for many non-terminating
    cmdlet/.NET errors.  That makes an agent tool envelope look successful even
    when stderr contains the real failure.  The wrapper preserves ordinary
    scripts while converting both PowerShell and native-command failures into a
    non-zero process result.
    """

    # Windows PowerShell 5.1 converts a native program's stderr into
    # ``NativeCommandError`` records when the caller writes ``2>&1``.  With a
    # global Stop preference, harmless warnings from otherwise successful
    # tools (pip, npm, ffmpeg, git, etc.) become terminating exceptions.  Run
    # the user block with Continue, then distinguish real PowerShell errors
    # from those native stderr transport records and preserve the native exit
    # code.  ``throw`` and explicitly terminating errors are still caught.
    return (
        "$ErrorActionPreference='Stop'; $global:LASTEXITCODE=0; "
        "try { "
        "$__elrenErrorCount=$Error.Count; "
        "$ErrorActionPreference='Continue'; "
        "& { " + str(command) + " }; "
        "$__elrenNativeExit=$global:LASTEXITCODE; "
        "$__elrenNewErrorCount=[Math]::Max(0,$Error.Count-$__elrenErrorCount); "
        "$__elrenRealErrors=@($Error | Select-Object -First $__elrenNewErrorCount | "
        "Where-Object { $_.FullyQualifiedErrorId -notmatch '^NativeCommandError(?:Message)?$' }); "
        "if ($__elrenNativeExit -ne 0) { exit $__elrenNativeExit }; "
        "if ($__elrenRealErrors.Count -gt 0) { exit 1 } "
        "} catch { Write-Error $_; exit 1 }"
    )


def decode_windows_process_output(data: bytes) -> str:
    """Decode redirected Windows process output without manufacturing mojibake.

    Windows PowerShell, legacy Office tools and native CLIs may use UTF-16,
    UTF-8, the active ANSI code page, an OEM code page, or a CJK code page in
    the same application session.  Prefer strict decoders and select the most
    plausible fallback instead of silently inserting U+FFFD into task/audit
    output.
    """

    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or data.count(b"\x00") > len(data) // 4:
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        pass

    encodings = [locale.getpreferredencoding(False), "mbcs", "gb18030", "cp936", "cp950"]
    candidates: list[str] = []
    for encoding in dict.fromkeys(item for item in encodings if item):
        try:
            candidates.append(data.decode(encoding, errors="strict"))
        except (LookupError, UnicodeDecodeError):
            continue
    if not candidates:
        return data.decode("utf-8", errors="replace")

    def score(text: str) -> tuple[float, int]:
        printable = sum(character.isprintable() or character in "\r\n\t" for character in text)
        controls = sum(
            ord(character) < 32 and character not in "\r\n\t" for character in text
        )
        cjk = sum("\u3400" <= character <= "\u9fff" for character in text)
        suspicious_latin = sum("\u00c0" <= character <= "\u00ff" for character in text)
        quality = printable - controls * 8 + cjk * 3 - suspicious_latin * 0.35
        return quality, cjk

    return max(candidates, key=score)
