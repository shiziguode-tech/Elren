"""Regression of caller ownership using real AirDatepicker lifecycle methods."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_frontend_high_priority import APP_JS, _function_source

ROOT = Path(__file__).resolve().parents[1]


def run_case(scenario, transition, language="en"):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = APP_JS.read_text("utf-8")
    functions = "\n".join(_function_source(source, name) for name in (
        "parseScheduleDateTime", "initializeScheduleDatePickers",
    ))
    result = subprocess.run([node, str(ROOT / "tests/fixtures/datepicker_lifecycle.cjs"),
                             str(ROOT), scenario, transition, language], input=functions,
                            capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("transition", ["instant", "animated"])
@pytest.mark.parametrize(("scenario", "calls", "creations"), [
    ("never", 0, 0), ("hidden", 1, 1), ("closing", 1, 1), ("visible", 1, 1),
    ("reopen-during-close", 2, None), ("repeat-reopen", 4, 4),
])
def test_dialog_close_and_scroll_only_hide_visible_picker(scenario, calls, creations, transition):
    result = run_case(scenario, transition)
    assert result["errors"] == []
    assert result["hideCalls"] == [calls, 0]
    assert result["visible"] is False
    assert result["children"] == 0
    if creations is None:
        creations = 1 if transition == "animated" else 2
    assert result["created"] == result["destroyed"] == creations


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("transition", ["instant", "animated"])
def test_time_slider_labels_are_localized_again_after_components_rebuild(language, transition):
    vendor = (ROOT / "deepdesk/static/vendor/air-datepicker/air-datepicker.js").read_text("utf-8")
    assert '<input type="range" name="hours"' in vendor
    assert '<input type="range" name="minutes"' in vendor
    result = run_case("labels", transition, language)
    expected = {"hours": "Hours", "minutes": "Minutes"} if language == "en" else {"hours": "小时", "minutes": "分钟"}
    assert result == {"first": expected, "second": expected, "errors": []}
