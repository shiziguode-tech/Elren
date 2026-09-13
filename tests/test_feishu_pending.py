import asyncio

import pytest

from deepdesk.feishu_pending import PendingFeishuAttachments


@pytest.mark.asyncio
async def test_pending_attachment_is_joined_to_next_text_without_expiring():
    pending = PendingFeishuAttachments(delay_seconds=0.05)
    expired = []

    async def on_expire(entry):
        expired.append(entry)

    assert await pending.stage(
        "open_id:ou_user",
        ["first.doc"],
        {"image_count": 0},
        on_expire,
    ) is True
    entry = pending.take("open_id:ou_user")
    assert entry["attachments"] == ["first.doc"]
    await asyncio.sleep(0.08)
    assert expired == []


@pytest.mark.asyncio
async def test_multiple_file_events_accumulate_and_expire_once():
    pending = PendingFeishuAttachments(delay_seconds=0.03)
    expired = []

    async def on_expire(entry):
        expired.append(entry)

    await pending.stage("open_id:ou_user", ["first.doc"], {"image_count": 0}, on_expire)
    await asyncio.sleep(0.01)
    assert await pending.stage(
        "open_id:ou_user",
        ["second.png"],
        {"image_count": 1},
        on_expire,
    ) is False
    await asyncio.sleep(0.06)

    assert len(expired) == 1
    assert expired[0]["attachments"] == ["first.doc", "second.png"]
    assert expired[0]["metadata"]["image_count"] == 1
