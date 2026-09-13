import sys
from types import SimpleNamespace

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin import computer


@pytest.fixture
def mac_input(monkeypatch):
    calls = []
    monkeypatch.setattr(computer, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(hotkey=lambda *keys: calls.append(keys)))
    monkeypatch.setattr(computer.time, "sleep", lambda _: None)
    return calls


@pytest.mark.parametrize("keys", [["command", "w"], ["cmd", "w"], ["cmd", "q"]])
def test_mac_close_does_not_require_windows_handle(tmp_path, monkeypatch, mac_input, keys):
    states = iter([
        {"application": "Notes", "window_title": "QA draft", "process_id": 0},
        {"application": "Finder", "window_title": "QA draft", "process_id": 0},
    ])
    monkeypatch.setattr(computer, "_foreground_window_state", lambda: next(states))
    result = computer.ComputerTool(tmp_path)._execute_sync({
        "action": "hotkey", "keys": keys, "target_window": "QA draft",
        "allow_user_input_interference": True,
    })
    assert result["effect_verified"] is True
    assert mac_input == [("command", keys[-1])]


@pytest.mark.parametrize("target", [None, "Other document"])
def test_mac_quit_requires_matching_observed_window(tmp_path, monkeypatch, mac_input, target):
    monkeypatch.setattr(computer, "_foreground_window_state", lambda: {
        "application": "Notes", "window_title": "Unsaved draft", "process_id": 0,
    })
    with pytest.raises((ValueError, RuntimeError), match="target_window"):
        computer.ComputerTool(tmp_path)._execute_sync({
            "action": "hotkey", "keys": ["cmd", "q"], "target_window": target,
            "allow_user_input_interference": True,
        })
    assert mac_input == []


@pytest.mark.parametrize("keys", [
    ["command", "option", "backspace"], ["cmd", "alt", "delete"],
])
def test_mac_permanent_delete_needs_explicit_current_request(tmp_path, mac_input, keys):
    arguments = {"action": "hotkey", "keys": keys, "allow_user_input_interference": True}
    with pytest.raises(PermissionError, match="is permanent"):
        computer.ComputerTool(tmp_path)._execute_sync(arguments, ToolContext(
            task_id="qa-mac-delete", workspace=str(tmp_path), user_prompt="删除选中文件",
        ))
    assert mac_input == []
    computer.ComputerTool(tmp_path)._execute_sync(arguments, ToolContext(
        task_id="qa-mac-delete", workspace=str(tmp_path), user_prompt="永久删除选中文件，不进入回收站",
    ))
    assert mac_input == [("command", "alt", keys[-1])]


def test_mac_no_change_is_not_reported_as_closed(tmp_path, monkeypatch, mac_input):
    monkeypatch.setattr(computer, "_foreground_window_state", lambda: {
        "application": "Notes", "window_title": "QA draft", "process_id": 0,
    })
    with pytest.raises(RuntimeError, match="not verified as closed"):
        computer.ComputerTool(tmp_path)._execute_sync({
            "action": "hotkey", "keys": ["command", "w"], "target_window": "QA draft",
            "allow_user_input_interference": True,
        })


def test_mac_foreground_guidance_names_available_platform_tool(tmp_path, mac_input):
    with pytest.raises(PermissionError, match="macos_ui"):
        computer.ComputerTool(tmp_path)._execute_sync({"action": "type", "text": "QA"})
    assert mac_input == []


def test_mac_control_w_is_not_misclassified_as_command_w(tmp_path, mac_input):
    result = computer.ComputerTool(tmp_path)._execute_sync({
        "action": "hotkey", "keys": ["ctrl", "w"], "allow_user_input_interference": True,
    })
    assert mac_input == [("ctrl", "w")]
    assert result["effect_verified"] is False
    assert result["verification_required"] is True


@pytest.mark.parametrize("returncode,stdout", [(1, ""), (0, "")])
def test_failed_mac_window_observation_cannot_masquerade_as_window_change(monkeypatch, mac_input, returncode, stdout):
    monkeypatch.setattr(computer.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=returncode, stdout=stdout,
    ))
    monkeypatch.setattr(computer, "credential_safe_environment", dict)
    with pytest.raises(RuntimeError, match="Unable to"):
        computer._foreground_window_state()
