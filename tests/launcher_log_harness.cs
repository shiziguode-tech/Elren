using System;
using System.IO;
using System.Reflection;
using System.Text;

internal static class LauncherLogTests
{
    static int Main(string[] args)
    {
        // Static reader only: never construct a Form or call Launcher.Main.
        var method = typeof(DesktopShellForm).GetMethod("ReadStartupLogTail", BindingFlags.Static | BindingFlags.NonPublic);
        string result;
        if (args.Length > 3 && args[3] == "writer")
        {
            using (var writer = new FileStream(args[0], FileMode.Open, FileAccess.Write, FileShare.ReadWrite))
                result = (string)method.Invoke(null, new object[] {args[0], Int32.Parse(args[2])});
        }
        else result = (string)method.Invoke(null, new object[] {args[0], Int32.Parse(args[2])});
        File.WriteAllText(args[1], result, new UTF8Encoding(false));
        return 0;
    }
}
