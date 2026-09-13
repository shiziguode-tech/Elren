from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "launcher" / "ElrenLauncher.cs").read_text(encoding="utf-8")


def method(start: str, end: str) -> str:
    return SOURCE[SOURCE.index(start) : SOURCE.index(end, SOURCE.index(start))]


def test_browser_preparation_runs_beside_service_probe_before_bootstrap() -> None:
    body = method(
        "private async Task EnsureApplicationReadyAsync",
        "private bool StartServiceAndWait",
    )
    assert body.index("Task<ServiceProbeResult> serviceProbe") < body.index("PrepareWebViewAsync()")
    assert body.index("PrepareWebViewAsync()") < body.index("await serviceProbe")
    assert body.index("PrepareWebViewAsync()") < body.index("StartServiceAndWait()")
    assert body.index("StartupStage.ServiceReady") < body.index("InitializeWebViewAsync()")
    assert "if (StartupCancelled) return;" in body


def test_shared_webview_preparation_does_not_navigate_or_mark_browser_ready() -> None:
    body = method("private Task<bool> PrepareWebViewAsync", "private async Task<bool> InitializeWebViewAsync")
    assert "if (webViewPreparationTask == null)" in body
    assert "return webViewPreparationTask;" in body
    assert "CoreWebView2Environment.CreateAsync" in body
    assert "EnsureCoreWebView2Async(environment)" in body
    assert ".Source =" not in body
    assert "TakeActivationRequest()" not in body
    assert "browserReady = true" not in body


def test_late_preparation_checks_disposal_and_control_replacement_after_awaits() -> None:
    body = method("private async Task<bool> PrepareWebViewCoreAsync", "private async Task<bool> InitializeWebViewAsync")
    assert body.count("if (StartupCancelled || preparingView != webView || preparingView.IsDisposed)") == 3
    assert "await Task.Run(() => WebViewRuntimeAvailable()" in body
    assert "(!StartupCancelled && InstallBundledWebViewRuntime())" in body


def test_preparation_wait_allows_exit_before_uninterruptible_com_completion() -> None:
    body = method("private async Task<bool> AwaitWebViewPreparationAsync", "private async Task<bool> PrepareWebViewCoreAsync")
    assert "while (!preparation.IsCompleted && !StartupCancelled)" in body
    assert "await Task.WhenAny(preparation, Task.Delay(100))" in body
    assert "return !StartupCancelled && await preparation;" in body


def test_browser_only_recovery_discards_old_preparation_task() -> None:
    body = method("private async Task<bool> RecreateWebViewAsync", "private bool WebViewRuntimeAvailable")
    assert "if (StartupCancelled) return false;" in body
    assert body.index("webView = CreateWebViewControl()") < body.index("webViewPreparationTask = null")
    assert body.index("webViewPreparationTask = null") < body.index("InitializeWebViewAsync()")


def test_startup_timing_is_separate_append_only_fixed_stage_diagnostics() -> None:
    body = method("private void RecordStartupStage", "private void ShowFatal")
    assert "RecordStartupStage(StartupStage stage)" in body
    assert '"startup-timing-"' in body
    assert "File.AppendAllText(startupTimingPath," in body
    assert "startupTimer.ElapsedMilliseconds" in body
    assert "stage.ToString()" in body
    for forbidden in ("latestLogPath", "ReadAllText", "ReadAllLines", "error.Message", "localUrl", ".env"):
        assert forbidden not in body


def test_navigation_timing_is_recorded_for_success_and_failure() -> None:
    body = method("private async void WebView_NavigationCompleted", "private void WebView_NewWindowRequested")
    assert "if (startupNavigationPending && !StartupCancelled)" in body
    assert "StartupStage.NavigationCompleted : StartupStage.NavigationFailed" in body


def test_same_package_reopen_signals_before_replacement_timeout() -> None:
    body = method("private static void Main", "internal static bool SamePackageLauncherRunning")
    branch = body.split("if (SamePackageLauncherRunning(root))", 1)[1]
    assert branch.index("showSignal.Set();") < branch.index("mutex.WaitOne(6000, false)")
    assert branch.index("return;") < branch.index("mutex.WaitOne(6000, false)")
    probe = method("internal static bool SamePackageLauncherRunning", "internal static void TerminatePreviousElrenLaunchers")
    assert "candidate.Id != currentId" in probe
    assert "Path.GetFullPath(candidate.MainModule.FileName).Equals(executable, StringComparison.OrdinalIgnoreCase)" in probe
    assert "finally { candidate.Dispose(); }" in probe
    assert "Process.Start" not in probe
    assert "Kill(" not in probe


def test_prewarm_crash_cannot_bypass_service_identity_gate() -> None:
    initialize = method("private async Task<bool> InitializeWebViewAsync", "private static WebView2 CreateWebViewControl")
    assert initialize.index("!serviceReadyForNavigation") < initialize.index("PrepareWebViewAsync()")
    assert "if (startupBrowserRetried) return false;" in initialize
    crash = method("private async void WebView_ProcessFailed", "private bool IsLocalTarget")
    assert crash.index("e.ProcessFailedKind != CoreWebView2ProcessFailedKind.BrowserProcessExited") < crash.index("DeferPreNavigationFailure()")
    assert crash.index("e.ProcessFailedKind != CoreWebView2ProcessFailedKind.RenderProcessExited") < crash.index("DeferPreNavigationFailure()")
    assert crash.index("if (DeferPreNavigationFailure()) return;") < crash.index("webView.Reload()")
    assert crash.count("if (StartupCancelled) return;") == 3


def test_recovery_revokes_stale_service_readiness_before_any_await() -> None:
    recovery = method("private async Task EnsureApplicationReadyAsync", "private bool StartServiceAndWait")
    assert recovery.index("serviceReadyForNavigation = false;") < recovery.index("await serviceProbe")
    assert recovery.index("startupBrowserRetried = false;") < recovery.index("await serviceProbe")
    initialize = method("private async Task<bool> InitializeWebViewAsync", "private static WebView2 CreateWebViewControl")
    assert initialize.count("if (!serviceReadyForNavigation || StartupCancelled) return false;") == 2
    crash = method("private async void WebView_ProcessFailed", "private bool IsLocalTarget")
    assert crash.count("if (DeferBrowserRecoveryUntilServiceReady()) return;") == 4
    health = method("private async void HealthTimer_Tick", "private void WebView_NavigationStarting")
    assert health.count("if (starting || closeToken.IsCancellationRequested || exiting) return;") == 3
    navigation = method("private async void WebView_NavigationCompleted", "private void WebView_NewWindowRequested")
    assert navigation.index("if (starting || !serviceReadyForNavigation) return;") < navigation.index("webView.Reload()")


def test_native_browser_locale_and_recovery_route_are_wired_to_real_controls() -> None:
    preparation = method("private async Task<bool> PrepareWebViewCoreAsync", "private async Task<bool> InitializeWebViewAsync")
    assert 'environmentOptions.Language = chineseUi ? "zh-CN" : "en-US";' in preparation
    assert "SourceChanged += WebView_SourceChanged;" in preparation
    recreation = method("private async Task<bool> RecreateWebViewAsync", "private bool WebViewRuntimeAvailable")
    assert recreation.index("previous.Source.AbsoluteUri") < recreation.index("previous.Dispose()")
    assert "SourceChanged -= WebView_SourceChanged;" in recreation


def test_closed_port_probe_skips_slow_http_without_trusting_open_ports() -> None:
    probe = method("private ServiceProbeResult ProbeExpectedService", "private bool IsExpectedServiceReady")
    assert probe.index("if (!IsServicePortListening()) return ServiceProbeResult.Missing;") < probe.index("WebRequest.Create")
    assert "request.Timeout = 4000" in probe
    assert 'localUrl + "api/desktop/identity"' in probe
    assert "MatchesServiceIdentity(" in probe
    assert "body.IndexOf(rootIdentity" not in probe
    assert "? ServiceProbeResult.Ready" in probe
    assert ": ServiceProbeResult.Foreign" in probe
    tcp = method("private bool IsServicePortListening", "private ServiceProbeResult ProbeExpectedService")
    assert "pending.AsyncWaitHandle.WaitOne(900)" in tcp
    assert "client.EndConnect(pending);" in tcp


def test_local_listener_snapshot_is_only_a_closed_port_shortcut() -> None:
    probe = method("private ServiceProbeResult ProbeExpectedService", "private bool IsExpectedServiceReady")
    assert "IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners()" in probe
    assert probe.index("LoopbackListenerPresent(localUrl, query) == false") < probe.index("if (!IsServicePortListening())")
    assert "!IPAddress.IsLoopback(address)" in probe
    assert "if (listeners == null) return null;" in probe
    assert "catch { return null; }" in probe
    table = method("internal static bool? LoopbackListenerPresent", "private ServiceProbeResult ProbeExpectedServiceWithListenerQuery")
    assert "ServiceProbeResult.Ready" not in table
    assert "Process.Start" not in table and "Kill(" not in table
