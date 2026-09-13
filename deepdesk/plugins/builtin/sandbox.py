from __future__ import annotations

import os
from typing import Any, Protocol

from deepdesk.command_safety import (
    DANGEROUS_PATTERNS,
    OS_PERSISTENCE_PATTERNS,
    READ_ONLY_PREFIXES,
    SENSITIVE_PATTERNS,
    SHELL_DELETE_PATTERNS,
    SHELL_PROCESS_TERMINATION_PATTERNS,
    allows_permanent_delete,
    binds_reserved_service_port,
)
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class ResourceSandbox(Protocol):
    def status(self) -> dict[str, Any]: ...
    async def run(self, command: str, **kwargs: Any) -> dict[str, Any]: ...


class SandboxTool(ToolPlugin):
    name = "sandbox"
    description = (
        "Run a shell with platform-enforced process-tree, memory, CPU and timeout limits. "
        "This does not provide filesystem or network namespace isolation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "run"]},
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            "memory_mb": {"type": "integer", "minimum": 64, "maximum": 2048},
            "cpu_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            "max_processes": {"type": "integer", "minimum": 1, "maximum": 16},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, sandbox: ResourceSandbox) -> None:
        self.sandbox = sandbox

    def risk(self, arguments: dict[str, Any]) -> Risk:
        if arguments.get("action") == "status":
            return Risk.SAFE
        command = arguments.get("command", "").strip()
        if (
            SENSITIVE_PATTERNS.search(command)
            or DANGEROUS_PATTERNS.search(command)
            or OS_PERSISTENCE_PATTERNS.search(command)
        ):
            return Risk.HIGH
        if any(command.lower().startswith(prefix) for prefix in READ_ONLY_PREFIXES):
            return Risk.SAFE
        return Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        return "检查 Windows 沙箱" if arguments.get("action") == "status" else "在 Windows Job Object 中运行命令"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        if arguments["action"] == "status":
            return self.sandbox.status()
        command = arguments.get("command", "")
        if not command:
            raise ValueError("command is required")
        if SHELL_PROCESS_TERMINATION_PATTERNS.search(command):
            raise PermissionError(
                "Raw sandbox process termination is blocked because it can stop Elren itself. "
                "Use process_manager with an exact PID."
            )
        reserved_port = int(os.getenv("ELREN_PORT", "8765") or "8765")
        if binds_reserved_service_port(command, {reserved_port}):
            raise PermissionError(
                f"Port {reserved_port} is reserved by Elren's local service. "
                "Use a different free port."
            )
        if OS_PERSISTENCE_PATTERNS.search(command):
            raise PermissionError(
                "OS-level persistence is blocked. Use Elren's audited scheduler tool instead."
            )
        if SHELL_DELETE_PATTERNS.search(command) and not allows_permanent_delete(
            context.user_prompt
        ):
            raise PermissionError(
                "Raw sandbox deletion is blocked. Use filesystem.delete to move the item to the "
                "Windows recycle bin. Permanent deletion requires an explicit request in the "
                "current user message."
            )
        return await self.sandbox.run(
            command,
            timeout_seconds=arguments.get("timeout_seconds", 30),
            memory_mb=arguments.get("memory_mb", 256),
            cpu_seconds=arguments.get("cpu_seconds", 20),
            max_processes=arguments.get("max_processes", 4),
        )
