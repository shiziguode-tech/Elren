using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class TrayLifecycleTests
{
    static void Check(bool value, string label) { if (!value) throw new Exception(label); }
    static object Field(object form, string name) { return form.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).GetValue(form); }
    static object Call(object form, string name, params object[] args) { return form.GetType().GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(form, args); }

    static void CheckProcessOwnership(string python, string root, string argumentRoot, string expected)
    {
        // Own synthetic process only. It never imports the app, reads config,
        // creates a listener, or calls an API. Never enumerate and kill PIDs.
        using (var child = Process.Start(new ProcessStartInfo {
            FileName = python,
            Arguments = "-c \"import time; time.sleep(15)\" --workspace=\"" + argumentRoot + "\"",
            UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
        }))
        {
            try
            {
                Thread.Sleep(150);
                var state = typeof(DesktopShellForm).GetMethod("ProbePackageProcesses", BindingFlags.NonPublic | BindingFlags.Static)
                    .Invoke(null, new object[] { root, String.Empty });
                Check(state.ToString() == expected, "real process query directory boundary: " + expected + " / " + state);
            }
            finally
            {
                if (!child.HasExited) { child.Kill(); child.WaitForExit(5000); }
            }
        }
    }

    static Task SyntheticIdentityServer(TcpListener listener, string body, int delayStart)
    {
        return Task.Run(() => {
            if (delayStart > 0) { Thread.Sleep(delayStart); listener.Start(); }
            for (;;) using (var client = listener.AcceptTcpClient())
            using (var stream = client.GetStream())
            {
                stream.ReadTimeout = 3000;
                byte[] request = new byte[4096];
                // The TCP preflight makes a connection without an HTTP request.
                if (stream.Read(request, 0, request.Length) == 0) continue;
                byte[] response = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: " + body.Length + "\r\nConnection: close\r\n\r\n" + body);
                stream.Write(response, 0, response.Length);
                return;
            }
        });
    }

    static void CheckServiceProbes(string root)
    {
        var allocation = new TcpListener(IPAddress.Loopback, 0);
        allocation.Start();
        int port = ((IPEndPoint)allocation.LocalEndpoint).Port;
        allocation.Stop();
        var listener = new TcpListener(IPAddress.Loopback, port);
        Task server = null;
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        using (var form = new DesktopShellForm(root, "http://127.0.0.1:" + port + "/", signal))
        {
            try
            {
                var timer = Stopwatch.StartNew();
                Check(Call(form, "ProbeExpectedService").ToString() == "Missing", "closed ephemeral port reports missing");
                Check(timer.Elapsed < TimeSpan.FromMilliseconds(2400), "closed port skips the slow HTTP retry path");
                string identity = "{\"app\":\"Elren\",\"root_hash\":\"" + (string)Field(form, "rootIdentity") + "\"}";
                string url = "http://127.0.0.1:" + port + "/";
                Func<IPEndPoint[]> unsupportedQuery = () => { throw new NotSupportedException("isolated table unavailable"); };
                Check(DesktopShellForm.LoopbackListenerPresent(url, unsupportedQuery) == null, "unsupported listener table is unknown, not closed");
                Check(DesktopShellForm.LoopbackListenerPresent(url, () => null) == null, "null listener table is unknown");
                Check(DesktopShellForm.LoopbackListenerPresent("http://[::ffff:127.0.0.1]:" + port + "/", () => new IPEndPoint[0]) == null, "mapped IPv6 representation falls back to TCP");
                Check(DesktopShellForm.LoopbackListenerPresent("http://192.0.2.1:1/", () => { throw new Exception("non-loopback should not query local table"); }) == null, "non-loopback retains ordinary connection probing");
                Check(DesktopShellForm.LoopbackListenerPresent(url, () => new [] { new IPEndPoint(IPAddress.Any, port) }) == true, "IPv4 wildcard is a possible loopback listener");
                Check(DesktopShellForm.LoopbackListenerPresent(url, () => new [] { new IPEndPoint(IPAddress.IPv6Any, port) }) == true, "dual-stack wildcard never gives a false closed result");
                Check(DesktopShellForm.LoopbackListenerPresent("http://[::1]:" + port + "/", () => new [] { new IPEndPoint(IPAddress.IPv6Any, port) }) == true, "IPv6 wildcard is recognized");
                Check(DesktopShellForm.LoopbackListenerPresent("http://[::1]:" + port + "/", () => new [] { new IPEndPoint(IPAddress.IPv6Loopback, port) }) == true, "IPv6 loopback is recognized");
                Check(DesktopShellForm.LoopbackListenerPresent(url, () => new [] { new IPEndPoint(IPAddress.Parse("127.0.0.2"), port) }) == false, "different local bind address is not the target listener");
                Check(Call(form, "ProbeExpectedServiceWithListenerQuery", new Func<IPEndPoint[]>(() => new [] { new IPEndPoint(IPAddress.Loopback, port) })).ToString() == "Missing", "a stale positive listener snapshot must still pass TCP");
                listener.Start();
                Check(Call(form, "ProbeExpectedServiceWithListenerQuery", new Func<IPEndPoint[]>(() => new IPEndPoint[0])).ToString() == "Missing", "a stale negative snapshot neither claims readiness nor mutates the new listener");
                server = SyntheticIdentityServer(listener, identity, 0);
                Check(Call(form, "ProbeExpectedService").ToString() == "Ready", "ready requires a matching HTTP package identity");
                Check(server.Wait(5000), "synthetic matching identity replied");
                server = SyntheticIdentityServer(listener, identity, 0);
                Check(Call(form, "ProbeExpectedServiceWithListenerQuery", unsupportedQuery).ToString() == "Ready", "unsupported listener query falls back to TCP plus HTTP identity");
                Check(server.Wait(5000), "fallback identity replied");
                server = SyntheticIdentityServer(listener, "unrelated-package", 0);
                Check(Call(form, "ProbeExpectedService").ToString() == "Foreign", "an occupied TCP port with another identity never becomes ready");
                Check(server.Wait(5000), "synthetic foreign identity replied");
                // Listening without responding simulates a busy event loop,
                // not a dead service. It must retain the four-second HTTP grace.
                timer.Restart();
                Check(Call(form, "ProbeExpectedService").ToString() == "BusyButListening", "slow occupied listener is not called missing or ready");
                Check(timer.ElapsedMilliseconds >= 3500, "occupied service retains its HTTP grace period");
                listener.Stop();
                listener = new TcpListener(IPAddress.Loopback, port);
                server = SyntheticIdentityServer(listener, identity, 250);
                timer.Restart();
                bool ready = false;
                while (timer.ElapsedMilliseconds < 2400)
                {
                    if (Call(form, "ProbeExpectedService").ToString() == "Ready") { ready = true; break; }
                    Thread.Sleep(150);
                }
                Check(ready && timer.ElapsedMilliseconds < 2400, "a newly ready service is found without another HTTP retry penalty");
                Check(server.Wait(5000), "delayed synthetic identity replied");
            }
            finally
            {
                listener.Stop();
                if (server != null && !server.IsCompleted) { try { server.Wait(4000); } catch { } }
                ((NotifyIcon)Field(form, "trayIcon")).Dispose();
                ((ContextMenuStrip)Field(form, "trayMenu")).Dispose();
            }
        }
    }

    [STAThread]
    static int Main(string[] args)
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        try { return Run(args); }
        catch (Exception error) { File.WriteAllText(Path.Combine(args[0], "failure.txt"), error.ToString()); Console.WriteLine(error.ToString()); return 1; }
    }

    static int Run(string[] args)
    {
        string root = args[0];
        string data = Path.Combine(root, "data");
        Directory.CreateDirectory(data);
        string current = Path.Combine(data, "service-current-instance.txt");
        Check(DesktopShellForm.StopPackageService(root), "no backend exits normally");
        File.WriteAllText(current, "../../other");
        Check(!DesktopShellForm.StopPackageService(root), "invalid instance rejected");
        string instance = Guid.NewGuid().ToString("N");
        File.WriteAllText(current, instance);
        var backend = new Thread(delegate() {
            string request = Path.Combine(data, "service-exit-request-" + instance);
            for (int i = 0; i < 100 && !File.Exists(request); i++) Thread.Sleep(20);
            Check(File.Exists(request), "exact instance received request");
            File.WriteAllText(Path.Combine(data, "service-exit-complete-" + instance), "stopped");
        });
        backend.Start();
        Check(DesktopShellForm.StopPackageService(root), "acknowledged backend exit");
        backend.Join();
        File.Delete(current);
        Check(Directory.GetFiles(data, "service-exit-*").Length == 0, "handshake cleaned up");

        string staleInstance = Guid.NewGuid().ToString("N");
        File.WriteAllText(current, staleInstance);
        var staleTimer = Stopwatch.StartNew();
        Check(DesktopShellForm.StopPackageService(root), "dead isolated instance can exit without an acknowledgment");
        Check(staleTimer.Elapsed < TimeSpan.FromSeconds(10), "stale marker does not consume the 40 second shutdown timeout");
        Check(File.ReadAllText(current) == staleInstance, "stale evidence is preserved rather than deleted to imply success");
        File.Delete(Path.Combine(data, "service-exit-request-" + staleInstance));
        string failedResult = Path.Combine(data, "service-exit-failed-" + staleInstance);
        string staleSuccess = Path.Combine(data, "service-exit-complete-" + staleInstance);
        File.WriteAllText(failedResult, "shutdown_failed");
        File.WriteAllText(staleSuccess, "stopped");
        Check(DesktopShellForm.HasRecordedShutdownFailure(root), "failure message detects the current instance failure");
        Check(!DesktopShellForm.StopPackageService(root), "failure wins over stale acknowledgment and process absence");
        Check(File.Exists(current) && File.Exists(failedResult), "failed shutdown evidence remains available");
        File.Delete(failedResult);
        File.Delete(staleSuccess);
        File.Delete(current);
        Check(!DesktopShellForm.HasRecordedShutdownFailure(root), "missing marker does not invent a recorded failure");
        Check(DesktopShellForm.CommandReferencesPackageRoot("python.exe --workspace \"" + root + "\\outputs\"", root), "child directory command belongs to exact package");
        Check(DesktopShellForm.CommandReferencesPackageRoot("python.exe --workspace=\"" + root.Replace('\\', '/') + "\"", root), "forward slash exact root is recognized");
        Check(!DesktopShellForm.CommandReferencesPackageRoot("python.exe --workspace \"" + root + "-other\\outputs\"", root), "similar sibling name is not package ownership");
        Check(!DesktopShellForm.CommandReferencesPackageRoot("python.exe --workspace \"" + root + "2\\outputs\"", root), "longer path prefix is not package ownership");
        CheckProcessOwnership(args[1], root, root + "-external", "Absent");
        CheckProcessOwnership(args[1], root, root, "Present");

        Application.EnableVisualStyles();
        CheckServiceProbes(root);
        string languagePath = Path.Combine(data, "desktop-ui-language.txt");
        File.WriteAllText(languagePath, "en");
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        using (var form = new DesktopShellForm(root, "http://127.0.0.1:1/", signal))
        {
            // Do not show the form: Shown would launch a real backend.
            IntPtr handle = form.Handle;
            var tray = (NotifyIcon)Field(form, "trayIcon");
            var menu = (ContextMenuStrip)Field(form, "trayMenu");
            Check(menu.Items[0].Text == "Open Elren" && menu.Items[2].Text == "Quit Elren completely", "persisted English overrides Windows language before browser starts");
            Check(form.ShutdownFailureMessage(true).Contains("Backend cleanup failed") && form.ShutdownFailureMessage(true).Contains("local startup logs"), "recorded failure explains logs instead of promising retry");
            Check(!form.ShutdownFailureMessage(true).Contains("Please retry Quit shortly"), "recorded failure is distinct from a pending shutdown");
            Check(form.ShutdownFailureMessage(false).Contains("Please retry Quit shortly"), "ordinary pending shutdown remains retryable");
            Check(form.WithDesktopLanguage("http://127.0.0.1:1/") == "http://127.0.0.1:1/?lang=en", "initial page receives English even without web storage");
            Check(form.WithDesktopLanguage("http://127.0.0.1:1/?task=test#anchor") == "http://127.0.0.1:1/?task=test&lang=en#anchor", "language restoration preserves task and fragment");
            Check(form.WithDesktopLanguage("http://127.0.0.1:1/?lang=zh") == "http://127.0.0.1:1/?lang=zh", "explicit URL language takes precedence");
            string chatRoute = "http://127.0.0.1:1/?task=isolated-recovery-check&lang=en#latest";
            Check(form.SelectApplicationNavigation(null, chatRoute) == chatRoute, "browser recreation preserves trusted current chat and fragment");
            Check(form.SelectApplicationNavigation("/?task=activation", chatRoute) == "http://127.0.0.1:1/?task=activation", "new explicit activation takes precedence over restored chat");
            foreach (string unsafeRoute in new [] { "https://example.com/", "http://127.0.0.1:2/?task=wrong-port", "file:///C:/preview.html", "http://127.0.0.1:1/outputs/preview.html", "http://user@127.0.0.1:1/" })
                Check(form.SelectApplicationNavigation(null, unsafeRoute) == "http://127.0.0.1:1/", "untrusted routes are never restored: " + unsafeRoute);
            Check(form.SelectApplicationNavigation("/outputs/preview.html", chatRoute) == chatRoute, "invalid activation cannot displace trusted current chat");
            Check((bool)Call(form, "DeferPreNavigationFailure"), "prewarm crash deferred until startup owns recovery");
            Check((bool)Field(form, "webViewPreparationInvalid"), "prewarm crash invalidates cached preparation");
            var earlyNavigation = (Task<bool>)Call(form, "InitializeWebViewAsync");
            Check(earlyNavigation.IsCompleted && !earlyNavigation.Result, "recovery cannot navigate before service identity ready");
            form.GetType().GetField("starting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            form.GetType().GetField("serviceReadyForNavigation", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            Check((bool)Call(form, "DeferBrowserRecoveryUntilServiceReady"), "old ready flag cannot authorize browser recovery during backend restart");
            Check(!(bool)Field(form, "browserReady") && (bool)Field(form, "webViewPreparationInvalid"), "backend recovery owns rebuilding an invalidated browser");
            form.GetType().GetField("starting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            Check(!(bool)Call(form, "DeferBrowserRecoveryUntilServiceReady"), "verified stable backend allows renderer-only recovery");
            form.GetType().GetField("serviceReadyForNavigation", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            Check((bool)Call(form, "DeferBrowserRecoveryUntilServiceReady"), "unverified backend defers renderer-only recovery");
            form.GetType().GetField("webViewPreparationInvalid", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            var preparation = new TaskCompletionSource<bool>();
            form.GetType().GetField("webViewPreparationTask", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, preparation.Task);
            Check(Object.ReferenceEquals(Call(form, "PrepareWebViewAsync"), preparation.Task), "preparation task reused");
            Check(Object.ReferenceEquals(Call(form, "PrepareWebViewAsync"), preparation.Task), "repeated preparation does not create another browser");
            var waiting = (Task<bool>)Call(form, "AwaitWebViewPreparationAsync", preparation.Task);
            Check(!waiting.IsCompleted, "wait remains pending during COM preparation");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            for (int i = 0; i < 100 && !waiting.IsCompleted; i++) { Application.DoEvents(); Thread.Sleep(20); }
            Check(waiting.IsCompleted && !waiting.Result, "exit does not wait for stalled COM preparation");
            preparation.SetResult(true);
            Application.DoEvents();
            Check(!(bool)Field(form, "browserReady"), "late completion never marks browser ready");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            var failedPreparation = (Task<bool>)Call(form, "AwaitWebViewPreparationAsync", Task.FromResult(false));
            Check(failedPreparation.IsCompleted && !failedPreparation.Result, "failed preparation is propagated");
            form.GetType().GetField("webViewPreparationTask", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, null);
            foreach (string source in new [] {
                "https://example.com/", "http://127.0.0.1:2/", "http://localhost:1/",
                "http://127.0.0.1:1/outputs/preview.html", "file:///C:/preview.html",
                "http://user@127.0.0.1:1/", "not-a-url"
            })
                Check(!form.AcceptDesktopLanguageMessage(source, "elren:ui-language:zh"), "reject untrusted language source: " + source);
            foreach (string message in new [] { "en", "elren:ui-language:fr", "elren:ui-language:EN", "elren:ui-language:zh\n", "elren:quit", "" })
                Check(!form.AcceptDesktopLanguageMessage("http://127.0.0.1:1/", message), "reject unknown message");
            Check(menu.Items[0].Text == "Open Elren", "rejected messages do not mutate menu");
            Check(form.AcceptDesktopLanguageMessage("http://127.0.0.1:1/?lang=zh&task=test", "elren:ui-language:zh"), "accept local UI language");
            Check(menu.Items[0].Text == "打开 Elren" && menu.Items[2].Text == "彻底退出", "live Chinese update");
            Check(form.ShutdownFailureMessage(true).Contains("后台清理失败") && form.ShutdownFailureMessage(true).Contains("本地启动日志"), "recorded cleanup failure follows Chinese UI language");
            Check(File.ReadAllText(languagePath) == "zh", "only language token persisted");
            Check(form.AcceptDesktopLanguageMessage("http://127.0.0.1:1/?lang=en", "elren:ui-language:en"), "accept English switch");
            Check(menu.Items[0].Text == "Open Elren" && menu.Items[2].Text == "Quit Elren completely", "live English update");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            form.AcceptDesktopLanguageMessage("http://127.0.0.1:1/", "elren:ui-language:en");
            Check(menu.Items[2].Text == "Quitting…", "pending exit stays translated without resetting state");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            form.AcceptDesktopLanguageMessage("http://127.0.0.1:1/", "elren:ui-language:en");
            File.WriteAllText(languagePath, "zh");
            Call(menu, "OnOpening", new System.ComponentModel.CancelEventArgs());
            Check(menu.Items[0].Text == "打开 Elren", "Edge fallback language refreshed on tray opening");
            File.WriteAllText(languagePath, "en");
            Call(menu, "OnOpening", new System.ComponentModel.CancelEventArgs());
            Check(menu.Items[0].Text == "Open Elren" && menu.Items[2].Text == "Quit Elren completely", "Edge fallback restores English labels");
            Check(tray.Visible && tray.Icon != null, "native tray icon registered");
            Check(menu.Items.Count == 3, "open/separator/quit menu");
            Check(!menu.ShowImageMargin && !menu.ShowCheckMargin, "no mismatched icon gutter");
            Check(!menu.DropShadowEnabled, "no rectangular native shadow behind rounded menu");
            ((ElrenTrayMenu)menu).PrepareLayout();
            Check(menu.Items[0].Height >= 36, "comfortable menu row height");
            Check(menu.Items[0].Height == menu.Items[2].Height, "consistent action heights");
            foreach (ToolStripItem item in menu.Items)
                if (!(item is ToolStripSeparator))
                    Check(item.Bounds.Left == 0 && item.Bounds.Right == menu.Width, "each action covers the full popup width: " + item.Bounds + " / " + menu.Size);
            Check(menu.Items[0].Bounds.Top == 0, "no unnecessary scroll buttons or top gap: " + menu.Items[0].Bounds + " / " + menu.Size + " display=" + menu.DisplayRectangle);
            using (var bitmap = new Bitmap(menu.Width, menu.Height))
            {
                menu.DrawToBitmap(bitmap, new Rectangle(Point.Empty, menu.Size));
                bitmap.Save(Path.Combine(root, "tray-normal.png"), ImageFormat.Png);
                if (!SystemInformation.HighContrast)
                    Check(bitmap.GetPixel(menu.Width / 2, 3).ToArgb() == ElrenTrayMenu.Surface.ToArgb(), "uniform white menu surface");
            }
            if (!SystemInformation.HighContrast)
            {
                Check(menu.Region != null && !menu.Region.IsVisible(0, 0), "rounded outer corners");
                Check(menu.Region.IsVisible(menu.Width / 2, 1), "rounded region preserves menu content");
            }
            menu.Items[0].Select();
            using (var bitmap = new Bitmap(menu.Width, menu.Height))
            {
                menu.DrawToBitmap(bitmap, new Rectangle(Point.Empty, menu.Size));
                bitmap.Save(Path.Combine(root, "tray-selected.png"), ImageFormat.Png);
                if (!SystemInformation.HighContrast)
                {
                    int mid = menu.Items[0].Bounds.Top + menu.Items[0].Height / 2;
                    Check(bitmap.GetPixel(2, mid).ToArgb() == ElrenTrayMenu.Hover.ToArgb(), "selection reaches left edge");
                    Check(bitmap.GetPixel(menu.Width - 3, mid).ToArgb() == ElrenTrayMenu.Hover.ToArgb(), "selection reaches right edge");
                    int divider = menu.Items[1].Bounds.Top + menu.Items[1].Height / 2;
                    Check(bitmap.GetPixel(2, divider).ToArgb() == ElrenTrayMenu.Edge.ToArgb(), "divider reaches left edge");
                    Check(bitmap.GetPixel(menu.Width - 3, divider).ToArgb() == ElrenTrayMenu.Edge.ToArgb(), "divider reaches right edge");
                }
            }
            menu.Items[2].Select();
            using (var bitmap = new Bitmap(menu.Width, menu.Height))
            {
                menu.DrawToBitmap(bitmap, new Rectangle(Point.Empty, menu.Size));
                bitmap.Save(Path.Combine(root, "tray-exit-selected.png"), ImageFormat.Png);
                if (!SystemInformation.HighContrast)
                    Check(bitmap.GetPixel(menu.Width - 3, menu.Items[2].Bounds.Top + menu.Items[2].Height / 2).ToArgb() == ElrenTrayMenu.Hover.ToArgb(), "exit selection reaches right edge");
            }
            var originalFont = menu.Font;
            string originalOpen = menu.Items[0].Text;
            string originalExit = menu.Items[2].Text;
            foreach (float zoom in new[] { 1F, 1.25F, 1.5F, 2F })
            using (var largeFont = new Font(originalFont.FontFamily, 10F * zoom))
            {
                menu.Font = largeFont;
                menu.Items[0].Text = "Open Elren";
                menu.Items[2].Text = "Quit Elren completely";
                for (int repeat = 0; repeat < 3; repeat++) ((ElrenTrayMenu)menu).PrepareLayout();
                Check(menu.Items[0].Bounds.Top == 0, "repeated layout does not introduce scroll arrows");
                foreach (ToolStripItem item in menu.Items)
                    if (!(item is ToolStripSeparator))
                    {
                        Check(item.Bounds.Right == menu.Width, "English / enlarged font has no white gutter");
                        Check(item.Height >= largeFont.Height + 16, "enlarged text fits row");
                        Check(item.Width >= TextRenderer.MeasureText(item.Text, largeFont).Width + 32, "English text not clipped");
                    }
                menu.Items[2].Select();
                using (var bitmap = new Bitmap(menu.Width, menu.Height))
                {
                    menu.DrawToBitmap(bitmap, new Rectangle(Point.Empty, menu.Size));
                    bitmap.Save(Path.Combine(root, "tray-font-" + (int)(zoom * 100) + ".png"), ImageFormat.Png);
                    if (!SystemInformation.HighContrast)
                        Check(bitmap.GetPixel(menu.Width - 3, menu.Items[2].Bounds.Top + menu.Items[2].Height / 2).ToArgb() == ElrenTrayMenu.Hover.ToArgb(), "enlarged selection reaches right edge");
                }
                menu.Font = originalFont;
            }
            menu.Items[0].Text = originalOpen;
            menu.Items[2].Text = originalExit;
            ((ElrenTrayMenu)menu).PrepareLayout();
            menu.Items[2].Enabled = false;
            using (var bitmap = new Bitmap(menu.Width, menu.Height))
            {
                menu.DrawToBitmap(bitmap, new Rectangle(Point.Empty, menu.Size));
                bitmap.Save(Path.Combine(root, "tray-disabled.png"), ImageFormat.Png);
                if (!SystemInformation.HighContrast)
                    Check(bitmap.GetPixel(menu.Width - 3, menu.Items[2].Bounds.Top + menu.Items[2].Height / 2).ToArgb() == ElrenTrayMenu.Surface.ToArgb(), "disabled item does not retain hover fill");
            }
            menu.Items[2].Enabled = true;
            menu.Items[0].Select();
            Call(menu, "ProcessDialogKey", Keys.Down);
            Check(menu.Items[2].Selected, "keyboard Down skips divider and selects exit");
            Call(menu, "ProcessDialogKey", Keys.Up);
            Check(menu.Items[0].Selected, "keyboard Up returns to open");
            // Suppress the one-time balloon during automated QA.
            form.GetType().GetField("trayHintShown", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            var closing = new FormClosingEventArgs(CloseReason.UserClosing, false);
            Call(form, "DesktopShellForm_FormClosing", form, closing);
            Check(closing.Cancel && !form.ShowInTaskbar && tray.Visible, "window X hides but retains tray");
            var logoff = new FormClosingEventArgs(CloseReason.WindowsShutDown, false);
            Call(form, "DesktopShellForm_FormClosing", form, logoff);
            Check(!logoff.Cancel, "Windows shutdown is not blocked");
            File.WriteAllText(Path.Combine(root, "start.ps1"),
                "[IO.File]::WriteAllText('" + current.Replace("'", "''") + "',$env:ELREN_SERVICE_INSTANCE_ID); Start-Sleep -Seconds 60");
            Exception startupError = null;
            var startup = new Thread(delegate() {
                try { Call(form, "StartServiceAndWait"); }
                catch (Exception error) { startupError = error; }
            });
            startup.Start();
            for (int i = 0; i < 500 && !File.Exists(current); i++) { Application.DoEvents(); Thread.Sleep(20); }
            Check(File.Exists(current), "isolated bootstrap started");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, true);
            for (int i = 0; i < 500 && startup.IsAlive; i++) { Application.DoEvents(); Thread.Sleep(20); }
            Check(!startup.IsAlive && startupError == null, "startup cancellation finishes");
            Check(!File.Exists(current), "cancelled bootstrap clears its new instance marker");
            form.GetType().GetField("exiting", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, false);
            // Exercise the actual menu event and asynchronous exit continuation.
            menu.Items[2].PerformClick();
            menu.Items[2].PerformClick();
            // Real process ownership probing has its own bounded deadline;
            // a loaded CI host may need more than three seconds to answer CIM.
            for (int i = 0; i < 500 && !form.IsDisposed; i++) { Application.DoEvents(); Thread.Sleep(20); }
            Check(form.IsDisposed && !tray.Visible, "quit disposes window and native tray icon");
        }
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        using (var restarted = new DesktopShellForm(root, "http://127.0.0.1:1/", signal))
        {
            var menu = (ContextMenuStrip)Field(restarted, "trayMenu");
            Check(menu.Items[0].Text == "Open Elren", "English survives a fresh shell instance");
            ((NotifyIcon)Field(restarted, "trayIcon")).Dispose();
            menu.Dispose();
        }
        File.WriteAllText(languagePath, new string('x', 100));
        Check(DesktopShellForm.ReadDesktopLanguage(root) == null, "oversize language file ignored");
        File.WriteAllText(languagePath, "fr");
        Check(DesktopShellForm.ReadDesktopLanguage(root) == null, "unsupported persisted language ignored");
        Console.WriteLine("TRAY_LIFECYCLE_OK");
        return 0;
    }
}
