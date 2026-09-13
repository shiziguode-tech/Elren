from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepdesk.command_safety import (
    DANGEROUS_PATTERNS,
    OS_PERSISTENCE_PATTERNS,
    READ_ONLY_PREFIXES,
    SHELL_DELETE_PATTERNS,
    SHELL_PROCESS_TERMINATION_PATTERNS,
    accesses_sensitive_environment,
    allows_permanent_delete,
    binds_reserved_service_port,
)
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, finish_owned_work
from deepdesk.posix_sandbox import PosixResourceSandbox
from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.windows_sandbox import WindowsJobSandbox, strict_powershell_command


class ShellTool(ToolPlugin):
    name = "shell"
    description = (
        "Run a command in the configured workspace: Windows PowerShell 5.1 (powershell.exe) on Windows, zsh on macOS. "
        "Windows PowerShell 5.1 does not support && or || command chaining. Use separate shell calls and inspect each "
        "exit_code, or explicitly guard a dependent native command with if ($LASTEXITCODE -eq 0) { ... }. "
        "Do not replace conditional chaining with an unconditional semicolon. Prefer direct filesystem or UI tools. "
        "Commands time out and output is truncated. Inspect ok, exit_code, stderr and timed_out; "
        "empty stdout is not success evidence. Commands already start in the configured workspace: "
        "prefer workspace-relative paths so generated scripts remain portable, and quote paths that contain spaces. "
        "Raw process termination is blocked; use process_manager with an exact PID. Preview servers must use a free "
        "port other than Elren's reserved local service port. "
        "Only after every practical portable path strategy has failed may an Agent disclose the exact limitation and "
        "continue with one minimal, centralized absolute-path fallback without pausing for approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Windows: PowerShell 5.1 syntax, not Bash or PowerShell 7; && and || are unsupported. macOS: zsh syntax.",
            },
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            "skill": {
                "type": "string",
                "description": "Optional exact skill name; injects only that skill's allowlisted credentials",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        sandbox: Any | None = None,
        credential_environment: Callable[[str], dict[str, str]] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.credential_environment = credential_environment or (lambda _skill: {})

    def risk(self, arguments: dict[str, Any]) -> Risk:
        command = arguments.get("command", "").strip()
        lowered = command.lower()
        skill_environment = self.credential_environment(arguments.get("skill", ""))
        if accesses_sensitive_environment(command, set(skill_environment)):
            return Risk.HIGH
        if DANGEROUS_PATTERNS.search(command):
            return Risk.HIGH
        if any(lowered.startswith(prefix) for prefix in READ_ONLY_PREFIXES):
            return Risk.SAFE
        return Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command", "")
        shell = "PowerShell" if os.name == "nt" else "zsh"
        return f"运行 {shell}：{command[:240]}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        if SHELL_PROCESS_TERMINATION_PATTERNS.search(arguments["command"]):
            raise PermissionError(
                "Raw shell process termination is blocked because it can stop Elren itself. "
                "Use process_manager with an exact PID; it protects the host process tree."
            )
        reserved_port = int(os.getenv("ELREN_PORT", "8765") or "8765")
        if binds_reserved_service_port(arguments["command"], {reserved_port}):
            raise PermissionError(
                f"Port {reserved_port} is reserved by Elren's local service. "
                "Use a different free port for preview servers."
            )
        if OS_PERSISTENCE_PATTERNS.search(arguments["command"]):
            raise PermissionError(
                "OS-level persistence is blocked. Use Elren's audited scheduler tool instead."
            )
        skill_environment = self.credential_environment(arguments.get("skill", ""))
        if accesses_sensitive_environment(arguments["command"], set(skill_environment)):
            raise PermissionError(
                "Commands that access credentials or process environment are blocked"
            )
        if SHELL_DELETE_PATTERNS.search(arguments["command"]) and not allows_permanent_delete(
            context.user_prompt
        ):
            raise PermissionError(
                "Raw shell deletion is blocked. Use filesystem.delete to move the item to the "
                "system trash/recycle bin. A permanent shell deletion is allowed only when the current "
                "user message explicitly requests permanent or irreversible deletion."
            )
        timeout = min(max(int(arguments.get("timeout_seconds", 30)), 1), 120)
        runner = self.sandbox
        if runner is not None and hasattr(runner, "workspace") and Path(runner.workspace).resolve() != Path(context.workspace).resolve():
            # Never mutate the shared runner: two tasks may use different roots concurrently.
            runner = None
        runner = runner or (
            WindowsJobSandbox(Path(context.workspace), temporary_root=Path(context.application_workspace or context.workspace))
            if os.name == "nt"
            else PosixResourceSandbox(Path(context.workspace))
        )
        if runner.status()["available"]:
            result = await runner.run(
                arguments["command"],
                timeout_seconds=timeout,
                memory_mb=1024,
                cpu_seconds=timeout,
                max_processes=8,
                environment=skill_environment,
            )
            result["working_directory"] = str(Path(context.workspace).resolve())
            result["process_tree_terminated_on_timeout"] = True
            if (
                os.name == "nt"
                and result.get("exit_code") not in {None, 0}
                and "InvalidEndOfLine" in str(result.get("stderr") or "")
                and any(operator in arguments["command"] for operator in ("&&", "||"))
            ):
                result["error_code"] = "unsupported_powershell_chain_operator"
                result["recovery_hint"] = (
                    "Windows PowerShell 5.1 cannot parse && or ||. The command was not rewritten. "
                    "Issue separate shell calls and check each exit_code before continuing, or write explicit "
                    "PowerShell conditional logic (for native programs: if ($LASTEXITCODE -eq 0) { ... }). "
                    "Do not blindly replace conditional operators with semicolons or rerun the unchanged command."
                )
            return result
        if os.name == "nt":
            # Without a Job there is no race-free ownership of grandchildren.
            # A parent-only kill can falsely report Stop while children write;
            # refuse this uncontained fallback instead of making that promise.
            raise RuntimeError("Windows Job Object sandbox is required for safe command cancellation")
        command = (
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                strict_powershell_command(arguments["command"]),
            ]
            if os.name == "nt"
            else ["/bin/zsh", "-f", "-c", arguments["command"]]
        )
        creation = asyncio.create_task(asyncio.create_subprocess_exec(
            *command,
            cwd=context.workspace,
            env=credential_safe_environment(skill_environment),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        ))
        process = None
        try:
            process = await asyncio.shield(creation)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await finish_owned_work(asyncio.create_task(process.communicate()))
            return {"exit_code": -1, "error": f"Timed out after {timeout}s"}
        except asyncio.CancelledError:
            if process is None:
                process = await finish_owned_work(creation)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await finish_owned_work(asyncio.create_task(process.communicate()))
            raise
        out = stdout.decode("utf-8", errors="replace")[-50_000:]
        err = stderr.decode("utf-8", errors="replace")[-20_000:]
        return {
            "exit_code": process.returncode,
            "stdout": out,
            "stderr": err,
            "working_directory": str(Path(context.workspace).resolve()),
        }
