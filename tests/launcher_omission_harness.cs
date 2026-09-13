using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class LauncherOmissionTests
{
    static void Check(bool value, string label) { if (!value) throw new Exception(label); Console.WriteLine("PASS " + label); }
    static object Field(object item, string name) { return item.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).GetValue(item); }
    static void Set(object item, string name, object value) { item.GetType().GetField(name, BindingFlags.NonPublic | BindingFlags.Instance).SetValue(item, value); }
    static object Call(object item, string name, params object[] args) { return item.GetType().GetMethod(name, BindingFlags.NonPublic | BindingFlags.Instance).Invoke(item, args); }
    static bool Pump(Func<bool> done, int milliseconds)
    {
        var timer = Stopwatch.StartNew();
        while (!done() && timer.ElapsedMilliseconds < milliseconds) { Application.DoEvents(); Thread.Sleep(10); }
        return done();
    }
    static DesktopShellForm Shell(string root, int port, EventWaitHandle signal)
    {
        Directory.CreateDirectory(Path.Combine(root, "data"));
        var form = new DesktopShellForm(root, "http://127.0.0.1:" + port + "/", signal);
        IntPtr handle = form.Handle; // Not Shown; no Launcher.Main or real startup.
        Set(form, "webViewPreparationTask", Task.FromResult(true));
        return form;
    }
    static void DisposeShell(DesktopShellForm form)
    {
        ((CancellationTokenSource)Field(form, "closeToken")).Cancel();
        ((NotifyIcon)Field(form, "trayIcon")).Dispose();
        ((ContextMenuStrip)Field(form, "trayMenu")).Dispose();
        form.Dispose();
    }
    static Task Reply(TcpListener listener, string body, bool slow, ManualResetEvent release, ManualResetEvent began)
    {
        return Task.Run(() => {
            try
            {
                for (;;) using (var client = listener.AcceptTcpClient())
                using (var stream = client.GetStream())
                {
                    stream.ReadTimeout = 2000;
                    byte[] request = new byte[4096];
                    if (stream.Read(request, 0, request.Length) == 0) continue;
                    string header = "HTTP/1.1 200 OK\r\nConnection: close\r\n"
                        + (slow ? "Transfer-Encoding: chunked" : "Content-Length: " + Encoding.UTF8.GetByteCount(body)) + "\r\n\r\n";
                    byte[] bytes = Encoding.UTF8.GetBytes(header + (slow ? "" : body));
                    stream.Write(bytes, 0, bytes.Length);
                    if (began != null) began.Set();
                    if (slow) while (!release.WaitOne(100))
                    {
                        bytes = Encoding.ASCII.GetBytes("1\r\n \r\n");
                        stream.Write(bytes, 0, bytes.Length);
                        stream.Flush();
                    }
                    return;
                }
            }
            catch (IOException) { } // Expected when this test's client aborts.
            catch (SocketException) { }
            catch (ObjectDisposedException) { }
        });
    }
    static void Identity(string baseRoot)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        {
            var form = Shell(Path.Combine(baseRoot, "identity"), ((IPEndPoint)listener.LocalEndpoint).Port, signal);
            try
            {
                string hash = (string)Field(form, "rootIdentity");
                string valid = "{\"app\":\"Elren\",\"version\":\"1.0.0\",\"root_hash\":\"" + hash + "\"}";
                Check(DesktopShellForm.MatchesServiceIdentity(valid, hash), "actual JSON contract accepted");
                string[] invalid = {
                    "Not JSON " + hash,
                    "{\"app\":\"OtherProduct\",\"root_hash\":\"wrong\",\"diagnostic\":\"" + hash + "\"}",
                    "{\"app\":\"OtherProduct\",\"root_hash\":\"" + hash + "\"}",
                    "{\"app\":\"Elren\",\"root_hash\":\"wrong\"}",
                    "{\"root_hash\":\"" + hash + "\"}",
                    "{\"app\":\"Elren\",\"root_hash\":[\"" + hash + "\"]}",
                    "{app:'Elren',root_hash:'" + hash + "'}",
                    "{\"app\":\"OtherProduct\",\"app\":\"Elren\",\"root_hash\":\"" + hash + "\"}",
                    valid + " trailing non-json",
                    "[" + valid + "]",
                    new string(' ', 5000) + valid,
                };
                for (int index = 0; index < invalid.Length; index++)
                    Check(!DesktopShellForm.MatchesServiceIdentity(invalid[index], hash), "invalid identity rejected " + index);
                Task server = Reply(listener, valid, false, null, null);
                Check(Call(form, "ProbeExpectedService").ToString() == "Ready", "real HTTP identity ready");
                Check(server.Wait(3000), "owned valid HTTP server finished");
                foreach (string body in new [] { invalid[0], invalid[1], invalid[10] })
                {
                    server = Reply(listener, body, false, null, null);
                    Check(Call(form, "ProbeExpectedService").ToString() == "Foreign", "real HTTP rejects false or oversized identity");
                    Check(server.Wait(3000), "owned false HTTP server finished");
                }
            }
            finally { listener.Stop(); DisposeShell(form); }
        }
    }
    static void Busy(string baseRoot)
    {
        string root = Path.Combine(baseRoot, "busy");
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start(32);
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        {
            var form = Shell(root, ((IPEndPoint)listener.LocalEndpoint).Port, signal);
            string entered = Path.Combine(root, "bootstrap-entered.txt");
            File.WriteAllText(Path.Combine(root, "start.ps1"), "[IO.File]::WriteAllText('" + entered.Replace("'", "''") + "','not permitted')");
            Task startup = (Task)Call(form, "EnsureApplicationReadyAsync", false);
            try
            {
                Pump(() => File.Exists(entered) || startup.IsCompleted, 5200);
                Check(!File.Exists(entered), "busy initial startup never invokes bootstrap");
                Check(!startup.IsCompleted, "busy startup remains a verified wait");
                Set(form, "exiting", true);
                Check(Pump(() => startup.IsCompleted, 1500), "busy startup exit cancels identity request");
            }
            finally { Set(form, "exiting", true); listener.Stop(); Pump(() => startup.IsCompleted, 6000); DisposeShell(form); }
        }
    }
    static void Marker(string baseRoot, bool replace)
    {
        string root = Path.Combine(baseRoot, replace ? "replacement-marker" : "own-marker");
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        {
            var form = Shell(root, 1, signal);
            string current = Path.Combine(root, "data", "service-current-instance.txt");
            string previous = new string('a', 32), replacement = new string('c', 32);
            File.WriteAllText(current, previous);
            File.WriteAllText(Path.Combine(root, "start.ps1"), "[IO.File]::WriteAllText('" + current.Replace("'", "''") + "',$env:ELREN_SERVICE_INSTANCE_ID); Start-Sleep -Seconds 45");
            Task startup = replace ? Task.Run(() => Call(form, "StartServiceAndWait"))
                : (Task)Call(form, "EnsureApplicationReadyAsync", false);
            try
            {
                bool published = Pump(() => File.ReadAllText(current).Trim() != previous || startup.IsFaulted, 6000);
                if (startup.IsFaulted) throw startup.Exception;
                Check(published, "owned dummy publishes assigned marker");
                string owned = File.ReadAllText(current).Trim();
                Guid parsed;
                Check(Guid.TryParseExact(owned, "N", out parsed), "bootstrap receives valid preassigned instance");
                if (replace) File.WriteAllText(current, replacement);
                Set(form, "exiting", true);
                Check(Pump(() => startup.IsCompleted, 10000), "only owned dummy process tree cancelled");
                Check(!startup.IsFaulted, "owned cancellation has no failure");
                Check(replace ? File.Exists(current) && File.ReadAllText(current) == replacement : !File.Exists(current),
                    replace ? "replacement marker preserved" : "owned marker cleared");
            }
            finally { Set(form, "exiting", true); Pump(() => startup.IsCompleted, 10000); DisposeShell(form); }
        }
    }
    static void SlowBody(string baseRoot, bool cancel)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        using (var release = new ManualResetEvent(false))
        using (var began = new ManualResetEvent(false))
        {
            var form = Shell(Path.Combine(baseRoot, cancel ? "slow-cancel" : "slow-deadline"), ((IPEndPoint)listener.LocalEndpoint).Port, signal);
            Task server = Reply(listener, "", true, release, began);
            Task startup = null;
            try
            {
                var timer = Stopwatch.StartNew();
                if (cancel)
                {
                    startup = (Task)Call(form, "EnsureApplicationReadyAsync", false);
                    Check(Pump(() => began.WaitOne(0), 3000), "slow chunked body began during actual startup");
                    Set(form, "exiting", true);
                    Check(Pump(() => startup.IsCompleted, 1500), "slow streaming body promptly cancelled");
                }
                else
                {
                    Check(Call(form, "ProbeExpectedService").ToString() == "BusyButListening", "slow stream times out without declaring service missing");
                    Check(timer.ElapsedMilliseconds >= 3500 && timer.ElapsedMilliseconds < 5500, "absolute HTTP body deadline enforced");
                }
            }
            finally
            {
                Set(form, "exiting", true); release.Set(); listener.Stop();
                if (startup != null) Pump(() => startup.IsCompleted, 6000);
                Check(server.Wait(3000), "owned slow server finished"); DisposeShell(form);
            }
        }
    }
    static void BootstrapExit(string baseRoot)
    {
        string root = Path.Combine(baseRoot, "bootstrap [QA] & O'Brien");
        using (var signal = new EventWaitHandle(false, EventResetMode.AutoReset))
        {
            var form = Shell(root, 1, signal);
            try
            {
                string script = Path.Combine(root, "synthetic.ps1");
                string[] programs = {
                    "Write-Output 'synthetic output'; exit 0",
                    "Write-Output 'synthetic failure'; exit 37",
                    "throw 'synthetic exception'",
                };
                int[] expected = { 0, 37, 1 };
                for (int index = 0; index < programs.Length; index++)
                {
                    File.WriteAllText(script, programs[index], new UTF8Encoding(true));
                    string log = Path.Combine(root, "synthetic-" + index + ".log");
                    var info = (ProcessStartInfo)Call(form, "CreateStartInfo", script, log);
                    using (var process = Process.Start(info))
                    {
                        if (!process.WaitForExit(10000))
                        {
                            process.Kill(); // This harness's own dummy script only.
                            throw new Exception("synthetic bootstrap timeout");
                        }
                        Check(process.ExitCode == expected[index], "bootstrap exit " + expected[index] + " actual " + process.ExitCode);
                    }
                    Check(File.Exists(log) && File.ReadAllText(log).Contains("synthetic"), "bootstrap output retained " + index);
                }
            }
            finally { DisposeShell(form); }
        }
    }
    [STAThread] static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();
            BootstrapExit(args[0]);
            Identity(args[0]); Busy(args[0]); Marker(args[0], false); Marker(args[0], true);
            SlowBody(args[0], false); SlowBody(args[0], true);
            Console.WriteLine("LIFECYCLE_OMISSIONS_OK"); return 0;
        }
        catch (Exception error) { Console.WriteLine(error.ToString()); return 1; }
    }
}
