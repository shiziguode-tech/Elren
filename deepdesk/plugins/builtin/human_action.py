from __future__ import annotations

from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class HumanActionTool(ToolPlugin):
    name = "request_human_action"
    description = (
        "Pause the current task and ask the user to take over the target window for an action "
        "that must be completed by a human, such as CAPTCHA, bot verification, QR scan, MFA, "
        "credential entry, consent, or a physical action. Never attempt to bypass CAPTCHA or "
        "anti-bot checks. The same task resumes after the user confirms completion."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short reason why human action is required",
            },
            "instructions": {
                "type": "string",
                "description": "Exact steps the user should complete before returning",
            },
            "target_window": {
                "type": "string",
                "description": "Distinctive title text of the window to focus for takeover",
            },
            "target_app": {
                "type": "string",
                "description": "Application or browser name the user must operate",
            },
            "target_page": {
                "type": "string",
                "description": "Exact page, dialog, account area, or screen the user must operate",
            },
        },
        "required": ["summary", "instructions"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"等待用户操作：{arguments.get('summary', '需要人工接管')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        raise RuntimeError("request_human_action must be handled by the Agent host")
