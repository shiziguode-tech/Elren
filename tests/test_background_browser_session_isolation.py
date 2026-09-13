"""Real headless browser regression; only synthetic pages and isolated profiles."""
from pathlib import Path

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool


@pytest.mark.asyncio
async def test_independent_tasks_unicode_and_cleanup(tmp_path):
    tool = BackgroundBrowserTool(workspace=tmp_path, screenshot_dir=tmp_path / 'shots')
    if tool._packaged_browser_executable() is None:
        pytest.skip('Packaged headless runtime required')
    first = ToolContext(task_id='isolated-first', workspace=str(tmp_path))
    second = ToolContext(task_id='isolated-second', workspace=str(tmp_path))
    html = '''<meta charset="utf-8"><label>输入 / Input<input id="text"></label>
        <button id="send">Submit</button><output id="result"></output>
        <script>document.querySelector('#send').onclick=()=>{
        document.querySelector('#result').textContent=document.querySelector('#text').value;
        };</script>'''
    try:
        browser_a, page_a = await tool._session(first)
        browser_b, page_b = await tool._session(second)
        assert browser_a is not browser_b
        await page_a.set_content(html)
        await page_b.set_content(html)
        text = '中文 English 123 — 标点 <>& " 🧪'
        await tool.execute({'action': 'fill', 'selector': '#text', 'text': text}, first)
        result = await tool.execute({'action': 'click', 'selector': '#send', 'capture': True}, first)
        assert await page_a.locator('#result').text_content() == text
        assert await page_b.locator('#text').input_value() == ''
        assert await page_b.locator('#result').text_content() == ''
        assert Path(result['screenshot']).is_file()
        await tool.cleanup(first)
        assert not browser_a.is_connected()
        assert browser_b.is_connected()
        await tool.execute({'action': 'fill', 'selector': '#text', 'text': 'second still works'}, second)
        assert await page_b.locator('#text').input_value() == 'second still works'
    finally:
        await tool.cleanup(first)
        await tool.cleanup(second)
    assert not tool._sessions
    assert tool._playwright is None
