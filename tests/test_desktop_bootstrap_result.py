"""Execute only the isolated bootstrap result function, never start.ps1 itself."""

import base64
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows bootstrap protocol")
@pytest.mark.parametrize("exit_code,replaced", [(0, False), (1, False), (1, True), (0, True)])
def test_real_powershell_service_result_preserves_failure_and_replacement(tmp_path, exit_code, replaced):
    source = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    result_function = source.split("function Complete-ServiceRun", 1)[1].split("function Invoke-ElrenService", 1)[0]
    data = tmp_path / "data"
    data.mkdir()
    instance = "a" * 32
    current = data / "service-current-instance.txt"
    current.write_text("b" * 32 if replaced else instance)
    # Dependencies are intentionally inert: no launcher, processes, secrets,
    # log readers, environment loaders, or runtime configuration are invoked.
    code = "\n".join([
        "$ErrorActionPreference='Stop'",
        "$ProjectRoot='" + str(tmp_path).replace("'", "''") + "'",
        "$Utf8NoBom=New-Object System.Text.UTF8Encoding($false)",
        "function Get-ServiceInstancePath([string]$InstanceId) { return $null }",
        "function Write-StartupStage([string]$Message) {}",
        "function Read-TrimmedFile([string]$Path) { if (Test-Path -LiteralPath $Path) { return [IO.File]::ReadAllText($Path).Trim() }; return '' }",
        "function Complete-ServiceRun" + result_function,
        f"Complete-ServiceRun '{instance}' {exit_code}",
    ])
    shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([
        str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode(code.encode("utf-16-le")).decode("ascii"),
    ], capture_output=True, timeout=15, check=False)
    assert result.returncode == exit_code, result.stderr
    failure = data / f"service-exit-failed-{instance}"
    assert failure.exists() == (exit_code != 0)
    if exit_code:
        assert failure.read_text(encoding="utf-8") == "service_exit_failed"
    assert current.exists() == (replaced or exit_code != 0)
    if current.exists():
        assert current.read_text() == ("b" * 32 if replaced else instance)
    assert not (data / f"service-exit-complete-{instance}").exists()
