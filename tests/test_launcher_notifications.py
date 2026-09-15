import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
def test_completion_notification_transitions(tmp_path):
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        pytest.skip('Windows .NET Framework compiler is not installed')
    refs = ['System.dll', 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Runtime.Serialization.dll']
    for name in ('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll'):
        dll = ROOT / 'launcher/webview2' / name
        if not dll.is_file():
            pytest.skip('Build the launcher to provision WebView2 assemblies first')
        shutil.copy2(dll, tmp_path / name)
        refs.append(str(dll))
    output = tmp_path / 'NotificationTests.exe'
    result = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
        '/main:NotificationTests', f'/out:{output}', *[f'/reference:{r}' for r in refs],
        str(ROOT / 'launcher/ElrenLauncher.cs'), str(ROOT / 'tests/launcher_notification_harness.cs')],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'NOTIFICATION_TESTS_OK' in result.stdout
