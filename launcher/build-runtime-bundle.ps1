param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [string]$NodeSource = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$WorkRoot = Join-Path $ProjectRoot "work"
$PythonExecutable = Join-Path $WorkRoot "python-runtime\python.exe"
$NodeRoot = Join-Path $WorkRoot "node-runtime"
$NodeExecutable = Join-Path $NodeRoot "node.exe"

function Assert-OfficialSignature([string]$Path, [string[]]$SignerFragments) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Runtime executable is missing: $Path" }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { "" }
    $trusted = $false
    foreach ($fragment in $SignerFragments) {
        if ($subject.IndexOf($fragment, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { $trusted = $true; break }
    }
    if ($signature.Status -ne "Valid" -or -not $trusted) { throw "Runtime executable is not signed by an approved publisher: $Path" }
}

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "The portable Python base must already exist at work\python-runtime\python.exe."
}

if (-not (Test-Path -LiteralPath $NodeExecutable -PathType Leaf)) {
    if (-not $NodeSource) {
        $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
        if (-not $nodeCommand) { throw "Node.js was not found. Pass -NodeSource with an official Node.js installation directory." }
        $NodeSource = Split-Path -Parent $nodeCommand.Source
    }
    $NodeSource = [System.IO.Path]::GetFullPath($NodeSource)
    Assert-OfficialSignature (Join-Path $NodeSource "node.exe") @("OpenJS Foundation", "Node.js Foundation")
    New-Item -ItemType Directory -Path $NodeRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $NodeSource "*") -Destination $NodeRoot -Recurse -Force
}

Assert-OfficialSignature $PythonExecutable @("Python Software Foundation")
Assert-OfficialSignature $NodeExecutable @("OpenJS Foundation", "Node.js Foundation")

$PythonVersion = (& $PythonExecutable -c "import platform; print(platform.python_version())" | Select-Object -Last 1).Trim()
$NodeVersion = (& $NodeExecutable --version | Select-Object -Last 1).Trim().TrimStart("v")
$OpenClawManifest = Join-Path $WorkRoot "openclaw-runtime\node_modules\openclaw\package.json"
$OpenClawVersion = if (Test-Path -LiteralPath $OpenClawManifest) {
    ([System.IO.File]::ReadAllText($OpenClawManifest) | ConvertFrom-Json).version
} else { "missing" }

$ToolRuntimeManifestPath = Join-Path $WorkRoot "tool-runtime\manifest.json"
$ToolRuntime = if (Test-Path -LiteralPath $ToolRuntimeManifestPath -PathType Leaf) {
    [System.IO.File]::ReadAllText($ToolRuntimeManifestPath) | ConvertFrom-Json
} else { $null }

$Manifest = [ordered]@{
    schema = 1
    bundle = "elren-portable-runtime"
    product_version = "1.0"
    isolation = "package-first-with-system-fallback"
    components = [ordered]@{
        python = [ordered]@{
            version = $PythonVersion
            executable = "work/python-runtime/python.exe"
            dependencies = ".venv/Lib/site-packages"
        }
        node = [ordered]@{
            version = $NodeVersion
            executable = "work/node-runtime/node.exe"
            package_manager = "work/node-runtime/npm.cmd"
        }
        openclaw = [ordered]@{
            version = $OpenClawVersion
            entry = "work/openclaw-runtime/node_modules/openclaw/openclaw.mjs"
            dependencies = "work/openclaw-runtime/node_modules"
        }
        tool_runtime = [ordered]@{
            version = "1.0"
            manifest = "work/tool-runtime/manifest.json"
            bin = "work/tool-runtime/bin"
            tool_count = if ($ToolRuntime -and $ToolRuntime.tools) { @($ToolRuntime.tools).Count } else { 0 }
            docker_required = $false
        }
    }
}

$ManifestPath = Join-Path $WorkRoot "runtime-bundle.json"
[System.IO.File]::WriteAllText(
    $ManifestPath,
    ($Manifest | ConvertTo-Json -Depth 6),
    (New-Object System.Text.UTF8Encoding($false))
)
Write-Output "Portable runtime bundle ready: Python $PythonVersion, Node.js $NodeVersion, OpenClaw $OpenClawVersion"
Write-Output "Manifest: $ManifestPath"
