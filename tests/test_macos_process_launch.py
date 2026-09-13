import plistlib
import types

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool


def make_app(tmp_path, binary="Demo"):
    app = tmp_path / "Demo Name.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": binary}))
    if "/" not in binary:
        (app / "Contents/MacOS" / binary).write_bytes(b"test fixture, not executed")
    return app


def test_mac_launch_uses_launchservices_without_fabricating_an_app_pid(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.sys.platform", "darwin")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.subprocess.run", run)
    result = ProcessManagerTool._execute_sync({"action": "launch", "application": str(app),
        "arguments": ["quoted argument", "-not-an-open-option"]}, ToolContext(task_id="qa", workspace=str(tmp_path)))
    assert calls[0][0] == ["/usr/bin/open", "-g", "-a", str(app.resolve()), "--args", "quoted argument", "-not-an-open-option"]
    assert result["launch_requested"] and result["pid"] is None
    assert not result["window_tracking"]["enabled"]


@pytest.mark.parametrize("binary", ["zsh", "Terminal", "osascript", "../escape"])
def test_mac_app_cannot_bypass_command_launcher_guard(tmp_path, monkeypatch, binary):
    app = make_app(tmp_path, binary)
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.sys.platform", "darwin")
    with pytest.raises(PermissionError):
        ProcessManagerTool._execute_sync({"action": "launch", "application": str(app)},
                                        ToolContext(task_id="qa", workspace=str(tmp_path)))


def test_mac_launch_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.sys.platform", "darwin")
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.subprocess.run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr="LaunchServices error"))
    with pytest.raises(RuntimeError, match="launch failed"):
        ProcessManagerTool._execute_sync({"action": "launch", "application": str(app)},
                                        ToolContext(task_id="qa", workspace=str(tmp_path)))
