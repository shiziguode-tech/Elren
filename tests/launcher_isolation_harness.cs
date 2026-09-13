using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

internal static class LauncherIsolationTests {
    static void Check(bool ok, string message) { if(!ok) throw new Exception(message); }
    public static int Main(string[] args) {
        if(args.Length == 1 && args[0] == "--sentinel") { Thread.Sleep(30000); return 0; }
        string root = Path.GetFullPath(args[0]);
        string sibling = root + "-other";
        Directory.CreateDirectory(sibling);
        string own = Process.GetCurrentProcess().MainModule.FileName;
        string sentinel = Path.Combine(sibling, "Elren.exe");
        File.Copy(own, sentinel);
        string a = ElrenLauncher.PackageChannelName(@"Local\ElrenShow", root);
        string b = ElrenLauncher.PackageChannelName(@"Local\ElrenShow", sibling);
        Check(a != b, "Sibling installation shared activation event");
        Check(a == ElrenLauncher.PackageChannelName(@"Local\ElrenShow", root.ToUpperInvariant()+"\\"), "Canonical path mismatch");
        using(var eventA = new EventWaitHandle(false, EventResetMode.AutoReset, a))
        using(var eventB = new EventWaitHandle(false, EventResetMode.AutoReset, b)) {
            eventA.Set();
            Check(!eventB.WaitOne(0), "Activation crossed installation");
            Check(eventA.WaitOne(0), "Own activation lost");
        }
        Check(ElrenLauncher.PackageChannelName(@"Local\ElrenLauncher", root) !=
              ElrenLauncher.PackageChannelName(@"Local\ElrenLauncher", sibling), "Shared mutex");
        Check(ElrenLauncher.PackageChannelName("Elren-activation", root) !=
              ElrenLauncher.PackageChannelName("Elren-activation", sibling), "Shared task activation file");
        using(var child = Process.Start(new ProcessStartInfo(sentinel, "--sentinel") {
            UseShellExecute=false, CreateNoWindow=true, WindowStyle=ProcessWindowStyle.Hidden })) {
            try {
                Thread.Sleep(200);
                ElrenLauncher.TerminatePreviousElrenLaunchers(root);
                ElrenLauncher.TerminatePreviousElrenLaunchers(null);
                Check(!child.HasExited, "Foreign Elren process was terminated");
            } finally {
                if(!child.HasExited) { child.Kill(); child.WaitForExit(5000); }
            }
        }
        Console.WriteLine("LAUNCHER_INSTALLATION_ISOLATION_OK");
        return 0;
    }
}
