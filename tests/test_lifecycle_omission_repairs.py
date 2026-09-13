"""Actual launcher call chains and mocked destructive PowerShell boundaries."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher call-chain integration")
def test_native_lifecycle_call_chains_without_user_backend(tmp_path):
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    assert compiler.is_file(), "Required native compiler unavailable; not a passing native check"
    webview = ROOT / "launcher/webview2"
    refs = ["System.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Runtime.Serialization.dll"]
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copy2(webview / name, tmp_path / name)
        refs.append(str(webview / name))
    output = tmp_path / "LifecycleOmissions.exe"
    compiled = subprocess.run([
        str(compiler), "/nologo", "/target:exe", "/platform:x64", "/main:LauncherOmissionTests",
        f"/out:{output}", *[f"/reference:{ref}" for ref in refs],
        str(ROOT / "launcher/ElrenLauncher.cs"), str(ROOT / "tests/launcher_omission_harness.cs"),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    # The .NET Framework launcher still has ordinary Win32 path length limits;
    # avoid turning pytest's deeply nested evidence path into the test subject.
    runtime_root = Path(tempfile.mkdtemp(prefix="elren-lc-repair-"))
    (tmp_path / "native-runtime-root.txt").write_text(str(runtime_root), encoding="utf-8")
    checked = subprocess.run([str(output), str(runtime_root)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=70, check=False)
    (tmp_path / "native-results.txt").write_text(checked.stdout + checked.stderr, encoding="utf-8")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "LIFECYCLE_OMISSIONS_OK" in checked.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell worker ownership contract")
def test_powershell_worker_cleanup_uses_exact_package_tokens(tmp_path):
    source = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    function = "function Stop-StaleElrenWorkers" + source.split("function Stop-StaleElrenWorkers", 1)[1].split("function Get-TrustedLocalInstaller", 1)[0]
    script = r"""
$ErrorActionPreference='Stop'
$ProjectRoot='C:\Isolated\Elren Space'
$script:killRequests=New-Object 'System.Collections.Generic.List[int]'
$script:queries=0
function Write-StartupStage([string]$Message) {}
function Start-Sleep { param([int]$Milliseconds) }
function Get-CimInstance {
  param($ClassName,$Filter,$ErrorAction)
  $script:queries++
  if ($script:queries -ne 1) { return }
  $rows=@(
    @(41001,1,'"C:\Isolated\Elren Space\.venv\Scripts\python.exe" -m deepdesk.feishu_ws_worker'),
    @(41002,41001,'"C:\Isolated\Elren Space\.venv\Scripts\python.exe" -m deepdesk.feishu_ws_worker'),
    @(42001,1,'"C:\Isolated\Elren Space-backup\.venv\Scripts\python.exe" -m deepdesk.feishu_ws_worker'),
    @(42002,1,'python -m deepdesk.feishu_ws_worker --workspace "C:\Isolated\Elren Space-backup"'),
    @(43001,1,'python -m deepdesk.feishu_ws_worker --workspace="C:/Isolated/Elren Space/"'),
    @(43002,1,'python -m deepdesk.feishu_ws_worker --workspace "C:\Isolated\Elren Space\..\Elren Space-backup"'),
    @(44001,1,'python -m deepdesk.feishu_ws_worker --diagnostic "C:\Isolated\Elren Space"'),
    @(45001,1,'"C:\Isolated\Elren Space\.venv\Scripts\python.exe" -m other_module'),
    @(45002,1,'"C:\Isolated\Elren Space\.venv\Scripts\python.exe" -m deepdesk.feishu_ws_worker --workspace "C:\Other"'),
    @(46001,1,'python -m deepdesk.feishu_ws_worker --workspace "c:\isolated\elren space"'),
    @(47001,1,'python -m deepdesk.feishu_ws_worker --workspace "C:\Isolated\Elren Space" --workspace "C:\Other"')
  )
  foreach($row in $rows) { [pscustomobject]@{ProcessId=$row[0]; ParentProcessId=$row[1]; CommandLine=$row[2]} }
}
function taskkill.exe {
  # This function shadows command lookup; no native kill is ever executed.
  $script:killRequests.Add([int]$args[1])
}
""" + function + "\nStop-StaleElrenWorkers\nConvertTo-Json -Compress -InputObject @($script:killRequests)\n"
    shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                            capture_output=True, text=True, timeout=15, check=False)
    (tmp_path / "worker-selection.json").write_text(result.stdout, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert sorted(json.loads(result.stdout)) == [41001, 43001, 46001]


@pytest.mark.skipif(os.name != "nt", reason="PowerShell bootstrap instance contract")
def test_actual_service_function_consumes_only_valid_assigned_instance(tmp_path):
    source = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    function = "function Invoke-ElrenService" + source.split("function Invoke-ElrenService", 1)[1].split("function Get-ListeningProcessIds", 1)[0]
    project = str(tmp_path).replace("'", "''")
    script = "$ErrorActionPreference='Stop'; $ProjectRoot='" + project + "'; " + r"""
$Utf8NoBom=New-Object System.Text.UTF8Encoding($false)
function Complete-ServiceRun { param([string]$InstanceId,[int]$ExitCode) $script:completed=$InstanceId }
function synthetic-python { $script:arguments=@($args); $global:LASTEXITCODE=0 }
""" + function + r"""
$records=@()
foreach($candidate in @(('a'*32),'../../other','')) {
  $env:ELREN_SERVICE_INSTANCE_ID=$candidate
  Invoke-ElrenService 'synthetic-python'
  $current=[IO.File]::ReadAllText((Join-Path $ProjectRoot 'data\service-current-instance.txt')).Trim()
  $records += [pscustomobject]@{candidate=$candidate; marker=$current; argument=$script:arguments[-1]; completed=$script:completed}
}
ConvertTo-Json -Compress -InputObject $records
"""
    shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    assert records[0]["marker"] == "a" * 32
    for record in records:
        assert len(record["marker"]) == 32 and set(record["marker"]) <= set("0123456789abcdef")
        assert record["marker"] == record["argument"] == record["completed"]
