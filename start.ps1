$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Windows PowerShell inherits PSModulePath from its parent process.  When the
# desktop shell is launched from PowerShell 7 (or another developer host), that
# inherited path can point at incompatible PS7 modules.  The result is a very
# confusing startup failure where Get-FileHash/Get-AuthenticodeSignature are
# missing or duplicate type data aborts module loading.  This bootstrap only
# needs the signed inbox Windows PowerShell modules, so use their canonical
# locations for a deterministic launch from Explorer, terminals, and IDEs.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    $inboxModuleRoots = @(
        (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\Modules"),
        (Join-Path $env:ProgramFiles "WindowsPowerShell\Modules")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    $env:PSModulePath = (@($inboxModuleRoots | Select-Object -Unique) -join ";")
}
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\')
Set-Location -LiteralPath $ProjectRoot

# Elren owns the current runtime identity.  Import only the small set of old
# bootstrap variables needed to upgrade an existing portable folder; all new
# processes and generated configuration use ELREN_* names.
foreach ($LegacySuffix in @("PORT", "BOOTSTRAP_ONLY", "SIMULATE_MISSING_RUNTIMES", "SIMULATE_LEGACY_COMPUTER")) {
    $ElrenName = "ELREN_$LegacySuffix"
    $LegacyNames = @("MILO_$LegacySuffix", "DEEPDESK_$LegacySuffix")
    if (-not [Environment]::GetEnvironmentVariable($ElrenName, "Process")) {
        foreach ($LegacyName in $LegacyNames) {
            $LegacyValue = [Environment]::GetEnvironmentVariable($LegacyName, "Process")
            if ($LegacyValue) {
                [Environment]::SetEnvironmentVariable($ElrenName, $LegacyValue, "Process")
                break
            }
        }
    }
}
$env:ELREN_WORKSPACE = $ProjectRoot

function Repair-MovedOpenClawJunctions {
    $stateRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "data\openclaw"))
    if (-not (Test-Path -LiteralPath $stateRoot)) { return }
    $repaired = 0
    $junctions = @(Get-ChildItem -LiteralPath $stateRoot -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue |
        Where-Object { $_.LinkType -eq "Junction" })
    foreach ($junction in $junctions) {
        $linkPath = [System.IO.Path]::GetFullPath($junction.FullName)
        if (-not $linkPath.StartsWith($stateRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $oldTarget = [string]@($junction.Target)[0]
        if (-not $oldTarget -or (Test-Path -LiteralPath $oldTarget)) { continue }
        $newTarget = ""
        $virtualStoreMarker = "\node_modules\.pnpm\openclaw@"
        $virtualStoreIndex = $oldTarget.IndexOf($virtualStoreMarker, [System.StringComparison]::OrdinalIgnoreCase)
        if ($virtualStoreIndex -ge 0) {
            $packageMarker = "\node_modules\openclaw"
            $packageIndex = $oldTarget.IndexOf(
                $packageMarker,
                $virtualStoreIndex + $virtualStoreMarker.Length,
                [System.StringComparison]::OrdinalIgnoreCase
            )
            if ($packageIndex -ge 0) {
                $packageSuffix = $oldTarget.Substring($packageIndex + $packageMarker.Length).TrimStart('\')
                $hoistedOpenClaw = Join-Path $ProjectRoot "work\openclaw-runtime\node_modules\openclaw"
                $newTarget = if ($packageSuffix) {
                    Join-Path $hoistedOpenClaw $packageSuffix
                } else { $hoistedOpenClaw }
            }
        }
        if (-not $newTarget) {
            $marker = "\work\openclaw-runtime\"
            $markerIndex = $oldTarget.IndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase)
            if ($markerIndex -lt 0) { continue }
            $suffix = $oldTarget.Substring($markerIndex + $marker.Length)
            $newTarget = Join-Path $ProjectRoot (Join-Path "work\openclaw-runtime" $suffix)
        }
        $newTarget = [System.IO.Path]::GetFullPath($newTarget)
        if (-not (Test-Path -LiteralPath $newTarget)) { continue }
        [System.IO.Directory]::Delete($linkPath)
        New-Item -ItemType Junction -Path $linkPath -Target $newTarget | Out-Null
        $repaired++
    }
    if ($repaired -gt 0) {
        Write-Output ("[{0}] Repaired {1} moved OpenClaw runtime junction(s)." -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $repaired)
    }
}

# Recursively enumerating the OpenClaw state tree is useful after a portable
# package is moved, but it can add several seconds to every warm launch on slow
# disks.  Remember the package location and rescan only on first use or after a
# move.  The marker lives in work/ because it describes package-owned runtime
# state rather than user data.
$PackageLocationMarker = Join-Path $ProjectRoot "work\.elren-package-location-v1"
try {
    $PreviousPackageLocation = if (Test-Path -LiteralPath $PackageLocationMarker) {
        ([System.IO.File]::ReadAllText($PackageLocationMarker)).Trim()
    } else { "" }
    if (-not $PreviousPackageLocation.Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Repair-MovedOpenClawJunctions
        New-Item -ItemType Directory -Path (Split-Path -Parent $PackageLocationMarker) -Force | Out-Null
        [System.IO.File]::WriteAllText($PackageLocationMarker, $ProjectRoot, $Utf8NoBom)
    }
} catch {
    # Relocation repair is best-effort. A read-only work directory must not
    # prevent the otherwise usable packaged runtime from starting.
}

function Write-StartupStage([string]$Message) {
    # Write-Host uses the information stream, which the launcher still captures
    # with *>>, without contaminating object return values from helper functions.
    Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

# Preserve the real startup failure in the launcher log.  Previously an
# unhandled PowerShell exception could make the background process exit with
# code 1 while the desktop shell could only report the generic exit code.
trap {
    $failureMessage = [string]$_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($failureMessage)) {
        $failureMessage = "Unknown PowerShell startup error."
    }
    $failureMessage = $failureMessage -replace '[\r\n]+', ' '
    $failureLine = if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) {
        " at start.ps1 line $($_.InvocationInfo.ScriptLineNumber)"
    } else { "" }
    Write-StartupStage ("Startup preparation failed{0}: {1}" -f $failureLine, $failureMessage)
    exit 1
}

function Read-TrimmedFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return ([System.IO.File]::ReadAllText($Path)).Trim()
}

function Get-ServiceInstancePath([string]$InstanceId) {
    if ($InstanceId -notmatch '^[0-9a-f]{32}$') { return "" }
    return Join-Path $ProjectRoot ("data\service-stop-intent-{0}" -f $InstanceId)
}

function Write-ServiceStopIntent([string]$CommandLine) {
    if (-not $CommandLine) { return }
    $match = [regex]::Match($CommandLine, '(?i)--elren-service-id(?:=|\s+)["'']?([0-9a-f]{32})')
    if (-not $match.Success) { return }
    $intentPath = Get-ServiceInstancePath $match.Groups[1].Value.ToLowerInvariant()
    if (-not $intentPath) { return }
    New-Item -ItemType Directory -Path (Split-Path -Parent $intentPath) -Force | Out-Null
    [System.IO.File]::WriteAllText($intentPath, "launcher replacement", $Utf8NoBom)
}

function Complete-ServiceRun([string]$InstanceId, [int]$ExitCode) {
    $currentPath = Join-Path $ProjectRoot "data\service-current-instance.txt"
    $intentPath = Get-ServiceInstancePath $InstanceId
    $intentional = $intentPath -and (Test-Path -LiteralPath $intentPath)
    if ($intentional) {
        Remove-Item -LiteralPath $intentPath -Force -ErrorAction SilentlyContinue
        Write-StartupStage "The local service stopped for an intentional launcher handoff (exit code $ExitCode)."
    } elseif ($ExitCode -eq 0) {
        Write-StartupStage "The local service exited normally."
    } else {
        Write-StartupStage "The local service exited unexpectedly (exit code $ExitCode)."
    }
    if ($ExitCode -ne 0 -and $InstanceId -match '^[0-9a-f]{32}$') {
        # Keep an explicit failure result even when Python could not finish its
        # handshake. Removing the current marker here previously looked like a
        # successful full exit to the desktop shell.
        $failedPath = Join-Path $ProjectRoot ("data\service-exit-failed-{0}" -f $InstanceId)
        try {
            if (-not (Test-Path -LiteralPath $failedPath -PathType Leaf)) {
                [System.IO.File]::WriteAllText($failedPath, "service_exit_failed", $Utf8NoBom)
            }
        } catch { Write-StartupStage "The failed service exit could not be recorded." }
    }
    if ($ExitCode -eq 0 -and (Read-TrimmedFile $currentPath) -eq $InstanceId) {
        Remove-Item -LiteralPath $currentPath -Force -ErrorAction SilentlyContinue
    }
    exit $ExitCode
}

function Invoke-ElrenService([string]$PythonExecutable) {
    $instanceId = [string]$env:ELREN_SERVICE_INSTANCE_ID
    if ($instanceId -cnotmatch '^[0-9a-f]{32}$') { $instanceId = [Guid]::NewGuid().ToString("N") }
    $dataRoot = Join-Path $ProjectRoot "data"
    $currentPath = Join-Path $dataRoot "service-current-instance.txt"
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    [System.IO.File]::WriteAllText($currentPath, $instanceId, $Utf8NoBom)
    Get-ChildItem -LiteralPath $dataRoot -Filter "service-stop-intent-*" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-1) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $PythonExecutable -u -m deepdesk.main --elren-service-id $instanceId
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    Complete-ServiceRun $instanceId $exitCode
}

function Get-ListeningProcessIds([int]$Port) {
    $owners = New-Object 'System.Collections.Generic.HashSet[int]'
    $portRowsSeen = $false
    try {
        # Get-NetTCPConnection takes roughly 2-3 seconds per call on some Windows
        # machines. This function is used both before and after taskkill, so that
        # delay used to dominate every warm launch. Native netstat returns the
        # same owner PID information in a few milliseconds.
        $netstatLines = @(netstat.exe -ano -p tcp 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "netstat failed" }
        $netstatLines | ForEach-Object {
            $parts = @($_.Trim() -split '\s+')
            if (
                $parts.Count -ge 5 -and
                $parts[0] -eq "TCP" -and
                $parts[1] -match (":" + $Port + "$")
            ) {
                $portRowsSeen = $true
                # LISTENING is localized on some Windows editions. Listening
                # rows can also be identified by netstat's wildcard foreign
                # endpoint, which is stable across display languages.
                $isListening = $parts[3] -eq "LISTENING" -or
                    $parts[2] -match '^(?:0\.0\.0\.0|\[::\]|\*):(?:0|\*)$'
                if ($isListening) {
                    $parsedOwner = 0
                    if ([int]::TryParse($parts[4], [ref]$parsedOwner) -and $parsedOwner -gt 0) {
                        [void]$owners.Add($parsedOwner)
                    }
                }
            }
        }
        if ($portRowsSeen -and $owners.Count -eq 0) {
            # An unfamiliar output shape is not evidence that the port is free.
            # Ask the structured cmdlet before launching a second backend.
            # An empty result is the normal "port is free" case. Some Windows
            # builds surface that case as a CIM error, so never let it abort
            # startup validation.
            @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) | ForEach-Object {
                if ($_.OwningProcess -gt 0) { [void]$owners.Add([int]$_.OwningProcess) }
            }
        }
    } catch {
        # Keep the PowerShell cmdlet as a compatibility fallback if netstat is
        # unavailable or changes its output shape on a future Windows release.
        @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) | ForEach-Object {
            if ($_.OwningProcess -gt 0) { [void]$owners.Add([int]$_.OwningProcess) }
        }
    }
    return @($owners)
}

function Stop-StaleElrenService([int]$Port) {
    $ownerIds = @(Get-ListeningProcessIds $Port)
    if ($ownerIds.Count -eq 0) { return }

    $canonicalRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
    $approvedOwners = @()
    foreach ($ownerId in $ownerIds) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        if (-not $processInfo) { continue }
        $commandLine = [string]$processInfo.CommandLine
        $isElrenService = $commandLine -match '(?i)(^|\s)-m\s+deepdesk\.main(\s|$)'
        if (-not $isElrenService) {
            throw "Port $Port is occupied by another application ($($processInfo.Name), PID $ownerId). Elren did not terminate it."
        }

        # A module name alone does not prove ownership: another installation
        # can serve the same port. Require our packaged Python executable and
        # validate every listener before terminating any of them.
        $executable = [regex]::Match($commandLine, '^\s*(?:"([^"]+)"|''([^'']+)''|(\S+))')
        $owned = $false
        if ($executable.Success) {
            $value = @($executable.Groups[1].Value, $executable.Groups[2].Value, $executable.Groups[3].Value) | Where-Object { $_ }
            try {
                if ([IO.Path]::IsPathRooted([string]$value)) {
                    $executablePath = [IO.Path]::GetFullPath([string]$value)
                    foreach ($relative in @('.venv\Scripts\python.exe', 'work\python-runtime\python.exe')) {
                        if ($executablePath.Equals((Join-Path $canonicalRoot $relative), [StringComparison]::OrdinalIgnoreCase)) {
                            $owned = $true
                        }
                    }
                }
            } catch { $owned = $false }
        }
        if (-not $owned) {
            throw "Port $Port belongs to another Elren installation or an unverified Python process (PID $ownerId). Elren did not terminate it."
        }
        $approvedOwners += $processInfo
    }

    foreach ($processInfo in $approvedOwners) {
        $ownerId = [int]$processInfo.ProcessId
        # Refuse PID reuse or a changed listener between inspection and stop.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        if (-not $current) { continue }
        if ($current.CommandLine -ne $processInfo.CommandLine -or $current.CreationDate -ne $processInfo.CreationDate) {
            throw "The process on port $Port changed during startup. Elren did not terminate it."
        }
        $commandLine = [string]$current.CommandLine
        Write-StartupStage "Stopping the previous Elren service on port $Port (PID $ownerId)..."
        Write-ServiceStopIntent $commandLine
        & taskkill.exe /PID $ownerId /T /F | Out-Null
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (@(Get-ListeningProcessIds $Port).Count -eq 0) {
            Write-StartupStage "The previous Elren service was stopped and port $Port is free."
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw "The previous Elren process was terminated, but port $Port was not released within 10 seconds."
}

function Stop-StaleElrenWorkers {
    function Test-WorkerPackageOwnership([string]$CommandLine) {
        $canonicalRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
        # Only an explicit workspace argument or the executable token proves
        # package ownership. A sibling prefix or a root embedded in diagnostics
        # is not permission to terminate a worker.
        $workspaces = [regex]::Matches($CommandLine, '(?i)(?:^|\s)--workspace(?:=|\s+)(?:"([^"]+)"|''([^'']+)''|(\S+))')
        if ($workspaces.Count -gt 1) { return $false }
        if ($workspaces.Count -eq 1) {
            $workspace = $workspaces[0]
            $value = @($workspace.Groups[1].Value, $workspace.Groups[2].Value, $workspace.Groups[3].Value) | Where-Object { $_ }
            try {
                return [IO.Path]::IsPathRooted([string]$value) -and
                    [IO.Path]::GetFullPath([string]$value).TrimEnd('\', '/').Equals($canonicalRoot, [StringComparison]::OrdinalIgnoreCase)
            } catch { return $false }
        }
        $executable = [regex]::Match($CommandLine, '^\s*(?:"([^"]+)"|''([^'']+)''|(\S+))')
        if (-not $executable.Success) { return $false }
        $value = @($executable.Groups[1].Value, $executable.Groups[2].Value, $executable.Groups[3].Value) | Where-Object { $_ }
        try {
            return [IO.Path]::IsPathRooted([string]$value) -and
                [IO.Path]::GetFullPath([string]$value).StartsWith($canonicalRoot + '\', [StringComparison]::OrdinalIgnoreCase)
        } catch { return $false }
    }
    # A venv launcher creates a two-process Python chain, and a dying backend
    # can orphan its listener just after the first CIM snapshot. Sweep a few
    # times so both the wrapper and any re-parented child are retired without
    # touching Python processes from another Elren installation.
    for ($sweep = 0; $sweep -lt 4; $sweep++) {
        $workers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue | Where-Object {
            $commandLine = [string]$_.CommandLine
            $commandLine -match '(?i)(^|\s)-m\s+deepdesk\.feishu_ws_worker(\s|$)' -and
            (Test-WorkerPackageOwnership $commandLine)
        })
        if ($workers.Count -eq 0) { return }
        if ($sweep -ge 3) {
            throw "A stale Feishu listener from this Elren folder could not be stopped safely."
        }

        $workerIds = New-Object 'System.Collections.Generic.HashSet[int]'
        foreach ($worker in $workers) { [void]$workerIds.Add([int]$worker.ProcessId) }
        $roots = @($workers | Where-Object { -not $workerIds.Contains([int]$_.ParentProcessId) })
        foreach ($worker in $roots) {
            Write-StartupStage "Stopping a stale Feishu listener process (PID $($worker.ProcessId))..."
            & taskkill.exe /PID $worker.ProcessId /T /F | Out-Null
        }
        Start-Sleep -Milliseconds 150
    }
}

function Get-TrustedLocalInstaller([string[]]$Names, [string[]]$SignerFragments) {
    $files = @(Get-ChildItem -LiteralPath $ProjectRoot -File -ErrorAction SilentlyContinue)
    foreach ($name in $Names) {
        foreach ($file in $files) {
            if ($file.Name -notlike $name) { continue }
            $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
            $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { "" }
            $trustedSigner = $false
            foreach ($fragment in $SignerFragments) {
                if ($subject.IndexOf($fragment, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                    $trustedSigner = $true
                    break
                }
            }
            if ($signature.Status -eq "Valid" -and $trustedSigner) { return $file.FullName }
            Write-StartupStage "Ignored installer '$($file.Name)' because its official digital signature could not be verified."
        }
    }
    return $null
}

function Get-PythonVersion([string]$Executable) {
    try {
        $text = (& $Executable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null | Select-Object -Last 1)
        if (-not $text) { return $null }
        return [version]$text.Trim()
    } catch {
        return $null
    }
}

function Get-HighestPathPython {
    if ($env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1") {
        if ($script:SimulatedPythonInstalled) {
            return [pscustomobject]@{ Path = "simulated-python.exe"; Version = [version]"3.14.6" }
        }
        return $null
    }
    if ($env:ELREN_SIMULATE_LEGACY_COMPUTER -eq "1") {
        return [pscustomobject]@{ Path = "simulated-python-3.12.exe"; Version = [version]"3.12.0" }
    }

    $paths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($directory in ([string]$env:Path -split ';')) {
        $cleanDirectory = $directory.Trim().Trim('"')
        if (-not $cleanDirectory) { continue }
        foreach ($name in @("python.exe", "python3.exe")) {
            $candidate = Join-Path $cleanDirectory $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $paths.Add($candidate) }
        }
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $versions = @()
    foreach ($path in $paths) {
        $fullPath = [System.IO.Path]::GetFullPath($path)
        if (-not $seen.Add($fullPath)) { continue }
        $version = Get-PythonVersion $fullPath
        if ($version) { $versions += [pscustomobject]@{ Path = $fullPath; Version = $version } }
    }
    return $versions | Sort-Object Version -Descending | Select-Object -First 1
}

function Get-CompatiblePython([version]$PreferredVersion = $null) {
    if ($env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1") {
        if ($script:SimulatedPythonInstalled) {
            return [pscustomobject]@{ Path = "simulated-python.exe"; Version = [version]"3.11.0" }
        }
        return $null
    }
    if ($env:ELREN_SIMULATE_LEGACY_COMPUTER -eq "1") {
        if ($PreferredVersion) {
            if ($script:SimulatedMatchingPythonInstalled) {
                return [pscustomobject]@{
                    Path = "simulated-python-$($PreferredVersion.Major).$($PreferredVersion.Minor).exe"
                    Version = $PreferredVersion
                }
            }
            return $null
        }
        return [pscustomobject]@{ Path = "simulated-python-3.12.exe"; Version = [version]"3.12.0" }
    }
    $paths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($commandName in @("python.exe", "python")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command -and $command.Source) { $paths.Add($command.Source) }
    }
    $systemDrive = if ($env:SystemDrive) { $env:SystemDrive } else { "C:" }
    foreach ($pattern in @(
        (Join-Path $env:LocalAppData "Programs\Python\Python*\python.exe"),
        (Join-Path $env:ProgramFiles "Python*\python.exe"),
        (Join-Path $systemDrive "Python*\python.exe")
    )) {
        @(Get-Item -Path $pattern -ErrorAction SilentlyContinue) | ForEach-Object { $paths.Add($_.FullName) }
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $compatible = @()
    foreach ($path in $paths) {
        if (-not $path -or -not $seen.Add($path)) { continue }
        $version = Get-PythonVersion $path
        if ($version -and $version -ge [version]"3.11.0" -and $version -lt [version]"4.0.0") {
            $compatible += [pscustomobject]@{ Path = $path; Version = $version }
        }
    }
    if ($PreferredVersion) {
        return $compatible |
            Where-Object {
                $_.Version.Major -eq $PreferredVersion.Major -and
                $_.Version.Minor -eq $PreferredVersion.Minor
            } |
            Sort-Object Version -Descending |
            Select-Object -First 1
    }
    return $compatible | Sort-Object Version -Descending | Select-Object -First 1
}

function Get-VerifiedPythonInstaller {
    return Get-TrustedLocalInstaller @(
        "python-installer.exe",
        "python-*-amd64.exe"
    ) @("Python Software Foundation")
}

function Get-InstallerProductVersion([string]$Installer) {
    if (-not $Installer -or -not (Test-Path -LiteralPath $Installer)) { return $null }
    try {
        $versionText = (Get-Item -LiteralPath $Installer).VersionInfo.ProductVersion
        $match = [regex]::Match([string]$versionText, '(\d+)\.(\d+)(?:\.(\d+))?')
        if (-not $match.Success) { return $null }
        $patch = if ($match.Groups[3].Success) { [int]$match.Groups[3].Value } else { 0 }
        return [version]::new(
            [int]$match.Groups[1].Value,
            [int]$match.Groups[2].Value,
            $patch
        )
    } catch {
        return $null
    }
}

function Ensure-CompatiblePython([version]$PreferredVersion = $null, [switch]$ForceInstaller) {
    $python = if ($ForceInstaller) { $null } else { Get-CompatiblePython -PreferredVersion $PreferredVersion }
    if ($python) { return $python }

    $installer = Get-VerifiedPythonInstaller
    if (-not $installer) {
        throw "Python 3.11+ was not found. Put the official signed Python x64 installer in this folder as python-installer.exe, then double-click Elren.exe again."
    }

    $installerVersion = Get-InstallerProductVersion $installer
    if (
        $PreferredVersion -and
        $installerVersion -and
        ($installerVersion.Major -ne $PreferredVersion.Major -or $installerVersion.Minor -ne $PreferredVersion.Minor)
    ) {
        throw "The bundled Python installer is $installerVersion, but the movable environment requires Python $($PreferredVersion.Major).$($PreferredVersion.Minor). Use the matching official x64 installer."
    }

    $requiredLabel = if ($PreferredVersion) {
        "Python $($PreferredVersion.Major).$($PreferredVersion.Minor)"
    } else { "Python 3.11+" }
    Write-StartupStage "$requiredLabel is missing. Opening the verified official Python installer..."
    if (
        $env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1" -or
        $env:ELREN_SIMULATE_LEGACY_COMPUTER -eq "1"
    ) {
        Write-StartupStage "SIMULATION: would open the visible Python installer: $installer"
        if ($PreferredVersion) {
            $script:SimulatedMatchingPythonInstalled = $true
        } else {
            $script:SimulatedPythonInstalled = $true
        }
        $installProcess = [pscustomobject]@{ ExitCode = 0 }
    } else {
        $installProcess = Start-Process -FilePath $installer -Wait -PassThru
    }
    if ($installProcess.ExitCode -ne 0) { throw "The Python installer did not finish successfully (exit code $($installProcess.ExitCode))." }
    $python = Get-CompatiblePython -PreferredVersion $PreferredVersion
    if (-not $python) { throw "Python installation finished, but $requiredLabel could not be found. Complete the installer and launch Elren again." }
    return $python
}

function Get-BundledPythonBase {
    $executable = Join-Path $ProjectRoot "work\python-runtime\python.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { return $null }
    $signature = Get-AuthenticodeSignature -LiteralPath $executable
    $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { "" }
    if (
        $signature.Status -ne "Valid" -or
        $subject.IndexOf("Python Software Foundation", [System.StringComparison]::OrdinalIgnoreCase) -lt 0
    ) {
        Write-StartupStage "Ignored the package-local Python runtime because its official signature could not be verified."
        return $null
    }
    $version = Get-PythonVersion $executable
    if (-not $version -or $version -lt [version]"3.11.0" -or $version -ge [version]"4.0.0") { return $null }
    $declared = Get-RuntimeBundleComponent "python"
    if ($declared -and $declared.version -and $version -ne [version]$declared.version) {
        Write-StartupStage "Ignored the package-local Python runtime because it does not match the portable runtime manifest."
        return $null
    }
    return [pscustomobject]@{ Path = $executable; Version = $version }
}

function Get-RuntimeBundleComponent([string]$Name) {
    $manifestPath = Join-Path $ProjectRoot "work\runtime-bundle.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $null }
    try {
        $manifest = [System.IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
        if ($manifest.schema -ne 1 -or $manifest.bundle -ne "elren-portable-runtime") { return $null }
        return $manifest.components.$Name
    } catch {
        Write-StartupStage "Ignored an invalid package-local runtime manifest; signed runtime discovery will continue."
        return $null
    }
}

function Get-VenvDeclaredVersion([string]$ConfigPath) {
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return $null }
    $match = [regex]::Match(
        [System.IO.File]::ReadAllText($ConfigPath),
        '(?m)^version\s*=\s*(\d+\.\d+(?:\.\d+)?)\s*$'
    )
    if (-not $match.Success) { return $null }
    try { return [version]$match.Groups[1].Value } catch { return $null }
}

function Repair-PortableVenvConfig([string]$VenvPath, $BasePython) {
    $configPath = Join-Path $VenvPath "pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $configPath) -or -not $BasePython) { return $false }

    $baseExecutable = [System.IO.Path]::GetFullPath([string]$BasePython.Path)
    $baseHome = Split-Path -Parent $baseExecutable
    $venvFullPath = [System.IO.Path]::GetFullPath($VenvPath)
    $desired = [ordered]@{
        home = $baseHome
        'include-system-site-packages' = 'false'
        version = $BasePython.Version.ToString()
        executable = $baseExecutable
        command = "$baseExecutable -m venv $venvFullPath"
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $updatedLines = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in [System.IO.File]::ReadAllLines($configPath)) {
        $separator = $line.IndexOf('=')
        $key = if ($separator -ge 0) { $line.Substring(0, $separator).Trim() } else { "" }
        if ($key -and $desired.Contains($key)) {
            $updatedLines.Add("$key = $($desired[$key])")
            [void]$seen.Add($key)
        } else {
            $updatedLines.Add($line)
        }
    }
    foreach ($key in $desired.Keys) {
        if (-not $seen.Contains($key)) { $updatedLines.Add("$key = $($desired[$key])") }
    }

    $oldText = [System.IO.File]::ReadAllText($configPath)
    $newText = ($updatedLines -join [Environment]::NewLine) + [Environment]::NewLine
    if ($oldText -eq $newText) { return $false }
    [System.IO.File]::WriteAllText($configPath, $newText, (New-Object System.Text.UTF8Encoding($false)))
    return $true
}

function Repair-PortablePythonScriptShebangs([string]$VenvPath) {
    $scriptsPath = Join-Path $VenvPath "Scripts"
    if (-not (Test-Path -LiteralPath $scriptsPath -PathType Container)) { return $false }
    $changed = $false
    $encoding = New-Object System.Text.UTF8Encoding($false)
    foreach ($script in Get-ChildItem -LiteralPath $scriptsPath -Filter "*.py" -File) {
        $lines = [System.IO.File]::ReadAllLines($script.FullName)
        if (
            $lines.Count -gt 0 -and
            $lines[0].StartsWith("#!") -and
            $lines[0] -match '(?i)(?:^|[\\/])python(?:\.exe)?\s*$' -and
            $lines[0] -ne "#!python"
        ) {
            $lines[0] = "#!python"
            [System.IO.File]::WriteAllLines($script.FullName, $lines, $encoding)
            $changed = $true
        }
    }
    return $changed
}

function Repair-PythonEditableMetadata([string]$VenvPath) {
    $sitePackages = Join-Path $VenvPath "Lib\site-packages"
    if (-not (Test-Path -LiteralPath $sitePackages)) { return $false }
    $changed = $false
    $encoding = New-Object System.Text.UTF8Encoding($false)

    # Builds before the 1.0 rename left a second editable distribution in the
    # bundled environment.  Its .pth file points at the same source tree, but
    # duplicate package metadata confuses dependency audits and can leave old
    # console entry points selected.  Remove only the two known generated
    # legacy artifacts; the actual deepdesk Python package remains untouched.
    foreach ($legacyName in @(
        "deepdesk_agent-0.1.0.dist-info",
        "_editable_impl_deepdesk_agent.pth"
    )) {
        $legacyPath = Join-Path $sitePackages $legacyName
        if (Test-Path -LiteralPath $legacyPath) {
            Remove-Item -LiteralPath $legacyPath -Recurse -Force
            $changed = $true
        }
    }

    foreach ($distribution in @(
        @{ Editable = "_editable_impl_elren_agent.pth"; Metadata = "elren_agent-1.0.0.dist-info" },
        @{ Editable = "_editable_impl_milo_agent.pth"; Metadata = "milo_agent-1.0.0.dist-info" },
        @{ Editable = "_editable_impl_qelvane_desk_agent.pth"; Metadata = "qelvane_desk_agent-1.0.0.dist-info" }
    )) {
        $editablePath = Join-Path $sitePackages $distribution.Editable
        if (Test-Path -LiteralPath $editablePath) {
            $desiredPath = $ProjectRoot + [Environment]::NewLine
            if ([System.IO.File]::ReadAllText($editablePath) -ne $desiredPath) {
                [System.IO.File]::WriteAllText($editablePath, $desiredPath, $encoding)
                $changed = $true
            }
        }
        $directUrlPath = Join-Path (Join-Path $sitePackages $distribution.Metadata) "direct_url.json"
        if (Test-Path -LiteralPath $directUrlPath) {
            $projectUri = ([Uri]$ProjectRoot).AbsoluteUri.TrimEnd('/')
            $desiredJson = (@{ dir_info = @{ editable = $true }; url = $projectUri } | ConvertTo-Json -Compress)
            if ([System.IO.File]::ReadAllText($directUrlPath).Trim() -ne $desiredJson) {
                [System.IO.File]::WriteAllText($directUrlPath, $desiredJson, $encoding)
                $changed = $true
            }
        }
    }

    # Once the Elren distribution is installed, remove only the two generated
    # package-identity artifacts from the former product name. The import
    # module itself stays `deepdesk`, so third-party plugins remain compatible.
    if (
        (Test-Path -LiteralPath (Join-Path $sitePackages "_editable_impl_elren_agent.pth")) -and
        (Test-Path -LiteralPath (Join-Path $sitePackages "elren_agent-1.0.0.dist-info"))
    ) {
        foreach ($legacyName in @(
            "_editable_impl_milo_agent.pth",
            "milo_agent-1.0.0.dist-info",
            "_editable_impl_qelvane_desk_agent.pth",
            "qelvane_desk_agent-1.0.0.dist-info"
        )) {
            $legacyPath = Join-Path $sitePackages $legacyName
            if (Test-Path -LiteralPath $legacyPath) {
                Remove-Item -LiteralPath $legacyPath -Recurse -Force
                $changed = $true
            }
        }
    }
    return $changed
}

function Get-NodeVersion([string]$Executable) {
    try {
        $text = (& $Executable --version 2>$null | Select-Object -Last 1)
        if (-not $text) { return $null }
        $clean = $text.Trim().TrimStart('v').Split('-')[0]
        return [version]$clean
    } catch {
        return $null
    }
}

function Test-CompatibleNodeVersion([version]$Version) {
    if (-not $Version) { return $false }
    if ($Version.Major -eq 22) { return $Version -ge [version]"22.22.3" }
    if ($Version.Major -eq 24) { return $Version -ge [version]"24.15.0" }
    if ($Version -ge [version]"25.9.0") { return $true }
    return $false
}

function Get-BundledNode {
    $executable = Join-Path $ProjectRoot "work\node-runtime\node.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { return $null }
    $signature = Get-AuthenticodeSignature -LiteralPath $executable
    $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { "" }
    if (
        $signature.Status -ne "Valid" -or
        (
            $subject.IndexOf("OpenJS Foundation", [System.StringComparison]::OrdinalIgnoreCase) -lt 0 -and
            $subject.IndexOf("Node.js Foundation", [System.StringComparison]::OrdinalIgnoreCase) -lt 0
        )
    ) {
        Write-StartupStage "Ignored the package-local Node.js runtime because its official signature could not be verified."
        return $null
    }
    $version = Get-NodeVersion $executable
    if (-not (Test-CompatibleNodeVersion $version)) { return $null }
    $declared = Get-RuntimeBundleComponent "node"
    if ($declared -and $declared.version -and $version -ne [version]$declared.version) {
        Write-StartupStage "Ignored the package-local Node.js runtime because it does not match the portable runtime manifest."
        return $null
    }
    return [pscustomobject]@{ Path = $executable; Version = $version }
}

function Get-HighestPathNode {
    if ($env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1") {
        if ($script:SimulatedNodeInstalled) {
            return [pscustomobject]@{ Path = "simulated-node.exe"; Version = [version]"24.15.0" }
        }
        return $null
    }
    if ($env:ELREN_SIMULATE_LEGACY_COMPUTER -eq "1") {
        return [pscustomobject]@{ Path = "simulated-node.exe"; Version = [version]"24.18.1" }
    }

    # Get-Command only returns the first matching executable. Enumerate every
    # PATH entry so an older Node.js earlier in PATH cannot hide a newer,
    # supported installation later in PATH.
    $paths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($directory in ([string]$env:Path -split ';')) {
        $cleanDirectory = $directory.Trim().Trim('"')
        if (-not $cleanDirectory) { continue }
        $candidate = Join-Path $cleanDirectory "node.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $paths.Add($candidate) }
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $versions = @()
    foreach ($path in $paths) {
        $fullPath = [System.IO.Path]::GetFullPath($path)
        if (-not $seen.Add($fullPath)) { continue }
        $version = Get-NodeVersion $fullPath
        if ($version) { $versions += [pscustomobject]@{ Path = $fullPath; Version = $version } }
    }
    return $versions | Sort-Object Version -Descending | Select-Object -First 1
}

function Ensure-CompatibleNode {
    $bundledNode = Get-BundledNode
    if ($bundledNode) {
        $env:Path = (Split-Path -Parent $bundledNode.Path) + ";" + $env:Path
        $env:ELREN_BUNDLED_NODE = $bundledNode.Path
        Write-StartupStage "Using the verified package-local Node.js runtime: $($bundledNode.Version)"
        return $bundledNode
    }
    $node = Get-HighestPathNode
    if ($node -and (Test-CompatibleNodeVersion $node.Version)) {
        $env:Path = (Split-Path -Parent $node.Path) + ";" + $env:Path
        return $node
    }

    if ($node) {
        Write-StartupStage "The highest Node.js on PATH is $($node.Version) at $($node.Path), but it does not satisfy the bundled OpenClaw runtime requirement."
    } else {
        Write-StartupStage "Node.js was not found on PATH."
    }

    $installer = Get-TrustedLocalInstaller @(
        "node-v*-x64.msi",
        "没node或版本不合适先安装.msi",
        "node-installer.msi"
    ) @("OpenJS Foundation", "Node.js Foundation")
    if (-not $installer) {
        throw "A compatible Node.js runtime was not found. Put the official signed Node.js 24 x64 MSI in this folder (for example node-v24.18.1-x64.msi), then double-click Elren.exe again."
    }

    Write-StartupStage "Compatible Node.js is missing. Opening the verified official Node.js installer..."
    $arguments = @("/i", ('"' + $installer + '"'))
    if ($env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1") {
        Write-StartupStage "SIMULATION: would open the visible Node.js MSI through msiexec.exe: $installer"
        $script:SimulatedNodeInstalled = $true
        $installProcess = [pscustomobject]@{ ExitCode = 0 }
    } else {
        $installProcess = Start-Process -FilePath "msiexec.exe" -ArgumentList $arguments -Wait -PassThru
    }
    if ($installProcess.ExitCode -notin @(0, 3010)) { throw "The Node.js installer did not finish successfully (exit code $($installProcess.ExitCode))." }
    $node = Get-HighestPathNode
    if (-not $node -or -not (Test-CompatibleNodeVersion $node.Version)) {
        throw "Node.js installation finished, but a supported runtime could not be found on PATH. Complete the installer and launch Elren again."
    }
    $env:Path = (Split-Path -Parent $node.Path) + ";" + $env:Path
    return $node
}

if ($env:ELREN_SIMULATE_MISSING_RUNTIMES -eq "1") {
    Write-StartupStage "Simulating a computer with no system Python or Node.js. Package runtimes must work without opening installers."
    $SimulatedPython = Get-BundledPythonBase
    $SimulatedNode = Get-BundledNode
    if (-not $SimulatedPython -or -not $SimulatedNode) {
        throw "Missing-system-runtime simulation failed because the portable package is incomplete."
    }
    Write-StartupStage "Missing-system-runtime simulation passed: signed package-local Python and Node.js were selected; no installer or network access is required."
    exit 0
}

if ($env:ELREN_SIMULATE_LEGACY_COMPUTER -eq "1") {
    Write-StartupStage "Simulating a slower computer with Python 3.12 and compatible Node.js already installed."
    $simulationConfig = Join-Path $ProjectRoot ".venv\pyvenv.cfg"
    $simulationDeclared = Get-VenvDeclaredVersion $simulationConfig
    if (-not $simulationDeclared) { throw "The packaged virtual environment has no declared Python version." }
    $legacyPython = Get-HighestPathPython
    if ($legacyPython.Version.Major -ne 3 -or $legacyPython.Version.Minor -ne 12) {
        throw "Legacy-computer simulation did not expose Python 3.12."
    }
    $matchingPython = Get-BundledPythonBase
    if (-not $matchingPython) {
        throw "Legacy-computer simulation requires the package-local Python base runtime."
    }
    $simulatedNode = Ensure-CompatibleNode
    if (
        $matchingPython.Version.Major -ne $simulationDeclared.Major -or
        $matchingPython.Version.Minor -ne $simulationDeclared.Minor -or
        -not $simulatedNode
    ) {
        throw "Legacy-computer portable-runtime simulation failed."
    }
    Write-StartupStage "Legacy-computer simulation passed: PATH Python 3.12 is accepted, the package-local base preserves dependencies, and existing Node.js is reused."
    exit 0
}

Write-StartupStage "Elren startup preparation began."

# A Docker-like tool filesystem is shipped as ordinary movable Windows files,
# so users do not need Docker Desktop, WSL, or administrator installation.
# Only a validated package-local bin directory is placed before the host PATH.
$ToolRuntimeRoot = Join-Path $ProjectRoot "work\tool-runtime"
$ToolRuntimeManifestPath = Join-Path $ToolRuntimeRoot "manifest.json"
$ToolRuntimeBin = ""
function Enable-PackageToolRuntimePath {
    if (-not $ToolRuntimeBin -or -not (Test-Path -LiteralPath $ToolRuntimeBin -PathType Container)) { return }
    $remaining = @(([string]$env:Path -split ';') | Where-Object {
        $_ -and $_.Trim().Trim('"').TrimEnd('\') -ne $ToolRuntimeBin.TrimEnd('\')
    })
    $env:Path = $ToolRuntimeBin + ";" + ($remaining -join ";")
    $env:ELREN_TOOL_RUNTIME = $ToolRuntimeRoot
}
if (Test-Path -LiteralPath $ToolRuntimeManifestPath -PathType Leaf) {
    try {
        $ToolRuntimeManifest = [System.IO.File]::ReadAllText($ToolRuntimeManifestPath) | ConvertFrom-Json
        if ($ToolRuntimeManifest.schema -ne 1 -or $ToolRuntimeManifest.bundle -ne "elren-tool-runtime" -or $ToolRuntimeManifest.product_version -ne "1.0") {
            throw "Unsupported package-local tool runtime manifest."
        }
        $ToolRuntimeBin = [System.IO.Path]::GetFullPath((Join-Path $ToolRuntimeRoot "bin"))
        if (-not $ToolRuntimeBin.StartsWith($ToolRuntimeRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Tool runtime bin directory escapes the package."
        }
        if (Test-Path -LiteralPath $ToolRuntimeBin -PathType Container) {
            Enable-PackageToolRuntimePath
            Write-StartupStage "Package-local tool runtime is active; Docker is not required."
        }
    } catch {
        Write-StartupStage "Ignored an invalid package-local tool runtime; host-native fallbacks remain available."
    }
}

$ServicePort = 8765
$ConfiguredServicePort = [string]$env:ELREN_PORT
if (-not $ConfiguredServicePort) {
    $EnvironmentFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) {
        foreach ($RawEnvironmentLine in [System.IO.File]::ReadAllLines($EnvironmentFile)) {
            $EnvironmentLine = $RawEnvironmentLine.Trim()
            if (-not $EnvironmentLine -or $EnvironmentLine.StartsWith("#")) { continue }
            $EnvironmentSeparator = $EnvironmentLine.IndexOf('=')
            if ($EnvironmentSeparator -le 0) { continue }
            $EnvironmentName = $EnvironmentLine.Substring(0, $EnvironmentSeparator).Trim()
            if (
                -not $EnvironmentName.Equals("ELREN_PORT", [System.StringComparison]::OrdinalIgnoreCase) -and
                -not $EnvironmentName.Equals("DEEPDESK_PORT", [System.StringComparison]::OrdinalIgnoreCase)
            ) { continue }
            $ConfiguredServicePort = $EnvironmentLine.Substring($EnvironmentSeparator + 1).Trim().Trim('"').Trim("'")
            break
        }
    }
}
if ($ConfiguredServicePort) {
    $parsedServicePort = 0
    if (-not [int]::TryParse($ConfiguredServicePort, [ref]$parsedServicePort) -or $parsedServicePort -lt 1 -or $parsedServicePort -gt 65535) {
        throw "ELREN_PORT must be an integer from 1 through 65535."
    }
    $ServicePort = $parsedServicePort
    $env:ELREN_PORT = $ServicePort.ToString([System.Globalization.CultureInfo]::InvariantCulture)
}
$VenvDirectory = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"
$VenvConfig = Join-Path $VenvDirectory "pyvenv.cfg"

# A prepared portable package should not spend tens of seconds rediscovering
# every Python/Node installation, launching runtimes merely to ask their
# versions, importing every dependency, or recounting hundreds of skills.
# The full signed-runtime and dependency audit below remains the cold/repair
# path. This fast path only activates when the location-independent dependency
# stamps and every essential package-local runtime artifact already agree.
$WarmLaunchAllowed = (
    $env:ELREN_BOOTSTRAP_ONLY -ne "1" -and
    $env:ELREN_SIMULATE_MISSING_RUNTIMES -ne "1" -and
    $env:ELREN_SIMULATE_LEGACY_COMPUTER -ne "1"
)
if ($WarmLaunchAllowed) {
    try {
        $WarmBundlePath = Join-Path $ProjectRoot "work\runtime-bundle.json"
        $WarmBundle = [System.IO.File]::ReadAllText($WarmBundlePath) | ConvertFrom-Json
        if ($WarmBundle.schema -ne 1 -or $WarmBundle.bundle -ne "elren-portable-runtime") {
            throw "Portable runtime manifest is not valid."
        }
        $WarmPythonVersion = [version]$WarmBundle.components.python.version
        $WarmNodeVersion = [version]$WarmBundle.components.node.version
        $WarmOpenClawVersion = [string]$WarmBundle.components.openclaw.version
        $WarmPythonBase = Join-Path $ProjectRoot "work\python-runtime\python.exe"
        $WarmNode = Join-Path $ProjectRoot "work\node-runtime\node.exe"
        $WarmRuntimeRoot = Join-Path $ProjectRoot "work\openclaw-runtime"
        $WarmNodeManifest = Join-Path $WarmRuntimeRoot "package.json"
        $WarmNodeLock = Join-Path $WarmRuntimeRoot "pnpm-lock.yaml"
        $WarmOpenClawManifest = Join-Path $WarmRuntimeRoot "node_modules\openclaw\package.json"
        $WarmOpenClawEntry = Join-Path $WarmRuntimeRoot "node_modules\openclaw\openclaw.mjs"
        $WarmOpenClawSkills = Join-Path $WarmRuntimeRoot "node_modules\openclaw\skills"
        $WarmMcpManifest = Join-Path $WarmRuntimeRoot "node_modules\@modelcontextprotocol\sdk\package.json"
        $WarmModulesState = Join-Path $WarmRuntimeRoot "node_modules\.modules.yaml"
        $WarmProjectManifest = Join-Path $ProjectRoot "pyproject.toml"
        $WarmPythonStampPath = Join-Path $VenvDirectory ".elren-python-dependencies-v2"
        $WarmNodeStampPath = Join-Path $WarmRuntimeRoot "node_modules\.elren-node-dependencies-v2"
        $WarmSkillsStampPath = Join-Path $ProjectRoot "skills\openclaw-bundled\.elren-openclaw-skills-v1"
        $WarmInstalledOpenClaw = ([System.IO.File]::ReadAllText($WarmOpenClawManifest) | ConvertFrom-Json).version
        $WarmDesiredOpenClaw = ([System.IO.File]::ReadAllText($WarmNodeManifest) | ConvertFrom-Json).dependencies.openclaw
        $WarmProjectFingerprint = (Get-FileHash -LiteralPath $WarmProjectManifest -Algorithm SHA256).Hash
        $WarmExpectedPythonStamp = "$($WarmPythonVersion.ToString())|$WarmProjectFingerprint"
        $WarmNodeFingerprintParts = @($WarmNodeVersion.ToString(), $WarmDesiredOpenClaw)
        foreach ($WarmDependencyFile in @($WarmNodeManifest, $WarmNodeLock)) {
            if (Test-Path -LiteralPath $WarmDependencyFile) {
                $WarmNodeFingerprintParts += (Get-FileHash -LiteralPath $WarmDependencyFile -Algorithm SHA256).Hash
            }
        }
        $WarmExpectedNodeStamp = $WarmNodeFingerprintParts -join "|"
        $WarmPythonStamp = Read-TrimmedFile $WarmPythonStampPath
        $WarmNodeStamp = Read-TrimmedFile $WarmNodeStampPath
        $WarmSkillsStamp = Read-TrimmedFile $WarmSkillsStampPath
        $WarmVenvVersion = Get-VenvDeclaredVersion $VenvConfig
        $WarmReady = (
            (Test-Path -LiteralPath $VenvPython -PathType Leaf) -and
            (Test-Path -LiteralPath $WarmPythonBase -PathType Leaf) -and
            (Test-Path -LiteralPath $WarmNode -PathType Leaf) -and
            (Test-Path -LiteralPath $WarmMcpManifest -PathType Leaf) -and
            (Test-Path -LiteralPath $WarmOpenClawEntry -PathType Leaf) -and
            (Test-Path -LiteralPath $WarmOpenClawSkills -PathType Container) -and
            (Test-Path -LiteralPath $WarmModulesState -PathType Leaf) -and
            ([System.IO.File]::ReadAllText($WarmModulesState) -match '(?m)nodeLinker"?\s*:\s*"?hoisted') -and
            $WarmVenvVersion -and
            $WarmVenvVersion.Major -eq $WarmPythonVersion.Major -and
            $WarmVenvVersion.Minor -eq $WarmPythonVersion.Minor -and
            $WarmPythonStamp.StartsWith($WarmExpectedPythonStamp, [System.StringComparison]::OrdinalIgnoreCase) -and
            $WarmNodeStamp -eq $WarmExpectedNodeStamp -and
            $WarmInstalledOpenClaw -eq $WarmDesiredOpenClaw -and
            $WarmInstalledOpenClaw -eq $WarmOpenClawVersion -and
            $WarmSkillsStamp.StartsWith($WarmOpenClawVersion + "|", [System.StringComparison]::OrdinalIgnoreCase)
        )
        if ($WarmReady) {
            $WarmPython = [pscustomobject]@{ Path = $WarmPythonBase; Version = $WarmPythonVersion }
            [void](Repair-PortableVenvConfig $VenvDirectory $WarmPython)
            [void](Repair-PortablePythonScriptShebangs $VenvDirectory)
            [void](Repair-PythonEditableMetadata $VenvDirectory)
            $env:ELREN_BUNDLED_PYTHON = $WarmPythonBase
            $env:ELREN_BUNDLED_NODE = $WarmNode
            $env:Path = (Split-Path -Parent $WarmPythonBase) + ";" + (Split-Path -Parent $WarmNode) + ";" + $env:Path
            Enable-PackageToolRuntimePath
            Write-StartupStage "Prepared portable runtime detected; skipped repeated dependency discovery."
            Stop-StaleElrenService $ServicePort
            Stop-StaleElrenWorkers
            Write-StartupStage "Starting the local Elren service..."
            $env:PYTHONUNBUFFERED = "1"
            Invoke-ElrenService $VenvPython
        }
    } catch {
        $warmFailure = ([string]$_.Exception.Message -replace '[\r\n]+', ' ').Trim()
        if (-not $warmFailure) { $warmFailure = "unknown fast-path error" }
        Write-StartupStage "Portable runtime fast-path check failed ($warmFailure); continuing with a full verification pass."
    }
}

$BundledBasePython = Get-BundledPythonBase
$PathPython = if ($BundledBasePython) { $null } else { Get-HighestPathPython }
if ($BundledBasePython) {
    $PythonBootstrap = $BundledBasePython
    $env:Path = (Split-Path -Parent $BundledBasePython.Path) + ";" + $env:Path
    $env:ELREN_BUNDLED_PYTHON = $BundledBasePython.Path
    Write-StartupStage "Using the verified package-local Python runtime: $($BundledBasePython.Version)"
} elseif (-not $PathPython -or $PathPython.Version -lt [version]"3.11.0") {
    $detectedLabel = if ($PathPython) { $PathPython.Version.ToString() } else { "none" }
    Write-StartupStage "Highest Python on PATH is $detectedLabel; Python 3.11 or newer is required."
    $PathPython = Ensure-CompatiblePython -ForceInstaller
    $env:Path = (Split-Path -Parent $PathPython.Path) + ";" + $env:Path
    $PythonBootstrap = $PathPython
} else {
    Write-StartupStage "Using the highest Python on PATH: $($PathPython.Version) from $($PathPython.Path)"
    $PythonBootstrap = $PathPython
}
$VenvDeclaredVersion = Get-VenvDeclaredVersion $VenvConfig
$MatchingBasePython = $null
if (
    $VenvDeclaredVersion -and $BundledBasePython -and
    $BundledBasePython.Version.Major -eq $VenvDeclaredVersion.Major -and
    $BundledBasePython.Version.Minor -eq $VenvDeclaredVersion.Minor
) {
    $MatchingBasePython = $BundledBasePython
    Write-StartupStage "Using the verified package-local Python base runtime for the movable environment."
} elseif ($VenvDeclaredVersion) {
    $MatchingBasePython = Get-CompatiblePython -PreferredVersion $VenvDeclaredVersion
}
if ($MatchingBasePython -and (Repair-PortableVenvConfig $VenvDirectory $MatchingBasePython)) {
    Write-StartupStage "Repaired the movable Python environment for this computer without deleting bundled dependencies."
}
[void](Repair-PortablePythonScriptShebangs $VenvDirectory)
$VenvVersion = if (Test-Path -LiteralPath $VenvPython) { Get-PythonVersion $VenvPython } else { $null }
if (-not $VenvVersion -or $VenvVersion -lt [version]"3.11.0" -or $VenvVersion -ge [version]"4.0.0") {
    $Python = if ($MatchingBasePython) { $MatchingBasePython } else { $PythonBootstrap }
    if (Test-Path -LiteralPath $VenvPython) {
        Write-StartupStage "Repairing the incompatible or damaged Python virtual environment..."
        & $Python.Path -m venv --clear $VenvDirectory
    } else {
        Write-StartupStage "Creating the Python virtual environment (first launch only)..."
        & $Python.Path -m venv $VenvDirectory
    }
    if ($LASTEXITCODE -ne 0) { throw "Python virtual environment creation failed." }
    [void](Repair-PortablePythonScriptShebangs $VenvDirectory)
    $VenvVersion = Get-PythonVersion $VenvPython
}

if (-not $VenvVersion) { throw "Unable to inspect the virtual-environment Python version." }
if (Repair-PythonEditableMetadata $VenvDirectory) {
    Write-StartupStage "Repaired movable Python package metadata for the current folder."
}
$PythonIdentity = $VenvVersion.ToString()
$ProjectManifest = Join-Path $ProjectRoot "pyproject.toml"
$ProjectFingerprint = (Get-FileHash -LiteralPath $ProjectManifest -Algorithm SHA256).Hash
$PythonDependencyStamp = Join-Path $VenvDirectory ".elren-python-dependencies-v2"
# The app starts from ProjectRoot, so its source is already first on sys.path.
# Keeping the absolute extraction path in this stamp forced a full online pip
# reinstall every time the ZIP was moved to another PC or folder.
$ExpectedPythonStamp = "$PythonIdentity|$ProjectFingerprint"
$StoredPythonStamp = Read-TrimmedFile $PythonDependencyStamp
$PythonDependenciesReady = (
    $StoredPythonStamp -eq $ExpectedPythonStamp -or
    $StoredPythonStamp.StartsWith($ExpectedPythonStamp + "|", [System.StringComparison]::OrdinalIgnoreCase)
)

if (-not $PythonDependenciesReady) {
    Write-StartupStage "Installing or relocating Python dependencies. This can take several minutes on the first launch..."
    & $VenvPython -m pip install --disable-pip-version-check --no-input --progress-bar off -e $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
    [void](Repair-PythonEditableMetadata $VenvDirectory)
    [System.IO.File]::WriteAllText($PythonDependencyStamp, $ExpectedPythonStamp)
    Write-StartupStage "Python dependencies are ready for: $ProjectRoot"
} else {
    if ($StoredPythonStamp -ne $ExpectedPythonStamp) {
        [System.IO.File]::WriteAllText($PythonDependencyStamp, $ExpectedPythonStamp)
        Write-StartupStage "Migrated the Python dependency cache to a location-independent stamp."
    }
    Write-StartupStage "Python dependencies are unchanged; skipped installation."
}

$Node = Ensure-CompatibleNode
Write-StartupStage "Using Node.js $($Node.Version) from $($Node.Path)"

$RuntimeRoot = Join-Path $ProjectRoot "work\openclaw-runtime"
$McpSdkManifest = Join-Path $RuntimeRoot "node_modules\@modelcontextprotocol\sdk\package.json"
$NodeManifest = Join-Path $RuntimeRoot "package.json"
$NodeLock = Join-Path $RuntimeRoot "pnpm-lock.yaml"
$OpenClawManifest = Join-Path $RuntimeRoot "node_modules\openclaw\package.json"
$OpenClawEntry = Join-Path $RuntimeRoot "node_modules\openclaw\openclaw.mjs"
$OpenClawSkills = Join-Path $RuntimeRoot "node_modules\openclaw\skills"
$PnpmModulesState = Join-Path $RuntimeRoot "node_modules\.modules.yaml"
$DesiredOpenClawVersion = ([System.IO.File]::ReadAllText($NodeManifest) | ConvertFrom-Json).dependencies.openclaw
$InstalledOpenClawVersion = ""
if (Test-Path -LiteralPath $OpenClawManifest) {
    try { $InstalledOpenClawVersion = ([System.IO.File]::ReadAllText($OpenClawManifest) | ConvertFrom-Json).version } catch { $InstalledOpenClawVersion = "" }
}

$NodeFingerprintParts = @($Node.Version.ToString(), $DesiredOpenClawVersion)
foreach ($DependencyFile in @($NodeManifest, $NodeLock)) {
    if (Test-Path -LiteralPath $DependencyFile) {
        $NodeFingerprintParts += (Get-FileHash -LiteralPath $DependencyFile -Algorithm SHA256).Hash
    }
}
$ExpectedNodeStamp = $NodeFingerprintParts -join "|"
$NodeDependencyStamp = Join-Path $RuntimeRoot "node_modules\.elren-node-dependencies-v2"
$SystemOpenClaw = Get-Command openclaw.cmd -ErrorAction SilentlyContinue
if (-not $SystemOpenClaw) { $SystemOpenClaw = Get-Command openclaw -ErrorAction SilentlyContinue }
$SystemOpenClawPath = if ($SystemOpenClaw -and $SystemOpenClaw.Source) {
    [System.IO.Path]::GetFullPath($SystemOpenClaw.Source)
} else { "" }
$UseSystemOpenClawFallback = (
    -not (Test-Path -LiteralPath $OpenClawEntry) -and
    $SystemOpenClawPath -and
    -not $SystemOpenClawPath.StartsWith($RuntimeRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)
)
$BundledNodeDependenciesComplete = (
    (Test-Path -LiteralPath $McpSdkManifest) -and
    (Test-Path -LiteralPath $OpenClawEntry) -and
    (Test-Path -LiteralPath $OpenClawSkills) -and
    (Test-Path -LiteralPath $PnpmModulesState) -and
    ([System.IO.File]::ReadAllText($PnpmModulesState) -match '(?m)nodeLinker"?\s*:\s*"?hoisted') -and
    $InstalledOpenClawVersion -eq $DesiredOpenClawVersion
)
$NodeDependenciesReady = $BundledNodeDependenciesComplete -and ((Read-TrimmedFile $NodeDependencyStamp) -eq $ExpectedNodeStamp)

if ($BundledNodeDependenciesComplete -and -not $NodeDependenciesReady) {
    [System.IO.File]::WriteAllText($NodeDependencyStamp, $ExpectedNodeStamp)
    $NodeDependenciesReady = $true
    Write-StartupStage "Verified the bundled OpenClaw $InstalledOpenClawVersion runtime and skills; skipped network installation."
}

if (-not $NodeDependenciesReady -and -not $UseSystemOpenClawFallback) {
    Write-StartupStage "Installing the locked OpenClaw $DesiredOpenClawVersion runtime, skills, and MCP dependencies..."
    $Pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if (-not $Pnpm) { $Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue }
    if ($Pnpm) {
        # The isolated pnpm linker stores directory junctions and an absolute
        # virtualStoreDir. Ordinary ZIP tools cannot preserve those junctions,
        # so a moved/extracted package may look "up to date" while its MCP and
        # OpenClaw links are empty. Hoisted mode materializes a portable tree;
        # --force also repairs packages created by older isolated builds.
        & $Pnpm.Source --dir $RuntimeRoot install --ignore-scripts --frozen-lockfile --force --config.node-linker=hoisted
    } else {
        $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $Npm) { $Npm = Get-Command npm -ErrorAction SilentlyContinue }
        if (-not $Npm) { throw "npm was not found beside the compatible Node.js installation." }
        # npm is a compatibility fallback when pnpm is unavailable. The same
        # security overrides are duplicated in package.json so this path does
        # not silently reinstall vulnerable transitive versions.
        & $Npm.Source install --prefix $RuntimeRoot --omit=dev --ignore-scripts --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) { throw "OpenClaw/MCP Node dependency installation failed." }
    if (-not (Test-Path -LiteralPath $McpSdkManifest)) { throw "The MCP SDK is still missing after rebuilding the portable Node.js runtime." }
    if (-not (Test-Path -LiteralPath $OpenClawManifest)) { throw "OpenClaw is still missing after rebuilding the portable Node.js runtime." }
    if (-not (Test-Path -LiteralPath $OpenClawEntry)) { throw "The package-local OpenClaw entry point is still missing after rebuilding the portable Node.js runtime." }
    $InstalledOpenClawVersion = ([System.IO.File]::ReadAllText($OpenClawManifest) | ConvertFrom-Json).version
    if ($InstalledOpenClawVersion -ne $DesiredOpenClawVersion) {
        throw "OpenClaw version mismatch: expected $DesiredOpenClawVersion, installed $InstalledOpenClawVersion."
    }
    [System.IO.File]::WriteAllText($NodeDependencyStamp, $ExpectedNodeStamp)
    Write-StartupStage "OpenClaw/MCP dependencies are ready."
} elseif ($UseSystemOpenClawFallback) {
    Write-StartupStage "Package-local OpenClaw is missing; Elren will fall back to the computer installation at $SystemOpenClawPath."
} else {
    Write-StartupStage "OpenClaw/MCP dependencies are unchanged; skipped installation."
}

$BundledSkillsSource = Join-Path $RuntimeRoot "node_modules\openclaw\skills"
$PackageSkillsRoot = Join-Path $ProjectRoot "skills\openclaw-bundled"
$PackageSkillsStamp = Join-Path $PackageSkillsRoot ".elren-openclaw-skills-v1"
$SourceSkillCount = @(Get-ChildItem -LiteralPath $BundledSkillsSource -Filter "SKILL.md" -Recurse -File -ErrorAction SilentlyContinue).Count
$PackageSkillCount = @(Get-ChildItem -LiteralPath $PackageSkillsRoot -Filter "SKILL.md" -Recurse -File -ErrorAction SilentlyContinue).Count
$ExpectedSkillsStamp = "$InstalledOpenClawVersion|$SourceSkillCount"
if (
    $SourceSkillCount -gt 0 -and
    ((Read-TrimmedFile $PackageSkillsStamp) -ne $ExpectedSkillsStamp -or $PackageSkillCount -lt $SourceSkillCount)
) {
    Write-StartupStage "Synchronizing $SourceSkillCount official OpenClaw skills into the movable package workspace..."
    New-Item -ItemType Directory -Path $PackageSkillsRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $BundledSkillsSource "*") -Destination $PackageSkillsRoot -Recurse -Force
    [System.IO.File]::WriteAllText($PackageSkillsStamp, $ExpectedSkillsStamp)
    $PackageSkillCount = @(Get-ChildItem -LiteralPath $PackageSkillsRoot -Filter "SKILL.md" -Recurse -File).Count
}
if ($PackageSkillCount -lt $SourceSkillCount) {
    throw "The package-local OpenClaw skill synchronization is incomplete: expected $SourceSkillCount, found $PackageSkillCount."
}
Write-StartupStage "Package-local OpenClaw skills ready: $PackageSkillCount (watched dynamically from $PackageSkillsRoot)."

# The same explicit allowlisted directory is first on PATH in warm and repair
# launches. Never add node_modules/.bin wholesale.
Enable-PackageToolRuntimePath

if ($env:ELREN_BOOTSTRAP_ONLY -eq "1") {
    Write-StartupStage "Dependency bootstrap check completed."
    exit 0
}

# Keep an already-running Elren service available while Python, Node,
# OpenClaw, and bundled skills are prepared. Only perform the handoff after
# every potentially slow prerequisite has succeeded, minimizing browser
# disconnection time during upgrades or package relocation.
Stop-StaleElrenService $ServicePort
Stop-StaleElrenWorkers
Write-StartupStage "Starting the local Elren service..."
$env:PYTHONUNBUFFERED = "1"
# Windows PowerShell 5 converts a native program's stderr into an ErrorRecord.
# Uvicorn and dependencies can legitimately write warnings there while staying
# healthy, so do not let the script-level Stop preference abort the service.
Invoke-ElrenService $VenvPython
