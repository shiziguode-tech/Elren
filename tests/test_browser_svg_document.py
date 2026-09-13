from pathlib import Path

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool


@pytest.mark.asyncio
@pytest.mark.parametrize('filename,content,expected', [
    ('中文 空格.svg', '<svg xmlns="http://www.w3.org/2000/svg"><style>text{fill:red}</style><text x="10" y="30">SVG 文本</text></svg>', 'SVG 文本'),
    ('empty.svg', '<svg xmlns="http://www.w3.org/2000/svg"><rect width="50" height="50"/></svg>', ''),
    ('page.html', '<html><body>Hello HTML</body></html>', 'Hello HTML'),
])
async def test_local_document_snapshot_without_body_timeout(tmp_path, filename, content, expected):
    path = tmp_path / filename
    path.write_text(content, encoding='utf-8')
    tool = BackgroundBrowserTool(workspace=tmp_path, screenshot_dir=tmp_path / 'shots')
    if tool._packaged_browser_executable() is None:
        pytest.skip('Set ELREN_BROWSER_RUNTIME to the packaged browser runtime')
    context = ToolContext(task_id='svg-document-regression', workspace=str(tmp_path))
    try:
        result = await tool.execute({'action': 'open', 'url': path.as_uri(), 'capture': True}, context)
        assert result['text'].strip() == expected
        assert Path(result['screenshot']).is_file()
        again = await tool.execute({'action': 'inspect'}, context)
        assert again['text'] == result['text']
    finally:
        await tool.cleanup(context)
