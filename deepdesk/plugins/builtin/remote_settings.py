from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from deepdesk.models import Risk
from deepdesk.platform_paths import unicode_font_candidates
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.speech_transcription import SUPPORTED_SPEECH_LANGUAGES

SettingsCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CHANGE_PROPERTIES: dict[str, dict[str, Any]] = {
    "model": {"type": "string", "maxLength": 240},
    "cross_conversation_context": {"type": "boolean"},
    "max_output_tokens": {"type": ["integer", "null"], "minimum": 1024, "maximum": 384000},
    "request_timeout": {"type": "number", "minimum": 10, "maximum": 600},
    "voice_language": {
        "type": "string",
        "enum": ["auto", *SUPPORTED_SPEECH_LANGUAGES],
    },
    "voice_name": {"type": "string", "maxLength": 240},
    "voice_rate": {"type": "number", "minimum": 0.5, "maximum": 2.0},
    "voice_auto_speak": {"type": "boolean"},
    "voice_hands_free": {"type": "boolean"},
    "voice_auto_continue": {"type": "boolean"},
}


class RemoteSettingsTool(ToolPlugin):
    name = "update_settings"
    description = (
        "Modify supported non-sensitive Elren preferences when the CURRENT Feishu, Telegram, or web text "
        "message starts with // or ／／, OR when the host marks the CURRENT request as originating "
        "from App live voice or Telegram voice. Infer the requested values, then call once. The host "
        "rejects unprefixed ordinary text. A voice-transcription near miss must never be mapped to auto, "
        "default, off, or an empty value. If the tool returns confirmation_required, ask the user whether "
        "the top suggested canonical value is what they meant and stop; do not retry until their next answer. "
        "Credentials, provider keys, channel identifiers, custom system prompts, and discussion-team "
        "instructions are local-Settings-only "
        "and are deliberately absent from this tool. "
        "Return and mention the before/after PNG evidence and TXT audit log after a real change."
    )
    parameters = {
        "type": "object",
        "properties": {
            "changes": {
                "type": "object",
                "properties": CHANGE_PROPERTIES,
                "additionalProperties": False,
            }
        },
        "required": ["changes"],
        "additionalProperties": False,
    }

    def __init__(self, callback_ref: dict[str, SettingsCallback], outputs_dir: Path) -> None:
        self.callback_ref = callback_ref
        self.outputs_dir = outputs_dir

    def risk(self, arguments: dict[str, Any]) -> Risk:
        # The current-message prefix is the explicit authorization token.  The
        # host validates it again at execution time, so an additional approval
        # card would make channel-based settings changes impossible.
        return Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        fields = sorted(str(key) for key in (arguments.get("changes") or {}))
        return "Update Elren settings: " + ", ".join(fields)

    @staticmethod
    def _has_prefix(prompt: str) -> bool:
        # Accept every two-character half-width/full-width slash combination.
        return bool(re.match(r"^\s*(?:(?:/|\uFF0F){2})", str(prompt or "")))

    @staticmethod
    def _font(size: int):
        for candidate in unicode_font_candidates():
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), size=size)
        return ImageFont.load_default()

    @classmethod
    def _render_snapshot(
        cls, path: Path, title: str, changed: dict[str, dict[str, Any]], side: str
    ) -> None:
        width = 1200
        row_height = 76
        height = 180 + max(1, len(changed)) * row_height
        image = Image.new("RGB", (width, height), "#f4f7fb")
        draw = ImageDraw.Draw(image)
        title_font = cls._font(32)
        body_font = cls._font(22)
        small_font = cls._font(17)
        draw.rounded_rectangle((28, 24, width - 28, height - 24), radius=22, fill="#ffffff", outline="#cbd8e8", width=2)
        draw.text((62, 52), title, fill="#172333", font=title_font)
        draw.text((62, 102), "Elren · 脱敏设置证据", fill="#5b7089", font=small_font)
        y = 142
        for field, values in changed.items():
            draw.rounded_rectangle((56, y, width - 56, y + 60), radius=12, fill="#eef3f9")
            draw.text((76, y + 16), field, fill="#334b67", font=body_font)
            value = str(values.get(side, ""))
            draw.text((430, y + 16), value[:70], fill="#0b6b5d", font=body_font)
            y += row_height
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG", optimize=True)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        if context.source not in {"web", "feishu", "telegram"}:
            raise PermissionError("Remote settings updates are limited to web, Feishu, and Telegram tasks")
        if not context.voice_request and not self._has_prefix(context.user_prompt):
            raise PermissionError("Settings updates require the current message to begin with // or ／／")
        changes = dict(arguments.get("changes") or {})
        if not changes:
            raise ValueError("At least one setting change is required")
        unknown = sorted(set(changes) - set(CHANGE_PROPERTIES))
        if unknown:
            raise ValueError("Unsupported settings: " + ", ".join(unknown))
        callback = self.callback_ref.get("apply")
        if callback is None or not inspect.iscoroutinefunction(callback):
            raise RuntimeError("Settings update service is not ready")
        result = await callback(changes)
        if result.get("confirmation_required"):
            suggestions = list(result.get("suggestions") or [])
            top = suggestions[0] if suggestions else {}
            requested = str(result.get("requested") or "").strip()
            label = str(top.get("label") or "").strip()
            contains_chinese = bool(re.search(r"[\u3400-\u9fff]", context.user_prompt))
            if label and contains_chinese:
                direct_user_response = (
                    f"我将“{requested}”理解为 {label}。你想把默认模型改为 {label} 吗？"
                )
            elif label:
                direct_user_response = (
                    f'I understood "{requested}" as {label}. Do you want to change the default model to {label}?'
                )
            elif contains_chinese:
                direct_user_response = (
                    f"我没有准确识别“{requested}”对应的已配置模型。请再说一次准确的模型名称。"
                )
            else:
                direct_user_response = (
                    f'I could not match "{requested}" to a configured model. Please say the exact model name again.'
                )
            return {
                **result,
                "direct_user_response": direct_user_response,
                "instruction": (
                    "No setting was changed. The host will send direct_user_response verbatim and end "
                    "this turn. On the user's next CURRENT message, if they confirm, call update_settings "
                    "with the suggestion's exact selector. If they deny it, follow their corrected value. "
                    "Do not create screenshots or claim success yet."
                ),
            }
        changed = dict(result.get("changed") or {})
        if not changed:
            raise ValueError("No setting value changed")
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        output_dir = (self.outputs_dir / "settings-changes").resolve()
        before_path = output_dir / f"{stamp}-before.png"
        after_path = output_dir / f"{stamp}-after.png"
        log_path = output_dir / f"{stamp}-changes.txt"
        self._render_snapshot(before_path, "设置修改前 / Before", changed, "before")
        self._render_snapshot(after_path, "设置修改后 / After", changed, "after")
        lines = [
            "Elren settings change audit",
            f"UTC: {datetime.now(UTC).isoformat()}",
            f"Task ID: {context.task_id}",
            f"Source: {context.source}",
            "",
        ]
        for field, values in changed.items():
            lines.append(f"{field}: {values.get('before')} -> {values.get('after')}")
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {
            "updated": True,
            "changed": changed,
            "before_screenshot": str(before_path),
            "after_screenshot": str(after_path),
            "txt_log": str(log_path),
            "delivery_instruction": (
                "Explicitly tell the user each changed setting and include all three absolute paths "
                "in the final answer so the current channel delivers the evidence files."
            ),
        }
