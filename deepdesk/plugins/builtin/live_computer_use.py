from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import math
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageChops, ImageGrab

from deepdesk.command_safety import allows_permanent_delete
from deepdesk.harness import allows_live_computer_use_start
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, finish_owned_work
from deepdesk.windows_activity import check_input_permit, foreground_input_scope
from deepdesk.windows_ocr import WindowsOCR
from deepdesk.windows_text import (
    click_mouse,
    move_mouse,
    scroll_mouse,
    send_key_chord,
    set_mouse_button,
    type_unicode,
)

logger = logging.getLogger(__name__)


_ZH_BANNER = "正在控制电脑，按Esc强制退出"
_EN_BANNER = "Controlling your computer — press Esc to force exit"
_MUTATING_ACTIONS = {
    "click",
    "double_click",
    "type",
    "key",
    "scroll",
    "drag",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _foreground_window() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"window_handle": 0, "window_title": "", "process_id": 0}
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    handle = int(user32.GetForegroundWindow())
    length = int(user32.GetWindowTextLengthW(handle)) if handle else 0
    buffer = ctypes.create_unicode_buffer(length + 1)
    process_id = wintypes.DWORD()
    rect = wintypes.RECT()
    focus = {'available': False}
    if handle:
        user32.GetWindowTextW(handle, buffer, len(buffer))
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        user32.GetWindowRect(handle, ctypes.byref(rect))
        class GUIThreadInfo(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('hwndActive', wintypes.HWND), ('hwndFocus', wintypes.HWND),
                        ('hwndCapture', wintypes.HWND), ('hwndMenuOwner', wintypes.HWND),
                        ('hwndMoveSize', wintypes.HWND), ('hwndCaret', wintypes.HWND),
                        ('rcCaret', wintypes.RECT)]
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIThreadInfo)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        info = GUIThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        thread_id = user32.GetWindowThreadProcessId(handle, None)
        if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            focus = {'available': True, 'window_handle': int(info.hwndFocus or 0),
                     'capture_handle': int(info.hwndCapture or 0),
                     'menu_owner_handle': int(info.hwndMenuOwner or 0)}
    return {
        "window_handle": handle,
        "window_title": buffer.value,
        "process_id": int(process_id.value),
        "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
        "focus": focus,
    }


def _window_dpi(window_handle: int) -> int:
    if sys.platform != "win32":
        return 96
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    if callable(get_dpi_for_window) and window_handle:
        try:
            get_dpi_for_window.argtypes = [ctypes.c_void_p]
            get_dpi_for_window.restype = ctypes.c_uint
            dpi = int(get_dpi_for_window(window_handle))
            if dpi > 0:
                return dpi
        except Exception as exc:
            logger.debug("GetDpiForWindow failed", exc_info=exc)
    get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
    if callable(get_dpi_for_system):
        try:
            get_dpi_for_system.argtypes = []
            get_dpi_for_system.restype = ctypes.c_uint
            dpi = int(get_dpi_for_system())
            if dpi > 0:
                return dpi
        except Exception as exc:
            logger.debug("GetDpiForSystem failed", exc_info=exc)
    return 96


class StaleObservationError(RuntimeError):
    """The visible desktop no longer matches the model's observation."""


class ActionRejectedError(ValueError):
    """An input action was rejected before any real input was delivered."""


class _Win32ControlBanner:
    """Small, non-activating native Win32 banner with no GUI dependency."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = 0
        self._error: BaseException | None = None
        self._wnd_proc: Any = None

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Live Computer Use is only available on Windows")
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="elren-live-computer-use-banner",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(3.0):
            raise TimeoutError("The native computer-control banner did not start")
        if self._error is not None:
            raise RuntimeError(f"The native computer-control banner failed: {self._error}")

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd and sys.platform == "win32":
            try:
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = [
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                ]
                user32.PostMessageW.restype = wintypes.BOOL
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            except Exception as exc:
                logger.debug("Posting control-banner close failed", exc_info=exc)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._hwnd = 0

    @property
    def healthy(self) -> bool:
        """Whether the native safety surface is still visible and responsive."""

        thread = self._thread
        return bool(
            thread is not None
            and thread.is_alive()
            and self._ready.is_set()
            and self._error is None
            and self._hwnd
        )

    def _run(self) -> None:
        # Constants are kept local so importing the tool stays safe on macOS.
        from ctypes import wintypes

        WM_CLOSE = 0x0010
        WM_DESTROY = 0x0002
        WM_PAINT = 0x000F
        WM_TIMER = 0x0113
        WM_NCHITTEST = 0x0084
        HTTRANSPARENT = -1
        WS_POPUP = 0x80000000
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TRANSPARENT = 0x00000020
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        HWND_TOPMOST = -1
        DT_CENTER = 0x0001
        DT_VCENTER = 0x0004
        DT_SINGLELINE = 0x0020
        TRANSPARENT = 1
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        MONITOR_DEFAULTTONEAREST = 0x00000002
        COLOR_BLUE = 0x00D47928  # COLORREF for RGB(40, 121, 212)

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wintypes.HDC),
                ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        class_name = f"ElrenLiveComputerUseBanner_{uuid4().hex}"
        brush = 0
        font = 0

        try:
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DefWindowProcW.restype = LRESULT
            user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
            user32.BeginPaint.restype = wintypes.HDC
            user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
            user32.EndPaint.restype = wintypes.BOOL
            user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetClientRect.restype = wintypes.BOOL
            user32.FillRect.argtypes = [
                wintypes.HDC,
                ctypes.POINTER(wintypes.RECT),
                wintypes.HBRUSH,
            ]
            user32.FillRect.restype = ctypes.c_int
            user32.DrawTextW.argtypes = [
                wintypes.HDC,
                wintypes.LPCWSTR,
                ctypes.c_int,
                ctypes.POINTER(wintypes.RECT),
                wintypes.UINT,
            ]
            user32.DrawTextW.restype = ctypes.c_int
            user32.LoadCursorW.restype = wintypes.HANDLE
            user32.RegisterClassW.restype = wintypes.WORD
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HANDLE,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
            user32.SetWindowRgn.restype = ctypes.c_int
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            user32.UnregisterClassW.restype = wintypes.BOOL
            user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            user32.SetTimer.argtypes = [
                wintypes.HWND,
                ctypes.c_size_t,
                wintypes.UINT,
                wintypes.LPVOID,
            ]
            user32.SetTimer.restype = ctypes.c_size_t
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = LRESULT
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.PostQuitMessage.restype = None
            gdi32.CreateSolidBrush.restype = wintypes.HANDLE
            gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
            gdi32.CreateFontW.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPCWSTR,
            ]
            gdi32.CreateFontW.restype = wintypes.HANDLE
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
            gdi32.SelectObject.restype = wintypes.HANDLE
            gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
            gdi32.SetTextColor.restype = wintypes.COLORREF
            gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
            gdi32.SetBkMode.restype = ctypes.c_int
            gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
            gdi32.DeleteObject.restype = wintypes.BOOL
            gdi32.CreateRoundRectRgn.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
            ]
            gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            instance = kernel32.GetModuleHandleW(None)
            brush = int(gdi32.CreateSolidBrush(COLOR_BLUE))
            font = int(
                gdi32.CreateFontW(
                    -18,
                    0,
                    0,
                    0,
                    600,
                    0,
                    0,
                    0,
                    1,
                    0,
                    0,
                    5,
                    0,
                    "Segoe UI",
                )
            )

            @WNDPROC
            def window_proc(hwnd: int, message: int, wparam: int, lparam: int) -> int:
                if message == WM_NCHITTEST:
                    return HTTRANSPARENT
                if message == WM_PAINT:
                    paint = PAINTSTRUCT()
                    hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
                    rect = wintypes.RECT()
                    user32.GetClientRect(hwnd, ctypes.byref(rect))
                    user32.FillRect(hdc, ctypes.byref(rect), brush)
                    previous_font = gdi32.SelectObject(hdc, font) if font else 0
                    gdi32.SetTextColor(hdc, 0x00FFFFFF)
                    gdi32.SetBkMode(hdc, TRANSPARENT)
                    user32.DrawTextW(
                        hdc,
                        self.text,
                        -1,
                        ctypes.byref(rect),
                        DT_CENTER | DT_VCENTER | DT_SINGLELINE,
                    )
                    if previous_font:
                        gdi32.SelectObject(hdc, previous_font)
                    user32.EndPaint(hwnd, ctypes.byref(paint))
                    return 0
                if message == WM_TIMER:
                    user32.SetWindowPos(
                        hwnd,
                        HWND_TOPMOST,
                        0,
                        0,
                        0,
                        0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                    )
                    return 0
                if message == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

            self._wnd_proc = window_proc
            window_class = WNDCLASSW(
                style=0,
                lpfnWndProc=window_proc,
                cbClsExtra=0,
                cbWndExtra=0,
                hInstance=instance,
                hIcon=0,
                hCursor=user32.LoadCursorW(
                    None,
                    ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR),
                ),
                hbrBackground=brush,
                lpszMenuName=None,
                lpszClassName=class_name,
            )
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())

            work_left = 0
            work_top = 0
            screen_width = max(640, int(user32.GetSystemMetrics(0)))
            foreground_handle = user32.GetForegroundWindow()
            monitor = user32.MonitorFromWindow(
                foreground_handle,
                MONITOR_DEFAULTTONEAREST,
            )
            monitor_info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
                work_left = int(monitor_info.rcWork.left)
                work_top = int(monitor_info.rcWork.top)
                screen_width = max(
                    640,
                    int(monitor_info.rcWork.right - monitor_info.rcWork.left),
                )
            width = min(560, max(380, screen_width - 40))
            height = 42
            x = work_left + max(0, (screen_width - width) // 2)
            y = work_top + 8
            hwnd = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
                class_name,
                self.text,
                WS_POPUP,
                x,
                y,
                width,
                height,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = int(hwnd)
            # Keep the status visible to the user without feeding the banner
            # back into screenshot/OCR observations as a false interface target.
            if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
                logger.debug(
                    "Windows could not exclude the control banner from capture",
                    extra={"winerror": ctypes.get_last_error()},
                )
            region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, 16, 16)
            if region:
                user32.SetWindowRgn(hwnd, region, True)
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                x,
                y,
                width,
                height,
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            user32.SetTimer(hwnd, 1, 1_000, None)
            self._ready.set()

            message = wintypes.MSG()
            while True:
                message_status = int(
                    user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                )
                if message_status == 0:
                    break
                if message_status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            self._hwnd = 0
            if font:
                gdi32.DeleteObject(font)
            if brush:
                gdi32.DeleteObject(brush)
            try:
                user32.UnregisterClassW(class_name, kernel32.GetModuleHandleW(None))
            except Exception as exc:
                logger.debug("Unregistering control-banner class failed", exc_info=exc)


@dataclass(slots=True)
class _ControlMetrics:
    observation_count: int = 0
    action_attempts: int = 0
    action_successes: int = 0
    action_failures: int = 0
    visible_screen_changes: int = 0
    preflight_rejections: int = 0
    stale_rejections: int = 0
    user_interference_rejections: int = 0
    policy_rejections: int = 0
    interference_stop_count: int = 0
    action_duration_ms: list[float] = field(default_factory=list)
    decision_latency_ms: list[float] = field(default_factory=list)
    recent_actions: list[dict[str, Any]] = field(default_factory=list)

    def record_rejection(self, message: str, *, kind: str) -> None:
        self.preflight_rejections += 1
        if kind == "user_interference":
            self.user_interference_rejections += 1
        elif kind == "policy":
            self.policy_rejections += 1
        else:
            self.stale_rejections += 1
        self.recent_actions.append(
            {
                "action": "preflight",
                "success": False,
                "input_delivered": False,
                "reason": str(message)[:240],
            }
        )
        self.recent_actions[:] = self.recent_actions[-30:]

    def record(
        self,
        *,
        action: str,
        success: bool,
        action_ms: float,
        decision_ms: float | None,
        screen_changed: bool,
    ) -> None:
        self.action_attempts += 1
        if success:
            self.action_successes += 1
        else:
            self.action_failures += 1
        if screen_changed:
            self.visible_screen_changes += 1
        self.action_duration_ms.append(round(action_ms, 2))
        if decision_ms is not None:
            self.decision_latency_ms.append(round(decision_ms, 2))
        self.action_duration_ms[:] = self.action_duration_ms[-100:]
        self.decision_latency_ms[:] = self.decision_latency_ms[-100:]
        self.recent_actions.append(
            {
                "action": action,
                "success": success,
                "action_ms": round(action_ms, 2),
                "decision_ms": None if decision_ms is None else round(decision_ms, 2),
                "screen_changed": screen_changed,
            }
        )
        self.recent_actions[:] = self.recent_actions[-30:]

    def payload(self) -> dict[str, Any]:
        attempts = self.action_attempts
        successes = self.action_successes
        decision_values = self.decision_latency_ms
        duration_values = self.action_duration_ms
        def percentile(values: list[float], fraction: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
            return round(ordered[index], 2)

        return {
            "observation_count": self.observation_count,
            "action_attempts": attempts,
            "action_successes": successes,
            "action_failures": self.action_failures,
            "preflight_rejections": self.preflight_rejections,
            "preflight_rejection_count": self.preflight_rejections,
            "stale_rejection_count": self.stale_rejections,
            "user_interference_rejection_count": self.user_interference_rejections,
            "policy_rejection_count": self.policy_rejections,
            "interference_stop_count": self.interference_stop_count,
            "prevented_stale_input_count": (
                self.stale_rejections + self.user_interference_rejections
            ),
            "action_delivery_success_rate": (
                round(successes / attempts, 4) if attempts else None
            ),
            "visible_screen_change_rate": (
                round(self.visible_screen_changes / attempts, 4) if attempts else None
            ),
            "unverified_effect_rate": (
                1.0 if attempts else None
            ),
            "average_action_ms": (
                round(sum(duration_values) / len(duration_values), 2)
                if duration_values
                else None
            ),
            "average_observation_to_action_ms": (
                round(sum(decision_values) / len(decision_values), 2)
                if decision_values
                else None
            ),
            "action_ms_p50": percentile(duration_values, 0.50),
            "action_ms_p95": percentile(duration_values, 0.95),
            "action_ms_max": round(max(duration_values), 2) if duration_values else None,
            "observation_to_action_ms_p50": percentile(decision_values, 0.50),
            "observation_to_action_ms_p95": percentile(decision_values, 0.95),
            "observation_to_action_ms_max": (
                round(max(decision_values), 2) if decision_values else None
            ),
            "recent_actions": list(self.recent_actions),
        }


@dataclass(slots=True)
class _ControlSession:
    session_id: str
    lease: str
    task_id: str
    started_at: str
    started_monotonic: float
    last_activity_monotonic: float
    timeout_seconds: int
    stop_event: threading.Event
    banner: _Win32ControlBanner
    metrics: _ControlMetrics = field(default_factory=_ControlMetrics)
    last_observed_monotonic: float | None = None
    monitor_thread: threading.Thread | None = None
    observation: _ObservationSnapshot | None = None
    stopping_reason: str = ""
    screenshots: list[Path] = field(default_factory=list)
    desktop_lease: Any = None


@dataclass(slots=True)
class _ObservationSnapshot:
    observation_id: str
    captured_monotonic: float
    image: Image.Image
    foreground: dict[str, Any]
    virtual_bounds: tuple[int, int, int, int]
    pointer: tuple[int, int]
    dpi: int
    uia_targets: dict[str, dict[str, Any]]
    activity_generation: int | None = None
    focused_element: dict[str, Any] | None = None
    monitors: list[dict[str, Any]] | None = None


class LiveComputerUseController:
    """One lease-protected, process-wide foreground computer-control session."""

    def __init__(
        self,
        screenshot_dir: Path,
        *,
        banner_factory: Callable[[str], Any] = _Win32ControlBanner,
        driver_loader: Callable[[], Any] | None = None,
        escape_pressed: Callable[[], bool] | None = None,
        screen_capture: Callable[[], Image.Image] | None = None,
        screen_bounds: Callable[[], tuple[int, int, int, int]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.screenshot_dir = Path(screenshot_dir)
        self._enable_dpi_awareness()
        self._banner_factory = banner_factory
        self._driver_loader = driver_loader or self._load_driver
        self._escape_pressed = escape_pressed or self._get_escape_pressed
        self._screen_capture = screen_capture or self._grab_virtual_desktop
        self._screen_bounds = screen_bounds or self._virtual_desktop_bounds
        self._clock = clock
        self._state_lock = threading.RLock()
        self._action_lock = threading.Lock()
        # UI Automation/comtypes wrappers are apartment-thread-affine. Every
        # observe/action operation uses this single worker so wrappers never
        # cross arbitrary asyncio.to_thread workers.
        self.worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="elren-live-computer-use-worker",
        )
        self._session: _ControlSession | None = None
        self._last_status: dict[str, Any] = {
            "available": sys.platform == "win32",
            "active": False,
            "stop_reason": "not_started",
            "metrics": _ControlMetrics().payload(),
        }

    @staticmethod
    def _enable_dpi_awareness() -> None:
        if sys.platform != "win32":
            return
        try:
            # PER_MONITOR_AWARE_V2 keeps UIA rectangles, ImageGrab pixels, and
            # SendInput coordinates in the same physical coordinate system.
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception as exc:
            # The launcher already sets this before Python starts. Windows
            # returns access denied when a process awareness context is set.
            logger.debug("DPI awareness was already configured", exc_info=exc)

    @staticmethod
    def _load_driver() -> Any:
        import pyautogui

        # Do not mutate PyAutoGUI's process-global PAUSE/FAILSAFE settings;
        # Live Computer Use owns its own bounds checks, pacing, and Esc stop.
        return pyautogui

    @staticmethod
    def _get_escape_pressed() -> bool:
        if sys.platform != "win32":
            return False
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        value = int(user32.GetAsyncKeyState(0x1B))
        # High bit means currently down; low bit records a short press since
        # the previous poll so a tap shorter than the monitor interval is seen.
        return bool(value & 0x8001)

    @staticmethod
    def _physical_controls_down(*, allowed_mouse_button: str = "") -> list[str]:
        """Return held modifiers/buttons that could change an input's meaning."""

        if sys.platform != "win32":
            return []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        controls = {
            "left_mouse": 0x01,
            "right_mouse": 0x02,
            "middle_mouse": 0x04,
            "shift": 0x10,
            "ctrl": 0x11,
            "alt": 0x12,
            "left_win": 0x5B,
            "right_win": 0x5C,
        }
        allowed_name = {
            "left": "left_mouse",
            "right": "right_mouse",
            "middle": "middle_mouse",
        }.get(str(allowed_mouse_button or "").casefold(), "")
        return [
            name
            for name, virtual_key in controls.items()
            if name != allowed_name and int(user32.GetAsyncKeyState(virtual_key)) & 0x8000
        ]

    @classmethod
    def _assert_no_external_controls_held(
        cls,
        *,
        allowed_mouse_button: str = "",
    ) -> None:
        held = cls._physical_controls_down(allowed_mouse_button=allowed_mouse_button)
        if held:
            raise InterruptedError(
                "Physical keyboard modifiers or mouse buttons are held "
                f"({', '.join(held)}); the session was stopped before more input"
            )

    @staticmethod
    def _virtual_desktop_bounds() -> tuple[int, int, int, int]:
        if sys.platform != "win32":
            return (0, 0, 0, 0)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width <= 0 or height <= 0:
            raise RuntimeError("Windows returned invalid virtual desktop geometry")
        return left, top, width, height

    @staticmethod
    def _grab_virtual_desktop() -> Image.Image:
        image = ImageGrab.grab(all_screens=True)
        if not isinstance(image, Image.Image):
            raise RuntimeError("Windows virtual-desktop capture returned an invalid image")
        return image

    def _active_payload(
        self, session: _ControlSession, *, include_lease: bool = False
    ) -> dict[str, Any]:
        now = self._clock()
        payload: dict[str, Any] = {
            "available": True,
            "active": True,
            "session_id": session.session_id,
            "task_id": session.task_id,
            "started_at": session.started_at,
            "expires_in_seconds": max(
                0,
                round(
                    session.timeout_seconds - (now - session.last_activity_monotonic),
                    2,
                ),
            ),
            "metrics": session.metrics.payload(),
            "stopping": bool(session.stopping_reason),
        }
        if include_lease:
            # The host redactor intentionally hides credential-shaped `token`
            # fields from the model. `session_lease` is an ephemeral, task-bound
            # capability handle and remains usable across consecutive tool turns.
            payload["session_lease"] = session.lease
        return payload

    def public_status(self) -> dict[str, Any]:
        self._expire_if_needed()
        with self._state_lock:
            if self._session is not None:
                return self._active_payload(self._session)
            return dict(self._last_status)

    def start(self, task_id: str, timeout_seconds: int, language: str) -> dict[str, Any]:
        if sys.platform != "win32":
            raise RuntimeError("Live Computer Use is only available on Windows")
        bounded_timeout = max(30, min(3_600, int(timeout_seconds)))
        self._expire_if_needed()
        with self._state_lock:
            if self._session is not None:
                if self._session.task_id == task_id:
                    if self._session.stop_event.is_set() or self._session.stopping_reason:
                        raise RuntimeError(
                            "The existing Live Computer Use session is still stopping"
                        )
                    return {
                        **self._active_payload(self._session, include_lease=True),
                        "already_active": True,
                    }
                raise RuntimeError(
                    "Another task already owns the Live Computer Use session; stop it first"
                )

            if language == "zh":
                text = _ZH_BANNER
            elif language == "en":
                text = _EN_BANNER
            else:
                text = _ZH_BANNER if any("\u3400" <= char <= "\u9fff" for char in task_id) else _EN_BANNER
            # Automatic language selection is refined by the tool before this
            # controller method is called; task_id itself never selects a locale.
            banner = self._banner_factory(text)
            now = self._clock()
            session = _ControlSession(
                session_id=uuid4().hex,
                lease=secrets.token_urlsafe(24),
                task_id=task_id,
                started_at=_utc_now(),
                started_monotonic=now,
                last_activity_monotonic=now,
                timeout_seconds=bounded_timeout,
                stop_event=threading.Event(),
                banner=banner,
            )
            self._session = session

        try:
            if self._uses_native_windows_input(self._driver_loader()):
                from deepdesk.live_control_desktop import DesktopLease
                session.desktop_lease = DesktopLease()
            banner.start()
            with self._state_lock:
                if self._session is not session or session.stop_event.is_set():
                    raise InterruptedError(
                        "The Live Computer Use session was stopped while starting"
                    )
            monitor = threading.Thread(
                target=self._monitor_session,
                args=(session.session_id,),
                name=f"elren-live-computer-use-{session.session_id[:8]}",
                daemon=True,
            )
            session.monitor_thread = monitor
            monitor.start()
        except BaseException:
            try:
                banner.stop()
            except Exception as exc:
                logger.debug("Stopping a partially-started control banner failed", exc_info=exc)
            self.emergency_stop("start_failed", expected_session_id=session.session_id)
            raise
        return self._active_payload(session, include_lease=True)

    def _monitor_session(self, session_id: str) -> None:
        # A held Esc key from before session startup must be released once;
        # only a new press is an emergency stop for this session.
        armed = not self._escape_pressed()
        was_down = not armed
        while True:
            with self._state_lock:
                session = self._session
                if session is None or session.session_id != session_id:
                    return
                stop_requested = session.stop_event.wait(0.035)
                stop_reason = session.stopping_reason or "stop_requested"
                expired = (
                    self._clock() - session.last_activity_monotonic
                    >= session.timeout_seconds
                )
            if stop_requested:
                # A blocked native/UIA call may outlive the first four-second
                # quiescence deadline. Keep the monitor alive and retry until
                # the in-flight input really releases; never abandon Esc safety
                # while the host still reports an active stopping session.
                if self.emergency_stop(stop_reason, expected_session_id=session_id):
                    return
                continue
            if expired:
                self.emergency_stop("expired", expected_session_id=session_id)
                return
            banner_health = getattr(session.banner, "healthy", True)
            if callable(banner_health):
                banner_health = banner_health()
            if banner_health is False:
                logger.error(
                    "Live Computer Use safety banner exited while control was active",
                    extra={"session_id": session_id},
                )
                self.emergency_stop("banner_lost", expected_session_id=session_id)
                return
            down = self._escape_pressed()
            if not armed:
                if not down:
                    armed = True
                was_down = down
                continue
            if down and not was_down and self.emergency_stop(
                "escape_key",
                expected_session_id=session_id,
            ):
                return
            was_down = down

    def _expire_if_needed(self) -> None:
        with self._state_lock:
            session = self._session
            expired = bool(
                session
                and self._clock() - session.last_activity_monotonic
                >= session.timeout_seconds
            )
            session_id = session.session_id if session else ""
        if expired:
            self.emergency_stop("expired", expected_session_id=session_id)

    def _require_session(self, lease: str, task_id: str) -> _ControlSession:
        with self._state_lock:
            session = self._session
            if session is None:
                raise RuntimeError("No active Live Computer Use session; call start first")
            if session.task_id != task_id:
                raise PermissionError("The active Live Computer Use session belongs to another task")
            if not lease or not secrets.compare_digest(session.lease, str(lease)):
                raise PermissionError("The Live Computer Use session lease is invalid")
            if self._clock() - session.last_activity_monotonic >= session.timeout_seconds:
                session.stopping_reason = "expired"
                session.stop_event.set()
                raise InterruptedError("The Live Computer Use session expired")
            if session.stop_event.is_set() or session.stopping_reason:
                raise InterruptedError("The Live Computer Use session is stopping")
            session.last_activity_monotonic = self._clock()
            return session

    def status_with_lease(self, lease: str, task_id: str) -> dict[str, Any]:
        self._expire_if_needed()
        session = self._require_session(lease, task_id)
        return self._active_payload(session, include_lease=True)

    def validate_observation_session(
        self,
        lease: str,
        task_id: str,
        observation_id: str,
    ) -> dict[str, Any]:
        """Reject an OCR result that finished after its session was stopped/replaced."""

        session = self._require_session(lease, task_id)
        observation = session.observation
        if observation is None or not secrets.compare_digest(
            observation.observation_id,
            str(observation_id or ""),
        ):
            raise StaleObservationError(
                "The screen observation was replaced while OCR was running; observe again"
            )
        return self._active_payload(session, include_lease=True)

    def _activity_generation(self) -> int | None:
        if not self._uses_native_windows_input(self._driver_loader()):
            return None
        from deepdesk.windows_activity import get_activity_monitor
        return get_activity_monitor().snapshot().generation

    def _focused_element(self) -> dict[str, Any] | None:
        if not self._uses_native_windows_input(self._driver_loader()):
            return None

        try:
            from pywinauto.uia_defines import IUIA
            element = IUIA().get_focused_element()
            return {'runtime_id':list(element.GetRuntimeId()),
                    'process_id':int(element.CurrentProcessId),
                    'control_type':int(element.CurrentControlType),
                    'password':bool(element.CurrentIsPassword)}
        except Exception:
            # Self-drawn applications can expose no UIA focus. Native focus and
            # actual visual judgment still apply; no control names/values read.
            return None

    def _monitors(self):
        if not self._uses_native_windows_input(self._driver_loader()):
            return None
        from deepdesk.live_control_desktop import display_layout
        return display_layout()

    def validate_visual_observation(self, lease: str, task_id: str, observation_id: str,
                                    image_box: tuple[int, int, int, int] | None = None) -> None:
        """Lightweight local check, no OCR/remote call or new mixed observation."""
        self.validate_observation_session(lease, task_id, observation_id)
        session = self._require_session(lease, task_id)
        observation = session.observation
        if observation.activity_generation != self._activity_generation():
            raise InterruptedError('External input invalidated the visual judgment')
        if observation.focused_element != self._focused_element():
            raise InterruptedError('Focused accessibility element changed during visual judgment')
        pointer = self._driver_loader().position()
        foreground = _foreground_window()
        if ((int(pointer.x), int(pointer.y)) != observation.pointer
                or any(foreground.get(key) != observation.foreground.get(key)
                       for key in ('window_handle', 'process_id', 'window_title', 'focus'))):
            raise InterruptedError('User pointer or focus changed during visual judgment')
        if (foreground.get('rect') != observation.foreground.get('rect')
                or self._monitors() != observation.monitors
                or self._screen_bounds() != observation.virtual_bounds
                or _window_dpi(int(observation.foreground.get('window_handle') or 0)) != observation.dpi):
            raise StaleObservationError('Display geometry changed during visual judgment')
        image = self._screen_capture()
        if image.size != observation.image.size:
            raise StaleObservationError('Screenshot dimensions changed during visual judgment')
        expected = observation.image
        if image_box is not None:
            if (len(image_box) != 4 or any(type(v) is not int for v in image_box)
                    or not 0 <= image_box[0] < image_box[2] <= image.width
                    or not 0 <= image_box[1] < image_box[3] <= image.height):
                raise StaleObservationError('Invalid visual validation region')
            image, expected = image.crop(image_box), expected.crop(image_box)
        if image.convert('RGB').tobytes() != expected.convert('RGB').tobytes():
            from PIL import ImageChops
            error = StaleObservationError('Screen pixels changed during visual judgment')
            error.visual_change_box = ImageChops.difference(image.convert('RGB'), expected.convert('RGB')).getbbox()
            raise error

    def validate_remote_wait(self, lease: str, task_id: str, observation_id: str) -> None:
        self.validate_observation_session(lease, task_id, observation_id)
        observation = self._require_session(lease, task_id).observation
        if observation.activity_generation != self._activity_generation() or _foreground_window() != observation.foreground:
            raise InterruptedError('External input or focus change interrupted visual request')
        if observation.focused_element != self._focused_element():
            raise InterruptedError('Focused accessibility element changed during visual request')

    def capture_observation(self, lease: str, task_id: str) -> dict[str, Any]:
        with self._action_lock:
            session = self._require_session(lease, task_id)
            image, payload, uia_targets = self._capture(session, include_uia=True)
            if session.stop_event.is_set():
                raise InterruptedError("The Live Computer Use session was stopped")
            now = self._clock()
            with self._state_lock:
                if self._session is session:
                    self._publish_observation(
                        session,
                        image=image,
                        payload=payload,
                        uia_targets=uia_targets,
                        observed_at=now,
                    )
            payload["session"] = self._active_payload(session)
            return payload

    def _capture(
        self,
        session: _ControlSession,
        *,
        include_uia: bool = False,
    ) -> tuple[Image.Image, dict[str, Any], dict[str, dict[str, Any]]]:
        if session.stop_event.is_set():
            raise InterruptedError("The Live Computer Use session was stopped")
        driver = self._driver_loader()
        started = self._clock()
        activity_generation = self._activity_generation()
        focused_element = self._focused_element()
        monitors = self._monitors()
        foreground = _foreground_window()
        bounds = self._screen_bounds()
        pointer_value = driver.position()
        pointer = [int(pointer_value.x), int(pointer_value.y)]
        dpi = _window_dpi(int(foreground.get("window_handle") or 0))
        screenshot_started = self._clock()
        image = self._screen_capture()
        screenshot_ms = (self._clock() - screenshot_started) * 1000
        if not isinstance(image, Image.Image):
            raise RuntimeError("The desktop screenshot provider returned an invalid image")
        virtual_left, virtual_top, virtual_width, virtual_height = bounds
        if image.size != (virtual_width, virtual_height):
            raise RuntimeError(
                "Virtual-desktop capture geometry does not match Windows physical coordinates: "
                f"capture={image.width}x{image.height}, desktop={virtual_width}x{virtual_height}"
            )
        screen_size = [virtual_width, virtual_height]
        coordinate_origin = [virtual_left, virtual_top]
        uia_elements: list[dict[str, Any]] = []
        uia_targets: dict[str, dict[str, Any]] = {}
        if include_uia:
            uia_started = self._clock()
            uia_elements, uia_targets = self._inspect_uia(foreground)
            uia_ms = (self._clock() - uia_started) * 1000
        else:
            uia_ms = 0.0
        uia_discarded_reason = None
        if uia_elements:
            check_started = self._clock()
            after_uia_image = self._screen_capture()
            screenshot_ms += (self._clock() - check_started) * 1000
            if after_uia_image.size != image.size:
                raise StaleObservationError('Display changed during element collection')
            if after_uia_image.convert('RGB').tobytes() != image.convert('RGB').tobytes():
                # Never publish positions collected from a moving interface
                # beside an older image. A fresh visual-only observation remains
                # usable even when accessibility cannot be synchronized.
                image = after_uia_image
                uia_elements, uia_targets = [], {}
                uia_discarded_reason = 'Pixels changed during element collection; using fresh visual-only frame'
        pointer_after_value = driver.position()
        pointer_after = [int(pointer_after_value.x), int(pointer_after_value.y)]
        foreground_after = _foreground_window()
        bounds_after = self._screen_bounds()
        dpi_after = _window_dpi(int(foreground_after.get("window_handle") or 0))
        if (
            foreground_after != foreground
            or bounds_after != bounds
            or dpi_after != dpi
            or pointer_after != pointer
            or session.stop_event.is_set()
            or activity_generation != self._activity_generation()
            or focused_element != self._focused_element()
            or monitors != self._monitors()
        ):
            raise StaleObservationError(
                "The desktop changed while it was being observed; no mixed-window observation was published"
            )

        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / (
            f"live-computer-use-{session.session_id[:10]}-{time.time_ns()}.png"
        )
        image.save(path, format="PNG")
        session.screenshots.append(path)
        while len(session.screenshots) > 3:
            stale = session.screenshots.pop(0)
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Removing an old desktop-control screenshot failed", exc_info=exc)
        try:
            historical = sorted(
                self.screenshot_dir.glob("live-computer-use-*.png"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
            cutoff = time.time() - 3_600
            for index, stale in enumerate(historical):
                if index >= 12 or stale.stat().st_mtime < cutoff:
                    stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Pruning desktop-control screenshots failed", exc_info=exc)
        payload = {
            "screenshot": str(path),
            "screenshot_url": f"/screenshots/{path.name}",
            "size": [image.width, image.height],
            "coordinate_space": "windows_virtual_desktop",
            "coordinate_origin": coordinate_origin,
            "coordinate_bounds": [
                virtual_left,
                virtual_top,
                virtual_left + virtual_width,
                virtual_top + virtual_height,
            ],
            "screen_size": screen_size,
            "foreground": foreground,
            "focused_element": focused_element,
            "monitors": monitors,
            "dpi_source": "GetDpiForWindow; not a CSS or universal monitor scale",
            "pointer": pointer,
            "dpi": dpi,
            "uia_elements": uia_elements,
            "uia_discarded_reason": uia_discarded_reason,
            "targeting_guidance": (
                "Use a matching uia_elements[].element_id, target_description, or exact physical x/y. "
                "Explicit coordinates will not be replaced with element centers. Every input action must include this "
                "observation_id; stale desktop state is rejected before input."
            ),
            "capture_ms": round((self._clock() - started) * 1_000, 2),
            "captured_at": _utc_now(),
            "activity_generation": activity_generation,
            "execution_readiness": "Requires valid observation and remote visual precondition check before input",
            "timing": {"screenshot_ms": round(screenshot_ms, 2), "elements_ms": round(uia_ms, 2)},
        }
        return image, payload, uia_targets

    @staticmethod
    def _inspect_uia(
        foreground: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        handle = int(foreground.get("window_handle") or 0)
        if not handle:
            return [], {}
        try:
            from pywinauto import Desktop

            window = Desktop(backend="uia").window(handle=handle)
            window.wait("exists visible", timeout=1.5)
            controls = window.descendants(depth=6)
        except Exception:
            return [], {}

        elements: list[dict[str, Any]] = []
        targets: dict[str, dict[str, Any]] = {}
        preferred_types = {
            "Button",
            "CheckBox",
            "ComboBox",
            "Document",
            "Edit",
            "Hyperlink",
            "ListItem",
            "MenuItem",
            "RadioButton",
            "Slider",
            "TabItem",
            "TreeItem",
        }
        for control in controls:
            if len(elements) >= 160:
                break
            try:
                info = control.element_info
                control_type = str(info.control_type or "")
                name = str(control.window_text() or "").strip()
                automation_id = str(info.automation_id or "").strip()
                # Static labels are already present in the screenshot/OCR. Keep
                # the model-facing tree focused on actionable controls so a
                # dense application cannot drown the decision context.
                if control_type not in preferred_types:
                    continue
                if (
                    not name
                    and not automation_id
                    and control_type not in {"Document", "Edit", "Slider"}
                ):
                    continue
                rect_value = control.rectangle()
                rect = [
                    int(rect_value.left),
                    int(rect_value.top),
                    int(rect_value.right),
                    int(rect_value.bottom),
                ]
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    continue
                visible = bool(control.is_visible())
                enabled = bool(control.is_enabled())
                if not visible:
                    continue
                element_id = f"uia-{len(elements) + 1:03d}-{uuid4().hex[:6]}"
                element = {
                    "element_id": element_id,
                    "name": (name or automation_id)[:500],
                    "control_type": control_type,
                    "automation_id": automation_id[:240],
                    "rect": rect,
                    "enabled": enabled,
                    "suggested_action": (
                        "type"
                        if control_type in {"Document", "Edit"}
                        else "click"
                    ),
                }
                elements.append(element)
                targets[element_id] = {"wrapper": control, "element": element}
            except Exception:
                continue
        return elements, targets

    @staticmethod
    def _publish_observation(
        session: _ControlSession,
        *,
        image: Image.Image,
        payload: dict[str, Any],
        uia_targets: dict[str, dict[str, Any]],
        observed_at: float,
    ) -> None:
        observation_id = uuid4().hex
        foreground = dict(payload.get("foreground") or {})
        screen = list(payload.get("screen_size") or [image.width, image.height])
        origin = list(payload.get("coordinate_origin") or [0, 0])
        pointer = list(payload.get("pointer") or [0, 0])
        session.observation = _ObservationSnapshot(
            observation_id=observation_id,
            captured_monotonic=observed_at,
            image=image.copy(),
            foreground=foreground,
            virtual_bounds=(
                int(origin[0]),
                int(origin[1]),
                int(screen[0]),
                int(screen[1]),
            ),
            pointer=(int(pointer[0]), int(pointer[1])),
            dpi=int(payload.get("dpi") or 96),
            uia_targets=uia_targets,
        )
        session.last_observed_monotonic = observed_at
        session.last_activity_monotonic = observed_at
        session.metrics.observation_count += 1
        payload["observation_id"] = observation_id
        session.observation.activity_generation = payload.get('activity_generation')
        session.observation.focused_element = payload.get('focused_element')
        session.observation.monitors = payload.get('monitors')
        payload["observation_valid_for_seconds"] = min(45, session.timeout_seconds)

    @staticmethod
    def _screen_change_ratio(before: Image.Image, after: Image.Image) -> float:
        if before.size != after.size:
            return 1.0
        sample_size = (160, 90)
        before_sample = before.convert("L").resize(sample_size)
        after_sample = after.convert("L").resize(sample_size)
        difference = ImageChops.difference(before_sample, after_sample)
        histogram = difference.histogram()
        changed = sum(histogram[4:])
        return round(changed / (sample_size[0] * sample_size[1]), 6)

    def _bounded_point(self, driver: Any, x: Any, y: Any) -> tuple[int, int]:
        if isinstance(x, bool) or isinstance(y, bool):
            raise ValueError("Desktop coordinates must be integers")
        try:
            point_x, point_y = int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ValueError("Desktop coordinates must be integers") from exc
        del driver
        left, top, width, height = self._screen_bounds()
        if not (left <= point_x < left + width and top <= point_y < top + height):
            raise ValueError(
                f"Desktop coordinate ({point_x}, {point_y}) is outside virtual desktop "
                f"[{left}, {top}, {left + width}, {top + height})"
            )
        return point_x, point_y

    def perform_action(
        self,
        action: str,
        arguments: dict[str, Any],
        lease: str,
        task_id: str,
    ) -> dict[str, Any]:
        session: _ControlSession | None = None
        try:
            with self._action_lock:
                session = self._require_session(lease, task_id)
                driver = self._driver_loader()
                try:
                    prepared_arguments, target_source, before = self._preflight_action(
                        driver,
                        session,
                        action,
                        arguments,
                    )
                except StaleObservationError as exc:
                    message = str(exc)
                    kind = (
                        "user_interference"
                        if "mouse moved" in message.casefold()
                        else "stale"
                    )
                    session.metrics.record_rejection(message, kind=kind)
                    raise
                except ActionRejectedError as exc:
                    session.metrics.record_rejection(str(exc), kind="policy")
                    raise
                action_started = self._clock()
                decision_ms = (
                    None
                    if session.last_observed_monotonic is None
                    else max(
                        0.0,
                        (action_started - session.last_observed_monotonic) * 1_000,
                    )
                )
                try:
                    with foreground_input_scope() if self._uses_native_windows_input(driver) else nullcontext():
                        input_started = self._clock()
                        self._deliver_action(driver, session, action, prepared_arguments)
                        native_input_ms = (self._clock() - input_started) * 1000
                    if session.stop_event.wait(0.16):
                        raise InterruptedError("The Live Computer Use session was stopped")
                    action_ms = (self._clock() - action_started) * 1_000
                    after, observation, uia_targets = self._capture(
                        session,
                        include_uia=True,
                    )
                    if session.stop_event.is_set():
                        raise InterruptedError("The Live Computer Use session was stopped")
                    change_ratio = self._screen_change_ratio(before, after)
                    screen_changed = change_ratio >= 0.001
                    observed_at = self._clock()
                    session.metrics.record(
                        action=action,
                        success=True,
                        action_ms=action_ms,
                        decision_ms=decision_ms,
                        screen_changed=screen_changed,
                    )
                    self._publish_observation(
                        session,
                        image=after,
                        payload=observation,
                        uia_targets=uia_targets,
                        observed_at=observed_at,
                    )
                    return {
                        "action": action,
                        "target_source": target_source,
                        "delivered": True,
                        "effect_verified": False,
                        "screen_changed": screen_changed,
                        "visible_change_detected": screen_changed,
                        "screen_change_ratio": change_ratio,
                        "verification_note": (
                            "A visible pixel change was detected, but the intended effect still "
                            "requires semantic/UI state verification."
                            if screen_changed
                            else (
                                "Input was delivered, but pixels did not visibly change. "
                                "Do not claim the intended effect without another observation."
                            )
                        ),
                        "observation": observation,
                        "timing": {
                            "observation_to_action_ms": (
                                None if decision_ms is None else round(decision_ms, 2)
                            ),
                            "action_ms": round(action_ms, 2),
                            "native_input_ms": round(native_input_ms, 2),
                        },
                        "session": self._active_payload(session),
                    }
                except BaseException as exc:
                    action_ms = (self._clock() - action_started) * 1_000
                    interruption = str(exc).casefold()
                    if isinstance(exc, InterruptedError) and any(
                        marker in interruption
                        for marker in (
                            "mouse",
                            "physical keyboard",
                            "foreground window changed",
                        )
                    ):
                        session.metrics.interference_stop_count += 1
                    session.metrics.record(
                        action=action,
                        success=False,
                        action_ms=action_ms,
                        decision_ms=decision_ms,
                        screen_changed=False,
                    )
                    raise
        except (StaleObservationError, ActionRejectedError):
            # No input was delivered; the lease remains valid for re-observe.
            raise
        except BaseException:
            # The action lock has been released before stopping, so a stop can
            # wait for all input to finish without deadlocking this worker.
            if session is not None:
                self.emergency_stop(
                    "action_error",
                    expected_session_id=session.session_id,
                )
            raise

    def _preflight_action(
        self,
        driver: Any,
        session: _ControlSession,
        action: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], str, Image.Image]:
        observation = session.observation
        supplied_id = str(arguments.get("observation_id") or "")
        if observation is None:
            raise StaleObservationError(
                "No bound screen observation exists. Call observe before any input action."
            )
        if observation.activity_generation != self._activity_generation():
            raise StaleObservationError('External input occurred after observation; observe again')
        if supplied_id != observation.observation_id:
            raise StaleObservationError(
                "The observation_id is missing or stale. Call observe again and use its exact latest id."
            )
        age = self._clock() - observation.captured_monotonic
        delayed_uia = age > min(45, session.timeout_seconds)
        # Slow model responses can outlive the normal observation window. Only
        # a concrete accessible target may be revalidated, and only against a
        # byte-identical fresh frame below. Coordinates/keys retain the strict
        # limit; leases, foreground, geometry and user-input guards still apply.
        if delayed_uia and (
            not arguments.get("element_id")
            or action not in {"click", "double_click", "type"}
            or age > min(180, session.timeout_seconds)
        ):
            raise StaleObservationError(
                f"The desktop observation is {age:.1f}s old. Call observe again before acting."
            )

        button = str(arguments.get("button") or "left").casefold()
        if button not in {"left", "right", "middle"}:
            raise ActionRejectedError(f"Unsupported mouse button: {button}")
        if action == "type":
            text = arguments.get("text", "")
            if not isinstance(text, str):
                raise ActionRejectedError("Live Computer Use type.text must be a string")
            if len(text) > 8_000:
                raise ActionRejectedError(
                    "Live Computer Use type text is limited to 8,000 characters"
                )
        if action == "key":
            raw_keys = arguments.get("keys") or []
            if not isinstance(raw_keys, list):
                raise ActionRejectedError("key.keys must be an array")
            keys = [str(value).strip().casefold() for value in raw_keys]
            if not keys or len(keys) > 8 or any(not key for key in keys):
                raise ActionRejectedError(
                    "key requires between one and eight non-empty key names"
                )
        if action == "scroll":
            for name in ("scroll_x", "scroll_y"):
                raw_value = arguments.get(name, 0)
                if isinstance(raw_value, bool):
                    raise ActionRejectedError(f"{name} must be an integer")
                try:
                    value = int(raw_value or 0)
                except (TypeError, ValueError) as exc:
                    raise ActionRejectedError(f"{name} must be an integer") from exc
                if abs(value) > 12_000:
                    raise ActionRejectedError(
                        "scroll_x and scroll_y are limited to 12,000 pixels per action"
                    )
        if action == "drag":
            raw_path = arguments.get("path") or []
            if not isinstance(raw_path, list) or not 2 <= len(raw_path) <= 64:
                raise ActionRejectedError(
                    "drag.path requires between two and 64 coordinate points"
                )
            try:
                duration = float(arguments.get("duration") or 0.6)
            except (TypeError, ValueError) as exc:
                raise ActionRejectedError("drag.duration must be a number") from exc
            if not math.isfinite(duration) or not 0.1 <= duration <= 5.0:
                raise ActionRejectedError(
                    "drag.duration must be between 0.1 and 5 seconds"
                )

        current_foreground = _foreground_window()
        expected_handle = int(observation.foreground.get("window_handle") or 0)
        current_handle = int(current_foreground.get("window_handle") or 0)
        if expected_handle != current_handle or any(
            observation.foreground.get(key) != current_foreground.get(key)
            for key in ("process_id", "window_title", "focus")
        ):
            raise StaleObservationError(
                "The foreground window changed after observe. No input was sent; observe again."
            )
        if list(observation.foreground.get("rect") or []) != list(
            current_foreground.get("rect") or []
        ):
            raise StaleObservationError(
                "The foreground window moved or resized after observe. No input was sent; observe again."
            )

        current_bounds = self._screen_bounds()
        if current_bounds != observation.virtual_bounds:
            raise StaleObservationError(
                "Screen geometry changed after observe. No input was sent; observe again."
            )
        current_dpi = _window_dpi(current_handle)
        if current_dpi != observation.dpi:
            raise StaleObservationError(
                "Display DPI changed after observe. No input was sent; observe again."
            )
        pointer_value = driver.position()
        current_pointer = (int(pointer_value.x), int(pointer_value.y))
        if (
            abs(current_pointer[0] - observation.pointer[0]) > 3
            or abs(current_pointer[1] - observation.pointer[1]) > 3
        ):
            raise StaleObservationError(
                "The mouse moved after observe, indicating user interference. No input was sent; observe again."
            )
        held_controls = self._physical_controls_down()
        if held_controls:
            raise StaleObservationError(
                "Physical keyboard modifiers or mouse buttons are held after observe "
                f"({', '.join(held_controls)}). No input was sent; observe again."
            )

        current_image = self._screen_capture()
        if not isinstance(current_image, Image.Image):
            raise RuntimeError("The desktop screenshot provider returned an invalid image")
        if delayed_uia and (
            observation.image.size != current_image.size
            or observation.image.mode != current_image.mode
            or observation.image.tobytes() != current_image.tobytes()
        ):
            raise StaleObservationError(
                "The delayed UIA target cannot be revalidated against an identical fresh screen. "
                "No input was sent; observe again."
            )
        global_change = self._screen_change_ratio(observation.image, current_image)
        prepared = dict(arguments)
        prepared["_expected_pointer"] = current_pointer
        prepared["_expected_foreground_handle"] = current_handle
        if action in {'key', 'type'}:
            prepared['_expected_focus'] = current_foreground.get('focus')
        target_source = "bound_screen_coordinates"
        element_id = str(arguments.get("element_id") or "")
        target_points: list[tuple[int, int]] = []

        if element_id:
            if action not in {"click", "double_click", "type"}:
                raise ActionRejectedError(
                    "element_id is supported for click, double_click, and type; use bound coordinates for this action"
                )
            target = observation.uia_targets.get(element_id)
            if target is None:
                raise StaleObservationError(
                    "The requested UIA element is not part of the latest observation. Observe again."
                )
            wrapper = target["wrapper"]
            element = dict(target["element"])
            if action == "type" and element.get("control_type") not in {"Document", "Edit"}:
                raise ActionRejectedError(
                    "Text input requires an Edit or Document UIA element"
                )
            try:
                # descendants() returns a concrete pywinauto wrapper, not a
                # WindowSpecification; wrappers intentionally have no exists().
                if not wrapper.is_visible():
                    raise StaleObservationError(
                        "The observed UIA target disappeared. No input was sent; observe again."
                    )
                if not wrapper.is_enabled():
                    raise ActionRejectedError("The observed UIA target is disabled")
                rect_value = wrapper.rectangle()
                current_rect = [
                    int(rect_value.left),
                    int(rect_value.top),
                    int(rect_value.right),
                    int(rect_value.bottom),
                ]
            except (StaleObservationError, ActionRejectedError):
                raise
            except Exception as exc:
                raise StaleObservationError(
                    f"The observed UIA target can no longer be resolved: {exc}"
                ) from exc
            if current_rect != list(element.get("rect") or []):
                raise StaleObservationError(
                    "The observed UIA target moved or resized. No input was sent; observe again."
                )
            center = (
                (current_rect[0] + current_rect[2]) // 2,
                (current_rect[1] + current_rect[3]) // 2,
            )
            self._bounded_point(driver, *center)
            prepared["x"], prepared["y"] = center
            prepared["_uia_wrapper"] = wrapper
            target_points.append(center)
            target_source = "uia_accessibility_element"
        elif action in {"click", "double_click"}:
            coordinate = self._bounded_point(
                driver,
                arguments.get("x"),
                arguments.get("y"),
            )
            for accessible in (() if arguments.get('_visual_fixed_coordinate') else observation.uia_targets.values()):
                element = accessible.get("element") or {}
                rect = list(element.get("rect") or [])
                if (
                    len(rect) == 4
                    and bool(element.get("enabled"))
                    and element.get("control_type")
                    in {
                        "Button",
                        "CheckBox",
                        "ComboBox",
                        "Hyperlink",
                        "ListItem",
                        "MenuItem",
                        "RadioButton",
                        "TabItem",
                        "TreeItem",
                    }
                    and rect[0] <= coordinate[0] < rect[2]
                    and rect[1] <= coordinate[1] < rect[3]
                ):
                    raise ActionRejectedError(
                        "An accessible UIA element covers these coordinates. Use its element_id instead of OCR coordinates."
                    )
            target_points.append(coordinate)
        elif action == "type":
            if "x" not in arguments or "y" not in arguments:
                raise ActionRejectedError(
                    "Text input requires an Edit/Document element_id, or x/y coordinates "
                    "when the visible field has no accessible UIA element"
                )
            coordinate = self._bounded_point(
                driver,
                arguments.get("x"),
                arguments.get("y"),
            )
            for accessible in (() if arguments.get('_visual_fixed_coordinate') else observation.uia_targets.values()):
                element = accessible.get("element") or {}
                rect = list(element.get("rect") or [])
                if (
                    len(rect) == 4
                    and bool(element.get("enabled"))
                    and element.get("control_type") in {"Document", "Edit"}
                    and rect[0] <= coordinate[0] < rect[2]
                    and rect[1] <= coordinate[1] < rect[3]
                ):
                    raise ActionRejectedError(
                        "An accessible text field covers these coordinates. Use its element_id."
                    )
            prepared["x"], prepared["y"] = coordinate
            target_points.append(coordinate)
            target_source = "bound_screen_text_coordinates"
        elif action == "scroll" and ("x" in arguments or "y" in arguments):
            target_points.append(
                self._bounded_point(driver, arguments.get("x"), arguments.get("y"))
            )
        elif action == "drag":
            for point in list(arguments.get("path") or []):
                if isinstance(point, dict):
                    target_points.append(
                        self._bounded_point(driver, point.get("x"), point.get("y"))
                    )

        if action == "key":
            keys = {str(value).strip().casefold() for value in arguments.get("keys") or []}
            if keys & {"esc", "escape"}:
                raise ActionRejectedError(
                    "Esc is reserved for the user's global emergency stop and cannot be sent by the model"
                )

        # Dynamic clocks and cursors may change a few distant pixels. Refuse a
        # globally different frame, and apply a much tighter comparison around
        # the intended coordinate target so moved/covered controls cannot be
        # clicked by stale OCR geometry.
        origin_x, origin_y, _screen_width, _screen_height = observation.virtual_bounds
        for x, y in target_points:
            pixel_x, pixel_y = x - origin_x, y - origin_y
            left = max(0, pixel_x - 48)
            top = max(0, pixel_y - 36)
            right = min(observation.image.width, pixel_x + 49)
            bottom = min(observation.image.height, pixel_y + 37)
            expected_patch = observation.image.crop((left, top, right, bottom))
            current_patch = current_image.crop((left, top, right, bottom))
            patch_change = self._screen_change_ratio(expected_patch, current_patch)
            if patch_change >= 0.035:
                raise StaleObservationError(
                    "The intended target region changed after observe. No input was sent; observe again."
                )
        if global_change >= 0.008:
            raise StaleObservationError(
                "The screen changed materially after observe (popup, occlusion, or content change). "
                "No input was sent; observe again."
            )
        return prepared, target_source, current_image

    @staticmethod
    def _ensure_not_stopped(session: _ControlSession) -> None:
        if session.stop_event.is_set():
            raise InterruptedError("The Live Computer Use session was stopped")

    @staticmethod
    def _ensure_action_alive(
        session: _ControlSession,
        arguments: dict[str, Any],
    ) -> None:
        LiveComputerUseController._ensure_not_stopped(session)
        check_input_permit()
        LiveComputerUseController._assert_foreground_unchanged(arguments)

    def _move_pointer_cooperatively(
        self,
        driver: Any,
        session: _ControlSession,
        arguments: dict[str, Any],
        destination: tuple[int, int],
        *,
        duration: float,
    ) -> None:
        """Move in short verified slices so Esc/human interference wins quickly."""

        current_value = driver.position()
        origin = (int(current_value.x), int(current_value.y))
        distance = math.hypot(destination[0] - origin[0], destination[1] - origin[1])
        steps = max(
            1,
            min(
                250,
                max(math.ceil(duration / 0.025), math.ceil(distance / 70)),
            ),
        )
        pause = duration / steps if duration > 0 else 0.0
        expected = origin
        for index in range(1, steps + 1):
            self._ensure_action_alive(session, arguments)
            self._assert_pointer_at(
                driver,
                expected,
                "The mouse was moved by the user during computer control",
            )
            fraction = index / steps
            point = (
                round(origin[0] + (destination[0] - origin[0]) * fraction),
                round(origin[1] + (destination[1] - origin[1]) * fraction),
            )
            self._set_pointer(driver, point)
            self._assert_pointer_at(
                driver,
                point,
                "The mouse was displaced during controlled movement",
            )
            expected = point
            if pause and index < steps and session.stop_event.wait(pause):
                raise InterruptedError("The Live Computer Use session was stopped")

    @staticmethod
    def _uses_native_windows_input(driver: Any) -> bool:
        return sys.platform == "win32" and str(getattr(driver, "__name__", "")) == "pyautogui"

    @classmethod
    def _set_pointer(cls, driver: Any, point: tuple[int, int]) -> None:
        if not cls._uses_native_windows_input(driver):
            driver.moveTo(*point, duration=0)
            return
        move_mouse(*point)

    @staticmethod
    def _wheel_delta_from_pixels(pixels: int) -> int:
        if pixels == 0:
            return 0
        magnitude = max(120, round(abs(pixels) * 1.2))
        return magnitude if pixels > 0 else -magnitude

    @classmethod
    def _send_scroll_pixels(cls, driver: Any, scroll_x: int, scroll_y: int) -> None:
        """Convert pixel-like computer-action deltas to native wheel deltas."""

        test_adapter = getattr(driver, "elren_scroll_pixels", None)
        if callable(test_adapter):
            test_adapter(scroll_x, scroll_y)
            return
        if not cls._uses_native_windows_input(driver):
            vertical_notches = -round(cls._wheel_delta_from_pixels(scroll_y) / 120)
            horizontal_notches = round(cls._wheel_delta_from_pixels(scroll_x) / 120)
            if vertical_notches:
                driver.scroll(vertical_notches)
            if horizontal_notches:
                hscroll = getattr(driver, "hscroll", None)
                if not callable(hscroll):
                    raise RuntimeError("Horizontal scrolling is unavailable on this host")
                hscroll(horizontal_notches)
            return

        scroll_mouse(
            horizontal_delta=cls._wheel_delta_from_pixels(scroll_x),
            vertical_delta=cls._wheel_delta_from_pixels(-scroll_y),
        )

    def _deliver_action(
        self,
        driver: Any,
        session: _ControlSession,
        action: str,
        arguments: dict[str, Any],
    ) -> None:
        button = str(arguments.get("button") or "left").casefold()
        if button not in {"left", "right", "middle"}:
            raise ValueError(f"Unsupported mouse button: {button}")
        if action in {"click", "double_click"}:
            x, y = self._bounded_point(driver, arguments.get("x"), arguments.get("y"))
            self._ensure_action_alive(session, arguments)
            self._assert_pointer_at(
                driver,
                tuple(arguments.get("_expected_pointer") or ()),
                "The mouse moved immediately before click",
            )
            self._move_pointer_cooperatively(
                driver,
                session,
                arguments,
                (x, y),
                duration=0.08,
            )
            self._ensure_action_alive(session, arguments)
            self._assert_no_external_controls_held()
            click_count = 2 if action == "double_click" else 1
            if self._uses_native_windows_input(driver):
                click_mouse(button, clicks=click_count)
            else:
                driver.click(x, y, clicks=click_count, button=button)
            self._ensure_not_stopped(session)
            self._assert_pointer_at(
                driver,
                (x, y),
                "The mouse was displaced during click",
            )
            return
        if action == "type":
            wrapper = arguments.get("_uia_wrapper")
            if wrapper is not None:
                self._ensure_action_alive(session, arguments)
                try:
                    wrapper.set_focus()
                except Exception as exc:
                    raise RuntimeError(f"The UIA text target could not receive focus: {exc}") from exc
                self._ensure_not_stopped(session)
                arguments["_expected_foreground_handle"] = int(
                    _foreground_window().get("window_handle") or 0
                )
                arguments['_expected_focus'] = _foreground_window().get('focus')
                expected_pointer = tuple(arguments.get("_expected_pointer") or ())
            else:
                x, y = self._bounded_point(
                    driver,
                    arguments.get("x"),
                    arguments.get("y"),
                )
                expected_pointer = tuple(arguments.get("_expected_pointer") or ())
                self._assert_pointer_at(
                    driver,
                    expected_pointer,
                    "The mouse moved immediately before focusing the text field",
                )
                self._move_pointer_cooperatively(
                    driver,
                    session,
                    arguments,
                    (x, y),
                    duration=0.08,
                )
                self._ensure_action_alive(session, arguments)
                if self._uses_native_windows_input(driver):
                    click_mouse("left", clicks=1)
                else:
                    driver.click(x, y, clicks=1, button="left")
                self._ensure_not_stopped(session)
                arguments["_expected_foreground_handle"] = int(
                    _foreground_window().get("window_handle") or 0
                )
                arguments['_expected_focus'] = _foreground_window().get('focus')
                expected_pointer = (x, y)
            text = str(arguments.get("text") or "")
            if len(text) > 8_000:
                raise ValueError("Live Computer Use type text is limited to 8,000 characters")
            for offset in range(0, len(text), 32):
                self._ensure_action_alive(session, arguments)
                self._assert_no_external_controls_held()
                self._assert_pointer_at(
                    driver,
                    expected_pointer,
                    "The user moved the mouse while text input was running",
                )
                type_unicode(text[offset : offset + 32])
                self._ensure_not_stopped(session)
            return
        if action == "key":
            keys = [str(value).strip().casefold() for value in arguments.get("keys") or []]
            if not keys or len(keys) > 8 or any(not key for key in keys):
                raise ValueError("key requires between one and eight non-empty key names")
            self._ensure_action_alive(session, arguments)
            self._assert_no_external_controls_held()
            self._assert_pointer_at(
                driver,
                tuple(arguments.get("_expected_pointer") or ()),
                "The mouse moved immediately before keyboard input",
            )
            if len(keys) == 1:
                if self._uses_native_windows_input(driver):
                    send_key_chord(keys)
                else:
                    driver.press(keys[0])
            else:
                if self._uses_native_windows_input(driver):
                    send_key_chord(keys)
                else:
                    driver.hotkey(*keys)
            self._ensure_not_stopped(session)
            return
        if action == "scroll":
            if "x" in arguments or "y" in arguments:
                x, y = self._bounded_point(driver, arguments.get("x"), arguments.get("y"))
                self._assert_pointer_at(
                    driver,
                    tuple(arguments.get("_expected_pointer") or ()),
                    "The mouse moved immediately before scroll",
                )
                self._move_pointer_cooperatively(
                    driver,
                    session,
                    arguments,
                    (x, y),
                    duration=0.08,
                )
                self._assert_pointer_at(driver, (x, y), "The mouse was displaced before scroll")
            else:
                self._assert_pointer_at(
                    driver,
                    tuple(arguments.get("_expected_pointer") or ()),
                    "The mouse moved immediately before scroll",
                )
            self._ensure_action_alive(session, arguments)
            self._assert_no_external_controls_held()
            scroll_y = int(arguments.get("scroll_y") or 0)
            scroll_x = int(arguments.get("scroll_x") or 0)
            if abs(scroll_y) > 12_000 or abs(scroll_x) > 12_000:
                raise ValueError("scroll_x and scroll_y are limited to 12,000 units per action")
            self._send_scroll_pixels(driver, scroll_x, scroll_y)
            self._ensure_not_stopped(session)
            return
        if action == "drag":
            raw_path = arguments.get("path") or []
            if not isinstance(raw_path, list) or not 2 <= len(raw_path) <= 64:
                raise ValueError("drag.path requires between two and 64 coordinate points")
            path = [
                self._bounded_point(driver, point.get("x"), point.get("y"))
                for point in raw_path
                if isinstance(point, dict)
            ]
            if len(path) != len(raw_path):
                raise ValueError("Every drag path item must contain integer x and y coordinates")
            duration = float(arguments.get("duration") or 0.6)
            if not math.isfinite(duration) or not 0.1 <= duration <= 5.0:
                raise ValueError("drag.duration must be between 0.1 and 5 seconds")
            segment_duration = duration / max(1, len(path) - 1)
            self._ensure_action_alive(session, arguments)
            self._assert_no_external_controls_held()
            self._assert_pointer_at(
                driver,
                tuple(arguments.get("_expected_pointer") or ()),
                "The mouse moved immediately before drag",
            )
            self._move_pointer_cooperatively(
                driver,
                session,
                arguments,
                path[0],
                duration=min(0.12, segment_duration),
            )
            self._assert_pointer_at(driver, path[0], "The mouse was displaced at drag start")
            self._ensure_action_alive(session, arguments)
            if self._uses_native_windows_input(driver):
                set_mouse_button(button, down=True)
            else:
                driver.mouseDown(button=button)
            try:
                for point in path[1:]:
                    self._assert_no_external_controls_held(
                        allowed_mouse_button=button,
                    )
                    self._move_pointer_cooperatively(
                        driver,
                        session,
                        arguments,
                        point,
                        duration=segment_duration,
                    )
                    self._assert_pointer_at(
                        driver,
                        point,
                        "The mouse was displaced during drag",
                    )
            finally:
                if self._uses_native_windows_input(driver):
                    set_mouse_button(button, down=False)
                else:
                    driver.mouseUp(button=button)
            self._ensure_not_stopped(session)
            return
        raise ValueError(f"Unsupported Live Computer Use action: {action}")

    @staticmethod
    def _assert_foreground_unchanged(arguments: dict[str, Any]) -> None:
        expected = int(arguments.get("_expected_foreground_handle") or 0)
        foreground = _foreground_window()
        current = int(foreground.get("window_handle") or 0)
        if expected != current:
            raise InterruptedError(
                "The foreground window changed during input; the session was stopped"
            )
        if '_expected_focus' in arguments and foreground.get('focus') != arguments['_expected_focus']:
            raise InterruptedError('Input focus changed during keyboard input; no further chunks sent')

    @staticmethod
    def _assert_pointer_at(
        driver: Any,
        expected: tuple[int, ...],
        message: str,
    ) -> None:
        if len(expected) != 2:
            raise RuntimeError("The bound pointer state is missing")
        current_value = driver.position()
        current = (int(current_value.x), int(current_value.y))
        if abs(current[0] - expected[0]) > 3 or abs(current[1] - expected[1]) > 3:
            raise InterruptedError(f"{message}; the session was stopped")

    def stop_with_lease(self, lease: str, task_id: str, reason: str) -> bool:
        session = self._require_session(lease, task_id)
        return self.emergency_stop(reason, expected_session_id=session.session_id)

    def stop_task(self, task_id: str, reason: str = "task_cleanup") -> bool:
        with self._state_lock:
            session = self._session
            session_id = session.session_id if session and session.task_id == task_id else ""
        if not session_id:
            return False
        return self.emergency_stop(reason, expected_session_id=session_id)

    def quiesce_task(self, task_id: str, reason: str) -> None:
        """Cancellation owner must not detach an input worker after a deadline."""
        with self._state_lock:
            session = self._session
            session_id = session.session_id if session and session.task_id == task_id else ''
        if not session_id:
            return
        while True:
            self.emergency_stop(reason, expected_session_id=session_id)
            with self._state_lock:
                if self._session is None or self._session.session_id != session_id:
                    return
            # A timed-out stop leaves the lease active/stopping. Retain ownership
            # and retry cleanup instead of claiming cancellation finished.
            time.sleep(.02)

    def emergency_stop(
        self,
        reason: str = "emergency_stop",
        *,
        expected_session_id: str = "",
    ) -> bool:
        with self._state_lock:
            session = self._session
            if session is None:
                return False
            if expected_session_id and session.session_id != expected_session_id:
                return False
            first_stop_request = not session.stopping_reason
            if first_stop_request:
                session.stopping_reason = str(reason or "stopped")[:80]
                if session.stopping_reason == "escape_key":
                    session.metrics.interference_stop_count += 1
            session.stop_event.set()

        # Do not report an inactive session while an already-started click,
        # key, scroll, type, or drag can still deliver late input. Cooperative
        # actions check stop_event in short slices and release this lock fast.
        if not self._action_lock.acquire(timeout=4.0):
            logger.error(
                "Live Computer Use input did not quiesce before the stop deadline",
                extra={"session_id": session.session_id, "reason": session.stopping_reason},
            )
            return False
        try:
            with self._state_lock:
                if self._session is not session:
                    return False
                self._session = None
                self._last_status = {
                    "available": True,
                    "active": False,
                    "session_id": session.session_id,
                    "task_id": session.task_id,
                    "started_at": session.started_at,
                    "stopped_at": _utc_now(),
                    "stop_reason": session.stopping_reason,
                    "metrics": session.metrics.payload(),
                }
        finally:
            self._action_lock.release()
        try:
            session.banner.stop()
        finally:
            if session.desktop_lease is not None:
                session.desktop_lease.close()
            monitor = session.monitor_thread
            if monitor and monitor is not threading.current_thread():
                monitor.join(timeout=1.0)
            for screenshot in list(session.screenshots):
                try:
                    screenshot.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug(
                        "Removing a stopped desktop-control screenshot failed",
                        exc_info=exc,
                    )
            session.screenshots.clear()
        return True


class LiveComputerUseTool(ToolPlugin):
    """OpenAI-style observe/action loop over the user's real visible desktop."""

    name = "live_computer_use"
    description = (
        "Lease-protected Live Computer Use for the user's real visible Windows desktop. "
        "Use ONLY when the CURRENT request explicitly needs foreground computer control or a logged-in "
        "visible application cannot be operated through windows_ui/background_browser. Call start, use its "
        "session_lease and observation_id for actions or an explicit sequence. Remote visual checks run before/after "
        "each complete action. Supply action_id for retry deduplication, optional target_description and expected_result; then "
        "stop. Do not use it for ordinary file, code, web, background UIA, CAPTCHA/MFA, credential, biometric, "
        "payment-confirmation, or system-permission steps. Existing computer_use and computer remain separate."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "start",
                    "status",
                    "observe",
                    "click",
                    "double_click",
                    "type",
                    "key",
                    "scroll",
                    "drag",
                    "stop",
                    "sequence",
                ],
            },
            "session_lease": {
                "type": "string",
                "description": "Ephemeral lease returned by start; required for every later tool action.",
            },
            "observation_id": {
                "type": "string",
                "description": (
                    "Exact latest observation_id; mandatory for every click, double_click, type, "
                    "key, scroll, and drag action. Stale ids are rejected before input."
                ),
            },
            "element_id": {
                "type": "string",
                "description": (
                    "UIA element_id from the latest observation. Alternatively supply exact physical x/y "
                    "or target_description for visual localization."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 3600,
                "description": "Idle expiry for start; default 600 seconds.",
            },
            "language": {"type": "string", "enum": ["auto", "zh", "en"]},
            "x": {
                "type": "integer",
                "description": "Physical virtual-desktop X; may be negative on a left-side monitor.",
            },
            "y": {
                "type": "integer",
                "description": "Physical virtual-desktop Y; may be negative on an upper monitor.",
            },
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "text": {"type": "string", "maxLength": 8000},
            "keys": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 8,
            },
            "scroll_x": {
                "type": "integer",
                "minimum": -12000,
                "maximum": 12000,
                "description": (
                    "Horizontal pixel-like intent converted to the native Windows wheel; "
                    "positive scrolls right. Re-observe after each step instead of guessing a page size."
                ),
            },
            "scroll_y": {
                "type": "integer",
                "minimum": -12000,
                "maximum": 12000,
                "description": (
                    "Vertical pixel-like intent converted to the native Windows wheel; "
                    "positive scrolls down. Re-observe after each step to account for inertia."
                ),
            },
            "path": {
                "type": "array",
                "minItems": 2,
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            },
            "duration": {"type": "number", "minimum": 0.1, "maximum": 5.0},
            "language_tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Optional installed Windows OCR language tags.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        screenshot_dir: Path,
        windows_ocr: WindowsOCR | None = None,
        *,
        controller: LiveComputerUseController | None = None,
        visual_judge: Any = None,
    ) -> None:
        self.controller = controller or LiveComputerUseController(screenshot_dir)
        self.windows_ocr = windows_ocr or WindowsOCR()
        from deepdesk.live_control_flow import LiveActionFlow
        from deepdesk.live_control_vision import LiveVisualJudge
        self.visual_flow = LiveActionFlow(self, visual_judge or LiveVisualJudge(list))
        # Extend this existing tool schema; no second tool/protocol is added.
        import copy
        self.parameters = copy.deepcopy(type(self).parameters)
        properties = self.parameters['properties']
        properties.update({
            'action_id': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                          'description': 'Stable call ID. Reuse only for transport retry of identical arguments.'},
            'target_description': {'type': 'string', 'maxLength': 2000},
            'expected_result': {'type': 'string', 'maxLength': 2000},
            'return_screenshot': {'type': 'boolean'},
            'return_elements': {'type': 'boolean'},
        })
        step_properties = {k: copy.deepcopy(v) for k, v in properties.items()
                           if k not in {'session_lease', 'timeout_seconds', 'language_tags', 'language'}}
        step_properties['action']['enum'] = sorted(_MUTATING_ACTIONS)
        properties['actions'] = {'type': 'array', 'minItems': 1, 'maxItems': 16,
            'items': {'type': 'object', 'properties': step_properties, 'required': ['action'], 'additionalProperties': False}}

    def risk(self, arguments: dict[str, Any]) -> Risk:
        action = str(arguments.get("action") or "")
        if action in {"status", "observe", "stop"}:
            return Risk.SAFE
        if action == "start":
            return Risk.MEDIUM
        return Risk.HIGH

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = str(arguments.get("action") or "")
        labels = {
            "start": "启动实时电脑控制（Esc 可随时急停）",
            "status": "读取实时电脑控制状态",
            "observe": "观察真实电脑屏幕",
            "click": "在真实电脑屏幕上点击",
            "double_click": "在真实电脑屏幕上双击",
            "type": "向真实电脑界面输入文本",
            "key": "向真实电脑界面发送按键",
            "scroll": "滚动真实电脑界面",
            "drag": "在真实电脑界面拖动",
            "stop": "停止实时电脑控制",
        }
        return labels.get(action, f"实时电脑控制：{action}")

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = str(arguments.get("action") or "")
        lease = str(arguments.get("session_lease") or "")
        try:
            if action == "start":
                if not allows_live_computer_use_start(
                    context.user_prompt,
                    context.agent_profile,
                ):
                    raise PermissionError(
                        "Live Computer Use can start only when the current request explicitly "
                        "requires real foreground computer control or uses the Computer Use profile"
                    )
                language = str(arguments.get("language") or "auto")
                if language == "auto":
                    language = (
                        "zh"
                        if any("\u3400" <= char <= "\u9fff" for char in context.user_prompt)
                        else "en"
                    )
                session = await self._controller_call(
                    self.controller.start,
                    context.task_id,
                    int(arguments.get("timeout_seconds") or 600),
                    language,
                )
                observation = await self._observe(
                    session["session_lease"],
                    context,
                    self._select_ocr_language_tags(
                        list(arguments.get("language_tags") or []),
                        context.user_prompt,
                    ),
                )
                return {**session, "observation": observation}
            if action == "status":
                if self.controller.public_status().get("active"):
                    status = await self._controller_call(
                        self.controller.status_with_lease,
                        lease,
                        context.task_id,
                    )
                    status['visual_metrics'] = self.visual_flow.metrics(context.task_id)
                    return status
                return self.controller.public_status()
            if action == "observe":
                return await self._observe(
                    lease,
                    context,
                    self._select_ocr_language_tags(
                        list(arguments.get("language_tags") or []),
                        context.user_prompt,
                    ),
                )
            if action == "stop":
                stopped = await asyncio.to_thread(
                    self.controller.stop_with_lease,
                    lease,
                    context.task_id,
                    "tool_stop",
                )
                self.visual_flow.release_task(context.task_id)
                return {"stopped": stopped, "status": self.controller.public_status()}
            if action not in _MUTATING_ACTIONS and action != 'sequence':
                raise ValueError(f"Unsupported Live Computer Use action: {action}")
            return await self.visual_flow.run(arguments, context)
        except asyncio.CancelledError:
            stop = asyncio.create_task(asyncio.to_thread(
                self.controller.quiesce_task, context.task_id, "task_cancelled",
            ))
            try:
                await finish_owned_work(stop)
            except Exception as error:
                # Keep the stop operation owned across repeated cancellation.
                # Controller errors may contain sensitive UI text; cancellation
                # still propagates, without logging that raw exception body.
                logger.warning("Computer-control cancellation cleanup failed (%s)",
                               type(error).__name__)
            raise
        except (StaleObservationError, ActionRejectedError, PermissionError):
            # A stale observation or invalid target is rejected before input.
            # Keep the lease alive so the model can immediately re-observe.
            raise
        except Exception:
            if action not in {"status", "stop"}:
                await asyncio.to_thread(
                    self.controller.stop_task,
                    context.task_id,
                    "tool_error",
                )
            raise

    @staticmethod
    def _validate_sensitive_key_action(
        action: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> None:
        if action != "key":
            return
        keys = {str(key).strip().casefold() for key in arguments.get("keys") or []}
        if keys == {"shift", "delete"} and not allows_permanent_delete(context.user_prompt):
            raise PermissionError(
                "Shift+Delete is permanent and requires an explicit current request for irreversible deletion"
            )

    async def _observe(
        self,
        lease: str,
        context: ToolContext,
        language_tags: list[str],
    ) -> dict[str, Any]:
        payload = await self._controller_call(
            self.controller.capture_observation,
            lease,
            context.task_id,
        )
        enriched = await self._enrich_observation(payload, language_tags)
        enriched["session"] = await self._controller_call(
            self.controller.validate_observation_session,
            lease,
            context.task_id,
            str(enriched.get("observation_id") or ""),
        )
        self.visual_flow.remember(lease, enriched, context.task_id)
        return enriched

    async def _controller_call(self, callback: Callable[..., Any], *args: Any) -> Any:
        """Run every COM/UIA operation on the controller's fixed worker."""

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.controller.worker, callback, *args)

    @staticmethod
    def _select_ocr_language_tags(requested: list[str], prompt: str) -> list[str]:
        """Use one primary OCR language and at most one default fallback.

        Explicit caller tags remain supported (up to the schema limit). Avoiding
        an empty list matters because Windows.Media.Ocr otherwise walks every
        installed language pack for every tile and multiplies action latency.
        """

        cleaned: list[str] = []
        for raw in requested:
            tag = str(raw or "").strip()
            if (
                tag
                and len(tag) <= 64
                and tag.replace("-", "").isalnum()
                and tag.casefold() not in {item.casefold() for item in cleaned}
            ):
                cleaned.append(tag)
            if len(cleaned) >= 8:
                break
        if cleaned:
            return cleaned
        if any("\u3400" <= character <= "\u9fff" for character in str(prompt or "")):
            return ["zh-Hans", "en-US"]
        return ["en-US"]

    @staticmethod
    def _tile_starts(length: int, tile_size: int, overlap: int) -> list[int]:
        if length <= tile_size:
            return [0]
        step = tile_size - overlap
        starts = list(range(0, max(1, length - tile_size + 1), step))
        last = length - tile_size
        if not starts or starts[-1] != last:
            starts.append(last)
        return starts

    async def _recognize_tiled(
        self,
        image_path: Path,
        language_tags: list[str],
    ) -> dict[str, Any]:
        """OCR an arbitrarily large virtual desktop without losing coordinates."""

        with Image.open(image_path) as opened:
            image = opened.convert("RGB").copy()
        tile_size = 2_200
        overlap = 96
        x_starts = self._tile_starts(image.width, tile_size, overlap)
        y_starts = self._tile_starts(image.height, tile_size, overlap)
        if len(x_starts) == 1 and len(y_starts) == 1:
            raw = await self.windows_ocr.recognize(
                image_path,
                language_tags=language_tags,
            )
            raw["tile_count"] = 1
            return raw

        combined: dict[str, dict[str, Any]] = {}
        seen_lines: dict[str, set[tuple[Any, ...]]] = {}
        errors: list[str] = []
        source = "Windows.Media.Ocr"
        successful_tiles = 0
        for top in y_starts:
            for left in x_starts:
                right = min(image.width, left + tile_size)
                bottom = min(image.height, top + tile_size)
                temporary_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        prefix=".elren-ocr-tile-",
                        suffix=".png",
                        dir=image_path.parent,
                        delete=False,
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                    image.crop((left, top, right, bottom)).save(
                        temporary_path,
                        format="PNG",
                    )
                    raw = await self.windows_ocr.recognize(
                        temporary_path,
                        language_tags=language_tags,
                    )
                    successful_tiles += 1
                    source = str(raw.get("source") or source)
                    for candidate in list(raw.get("candidates") or []):
                        language = str(candidate.get("language") or "unknown")
                        entry = combined.setdefault(
                            language,
                            {"language": language, "lines": [], "text_parts": []},
                        )
                        language_seen = seen_lines.setdefault(language, set())
                        for line in list(candidate.get("lines") or []):
                            translated_words: list[dict[str, Any]] = []
                            for word in list(line.get("words") or []):
                                box = list(word.get("box") or [])[:4]
                                text = str(word.get("text") or "").strip()
                                if not text or len(box) != 4:
                                    continue
                                translated_words.append(
                                    {
                                        "text": text,
                                        "box": [
                                            round(float(box[0]) + left, 2),
                                            round(float(box[1]) + top, 2),
                                            round(float(box[2]), 2),
                                            round(float(box[3]), 2),
                                        ],
                                    }
                                )
                            line_text = str(line.get("text") or "").strip()
                            if not translated_words and not line_text:
                                continue
                            first_box = (
                                translated_words[0]["box"]
                                if translated_words
                                else [float(left), float(top), 0.0, 0.0]
                            )
                            line_key = (
                                line_text.casefold(),
                                round(float(first_box[0]) / 8),
                                round(float(first_box[1]) / 6),
                            )
                            if line_key in language_seen:
                                continue
                            language_seen.add(line_key)
                            entry["lines"].append(
                                {"text": line_text, "words": translated_words}
                            )
                            if line_text:
                                entry["text_parts"].append(line_text)
                except Exception as exc:
                    errors.append(
                        f"tile({left},{top},{right},{bottom}): {type(exc).__name__}: {exc}"
                    )
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except OSError as exc:
                            logger.debug("Removing a temporary OCR tile failed", exc_info=exc)

        if successful_tiles == 0:
            raise RuntimeError(errors[0] if errors else "No OCR tile completed")
        candidates: list[dict[str, Any]] = []
        for entry in combined.values():
            text = "\n".join(entry.pop("text_parts", []))
            entry["text"] = text
            entry["text_length"] = len(text)
            candidates.append(entry)
        primary = max(candidates, key=lambda item: int(item.get("text_length") or 0), default={})
        return {
            "ok": True,
            "source": source,
            "image": {"width": image.width, "height": image.height},
            "primary_language": str(primary.get("language") or ""),
            "text": str(primary.get("text") or ""),
            "candidates": candidates,
            "tile_count": len(x_starts) * len(y_starts),
            "successful_tile_count": successful_tiles,
            "partial_errors": errors[:4],
        }

    async def _enrich_observation(
        self,
        payload: dict[str, Any],
        language_tags: list[str],
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            raw = await self._recognize_tiled(
                Path(payload["screenshot"]),
                language_tags,
            )
            candidates = list(raw.get("candidates") or [])
            primary_language = str(raw.get("primary_language") or "")
            primary = next(
                (
                    candidate
                    for candidate in candidates
                    if str(candidate.get("language") or "") == primary_language
                ),
                candidates[0] if candidates else {},
            )
            elements: list[dict[str, Any]] = []
            seen_elements: set[tuple[Any, ...]] = set()
            coordinate_origin = list(payload.get("coordinate_origin") or [0, 0])
            origin_x = int(coordinate_origin[0])
            origin_y = int(coordinate_origin[1])
            named_uia_rects = [
                list(element.get("rect") or [])
                for element in list(payload.get("uia_elements") or [])
                if str(element.get("name") or "").strip()
                and len(list(element.get("rect") or [])) == 4
            ]
            for line in list(primary.get("lines") or [])[:200]:
                for word in list(line.get("words") or [])[:100]:
                    text = str(word.get("text") or "").strip()
                    box = list(word.get("box") or [])[:4]
                    if text and len(box) == 4:
                        image_box = [round(float(value), 2) for value in box]
                        center_x = image_box[0] + image_box[2] / 2 + origin_x
                        center_y = image_box[1] + image_box[3] / 2 + origin_y
                        if any(
                            rect[0] <= center_x < rect[2]
                            and rect[1] <= center_y < rect[3]
                            for rect in named_uia_rects
                        ):
                            continue
                        element_key = (
                            text.casefold(),
                            round(image_box[0] / 4),
                            round(image_box[1] / 4),
                            round(image_box[2] / 4),
                            round(image_box[3] / 4),
                        )
                        if element_key in seen_elements:
                            continue
                        seen_elements.add(element_key)
                        elements.append(
                            {
                                "text": text,
                                "image_box": image_box,
                                "screen_box": [
                                    round(image_box[0] + origin_x, 2),
                                    round(image_box[1] + origin_y, 2),
                                    image_box[2],
                                    image_box[3],
                                ],
                            }
                        )
                    if len(elements) >= 160:
                        break
                if len(elements) >= 160:
                    break
            payload["ocr"] = {
                "ready": True,
                "source": str(raw.get("source") or "Windows.Media.Ocr"),
                "primary_language": primary_language,
                "text": str(raw.get("text") or "")[:4_000],
                "elements": elements,
                "tile_count": int(raw.get("tile_count") or 1),
                "successful_tile_count": int(raw.get("successful_tile_count") or 1),
                "partial_errors": list(raw.get("partial_errors") or []),
            }
        except Exception as exc:
            payload["ocr"] = {
                "ready": False,
                "source": "Windows.Media.Ocr",
                "text": "",
                "elements": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        payload["ocr_ms"] = round((time.monotonic() - started) * 1_000, 2)
        payload["next_step"] = (
            "Use OCR element boxes and the screenshot to choose exactly one action, then inspect "
            "that action's returned observation. Stop when the visible goal is verified."
        )
        payload["serialized_observation_bytes"] = len(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        )
        return payload

    async def cleanup(self, context: ToolContext) -> None:
        await asyncio.to_thread(
            self.controller.stop_task,
            context.task_id,
            "task_cleanup",
        )
        self.visual_flow.release_task(context.task_id)

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.controller.emergency_stop, "application_shutdown")
        await asyncio.to_thread(
            self.controller.worker.shutdown,
            wait=True,
            cancel_futures=True,
        )
        self.visual_flow.observations.clear()
        self.visual_flow.owners.clear()
        self.visual_flow.cache.clear()
        self.visual_flow.receipts.clear()
