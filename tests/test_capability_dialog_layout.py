import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "deepdesk" / "static"


def test_capability_dialog_has_definite_viewport_bounded_height():
    css = (STATIC / "editorial-ui.css").read_text(encoding="utf-8")
    rule = re.search(r"\n\.capability-dialog\s*\{([^}]+)\}", css).group(1)
    assert "height: min(820px, calc(100vh - 40px));" in rule
    assert "height: min(820px, calc(100dvh - 40px));" in rule
    assert "max-height: min(820px, calc(100dvh - 40px));" in rule
    # A max-height alone does not resolve the flex scroll area's basis in WebKit.
    assert re.search(r"(?m)^\s+height:", rule)


def test_capability_body_scrolls_without_shrinking_header():
    css = (STATIC / "trace-view.css").read_text(encoding="utf-8")
    assert ".capability-dialog .capability-head { flex: none; }" in css
    body = re.search(r"\.capability-dialog \.capability-content\s*\{([^}]+)\}", css).group(1)
    assert "min-height: 0;" in body
    assert "overflow: auto;" in body
    assert "max-height: none;" in body
