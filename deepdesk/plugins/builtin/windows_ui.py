from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes
from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread
from deepdesk.windows_activity import check_input_permit, foreground_input_scope
from deepdesk.windows_text import click_at, type_unicode

logger = logging.getLogger(__name__)

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002


def _normalise_windows_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _send_message_timeout(hwnd: int, message: int, wparam: int, lparam: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send = user32.SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    ok = send(
        hwnd,
        message,
        wparam,
        lparam,
        SMTO_BLOCK | SMTO_ABORTIFHUNG,
        2_000,
        ctypes.byref(result),
    )
    if not ok:
        error = ctypes.get_last_error()
        raise TimeoutError(f"Native text control did not respond (Win32 error {error})")
    return int(result.value)


def _native_text_control(match: Any) -> tuple[int, str]:
    info = match.element_info
    hwnd = int(getattr(info, "handle", 0) or 0)
    class_name = str(getattr(info, "class_name", "") or "")
    if not hwnd or not re.match(r"^(?:Edit|RichEdit)", class_name, re.IGNORECASE):
        raise RuntimeError(
            "The control exposes neither UIA ValuePattern nor a supported native Edit/RichEdit handle"
        )
    return hwnd, class_name


def _read_native_text(hwnd: int) -> str:
    length = _send_message_timeout(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _send_message_timeout(hwnd, WM_GETTEXT, length + 1, ctypes.addressof(buffer))
    return buffer.value


def _set_native_text(match: Any, text: str) -> dict[str, Any]:
    hwnd, class_name = _native_text_control(match)
    buffer = ctypes.create_unicode_buffer(text)
    _send_message_timeout(hwnd, WM_SETTEXT, 0, ctypes.addressof(buffer))
    actual = _read_native_text(hwnd)
    if _normalise_windows_text(actual) != _normalise_windows_text(text):
        raise RuntimeError("Native background text write could not be verified")
    return {
        "input_mode": "background_win32_message",
        "native_handle": hwnd,
        "native_class": class_name,
        "verified_exact": True,
    }


class WindowsUITool(ToolPlugin):
    name = "windows_ui"
    description = (
        "Inspect and operate native Windows applications through the UI Automation accessibility tree. "
        "Use list_windows, then inspect_window, then click/type a matching control."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_windows",
                    "inspect_window",
                    "read_value",
                    "click",
                    "type",
                    "close_window",
                ],
            },
            "window": {"type": "string", "description": "Regex matching a window title"},
            "window_handle": {"type": "integer", "minimum": 1},
            "control": {"type": "string", "description": "Regex matching control title/name"},
            "control_type": {
                "type": "string",
                "description": "Optional UIA type, e.g. Button or Edit",
            },
            "text": {"type": "string"},
            "allow_foreground": {
                "type": "boolean",
                "description": "Allow a fallback that focuses the target and may interrupt the user; default false",
            },
            "discard_unsaved": {
                "type": "boolean",
                "description": "For close_window only: choose Don't save/不保存 if prompted",
            },
            "depth": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        if (
            arguments.get("action") == "close_window"
            and arguments.get("discard_unsaved") is True
        ):
            return Risk.HIGH
        return (
            Risk.SAFE
            if arguments.get("action") in {"list_windows", "inspect_window", "read_value"}
            else Risk.MEDIUM
        )

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "list_windows":
            return "读取当前可见窗口"
        if action == "inspect_window":
            return f"检查窗口“{arguments.get('window', '')}”的控件"
        if action == "read_value":
            return f"读取窗口“{arguments.get('window', '')}”中的控件值"
        if action == "close_window":
            return f"关闭窗口“{arguments.get('window', '')}”"
        if action == "type":
            return f"在“{arguments.get('window', '')}”的“{arguments.get('control', '')}”输入文本"
        return f"操作窗口“{arguments.get('window', '')}”中的控件“{arguments.get('control', '')}”"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments)

    def _execute_sync(self, arguments: dict[str, Any]) -> Any:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        action = arguments["action"]
        if action == "list_windows":
            foreground_handle = int(ctypes.WinDLL("user32").GetForegroundWindow())
            windows = []
            for win in desktop.windows():
                try:
                    title = win.window_text().strip()
                    if title and win.is_visible():
                        rect = win.rectangle()
                        windows.append(
                            {
                                "title": title,
                                "handle": win.handle,
                                "process_id": win.process_id(),
                                "class_name": win.element_info.class_name,
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                                "is_foreground": int(win.handle) == foreground_handle,
                            }
                        )
                except Exception:
                    continue
            return {"windows": windows[:100]}

        pattern = arguments.get("window", ".*")
        handle = arguments.get("window_handle")
        if handle:
            win = desktop.window(handle=int(handle))
            win.wait("exists", timeout=3)
        else:
            win = next(
                (
                    candidate
                    for candidate in desktop.windows()
                    if re.search(pattern, candidate.window_text() or "", re.IGNORECASE)
                ),
                None,
            )
            if win is None:
                raise LookupError(f"No visible window matching {pattern!r}")
        if action == "inspect_window":
            depth = int(arguments.get("depth", 4))
            controls = []
            for control in win.descendants(depth=depth):
                try:
                    title = control.window_text().strip()
                    info = control.element_info
                    rect = control.rectangle()
                    if title or info.control_type in {
                        "Edit",
                        "Document",
                        "Button",
                        "ComboBox",
                        "ListItem",
                        "MenuItem",
                        "TabItem",
                    }:
                        controls.append(
                            {
                                "title": title,
                                "type": info.control_type,
                                "automation_id": info.automation_id,
                                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                                "enabled": control.is_enabled(),
                            }
                        )
                except Exception:
                    continue
            return {"window": win.window_text(), "controls": controls[:500]}

        if action == "close_window":
            title = win.window_text()
            target_handle = int(win.handle)
            win.close()
            discarded = False
            if arguments.get("discard_unsaved"):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        # Modern Notepad embeds its save prompt in this window. Keep
                        # the lookup scoped to the exact handle so a second Notepad
                        # window owned by the user cannot be dismissed accidentally.
                        for button in win.descendants(control_type="Button"):
                            name = (button.window_text() or "").strip().casefold()
                            automation_id = (
                                button.element_info.automation_id or ""
                            ).strip().casefold()
                            if automation_id == "secondarybutton" or name in {
                                "don't save",
                                "dont save",
                                "不保存",
                                "nicht speichern",
                                "ne pas enregistrer",
                                "no guardar",
                            }:
                                button.invoke()
                                discarded = True
                                break
                    except Exception:
                        logger.debug("Failed to inspect a close-confirmation button", exc_info=True)
                    if discarded:
                        break
                    time.sleep(0.15)
            close_deadline = time.monotonic() + 5
            closed = False
            while time.monotonic() < close_deadline:
                live_handles = set()
                for candidate in desktop.windows():
                    try:
                        live_handles.add(int(candidate.handle))
                    except Exception:
                        continue
                if target_handle not in live_handles:
                    closed = True
                    break
                time.sleep(0.1)
            return {
                "ok": closed,
                "window": title,
                "action": action,
                "discarded_unsaved": discarded,
                "foreground_used": False,
            }

        control_pattern = arguments.get("control")
        control_type = arguments.get("control_type")
        if action == "click" and not control_pattern and not control_type:
            raise ValueError("control or control_type is required for click")
        if action in {"type", "read_value"} and not control_pattern and not control_type:
            candidates = [
                control
                for preferred_type in ("Document", "Edit")
                for control in win.descendants(control_type=preferred_type)
            ]
        else:
            candidates = (
                win.descendants(control_type=control_type) if control_type else win.descendants()
            )
        match = next(
            (
                c
                for c in candidates
                if (not control_pattern or re.search(control_pattern, c.window_text(), re.IGNORECASE))
                and c.is_enabled()
                and c.is_visible()
            ),
            None,
        )
        if match is None:
            raise LookupError(
                f"No visible enabled control matching {control_pattern or control_type or 'Document/Edit'}"
            )
        if action == "read_value":
            try:
                value = match.get_value()
            except Exception:
                value = match.window_text()
            return {
                "ok": True,
                "window": win.window_text(),
                "window_handle": int(win.handle),
                "process_id": int(win.process_id()),
                "control": match.window_text(),
                "control_type": match.element_info.control_type,
                "automation_id": match.element_info.automation_id,
                "value": value,
                "foreground_used": False,
            }
        allow_foreground = bool(arguments.get("allow_foreground", False))
        foreground_used = False
        if action == "click":
            try:
                match.invoke()
            except Exception as exc:
                if not allow_foreground:
                    raise RuntimeError(
                        "Control does not support background Invoke. Keep the user undisturbed: "
                        "use another UIA control/path or request human takeover."
                    ) from exc
                with foreground_input_scope():
                    win.set_focus()
                    check_input_permit()
                    rect = match.rectangle()
                    click_at((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
                    foreground_used = True
        elif action == "type":
            input_details: dict[str, Any]
            try:
                match.set_edit_text(arguments.get("text", ""))
                input_details = {
                    "input_mode": "background_uia_value",
                    "verified_exact": True,
                }
            except Exception:
                try:
                    input_details = _set_native_text(match, arguments.get("text", ""))
                except Exception as native_exc:
                    if not allow_foreground:
                        raise RuntimeError(
                            "Control supports neither background UIA ValuePattern nor verified "
                            "native Edit/RichEdit input. Keep the user undisturbed: use another "
                            "background path or request human takeover."
                        ) from native_exc
                    with foreground_input_scope():
                        win.set_focus()
                        check_input_permit()
                        match.set_focus()
                        type_unicode(arguments.get("text", ""), interval=0.001)
                    input_details = {
                        "input_mode": "foreground_unicode_ime_independent",
                        "verified_exact": False,
                    }
        else:
            raise ValueError(f"Unsupported windows_ui action: {action}")
        result = {
            "ok": True,
            "window": win.window_text(),
            "window_handle": int(win.handle),
            "process_id": int(win.process_id()),
            "control": match.window_text(),
            "control_type": match.element_info.control_type,
            "automation_id": match.element_info.automation_id,
            "action": action,
        }
        if action == "type":
            result.update(input_details)
        result["foreground_used"] = foreground_used or bool(
            action == "type"
            and result.get("input_mode") == "foreground_unicode_ime_independent"
        )
        return result
