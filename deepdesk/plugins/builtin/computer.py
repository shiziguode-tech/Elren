from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from deepdesk.command_safety import allows_permanent_delete
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread
from deepdesk.subprocess_env import credential_safe_environment

if sys.platform == "win32":
    from ctypes import wintypes

    from deepdesk.windows_text import type_unicode as _type_unicode_windows


def _foreground_window_state() -> dict[str, Any]:
    if sys.platform == "darwin":
        script = '''
tell application "System Events"
  set frontProcess to first application process whose frontmost is true
  set appName to name of frontProcess
  set windowName to ""
  try
    set windowName to name of front window of frontProcess
  end try
  return appName & linefeed & windowName
end tell
'''
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=credential_safe_environment(),
        )
        if result.returncode:
            raise RuntimeError(
                "Unable to inspect the focused macOS window. Verify Automation and Accessibility permissions."
            )
        parts = result.stdout.rstrip().splitlines()
        if not parts or not parts[0].strip():
            raise RuntimeError("Unable to identify the focused macOS application")
        return {
            "application": parts[0] if parts else "",
            "window_title": "\n".join(parts[1:]),
            "process_id": 0,
        }
    if sys.platform != "win32":
        return {"window_title": "", "process_id": 0}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handle = int(user32.GetForegroundWindow())
    length = int(user32.GetWindowTextLengthW(handle)) if handle else 0
    buffer = ctypes.create_unicode_buffer(length + 1)
    if handle:
        user32.GetWindowTextW(handle, buffer, len(buffer))
    process_id = wintypes.DWORD()
    if handle:
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
    return {
        "window_handle": handle,
        "window_title": buffer.value,
        "process_id": int(process_id.value),
    }


def type_unicode(text: str, interval: float = 0.001) -> None:
    if sys.platform == "win32":
        _type_unicode_windows(text, interval=interval)
        return
    import pyautogui
    import pyperclip

    previous = pyperclip.paste()
    try:
        pyperclip.copy(text)
        pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
    finally:
        time.sleep(0.08)
        pyperclip.copy(previous)


class ComputerTool(ToolPlugin):
    name = "computer"
    description = (
        "Control mouse and keyboard by screen coordinates, or take a screenshot. This is a fallback: "
        "prefer the platform accessibility tool and use vision to inspect screenshots. Text input "
        "uses literal Unicode and is independent of the active input method."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["screenshot", "position", "click", "move", "type", "hotkey", "scroll"],
            },
            "x": {"type": "integer", "minimum": 0},
            "y": {"type": "integer", "minimum": 0},
            "text": {"type": "string"},
            "keys": {"type": "array", "items": {"type": "string"}},
            "amount": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "allow_user_input_interference": {
                "type": "boolean",
                "description": "Explicitly permit foreground mouse/keyboard interference; default false",
            },
            "target_window": {
                "type": "string",
                "description": "Required for close shortcuts: title text just observed on the intended foreground page/window",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, screenshot_dir: Path) -> None:
        self.screenshot_dir = screenshot_dir

    def risk(self, arguments: dict[str, Any]) -> Risk:
        if arguments.get("action") in {"screenshot", "position"}:
            return Risk.SAFE
        return Risk.HIGH if arguments.get("allow_user_input_interference") else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "type":
            return f"向当前窗口输入文本：{arguments.get('text', '')[:120]}"
        return f"执行桌面操作：{action}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments, context)

    def _execute_sync(
        self, arguments: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        action = arguments["action"]
        if action == "screenshot":
            path = self.screenshot_dir / f"{int(time.time() * 1000)}.png"
            image = pyautogui.screenshot()
            image.save(path)
            return {
                "path": str(path),
                "width": image.width,
                "height": image.height,
                "note": f"Use vision for pixels and {'macos_ui' if sys.platform == 'darwin' else 'windows_ui'} for accessible controls.",
            }
        if action == "position":
            point = pyautogui.position()
            size = pyautogui.size()
            return {
                "x": point.x,
                "y": point.y,
                "screen_width": size.width,
                "screen_height": size.height,
            }
        if not arguments.get("allow_user_input_interference", False):
            raise PermissionError(
                "Foreground mouse/keyboard action blocked to avoid stealing the user's focus or input. "
                f"Use {'macos_ui' if sys.platform == 'darwin' else 'windows_ui'} accessibility controls. "
                "Only set allow_user_input_interference when the "
                "user explicitly requested visible foreground automation."
            )
        if action == "click":
            if sys.platform == "win32":
                from deepdesk.windows_text import click_at
                click_at(arguments["x"], arguments["y"], button=arguments.get("button", "left"))
            else:
                pyautogui.click(arguments["x"], arguments["y"], button=arguments.get("button", "left"))
        elif action == "move":
            if sys.platform == "win32":
                from deepdesk.windows_text import move_mouse
                move_mouse(arguments["x"], arguments["y"])
            else:
                pyautogui.moveTo(arguments["x"], arguments["y"], duration=0.25)
        elif action == "type":
            type_unicode(arguments.get("text", ""), interval=0.001)
        elif action == "hotkey":
            keys = [str(key).strip().casefold() for key in arguments.get("keys", [])]
            if sys.platform == "darwin":
                keys = [{"cmd": "command", "option": "alt"}.get(key, key) for key in keys]
            permanent_shortcut = (
                set(keys) == {"shift", "delete"}
                or sys.platform == "darwin" and set(keys) in (
                    {"command", "alt", "backspace"}, {"command", "alt", "delete"},
                )
            )
            if permanent_shortcut and not allows_permanent_delete(
                context.user_prompt if context else ""
            ):
                raise PermissionError(
                    f"{'Shift+Delete' if set(keys) == {'shift', 'delete'} else 'Command+Option+Delete'} "
                    "is permanent and is blocked unless the current user message explicitly requests "
                    "permanent or irreversible deletion. Use filesystem.delete for recoverable trash instead."
                )
            close_keys = (
                ({"command", "w"}, {"command", "q"})
                if sys.platform == "darwin"
                else ({"ctrl", "w"}, {"ctrl", "f4"}, {"alt", "f4"})
            )
            close_shortcut = set(keys) in close_keys
            before = _foreground_window_state() if close_shortcut else None
            if close_shortcut:
                target_window = str(arguments.get("target_window") or "").strip()
                if not target_window:
                    raise ValueError(
                        "Close shortcuts require target_window from an immediately preceding window/page observation"
                    )
                if target_window.casefold() not in str(before.get("window_title") or "").casefold():
                    raise RuntimeError(
                        "Close shortcut blocked because the currently focused page/window does not match target_window"
                    )
            if sys.platform == "win32":
                from deepdesk.windows_text import send_key_chord
                send_key_chord(keys)
            else:
                pyautogui.hotkey(*keys)
            if close_shortcut:
                time.sleep(0.6)
                after = _foreground_window_state()
                changed = bool(
                    before.get("window_handle") != after.get("window_handle")
                    or before.get("window_title") != after.get("window_title")
                    or before.get("application") != after.get("application")
                )
                if not changed:
                    raise RuntimeError(
                        "Close shortcut was delivered, but the foreground window/title did not change; "
                        "the page or window was not verified as closed"
                    )
        elif action == "scroll":
            if sys.platform == "win32":
                from deepdesk.windows_text import scroll_mouse
                scroll_mouse(vertical_delta=int(arguments.get("amount", 0)) * 120)
            else:
                pyautogui.scroll(arguments.get("amount", 0))
        else:
            raise ValueError(f"Unsupported computer action: {action}")
        result = {
            "ok": True,
            "action": action,
            "effect_verified": False,
            "verification_required": action not in {"move"},
        }
        if action == "hotkey" and close_shortcut:
            result.update(
                {
                    "effect_verified": True,
                    "verification_required": False,
                    "before": before,
                    "after": after,
                }
            )
        if action == "type":
            result["input_mode"] = "unicode_ime_independent"
        return result
