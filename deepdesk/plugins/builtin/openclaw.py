from __future__ import annotations

from typing import Any

from deepdesk.models import Risk
from deepdesk.openclaw_bridge import OpenClawBridge
from deepdesk.plugins.base import ToolContext, ToolPlugin


class OpenClawBridgeTool(ToolPlugin):
    name = "openclaw"
    description = (
        "Bridge to an official OpenClaw runtime. Inspect status/catalog, invoke a dynamically "
        "discovered OpenClaw tool, or delegate a complete task to OpenClaw agent exec. "
        "Call catalog before invoke. External plugins and channels require their own configuration."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "catalog", "plugins", "invoke", "agent_exec"],
            },
            "tool": {"type": "string", "description": "OpenClaw tool name for invoke"},
            "args": {"type": "object", "description": "Arguments for the OpenClaw tool"},
            "session_key": {"type": "string"},
            "prompt": {"type": "string", "description": "Complete delegated task for agent_exec"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, bridge: OpenClawBridge) -> None:
        self.bridge = bridge

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return (
            Risk.SAFE if arguments.get("action") in {"status", "catalog", "plugins"} else Risk.HIGH
        )

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "invoke":
            return f"通过 OpenClaw Gateway 调用工具：{arguments.get('tool')}"
        if action == "agent_exec":
            return f"把完整任务委派给 OpenClaw：{arguments.get('prompt', '')[:140]}"
        return f"检查 OpenClaw：{action}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "status":
            return await self.bridge.status(probe_catalog=False)
        if action == "catalog":
            return await self.bridge.catalog()
        if action == "plugins":
            return await self.bridge.plugins()
        if action == "invoke":
            tool = arguments.get("tool")
            if not tool:
                raise ValueError("tool is required for invoke")
            return await self.bridge.invoke(
                tool, arguments.get("args", {}), arguments.get("session_key", "main")
            )
        if action == "agent_exec":
            prompt = arguments.get("prompt")
            if not prompt:
                raise ValueError("prompt is required for agent_exec")
            return await self.bridge.agent_exec(prompt)
        raise ValueError(f"Unsupported OpenClaw action: {action}")
