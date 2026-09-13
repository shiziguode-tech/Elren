from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from deepdesk import windows_activity as activity
from deepdesk import windows_text


class FakeMonitor:
    def __init__(self, idle=10, held=False):
        self.idle = idle
        self.held = held
        self.generation = 0

    def snapshot(self):
        return activity.ActivitySnapshot(self.generation, self.idle)

    def controls_held(self):
        return self.held


@pytest.fixture
def monitor(monkeypatch):
    value = FakeMonitor()
    monkeypatch.setattr(activity, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(activity, "get_activity_monitor", lambda: value)
    return value


@pytest.mark.parametrize("seconds", [0, 0.1, 4.999])
def test_no_foreground_until_five_continuous_idle_seconds(monitor, seconds):
    monitor.idle = seconds
    with pytest.raises(activity.UserInputActive, match="5 continuous"), activity.foreground_input_scope():
        pytest.fail("foreground work entered while user active")


def test_held_ordinary_key_blocks_even_after_five_seconds(monitor):
    monitor.held = True
    with pytest.raises(activity.UserInputActive), activity.foreground_input_scope():
        pytest.fail("held key was ignored")


def test_resume_stops_existing_permit_and_cleans_up(monitor):
    with activity.foreground_input_scope():
        activity.check_input_permit()
        monitor.generation += 1
        with pytest.raises(activity.UserInputActive, match="resumed"):
            activity.check_input_permit()
    with activity.foreground_input_scope():
        activity.check_input_permit()


def test_nested_actions_share_same_user_activity_epoch(monitor):
    with activity.foreground_input_scope():
        monitor.idle = 0
        with activity.foreground_input_scope():
            activity.check_input_permit()
        monitor.generation += 1
        with pytest.raises(activity.UserInputActive), activity.foreground_input_scope():
            pass


@pytest.mark.parametrize("injected,tag,expected", [(True, activity.INPUT_TAG, 0),
                                                  (False, activity.INPUT_TAG, 1), (True, 0, 1), (False, 0, 1)])
def test_only_this_process_tagged_injection_is_ignored(injected, tag, expected):
    monitor = object.__new__(activity.WindowsActivityMonitor)
    monitor._lock = threading.Lock()
    monitor._generation = 0
    monitor._last_external = 0
    monitor._record(injected=injected, tag=tag, tick=123)
    assert monitor._generation == expected
    assert monitor._observed_tick == 123
    assert (monitor._last_external > 0) is bool(expected)


def test_missing_monitor_fails_closed(monkeypatch, monitor):
    def fail():
        raise activity.ActivityUnavailable("unavailable")
    monkeypatch.setattr(activity, "get_activity_monitor", fail)
    with pytest.raises(activity.ActivityUnavailable), activity.foreground_input_scope():
        pytest.fail("unavailable detector allowed input")
    assert not activity._input_lock.locked()


def test_another_foreground_action_cannot_interleave(monitor):
    errors = []

    def other_thread():
        try:
            with activity.foreground_input_scope():
                errors.append("entered")
        except activity.UserInputActive:
            errors.append("blocked")
    with activity.foreground_input_scope():
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(1)
    assert errors == ["blocked"]


def test_unicode_stream_stops_between_small_chunks(monitor, monkeypatch):
    counts = []

    class Send:
        def __call__(self, count, inputs, size):
            counts.append(count)
            assert all(inputs[i].ki.dwExtraInfo == activity.INPUT_TAG for i in range(count))
            monitor.generation += 1
            return count
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(SendInput=Send()), raising=False)
    with pytest.raises(activity.UserInputActive):
        windows_text.type_unicode("中" * 100)
    assert counts == [64]


def test_mouse_up_cleanup_is_not_blocked_by_resumed_user(monitor, monkeypatch):
    flags = []

    def send(flag, **kwargs):
        flags.append(flag)
        monitor.generation += 1
    monkeypatch.setattr(windows_text, "send_mouse_input", send)
    windows_text.click_mouse()
    assert flags == [windows_text.MOUSEEVENTF_LEFTDOWN, windows_text.MOUSEEVENTF_LEFTUP]


@pytest.mark.parametrize("mode", ["unicode", "chord"])
def test_partial_sendinput_releases_keys_and_preserves_original_error(monitor, monkeypatch, mode):
    calls = []
    error = {"value": 5}

    class Send:
        def __call__(self, count, inputs, size):
            calls.append([inputs[i].ki.dwFlags for i in range(count)])
            if len(calls) == 1:
                monitor.generation += 1
                return 1
            error["value"] = 0
            return count

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(SendInput=Send()), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error["value"], raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic rejection"), raising=False)
    with pytest.raises(OSError) as failure:
        if mode == "unicode":
            windows_text.type_unicode("中文")
        else:
            windows_text.send_key_chord(["ctrl", "end"])
    assert failure.value.errno == 5
    assert len(calls) == 2
    assert all(flags & windows_text.KEYEVENTF_KEYUP for flags in calls[1])


@pytest.mark.parametrize("scan", [0x0131, 0x0331, 0x0631])
def test_symbol_key_never_silently_discards_required_modifiers(monkeypatch, scan):
    class KeyScan:
        def __call__(self, character):
            return scan
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(VkKeyScanW=KeyScan()), raising=False)
    with pytest.raises(ValueError, match="explicit modifier"):
        windows_text._virtual_key("!")


def test_missed_hook_event_invalidates_permit():
    monitor = object.__new__(activity.WindowsActivityMonitor)
    monitor._lock = threading.Lock()
    monitor._generation = 1
    monitor._last_external = time.monotonic() - 30
    monitor._observed_tick = 10
    monitor._failed = False
    monitor._thread = SimpleNamespace(is_alive=lambda: True)
    monitor._heartbeat = time.monotonic()
    monitor._last_input = lambda: 11
    snapshot = monitor.snapshot()
    assert snapshot.generation == 2
    assert snapshot.idle_seconds < 0.1


def test_uia_foreground_fallback_reports_its_actual_input_mode(monkeypatch):
    from deepdesk.plugins.builtin import windows_ui

    def unsupported():
        raise RuntimeError("No Invoke pattern")
    clicks = []
    control = SimpleNamespace(window_text=lambda: "QA button", is_enabled=lambda: True,
                              is_visible=lambda: True, invoke=unsupported,
                              rectangle=lambda: SimpleNamespace(left=10, top=10, right=30, bottom=30),
                              element_info=SimpleNamespace(control_type="Button", automation_id="qa"))
    win = SimpleNamespace(window_text=lambda: "QA", descendants=lambda **kw: [control],
                          set_focus=lambda: None, handle=123, process_id=lambda: 1)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **kw: SimpleNamespace(windows=lambda: [win])))
    monkeypatch.setattr(windows_ui, "foreground_input_scope", contextlib.nullcontext)
    monkeypatch.setattr(windows_ui, "click_at", lambda x, y: clicks.append((x, y)))
    result = windows_ui.WindowsUITool()._execute_sync({"action": "click", "window": "QA",
                                                       "control": "QA button", "allow_foreground": True})
    assert result["foreground_used"] is True
    assert clicks == [(20, 20)]
