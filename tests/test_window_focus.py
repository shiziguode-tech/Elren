from __future__ import annotations

import pytest

from deepdesk.window_focus import focus_window


@pytest.mark.asyncio
async def test_missing_takeover_target_returns_localizable_status_code() -> None:
    result = await focus_window("  ")
    assert result == {
        "focused": False,
        "code": "target_window_missing",
        "message": "模型未提供目标窗口，请手动切换到需要操作的窗口",
    }
