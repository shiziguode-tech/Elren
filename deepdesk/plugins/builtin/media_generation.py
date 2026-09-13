from __future__ import annotations

from typing import Any

from deepdesk.media_generation import FreeMediaGenerator
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class MediaGenerationTool(ToolPlugin):
    name = "generate_media"
    description = (
        "Generate an image, video, or music file using a dynamically checked free-tier model. "
        "Use status to see current availability. Paid-only models and paid fallback are always "
        "blocked. Generated files are saved in outputs/; include the exact returned path in the "
        "final answer so the desktop UI, Feishu, and Telegram can deliver it. Automatic selection "
        "fails over across Hugging Face ZeroGPU, anonymous ModelScope Studios, and Pollinations "
        "models whose live catalog explicitly reports zero pricing. Shared free pools can still "
        "have queues or per-user quotas; never stop after the first provider fails."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "generate"]},
            "kind": {"type": "string", "enum": ["image", "video", "music"]},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
            "model": {"type": "string", "description": "Optional free-tier model name from status"},
            "width": {"type": "integer", "minimum": 256, "maximum": 2048},
            "height": {"type": "integer", "minimum": 256, "maximum": 2048},
            "duration": {"type": "integer", "minimum": 1, "maximum": 300},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
            "seed": {"type": "integer", "minimum": -1, "maximum": 2147483647},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, generator: FreeMediaGenerator) -> None:
        self.generator = generator

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") == "status" else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        if arguments.get("action") == "status":
            return "Check free image, video, and music model availability"
        return f"Generate free-tier {arguments.get('kind', 'media')}: {str(arguments.get('prompt') or '')[:120]}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = str(arguments.get("action") or "")
        if action == "status":
            return await self.generator.status(egress_country=context.egress_country)
        if action != "generate":
            raise ValueError("Unsupported media generation action")
        return await self.generator.generate(
            str(arguments.get("kind") or ""),
            str(arguments.get("prompt") or ""),
            model=str(arguments.get("model") or ""),
            width=int(arguments.get("width") or 1024),
            height=int(arguments.get("height") or 1024),
            duration=int(arguments.get("duration") or 6),
            aspect_ratio=str(arguments.get("aspect_ratio") or "16:9"),
            seed=int(arguments.get("seed", -1)),
            egress_country=context.egress_country,
        )
