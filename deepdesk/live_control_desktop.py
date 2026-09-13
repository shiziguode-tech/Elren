"""Cross-process desktop ownership for live control only; no global policy edits."""
import ctypes
import threading
from ctypes import wintypes


def display_layout():
    """Physical monitor rectangles; DPI is separately reported per window.

    Do not call GetDpiForMonitor on our per-monitor-aware input thread or assume
    that a foreign window's DPI equals the physical monitor/browser CSS scale.
    """
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    user.EnumDisplayMonitors.restype = wintypes.BOOL
    monitors = []
    @callback_type
    def collect(handle, _dc, rect_pointer, _data):
        rect=rect_pointer.contents
        monitors.append({'monitor_id':int(handle), 'rect':[rect.left,rect.top,rect.right,rect.bottom]})
        return True
    if not user.EnumDisplayMonitors(None,None,collect,0):
        raise RuntimeError('Display enumeration unavailable')
    return sorted(monitors,key=lambda item:item['monitor_id'])


class DesktopLease:
    def __init__(self, *, name='Local\\Elren.LiveComputerUse.Desktop.v1'):
        self.lock = threading.Lock()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateSemaphoreW.argtypes = [ctypes.c_void_p, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR]
        self.kernel.CreateSemaphoreW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseSemaphore.argtypes = [wintypes.HANDLE, wintypes.LONG, ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # Local namespace isolates Windows logon sessions (all their desktops
        # conservatively share this lease). Unlike a
        # mutex, a semaphore can be released by the cancellation cleanup thread.
        handle = self.kernel.CreateSemaphoreW(None, 1, 1, name)
        if not handle:
            raise RuntimeError('Cannot create desktop control lease')
        if self.kernel.WaitForSingleObject(handle, 0) != 0:
            self.kernel.CloseHandle(handle)
            raise RuntimeError('Another Elren process owns desktop input')
        self.handle = handle

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.ReleaseSemaphore(self.handle, 1, None)
                self.kernel.CloseHandle(self.handle)
                self.handle = None
