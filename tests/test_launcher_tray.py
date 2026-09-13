import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows NotifyIcon integration")
def test_native_tray_lifecycle_without_starting_user_backend(tmp_path):
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.exists():
        pytest.skip(".NET Framework compiler unavailable")
    webview = ROOT / "launcher/webview2"
    output = tmp_path / "TrayTests.exe"
    refs = ["System.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Runtime.Serialization.dll"]
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copy2(webview / name, tmp_path / name)
        refs.append(str(webview / name))
    compiled = subprocess.run([
        str(compiler), "/nologo", "/target:exe", "/platform:x64", "/main:TrayLifecycleTests",
        f"/out:{output}", *[f"/reference:{ref}" for ref in refs],
        str(ROOT / "launcher/ElrenLauncher.cs"), str(ROOT / "tests/launcher_tray_harness.cs"),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    checked = subprocess.run([str(output), str(tmp_path), sys.executable], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
    failure = tmp_path / "failure.txt"
    assert checked.returncode == 0, failure.read_text(encoding="utf-8") if failure.exists() else (checked.returncode, checked.stdout, checked.stderr)
    assert "TRAY_LIFECYCLE_OK" in checked.stdout
