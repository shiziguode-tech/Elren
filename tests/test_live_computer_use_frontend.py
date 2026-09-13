from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "deepdesk" / "static"


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening = source.index(") {", start) + 2
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Could not extract JavaScript function {name}")


def _async_function_source(source: str, name: str) -> str:
    start = source.index(f"async function {name}(")
    opening = source.index(") {", start) + 2
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"Could not extract async JavaScript function {name}")


def test_live_computer_use_has_an_independent_accessible_top_layer_status() -> None:
    index = (STATIC / "index.html").read_text("utf-8")
    assert 'id="desktopControlStatus"' in index
    assert 'id="desktopControlStatusText" role="status" aria-live="polite" aria-atomic="true"' in index
    assert 'role="group" aria-labelledby="desktopControlStatusText"' in index
    assert 'popover="manual"' in index
    assert 'id="desktopControlStop" type="button"' in index
    assert 'app-symbol-pointer' in index
    assert 'app-symbol-stop' in index


def test_live_computer_use_polling_and_escape_are_bounded_and_idempotent() -> None:
    source = (STATIC / "app.js").read_text("utf-8")
    assert '"/api/desktop-control/status"' in source
    assert '"/api/desktop-control/stop"' in source
    assert "if (desktopControlPollPending) return;" in source
    assert "if (!desktopControlActive || desktopControlStopPending) return false;" in source
    assert 'event.key !== "Escape" || event.repeat' in source
    assert "event.stopImmediatePropagation();" in source
    assert "desktopControlFailureCount += 1;" in source
    assert "showToast" not in source[source.index("async function pollDesktopControlStatus"):source.index("async function stopDesktopControl")]
    assert "session_token" not in source


def test_live_computer_use_is_distinct_from_legacy_computer_tools() -> None:
    source = (STATIC / "app.js").read_text("utf-8")
    assert 'computer_use: "Computer Use"' in source
    assert 'live_computer_use: "Live Computer Use"' in source
    assert 'computer_use: "电脑操作"' in source
    assert 'live_computer_use: "实时电脑控制"' in source


def test_live_computer_use_status_never_shifts_the_layout_at_any_width() -> None:
    css = (STATIC / "professional-ui.css").read_text("utf-8")
    assert ".desktop-control-status[popover]" in css
    assert "position:fixed;" in css
    assert "z-index:2147483000;" in css
    assert "max-width:calc(100vw - 28px);" in css
    assert "@media (max-width:520px)" in css
    assert "width:calc(100vw - 20px);" in css
    assert ".desktop-control-status[hidden] { display:none!important; }" in css
    assert "@media (prefers-reduced-motion:reduce)" in css


def test_live_computer_use_icons_are_repo_native_lucide_assets() -> None:
    for name in ("mouse-pointer-2.svg", "square.svg"):
        asset = (STATIC / "icons" / name).read_text("utf-8")
        assert asset.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
        assert "<text" not in asset
    assert (STATIC / "icons" / "LICENSE-lucide.txt").is_file()


def test_live_computer_use_normalizes_128_hostile_state_payloads() -> None:
    """Exercise truthiness, missing fields and untrusted metadata at scale."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend state stress matrix")
    source = (STATIC / "app.js").read_text("utf-8")
    normalizer = _function_source(source, "normalizeDesktopControlStatus")
    payloads = []
    for index in range(128):
        payloads.append(
            {
                "ok": index % 7 != 0,
                "active": True if index % 2 else ("true" if index % 4 == 0 else False),
                "session_id": f"session-{index}" if index % 3 else index,
                "task_id": f"task-{index}" if index % 5 else ["invalid"],
                "started_at": "2026-08-20T12:00:00Z" if index % 6 else None,
                "expires_at": "2026-08-20T12:01:00Z" if index % 8 else {},
                "stop_reason": "esc" if index % 11 == 0 else "",
                "session_token": f"must-not-surface-{index}",
            }
        )
    script = (
        normalizer
        + "\nconst payloads = " + json.dumps(payloads) + ";"
        + "\nprocess.stdout.write(JSON.stringify(payloads.map(normalizeDesktopControlStatus)));"
    )
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    normalized = json.loads(result.stdout)
    assert len(normalized) == 128
    for index, state in enumerate(normalized):
        assert state["active"] is (index % 2 == 1)
        assert state["sessionId"] == (f"session-{index}" if index % 3 else "")
        assert state["taskId"] == (f"task-{index}" if index % 5 else "")
        assert "session_token" not in state


def test_live_computer_use_survives_disconnect_task_switch_and_visibility_changes() -> None:
    source = (STATIC / "app.js").read_text("utf-8")
    poll = source[source.index("async function pollDesktopControlStatus"):source.index("async function stopDesktopControl")]
    visibility = source[source.index('document.addEventListener("visibilitychange"'):]
    assert "renderDesktopControlStatus" not in poll.split("} catch {", 1)[1].split("} finally", 1)[0]
    assert "desktopControlFailureCount += 1" in poll
    assert 'currentTaskSnapshot?.status' in source[source.index("function desktopControlPollDelay"):source.index("function scheduleDesktopControlPoll")]
    assert "scheduleDesktopControlPoll(15000)" in visibility
    assert "scheduleDesktopControlPoll(0)" in visibility
    assert 'window.addEventListener("focus", () => scheduleDesktopControlPoll(0))' in source


def test_live_computer_use_stop_waits_for_explicit_backend_confirmation() -> None:
    """The emergency banner must not disappear during a lost or slow POST."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the desktop-control stop race test")
    source = (STATIC / "app.js").read_text("utf-8")
    script = "\n".join(
        [
            "let desktopControlActive = false;",
            "let desktopControlStopPending = false;",
            "let desktopControlSnapshot = null;",
            "let resolver = null;",
            "let rejecter = null;",
            "let toasts = [];",
            "let polls = [];",
            "const text = { textContent: '' };",
            "const stop = { disabled: false, title: '', attrs: {},",
            "  setAttribute(name, value) { this.attrs[name] = String(value); },",
            "  toggleAttribute(name, force) { if (name === 'disabled') this.disabled = Boolean(force); },",
            "  removeAttribute(name) { if (name === 'disabled') this.disabled = false; delete this.attrs[name]; },",
            "};",
            "const banner = { hidden: true, popoverOpen: false, attrs: {},",
            "  toggleAttribute(name, force) { if (name === 'hidden') this.hidden = Boolean(force); },",
            "  setAttribute(name, value) { this.attrs[name] = String(value); },",
            "  matches(value) { return value === ':popover-open' && this.popoverOpen; },",
            "  showPopover() { this.popoverOpen = true; },",
            "  hidePopover() { this.popoverOpen = false; },",
            "};",
            "const $ = (selector) => selector === '#desktopControlStatusText' ? text : (selector === '#desktopControlStop' ? stop : banner);",
            "const uiText = (_zh, en) => en;",
            "const showToast = (message, kind) => toasts.push({ message, kind });",
            "const scheduleDesktopControlPoll = (delay) => polls.push(delay);",
            "const requestDesktopControl = () => new Promise((resolve, reject) => { resolver = resolve; rejecter = reject; });",
            _function_source(source, "normalizeDesktopControlStatus"),
            _function_source(source, "updateDesktopControlCopy"),
            _function_source(source, "renderDesktopControlStatus"),
            _async_function_source(source, "stopDesktopControl"),
            "const snapshot = () => ({ active: desktopControlActive, pending: desktopControlStopPending, hidden: banner.hidden, disabled: stop.disabled, text: text.textContent, toasts: [...toasts], polls: [...polls] });",
            "const reset = () => { desktopControlActive = true; desktopControlStopPending = false; desktopControlSnapshot = { ok: true, active: true, sessionId: 'session' }; banner.hidden = false; banner.popoverOpen = true; stop.disabled = false; text.textContent = ''; toasts = []; polls = []; };",
            "(async () => {",
            "  reset();",
            "  const failedPromise = stopDesktopControl();",
            "  await Promise.resolve();",
            "  const failurePending = snapshot();",
            "  rejecter(new Error('timeout'));",
            "  const failureResult = await failedPromise;",
            "  const failureSettled = snapshot();",
            "  reset();",
            "  const successPromise = stopDesktopControl();",
            "  await Promise.resolve();",
            "  const successPending = snapshot();",
            "  resolver({ ok: true, stopped: true, status: { ok: true, active: false, stop_reason: 'web_emergency_stop' } });",
            "  const successResult = await successPromise;",
            "  const successSettled = snapshot();",
            "  reset();",
            "  const ambiguousPromise = stopDesktopControl();",
            "  await Promise.resolve();",
            "  resolver({ ok: true });",
            "  await ambiguousPromise;",
            "  const ambiguousSettled = snapshot();",
            "  process.stdout.write(JSON.stringify({ failurePending, failureResult, failureSettled, successPending, successResult, successSettled, ambiguousSettled }));",
            "})().catch((error) => { console.error(error); process.exit(1); });",
        ]
    )
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    states = json.loads(result.stdout)

    for pending in (states["failurePending"], states["successPending"]):
        assert pending["active"] is True
        assert pending["hidden"] is False
        assert pending["disabled"] is True
        assert pending["text"] == "Stopping…"

    assert states["failureResult"] is False
    assert states["failureSettled"]["active"] is True
    assert states["failureSettled"]["hidden"] is False
    assert states["failureSettled"]["disabled"] is False
    assert states["failureSettled"]["text"].startswith("Controlling your computer")
    assert len(states["failureSettled"]["toasts"]) == 1
    assert states["failureSettled"]["toasts"][0]["kind"] == "error"
    assert "Active status was restored" in states["failureSettled"]["toasts"][0]["message"]

    assert states["successResult"] is True
    assert states["successSettled"]["active"] is False
    assert states["successSettled"]["hidden"] is True
    assert states["successSettled"]["disabled"] is False
    assert states["successSettled"]["toasts"] == []

    assert states["ambiguousSettled"]["active"] is True
    assert states["ambiguousSettled"]["hidden"] is False
    assert states["ambiguousSettled"]["disabled"] is False
    assert states["ambiguousSettled"]["polls"] == [0]
    assert len(states["ambiguousSettled"]["toasts"]) == 1
    assert states["ambiguousSettled"]["toasts"][0]["kind"] == "error"
