from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread


class ComputerUseAgentTool(ToolPlugin):
    name = "computer_use"
    description = (
        "High-level Computer Use observer for Windows. It combines visible-window discovery, "
        "UI Automation control inspection, screenshot capture, and window focus. Use it to begin "
        "and verify desktop tasks before lower-level computer or windows_ui actions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["observe", "inspect", "screenshot", "click"]},
            "window": {"type": "string", "description": "Regex matching the target window title"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 8},
            "x": {"type": "integer", "minimum": 0, "description": "Window-relative X coordinate"},
            "y": {"type": "integer", "minimum": 0, "description": "Window-relative Y coordinate"},
            "allow_user_input_interference": {
                "type": "boolean",
                "description": (
                    "Required true for coordinate click. The click moves the user's real mouse "
                    "and may interrupt foreground input. Prefer windows_ui background Invoke."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, screenshot_dir: Path) -> None:
        self.screenshot_dir = screenshot_dir

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.HIGH if arguments.get("action") == "click" else Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        labels = {
            "observe": "观察当前桌面状态",
            "inspect": "检查目标窗口",
            "screenshot": "截取目标窗口",
            "click": "点击目标窗口坐标",
        }
        return labels.get(str(action), f"Computer Use：{action}")

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments)

    def _execute_sync(self, arguments: dict[str, Any]) -> Any:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        action = arguments["action"]
        if action == "screenshot" and not arguments.get("window"):
            import pyautogui

            path = self.screenshot_dir / f"computer-use-{int(time.time() * 1000)}.png"
            image = pyautogui.screenshot()
            image.save(path)
            return {
                "screenshot": str(path),
                "size": [image.width, image.height],
                "coordinate_space": "screen",
            }
        if action == "observe":
            windows = []
            for window in desktop.windows():
                try:
                    title = window.window_text().strip()
                    if title and window.is_visible():
                        rect = window.rectangle()
                        windows.append(
                            {
                                "title": title,
                                "handle": window.handle,
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                            }
                        )
                except Exception:
                    continue
            return {"visible_windows": windows[:150]}
        window_pattern = arguments.get("window")
        if not window_pattern:
            raise ValueError("window is required for inspect/window screenshot/click")
        window = desktop.window(title_re=window_pattern)
        window.wait("exists visible", timeout=8)
        if action == "screenshot":
            rect = window.rectangle()
            width = max(1, rect.right - rect.left)
            height = max(1, rect.bottom - rect.top)
            path = self.screenshot_dir / f"computer-use-{int(time.time() * 1000)}.png"
            image = window.capture_as_image()
            image.save(path)
            return {
                "screenshot": str(path),
                "size": [image.width, image.height],
                "window": window.window_text(),
                "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                "coordinate_space": "window",
                "foreground_used": False,
            }
        if action == "click":
            from deepdesk.windows_text import click_at

            if "x" not in arguments or "y" not in arguments:
                raise ValueError("x and y are required for click")
            if arguments.get("allow_user_input_interference") is not True:
                raise PermissionError(
                    "Coordinate click uses the user's real mouse. Set "
                    "allow_user_input_interference=true only with explicit user authorization, "
                    "or use windows_ui for background Invoke."
                )
            rect = window.rectangle()
            local_x, local_y = int(arguments["x"]), int(arguments["y"])
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if not (0 <= local_x < width and 0 <= local_y < height):
                raise ValueError(f"Window-relative coordinate outside bounds {width}x{height}")
            global_x, global_y = rect.left + local_x, rect.top + local_y
            if not window.has_focus():
                raise RuntimeError(
                    "Coordinate click was blocked because the target is not foreground. "
                    "Use windows_ui for background Invoke, or request human takeover."
                )
            click_at(global_x, global_y)
            return {
                "clicked": True,
                "window": window.window_text(),
                "window_coordinates": [local_x, local_y],
                "screen_coordinates": [global_x, global_y],
                "foreground_used": True,
            }
        if action == "inspect":
            controls = []
            for control in window.descendants(depth=int(arguments.get("depth", 4))):
                try:
                    title = control.window_text().strip()
                    info = control.element_info
                    if title or info.control_type in {"Button", "Edit", "MenuItem", "ListItem"}:
                        rect = control.rectangle()
                        controls.append(
                            {
                                "title": title,
                                "type": info.control_type,
                                "automation_id": info.automation_id,
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                            }
                        )
                except Exception:
                    continue
            return {"window": window.window_text(), "controls": controls[:500]}
        raise ValueError(f"Unsupported computer_use action: {action}")
