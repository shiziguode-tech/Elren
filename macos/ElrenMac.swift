import AppKit
import Darwin
import WebKit

func openLaunchLog(at url: URL) -> FileHandle? {
    // Append atomically across launches; createFile(contents: nil) truncates an
    // existing log. Never follow a log-file symlink into another user file.
    let descriptor = url.path.withCString {
        Darwin.open($0, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_NONBLOCK, mode_t(0o600))
    }
    guard descriptor >= 0 else { return nil }
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0,
          (metadata.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG) else {
        Darwin.close(descriptor)
        return nil
    }
    return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
}

enum StartupProbeDecision {
    case wait, probe, showWaitingAndProbe, failed
}

struct StartupProbeState {
    let startedAt: TimeInterval
    private var nextProbeAt: TimeInterval = 0
    private var delayShown = false

    init(startedAt: TimeInterval) {
        self.startedAt = startedAt
    }

    mutating func next(now: TimeInterval, backendRunning: Bool, probeInFlight: Bool) -> StartupProbeDecision {
        guard backendRunning else { return .failed }
        guard !probeInFlight, now >= nextProbeAt else { return .wait }
        let showWaiting = !delayShown && now - startedAt >= 45
        if showWaiting { delayShown = true }
        // Consent dialogs may take arbitrarily long. Keep recovery possible,
        // but reduce network probes while the user handles the system dialog.
        nextProbeAt = now + (delayShown ? 2 : 0.25)
        return showWaiting ? .showWaitingAndProbe : .probe
    }
}

func shouldOpenLocalLinkExternally(url: URL, newWindow: Bool, linkActivated: Bool) -> Bool {
    guard linkActivated else { return false }
    // WKWebView has no default tab UI. Explicit file links must not replace
    // the chat page, including PDFs/images that WebKit can display inline.
    let fileRoutes = ["/api/artifacts/", "/api/uploads/", "/screenshots/"]
    return newWindow || fileRoutes.contains { url.path.hasPrefix($0) }
}

final class ExternalNavigationDelegate: NSObject, WKNavigationDelegate {
    var allowedPort = 8765
    var openExternalURL: (URL) -> Void = { NSWorkspace.shared.open($0) }
    var onFailure: ((String) -> Void)?
    private var contentRecoveryCount = 0

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard contentRecoveryCount < 2 else {
            onFailure?("The page renderer stopped repeatedly. Please quit and reopen Elren.")
            return
        }
        contentRecoveryCount += 1
        webView.reload()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        onFailure?("The local page could not load: \(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // A first-launch system prompt can interrupt the initial draw. Refresh
        // layout/painting without navigating again or losing the user's draft.
        webView.layoutSubtreeIfNeeded()
        webView.needsDisplay = true
        // Opt-in QA diagnostics contain geometry/counts only, never conversation
        // contents, credentials, form values or full URLs.
        guard ProcessInfo.processInfo.environment["ELREN_QA_DIAGNOSTICS"] == "1" else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            let script = """
            JSON.stringify({viewport:[innerWidth,innerHeight],ready:document.readyState,
              nodes:document.body?.children.length,
              shell:(()=>{const x=document.querySelector('.shell');if(!x)return null;
                const r=x.getBoundingClientRect(),s=getComputedStyle(x);
                return {width:r.width,height:r.height,x:r.x,y:r.y,display:s.display,
                  visibility:s.visibility,opacity:s.opacity};})(),
              center:document.elementFromPoint(innerWidth/2,innerHeight/2)?.className,
              children:[...document.querySelectorAll('.shell > *, .welcome, .composer')].map(x=>{
                const r=x.getBoundingClientRect(),s=getComputedStyle(x);
                return {tag:x.tagName,id:x.id,width:r.width,height:r.height,x:r.x,y:r.y,
                  display:s.display,visibility:s.visibility,opacity:s.opacity,color:s.color,
                  background:s.backgroundColor,textLength:x.textContent.length};})})
            """
            webView.evaluateJavaScript(script) { value, error in
                print("ELREN_QA native=\(webView.bounds) dom=\(value ?? "unavailable") error=\(error != nil)")
                fflush(stdout)
            }
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" {
            decisionHandler(.allow)
            return
        }
        let host = url.host?.lowercased()
        let port = url.port ?? (url.scheme == "http" ? 80 : 443)
        if url.scheme == "http",
           (host == "127.0.0.1" || host == "localhost"),
           port == allowedPort {
            if shouldOpenLocalLinkExternally(
                url: url, newWindow: navigationAction.targetFrame == nil,
                linkActivated: navigationAction.navigationType == .linkActivated
            ) {
                openExternalURL(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
            return
        }
        if navigationAction.navigationType == .linkActivated {
            openExternalURL(url)
        }
        decisionHandler(.cancel)
    }
}

func acceptedDesktopLanguage(body: Any, mainFrame: Bool, url: URL?, allowedPort: Int) -> String? {
    guard mainFrame, let url = url, url.scheme == "http",
          ["127.0.0.1", "localhost"].contains(url.host?.lowercased() ?? ""),
          (url.port ?? 80) == allowedPort, url.path == "/" || url.path.isEmpty,
          let language = body as? String, language == "zh" || language == "en" else { return nil }
    return language
}

func allowsLocalFilePicker(mainFrame: Bool, url: URL?, allowedPort: Int) -> Bool {
    guard mainFrame, let url = url, url.scheme == "http",
          ["127.0.0.1", "localhost"].contains(url.host?.lowercased() ?? ""),
          (url.port ?? 80) == allowedPort,
          url.path == "/" || url.path.isEmpty else { return false }
    return true
}

final class DesktopUIDelegate: NSObject, WKUIDelegate {
    var allowedPort = 8765

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        // macOS WebKit disables file inputs unless the host implements this.
        // Only the local app's main page can ask the user to select attachments.
        guard allowsLocalFilePicker(mainFrame: frame.isMainFrame,
                  url: frame.request.url, allowedPort: allowedPort),
              let window = webView.window else { completionHandler(nil); return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }
}

final class DesktopLanguageBridge: NSObject, WKScriptMessageHandler {
    var allowedPort = 8765
    var onLanguage: ((String) -> Void)?
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        // This bridge accepts only a locale from the local top-level app page.
        // Artifacts, remote pages and child frames cannot invoke native actions.
        guard message.name == "elrenLanguage",
              let language = acceptedDesktopLanguage(body: message.body,
                  mainFrame: message.frameInfo.isMainFrame, url: message.frameInfo.request.url,
                  allowedPort: allowedPort) else { return }
        onLanguage?(language)
    }
}

func makeElrenMainMenu(chinese: Bool) -> NSMenu {
    func label(_ english: String, _ translated: String) -> String { chinese ? translated : english }
    let bar = NSMenu()
    func submenu(_ title: String) -> NSMenu {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let menu = NSMenu(title: title)
        item.submenu = menu
        bar.addItem(item)
        return menu
    }
    let application = submenu("Elren")
    application.addItem(withTitle: label("Hide Elren", "隐藏 Elren"),
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    let hideOthers = application.addItem(withTitle: label("Hide Others", "隐藏其他"),
                        action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
    hideOthers.keyEquivalentModifierMask = [.command, .option]
    application.addItem(withTitle: label("Show All", "显示全部"),
                        action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
    application.addItem(.separator())
    application.addItem(withTitle: label("Quit Elren", "退出 Elren"),
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    let edit = submenu(label("Edit", "编辑"))
    edit.addItem(withTitle: label("Undo", "撤销"), action: NSSelectorFromString("undo:"), keyEquivalent: "z")
    let redo = edit.addItem(withTitle: label("Redo", "重做"), action: NSSelectorFromString("redo:"), keyEquivalent: "z")
    redo.keyEquivalentModifierMask = [.command, .shift]
    edit.addItem(.separator())
    edit.addItem(withTitle: label("Cut", "剪切"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: label("Copy", "复制"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: label("Paste", "粘贴"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: label("Select All", "全选"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    // Nil targets let the focused WKWebView/native field handle editing and
    // automatic menu validation. Never implement these by reading form values.
    let window = submenu(label("Window", "窗口"))
    window.addItem(withTitle: label("Minimize", "最小化"),
                   action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
    window.addItem(withTitle: label("Close", "关闭"),
                   action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    return bar
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var port = 8765
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private let navigationDelegate = ExternalNavigationDelegate()
    private let languageBridge = DesktopLanguageBridge()
    private let uiDelegate = DesktopUIDelegate()
    private var desktopLanguage = UserDefaults.standard.string(forKey: "ElrenUILanguage")
        ?? (Locale.preferredLanguages.first?.hasPrefix("zh") == true ? "zh" : "en")
    private var healthTimer: Timer?
    private var attempts = 0
    private var healthProbeInFlight = false
    private var startupProbe = StartupProbeState(startedAt: 0)
    private var healthGeneration = 0
    private var isTerminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = makeElrenMainMenu(chinese: desktopLanguage == "zh")
        buildWindow()
        attachToExistingOrStartBackend()
    }

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        languageBridge.onLanguage = { [weak self] language in
            guard let self = self else { return }
            UserDefaults.standard.set(language, forKey: "ElrenUILanguage")
            guard self.desktopLanguage != language else { return }
            self.desktopLanguage = language
            NSApp.mainMenu = makeElrenMainMenu(chinese: language == "zh")
        }
        configuration.userContentController.add(languageBridge, name: "elrenLanguage")
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1220, height: 820), configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = navigationDelegate
        webView.uiDelegate = uiDelegate
        navigationDelegate.onFailure = { [weak self] message in self?.showFailure(message) }

        let preparing = desktopLanguage == "zh" ? "正在准备本地智能体…" : "Preparing the private local agent…"
        let loading = """
        <!doctype html><meta charset="utf-8"><style>
        :root{color-scheme:light dark}body{margin:0;display:grid;place-items:center;height:100vh;
        font:15px -apple-system,BlinkMacSystemFont;color:#1d2433;background:#f5f7fa}
        main{width:min(520px,calc(100vw - 64px));padding:36px;border:1px solid #dfe4ec;border-radius:22px;
        background:white;box-shadow:0 18px 60px rgba(20,30,50,.1)}h1{margin:0 0 8px;font-size:28px}
        p{color:#657086;margin:0 0 28px}.bar{height:5px;border-radius:9px;background:#e7ebf1;overflow:hidden}
        .bar:after{content:'';display:block;width:38%;height:100%;border-radius:inherit;background:#111827;
        animation:move 1.3s ease-in-out infinite}@keyframes move{50%{transform:translateX(165%)}}
        @media(prefers-color-scheme:dark){body{background:#0c111b;color:#f5f7fa}main{background:#121a27;border-color:#283447}p{color:#9aa8bd}}
        </style><main><h1>Elren</h1><p>\(preparing)</p><div class="bar"></div></main>
        """

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1220, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Elren"
        window.minSize = NSSize(width: 880, height: 620)
        window.center()
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        // Begin navigation only after WebKit has a nonzero, attached viewport.
        webView.loadHTMLString(loading, baseURL: nil)
    }

    private func attachToExistingOrStartBackend() {
        guard let resources = Bundle.main.resourceURL else {
            showFailure("Application resources are missing.")
            return
        }
        let applicationSupport = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        )[0]
        let supportRoot = applicationSupport.appendingPathComponent("Elren", isDirectory: true)
        let legacyRoots = [
            applicationSupport.appendingPathComponent("Milo", isDirectory: true),
            applicationSupport.appendingPathComponent("Qelvane Desk", isDirectory: true),
        ]
        if !FileManager.default.fileExists(atPath: supportRoot.path) {
            // Preserve conversations, settings and local credentials during
            // the product-name upgrade. A failed move is non-destructive.
            for legacyRoot in legacyRoots where FileManager.default.fileExists(atPath: legacyRoot.path) {
                do {
                    try FileManager.default.moveItem(at: legacyRoot, to: supportRoot)
                    break
                } catch { continue }
            }
        }
        port = configuredPort(for: supportRoot)
        navigationDelegate.allowedPort = port
        languageBridge.allowedPort = port
        uiDelegate.allowedPort = port
        let expectedIdentity = rootIdentity(for: supportRoot)
        probeIdentity(expectedIdentity) { [weak self] ready in
            DispatchQueue.main.async {
                guard let self, !self.isTerminating else { return }
                if ready {
                    self.beginHealthCheck(expectedIdentity: expectedIdentity)
                } else {
                    self.startBackend(resources: resources, supportRoot: supportRoot, expectedIdentity: expectedIdentity)
                }
            }
        }
    }

    private func startBackend(resources: URL, supportRoot: URL, expectedIdentity: String) {
        let executable = resources.appendingPathComponent("backend/ElrenBackend")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            showFailure("The bundled Agent backend is missing or not executable.")
            return
        }

        do {
            try FileManager.default.createDirectory(at: supportRoot, withIntermediateDirectories: true)
            try installBundledWorkspace(from: resources, to: supportRoot)
        } catch {
            showFailure("Unable to create the local workspace: \(error.localizedDescription)")
            return
        }

        let process = Process()
        process.executableURL = executable
        process.currentDirectoryURL = supportRoot
        var environment = ProcessInfo.processInfo.environment
        environment["ELREN_WORKSPACE"] = supportRoot.path
        environment["ELREN_HOST"] = "127.0.0.1"
        environment["ELREN_PORT"] = String(port)
        environment["ELREN_SKIP_AUTO_BROWSER"] = "1"
        environment["ELREN_DESKTOP_SHELL"] = "1"
        environment["ELREN_BROWSER_RUNTIME"] = resources
            .appendingPathComponent("bundle/work/browser-runtime", isDirectory: true).path
        environment["ELREN_LILYPOND_HOME"] = resources
            .appendingPathComponent("bundle/work/tool-runtime/native/lilypond/lilypond-2.26.0", isDirectory: true).path
        environment["ELREN_JIANPU_LY_HOME"] = resources
            .appendingPathComponent("bundle/work/tool-runtime/native/jianpu-ly", isDirectory: true).path
        environment["ELREN_AUDIVERIS_HOME"] = resources
            .appendingPathComponent("bundle/work/tool-runtime/native/audiveris", isDirectory: true).path
        let nativeRuntime = resources.appendingPathComponent("bundle/work/tool-runtime/native")
        let runtimePaths = ["python/bin", "node/bin", "node", "cli/bin", "git/bin"].map {
            nativeRuntime.appendingPathComponent($0).path
        }
        environment["PATH"] = (runtimePaths + [environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]).joined(separator: ":")
        // Host developer environments must not redirect the signed interpreter.
        for key in ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"] { environment.removeValue(forKey: key) }
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        // Additional project packages belong in a workspace venv, never .app.
        environment["PIP_REQUIRE_VIRTUALENV"] = "true"
        let bundledOpenClaw = nativeRuntime.appendingPathComponent("node/openclaw")
        if FileManager.default.isExecutableFile(atPath: bundledOpenClaw.path) {
            environment["ELREN_OPENCLAW_CLI"] = bundledOpenClaw.path
        }
        process.environment = environment

        let logs = supportRoot.appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("macos-launch.log")
        if let handle = openLaunchLog(at: logURL) {
            process.standardOutput = handle
            process.standardError = handle
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self, !self.isTerminating else { return }
                self.showFailure("The local Agent service exited with code \(process.terminationStatus). See \(logURL.path)")
            }
        }
        do {
            try process.run()
            backend = process
            beginHealthCheck(expectedIdentity: expectedIdentity)
        } catch {
            showFailure("Unable to start the local Agent service: \(error.localizedDescription)")
        }
    }

    private func installBundledWorkspace(from resources: URL, to supportRoot: URL) throws {
        let bundleRoot = resources.appendingPathComponent("bundle", isDirectory: true)
        for name in ["skills", "plugins"] {
            let source = bundleRoot.appendingPathComponent(name, isDirectory: true)
            let destination = supportRoot.appendingPathComponent(name, isDirectory: true)
            guard FileManager.default.fileExists(atPath: source.path) else { continue }
            if !FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.copyItem(at: source, to: destination)
            }
        }
        let rules = bundleRoot.appendingPathComponent("AGENTS.md")
        let destinationRules = supportRoot.appendingPathComponent("AGENTS.md")
        if FileManager.default.fileExists(atPath: rules.path),
           !FileManager.default.fileExists(atPath: destinationRules.path) {
            try FileManager.default.copyItem(at: rules, to: destinationRules)
        }
    }

    private func rootIdentity(for root: URL) -> String {
        var normalized = root.standardizedFileURL.path.lowercased()
        while normalized.count > 1 && normalized.hasSuffix("/") {
            normalized.removeLast()
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/shasum")
        process.arguments = ["-a", "256"]
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        do {
            try process.run()
            input.fileHandleForWriting.write(Data(normalized.utf8))
            try? input.fileHandleForWriting.close()
            process.waitUntilExit()
            let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            return text.split(separator: " ").first.map(String.init) ?? ""
        } catch {
            return ""
        }
    }

    private func configuredPort(for supportRoot: URL) -> Int {
        func validPort(_ raw: String?) -> Int? {
            guard let raw,
                  let value = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
                  (1...65535).contains(value) else { return nil }
            return value
        }

        if let value = validPort(
            ProcessInfo.processInfo.environment["ELREN_PORT"]
                ?? ProcessInfo.processInfo.environment["MILO_PORT"]
                ?? ProcessInfo.processInfo.environment["DEEPDESK_PORT"]
        ) {
            return value
        }
        let envFile = supportRoot.appendingPathComponent(".env")
        guard let contents = try? String(contentsOf: envFile, encoding: .utf8) else {
            return 8765
        }
        for rawLine in contents.split(whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !line.isEmpty, !line.hasPrefix("#"),
                  let separator = line.firstIndex(of: "=") else { continue }
            let key = line[..<separator].trimmingCharacters(in: .whitespaces)
            guard key == "ELREN_PORT" || key == "MILO_PORT" || key == "DEEPDESK_PORT" else { continue }
            var rawValue = line[line.index(after: separator)...]
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if rawValue.count >= 2,
               (rawValue.hasPrefix("\"") && rawValue.hasSuffix("\"") ||
                rawValue.hasPrefix("'") && rawValue.hasSuffix("'")) {
                rawValue.removeFirst()
                rawValue.removeLast()
            }
            return validPort(rawValue) ?? 8765
        }
        return 8765
    }

    private func probeIdentity(_ expectedIdentity: String, completion: @escaping (Bool) -> Void) {
        guard !expectedIdentity.isEmpty,
              let url = URL(string: "http://127.0.0.1:\(port)/api/desktop/identity") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.2
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  let data,
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  object["app"] as? String == "Elren",
                  object["root_hash"] as? String == expectedIdentity else {
                completion(false)
                return
            }
            completion(true)
        }.resume()
    }

    private func beginHealthCheck(expectedIdentity: String) {
        healthTimer?.invalidate()
        attempts = 0
        healthProbeInFlight = false
        startupProbe = StartupProbeState(startedAt: ProcessInfo.processInfo.systemUptime)
        healthGeneration += 1
        let generation = healthGeneration
        healthTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] timer in
            guard let self, !self.isTerminating else { timer.invalidate(); return }
            guard generation == self.healthGeneration else { timer.invalidate(); return }
            switch self.startupProbe.next(
                now: ProcessInfo.processInfo.systemUptime,
                backendRunning: self.backend?.isRunning == true,
                probeInFlight: self.healthProbeInFlight
            ) {
            case .wait:
                return
            case .failed:
                self.showFailure("The local Agent service stopped before becoming ready.")
                return
            case .showWaitingAndProbe:
                self.showStartupDelay()
            case .probe:
                break
            }
            self.healthProbeInFlight = true
            self.attempts += 1
            self.probeIdentity(expectedIdentity) { ready in
                DispatchQueue.main.async {
                    guard generation == self.healthGeneration, !self.isTerminating else { return }
                    self.healthProbeInFlight = false
                    if ready {
                        timer.invalidate()
                        self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
                    }
                }
            }
        }
        healthTimer?.fire()
    }

    private func showStartupDelay() {
        let chinese = desktopLanguage == "zh"
        let title = chinese ? "仍在等待本地服务" : "Still waiting for the local service"
        let message = chinese
            ? "启动时间比平常长。如果有钥匙串或系统授权弹窗，请先完成确认。服务就绪后会自动进入；也可以关闭窗口后重新启动。"
            : "Startup is taking longer than usual. If a Keychain or system permission dialog is open, please complete it. Elren will open automatically when the service is ready. You can also close the window and restart."
        webView.loadHTMLString("<meta charset='utf-8'><style>body{font:15px -apple-system;padding:48px;color:#1d2433}main{max-width:700px;margin:auto}h1{font-size:24px}p{line-height:1.6}</style><main><h1>\(title)</h1><p>\(message)</p></main>", baseURL: nil)
    }

    private func showFailure(_ message: String) {
        healthTimer?.invalidate()
        healthGeneration += 1
        healthProbeInFlight = false
        let escaped = message
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        webView.loadHTMLString("<meta charset='utf-8'><style>body{font:15px -apple-system;padding:48px;color:#1d2433}main{max-width:700px;margin:auto}h1{font-size:24px}p{line-height:1.6}</style><main><h1>Elren could not start</h1><p>\(escaped)</p></main>", baseURL: nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        healthGeneration += 1
        healthTimer?.invalidate()
        if let backend, backend.isRunning {
            backend.terminate()
            let deadline = Date().addingTimeInterval(2)
            while backend.isRunning && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
            if backend.isRunning { kill(backend.processIdentifier, SIGKILL) }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
