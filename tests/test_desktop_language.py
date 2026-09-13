import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "deepdesk/static/app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("saved,query,expected", [
    ("en", "", "en"), ("zh", "", "zh"),
    ("zh", "?lang=en", "en"), ("en", "?lang=zh", "zh"),
])
def test_language_bridge_syncs_initial_preference_and_live_switch(saved, query, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    bootstrap = APP[APP.index("const LANGUAGE_STORAGE_KEY"):APP.index("function sidebarWidthBounds")]
    script = f"""
const messages = [];
global.window = {{
  location: {{search: {json.dumps(query)}}},
  localStorage: {{getItem: () => {json.dumps(saved)}}},
  chrome: {{webview: {{postMessage: message => messages.push(message)}}}},
}};
{bootstrap}
syncDesktopLanguage('en');
syncDesktopLanguage('zh');
syncDesktopLanguage('fr');
syncDesktopLanguage({{language: 'en'}});
delete window.chrome;
syncDesktopLanguage('en');
window.chrome = {{webview: {{postMessage: () => {{throw Error('bridge unavailable');}}}}}};
syncDesktopLanguage('zh');
console.log(JSON.stringify(messages));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == [f"elren:ui-language:{expected}", "elren:ui-language:en", "elren:ui-language:zh"]


def test_language_toggle_updates_tray_before_reload():
    toggle = APP.split('$("#languageToggle").onclick =', 1)[1].split('window.addEventListener("beforeunload"', 1)[0]
    assert toggle.index("syncDesktopLanguage(nextLanguage)") < toggle.index("window.location.assign")
    assert toggle.index("if (!confirmed) return") < toggle.index("syncDesktopLanguage(nextLanguage)")


@pytest.mark.parametrize("bridge_throws", [False, True])
def test_macos_language_bridge_sends_only_valid_locale_and_falls_back(bridge_throws):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    bootstrap = APP[APP.index("const LANGUAGE_STORAGE_KEY"):APP.index("function sidebarWidthBounds")]
    script = f"""
const mac = [], fallback = [];
global.window = {{
  location: {{search: '?lang=en'}}, localStorage: {{getItem: () => null}},
  webkit: {{messageHandlers: {{elrenLanguage: {{postMessage: value => {{
    if ({str(bridge_throws).lower()}) throw Error('unavailable');
    mac.push(value);
  }}}}}}}},
  fetch: (url, options) => {{fallback.push([url, JSON.parse(options.body)]); return Promise.resolve();}},
}};
{bootstrap}
syncDesktopLanguage('zh');
syncDesktopLanguage('fr');
syncDesktopLanguage({{language:'en'}});
console.log(JSON.stringify({{mac, fallback}}));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
    actual = json.loads(result.stdout)
    if bridge_throws:
        assert actual == {"mac": [], "fallback": [["/api/desktop/language", {"language": "en"}],
                                                  ["/api/desktop/language", {"language": "zh"}]]}
    else:
        assert actual == {"mac": ["en", "zh"], "fallback": []}


def test_compatibility_browser_uses_only_locale_endpoint():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    bootstrap = APP[APP.index("const LANGUAGE_STORAGE_KEY"):APP.index("function sidebarWidthBounds")]
    script = f"""
const calls = [];
global.window = {{
  location: {{search: '?lang=en'}},
  localStorage: {{getItem: () => null}},
  fetch: (url, options) => {{calls.push([url, options]); return Promise.resolve({{ok:true}});}},
}};
{bootstrap}
syncDesktopLanguage('zh');
console.log(JSON.stringify(calls));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
    calls = json.loads(result.stdout)
    assert [json.loads(options["body"]) for _, options in calls] == [{"language": "en"}, {"language": "zh"}]
    for url, options in calls:
        assert url == "/api/desktop/language"
        assert options["method"] == "PUT"
        assert options["keepalive"] is True
