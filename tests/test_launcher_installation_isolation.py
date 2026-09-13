import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != 'nt', reason='Windows native IPC')
def test_real_native_installation_channels_and_foreign_process_protection(tmp_path):
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.exists():
        pytest.skip('.NET compiler unavailable')
    libraries = ROOT / 'launcher/webview2'
    references = ['System.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Runtime.Serialization.dll']
    for name in ('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll'):
        shutil.copy2(libraries/name,tmp_path/name)
        references.append(str(libraries/name))
    executable = tmp_path/'LauncherIsolation.exe'
    built = subprocess.run([str(compiler),'/nologo','/target:exe','/platform:x64',
        '/main:LauncherIsolationTests',f'/out:{executable}',*[f'/reference:{ref}' for ref in references],
        str(ROOT/'launcher/ElrenLauncher.cs'),str(ROOT/'tests/launcher_isolation_harness.cs')],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30,check=False)
    assert built.returncode == 0, built.stdout + built.stderr
    checked = subprocess.run([str(executable),str(tmp_path/'package')],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW,check=False)
    assert checked.returncode == 0, checked.stdout+checked.stderr
    assert 'LAUNCHER_INSTALLATION_ISOLATION_OK' in checked.stdout


def test_startup_and_recovery_never_call_cross_installation_retirement():
    source=(ROOT/'launcher/ElrenLauncher.cs').read_text('utf-8')
    assert 'TerminatePreviousElrenLaunchers(root);' not in source
    assert 'MarkServiceReplacementIntent' not in source
    assert 'PackageChannelName(@"Local\\ElrenLauncher", root)' in source
    assert 'PackageChannelName(@"Local\\ElrenShow", root)' in source
    assert 'PackageChannelName("Elren-activation", AppDomain.CurrentDomain.BaseDirectory)' in source
