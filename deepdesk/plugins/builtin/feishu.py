from __future__ import annotations

from typing import Any

from deepdesk.feishu import FeishuBridge
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class FeishuTool(ToolPlugin):
    name = "feishu"
    description = (
        "Use the optional Feishu bot channel. Check status or send text, image, document, audio, "
        "or video files to a Feishu open_id/chat_id. Incoming Feishu bot messages and media are "
        "converted into Elren tasks."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "send_text", "send_image", "send_file"]},
            "receive_id": {"type": "string", "description": "Feishu open_id or chat_id; omit it to use the Open ID saved in Settings"},
            "receive_id_type": {"type": "string", "enum": ["open_id", "chat_id"]},
            "text": {"type": "string", "maxLength": 4000},
            "file_path": {"type": "string", "description": "Absolute local image/document/audio/video path"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, bridge: FeishuBridge) -> None:
        self.bridge = bridge

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") == "status" else Risk.HIGH

    def summarize(self, arguments: dict[str, Any]) -> str:
        if arguments.get("action") == "send_text":
            return f"通过飞书发送消息：{str(arguments.get('text') or '')[:120]}"
        if arguments.get("action") in {"send_image", "send_file"}:
            return f"通过飞书发送文件：{str(arguments.get('file_path') or '')[:160]}"
        return "检查飞书机器人连接状态"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments.get("action")
        if action == "status":
            return await self.bridge.status(probe=True)
        if action == "send_text":
            return await self.bridge.send_text(
                str(arguments.get("receive_id") or ""),
                str(arguments.get("text") or ""),
                receive_id_type=str(arguments.get("receive_id_type") or "open_id"),
            )
        if action == "send_image":
            return await self.bridge.send_image(
                str(arguments.get("receive_id") or ""),
                str(arguments.get("file_path") or ""),
                receive_id_type=str(arguments.get("receive_id_type") or "open_id"),
            )
        if action == "send_file":
            return await self.bridge.send_file(
                str(arguments.get("receive_id") or ""),
                str(arguments.get("file_path") or ""),
                receive_id_type=str(arguments.get("receive_id_type") or "open_id"),
            )
        raise ValueError("Unsupported Feishu action")
