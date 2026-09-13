from __future__ import annotations

import asyncio

from deepdesk.windows_activity import check_input_permit, foreground_input_scope


async def focus_window(title_hint: str) -> dict[str, object]:
    if not title_hint.strip():
        return {
            "focused": False,
            "code": "target_window_missing",
            "message": "模型未提供目标窗口，请手动切换到需要操作的窗口",
        }
    return await asyncio.to_thread(_focus_window_sync, title_hint.strip())


def _focus_window_sync(title_hint: str) -> dict[str, object]:
    try:
        from pywinauto import Desktop

        hint = title_hint.casefold()
        candidates = []
        for window in Desktop(backend="uia").windows():
            title = (window.window_text() or "").strip()
            if title and hint in title.casefold():
                candidates.append((window, title))
        if not candidates:
            return {
                "focused": False,
                "code": "target_window_not_found",
                "title_hint": title_hint,
                "message": f"未找到标题包含“{title_hint}”的窗口，请手动切换",
            }
        window, title = candidates[0]
        with foreground_input_scope():
            if window.is_minimized():
                window.restore()
            check_input_permit()
            window.set_focus()
        return {
            "focused": True,
            "code": "target_window_focused",
            "window": title,
            "message": "目标窗口已聚焦",
        }
    except Exception as exc:
        return {
            "focused": False,
            "code": "target_window_focus_failed",
            "detail": str(exc),
            "message": f"窗口聚焦失败，请手动切换：{exc}",
        }
