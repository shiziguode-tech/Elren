from __future__ import annotations

from typing import Any

from deepdesk.mcp_runtime import MCPRuntime
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class MCPTool(ToolPlugin):
    name = "mcp"
    description = (
        "Use the real Elren stdio MCP server. Probe the standard MCP handshake, list tools, "
        "or call an MCP tool. write_artifact is approval-protected and confined to outputs/."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "list_tools", "call"]},
            "tool": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, runtime: MCPRuntime) -> None:
        self.runtime = runtime

    def risk(self, arguments: dict[str, Any]) -> Risk:
        if arguments.get("action") != "call":
            return Risk.SAFE
        tool = arguments.get("tool", "")
        return Risk.HIGH if tool == "write_artifact" else Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        if arguments.get("action") == "call":
            return f"通过 MCP 调用：{arguments.get('tool', '')}"
        return f"检查 MCP：{arguments.get('action')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "status":
            return await self.runtime.status(probe=True)
        if action == "list_tools":
            return await self.runtime.list_tools()
        if action == "call":
            tool = arguments.get("tool")
            if not tool:
                raise ValueError("tool is required for call")
            return await self.runtime.call_tool(tool, arguments.get("arguments", {}))
        raise ValueError(f"Unsupported MCP action: {action}")
