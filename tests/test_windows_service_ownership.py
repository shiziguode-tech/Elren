"""Exercise the real PowerShell cleanup function without terminating processes."""
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows process ownership")
@pytest.mark.parametrize("scenario,expected", [
    ("own", [41001]), ("runtime", [41001]), ("sibling", []),
    ("relative", []), ("other", []), ("mixed", []), ("reused", []),
])
def test_service_cleanup_requires_exact_installation(scenario, expected):
    source = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    function = "function Stop-StaleElrenService" + source.split(
        "function Stop-StaleElrenService", 1
    )[1].split("function Stop-StaleElrenWorkers", 1)[0]
    script = r'''
$ErrorActionPreference='Stop'
$ProjectRoot='C:\Isolated\Elren Space'
$script:kills=New-Object 'System.Collections.Generic.List[int]'
$script:reads=0
function Write-StartupStage { param($Message) }
function Write-ServiceStopIntent { param($CommandLine) }
function Start-Sleep { param($Milliseconds) }
function taskkill.exe { $script:kills.Add([int]$args[1]) }
function Get-ListeningProcessIds {
    param($Port)
    if ($script:kills.Count) { return }
    if ($scenario -eq 'mixed') { return @(41001,41002) }
    return @(41001)
}
function Get-CimInstance {
    param($ClassName,$Filter,$ErrorAction)
    $script:reads++
    $id=[int]($Filter -replace '\D','')
    $path='C:\Isolated\Elren Space\.venv\Scripts\python.exe'
    if ($scenario -eq 'runtime') { $path='C:/Isolated/Elren Space/work/python-runtime/python.exe' }
    if ($scenario -eq 'sibling' -or $id -eq 41002) { $path='C:\Isolated\Elren Space-backup\.venv\Scripts\python.exe' }
    if ($scenario -eq 'relative') { $path='python.exe' }
    if ($scenario -eq 'other') { $path='C:\Other\python.exe' }
    $created='original'
    if ($scenario -eq 'reused' -and $script:reads -gt 1) { $created='replacement' }
    [pscustomobject]@{ProcessId=$id;Name='python.exe';CommandLine=('"'+$path+'" -m deepdesk.main');CreationDate=$created}
}
'''
    script += "\n$scenario='" + scenario + "'\n" + function
    script += r'''
$blocked=$false
try { Stop-StaleElrenService 8765 } catch { $blocked=$true }
ConvertTo-Json -Compress @{kills=@($script:kills);blocked=$blocked}
'''
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["kills"] == expected
    assert report["blocked"] == (not expected)
