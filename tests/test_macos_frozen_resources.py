from pathlib import Path

import pytest

from macos.check_frozen_resources import REQUIRED, verify


def test_canonical_builder_includes_and_verifies_chat_catalog():
    source = (Path(__file__).parents[1] / "macos/build-macos-app.sh").read_text("utf-8")
    assert '--add-data "$ROOT/deepdesk/subagent_catalog.json:deepdesk"' in source
    assert 'python "$ROOT/macos/check_frozen_resources.py" "$RESOURCES/backend"' in source


@pytest.mark.parametrize("defect", ["missing", "stale", "none"])
def test_packaging_checks_actual_resource_bytes(tmp_path, defect):
    source, backend = tmp_path / "source", tmp_path / "backend"
    for relative in REQUIRED:
        original = source / "deepdesk" / relative
        bundled = backend / "_internal/deepdesk" / relative
        original.parent.mkdir(parents=True, exist_ok=True)
        bundled.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"fixture")
        if defect == "missing" and relative == "subagent_catalog.json":
            continue
        bundled.write_bytes(b"stale" if defect == "stale" else b"fixture")
    if defect == "none":
        verify(backend, source)
    else:
        with pytest.raises(ValueError, match="subagent_catalog.json"):
            verify(backend, source)
