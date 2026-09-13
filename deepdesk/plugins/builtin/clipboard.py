from __future__ import annotations

from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread


class ClipboardTool(ToolPlugin):
    name = "clipboard"
    description = (
        "Read or write the system clipboard on Windows or macOS. Reading may expose private data and always requires "
        "high-risk approval outside autonomous mode."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "clear"]},
            "text": {"type": "string", "description": "Text used by write"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.HIGH if arguments.get("action") == "read" else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "read":
            return "读取系统剪贴板内容（可能包含隐私数据）"
        if action == "write":
            return f"写入系统剪贴板：{arguments.get('text', '')[:100]}"
        return "清空系统剪贴板"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments)

    @staticmethod
    def _execute_sync(arguments: dict[str, Any]) -> Any:
        import pyperclip

        action = arguments["action"]
        if action == "read":
            value = pyperclip.paste()
            return {"text": value[:100_000], "truncated": len(value) > 100_000}
        if action == "write":
            pyperclip.copy(arguments.get("text", ""))
            return {"ok": True, "characters": len(arguments.get("text", ""))}
        if action == "clear":
            pyperclip.copy("")
            return {"ok": True}
        raise ValueError(f"Unsupported clipboard action: {action}")
