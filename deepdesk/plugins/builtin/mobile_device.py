from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from deepdesk.mobile_bridge import MobileBridge
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin


class MobileDeviceTool(ToolPlugin):
    name = "mobile_device"
    description = (
        "Inspect and operate a user-paired Android companion device over an encrypted local-network "
        "session. Prefer observe for one-call screenshot plus semantic analysis; provide a focused question. Use list/status before actions. To answer which apps are installed, always call "
        "list_apps first: it reads Android's user-visible launcher catalog directly, defaults to "
        "non-system apps, "
        "and avoids opening Settings or scrolling an app list. For reading any other long phone page "
        "or list, always use "
        "the semantic scroll action instead of guessing swipe coordinates: it sizes movement from the "
        "actual scroll container, waits for inertial motion to settle, and returns overlap/new-item "
        "metadata so content is neither skipped nor counted twice. Omit overlap_ratio for normal "
        "reading so the phone can adapt the next movement from observed overlap; provide it only "
        "when the user explicitly requests a particular overlap. Reserve swipe for non-scrolling "
        "gestures. In autonomous approval mode, ordinary actions do not "
        "need Elren approval, but Android system permission screens, credentials, CAPTCHA, biometrics, "
        "payments, security settings, and app installation remain user-controlled and cannot be bypassed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status", "list", "list_apps", "inspect", "screenshot", "observe", "tap", "scroll",
                    "swipe", "type", "back", "home", "recents", "launch", "open_url",
                ],
            },
            "device_id": {"type": "string", "maxLength": 128},
            "x": {"type": "integer", "minimum": 0},
            "y": {"type": "integer", "minimum": 0},
            "end_x": {"type": "integer", "minimum": 0},
            "end_y": {"type": "integer", "minimum": 0},
            "duration_ms": {"type": "integer", "minimum": 50, "maximum": 5000},
            "direction": {"type": "string", "enum": ["down", "up"]},
            "overlap_ratio": {"type": "number", "minimum": 0.25, "maximum": 0.75},
            "question": {"type": "string", "maxLength": 4000},
            "text": {"type": "string", "maxLength": 20000},
            "package": {"type": "string", "maxLength": 240},
            "url": {"type": "string", "maxLength": 4000},
            "include_system": {"type": "boolean"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, bridge: MobileBridge, screenshot_dir: Path, vision: ToolPlugin | None = None) -> None:
        self.bridge = bridge
        self.screenshot_dir = screenshot_dir
        self.vision = vision

    def risk(self, arguments: dict[str, Any]) -> Risk:
        action = str(arguments.get("action") or "")
        if action in {"status", "list", "inspect"}:
            return Risk.SAFE
        if action in {"screenshot", "observe", "list_apps"}:
            return Risk.MEDIUM
        return Risk.HIGH

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"Android · {arguments.get('action', 'status')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = str(arguments.get("action") or "status")
        if action in {"status", "list"}:
            return self.bridge.status()
        device_id = str(arguments.get("device_id") or "")
        command_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"action", "device_id", "question"}
        }
        result = await self.bridge.command(
            "screenshot" if action == "observe" else action,
            command_arguments,
            device_id=device_id,
            autonomous=context.approval_policy == "autonomous",
        )
        if action in {"screenshot", "observe"} and result.get("image_base64"):
            raw = base64.b64decode(str(result.pop("image_base64")), validate=True)
            if not raw or len(raw) > 12 * 1024 * 1024:
                raise ValueError("Android screenshot is empty or exceeds 12 MiB")
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"android-{int(time.time() * 1000)}.png"
            path.write_bytes(raw)
            result["path"] = str(path)
            if action == "observe":
                if self.vision is None:
                    result["analysis_error"] = "Semantic analysis is unavailable; use vision on this saved screenshot"
                else:
                    context.authorized_read_paths = tuple(dict.fromkeys([*context.authorized_read_paths, str(path.resolve())]))
                    try:
                        result["analysis"] = await self.vision.execute({
                            "image_path": str(path), "mode": "semantic",
                            "question": arguments.get("question") or "Describe the current app, visible controls with pixel coordinates, and any errors. Do not infer unseen results.",
                        }, context)
                    except Exception as exc:
                        result["analysis_error"] = f"{type(exc).__name__}: {exc}"
        return result
