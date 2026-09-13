"""Passive user-activity gate for foreground input, not a key logger.

Only activity times/generations are retained. No key values, text, coordinates,
or input history are stored. Hooks always pass events through unchanged. Inputs
from other automation/remote-control processes count as user activity too.
"""
from __future__ import annotations

import contextlib
import ctypes
import secrets
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

IDLE_SECONDS = 5.0
INPUT_TAG = secrets.randbits(31) | 0x40000000


class UserInputActive(InterruptedError):
    pass


class ActivityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ActivitySnapshot:
    generation: int
    idle_seconds: float


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM)]


class _MouseEvent(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", wintypes.WPARAM)]


class WindowsActivityMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._generation = 0
        self._last_external = time.monotonic()
        self._observed_tick = 0
        self._heartbeat = 0.0
        self._failed = False
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._thread_id = 0
        self._thread = threading.Thread(target=self._run, name="elren-user-activity", daemon=True)
        self._thread.start()
        if not self._ready.wait(2) or self._failed:
            self.close()
            raise ActivityUnavailable("User activity detection unavailable; no foreground input is allowed")

    def _record(self, *, injected: bool, tag: int, tick: int):
        with self._lock:
            self._observed_tick = tick
            if not (injected and tag == INPUT_TAG):
                self._generation += 1
                self._last_external = time.monotonic()

    def _last_input(self) -> int:
        info = _LastInput(cbSize=ctypes.sizeof(_LastInput))
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            raise ActivityUnavailable("Cannot read user activity; foreground input blocked")
        return int(info.dwTime)

    def _run(self):
        hooks = []
        timer = 0
        try:
            if sys.platform != "win32":
                raise ActivityUnavailable("Windows activity monitoring requires Windows")
            u = self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            u.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_type, wintypes.HINSTANCE, wintypes.DWORD]
            u.SetWindowsHookExW.restype = wintypes.HANDLE
            u.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            u.CallNextHookEx.restype = ctypes.c_ssize_t
            u.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
            u.UnhookWindowsHookEx.restype = wintypes.BOOL
            u.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInput)]
            u.GetLastInputInfo.restype = wintypes.BOOL
            u.GetAsyncKeyState.argtypes = [ctypes.c_int]
            u.GetAsyncKeyState.restype = ctypes.c_short
            u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u.GetMessageW.restype = wintypes.BOOL
            u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostThreadMessageW.restype = wintypes.BOOL
            u.SetTimer.argtypes = [wintypes.HWND, wintypes.WPARAM, wintypes.UINT, ctypes.c_void_p]
            u.SetTimer.restype = wintypes.WPARAM
            u.KillTimer.argtypes = [wintypes.HWND, wintypes.WPARAM]
            u.KillTimer.restype = wintypes.BOOL
            k.GetCurrentThreadId.restype = wintypes.DWORD
            k.GetTickCount64.restype = ctypes.c_ulonglong
            k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            k.GetModuleHandleW.restype = wintypes.HMODULE
            self._thread_id = k.GetCurrentThreadId()
            self._observed_tick = self._last_input()
            age_ms = ((int(k.GetTickCount64()) & 0xFFFFFFFF) - self._observed_tick) & 0xFFFFFFFF
            self._last_external = time.monotonic() - age_ms / 1000

            def callback(event_type, injected_bit):
                @hook_type
                def observe(code, wparam, lparam):
                    if code >= 0:
                        try:
                            event = ctypes.cast(lparam, ctypes.POINTER(event_type)).contents
                            # Do not read vkCode, scanCode, mouseData, or pt.
                            self._record(injected=bool(event.flags & injected_bit),
                                         tag=int(event.dwExtraInfo), tick=int(event.time))
                        except BaseException:
                            self._failed = True
                    return u.CallNextHookEx(None, code, wparam, lparam)
                return observe

            # Keep callbacks alive until both hooks have been uninstalled.
            callbacks = [callback(_KeyboardEvent, 0x10), callback(_MouseEvent, 0x01)]
            for hook_id, fn in zip((13, 14), callbacks):
                handle = u.SetWindowsHookExW(hook_id, fn, k.GetModuleHandleW(None), 0)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(handle)
            timer = u.SetTimer(None, 0, 100, None)
            if not timer:
                raise ctypes.WinError(ctypes.get_last_error())
            self._heartbeat = time.monotonic()
            self._ready.set()
            message = wintypes.MSG()
            while not self._closed.is_set():
                result = u.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                self._heartbeat = time.monotonic()
        except BaseException:
            self._failed = True
        finally:
            if timer:
                self._user32.KillTimer(None, timer)
            for handle in hooks:
                self._user32.UnhookWindowsHookEx(handle)
            self._failed = True
            self._ready.set()

    def snapshot(self) -> ActivitySnapshot:
        if self._failed or not self._thread.is_alive() or time.monotonic() - self._heartbeat > 1:
            raise ActivityUnavailable("User activity monitor is not healthy; foreground input blocked")
        last_tick = self._last_input()
        with self._lock:
            # Detect input missed by a hook (including a silently removed hook).
            # An ambiguous ordering is rejected conservatively, never ignored.
            if last_tick != self._observed_tick:
                self._generation += 1
                self._last_external = time.monotonic()
                self._observed_tick = last_tick
            return ActivitySnapshot(self._generation, max(0.0, time.monotonic() - self._last_external))

    def controls_held(self) -> bool:
        # High bit only: ordinary held keys count, not just Ctrl/Alt/Shift.
        return any(int(self._user32.GetAsyncKeyState(code)) & 0x8000 for code in range(1, 255))

    def close(self):
        self._closed.set()
        if self._thread_id and hasattr(self, "_user32"):
            self._user32.PostThreadMessageW(self._thread_id, 0x12, 0, 0)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)


@dataclass(frozen=True)
class InputPermit:
    monitor: WindowsActivityMonitor
    generation: int

    def check(self):
        if self.monitor.snapshot().generation != self.generation:
            raise UserInputActive("User input resumed; stopped before sending more foreground input")


_monitor = None
_monitor_lock = threading.Lock()
_input_lock = threading.Lock()
_local = threading.local()


def get_activity_monitor() -> WindowsActivityMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = WindowsActivityMonitor()
        return _monitor


def check_input_permit():
    permit = getattr(_local, "permit", None)
    if permit is not None:
        permit.check()


@contextlib.contextmanager
def foreground_input_scope():
    """No waiting while owning focus: refuse until five seconds idle, then retry."""
    if sys.platform != "win32":
        yield
        return
    if getattr(_local, "permit", None) is not None:
        check_input_permit()
        yield
        return
    if not _input_lock.acquire(blocking=False):
        raise UserInputActive("Another foreground action is already running")
    try:
        monitor = get_activity_monitor()
        snapshot = monitor.snapshot()
        if snapshot.idle_seconds < IDLE_SECONDS or monitor.controls_held():
            raise UserInputActive("User is active; wait for 5 continuous idle seconds before foreground control")
        _local.permit = InputPermit(monitor, snapshot.generation)
        check_input_permit()
        yield
    finally:
        _local.permit = None
        _input_lock.release()
