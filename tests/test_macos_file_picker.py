from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_mac_file_input_has_retained_native_delegate_and_sheet_completion():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    assert "private let uiDelegate = DesktopUIDelegate()" in source
    assert "webView.uiDelegate = uiDelegate" in source
    assert "uiDelegate.allowedPort = port" in source
    picker = source.split("final class DesktopUIDelegate", 1)[1].split("final class DesktopLanguageBridge", 1)[0]
    assert "runOpenPanelWith parameters: WKOpenPanelParameters" in picker
    assert "panel.allowsMultipleSelection = parameters.allowsMultipleSelection" in picker
    assert "panel.canChooseDirectories = false" in picker
    assert "panel.canChooseFiles = true" in picker
    assert "panel.beginSheetModal(for: window)" in picker
    assert "completionHandler(response == .OK ? panel.urls : nil)" in picker
    assert "completionHandler(nil); return" in picker
    assert picker.index("guard allowsLocalFilePicker") < picker.index("NSOpenPanel()")


def test_picker_restricts_origin_frame_and_path_without_network_or_file_reads():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    gate = source.split("func allowsLocalFilePicker", 1)[1].split("final class DesktopUIDelegate", 1)[0]
    assert 'guard mainFrame' in gate
    assert 'url.scheme == "http"' in gate
    assert '(url.port ?? 80) == allowedPort' in gate
    assert 'url.path == "/" || url.path.isEmpty' in gate
    assert '"127.0.0.1", "localhost"' in gate
    assert "URLSession" not in gate
    assert "FileManager" not in gate
