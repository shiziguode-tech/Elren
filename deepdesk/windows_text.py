from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from deepdesk.windows_activity import INPUT_TAG, check_input_permit, foreground_input_scope

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


def utf16_code_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]


@foreground_input_scope()
def type_unicode(text: str, interval: float = 0.0) -> None:
    """Type literal Unicode with SendInput, independent of the active keyboard layout/IME."""
    if not text:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    units = utf16_code_units(text)
    for offset in range(0, len(units), 32):
        check_input_permit()
        chunk = units[offset : offset + 32]
        inputs: list[_Input] = []
        for unit in chunk:
            inputs.append(
                _Input(
                    type=INPUT_KEYBOARD,
                    ki=_KeyboardInput(wScan=unit, dwFlags=KEYEVENTF_UNICODE, dwExtraInfo=INPUT_TAG),
                )
            )
            inputs.append(
                _Input(
                    type=INPUT_KEYBOARD,
                    ki=_KeyboardInput(
                        wScan=unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        dwExtraInfo=INPUT_TAG,
                    ),
                )
            )
        array = (_Input * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
        if sent != len(inputs):
            error = ctypes.get_last_error()
            releases = [item for item in inputs if item.ki.dwFlags & KEYEVENTF_KEYUP]
            release_array = (_Input * len(releases))(*releases)
            user32.SendInput(len(releases), release_array, ctypes.sizeof(_Input))
            raise ctypes.WinError(error)
        if interval and offset + len(chunk) < len(units):
            time.sleep(interval * len(chunk))


def send_mouse_input(flags: int, *, data: int = 0) -> None:
    """Send one checked native mouse event; raise when Windows/UIPI rejects it."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    item = _Input(
        type=INPUT_MOUSE,
        mi=_MouseInput(
            mouseData=int(data) & 0xFFFFFFFF,
            dwFlags=int(flags),
            dwExtraInfo=INPUT_TAG,
        ),
    )
    sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input))
    if sent != 1:
        raise ctypes.WinError(ctypes.get_last_error())


@foreground_input_scope()
def click_mouse(button: str = "left", *, clicks: int = 1) -> None:
    pairs = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    try:
        down, up = pairs[button]
    except KeyError as exc:
        raise ValueError(f"Unsupported mouse button: {button}") from exc
    for index in range(clicks):
        check_input_permit()
        try:
            send_mouse_input(down)
        finally:
            # Cleanup releases must remain possible after user interruption.
            send_mouse_input(up)
        if index + 1 < clicks:
            time.sleep(0.06)


def set_mouse_button(button: str, *, down: bool) -> None:
    pairs = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    try:
        flags = pairs[button][0 if down else 1]
    except KeyError as exc:
        raise ValueError(f"Unsupported mouse button: {button}") from exc
    if down:
        check_input_permit()
    send_mouse_input(flags)


@foreground_input_scope()
def scroll_mouse(*, horizontal_delta: int = 0, vertical_delta: int = 0) -> None:
    check_input_permit()
    if vertical_delta:
        send_mouse_input(MOUSEEVENTF_WHEEL, data=vertical_delta)
    if horizontal_delta:
        send_mouse_input(MOUSEEVENTF_HWHEEL, data=horizontal_delta)


_VIRTUAL_KEYS = {
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "space": 0x20,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "win": 0x5B,
    "winleft": 0x5B,
    "winright": 0x5C,
    "numlock": 0x90,
    "scrolllock": 0x91,
}


def _virtual_key(name: str) -> int:
    normalized = str(name).strip().casefold()
    if normalized in _VIRTUAL_KEYS:
        return _VIRTUAL_KEYS[normalized]
    if normalized.startswith("f") and normalized[1:].isdigit():
        number = int(normalized[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    if len(normalized) == 1:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
        user32.VkKeyScanW.restype = ctypes.c_short
        value = int(user32.VkKeyScanW(normalized))
        if value != -1:
            if value & 0xFF00:
                raise ValueError(
                    "This character requires explicit modifier keys and its base key "
                    "(for example shift + 1), or use literal Unicode typing. "
                    "No input was sent."
                )
            return value & 0xFF
    raise ValueError(f"Unsupported key name: {name}")


@foreground_input_scope()
def send_key_chord(keys: list[str]) -> None:
    """Send a checked virtual-key press/chord with SendInput."""

    if not keys:
        raise ValueError("At least one key is required")
    codes = [_virtual_key(key) for key in keys]
    inputs = [
        _Input(type=INPUT_KEYBOARD, ki=_KeyboardInput(wVk=code, dwExtraInfo=INPUT_TAG)) for code in codes
    ]
    inputs.extend(
        _Input(
            type=INPUT_KEYBOARD,
            ki=_KeyboardInput(wVk=code, dwFlags=KEYEVENTF_KEYUP, dwExtraInfo=INPUT_TAG),
        )
        for code in reversed(codes)
    )
    array = (_Input * len(inputs))(*inputs)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    check_input_permit()
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        release = (_Input * len(codes))(*inputs[len(codes):])
        user32.SendInput(len(codes), release, ctypes.sizeof(_Input))
        raise ctypes.WinError(error)


@foreground_input_scope()
def move_mouse(x: int, y: int) -> None:
    """Tagged absolute movement over the physical virtual desktop, including negative origins."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int
    left, top, width, height = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
        raise ValueError("Mouse target is outside the virtual desktop")
    # Pixel centers avoid off-by-one rounding on scaled/multi-monitor desktops.
    dx = min(65535, int((x - left + 0.5) * 65536 / width))
    dy = min(65535, int((y - top + 0.5) * 65536 / height))
    item = _Input(type=INPUT_MOUSE, mi=_MouseInput(dx=dx, dy=dy, dwFlags=0x0001 | 0x8000 | 0x4000,
                                               dwExtraInfo=INPUT_TAG))
    u.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int)
    u.SendInput.restype = wintypes.UINT
    check_input_permit()
    if u.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())


@foreground_input_scope()
def click_at(x: int, y: int, *, button: str = "left") -> None:
    move_mouse(int(x), int(y))
    click_mouse(button)
