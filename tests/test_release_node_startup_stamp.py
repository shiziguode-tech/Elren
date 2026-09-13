import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from launcher.build_release_archive import (
    NODE_DEPENDENCY_STAMP,
    build,
    portable_node_dependency_stamp,
)


def fixture_bundle(root: Path) -> Path:
    runtime = root / "work/openclaw-runtime"
    files = {
        "work/runtime-bundle.json": json.dumps({"schema": 1, "bundle": "elren-portable-runtime",
            "components": {"node": {"version": "24.18.1"}, "openclaw": {"version": "2026.7.1-2"}}}),
        "work/node-runtime/node.exe": "synthetic-test-executable",
        "work/openclaw-runtime/package.json": json.dumps({"dependencies": {"openclaw": "2026.7.1-2"}}),
        "work/openclaw-runtime/pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "work/openclaw-runtime/node_modules/openclaw/package.json": '{"version":"2026.7.1-2"}',
        "work/openclaw-runtime/node_modules/openclaw/openclaw.mjs": "// fixture",
        "work/openclaw-runtime/node_modules/openclaw/skills/demo/SKILL.md": "fixture",
        "work/openclaw-runtime/node_modules/@modelcontextprotocol/sdk/package.json": "{}",
        "work/openclaw-runtime/node_modules/.modules.yaml": '{"nodeLinker": "hoisted"}',
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return runtime


@pytest.mark.parametrize("old_stamp", [None, "24.18.1|2026.7.1-2|STALE|STALE"])
def test_archive_refreshes_node_stamp_from_exact_payload(tmp_path, old_stamp):
    root = tmp_path / "package"
    runtime = fixture_bundle(root)
    stamp = root / Path(*NODE_DEPENDENCY_STAMP)
    if old_stamp is not None:
        stamp.write_text(old_stamp, encoding="utf-8")
    expected = "|".join(["24.18.1", "2026.7.1-2", *[
        hashlib.sha256((runtime / name).read_bytes()).hexdigest().upper()
        for name in ("package.json", "pnpm-lock.yaml")
    ]]).encode()
    archive_path = tmp_path / "release.zip"
    build(root, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("package/" + Path(*NODE_DEPENDENCY_STAMP).as_posix()) == expected
    assert (stamp.read_text("utf-8") if stamp.exists() else None) == old_stamp


@pytest.mark.parametrize("bad_file,contents", [
    ("node_modules/openclaw/package.json", '{"version":"wrong"}'),
    ("node_modules/.modules.yaml", "nodeLinker: isolated"),
    ("package.json", "invalid json"),
    ("node_modules/openclaw/openclaw.mjs", None),
    ("node_modules/@modelcontextprotocol/sdk/package.json", None),
])
def test_incomplete_or_wrong_bundle_never_gets_fresh_stamp(tmp_path, bad_file, contents):
    runtime = fixture_bundle(tmp_path)
    path = runtime / bad_file
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    assert portable_node_dependency_stamp(tmp_path) is None


def test_lockfile_change_changes_stamp(tmp_path):
    runtime = fixture_bundle(tmp_path)
    before = portable_node_dependency_stamp(tmp_path)
    (runtime / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n# updated\n", encoding="utf-8")
    assert portable_node_dependency_stamp(tmp_path) != before


@pytest.mark.parametrize("outside", [False, True])
def test_source_pnpm_links_use_existing_archive_boundary_checks(tmp_path, outside):
    root = tmp_path / "source"
    runtime = fixture_bundle(root)
    before = portable_node_dependency_stamp(root)
    original = runtime / "node_modules/openclaw"
    target = (tmp_path / "external-openclaw" if outside else
              runtime / "node_modules/.pnpm/openclaw@2026.7.1-2/node_modules/openclaw")
    target.parent.mkdir(parents=True, exist_ok=True)
    original.rename(target)
    try:
        original.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation unavailable on this host")
    if outside:
        with pytest.raises(ValueError, match="inside OpenClaw node_modules"):
            portable_node_dependency_stamp(root)
    else:
        assert portable_node_dependency_stamp(root) == before
