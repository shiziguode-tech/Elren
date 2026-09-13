"""Native append-only launch log test; no application or privacy dialogs."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_launcher_uses_non_truncating_regular_file_log():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    assert "openLaunchLog(at: logURL)" in source
    assert "createFile(atPath: logURL.path" not in source
    assert "O_APPEND | O_NOFOLLOW | O_NONBLOCK" in source


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires native macOS Foundation/Darwin")
def test_native_log_preserves_existing_data_and_rejects_special_files(tmp_path):
    assert shutil.which("swiftc"), "Native QA requires the build host Swift compiler"
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    helper = "func openLaunchLog" + source.split("func openLaunchLog", 1)[1].split("enum StartupProbeDecision", 1)[0]
    fixture = tmp_path / "main.swift"
    fixture.write_text("import Foundation\nimport Darwin\n" + helper + r'''
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let log = root.appendingPathComponent("launch.log")
try Data("previous failure\n".utf8).write(to: log)
for text in ["restart one\n", "restart two\n"] {
    guard let handle = openLaunchLog(at: log) else { fatalError("open log") }
    try handle.write(contentsOf: Data(text.utf8))
    try handle.close()
}
precondition(try! String(contentsOf: log, encoding: .utf8) == "previous failure\nrestart one\nrestart two\n")
let link = root.appendingPathComponent("link.log")
try FileManager.default.createSymbolicLink(at: link, withDestinationURL: log)
precondition(openLaunchLog(at: link) == nil)
precondition(openLaunchLog(at: root) == nil)
let fifo = root.appendingPathComponent("fifo.log")
precondition(mkfifo(fifo.path, 0o600) == 0)
precondition(openLaunchLog(at: fifo) == nil)
let fresh = root.appendingPathComponent("fresh.log")
try openLaunchLog(at: fresh)!.close()
let attrs = try FileManager.default.attributesOfItem(atPath: fresh.path)
precondition((attrs[.posixPermissions] as! NSNumber).intValue & 0o077 == 0)
print("Native append/reopen/symlink/directory/FIFO/private-create checks passed")
''', encoding="utf-8")
    executable = tmp_path / "log-qa"
    subprocess.run(["swiftc", str(fixture), "-o", str(executable)], check=True, timeout=60)
    result = subprocess.run([str(executable), str(tmp_path)], check=True, timeout=10,
                            capture_output=True, text=True)
    assert "checks passed" in result.stdout
