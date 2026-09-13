[CmdletBinding()]
param(
    [string]$Source = "C:\Program Files\Audiveris",
    [string]$TessdataSource = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
# Use the framework directly: child Windows PowerShell sessions may inherit a
# PowerShell 7 module path without the module exporting Get-FileHash.
function Get-ElrenSha256([string]$LiteralPath) {
    $stream = [IO.File]::OpenRead($LiteralPath)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "").ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}
$LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $LauncherRoot))
$ExpectedTarget = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "work\tool-runtime\native\audiveris"))
$Target = $ExpectedTarget
$Source = [IO.Path]::GetFullPath($Source)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Workspace Python is required to verify offline OCR data' }
if (-not $TessdataSource) { $TessdataSource = Join-Path $Target 'tessdata' }
$TessdataHelper = Join-Path $ProjectRoot 'deepdesk\audiveris_tessdata.py'

if (-not $Target.Equals($ExpectedTarget, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Audiveris runtime target escaped the verified Docker Lite directory"
}
if (-not (Test-Path -LiteralPath (Join-Path $Source "app\audiveris.jar"))) {
    throw "Audiveris app payload is missing below: $Source"
}
if (-not (Test-Path -LiteralPath (Join-Path $Source "runtime\bin\java.exe"))) {
    throw "Audiveris private Java runtime is missing below: $Source"
}
$Config = Get-Content -LiteralPath (Join-Path $Source "app\Audiveris.cfg") -Raw -Encoding UTF8
if ($Config -notmatch 'jpackage\.app-version=5\.11\.0') {
    throw "Expected the reviewed Audiveris 5.11.0 runtime"
}

# Verify and stage all language data before a requested replacement could remove
# the existing runtime (which is also the default offline source).
$TessdataStage = Join-Path ([IO.Path]::GetTempPath()) ('elren-tessdata-' + [Guid]::NewGuid().ToString('N'))
try {
& $Python -B $TessdataHelper --source $TessdataSource --install-to $TessdataStage
if ($LASTEXITCODE -ne 0) { throw 'Offline legacy OCR data preflight failed; existing runtime was not changed' }
if (Test-Path -LiteralPath $Target) {
    if (-not $Force) {
        throw "Docker Lite Audiveris runtime already exists; pass -Force to replace exactly this reviewed target"
    }
    $ResolvedTarget = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Target))
    if (-not $ResolvedTarget.Equals($ExpectedTarget, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace an unexpected directory: $ResolvedTarget"
    }
    Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
}

New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
Copy-Item -LiteralPath $Source -Destination $Target -Recurse -Force
& $Python -B $TessdataHelper --source (Join-Path $TessdataStage 'tessdata') --install-to $Target
if ($LASTEXITCODE -ne 0) { throw 'Offline legacy OCR data installation failed' }

$LicenseSource = Join-Path $LauncherRoot "audiveris-AGPL-3.0-LICENSE.txt"
$LicenseHash = "76a97c878c9c7a8321bb395c2b44d3fe2f8d81314d219b20138ed0e2dddd5182"
if (-not (Test-Path -LiteralPath $LicenseSource)) {
    throw "Reviewed Audiveris license text is missing: $LicenseSource"
}
if ((Get-ElrenSha256 $LicenseSource) -ne $LicenseHash) {
    throw "Audiveris license text failed SHA-256 verification"
}
$LicenseTarget = Join-Path $ProjectRoot "work\tool-runtime\licenses\audiveris-AGPL-3.0.txt"
New-Item -ItemType Directory -Path (Split-Path -Parent $LicenseTarget) -Force | Out-Null
Copy-Item -LiteralPath $LicenseSource -Destination $LicenseTarget -Force

# Audiveris loads this classifier from audiveris.jar. Keeping a separate,
# hashed copy makes the local recognition model explicit and independently
# auditable inside Elren's Docker Lite payload.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$JarPath = Join-Path $Target "app\audiveris.jar"
$ModelRoot = Join-Path $Target "model"
New-Item -ItemType Directory -Path $ModelRoot -Force | Out-Null
$Archive = [IO.Compression.ZipFile]::OpenRead($JarPath)
try {
    $Entry = $Archive.GetEntry("res/basic-classifier.zip")
    if (-not $Entry) { throw "Audiveris classifier model was not found in audiveris.jar" }
    $InputStream = $Entry.Open()
    $OutputStream = [IO.File]::Create((Join-Path $ModelRoot "basic-classifier.zip"))
    try { $InputStream.CopyTo($OutputStream) } finally { $OutputStream.Dispose(); $InputStream.Dispose() }
} finally {
    $Archive.Dispose()
}

$Files = @(Get-ChildItem -LiteralPath $Target -Recurse -File | Sort-Object FullName)
$Integrity = [ordered]@{
    schema = 1
    component = "elren-docker-lite-audiveris"
    version = "5.11.0"
    commit = "9e1e55cd2746037d059345881c53e6a6754bffbd"
    source = "https://github.com/Audiveris/audiveris"
    license = "AGPL-3.0-only"
    cloud_upload = $false
    model = "model/basic-classifier.zip"
    files = @($Files | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($Target.Length).TrimStart("\").Replace("\", "/")
            bytes = $_.Length
            sha256 = Get-ElrenSha256 $_.FullName
        }
    })
}
$IntegrityPath = Join-Path $Target "elren-integrity.json"
[IO.File]::WriteAllText($IntegrityPath, ($Integrity | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))

$RuntimeManifestPath = Join-Path $ProjectRoot "work\tool-runtime\manifest.json"
if (-not (Test-Path -LiteralPath $RuntimeManifestPath)) {
    throw "Tool runtime manifest is missing: $RuntimeManifestPath"
}
$RuntimeManifest = Get-Content -LiteralPath $RuntimeManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$RuntimeManifest.components = @($RuntimeManifest.components | Where-Object id -ne "audiveris") + @(
    [pscustomobject]@{
        id = "audiveris"
        version = "5.11.0"
        source = "https://github.com/Audiveris/audiveris"
        license = "AGPL-3.0-only"
        integrity_manifest = "native/audiveris/elren-integrity.json"
    }
)
$JavaRelative = "native/audiveris/runtime/bin/java.exe"
$JavaPath = Join-Path (Join-Path $ProjectRoot "work\tool-runtime") $JavaRelative
$RuntimeManifest.tools = @($RuntimeManifest.tools | Where-Object id -ne "audiveris") + @(
    [pscustomobject]@{
        id = "audiveris"
        version = "5.11.0"
        executable = $JavaRelative
        sha256 = Get-ElrenSha256 $JavaPath
        host_only = $false
        execution_mode = "docker-lite-isolated-subprocess"
    }
)
[IO.File]::WriteAllText($RuntimeManifestPath, ($RuntimeManifest | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))

$ModelPath = Join-Path $ModelRoot "basic-classifier.zip"
$TotalBytes = ($Files | Measure-Object Length -Sum).Sum
Write-Host ("Docker Lite Audiveris ready: {0:N1} MiB, model SHA-256 {1}" -f ($TotalBytes / 1MB), (Get-ElrenSha256 $ModelPath))
} finally {
    # Only the fresh, GUID-owned staging directory is eligible for cleanup.
    $ExpectedStage = [IO.Path]::GetFullPath($TessdataStage)
    if ((Test-Path -LiteralPath $ExpectedStage) -and
        [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExpectedStage)).Equals($ExpectedStage, [StringComparison]::OrdinalIgnoreCase) -and
        -not ((Get-Item -LiteralPath $ExpectedStage).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $ExpectedStage -Recurse -Force
    }
}
