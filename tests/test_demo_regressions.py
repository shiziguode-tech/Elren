import asyncio
import json
from pathlib import Path

import pytest

from deepdesk.browser_downloads import BrowserDownloads
from deepdesk.command_safety import accesses_sensitive_environment
from deepdesk.harness import delegation_batch_needs_separation
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool
from deepdesk.plugins.builtin.filesystem import FileSystemTool


@pytest.mark.parametrize('name', ['PYTHONIOENCODING', 'PYTHONUTF8'])
def test_encoding_environment_is_not_a_credential(name):
    assert not accesses_sensitive_environment(f"$env:{name}='utf-8'; python --version")
    assert accesses_sensitive_environment(f"$env:{name}=$env:OPENAI_API_KEY")


def test_whole_environment_and_secrets_still_denied():
    assert accesses_sensitive_environment('Get-ChildItem env:')
    assert accesses_sensitive_environment('$env:UNRECOGNIZED_SECRET')


@pytest.mark.parametrize('action,blocked', [('map', False), ('read', False), ('search', False), ('write', True), ('delete', True)])
def test_readonly_delegation_batch(action, blocked):
    calls = [{'function': {'name': 'delegate_specialists', 'arguments': '{}'}},
             {'function': {'name': 'filesystem', 'arguments': json.dumps({'action': action})}}]
    assert delegation_batch_needs_separation(calls) == blocked


def test_unknown_tool_still_separated():
    calls = [{'function': {'name': 'delegate_specialists'}}, {'function': {'name': 'shell'}}]
    assert delegation_batch_needs_separation(calls)


def test_workspace_boundary_has_actionable_recovery(tmp_path):
    with pytest.raises(PermissionError, match='Open/select'):
        FileSystemTool._resolve(str(tmp_path), '../outside.py')
    assert FileSystemTool._resolve(str(tmp_path), 'inside.py') == tmp_path/'inside.py'


class Download:
    suggested_filename = '../报告.csv'

    def __init__(self, path, delay=0):
        self.source, self.delay, self.cancelled = path, delay, False

    async def path(self):
        await asyncio.sleep(self.delay)
        return str(self.source)

    async def save_as(self, path):
        Path(path).write_bytes(self.source.read_bytes())

    async def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_download_names_unique_and_survive_cleanup(tmp_path):
    source=tmp_path/'source.csv'
    source.write_text('title,status\n中文,done\n', encoding='utf-8')
    manager=BrowserDownloads(str(tmp_path), '../../task')
    manager.receive(Download(source))
    manager.receive(Download(source))
    records=await manager.collect()
    assert len(records)==2 and all(r['status']=='saved' for r in records)
    assert records[0]['path']!=records[1]['path']
    assert all(Path(r['path']).is_relative_to(tmp_path/'outputs') for r in records)
    await manager.close()
    assert Path(records[0]['path']).read_bytes()==source.read_bytes()
    assert await manager.collect()==records


@pytest.mark.asyncio
async def test_download_size_timeout_and_cancel(tmp_path):
    source=tmp_path/'source.csv'
    source.write_bytes(b'large')
    manager=BrowserDownloads(str(tmp_path), 'bounded')
    manager.max_bytes=2
    download=Download(source)
    manager.receive(download)
    assert (await manager.collect())[0]['status']=='failed'
    assert download.cancelled
    manager.max_bytes=100
    manager.timeout=0.01
    slow=Download(source,delay=1)
    manager.receive(slow)
    assert (await manager.collect())[-1]['error']=='TimeoutError'
    assert slow.cancelled
    pending=Download(source)
    manager.receive(pending)
    await manager.close()
    assert pending.cancelled


@pytest.mark.asyncio
async def test_real_browser_csv_download_and_delayed_event(tmp_path):
    tool=BackgroundBrowserTool(workspace=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip('Set ELREN_BROWSER_RUNTIME to an installed headless runtime')
    context=ToolContext(task_id='csv-real',workspace=str(tmp_path))
    try:
        _,page=await tool._session(context)
        await page.set_content('''<meta charset="utf-8"><button id="export">Export</button>
        <script>document.querySelector('#export').onclick=()=>setTimeout(()=>{
        const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['title,status\\n中文,done\\n'],{type:'text/csv;charset=utf-8'}));
        a.download='report.csv';a.click();},300);</script>''')
        await tool.execute({'action':'click','selector':'#export'},context)
        await page.wait_for_timeout(500)
        result=await tool.execute({'action':'inspect'},context)
        record=result['downloads'][0]
        assert record['status']=='saved'
        path=Path(record['path'])
        assert path.read_text(encoding='utf-8')=='title,status\n中文,done\n'
        assert len(record['sha256'])==64
        assert result['foreground_used'] is False
        again=await tool.execute({'action':'inspect'},context)
        assert again['downloads']==result['downloads']
        await tool.cleanup(context)
        assert path.exists()
    finally:
        await tool.cleanup(context)


@pytest.mark.asyncio
async def test_cancelling_collection_cancels_entire_owned_batch(tmp_path):
    source=tmp_path/'source.csv'
    source.write_bytes(b'x')
    manager=BrowserDownloads(str(tmp_path),'cancel-test')
    first,second=Download(source,delay=10),Download(source)
    manager.receive(first)
    manager.receive(second)
    task=asyncio.create_task(manager.collect())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.cancelled and second.cancelled
    assert manager.records==[]


@pytest.mark.asyncio
async def test_partial_download_not_reported_or_left_as_complete(tmp_path):
    class Broken(Download):
        async def save_as(self, path):
            Path(path).write_bytes(b'incomplete')
            raise OSError('simulated transfer failure')
    source=tmp_path/'source.csv'
    source.write_bytes(b'x')
    manager=BrowserDownloads(str(tmp_path),'broken')
    manager.receive(Broken(source))
    result=await manager.collect()
    assert result[0]['status']=='failed'
    assert list(manager.directory.iterdir())==[]


@pytest.mark.asyncio
async def test_download_count_limit_is_reported(tmp_path):
    source=tmp_path/'source.csv'
    source.write_bytes(b'x')
    manager=BrowserDownloads(str(tmp_path),'limit')
    downloads=[Download(source) for _ in range(17)]
    for download in downloads:
        manager.receive(download)
    result=await manager.collect()
    assert result[-1]['error']=='DownloadLimitExceeded'
    assert result[-1]['count']==1 and downloads[-1].cancelled
