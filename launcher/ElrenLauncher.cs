using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("Elren")]
[assembly: AssemblyDescription("Native desktop shell for Elren")]
[assembly: AssemblyProduct("Elren")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

// A single surface renderer avoids WinForms' separate image-margin gradient,
// classic selection fill and system border producing mismatched colour blocks.
internal sealed class ElrenTrayMenu : ContextMenuStrip
{
    internal static readonly Color Surface = Color.White;
    internal static readonly Color Ink = Color.FromArgb(28, 28, 28);
    internal static readonly Color Hover = Color.FromArgb(243, 243, 243);
    internal static readonly Color Edge = Color.FromArgb(229, 229, 229);
    private readonly Font menuFont;

    protected override Padding DefaultPadding { get { return Padding.Empty; } }

    public ElrenTrayMenu()
    {
        ShowImageMargin = false;
        ShowCheckMargin = false;
        // The legacy CS_DROPSHADOW belongs to the rectangular native popup,
        // not its rounded Region. It leaves a dark square at the bottom/right.
        // Keep the softly outlined rounded surface, without that second frame.
        DropShadowEnabled = false;
        AutoSize = false;
        menuFont = new Font("Microsoft YaHei UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
        Font = menuFont;
        BackColor = SystemInformation.HighContrast ? SystemColors.Menu : Surface;
        ForeColor = SystemInformation.HighContrast ? SystemColors.MenuText : Ink;
        Renderer = SystemInformation.HighContrast
            ? (ToolStripRenderer)new ToolStripSystemRenderer() : new TrayRenderer();
        Padding = Padding.Empty;
    }

    protected override void OnOpening(System.ComponentModel.CancelEventArgs e)
    {
        PrepareLayout();
        base.OnOpening(e);
    }

    internal static GraphicsPath RoundedRectangle(Rectangle bounds, int radius)
    {
        var path = new GraphicsPath();
        int d = Math.Min(radius * 2, Math.Min(bounds.Width, bounds.Height));
        if (d <= 0) { path.AddRectangle(bounds); return path; }
        path.AddArc(bounds.Left, bounds.Top, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Top, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.Left, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (Width < 2 || Height < 2) return;
        Region previous = Region;
        if (SystemInformation.HighContrast) Region = null;
        else
        {
            float scale;
            using (Graphics graphics = CreateGraphics()) scale = graphics.DpiX / 96F;
            using (var path = RoundedRectangle(ClientRectangle, (int)(8 * scale)))
                Region = new Region(path);
        }
        if (previous != null) previous.Dispose();
    }

    internal void PrepareLayout()
    {
        float scale;
        using (Graphics graphics = CreateGraphics()) scale = graphics.DpiX / 96F;
        int width = (int)Math.Round(164 * scale);
        foreach (ToolStripItem item in Items)
            width = Math.Max(width, TextRenderer.MeasureText(item.Text, Font).Width + (int)(40 * scale));
        SuspendLayout();
        // Items share the popup's full width. Text alone has an inset; neither
        // selection backgrounds nor the divider may leave a white side gutter.
        Padding = Padding.Empty;
        int height = Padding.Vertical;
        foreach (ToolStripItem item in Items)
        {
            item.AutoSize = false;
            item.Size = new Size(width, item is ToolStripSeparator ? 6 : Math.Max((int)Math.Round(40 * scale), Font.Height + (int)Math.Round(16 * scale)));
            item.Margin = Padding.Empty;
            height += item.Height;
        }
        // Native menu visibility checks use an exclusive bottom coordinate.
        // Leave one border pixel so the last row is not treated as overflow.
        Size = new Size(width + Padding.Horizontal, height + 1);
        ResumeLayout(true);
        PerformLayout();
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing) menuFont.Dispose();
    }

    private sealed class TrayRenderer : ToolStripProfessionalRenderer
    {
        public TrayRenderer() { RoundedEdges = false; }
        protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e)
        {
            e.Graphics.Clear(Surface);
        }
        protected override void OnRenderImageMargin(ToolStripRenderEventArgs e) { }
        protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
        {
            using (var pen = new Pen(Edge))
            using (var path = RoundedRectangle(new Rectangle(0, 0, e.ToolStrip.Width - 1, e.ToolStrip.Height - 1), (int)(8 * e.Graphics.DpiX / 96F)))
            {
                var previous = e.Graphics.SmoothingMode;
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                e.Graphics.DrawPath(pen, path);
                e.Graphics.SmoothingMode = previous;
            }
            // ToolStripDropDownMenu forcibly insets separator item bounds.
            // Paint the divider in popup coordinates so it spans the surface.
            using (var pen = new Pen(Edge))
                foreach (ToolStripItem item in e.ToolStrip.Items)
                    if (item is ToolStripSeparator && item.Available)
                    {
                        int y = item.Bounds.Top + item.Height / 2;
                        e.Graphics.DrawLine(pen, 0, y, e.ToolStrip.Width - 1, y);
                    }
        }
        protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e)
        {
            if (!e.Item.Selected || !e.Item.Enabled) return;
            using (var brush = new SolidBrush(Hover))
            {
                // The popup Region clips the OUTER corners. Rounding every
                // item separately creates white notches inside the menu.
                e.Graphics.FillRectangle(brush, 0, 0, e.Item.Width, e.Item.Height);
            }
        }
        protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e)
        {
            // Drawn once by OnRenderToolStripBorder without item clipping.
        }
        protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e)
        {
            int inset = Math.Max(16, (int)(16 * e.Graphics.DpiX / 96F));
            var bounds = new Rectangle(inset, 0, Math.Max(0, e.Item.Width - inset * 2), e.Item.Height);
            TextRenderer.DrawText(e.Graphics, e.Text, e.TextFont, bounds,
                e.Item.Enabled ? Ink : SystemColors.GrayText,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix);
        }
    }
}

internal static class ElrenLauncher
{
    internal const string AppName = "Elren";
    internal const int DefaultPort = 8765;
    internal static readonly bool ChineseUi = DetectChineseSystemLanguage();

    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);

    [STAThread]
    private static void Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        if (args.Length == 1 && args[0].Equals("--self-test", StringComparison.OrdinalIgnoreCase))
        {
            Environment.Exit(RunSelfTest(root));
            return;
        }

        bool activationRequested = WriteActivationRequest(args);

        // Installations own independent shells. A matching product name is
        // not authority to close another folder's app or its running tasks.

        try
        {
            // Per-monitor V2 prevents blurry or incorrectly sized startup UI
            // when a window moves between displays with different scaling.
            // Fall back for older Windows builds that do not expose the API.
            if (!SetProcessDpiAwarenessContext(new IntPtr(-4))) SetProcessDPIAware();
        }
        catch { try { SetProcessDPIAware(); } catch { } }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        using (var mutex = new Mutex(false, PackageChannelName(@"Local\ElrenLauncher", root)))
        using (var showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, PackageChannelName(@"Local\ElrenShow", root)))
        {
            bool ownsMutex;
            try { ownsMutex = mutex.WaitOne(0, false); }
            catch (AbandonedMutexException) { ownsMutex = true; }

            if (!ownsMutex)
            {
                // A task activation is a routing message from the service,
                // not a request to replace the desktop shell. Killing the
                // mutex owner here made every human-takeover prompt look like
                // a crash followed by an immediate restart.
                if (activationRequested)
                {
                    showSignal.Set();
                    return;
                }

                // The same installed copy already owns the mutex. Revealing
                // its tray window needs no six-second replacement handoff.
                if (SamePackageLauncherRunning(root))
                {
                    // It may be finishing a user-requested exit. Take over
                    // immediately if its mutex was released during the probe.
                    try { ownsMutex = mutex.WaitOne(0, false); }
                    catch (AbandonedMutexException) { ownsMutex = true; }
                    if (!ownsMutex)
                    {
                        showSignal.Set();
                        return;
                    }
                }

                // The same package may be finishing shutdown. Never close
                // another installation to acquire a desktop mutex.
                if (!ownsMutex)
                {
                    try { ownsMutex = mutex.WaitOne(6000, false); }
                    catch (AbandonedMutexException) { ownsMutex = true; }
                }
                if (!ownsMutex)
                {
                    showSignal.Set();
                    return;
                }
            }

            try
            {
                Application.Run(new DesktopShellForm(root, ResolveLocalUrl(root), showSignal));
            }
            finally
            {
                mutex.ReleaseMutex();
            }
        }
    }

    internal static bool SamePackageLauncherRunning(string root)
    {
        string executable = Path.GetFullPath(Path.Combine(root, "Elren.exe"));
        int currentId = Process.GetCurrentProcess().Id;
        bool found = false;
        foreach (Process candidate in Process.GetProcessesByName("Elren"))
        {
            try
            {
                if (candidate.Id != currentId && !candidate.HasExited && candidate.MainModule != null
                    && Path.GetFullPath(candidate.MainModule.FileName).Equals(executable, StringComparison.OrdinalIgnoreCase))
                    found = true;
            }
            catch { /* An inaccessible process is never assumed to be ours. */ }
            finally { candidate.Dispose(); }
        }
        return found;
    }

    internal static void TerminatePreviousElrenLaunchers(string currentRoot = null)
    {
        // Compatibility hook retained for old diagnostic harnesses only.
        // No automatic cross-installation or legacy-brand process termination.
    }

    internal static string PackageChannelName(string prefix, string packageRoot)
    {
        string canonical = Path.GetFullPath(packageRoot).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar).ToLowerInvariant();
        using (SHA256 hash = SHA256.Create())
        {
            return prefix + "-" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(canonical))).Replace("-", "").ToLowerInvariant();
        }
    }

    private static int RunSelfTest(string root)
    {
        string[] required = {
            "start.ps1",
            "Microsoft.Web.WebView2.Core.dll",
            "Microsoft.Web.WebView2.WinForms.dll",
            "WebView2Loader.dll"
        };
        foreach (string name in required)
        {
            if (!File.Exists(Path.Combine(root, name)))
                return 2;
        }
        return Environment.Is64BitOperatingSystem ? 0 : 3;
    }

    private static string ActivationRequestPath()
    {
        return Path.Combine(Path.GetTempPath(), PackageChannelName("Elren-activation", AppDomain.CurrentDomain.BaseDirectory) + ".txt");
    }

    private static bool WriteActivationRequest(string[] args)
    {
        foreach (string argument in args)
        {
            if (!argument.StartsWith("--task=", StringComparison.OrdinalIgnoreCase)) continue;
            string taskId = argument.Substring(7).Trim();
            if (taskId.Length < 16 || taskId.Length > 80) return false;
            foreach (char value in taskId)
                if (!Char.IsLetterOrDigit(value) && value != '-' && value != '_') return false;
            try
            {
                File.WriteAllText(
                    ActivationRequestPath(),
                    "/?task=" + Uri.EscapeDataString(taskId) + "&handoff=1",
                    new UTF8Encoding(false)
                );
            }
            catch { return false; }
            return true;
        }
        return false;
    }

    internal static string TakeActivationRequest()
    {
        string path = ActivationRequestPath();
        try
        {
            if (!File.Exists(path)) return String.Empty;
            string route = File.ReadAllText(path, Encoding.UTF8).Trim();
            try { File.Delete(path); } catch { }
            return route.StartsWith("/?task=", StringComparison.Ordinal) ? route : String.Empty;
        }
        catch { return String.Empty; }
    }

    internal static string ResolveLocalUrl(string root)
    {
        int port;
        string configured = Environment.GetEnvironmentVariable("ELREN_PORT");
        if (String.IsNullOrWhiteSpace(configured))
            configured = Environment.GetEnvironmentVariable("MILO_PORT");
        if (String.IsNullOrWhiteSpace(configured))
            configured = Environment.GetEnvironmentVariable("DEEPDESK_PORT");
        if (String.IsNullOrWhiteSpace(configured))
        {
            string envPath = Path.Combine(root, ".env");
            if (File.Exists(envPath))
            {
                try
                {
                    foreach (string rawLine in File.ReadAllLines(envPath, Encoding.UTF8))
                    {
                        string line = rawLine.Trim();
                        if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
                            continue;
                        int separator = line.IndexOf('=');
                        if (separator <= 0) continue;
                        string key = line.Substring(0, separator).Trim();
                        if (!key.Equals("ELREN_PORT", StringComparison.OrdinalIgnoreCase)
                            && !key.Equals("MILO_PORT", StringComparison.OrdinalIgnoreCase)
                            && !key.Equals("DEEPDESK_PORT", StringComparison.OrdinalIgnoreCase))
                            continue;
                        configured = line.Substring(separator + 1).Trim().Trim('"', '\'');
                        break;
                    }
                }
                catch { }
            }
        }
        if (!Int32.TryParse(configured, out port) || port < 1 || port > 65535)
            port = DefaultPort;
        return "http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/";
    }

    internal static string RootIdentity(string root)
    {
        string normalized = Path.GetFullPath(root).TrimEnd('\\', '/').ToLowerInvariant();
        using (SHA256 sha = SHA256.Create())
        {
            byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(normalized));
            var result = new StringBuilder(digest.Length * 2);
            foreach (byte value in digest)
                result.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            return result.ToString();
        }
    }

    private static bool DetectChineseSystemLanguage()
    {
        string language = CultureInfo.CurrentUICulture == null ? String.Empty : CultureInfo.CurrentUICulture.Name;
        if (String.IsNullOrWhiteSpace(language) && CultureInfo.InstalledUICulture != null)
            language = CultureInfo.InstalledUICulture.Name;
        return !String.IsNullOrWhiteSpace(language) && language.StartsWith("zh", StringComparison.OrdinalIgnoreCase);
    }
}

internal static class StartupDrawing
{
    internal static GraphicsPath RoundedRectangle(RectangleF bounds, float radius)
    {
        float diameter = radius * 2F;
        var path = new GraphicsPath();
        path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 180F, 90F);
        path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270F, 90F);
        path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0F, 90F);
        path.AddArc(bounds.Left, bounds.Bottom - diameter, diameter, diameter, 90F, 90F);
        path.CloseFigure();
        return path;
    }
}

internal sealed class RefinedStartupCard : Panel
{
    public RefinedStartupCard()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true);
        BackColor = Color.FromArgb(246, 246, 244);
    }

    protected override void OnPaintBackground(PaintEventArgs e)
    {
        // A transparent custom WinForms panel can expose the uninitialised
        // double buffer as a solid black rectangle on some GPUs.  Paint the
        // same neutral surface as the splash before drawing the card/shadow.
        e.Graphics.Clear(BackColor);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        RectangleF cardBounds = new RectangleF(8F, 4F, Width - 17F, Height - 17F);
        RectangleF shadowBounds = new RectangleF(10F, 10F, Width - 17F, Height - 17F);
        using (GraphicsPath shadow = StartupDrawing.RoundedRectangle(shadowBounds, 18F))
        using (var shadowBrush = new SolidBrush(Color.FromArgb(15, 15, 23, 42)))
            e.Graphics.FillPath(shadowBrush, shadow);
        using (GraphicsPath card = StartupDrawing.RoundedRectangle(cardBounds, 18F))
        using (var cardBrush = new SolidBrush(Color.White))
        using (var border = new Pen(Color.FromArgb(226, 228, 232), 1F))
        {
            e.Graphics.FillPath(cardBrush, card);
            e.Graphics.DrawPath(border, card);
        }
        base.OnPaint(e);
    }
}

internal sealed class RefinedProgressBar : Control
{
    private readonly System.Windows.Forms.Timer animationTimer;
    private int position;

    public RefinedProgressBar()
    {
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true);
        animationTimer = new System.Windows.Forms.Timer { Interval = 22 };
        animationTimer.Tick += delegate {
            position = (position + 2) % 130;
            Invalidate();
        };
        Height = 5;
    }

    public bool Running
    {
        get { return animationTimer.Enabled; }
        set
        {
            if (value) animationTimer.Start(); else animationTimer.Stop();
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        RectangleF track = new RectangleF(0F, 0F, Math.Max(1F, Width - 1F), Math.Max(1F, Height - 1F));
        using (GraphicsPath trackPath = StartupDrawing.RoundedRectangle(track, track.Height / 2F))
        using (var trackBrush = new SolidBrush(Color.FromArgb(231, 232, 235)))
            e.Graphics.FillPath(trackBrush, trackPath);
        if (!Running) return;
        float segmentWidth = Math.Max(48F, Width * .27F);
        float available = Width + segmentWidth;
        float left = ((position / 129F) * available) - segmentWidth;
        RectangleF segment = new RectangleF(left, 0F, segmentWidth, Math.Max(1F, Height - 1F));
        using (GraphicsPath segmentPath = StartupDrawing.RoundedRectangle(segment, segment.Height / 2F))
        using (var segmentBrush = new SolidBrush(Color.FromArgb(20, 20, 22)))
            e.Graphics.FillPath(segmentBrush, segmentPath);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) animationTimer.Dispose();
        base.Dispose(disposing);
    }
}

internal sealed class HighQualityPictureBox : PictureBox
{
    public HighQualityPictureBox()
    {
        SetStyle(
            ControlStyles.AllPaintingInWmPaint
            | ControlStyles.OptimizedDoubleBuffer
            | ControlStyles.UserPaint,
            true
        );
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        if (Image == null) return;
        e.Graphics.CompositingMode = CompositingMode.SourceOver;
        e.Graphics.CompositingQuality = CompositingQuality.HighQuality;
        e.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        e.Graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        e.Graphics.DrawImage(
            Image,
            ClientRectangle,
            0,
            0,
            Image.Width,
            Image.Height,
            GraphicsUnit.Pixel
        );
    }
}

internal sealed class DesktopShellForm : Form
{
    private const string ServiceHandoffMarker = "Starting the local Elren service...";
    private readonly string root;
    private readonly string localUrl;
    private readonly string rootIdentity;
    private readonly EventWaitHandle showSignal;
    private readonly Panel splash;
    private readonly Label statusLabel;
    private readonly RefinedProgressBar progress;
    private WebView2 webView;
    private readonly System.Windows.Forms.Timer healthTimer;
    private readonly CancellationTokenSource closeToken = new CancellationTokenSource();
    private Thread showSignalThread;
    private bool starting;
    private bool browserReady;
    private Task<bool> webViewPreparationTask;
    private bool serviceReadyForNavigation;
    private string recoveryApplicationUrl;
    private bool webViewPreparationInvalid;
    private bool startupBrowserRetried;
    private readonly Stopwatch startupTimer = Stopwatch.StartNew();
    private readonly object startupTimingLock = new object();
    private string startupTimingPath = String.Empty;
    private bool startupNavigationPending;
    private bool recoveringBrowser;
    private bool recoveringNavigation;
    private bool healthCheckRunning;
    private int failedHealthChecks;
    private string latestLogPath = String.Empty;
    private readonly NotifyIcon trayIcon;
    private readonly ContextMenuStrip trayMenu;
    private readonly ToolStripMenuItem exitMenuItem;
    private bool chineseUi;
    private string persistedDesktopLanguage;
    private volatile bool exiting;
    private bool allowClose;
    private bool fallbackBrowser;
    private bool trayHintShown;

    public DesktopShellForm(string root, string localUrl, EventWaitHandle showSignal)
    {
        this.root = root;
        this.localUrl = localUrl;
        this.rootIdentity = ElrenLauncher.RootIdentity(root);
        this.showSignal = showSignal;
        persistedDesktopLanguage = ReadDesktopLanguage(root);
        chineseUi = persistedDesktopLanguage == null ? ElrenLauncher.ChineseUi : persistedDesktopLanguage == "zh";

        Text = ElrenLauncher.AppName;
        Icon = LoadApplicationIcon(root);
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(960, 680);
        Size = new Size(1440, 940);
        BackColor = Color.FromArgb(246, 246, 244);
        KeyPreview = true;

        webView = CreateWebViewControl();
        Controls.Add(webView);

        splash = new Panel { Dock = DockStyle.Fill, BackColor = Color.FromArgb(246, 246, 244) };
        var card = new RefinedStartupCard { Size = new Size(560, 320) };
        var icon = new HighQualityPictureBox {
            Image = LoadSplashImage(root),
            Location = new Point(42, 38), Size = new Size(54, 54), BackColor = Color.Transparent
        };
        var title = new Label {
            AutoSize = false, Text = ElrenLauncher.AppName, Font = new Font("Microsoft YaHei UI", 20F, FontStyle.Bold),
            ForeColor = Color.FromArgb(19, 20, 22), Location = new Point(116, 35), Size = new Size(390, 39), BackColor = Color.Transparent
        };
        var productLine = new Label {
            AutoSize = false, Text = T("本地智能体 · 版本 1.0", "Private local agent · Version 1.0"),
            Font = new Font("Microsoft YaHei UI", 9.5F), ForeColor = Color.FromArgb(101, 105, 113),
            Location = new Point(118, 75), Size = new Size(390, 25), BackColor = Color.Transparent
        };
        var divider = new Panel {
            Location = new Point(42, 119), Size = new Size(476, 1), BackColor = Color.FromArgb(232, 233, 236)
        };
        var statusKicker = new Label {
            AutoSize = false, Text = T("启动状态", "STARTUP STATUS"),
            Font = new Font("Microsoft YaHei UI", 8.5F, FontStyle.Bold), ForeColor = Color.FromArgb(112, 116, 124),
            Location = new Point(42, 140), Size = new Size(476, 22), BackColor = Color.Transparent
        };
        statusLabel = new Label {
            AutoSize = false, Text = T("正在准备安全的本机运行环境…", "Preparing the secure local runtime…"),
            Font = new Font("Microsoft YaHei UI", 11F), ForeColor = Color.FromArgb(35, 38, 44),
            Location = new Point(42, 165), Size = new Size(476, 48), BackColor = Color.Transparent
        };
        progress = new RefinedProgressBar { Location = new Point(42, 226), Size = new Size(476, 5), Running = true };
        var privacyLine = new Label {
            AutoSize = false, Text = T("正在安全启动 · 工作区数据保留在本机", "Starting securely · Workspace data stays on this device"),
            Font = new Font("Microsoft YaHei UI", 9F), ForeColor = Color.FromArgb(126, 130, 138),
            Location = new Point(42, 251), Size = new Size(476, 24), BackColor = Color.Transparent
        };
        card.Controls.Add(icon);
        card.Controls.Add(title);
        card.Controls.Add(productLine);
        card.Controls.Add(divider);
        card.Controls.Add(statusKicker);
        card.Controls.Add(statusLabel);
        card.Controls.Add(progress);
        card.Controls.Add(privacyLine);
        splash.Controls.Add(card);
        splash.Resize += delegate { card.Left = Math.Max(20, (splash.ClientSize.Width - card.Width) / 2); card.Top = Math.Max(20, (splash.ClientSize.Height - card.Height) / 2); };
        Controls.Add(splash);
        splash.BringToFront();

        healthTimer = new System.Windows.Forms.Timer { Interval = 5000 };
        healthTimer.Tick += HealthTimer_Tick;
        trayMenu = new ElrenTrayMenu();
        trayMenu.Items.Add(T("打开 Elren", "Open Elren"), null, delegate { ActivateWindow(); });
        trayMenu.Items.Add(new ToolStripSeparator());
        exitMenuItem = new ToolStripMenuItem(T("彻底退出", "Quit Elren completely"));
        exitMenuItem.Click += async delegate { await ExitCompletelyAsync(); };
        trayMenu.Items.Add(exitMenuItem);
        trayMenu.Opening += delegate {
            string language = ReadDesktopLanguage(root);
            if (language != null && language != (chineseUi ? "zh" : "en"))
                AcceptDesktopLanguageMessage(localUrl, "elren:ui-language:" + language);
        };
        trayIcon = new NotifyIcon { Icon = Icon, Text = "Elren", ContextMenuStrip = trayMenu, Visible = true };
        trayIcon.DoubleClick += delegate { ActivateWindow(); };
        Shown += DesktopShellForm_Shown;
        FormClosing += DesktopShellForm_FormClosing;
        FormClosed += DesktopShellForm_FormClosed;
        KeyDown += DesktopShellForm_KeyDown;
    }

    private async void DesktopShellForm_Shown(object sender, EventArgs e)
    {
        RecordStartupStage(StartupStage.WindowShown);
        StartShowSignalListener();
        await EnsureApplicationReadyAsync(false);
    }

    private async Task EnsureApplicationReadyAsync(bool recovering)
    {
        if (starting || exiting) return;
        starting = true;
        serviceReadyForNavigation = false;
        startupBrowserRetried = false;
        ShowSplash(recovering
            ? T("本机服务连接中断，正在自动恢复…", "The local service disconnected. Recovering automatically…")
            : T("正在检查本机 Agent…", "Checking the local Agent…"));

        try
        {
            // WebView2 startup and the backend are independent. Start both now,
            // but never navigate the prepared browser until the expected
            // package's service has passed its identity check below.
            RecordStartupStage(StartupStage.ServiceProbeStarted);
            Task<ServiceProbeResult> serviceProbe = Task.Run(() => ProbeExpectedService());
            Task<bool> browserPreparation = browserReady ? Task.FromResult(true) : PrepareWebViewAsync();
            ServiceProbeResult probe = await serviceProbe;
            var busyWait = Stopwatch.StartNew();
            while (probe == ServiceProbeResult.BusyButListening && !StartupCancelled
                && busyWait.Elapsed < TimeSpan.FromMinutes(2))
            {
                SetStatus(T("本机服务仍在运行，正在等待响应；不会重启正在进行的任务…",
                    "The local service is still running. Waiting without restarting active tasks…"));
                await Task.Delay(250);
                probe = await Task.Run(() => ProbeExpectedService());
            }
            if (closeToken.IsCancellationRequested || IsDisposed || exiting) return;
            if (probe == ServiceProbeResult.Foreign || probe == ServiceProbeResult.BusyButListening)
            {
                ShowFatal(probe == ServiceProbeResult.Foreign
                    ? T("本机端口上的服务不属于此 Elren 文件夹，未停止或替换它。", "The local port belongs to a different service. It was not stopped or replaced.")
                    : T("本机服务仍在忙碌，未停止或替换它。请稍后重试。", "The local service is still busy. It was not stopped or replaced. Please retry later."));
                return;
            }
            bool ready = probe == ServiceProbeResult.Ready;
            if (probe == ServiceProbeResult.Missing)
            {
                RecordStartupStage(StartupStage.ServiceBootstrapStarted);
                ready = await Task.Run(() => StartServiceAndWait());
            }
            if (closeToken.IsCancellationRequested || IsDisposed || exiting) return;
            if (!ready)
            {
                ShowFatal(T("本机服务未能启动。请查看日志：\n", "The local service could not start. See the log:\n") + latestLogPath);
                return;
            }
            RecordStartupStage(StartupStage.ServiceReady);
            serviceReadyForNavigation = true;

            if (!browserReady)
            {
                SetStatus(T("正在创建桌面窗口…", "Creating the desktop window…"));
                await AwaitWebViewPreparationAsync(browserPreparation);
                bool embedded = await InitializeWebViewAsync();
                if (StartupCancelled) return;
                if (!embedded)
                {
                    if (LaunchEdgeApplicationMode())
                    {
                        fallbackBrowser = true;
                        HideToTray();
                        return;
                    }
                    ShowFatal(T(
                        "无法创建桌面窗口。请安装 Microsoft Edge WebView2 Runtime 后重试。",
                        "The desktop window could not be created. Install Microsoft Edge WebView2 Runtime and try again."
                    ));
                    return;
                }
            }
            else if (recovering)
            {
                // A renderer may be showing WebView's offline error document
                // rather than the still-live SPA. Reload only after the
                // package-identity health check has confirmed recovery.
                try { webView.Reload(); } catch { }
            }

            if (exiting || closeToken.IsCancellationRequested || IsDisposed) return;
            failedHealthChecks = 0;
            webView.Visible = true;
            progress.Running = false;
            splash.Visible = false;
            healthTimer.Start();
        }
        catch (Exception error)
        {
            ShowFatal(T("启动失败：", "Startup failed: ") + error.Message + (String.IsNullOrWhiteSpace(latestLogPath) ? "" : "\n" + latestLogPath));
        }
        finally
        {
            starting = false;
        }
    }

    private bool StartServiceAndWait()
    {
        string startScript = Path.Combine(root, "start.ps1");
        string dataDirectory = Path.Combine(root, "data");
        if (!File.Exists(startScript))
            throw new FileNotFoundException(T("找不到 start.ps1，请完整解压应用后重试。", "start.ps1 is missing. Extract the complete app and try again."), startScript);

        Directory.CreateDirectory(dataDirectory);
        string attemptId = DateTime.Now.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture) + "-" + Process.GetCurrentProcess().Id;
        latestLogPath = Path.Combine(dataDirectory, "launcher-" + attemptId + ".log");
        File.WriteAllText(latestLogPath, Environment.NewLine + "=== Desktop launch " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " ===" + Environment.NewLine, new UTF8Encoding(false));
        File.WriteAllText(Path.Combine(dataDirectory, "launcher-latest.txt"), latestLogPath, Encoding.UTF8);
        PruneLauncherLogs(dataDirectory, latestLogPath, 30);
        string instancePath = Path.Combine(dataDirectory, "service-current-instance.txt");
        string bootstrapInstance = Guid.NewGuid().ToString("N");
        ProcessStartInfo startInfo = CreateStartInfo(startScript, latestLogPath);
        startInfo.EnvironmentVariables["ELREN_SERVICE_INSTANCE_ID"] = bootstrapInstance;

        using (Process process = Process.Start(startInfo))
        {
            if (process == null)
                throw new InvalidOperationException(T("无法创建后台启动进程。", "The background startup process could not be created."));
            var timer = Stopwatch.StartNew();
            int lastStageSecond = -1;
            bool handoffRecorded = false;
            while (timer.Elapsed < TimeSpan.FromMinutes(20) && !closeToken.IsCancellationRequested && !exiting)
            {
                if (process.HasExited)
                {
                    // A successful service handoff can become observable a few
                    // milliseconds after the bootstrap process exits.  Check
                    // identity once more before reporting a false startup
                    // failure, then include the actionable log tail.
                    Thread.Sleep(250);
                    if (IsExpectedServiceReady())
                        return true;
                    string detail = ReadStartupLogTail(latestLogPath, 8);
                    throw new InvalidOperationException(
                        T("后台服务提前退出，退出码：", "The background service exited early with code ")
                        + process.ExitCode
                        + (String.IsNullOrWhiteSpace(detail) ? "" : Environment.NewLine + detail));
                }

                bool handoff = HasReachedServiceHandoff(latestLogPath);
                if (handoff && !handoffRecorded)
                {
                    handoffRecorded = true;
                    RecordStartupStage(StartupStage.ServiceHandoff);
                }
                if (handoff && IsExpectedServiceReady())
                    return true;

                int elapsed = (int)timer.Elapsed.TotalSeconds;
                if (elapsed != lastStageSecond && elapsed % 2 == 0)
                {
                    lastStageSecond = elapsed;
                    SetStatus(handoff
                        ? T("正在启动 Agent 核心…", "Starting the Agent core…")
                        : T("正在检查 Python、Node.js 与内置能力…", "Checking Python, Node.js, and bundled capabilities…"));
                }
                Thread.Sleep(150);
            }
            // Closing the shell or reaching the bounded startup timeout must
            // not leave a hidden PowerShell/bootstrap tree running for minutes.
            // The live backend is deliberately retained only after readiness.
            if (!process.HasExited && TerminateSpawnedProcessTree(process))
            {
                // The cancelled bootstrap cannot run its PowerShell finally
                // cleanup. Remove only the NEW instance marker it created.
                if (File.Exists(instancePath))
                {
                    string cancelledInstance = File.ReadAllText(instancePath).Trim();
                    if (String.Equals(cancelledInstance, bootstrapInstance, StringComparison.Ordinal)) File.Delete(instancePath);
                }
            }
        }
        return false;
    }

    private static void PruneLauncherLogs(string dataDirectory, string currentLogPath, int keepCount)
    {
        try
        {
            string canonicalRoot = Path.GetFullPath(dataDirectory).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string[] candidates = Directory.GetFiles(dataDirectory, "launcher-*.log", SearchOption.TopDirectoryOnly);
            Array.Sort(candidates, delegate(string left, string right) {
                int newestFirst = File.GetLastWriteTimeUtc(right).CompareTo(File.GetLastWriteTimeUtc(left));
                return newestFirst != 0 ? newestFirst : StringComparer.OrdinalIgnoreCase.Compare(right, left);
            });
            for (int index = Math.Max(1, keepCount); index < candidates.Length; index++)
            {
                string candidate = Path.GetFullPath(candidates[index]);
                if (!candidate.StartsWith(canonicalRoot, StringComparison.OrdinalIgnoreCase)) continue;
                if (candidate.Equals(currentLogPath, StringComparison.OrdinalIgnoreCase)) continue;
                try { File.Delete(candidate); } catch { }
            }
        }
        catch
        {
            // Log retention is maintenance only and must never block startup.
        }
    }

    private static bool TerminateSpawnedProcessTree(Process process)
    {
        try
        {
            using (Process killer = Process.Start(new ProcessStartInfo {
                FileName = "taskkill.exe",
                Arguments = "/PID " + process.Id.ToString(CultureInfo.InvariantCulture) + " /T /F",
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            }))
            {
                if (killer != null && killer.WaitForExit(5000))
                    return killer.ExitCode == 0 && process.WaitForExit(2000);
            }
        }
        catch { }
        return false;
    }

    private ProcessStartInfo CreateStartInfo(string startScript, string logPath)
    {
        string powerShell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
        // A successful log pipeline does not imply that start.ps1 succeeded.
        // Preserve its explicit exit code; capture pipeline failure before any
        // subsequent PowerShell statement can replace the value of $?.
        string appendLog = "[IO.File]::AppendAllText('" + EscapePowerShell(logPath)
            + "', ([string]$_ + [Environment]::NewLine), $utf8)";
        string command = "$utf8=New-Object System.Text.UTF8Encoding($false); [Console]::InputEncoding=$utf8; [Console]::OutputEncoding=$utf8; $OutputEncoding=$utf8; $env:PYTHONIOENCODING='utf-8'; $global:LASTEXITCODE=0; $ErrorActionPreference='Stop'; try { & '"
            + EscapePowerShell(startScript) + "' *>&1 | ForEach-Object { " + appendLog
            + " }; $pipelineSucceeded=$?; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; if (-not $pipelineSucceeded) { exit 1 }; exit 0 } catch { try { "
            + appendLog + " } catch { }; exit 1 }";
        var info = new ProcessStartInfo {
            FileName = powerShell,
            Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -Command \"" + command + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        info.EnvironmentVariables["ELREN_SKIP_AUTO_BROWSER"] = "1";
        info.EnvironmentVariables["ELREN_DESKTOP_SHELL"] = "1";
        info.EnvironmentVariables["ELREN_HOST"] = "127.0.0.1";
        info.EnvironmentVariables["ELREN_PORT"] = new Uri(localUrl).Port.ToString(CultureInfo.InvariantCulture);
        info.EnvironmentVariables["ELREN_LAUNCHER_LANGUAGE"] = chineseUi ? "zh" : "en";
        // Do not let a parent PowerShell 7/IDE PSModulePath poison the Windows
        // PowerShell 5.1 bootstrap.  Only inbox module roots are required.
        string systemModules = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            @"System32\WindowsPowerShell\v1.0\Modules");
        string programModules = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            @"WindowsPowerShell\Modules");
        info.EnvironmentVariables["PSModulePath"] = systemModules + ";" + programModules;
        return info;
    }

    private static string ReadStartupLogTail(string path, int maximumLines)
    {
        try
        {
            if (String.IsNullOrWhiteSpace(path) || !File.Exists(path)) return String.Empty;
            // The backend can still be appending when startup fails. Permit its
            // writer handle and bound both disk I/O and memory before decoding;
            // one giant line must not turn a small error card into a huge read.
            const int maximumBytes = 32 * 1024;
            byte[] bytes = new byte[maximumBytes];
            int count = 0;
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                               FileShare.ReadWrite | FileShare.Delete))
            {
                stream.Seek(Math.Max(0, stream.Length - maximumBytes), SeekOrigin.Begin);
                int read;
                while (count < bytes.Length && (read = stream.Read(bytes, count, bytes.Length - count)) > 0)
                    count += read;
            }
            var lines = new System.Collections.Generic.Queue<string>();
            int firstByte = 0;
            while (firstByte < count && (bytes[firstByte] & 0xC0) == 0x80) firstByte++;
            using (var stream = new MemoryStream(bytes, firstByte, count - firstByte))
            using (var reader = new StreamReader(stream, Encoding.UTF8, true))
            {
                string line;
                while ((line = reader.ReadLine()) != null)
                {
                    lines.Enqueue(line);
                    if (lines.Count > Math.Max(1, maximumLines)) lines.Dequeue();
                }
            }
            var tail = new StringBuilder();
            foreach (string line in lines)
            {
                if (tail.Length > 0) tail.AppendLine();
                tail.Append(line);
            }
            string result = tail.ToString();
            if (result.Length <= 4096) return result;
            int firstCharacter = result.Length - 4096;
            if (Char.IsLowSurrogate(result[firstCharacter])) firstCharacter++;
            return result.Substring(firstCharacter);
        }
        catch { return String.Empty; }
    }

    private bool StartupCancelled
    {
        get { return closeToken.IsCancellationRequested || IsDisposed || Disposing || exiting; }
    }

    private Task<bool> PrepareWebViewAsync()
    {
        // Called only on the UI thread. Every startup caller shares the same
        // task and the same control, including when the backend is still cold.
        if (webViewPreparationTask == null)
            webViewPreparationTask = PrepareWebViewCoreAsync(webView);
        return webViewPreparationTask;
    }

    private async Task<bool> AwaitWebViewPreparationAsync(Task<bool> preparation)
    {
        // WebView2's environment API has no cancellation-token overload. Let
        // desktop exit finish without waiting indefinitely for COM/installer
        // work; its late continuation checks the disposed/replaced control.
        while (!preparation.IsCompleted && !StartupCancelled)
            await Task.WhenAny(preparation, Task.Delay(100));
        return !StartupCancelled && await preparation;
    }

    private async Task<bool> PrepareWebViewCoreAsync(WebView2 preparingView)
    {
        try
        {
            if (StartupCancelled) return false;
            RecordStartupStage(StartupStage.WebViewPreparationStarted);
            // Runtime detection/repair may run an installer. It must not block
            // the desktop message pump or serialise the backend preparation.
            bool runtimeAvailable = await Task.Run(() => WebViewRuntimeAvailable()
                || (!StartupCancelled && InstallBundledWebViewRuntime()));
            if (StartupCancelled || preparingView != webView || preparingView.IsDisposed) return false;
            if (!runtimeAvailable)
            {
                RecordStartupStage(StartupStage.WebViewUnavailable);
                return false;
            }
            string userData = Path.Combine(root, "data", "webview2");
            Directory.CreateDirectory(userData);
            var environmentOptions = new CoreWebView2EnvironmentOptions();
            environmentOptions.AdditionalBrowserArguments = "--autoplay-policy=no-user-gesture-required";
            environmentOptions.Language = chineseUi ? "zh-CN" : "en-US";
            RecordStartupStage(StartupStage.WebViewEnvironmentStarted);
            CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, userData, environmentOptions);
            if (StartupCancelled || preparingView != webView || preparingView.IsDisposed) return false;
            RecordStartupStage(StartupStage.WebViewEnvironmentReady);
            await preparingView.EnsureCoreWebView2Async(environment);
            if (StartupCancelled || preparingView != webView || preparingView.IsDisposed) return false;
            // Keep WebView2's HTTP cache across warm starts. Static assets are
            // versioned by the application and clearing the cache on every
            // launch made older computers pay a needless reload penalty.
            preparingView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            preparingView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            preparingView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            preparingView.CoreWebView2.Settings.IsZoomControlEnabled = true;
            preparingView.CoreWebView2.NavigationStarting += WebView_NavigationStarting;
            preparingView.CoreWebView2.NavigationCompleted += WebView_NavigationCompleted;
            preparingView.CoreWebView2.NewWindowRequested += WebView_NewWindowRequested;
            preparingView.CoreWebView2.ProcessFailed += WebView_ProcessFailed;
            preparingView.CoreWebView2.WebMessageReceived += WebView_WebMessageReceived;
            preparingView.CoreWebView2.SourceChanged += WebView_SourceChanged;
            RecordStartupStage(StartupStage.WebViewPrepared);
            return true;
        }
        catch
        {
            // Never log exception text: it can include a local URL, path or
            // command. Fixed stage names and elapsed times are sufficient.
            RecordStartupStage(StartupStage.WebViewUnavailable);
            return false;
        }
    }

    private async Task<bool> InitializeWebViewAsync()
    {
        try
        {
            if (!serviceReadyForNavigation || StartupCancelled) return false;
            bool prepared = await AwaitWebViewPreparationAsync(PrepareWebViewAsync());
            if (!serviceReadyForNavigation || StartupCancelled) return false;
            if (webViewPreparationInvalid)
            {
                // A pre-navigation crash is retried once, only after backend
                // identity verification. Repeated crashes use the fallback.
                if (startupBrowserRetried) return false;
                startupBrowserRetried = true;
                return await RecreateWebViewAsync();
            }
            if (!prepared) return false;
            // Preparation above is deliberately navigation-free. Initial
            // startup reaches here only after expected-service verification.
            string activationRoute = ElrenLauncher.TakeActivationRequest();
            string destination = SelectApplicationNavigation(activationRoute, recoveryApplicationUrl);
            startupNavigationPending = true;
            RecordStartupStage(StartupStage.NavigationStarted);
            webView.Source = new Uri(WithDesktopLanguage(destination));
            recoveryApplicationUrl = destination;
            browserReady = true;
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static WebView2 CreateWebViewControl()
    {
        return new WebView2 {
            Dock = DockStyle.Fill,
            Visible = false,
            DefaultBackgroundColor = Color.White,
        };
    }

    private async Task<bool> RecreateWebViewAsync()
    {
        if (StartupCancelled) return false;
        WebView2 previous = webView;
        try
        {
            string previousUrl = previous == null || previous.Source == null ? null : previous.Source.AbsoluteUri;
            if (IsTrustedApplicationUrl(previousUrl)) recoveryApplicationUrl = previousUrl;
        }
        catch { /* The last valid route remains usable after a renderer crash. */ }
        try
        {
            if (previous != null && previous.CoreWebView2 != null)
            {
                previous.CoreWebView2.ProcessFailed -= WebView_ProcessFailed;
                previous.CoreWebView2.NavigationCompleted -= WebView_NavigationCompleted;
                previous.CoreWebView2.WebMessageReceived -= WebView_WebMessageReceived;
                previous.CoreWebView2.SourceChanged -= WebView_SourceChanged;
            }
        }
        catch { }

        try { Controls.Remove(previous); } catch { }
        try { if (previous != null) previous.Dispose(); } catch { }

        webView = CreateWebViewControl();
        Controls.Add(webView);
        webView.SendToBack();
        splash.BringToFront();
        browserReady = false;
        webViewPreparationTask = null;
        webViewPreparationInvalid = false;
        return await InitializeWebViewAsync();
    }

    private bool WebViewRuntimeAvailable()
    {
        try { return !String.IsNullOrWhiteSpace(CoreWebView2Environment.GetAvailableBrowserVersionString()); }
        catch { return false; }
    }

    private bool InstallBundledWebViewRuntime()
    {
        string[] candidates = {
            Path.Combine(root, "MicrosoftEdgeWebview2Setup.exe"),
            Path.Combine(root, "MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
        };
        foreach (string installer in candidates)
        {
            if (!File.Exists(installer) || !IsTrustedMicrosoftInstaller(installer))
                continue;
            try
            {
                SetStatus(T("正在安装 Microsoft WebView2 运行时…", "Installing the Microsoft WebView2 Runtime…"));
                using (Process process = Process.Start(new ProcessStartInfo {
                    FileName = installer,
                    Arguments = "/silent /install",
                    WorkingDirectory = root,
                    UseShellExecute = true
                }))
                {
                    if (process != null) process.WaitForExit((int)TimeSpan.FromMinutes(8).TotalMilliseconds);
                }
                if (WebViewRuntimeAvailable()) return true;
            }
            catch { }
        }
        return WebViewRuntimeAvailable();
    }

    private bool IsTrustedMicrosoftInstaller(string path)
    {
        try
        {
            string ps = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
            string command = "$s=Get-AuthenticodeSignature -LiteralPath '" + EscapePowerShell(path) + "'; if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Microsoft Corporation'){exit 0}else{exit 7}";
            using (Process process = Process.Start(new ProcessStartInfo {
                FileName = ps, Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -Command \"" + command + "\"",
                UseShellExecute = false, CreateNoWindow = true
            }))
            {
                if (process == null) return false;
                process.WaitForExit(20000);
                return process.HasExited && process.ExitCode == 0;
            }
        }
        catch { return false; }
    }

    private bool LaunchEdgeApplicationMode()
    {
        foreach (string edge in EdgeCandidates())
        {
            if (!File.Exists(edge)) continue;
            try
            {
                Process.Start(new ProcessStartInfo {
                    FileName = edge,
                    Arguments = "--app=\"" + WithDesktopLanguage(localUrl) + "\" --start-maximized --no-first-run",
                    WorkingDirectory = root,
                    UseShellExecute = true
                });
                return true;
            }
            catch { }
        }
        return false;
    }

    private static string[] EdgeCandidates()
    {
        return new[] {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Microsoft\Edge\Application\msedge.exe")
        };
    }

    private enum ServiceProbeResult
    {
        Ready,
        BusyButListening,
        Missing,
        Foreign,
    }

    private bool IsServicePortListening()
    {
        try
        {
            Uri target = new Uri(localUrl);
            using (var client = new TcpClient())
            {
                IAsyncResult pending = client.BeginConnect(target.Host, target.Port, null, null);
                try
                {
                    if (!pending.AsyncWaitHandle.WaitOne(900)) return false;
                    client.EndConnect(pending);
                    return client.Connected;
                }
                finally
                {
                    try { pending.AsyncWaitHandle.Close(); } catch { }
                }
            }
        }
        catch { return false; }
    }

    private ServiceProbeResult ProbeExpectedService()
    {
        return ProbeExpectedServiceWithListenerQuery(() => IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners());
    }

    internal static bool? LoopbackListenerPresent(string applicationUrl, Func<IPEndPoint[]> query)
    {
        try
        {
            Uri target;
            IPAddress address;
            if (!Uri.TryCreate(applicationUrl, UriKind.Absolute, out target)
                || !IPAddress.TryParse(target.Host.Trim('[', ']'), out address)
                || !IPAddress.IsLoopback(address)) return null;
            // Mapped or unusual IPv6 loopback forms can be represented using
            // another address family in the OS table; defer them to TCP.
            if (address.AddressFamily == AddressFamily.InterNetworkV6 && !address.Equals(IPAddress.IPv6Loopback)) return null;
            IPEndPoint[] listeners = query();
            if (listeners == null) return null;
            foreach (IPEndPoint listener in listeners)
            {
                if (listener == null) return null;
                if (listener.Port != target.Port) continue;
                // IPv6Any may be dual-stack. Treat it conservatively as a
                // possible listener for either family; TCP still checks it.
                if (listener.Address.Equals(address) || listener.Address.Equals(IPAddress.IPv6Any)
                    || (address.AddressFamily == AddressFamily.InterNetwork && listener.Address.Equals(IPAddress.Any)))
                    return true;
            }
            return false;
        }
        catch { return null; }
    }

    private ServiceProbeResult ProbeExpectedServiceWithListenerQuery(Func<IPEndPoint[]> query)
    {
        try
        {
            // A local listener snapshot avoids Windows' refused-connection
            // retries altogether when the port is known to be closed. Unknown
            // or unsupported snapshots fall back to the bounded TCP probe.
            // This snapshot never authorizes readiness or process replacement.
            if (LoopbackListenerPresent(localUrl, query) == false) return ServiceProbeResult.Missing;
            // Windows HTTP connection retries can consume several seconds
            // while this loopback port is still closed. Reuse the existing
            // bounded TCP check first, including bootstrap readiness polling.
            // A listener is only permission to perform the identity request,
            // never evidence that it belongs to this package or is ready.
            if (!IsServicePortListening()) return ServiceProbeResult.Missing;
            var request = (HttpWebRequest)WebRequest.Create(localUrl + "api/desktop/identity");
            request.Method = "GET";
            // Tool execution can briefly occupy the Python event loop.  A slow
            // identity response is not proof that the service died, so use a
            // humane timeout and distinguish an open TCP listener from a
            // genuinely missing process.
            request.Timeout = 4000;
            request.ReadWriteTimeout = 4000;
            request.Proxy = null;
            request.AllowAutoRedirect = false;
            var deadline = Stopwatch.StartNew();
            using (closeToken.Token.Register(() => request.Abort()))
            using (var abortTimer = new System.Threading.Timer(_ => {
                if (StartupCancelled || deadline.ElapsedMilliseconds >= 4000)
                    request.Abort();
            }, null, 0, 50))
            using (var response = (HttpWebResponse)request.GetResponse())
            using (var stream = response.GetResponseStream())
            using (var bodyBytes = new MemoryStream())
            {
                if (response.StatusCode != HttpStatusCode.OK)
                    return IsServicePortListening() ? ServiceProbeResult.BusyButListening : ServiceProbeResult.Missing;
                const int maximumIdentityBytes = 4096;
                if (response.ContentLength > maximumIdentityBytes) return ServiceProbeResult.Foreign;
                byte[] buffer = new byte[1024];
                int read;
                while ((read = stream.Read(buffer, 0, buffer.Length)) != 0)
                {
                    if (bodyBytes.Length + read > maximumIdentityBytes) return ServiceProbeResult.Foreign;
                    bodyBytes.Write(buffer, 0, read);
                }
                return MatchesServiceIdentity(new UTF8Encoding(false, true).GetString(bodyBytes.ToArray()), rootIdentity)
                    ? ServiceProbeResult.Ready
                    : ServiceProbeResult.Foreign;
            }
        }
        catch
        {
            return IsServicePortListening() ? ServiceProbeResult.BusyButListening : ServiceProbeResult.Missing;
        }
    }

    internal static bool MatchesServiceIdentity(string body, string expectedRoot)
    {
        try
        {
            if (body.Length > 4096) return false;
            var json = new DataContractJsonSerializer(typeof(ServiceIdentity));
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(body)))
            {
                var identity = json.ReadObject(stream) as ServiceIdentity;
                return identity != null && String.Equals(identity.App, "Elren", StringComparison.Ordinal)
                    && String.Equals(identity.RootHash, expectedRoot, StringComparison.OrdinalIgnoreCase);
            }
        }
        catch { return false; }
    }

    [DataContract]
    private sealed class ServiceIdentity
    {
        [DataMember(Name = "app", IsRequired = true)] public string App;
        [DataMember(Name = "root_hash", IsRequired = true)] public string RootHash;
    }

    private bool IsExpectedServiceReady()
    {
        return ProbeExpectedService() == ServiceProbeResult.Ready;
    }

    private async void HealthTimer_Tick(object sender, EventArgs e)
    {
        if (starting || !browserReady || healthCheckRunning || closeToken.IsCancellationRequested || exiting) return;
        healthCheckRunning = true;
        try
        {
            ServiceProbeResult probe = await Task.Run(() => ProbeExpectedService());
            if (starting || closeToken.IsCancellationRequested || exiting) return;
            if (probe == ServiceProbeResult.Ready || probe == ServiceProbeResult.BusyButListening)
            {
                // Never destroy a live service merely because a long operation made
                // the HTTP health endpoint slow.  This was the cause of the repeated
                // 15-second restart loop seen in launcher logs.
                failedHealthChecks = 0;
                return;
            }
            failedHealthChecks++;
            if (failedHealthChecks >= 4)
            {
                // Debounce one final time immediately before recovery.  A stale
                // timer callback must not launch start.ps1 over a newly healthy
                // backend and kill it during handoff.
                await Task.Delay(750);
                if (starting || closeToken.IsCancellationRequested || exiting) return;
                probe = await Task.Run(() => ProbeExpectedService());
                if (starting || closeToken.IsCancellationRequested || exiting) return;
                if (probe == ServiceProbeResult.Ready || probe == ServiceProbeResult.BusyButListening)
                {
                    failedHealthChecks = 0;
                    return;
                }
                if (probe == ServiceProbeResult.Foreign)
                {
                    // Another installation or application owns this port.
                    // Recovery must report the conflict, never terminate it.
                }
                healthTimer.Stop();
                await EnsureApplicationReadyAsync(true);
            }
        }
        finally { healthCheckRunning = false; }
    }

    private void WebView_NavigationStarting(object sender, CoreWebView2NavigationStartingEventArgs e)
    {
        Uri target;
        if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out target) || IsLocalTarget(target)) return;
        e.Cancel = true;
        OpenExternal(target.AbsoluteUri);
    }

    private async void WebView_NavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (startupNavigationPending && !StartupCancelled)
        {
            startupNavigationPending = false;
            RecordStartupStage(e.IsSuccess ? StartupStage.NavigationCompleted : StartupStage.NavigationFailed);
        }
        if (e.IsSuccess || starting || recoveringNavigation || closeToken.IsCancellationRequested || exiting) return;
        Uri source = null;
        try { source = webView.Source; } catch { }
        if (source == null || !IsLocalTarget(source)) return;

        recoveringNavigation = true;
        healthTimer.Stop();
        ShowSplash(T(
            "本机服务连接中断，正在恢复当前任务…",
            "The local service disconnected. Restoring the current task…"
        ));
        try
        {
            ServiceProbeResult probe = ServiceProbeResult.Missing;
            // A long model/tool operation may delay HTTP without closing the
            // listener. Give that live service time to answer and never replace
            // it merely because WebView timed out once.
            for (int attempt = 0; attempt < 5 && !closeToken.IsCancellationRequested; attempt++)
            {
                probe = await Task.Run(() => ProbeExpectedService());
                if (probe == ServiceProbeResult.Ready) break;
                if (probe != ServiceProbeResult.BusyButListening) break;
                await Task.Delay(650);
            }
            if (closeToken.IsCancellationRequested || exiting) return;
            if (starting || !serviceReadyForNavigation) return;

            if (probe == ServiceProbeResult.Ready || probe == ServiceProbeResult.BusyButListening)
            {
                failedHealthChecks = 0;
                try { webView.Reload(); } catch { }
                webView.Visible = true;
                progress.Running = false;
                splash.Visible = false;
                healthTimer.Start();
                return;
            }

            // A genuinely missing loopback service is recovered behind the
            // native splash so users never have to stare at Edge's error page.
            await EnsureApplicationReadyAsync(true);
        }
        finally
        {
            recoveringNavigation = false;
        }
    }

    private void WebView_NewWindowRequested(object sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        e.Handled = true;
        OpenExternal(e.Uri);
    }

    private async void WebView_ProcessFailed(object sender, CoreWebView2ProcessFailedEventArgs e)
    {
        if (recoveringBrowser || closeToken.IsCancellationRequested || exiting) return;
        if (e.ProcessFailedKind != CoreWebView2ProcessFailedKind.BrowserProcessExited
            && e.ProcessFailedKind != CoreWebView2ProcessFailedKind.RenderProcessExited) return;
        if (DeferPreNavigationFailure()) return;
        if (DeferBrowserRecoveryUntilServiceReady()) return;
        recoveringBrowser = true;
        try
        {
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited)
            {
                try
                {
                    await Task.Delay(180);
                    if (StartupCancelled) return;
                    if (DeferBrowserRecoveryUntilServiceReady()) return;
                    webView.Reload();
                    return;
                }
                catch { }
            }

            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited
                || e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited)
            {
                // Recover only the embedded browser.  Restarting the whole EXE
                // made a renderer hiccup look like an application crash and
                // discarded the visible page state.
                ShowSplash(T("桌面渲染进程已退出，正在恢复界面…", "The desktop renderer exited. Restoring the interface…"));
                await Task.Delay(300);
                if (StartupCancelled) return;
                if (DeferBrowserRecoveryUntilServiceReady()) return;
                bool restored = await RecreateWebViewAsync();
                if (StartupCancelled) return;
                if (DeferBrowserRecoveryUntilServiceReady()) return;
                if (restored)
                {
                    webView.Visible = true;
                    progress.Running = false;
                    splash.Visible = false;
                }
                else
                {
                    ShowFatal(T(
                        "桌面渲染进程恢复失败。请关闭后重新打开 Elren。",
                        "The desktop renderer could not be restored. Close and reopen Elren."
                    ));
                }
            }
        }
        finally { recoveringBrowser = false; }
    }

    private bool DeferBrowserRecoveryUntilServiceReady()
    {
        if (!starting && serviceReadyForNavigation) return false;
        // The backend-recovery owner must verify the replacement service
        // before any browser-only recovery can navigate to that port.
        browserReady = false;
        webViewPreparationInvalid = true;
        return true;
    }

    private bool IsLocalTarget(Uri target)
    {
        Uri local = new Uri(localUrl);
        return target.Scheme == local.Scheme && target.Host == local.Host && target.Port == local.Port;
    }

    private bool IsTrustedApplicationUrl(string value)
    {
        Uri target;
        return Uri.TryCreate(value, UriKind.Absolute, out target) && IsLocalTarget(target)
            && target.AbsolutePath == "/" && String.IsNullOrEmpty(target.UserInfo);
    }

    internal string SelectApplicationNavigation(string activationRoute, string previousUrl)
    {
        if (!String.IsNullOrWhiteSpace(activationRoute))
        {
            string activationUrl = localUrl.TrimEnd('/') + activationRoute;
            if (IsTrustedApplicationUrl(activationUrl)) return activationUrl;
        }
        return IsTrustedApplicationUrl(previousUrl) ? previousUrl : localUrl;
    }

    private void WebView_SourceChanged(object sender, CoreWebView2SourceChangedEventArgs e)
    {
        if (StartupCancelled) return;
        try
        {
            string value = webView.Source == null ? null : webView.Source.AbsoluteUri;
            if (IsTrustedApplicationUrl(value)) recoveryApplicationUrl = value;
        }
        catch { /* Keep the last verified application route during a crash. */ }
    }

    private static void OpenExternal(string url)
    {
        try { Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true }); } catch { }
    }

    private void DesktopShellForm_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Control && e.KeyCode == Keys.R && browserReady)
        {
            webView.Reload();
            e.Handled = true;
        }
    }

    private void StartShowSignalListener()
    {
        showSignalThread = new Thread(delegate() {
            while (!closeToken.IsCancellationRequested)
            {
                if (!showSignal.WaitOne(500)) continue;
                try { BeginInvoke((Action)ActivateWindow); } catch { return; }
            }
        });
        showSignalThread.IsBackground = true;
        showSignalThread.Start();
    }

    private void ActivateWindow()
    {
        if (exiting || IsDisposed) return;
        if (fallbackBrowser) { LaunchEdgeApplicationMode(); return; }
        ShowInTaskbar = true;
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Show();
        Activate();
        BringToFront();
        string activationRoute = ElrenLauncher.TakeActivationRequest();
        if (browserReady && !String.IsNullOrWhiteSpace(activationRoute))
            webView.Source = new Uri(localUrl.TrimEnd('/') + activationRoute);
    }

    private void DesktopShellForm_FormClosed(object sender, FormClosedEventArgs e)
    {
        trayIcon.Visible = false;
        trayIcon.Dispose();
        trayMenu.Dispose();
        healthTimer.Stop();
        closeToken.Cancel();
        showSignal.Set();
        if (webView != null) webView.Dispose();
    }

    private void HideToTray()
    {
        Hide();
        ShowInTaskbar = false;
        if (!trayHintShown)
        {
            trayHintShown = true;
            trayIcon.ShowBalloonTip(2500, "Elren", T(
                "Elren 仍在后台运行。右键托盘图标可打开或彻底退出。",
                "Elren is running in the background. Right-click its tray icon to open or quit."), ToolTipIcon.Info);
        }
    }

    private void DesktopShellForm_FormClosing(object sender, FormClosingEventArgs e)
    {
        if (allowClose) return;
        if (e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            HideToTray();
        }
        // Do not block Windows logoff/shutdown. The OS owns process teardown.
    }

    private async Task ExitCompletelyAsync()
    {
        if (exiting) return;
        exiting = true;
        exitMenuItem.Enabled = false;
        exitMenuItem.Text = T("正在退出…", "Quitting…");
        healthTimer.Stop();
        bool stopped = false;
        try
        {
            // The startup worker cancels only its own spawned bootstrap tree.
            // Wait for it before reading the final service instance marker.
            for (int count = 0; starting && count < 100; count++) await Task.Delay(100);
            if (!starting) stopped = await Task.Run(() => StopPackageService(root));
        }
        catch { stopped = false; }
        if (stopped)
        {
            allowClose = true;
            Close();
            return;
        }
        exiting = false;
        exitMenuItem.Enabled = true;
        exitMenuItem.Text = T("彻底退出", "Quit Elren completely");
        ActivateWindow();
        // No automatic recovery after a pending stop request: retrying exit is
        // safe, while a health tick here could accidentally restart the service.
        MessageBox.Show(this, ShutdownFailureMessage(HasRecordedShutdownFailure(root)),
            "Elren", MessageBoxButtons.OK, MessageBoxIcon.Warning);
    }

    internal string ShutdownFailureMessage(bool recordedFailure)
    {
        return recordedFailure ? T(
            "后台清理失败，无法确认安全退出。请查看本地启动日志排查；单纯重试退出不会清除失败记录。没有终止其他应用。",
            "Backend cleanup failed; a safe shutdown could not be confirmed. Check the local startup logs. Retrying Quit does not clear the failure record. No other applications were terminated.") : T(
            "后台服务尚未确认退出，请稍后再次右键选择彻底退出。没有终止其他应用。",
            "The backend has not confirmed shutdown. Please retry Quit shortly. No other applications were terminated.");
    }

    internal static bool HasRecordedShutdownFailure(string packageRoot)
    {
        try
        {
            string data = Path.Combine(packageRoot, "data");
            string instance = File.ReadAllText(Path.Combine(data, "service-current-instance.txt")).Trim();
            Guid parsed;
            return instance.Length == 32 && Guid.TryParseExact(instance, "N", out parsed)
                && File.Exists(Path.Combine(data, "service-exit-failed-" + instance.ToLowerInvariant()));
        }
        catch { return false; }
    }

    internal static bool StopPackageService(string packageRoot)
    {
        string data = Path.Combine(packageRoot, "data");
        string current = Path.Combine(data, "service-current-instance.txt");
        if (!File.Exists(current)) return ProbePackageProcesses(packageRoot, String.Empty) == PackageProcessState.Absent;
        string instance = File.ReadAllText(current).Trim();
        Guid parsed;
        if (instance.Length != 32 || !Guid.TryParseExact(instance, "N", out parsed)) return false;
        instance = instance.ToLowerInvariant();
        string request = Path.Combine(data, "service-exit-request-" + instance);
        string complete = Path.Combine(data, "service-exit-complete-" + instance);
        string failed = Path.Combine(data, "service-exit-failed-" + instance);
        if (File.Exists(failed)) return false;
        File.WriteAllText(request, "desktop tray exit", new UTF8Encoding(false));
        var timer = Stopwatch.StartNew();
        long nextProcessCheck = 500;
        while (timer.Elapsed < TimeSpan.FromSeconds(40))
        {
            // A failure result wins even when bootstrap teardown removed or
            // replaced a marker. Absence is not itself a successful handshake.
            if (File.Exists(failed)) return false;
            if (File.Exists(complete))
            {
                try { File.Delete(request); File.Delete(complete); } catch { }
                return true;
            }
            // Never stop a replacement instance that appeared during shutdown.
            if (File.Exists(current)
                && !String.Equals(File.ReadAllText(current).Trim(), instance, StringComparison.OrdinalIgnoreCase)) return true;
            if (timer.ElapsedMilliseconds >= nextProcessCheck)
            {
                PackageProcessState state = ProbePackageProcesses(packageRoot, instance);
                if (File.Exists(failed)) return false;
                if (state == PackageProcessState.Absent)
                {
                    // Retain stale markers as evidence; do not manufacture an
                    // acknowledgment or delete a failed result to make exit pass.
                    return true;
                }
                if (state == PackageProcessState.Unknown) return false;
                nextProcessCheck = timer.ElapsedMilliseconds + 5000;
            }
            Thread.Sleep(200);
        }
        return false;
    }

    private enum PackageProcessState { Absent, Present, Unknown }

    private static string PackageRootCommandPattern(string packageRoot)
    {
        string canonicalRoot = Path.GetFullPath(packageRoot).TrimEnd('\\', '/');
        return @"(?<![\w./\\-])" + Regex.Escape(canonicalRoot) + @"(?=[\\/\s""']|$)";
    }

    internal static bool CommandReferencesPackageRoot(string commandLine, string packageRoot)
    {
        return !String.IsNullOrEmpty(commandLine) && Regex.IsMatch(commandLine.Replace('/', '\\'),
            PackageRootCommandPattern(packageRoot), RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    }

    private static PackageProcessState ProbePackageProcesses(string packageRoot, string instance)
    {
        try
        {
            // Read-only process evidence: detect the exact service instance,
            // package-local children, and Python/Node workers with this root.
            // No PID from this query is ever terminated. The query itself is
            // bounded, and inaccessible evidence fails closed.
            string canonicalRoot = Path.GetFullPath(packageRoot).TrimEnd('\\', '/');
            string command = "$ErrorActionPreference='Stop'; $r='" + EscapePowerShell(canonicalRoot)
                + "'; $pattern='" + EscapePowerShell(PackageRootCommandPattern(canonicalRoot))
                + "'; $i='" + instance + "'; $owner=" + Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture)
                + "; try { $unknown=$false; foreach($p in Get-CimInstance Win32_Process) {"
                + " if($p.ProcessId -eq $PID -or $p.ProcessId -eq $owner){continue};"
                + " $exe=[string]$p.ExecutablePath; $cmd=[string]$p.CommandLine;"
                + " if($exe -and $exe.StartsWith($r+'\\',[StringComparison]::OrdinalIgnoreCase)){exit 10};"
                + " if($p.Name -in @('python.exe','pythonw.exe','node.exe')) {"
                + " if(-not $exe -and -not $cmd){$unknown=$true;continue};"
                + " if(($i -and $cmd.IndexOf($i,[StringComparison]::OrdinalIgnoreCase) -ge 0)"
                + " -or $cmd.Replace('/','\\') -match $pattern){exit 10} } };"
                + " if($unknown){exit 20}; exit 0 } catch {exit 20}";
            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
            var info = new ProcessStartInfo {
                FileName = powershell,
                Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand "
                    + Convert.ToBase64String(Encoding.Unicode.GetBytes(command)),
                UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden,
            };
            info.EnvironmentVariables["PSModulePath"] = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Windows), @"System32\WindowsPowerShell\v1.0\Modules")
                + ";" + Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"WindowsPowerShell\Modules");
            using (Process probe = Process.Start(info))
            {
                if (probe == null) return PackageProcessState.Unknown;
                if (!probe.WaitForExit(8000))
                {
                    // Only this just-created read-only probe, never its parent,
                    // the app service, another Python process, or a process tree.
                    try { probe.Kill(); } catch { }
                    return PackageProcessState.Unknown;
                }
                return probe.ExitCode == 0 ? PackageProcessState.Absent
                    : probe.ExitCode == 10 ? PackageProcessState.Present : PackageProcessState.Unknown;
            }
        }
        catch { return PackageProcessState.Unknown; }
    }

    private void ShowSplash(string text)
    {
        if (InvokeRequired) { BeginInvoke((Action)(() => ShowSplash(text))); return; }
        statusLabel.Text = text;
        splash.Visible = true;
        splash.BringToFront();
        progress.Running = true;
    }

    private void SetStatus(string text)
    {
        if (IsDisposed) return;
        if (InvokeRequired) { try { BeginInvoke((Action)(() => SetStatus(text))); } catch { } return; }
        statusLabel.Text = text;
    }

    private enum StartupStage
    {
        WindowShown,
        ServiceProbeStarted,
        ServiceBootstrapStarted,
        ServiceHandoff,
        ServiceReady,
        WebViewPreparationStarted,
        WebViewEnvironmentStarted,
        WebViewEnvironmentReady,
        WebViewPrepared,
        WebViewUnavailable,
        NavigationStarted,
        NavigationCompleted,
        NavigationFailed,
    }

    private void RecordStartupStage(StartupStage stage)
    {
        // This is a separate, append-only diagnostic file. Never replace the
        // PowerShell launcher log, inspect user settings, or log runtime input.
        try
        {
            lock (startupTimingLock)
            {
                if (String.IsNullOrWhiteSpace(startupTimingPath))
                {
                    string dataDirectory = Path.Combine(root, "data");
                    Directory.CreateDirectory(dataDirectory);
                    string attempt = DateTime.Now.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture)
                        + "-" + Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture);
                    startupTimingPath = Path.Combine(dataDirectory, "startup-timing-" + attempt + ".log");
                }
                File.AppendAllText(startupTimingPath,
                    DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
                    + " elapsed_ms=" + startupTimer.ElapsedMilliseconds.ToString(CultureInfo.InvariantCulture)
                    + " stage=" + stage.ToString() + Environment.NewLine,
                    new UTF8Encoding(false));
            }
        }
        catch { /* Diagnostics must never prevent an otherwise valid startup. */ }
    }

    private void ShowFatal(string message)
    {
        if (IsDisposed || Disposing || closeToken.IsCancellationRequested || exiting) return;
        if (InvokeRequired) { try { BeginInvoke((Action)(() => ShowFatal(message))); } catch { } return; }
        progress.Running = false;
        statusLabel.Text = message;
        MessageBox.Show(this, message, ElrenLauncher.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private static Icon LoadApplicationIcon(string root)
    {
        string path = Path.Combine(root, "launcher", "elren.ico");
        try { if (File.Exists(path)) return new Icon(path, new Size(64, 64)); } catch { }
        return SystemIcons.Application;
    }

    private static Image LoadSplashImage(string root)
    {
        string path = Path.Combine(root, "launcher", "elren-app-icon.png");
        try
        {
            if (File.Exists(path))
            {
                using (Image source = Image.FromFile(path))
                    return new Bitmap(source);
            }
        }
        catch { }
        return LoadApplicationIcon(root).ToBitmap();
    }

    private static string EscapePowerShell(string value) { return value.Replace("'", "''"); }

    private static bool HasReachedServiceHandoff(string logPath)
    {
        try
        {
            using (var stream = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var reader = new StreamReader(stream, Encoding.UTF8, true))
                return reader.ReadToEnd().IndexOf(ServiceHandoffMarker, StringComparison.Ordinal) >= 0;
        }
        catch { return false; }
    }

    internal static string ReadDesktopLanguage(string root)
    {
        try
        {
            var path = new FileInfo(Path.Combine(root, "data", "desktop-ui-language.txt"));
            if (!path.Exists || path.Length > 16) return null;
            string language = File.ReadAllText(path.FullName, Encoding.UTF8).Trim();
            return language == "zh" || language == "en" ? language : null;
        }
        catch { return null; }
    }

    private bool DeferPreNavigationFailure()
    {
        if (browserReady) return false;
        webViewPreparationInvalid = true;
        return true;
    }

    internal string WithDesktopLanguage(string url)
    {
        if (persistedDesktopLanguage == null) return url;
        var target = new UriBuilder(url);
        foreach (string parameter in target.Query.TrimStart('?').Split('&'))
        {
            string key = parameter.Split('=')[0];
            if (Uri.UnescapeDataString(key) == "lang") return url;
        }
        string query = target.Query.TrimStart('?');
        target.Query = query + (query.Length == 0 ? "" : "&") + "lang=" + persistedDesktopLanguage;
        return target.Uri.AbsoluteUri;
    }

    private void WebView_WebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try { AcceptDesktopLanguageMessage(e.Source, e.TryGetWebMessageAsString()); }
        catch { /* Non-string messages from a page are not desktop commands. */ }
    }

    internal bool AcceptDesktopLanguageMessage(string source, string message)
    {
        if (IsDisposed || closeToken.IsCancellationRequested) return false;
        Uri page;
        var origin = new Uri(localUrl);
        // Only the local application document can update this preference.
        // File previews, other local servers and external documents are excluded.
        if (!Uri.TryCreate(source, UriKind.Absolute, out page)
            || page.Scheme != origin.Scheme || page.Host != origin.Host || page.Port != origin.Port
            || page.AbsolutePath != "/" || !String.IsNullOrEmpty(page.UserInfo)) return false;
        string language;
        if (message == "elren:ui-language:en") language = "en";
        else if (message == "elren:ui-language:zh") language = "zh";
        else return false;
        chineseUi = language == "zh";
        trayMenu.Items[0].Text = T("打开 Elren", "Open Elren");
        exitMenuItem.Text = exiting ? T("正在退出…", "Quitting…") : T("彻底退出", "Quit Elren completely");
        ((ElrenTrayMenu)trayMenu).PrepareLayout();
        if (persistedDesktopLanguage != language)
        {
            try
            {
                string directory = Path.Combine(root, "data");
                Directory.CreateDirectory(directory);
                File.WriteAllText(Path.Combine(directory, "desktop-ui-language.txt"), language, new UTF8Encoding(false));
                persistedDesktopLanguage = language;
            }
            catch { /* Read-only packages still get live language updates. */ }
        }
        return true;
    }

    private string T(string chinese, string english) { return chineseUi ? chinese : english; }
}
