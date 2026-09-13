import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_and_readme_support_double_click_startup() -> None:
    source = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "start.ps1" in source
    assert "ELREN_SKIP_AUTO_BROWSER" in source
    assert "ResolveLocalUrl(root)" in source
    assert 'Environment.GetEnvironmentVariable("ELREN_PORT")' in source
    assert 'Path.Combine(root, ".env")' in source
    assert 'args[0].Equals("--self-test"' in source
    assert "DetectChineseSystemLanguage" in source
    assert "CultureInfo.CurrentUICulture.Name" in source
    assert 'language.StartsWith("zh", StringComparison.OrdinalIgnoreCase)' in source
    assert 'info.EnvironmentVariables["ELREN_LAUNCHER_LANGUAGE"]' in source
    assert "new DesktopShellForm(root, ResolveLocalUrl(root), showSignal)" in source
    assert "Microsoft.Web.WebView2.WinForms" in source
    assert "CoreWebView2Environment.CreateAsync" in source
    assert "LaunchEdgeApplicationMode" in source
    assert 'info.EnvironmentVariables["ELREN_HOST"] = "127.0.0.1"' in source
    assert 'info.EnvironmentVariables["ELREN_DESKTOP_SHELL"] = "1"' in source
    assert "Elren.exe" in readme
    assert readme.index("## English") < readme.index("# Elren — 中文说明")
    assert "### Quick start" in readme
    assert "## 快速启动" in readme


def test_manual_start_keeps_browser_open_behavior() -> None:
    main_source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")
    assert '"ELREN_SKIP_AUTO_BROWSER"' in main_source
    assert 'os.getenv("MILO_SKIP_AUTO_BROWSER", os.getenv("DEEPDESK_SKIP_AUTO_BROWSER", ""))' in main_source
    assert 'if skip_browser.strip() != "1"' in main_source
    assert "threading.Timer(1.2" in main_source


def test_startup_uses_dependency_fingerprints_instead_of_reinstalling_every_time() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert ".elren-python-dependencies-v2" in source
    assert ".elren-node-dependencies-v2" in source
    assert "Get-FileHash" in source
    assert '$ExpectedPythonStamp = "$PythonIdentity|$ProjectFingerprint"' in source
    assert '$StoredPythonStamp.StartsWith($ExpectedPythonStamp + "|"' in source
    assert "Python dependencies are unchanged; skipped installation." in source
    assert "OpenClaw/MCP dependencies are unchanged; skipped installation." in source
    assert "ELREN_BOOTSTRAP_ONLY" in source
    assert "--disable-pip-version-check" in source
    assert "pip install -q -e ." not in source
    assert 'Write-Host ("[{0}] {1}"' in source
    assert "Prepared portable runtime detected; skipped repeated dependency discovery." in source
    assert "$WarmLaunchAllowed" in source
    assert "$WarmExpectedPythonStamp" in source
    assert "$WarmExpectedNodeStamp" in source


def test_manual_start_reads_the_private_port_from_dotenv_before_process_handoff() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")
    service_port = source.index("$ServicePort = 8765")
    handoff = source.rindex("Stop-StaleElrenService $ServicePort")

    assert 'Join-Path $ProjectRoot ".env"' in source[service_port:handoff]
    assert 'Equals("ELREN_PORT"' in source[service_port:handoff]
    assert "$env:ELREN_PORT = $ServicePort.ToString" in source[service_port:handoff]


def test_startup_can_repair_python_and_install_verified_local_runtimes() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert '"3.11.0"' in source
    assert "-m venv --clear" in source
    assert '"python-installer.exe"' in source
    assert "没python或在3.11以下先安装.exe" not in source
    assert "Python Software Foundation" in source
    assert "Get-AuthenticodeSignature" in source
    assert "node-v*-x64.msi" in source
    assert "OpenJS Foundation" in source
    assert "msiexec.exe" in source
    assert "ELREN_SIMULATE_MISSING_RUNTIMES" in source
    assert "no system Python or Node.js" in source
    assert "no installer or network access is required" in source
    assert "ELREN_SIMULATE_LEGACY_COMPUTER" in source
    assert "Legacy-computer simulation passed" in source
    assert "function Get-HighestPathPython" in source
    assert 'Sort-Object Version -Descending' in source
    assert '$PathPython.Version -lt [version]"3.11.0"' in source
    assert "function Get-BundledPythonBase" in source
    assert 'work\\python-runtime\\python.exe' in source
    assert "function Get-VenvDeclaredVersion" in source
    assert "function Repair-PortableVenvConfig" in source
    assert "function Repair-PythonEditableMetadata" in source
    assert "_editable_impl_qelvane_desk_agent.pth" in source
    assert "deepdesk_agent-0.1.0.dist-info" in source
    assert "_editable_impl_deepdesk_agent.pth" in source
    assert "direct_url.json" in source
    assert "without deleting bundled dependencies" in source
    assert "Using the verified package-local Python base runtime" in source
    assert "Using the verified package-local Python runtime" in source
    assert "function Get-RuntimeBundleComponent" in source
    assert 'work\\runtime-bundle.json' in source


def test_external_channels_do_not_block_the_local_http_startup() -> None:
    source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")

    assert "async def initialize_message_channels()" in source
    assert "channel_bootstrap_task = asyncio.create_task(initialize_message_channels())" in source
    assert "External channels must never hold the local HTTP interface" in source


def test_launcher_build_supports_verified_release_signing() -> None:
    build = (ROOT / "launcher" / "build-launcher.ps1").read_text(encoding="utf-8")

    assert "ELREN_SIGNING_CERT_THUMBPRINT" in build
    assert "Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert" in build
    assert "Set-AuthenticodeSignature" in build
    assert "-HashAlgorithm SHA256" in build
    assert "-TimestampServer" in build
    assert "$signature.Status -ne \"Valid\"" in build
    assert "[switch]$RequireSigned" in build


def test_launcher_logs_are_written_and_read_as_utf8() -> None:
    source = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")

    assert "new UTF8Encoding(false)" in source
    # PowerShell -EncodedCommand requires UTF-16LE; this is command transport,
    # not a log file. All log writers/readers must still use UTF-8.
    encoded_command = "Convert.ToBase64String(Encoding.Unicode.GetBytes(command))"
    assert "-EncodedCommand " in source
    assert "Encoding.Unicode" not in source.replace(encoded_command, "")
    assert "System.Text.UTF8Encoding($false)" in source
    assert "[IO.File]::AppendAllText" in source
    assert "new StreamReader(stream, Encoding.UTF8, true)" in source


def test_launcher_isolates_windows_powershell_modules_and_reports_real_failures() -> None:
    launcher = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")

    # A launcher started by PowerShell 7 or an IDE must not pass its incompatible
    # module search path into the Windows PowerShell 5.1 bootstrap.
    assert 'info.EnvironmentVariables["PSModulePath"]' in launcher
    assert 'System32\\WindowsPowerShell\\v1.0\\Modules' in launcher
    assert '$PSVersionTable.PSEdition -eq "Desktop"' in script
    assert '$env:PSModulePath = ' in script

    # Early exits must carry the actual script error in both the log and dialog,
    # rather than exposing only an unexplained process exit code.
    assert "Startup preparation failed" in script
    assert "Portable runtime fast-path check failed" in script
    assert "ReadStartupLogTail(latestLogPath, 8)" in launcher


def test_routine_status_poll_does_not_run_slow_runtime_probes() -> None:
    source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")
    status_body = source[source.index('@app.get("/api/status")') : source.index('@app.post("/api/runtimes/probe")')]

    assert "vision_runtime.status(probe=False)" in status_body
    assert "windows_ocr.status(probe=False)" in status_body
    assert "paddle_ocr.status(probe=False)" in status_body
    assert "deepseek_ocr" not in status_body
    assert "openclaw_bridge.status(probe_catalog=False, probe_health=False)" in status_body
    assert "mcp_runtime.status(probe=False)" in status_body


def test_startup_enforces_openclaw_and_node_compatibility() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "function Get-HighestPathNode" in source
    assert "foreach ($directory in ([string]$env:Path -split ';'))" in source
    assert 'Join-Path $cleanDirectory "node.exe"' in source
    assert "return $versions | Sort-Object Version -Descending | Select-Object -First 1" in source
    assert "$node = Get-HighestPathNode" in source
    assert "Test-CompatibleNodeVersion $node.Version" in source
    assert "The highest Node.js on PATH is" in source
    assert '"22.22.3"' in source
    assert '"24.15.0"' in source
    assert '"25.9.0"' in source
    assert "DesiredOpenClawVersion" in source
    assert "InstalledOpenClawVersion -eq $DesiredOpenClawVersion" in source
    assert 'node_modules\\openclaw\\skills' in source
    assert 'node_modules\\openclaw\\openclaw.mjs' in source
    assert "Test-Path -LiteralPath $OpenClawEntry" in source
    assert "UseSystemOpenClawFallback" in source
    assert "Package-local OpenClaw is missing" in source
    assert "Verified the bundled OpenClaw" in source
    assert "function Get-BundledNode" in source
    assert 'work\\node-runtime\\node.exe' in source
    assert "Using the verified package-local Node.js runtime" in source


def test_portable_runtime_bundle_builder_covers_python_node_and_openclaw() -> None:
    builder = (ROOT / "launcher" / "build-runtime-bundle.ps1").read_text(encoding="utf-8")

    assert 'bundle = "elren-portable-runtime"' in builder
    assert 'isolation = "package-first-with-system-fallback"' in builder
    assert '"work/python-runtime/python.exe"' in builder
    assert '"work/node-runtime/node.exe"' in builder
    assert '"work/openclaw-runtime/node_modules/openclaw/openclaw.mjs"' in builder
    assert "Assert-OfficialSignature" in builder
    assert "Python Software Foundation" in builder
    assert "OpenJS Foundation" in builder
    assert 'tool_runtime = [ordered]@{' in builder
    assert 'manifest = "work/tool-runtime/manifest.json"' in builder
    assert 'docker_required = $false' in builder


def test_tool_runtime_python_shim_uses_project_environment_with_debugpy() -> None:
    builder = (ROOT / "launcher" / "build-tool-runtime.ps1").read_text(encoding="utf-8")
    verifier = (ROOT / "launcher" / "verify-tool-runtime.py").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"' in builder
    assert '"debugpy==1.8.21"' in project
    assert "import debugpy; print(debugpy.__version__)" in verifier


def test_startup_activates_package_tool_runtime_without_docker() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert '$env:ELREN_WORKSPACE = $ProjectRoot' in source
    assert 'work\\tool-runtime' in source
    assert 'bundle -ne "elren-tool-runtime"' in source
    assert '$env:Path = $ToolRuntimeBin + ";" + ($remaining -join ";")' in source
    assert "Enable-PackageToolRuntimePath" in source
    assert "Docker is not required" in source


def test_startup_repairs_python_script_shebangs_after_package_move() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "function Repair-PortablePythonScriptShebangs" in source
    assert source.count("Repair-PortablePythonScriptShebangs $VenvDirectory") >= 3
    assert '$lines[0] = "#!python"' in source


def test_portable_runtime_bundle_is_visible_in_runtime_diagnostics() -> None:
    main_source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")
    app_source = (ROOT / "deepdesk" / "static" / "app.js").read_text(encoding="utf-8")

    assert "def portable_runtime_status" in main_source
    assert '"runtime_bundle": portable_runtime_status(settings.workspace)' in main_source
    assert '"Portable runtime"' in app_source
    assert '"便携运行时"' in app_source


def test_startup_materializes_official_openclaw_skills_in_the_movable_package() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert '"skills\\openclaw-bundled"' in source
    assert ".elren-openclaw-skills-v1" in source
    assert "SourceSkillCount" in source
    assert "PackageSkillCount -lt $SourceSkillCount" in source
    assert "Copy-Item" in source


def test_startup_repairs_absolute_openclaw_junctions_after_package_move() -> None:
    source = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "function Repair-MovedOpenClawJunctions" in source
    assert "[System.IO.Directory]::Delete($linkPath)" in source
    assert '$marker = "\\work\\openclaw-runtime\\"' in source
    assert "New-Item -ItemType Junction" in source
    assert ".elren-package-location-v1" in source
    assert "PreviousPackageLocation.Equals($ProjectRoot" in source


def test_launcher_replaces_only_a_verified_stale_elren_port_owner() -> None:
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")

    assert "Stop-StaleElrenService" in script
    assert "Get-NetTCPConnection" in script
    assert "netstat.exe" in script
    assert script.index("netstat.exe -ano -p tcp") < script.index(
        "Get-NetTCPConnection -LocalPort $Port"
    )
    assert '-Filter "Name = \'python.exe\' OR Name = \'pythonw.exe\'"' in script
    assert "deepdesk\\.main" in script
    assert "$portRowsSeen" in script
    assert "wildcard foreign" in script
    assert "for ($sweep = 0; $sweep -lt 4; $sweep++)" in script
    assert "taskkill.exe /PID $ownerId /T /F" in script
    assert "Elren did not terminate it" in script
    assert '$ErrorActionPreference = "Continue"' in script
    assert "Thread.Sleep(1000)" not in launcher
    assert "Thread.Sleep(150)" in launcher
    assert "request.Timeout = 4000" in launcher
    assert "mutex.WaitOne(0, false)" in launcher
    assert "AbandonedMutexException" in launcher
    assert "mutex.ReleaseMutex()" in launcher
    assert "TerminatePreviousElrenLaunchers(root);" not in launcher
    assert "bool activationRequested = WriteActivationRequest(args);" in launcher
    assert "if (activationRequested)" in launcher
    activation_branch = launcher.split("if (activationRequested)", 1)[1].split(
        "try { ownsMutex = mutex.WaitOne(6000, false); }", 1
    )[0]
    assert "showSignal.Set();" in activation_branch
    assert "return;" in activation_branch
    assert "mutex.WaitOne(6000, false)" in launcher
    assert 'Arguments = "/PID " + candidate.Id.ToString' not in launcher
    assert 'PackageChannelName(@"Local\\ElrenLauncher", root)' in launcher
    assert "CoreWebView2BrowsingDataKinds.DiskCache" not in launcher
    assert "Keep WebView2's HTTP cache across warm starts" in launcher
    assert "else if (recovering)" in launcher
    assert "webView.Reload();" in launcher
    assert "BusyButListening" in launcher
    assert "IsServicePortListening" in launcher
    assert "if (failedHealthChecks >= 4)" in launcher
    assert "healthCheckRunning" in launcher
    assert "finally { healthCheckRunning = false; }" in launcher
    assert "if (probe == ServiceProbeResult.Foreign)" in launcher
    assert "ElrenLauncher.TerminatePreviousElrenLaunchers(root);" not in launcher
    assert "Application.Restart();" not in launcher
    assert "RecreateWebViewAsync" in launcher
    assert "NavigationCompleted += WebView_NavigationCompleted" in launcher
    assert "CoreWebView2NavigationCompletedEventArgs" in launcher
    navigation_recovery = launcher.split("private async void WebView_NavigationCompleted", 1)[1].split(
        "private async void WebView_ProcessFailed", 1
    )[0]
    assert "ShowSplash" in navigation_recovery
    assert "BusyButListening" in navigation_recovery
    assert "EnsureApplicationReadyAsync(true)" in navigation_recovery
    assert '"launcher-" + attemptId + ".log"' in launcher
    assert '"launcher-latest.txt"' in launcher
    assert "PruneLauncherLogs(dataDirectory, latestLogPath, 30)" in launcher
    assert 'Directory.GetFiles(dataDirectory, "launcher-*.log", SearchOption.TopDirectoryOnly)' in launcher
    assert "MarkServiceReplacementIntent" not in launcher
    assert "service-current-instance.txt" in launcher
    assert "service-stop-intent-" not in launcher
    assert "Invoke-ElrenService $VenvPython" in script
    assert "--elren-service-id" in script
    assert "intentional launcher handoff" in script
    assert "TerminateSpawnedProcessTree(process)" in launcher
    assert "handoff && IsExpectedServiceReady()" in launcher
    assert "FileShare.ReadWrite | FileShare.Delete" in launcher
    assert launcher.index("if (process.HasExited)") < launcher.index(
        "if (handoff && IsExpectedServiceReady())",
        launcher.index("var timer"),
    )


def test_empty_listening_port_is_a_normal_non_throwing_probe_result() -> None:
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")
    probe = script.split("function Get-ListeningProcessIds", 1)[1].split(
        "function Stop-StaleElrenService", 1
    )[0]
    error_modes = re.findall(
        r"Get-NetTCPConnection\s+-LocalPort\s+\$Port\s+-State\s+Listen\s+"
        r"-ErrorAction\s+(\w+)",
        probe,
    )

    # Get-NetTCPConnection may surface "no matching objects" as a non-terminating
    # CIM error. Cold startup on a free port must convert that to an empty list,
    # not promote it through the script-wide Stop preference.
    assert len(error_modes) == 2
    assert set(error_modes) == {"SilentlyContinue"}
    assert "return @($owners)" in probe


def test_native_desktop_shell_hides_loopback_and_has_compatibility_fallbacks() -> None:
    launcher = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")
    build = (ROOT / "launcher" / "build-launcher.ps1").read_text(encoding="utf-8")
    main_source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")

    assert "WebView2" in launcher
    assert "IsStatusBarEnabled = false" in launcher
    assert "NewWindowRequested" in launcher
    assert "ProcessFailed" in launcher
    assert "WebViewRuntimeAvailable" in launcher
    assert "MicrosoftEdgeWebview2Setup.exe" in launcher
    assert "IsTrustedMicrosoftInstaller" in launcher
    assert 'Arguments = "--app=\\\"" + WithDesktopLanguage(localUrl)' in launcher
    assert "SetProcessDPIAware" in launcher
    assert "SetProcessDpiAwarenessContext(new IntPtr(-4))" in launcher
    assert "MinimumSize = new Size(960, 680)" in launcher
    assert "Local\\ElrenLauncher" in launcher
    assert "Local\\ElrenShow" in launcher
    assert 'PackageChannelName("Elren-activation", AppDomain.CurrentDomain.BaseDirectory)' in launcher
    assert 'argument.StartsWith("--task="' in launcher
    assert "TakeActivationRequest" in launcher
    assert 'localUrl + "api/desktop/identity"' in launcher
    assert "RefinedStartupCard" in launcher
    assert "RefinedProgressBar" in launcher
    assert "e.Graphics.Clear(BackColor)" in launcher
    assert 'Path.Combine(root, "launcher", "elren-app-icon.png")' in launcher
    assert "HighQualityPictureBox" in launcher
    assert "InterpolationMode.HighQualityBicubic" in launcher
    assert "CompositingQuality.HighQuality" in launcher
    assert "PixelOffsetMode.HighQuality" in launcher
    assert "new Icon(path, new Size(64, 64))" in launcher
    assert "Workspace data stays on this device" in launcher
    assert "Private local agent · Version 1.0" in launcher
    assert '@app.get("/api/desktop/identity")' in main_source
    assert '"ELREN_DESKTOP_SHELL"' in main_source
    assert 'os.getenv("MILO_DESKTOP_SHELL", os.getenv("DEEPDESK_DESKTOP_SHELL", ""))' in main_source
    assert 'if desktop_shell == "1"' in main_source
    assert 'f"--task={task_id}"' in main_source
    assert "/platform:x64" in build
    assert "Microsoft.Web.WebView2.Core.dll" in build
    assert "Microsoft.Web.WebView2.WinForms.dll" in build
    assert "WebView2Loader.dll" in build
    assert "elren-app-icon.png" in build


def test_launcher_icon_assets_are_real_images() -> None:
    from PIL import Image, ImageChops, ImageStat

    png_path = ROOT / "launcher" / "elren.png"
    ico_path = ROOT / "launcher" / "elren.ico"
    assert png_path.exists()
    assert ico_path.exists()

    with Image.open(png_path) as image:
        assert image.size == (256, 256)
        assert image.mode == "RGBA"
        assert max(ImageStat.Stat(image.convert("RGB")).var) > 100

    with Image.open(ico_path) as image:
        assert image.format == "ICO"
        assert image.size == (256, 256)
        rgb = image.convert("RGB")
        assert max(ImageStat.Stat(rgb).var) > 100
        # A formerly corrupted ICO decoded as high-frequency random noise.
        # Real logo pixels have strong local continuity instead.
        tiny = rgb.resize((64, 64))
        shifted = ImageChops.offset(tiny, 1, 0)
        difference = ImageStat.Stat(ImageChops.difference(tiny, shifted)).mean
        assert sum(difference) / len(difference) < 40


def test_built_windows_launcher_embeds_the_current_icon(tmp_path: Path) -> None:
    executable = ROOT / "Elren.exe"
    if os.name != "nt" or not executable.is_file():
        return

    from PIL import Image, ImageChops

    icon_path = ROOT / "launcher" / "elren.ico"
    extracted = tmp_path / "embedded-icon.png"
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")

    assert executable.is_file()
    assert powershell is not None
    environment = os.environ.copy()
    environment["ELREN_TEST_EXE"] = str(executable)
    environment["ELREN_TEST_ICON_OUT"] = str(extracted)
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        "$icon=[System.Drawing.Icon]::ExtractAssociatedIcon($env:ELREN_TEST_EXE); "
        "if($null -eq $icon){exit 2}; "
        "$bitmap=$icon.ToBitmap(); "
        "$bitmap.Save($env:ELREN_TEST_ICON_OUT,[System.Drawing.Imaging.ImageFormat]::Png); "
        "$bitmap.Dispose(); $icon.Dispose()"
    )
    subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=20,
    )

    with Image.open(icon_path) as icon, Image.open(extracted) as embedded:
        expected = icon.ico.getimage((32, 32)).convert("RGBA")
        actual = embedded.convert("RGBA")
        assert actual.size == expected.size
        assert ImageChops.difference(actual, expected).getbbox() is None


def test_startup_prepares_dependencies_before_replacing_the_running_service() -> None:
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")

    handoff = script.rindex("Stop-StaleElrenService $ServicePort")
    assert script.index("Python dependencies are unchanged; skipped installation.") < handoff
    assert script.index("OpenClaw/MCP dependencies are unchanged; skipped installation.") < handoff
    assert script.index("Package-local OpenClaw skills ready:") < handoff
    assert handoff < script.index(
        'Write-StartupStage "Starting the local Elren service..."', handoff
    )


def test_openclaw_runtime_is_rebuilt_as_a_portable_hoisted_tree() -> None:
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")
    npmrc = (ROOT / "work" / "openclaw-runtime" / ".npmrc").read_text(encoding="utf-8")
    manifest = (ROOT / "work" / "openclaw-runtime" / "package.json").read_text(
        encoding="utf-8"
    )

    assert "node-linker=hoisted" in npmrc
    assert 'nodeLinker"?\\s*:\\s*"?hoisted' in script
    assert "--config.node-linker=hoisted" in script
    assert "--force" in script
    assert '$virtualStoreMarker = "\\node_modules\\.pnpm\\openclaw@"' in script
    assert '"work\\openclaw-runtime\\node_modules\\openclaw"' in script
    assert '"undici": "8.10.0"' in manifest
    assert '"tar": "7.5.22"' in manifest
    assert '"@hono/node-server": "2.1.0"' in manifest
    assert '"@a2ui/lit": "0.10.1"' in manifest
    assert '"@lit/context": "1.1.6"' in manifest
    assert '"lit": "3.3.3"' in manifest
    assert "--omit=dev --ignore-scripts --no-audit --no-fund" in script


def test_uvicorn_info_logs_do_not_look_like_powershell_native_errors() -> None:
    source = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")

    assert 'log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"' in source
    assert 'log_config["handlers"]["access"]["stream"] = "ext://sys.stdout"' in source
    assert "log_config=log_config" in source
