from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "deepdesk" / "static" / "app.js"


def test_task_poll_does_not_force_the_timeline_to_the_bottom() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    poll_body = source.split("async function poll(", 1)[1].split("async function decide(", 1)[0]

    assert "const timelineScrollState = captureTimelineScrollState();" in poll_body
    assert "restoreTimelineScrollState(timelineScrollState);" in poll_body
    assert "scrollTop = timeline.scrollHeight" not in poll_body


def test_manual_timeline_scrolling_controls_auto_follow() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "const TIMELINE_BOTTOM_THRESHOLD = 72;" in source
    assert "function timelineIsNearBottom" in source
    assert '$("#timeline")?.addEventListener("scroll"' in source
    assert "timelineAutoFollow = timelineIsNearBottom();" in source
    assert "timelineAutoFollow = false;" in source


def test_new_or_opened_task_starts_at_the_latest_event() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    open_task_body = source.split("async function openTask(", 1)[1].split("function closeNavigation(", 1)[0]
    start_body = source.split("async function start(", 1)[1].split("async function poll(", 1)[0]

    expected = "restoreTimelineScrollState({ follow: true }, { forceFollow: true });"
    assert expected in open_task_body
    assert expected in start_body
