from __future__ import annotations

from pathlib import Path
from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.telegram import TelegramBridge


class TelegramTool(ToolPlugin):
    name = "telegram"
    description = (
        "Use the optional Telegram bot channel. Check status or send plain text, images, "
        "documents, audio, and video to a Telegram chat. Incoming Telegram messages and "
        "attachments automatically become autonomous Agent tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "send_text", "send_file"]},
            "chat_id": {"type": "string", "description": "Telegram chat ID; omit to use the learned default"},
            "text": {"type": "string"},
            "path": {"type": "string", "description": "Local image/document/audio/video path"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, bridge: TelegramBridge) -> None:
        self.bridge = bridge

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") == "status" else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"Telegram：{arguments.get('action', '')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = str(arguments.get("action") or "")
        if action == "status":
            return await self.bridge.status(probe=True)
        chat_id = str(arguments.get("chat_id") or self.bridge.default_chat_id).strip()
        if not chat_id:
            raise ValueError("Telegram chat_id is not configured and has not been learned from an incoming message")
        if action == "send_text":
            return await self.bridge.send_text(chat_id, str(arguments.get("text") or ""))
        if action == "send_file":
            return await self.bridge.send_file(chat_id, Path(str(arguments.get("path") or "")))
        raise ValueError("Unsupported Telegram action")
