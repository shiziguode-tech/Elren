from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deepdesk.runtime_settings import RuntimeSettings

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "deepdesk" / "static"


def _run_theme_init(stored_mode: str, stored_accent: str, system_dark: bool):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the theme bootstrap regression")
    theme_path = json.dumps(str(STATIC / "theme-init.js"))
    initial = json.dumps(
        {
            "elren.theme-mode.v1": stored_mode,
            "elren.accent-color.v1": stored_accent,
        }
    )
    script = f"""
const fs = require('fs');
const values = {initial};
const props = {{}};
global.document = {{documentElement: {{dataset: {{}}, style: {{setProperty: (key, value) => props[key] = value}}}}}};
global.window = {{
  localStorage: {{getItem: key => values[key] ?? null, setItem: (key, value) => values[key] = value}},
  matchMedia: () => ({{matches: {str(system_dark).lower()}, addEventListener: () => {{}}}}),
}};
eval(fs.readFileSync({theme_path}, 'utf8'));
const initialDataset = {{...document.documentElement.dataset}};
const initialProps = {{...props}};
process.stdout.write(JSON.stringify({{
  dataset: initialDataset,
  props: initialProps,
  read: window.ElrenTheme.read(),
  saved: window.ElrenTheme.save('light', '#8B5CF6'),
  values,
}}));
"""
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def test_theme_bootstrap_ignores_retired_visual_preferences() -> None:
    result = _run_theme_init("system", "#4F7CFF", system_dark=True)
    assert result["dataset"] == {"themeMode": "light", "theme": "light"}
    assert result["props"]["--accent"] == "#a6533f"
    assert result["read"] == {"mode": "light", "accent": "#a6533f"}


def test_theme_save_keeps_the_single_editorial_theme() -> None:
    result = _run_theme_init("dark", "invalid", system_dark=False)
    assert result["saved"] == {
        "mode": "light",
        "accent": "#a6533f",
        "resolvedMode": "light",
    }
    assert result["values"]["elren.theme-mode.v1"] == "light"
    assert result["values"]["elren.accent-color.v1"] == "#a6533f"


def test_retired_accent_values_cannot_override_the_brand_palette() -> None:
    result = _run_theme_init("dark", "#000000", system_dark=True)
    assert result["props"]["--accent"] == "#a6533f"
    assert result["props"]["--accent-contrast"] == "#ffffff"
    assert result["props"]["--accent-visible"] == "#a6533f"
    assert result["read"] == {"mode": "light", "accent": "#a6533f"}


def test_first_run_defaults_to_the_warm_editorial_theme() -> None:
    settings = RuntimeSettings()
    assert settings.theme_mode == "light"
    assert settings.theme_accent == "#a6533f"

    result = _run_theme_init("", "", system_dark=True)
    assert result["dataset"] == {"themeMode": "light", "theme": "light"}
    assert result["props"]["--accent"] == "#a6533f"
    assert result["props"]["--accent-visible"] == "#a6533f"
    assert result["read"] == {"mode": "light", "accent": "#a6533f"}


def test_default_light_canvas_matches_the_white_reference() -> None:
    css = (STATIC / "theme.css").read_text("utf-8")
    light_tokens = css.split(':root[data-theme="light"] body {', 1)[1].split("}", 1)[0]
    assert "--bg:#ffffff;" in light_tokens
    assert "--panel:#ffffff;" in light_tokens
    assert ':root[data-theme="light"] body main { background:var(--bg); }' in css
    assert ':root[data-theme="light"] body aside { background:var(--bg);' in css


def test_theme_controls_and_remote_visual_mutation_are_retired() -> None:
    app = (STATIC / "app.js").read_text("utf-8")
    index = (STATIC / "index.html").read_text("utf-8")
    remote = (ROOT / "deepdesk" / "plugins" / "builtin" / "remote_settings.py").read_text("utf-8")
    for token in ("settingThemeMode", "settingAccentCustom", "accent-swatch", "theme-setting"):
        assert token not in index
        assert token not in app
    assert 'data-settings-target="appearance"' not in index
    assert '"theme_mode":' not in remote
    assert '"theme_accent":' not in remote


def test_light_theme_is_complete_without_a_private_theme_branch() -> None:
    css = (STATIC / "theme.css").read_text("utf-8")
    required_surfaces = [
        "aside", "main", "header", "footer", ".event-card", ".workspace-dialog",
        ".capability-dialog", ".human-action", ".human-problem-dialog",
        ".composer", ".terminal-event",
    ]
    for surface in required_surfaces:
        assert f':root[data-theme="light"] body {surface}' in css
    assert "private-chat" not in css


def test_theme_assets_are_loaded_in_flicker_safe_order_with_cache_versions() -> None:
    index = (STATIC / "index.html").read_text("utf-8")
    bootstrap = '<script src="/static/theme-init.js?v=10"></script>'
    base_css = '<link rel="stylesheet" href="/static/style.css?v=4" />'
    composer_css = '<link rel="stylesheet" href="/static/composer-controls.css?v=2" />'
    theme_css = '<link rel="stylesheet" href="/static/theme.css?v=19" />'
    app = '<script src="/static/app.js?v=264"></script>'
    assert index.index(bootstrap) < index.index(base_css)
    assert index.index(composer_css) < index.index(theme_css) < index.index(app)
    assert 'id="settingThemeMode"' not in index
    assert 'id="settingAccentCustom"' not in index
    assert 'data-settings-target="appearance"' not in index
