"""Read the actual native launcher log helper without creating any desktop UI."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows native launcher')


@pytest.fixture(scope='module')
def log_probe(tmp_path_factory):
    folder = tmp_path_factory.mktemp('log-helper')
    output = folder / 'ReadLog.exe'
    refs = ['System.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Runtime.Serialization.dll']
    for name in ('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll'):
        library = ROOT / 'launcher/webview2' / name
        refs.append(str(library))
        shutil.copyfile(library, folder / name)
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    result = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                             '/main:LauncherLogTests', f'/out:{output}',
                             *[f'/reference:{ref}' for ref in refs],
                             str(ROOT / 'launcher/ElrenLauncher.cs'), str(ROOT / 'tests/launcher_log_harness.cs')],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    assert result.returncode == 0, result.stdout
    return output


@pytest.mark.parametrize('mode', ['normal', 'long_line', 'many_lines', 'writer', 'missing', 'unicode0', 'unicode1', 'unicode2'])
def test_startup_error_tail_is_bounded_and_retains_latest_unicode_error(tmp_path, log_probe, mode):
    log = tmp_path / "错误 [log] & O'Brien.log"
    result = tmp_path / 'tail.txt'
    marker = 'LATEST_ERROR 中文末尾'
    prefix = {'normal': 'old\r\n', 'long_line': 'x' * (2 * 1024 * 1024),
              'many_lines': 'earlier line\n' * 150000, 'writer': 'old\n', 'missing': ''}.get(mode, '')
    if mode.startswith('unicode'):
        prefix = '中' * 30000 + 'x' * int(mode[-1])
    if mode != 'missing':
        log.write_text(prefix + marker + '\r\n', encoding='utf-8-sig', newline='')
    child = subprocess.run([str(log_probe), str(log), str(result), '8', mode],
                           capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    assert child.returncode == 0, child.stderr
    tail = result.read_text('utf-8')
    if mode == 'missing':
        assert tail == ''
    else:
        assert tail.endswith(marker)
        assert len(tail) <= 4096, 'A single giant log line must not become a multi-megabyte UI error'
        assert len(tail.splitlines()) <= 8
        assert '\ufffd' not in tail, 'Bounded UTF-8 tail must not start midway through a code point'
