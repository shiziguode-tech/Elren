[CmdletBinding()]
param(
    [ValidateSet("default", "extended")]
    [string]$Profile = "default",
    [switch]$Force,
    [switch]$Offline,
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Net.Http

$LauncherRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $LauncherRoot
$LockPath = Join-Path $LauncherRoot "tool-runtime.lock.json"
$Lock = Get-Content -LiteralPath $LockPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $Destination) {
    $Destination = Join-Path $ProjectRoot "work\tool-runtime"
}
$Destination = [IO.Path]::GetFullPath($Destination)
$WorkRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "work"))
$ExpectedDestination = [IO.Path]::GetFullPath((Join-Path $WorkRoot "tool-runtime"))
if (-not $Destination.Equals($ExpectedDestination, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Tool runtime destination must be exactly $ExpectedDestination"
}

$DownloadCache = Join-Path $WorkRoot ".tool-runtime-downloads"
$NpmCache = Join-Path $WorkRoot ".tool-runtime-npm-cache"
$StageRoot = Join-Path $WorkRoot ".tool-runtime-stage"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-RelativePath([string]$BaseDirectory, [string]$TargetPath) {
    $base = [IO.Path]::GetFullPath($BaseDirectory).TrimEnd("\") + "\"
    $target = [IO.Path]::GetFullPath($TargetPath)
    $relative = ([Uri]$base).MakeRelativeUri([Uri]$target).ToString()
    return [Uri]::UnescapeDataString($relative).Replace("/", "\")
}

function Get-Download([object]$Artifact) {
    $name = [IO.Path]::GetFileName(([Uri]$Artifact.url).AbsolutePath)
    $path = Join-Path $DownloadCache ("{0}-{1}" -f $Artifact.id, $name)
    if (Test-Path -LiteralPath $path) {
        $actual = Get-Sha256 $path
        if ($actual -eq $Artifact.sha256) {
            Write-Host "Using verified cache: $($Artifact.id)"
            return $path
        }
        Remove-Item -LiteralPath $path -Force
    }
    if ($Offline) {
        throw "Offline cache is missing the verified artifact: $($Artifact.id)"
    }
    Write-Host "Downloading $($Artifact.id) $($Artifact.version) from its pinned upstream release..."
    $client = [Net.Http.HttpClient]::new()
    $partial = $path + ".partial"
    try {
        $client.DefaultRequestHeaders.UserAgent.ParseAdd("Elren-Tool-Runtime/1.0")
        $response = $client.GetAsync([string]$Artifact.url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $null = $response.EnsureSuccessStatusCode()
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [IO.File]::Open($partial, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $inputStream.CopyTo($outputStream)
        } finally {
            $outputStream.Dispose()
            $inputStream.Dispose()
        }
        Move-Item -LiteralPath $partial -Destination $path -Force
    } finally {
        $client.Dispose()
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
    $actual = Get-Sha256 $path
    if ($actual -ne $Artifact.sha256) {
        Remove-Item -LiteralPath $path -Force
        throw "SHA-256 mismatch for $($Artifact.id): expected $($Artifact.sha256), got $actual"
    }
    return $path
}

function Resolve-StageFiles([string]$Stage, [string]$Pattern) {
    $normalizedPattern = $Pattern.Replace("\", "/")
    return @(Get-ChildItem -LiteralPath $Stage -File -Recurse | Where-Object {
        $relative = $_.FullName.Substring($Stage.Length).TrimStart("\", "/").Replace("\", "/")
        $relative -like $normalizedPattern
    })
}

function Install-Artifact([object]$Artifact) {
    $download = Get-Download $Artifact
    $stage = Join-Path $StageRoot $Artifact.id
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    if ($Artifact.archive -eq "file") {
        $originalName = [IO.Path]::GetFileName(([Uri]$Artifact.url).AbsolutePath)
        Copy-Item -LiteralPath $download -Destination (Join-Path $stage $originalName)
    } elseif ($Artifact.archive -eq "zip" -or $Artifact.archive -eq "zip-tree") {
        Expand-Archive -LiteralPath $download -DestinationPath $stage -Force
    } else {
        throw "Unsupported archive format: $($Artifact.archive)"
    }

    if ($Artifact.archive -eq "zip-tree") {
        $treeDestination = Join-Path $Destination $Artifact.destination
        New-Item -ItemType Directory -Path $treeDestination -Force | Out-Null
        Copy-Item -Path (Join-Path $stage "*") -Destination $treeDestination -Recurse -Force
    } else {
        foreach ($mapping in $Artifact.install) {
            # Do not call this variable $Matches: PowerShell reserves that name for regex results.
            $selectedFiles = @(Resolve-StageFiles $stage ([string]$mapping.source))
            if ($mapping.include_names) {
                $allowedNames = @($mapping.include_names)
                $selectedFiles = @($selectedFiles | Where-Object { $allowedNames -contains $_.Name })
            }
            if (-not $selectedFiles) {
                throw "No files matched '$($mapping.source)' in $($Artifact.id)"
            }
            $targetPath = Join-Path $Destination ([string]$mapping.destination)
            $targetIsDirectory = ($selectedFiles.Count -gt 1) -or (
                [string]$mapping.destination_type -eq "directory"
            )
            if ($targetIsDirectory) {
                New-Item -ItemType Directory -Path $targetPath -Force | Out-Null
                foreach ($selectedFile in $selectedFiles) {
                    Copy-Item -LiteralPath $selectedFile.FullName -Destination (Join-Path $targetPath $selectedFile.Name) -Force
                }
            } else {
                New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
                Copy-Item -LiteralPath $selectedFiles[0].FullName -Destination $targetPath -Force
            }
        }
    }

    if ($Artifact.license_url) {
        $licenseTarget = Join-Path $Destination ("licenses\{0}-LICENSE" -f $Artifact.id)
        if ($Offline -and -not (Test-Path -LiteralPath $licenseTarget)) {
            throw "Offline mode cannot download the license for $($Artifact.id)"
        }
        if (-not $Offline) {
            Invoke-WebRequest -UseBasicParsing -Uri $Artifact.license_url -OutFile $licenseTarget -Headers @{"User-Agent"="Elren-Tool-Runtime/1.0"}
        }
        if ($Artifact.license_sha256 -and (Get-Sha256 $licenseTarget) -ne $Artifact.license_sha256) {
            throw "License SHA-256 mismatch for $($Artifact.id)"
        }
    }
    Remove-Item -LiteralPath $stage -Recurse -Force
}

function Write-CmdShim([string]$Name, [string]$Body) {
    if (@($Lock.shim_allowlist) -notcontains $Name) {
        throw "Refusing to create non-allowlisted shim: $Name"
    }
    $path = Join-Path $Destination ("bin\{0}.cmd" -f $Name)
    $content = "@echo off`r`nsetlocal`r`n$Body`r`n"
    [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
}

function Install-NodePackages {
    $nodeRoot = Join-Path $Destination "node"
    New-Item -ItemType Directory -Path $nodeRoot -Force | Out-Null
    $packages = [ordered]@{}
    foreach ($property in $Lock.node.default_packages.PSObject.Properties) {
        $packages[$property.Name] = $property.Value
    }
    if ($Profile -eq "extended") {
        foreach ($property in $Lock.node.extended_packages.PSObject.Properties) {
            $packages[$property.Name] = $property.Value
        }
    }
    $dependencies = [ordered]@{}
    foreach ($name in $packages.Keys) {
        $dependencies[$name] = [string]$packages[$name].version
    }
    $packageJson = [ordered]@{
        name = "elren-tool-runtime-node"
        version = "1.0.0"
        private = $true
        description = "Pinned credential-free Node CLI dependencies for Elren"
        dependencies = $dependencies
    }
    [IO.File]::WriteAllText(
        (Join-Path $nodeRoot "package.json"),
        ($packageJson | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )

    $npm = Join-Path $ProjectRoot "work\node-runtime\npm.cmd"
    if (-not (Test-Path -LiteralPath $npm)) {
        throw "Bundled Node package manager is missing: $npm"
    }
    $oldNpmCache = $env:npm_config_cache
    $oldAudit = $env:npm_config_audit
    $oldFund = $env:npm_config_fund
    try {
        $env:npm_config_cache = $NpmCache
        $env:npm_config_audit = "false"
        $env:npm_config_fund = "false"
        & $npm install --prefix $nodeRoot --ignore-scripts --omit=dev --save-exact
        if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
    } finally {
        $env:npm_config_cache = $oldNpmCache
        $env:npm_config_audit = $oldAudit
        $env:npm_config_fund = $oldFund
    }

    foreach ($name in $packages.Keys) {
        $package = $packages[$name]
        $packageDir = Join-Path $nodeRoot ("node_modules\" + $name.Replace("/", "\"))
        if (-not (Test-Path -LiteralPath (Join-Path $packageDir "package.json"))) {
            throw "Installed Node package is missing: $name"
        }
        $installed = Get-Content -LiteralPath (Join-Path $packageDir "package.json") -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$installed.version -ne [string]$package.version) {
            throw "Node package version mismatch for $name"
        }
        foreach ($binProperty in $package.bin.PSObject.Properties) {
            $shimName = $binProperty.Name
            $entry = Join-Path $packageDir ([string]$binProperty.Value).Replace("/", "\")
            if (-not (Test-Path -LiteralPath $entry)) {
                throw "Node CLI entry is missing: $shimName -> $entry"
            }
            $relativeEntry = Get-RelativePath (Join-Path $Destination "bin") $entry
            if ($entry.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase)) {
                Write-CmdShim $shimName ('"%~dp0' + $relativeEntry + '" %*')
            } else {
                $relativeNode = Get-RelativePath (Join-Path $Destination "bin") (Join-Path $ProjectRoot "work\node-runtime\node.exe")
                Write-CmdShim $shimName ('"%~dp0' + $relativeNode + '" "%~dp0' + $relativeEntry + '" %*')
            }
        }
        $license = Get-ChildItem -LiteralPath $packageDir -File | Where-Object { $_.Name -match '^LICEN[CS]E' } | Select-Object -First 1
        if ($license) {
            $safeName = $name.Replace("@", "").Replace("/", "-")
            Copy-Item -LiteralPath $license.FullName -Destination (Join-Path $Destination "licenses\node-$safeName-$($license.Name)") -Force
        }
    }
}

function Write-NativeShims {
    $nativeShims = @{
        "jq" = "native\jq\jq.exe"
        "rg" = "native\ripgrep\rg.exe"
        "gh" = "native\github-cli\gh.exe"
        "ffmpeg" = "native\ffmpeg\bin\ffmpeg.exe"
        "ffprobe" = "native\ffmpeg\bin\ffprobe.exe"
        "xurl" = "native\xurl\xurl.exe"
        "pandoc" = "native\pandoc\pandoc.exe"
        "git" = "native\mingit\cmd\git.exe"
    }
    foreach ($name in $nativeShims.Keys) {
        $target = Join-Path $Destination $nativeShims[$name]
        if (Test-Path -LiteralPath $target) {
            $relative = Get-RelativePath (Join-Path $Destination "bin") $target
            Write-CmdShim $name ('"%~dp0' + $relative + '" %*')
        }
    }
    # The project environment contains the app's pinned Python skill
    # dependencies (including debugpy). The bare base runtime does not.
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $basePython = Join-Path $ProjectRoot "work\python-runtime\python.exe"
    $relativeVenvPython = Get-RelativePath (Join-Path $Destination "bin") $venvPython
    $relativeBasePython = Get-RelativePath (Join-Path $Destination "bin") $basePython
    $pythonBody = @"
if exist "%~dp0$relativeVenvPython" goto elren_venv_python
if not exist "%~dp0$relativeBasePython" (
  echo Elren bundled Python is unavailable. 1>&2
  exit /b 9009
)
"%~dp0$relativeBasePython" %*
exit /b %ERRORLEVEL%
:elren_venv_python
"%~dp0$relativeVenvPython" %*
exit /b %ERRORLEVEL%
"@
    Write-CmdShim "python3" $pythonBody
    $curlBody = @"
if exist "%SystemRoot%\System32\curl.exe" goto elren_system_curl
if exist "%~dp0..\native\mingit\mingw64\bin\curl.exe" goto elren_bundled_curl
echo curl is unavailable on this Windows installation. 1>&2
exit /b 9009
:elren_system_curl
"%SystemRoot%\System32\curl.exe" %*
exit /b %ERRORLEVEL%
:elren_bundled_curl
"%~dp0..\native\mingit\mingw64\bin\curl.exe" %*
exit /b %ERRORLEVEL%
"@
    Write-CmdShim "curl" $curlBody
}

if (-not (Test-Path -LiteralPath $LockPath)) { throw "Lock file is missing: $LockPath" }
New-Item -ItemType Directory -Path $DownloadCache, $NpmCache, $StageRoot -Force | Out-Null
if (Test-Path -LiteralPath $Destination) {
    if (-not $Force) {
        throw "Destination already exists. Pass -Force to rebuild it: $Destination"
    }
    Remove-Item -LiteralPath $Destination -Recurse -Force
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Destination "bin"), (Join-Path $Destination "native"), (Join-Path $Destination "node"), (Join-Path $Destination "licenses") -Force | Out-Null

$selectedIds = @($Lock.profiles.$Profile | Where-Object { $_ -notmatch '^node-cli-' })
foreach ($artifact in $Lock.artifacts) {
    if ($selectedIds -contains $artifact.id) {
        Install-Artifact $artifact
    }
}
Install-NodePackages
Write-NativeShims

$lockHash = Get-Sha256 $LockPath
$runtimeBundlePath = Join-Path $ProjectRoot "work\runtime-bundle.json"
$runtimeBundle = if (Test-Path -LiteralPath $runtimeBundlePath) {
    Get-Content -LiteralPath $runtimeBundlePath -Raw -Encoding UTF8 | ConvertFrom-Json
} else { $null }
$components = @()
foreach ($artifact in $Lock.artifacts) {
    if ($selectedIds -contains $artifact.id) {
        $components += [ordered]@{id=$artifact.id; version=$artifact.version; sha256=$artifact.sha256; source=$artifact.source}
    }
}
$manifestShims = @(Get-ChildItem -LiteralPath (Join-Path $Destination "bin") -File -Filter "*.cmd" | Sort-Object Name)
$toolVersionByShim = @{
    jq = [string]($Lock.artifacts | Where-Object id -eq "jq").version
    rg = [string]($Lock.artifacts | Where-Object id -eq "ripgrep").version
    gh = [string]($Lock.artifacts | Where-Object id -eq "github-cli").version
    ffmpeg = [string]($Lock.artifacts | Where-Object id -eq "ffmpeg-lgpl").version
    ffprobe = [string]($Lock.artifacts | Where-Object id -eq "ffmpeg-lgpl").version
    pandoc = [string]($Lock.artifacts | Where-Object id -eq "pandoc").version
    git = [string]($Lock.artifacts | Where-Object id -eq "mingit").version
    python3 = if ($runtimeBundle) { [string]$runtimeBundle.components.python.version } else { "package-runtime" }
    curl = "windows-or-mingit"
}
foreach ($packageProperty in $Lock.node.default_packages.PSObject.Properties) {
    foreach ($binProperty in $packageProperty.Value.bin.PSObject.Properties) {
        $toolVersionByShim[$binProperty.Name] = [string]$packageProperty.Value.version
    }
}
if ($Profile -eq "extended") {
    foreach ($packageProperty in $Lock.node.extended_packages.PSObject.Properties) {
        foreach ($binProperty in $packageProperty.Value.bin.PSObject.Properties) {
            $toolVersionByShim[$binProperty.Name] = [string]$packageProperty.Value.version
        }
    }
}
$tools = @($manifestShims | ForEach-Object {
    [ordered]@{
        id = $_.BaseName.ToLowerInvariant()
        version = [string]$toolVersionByShim[$_.BaseName]
        executable = "bin/$($_.Name)"
        sha256 = (Get-Sha256 $_.FullName)
        host_only = $false
    }
})
$manifest = [ordered]@{
    schema = 1
    bundle = $Lock.bundle
    product_version = $Lock.product_version
    platform = $Lock.platform
    profile = $Profile
    generated_at = [DateTime]::UtcNow.ToString("o")
    lock_sha256 = $lockHash
    docker_required = $false
    host_runtimes_reused = @("work/python-runtime", "work/node-runtime")
    components = $components
    shims = @($manifestShims | ForEach-Object Name)
    bin_directories = @("bin")
    tools = $tools
}
[IO.File]::WriteAllText(
    (Join-Path $Destination "manifest.json"),
    ($manifest | ConvertTo-Json -Depth 10),
    [Text.UTF8Encoding]::new($false)
)

$JianpuRuntimeBuilder = Join-Path $LauncherRoot "install-jianpu-runtime.ps1"
if (-not (Test-Path -LiteralPath $JianpuRuntimeBuilder)) {
    throw "The required staff-notation recognition runtime builder is missing"
}
& $JianpuRuntimeBuilder
if ($LASTEXITCODE -ne 0) {
    throw "Docker Lite Audiveris installation failed with exit code $LASTEXITCODE"
}

[IO.File]::WriteAllText((Join-Path $Destination "VERSION"), "1.0`r`n", [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $Destination "README.txt"), @"
Elren portable tool runtime 1.0

This is an application-integrated, directory-isolated dependency payload. Docker is not
required. It reuses the package's existing Python and Node runtimes rather than copying
them. Every downloaded native artifact is pinned by SHA-256 in
launcher/tool-runtime.lock.json. No API keys, access tokens, user profiles, or credentials
are stored here.
"@, [Text.UTF8Encoding]::new($false))

$verifier = Join-Path $LauncherRoot "verify-tool-runtime.py"
$pythonVerifier = Join-Path $ProjectRoot "work\python-runtime\python.exe"
if (-not (Test-Path -LiteralPath $pythonVerifier)) { $pythonVerifier = "python" }
& $pythonVerifier $verifier --root $Destination --profile $Profile
if ($LASTEXITCODE -ne 0) { throw "Tool runtime verification failed" }

$totalBytes = (Get-ChildItem -LiteralPath $Destination -File -Recurse | Measure-Object Length -Sum).Sum
if ($Profile -eq "default" -and $totalBytes -gt [int64]$Lock.policy.default_max_extracted_bytes) {
    throw "Default tool runtime exceeds the locked size budget: $totalBytes bytes"
}
Write-Host ("Tool runtime ready: {0} ({1:N1} MiB, profile {2})" -f $Destination, ($totalBytes / 1MB), $Profile)
