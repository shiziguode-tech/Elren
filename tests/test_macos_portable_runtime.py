from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from deepdesk.tool_runtime import discover_tool_runtime, packaged_workspace_root
from macos import bundle_python, runtime_manifest


def archive_at(tmp_path, monkeypatch, names):
    archive = tmp_path / "python.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name in names:
            item = tarfile.TarInfo(name)
            item.size = 4
            stream.addfile(item, io.BytesIO(b"test"))
    monkeypatch.setattr(bundle_python, "SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    return archive


def test_portable_python_rejects_hash_before_writing(tmp_path):
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"untrusted")
    with pytest.raises(ValueError, match="SHA-256"):
        bundle_python.install(archive, tmp_path / "native")
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize("name", ["../escape", "/python/escape", "other/bin/python", "python/../../escape"])
def test_portable_python_rejects_archive_escape(tmp_path, monkeypatch, name):
    archive = archive_at(tmp_path, monkeypatch, ["python/valid", name])
    with pytest.raises(ValueError, match="escapes"):
        bundle_python.install(archive, tmp_path / "native")
    assert not (tmp_path / "native").exists()


def test_portable_python_preserves_license_and_refuses_overwrite(tmp_path, monkeypatch):
    archive = archive_at(tmp_path, monkeypatch, [
        "python/bin/python3.12", "python/lib/libpython3.12.dylib", "python/lib/python3.12/LICENSE.txt",
    ])
    target = bundle_python.install(archive, tmp_path / "native")
    assert (target / "lib/python3.12/LICENSE.txt").read_bytes() == b"test"
    assert json.loads((target / "ELREN-UPSTREAM.json").read_text())["version"] == "3.12.14"
    with pytest.raises(FileExistsError):
        bundle_python.install(archive, tmp_path / "native")


def test_frozen_mac_resolves_signed_bundle_not_mutable_user_workspace(tmp_path, monkeypatch):
    backend = tmp_path / "Elren.app/Contents/Resources/backend/ElrenBackend"
    backend.parent.mkdir(parents=True)
    backend.touch()
    monkeypatch.setattr("deepdesk.tool_runtime.sys.platform", "darwin")
    monkeypatch.setattr("deepdesk.tool_runtime.sys.frozen", True, raising=False)
    monkeypatch.setattr("deepdesk.tool_runtime.sys.executable", str(backend))
    assert packaged_workspace_root(tmp_path / "user-data") == backend.parent.parent / "bundle"


def test_manifest_requires_actual_commands_and_matches_final_bytes(tmp_path):
    bundle = tmp_path / "bundle"
    root = bundle / "work/tool-runtime"
    root.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        runtime_manifest.write_manifests(bundle)
    assert not (root / "manifest.json").exists()
    for relative in ["python/bin/python3.12", "node/bin/node", "node/bin/npm", "node/bin/npx",
                     "node/openclaw", "lilypond/lilypond-2.26.0/bin/lilypond", "audiveris/runtime/bin/java",
                     "cli/bin/jq", "cli/bin/rg", "cli/bin/gh", "git/bin/git",
                     "git/libexec/git-core/git-lfs", "git/libexec/git-core/git-credential-manager"]:
        path = root / "native" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"signed test bytes")
    runtime_manifest.write_manifests(bundle)
    runtime = discover_tool_runtime(bundle, verify_hashes=True)
    assert runtime.status()["available_tool_count"] == 13
    assert len(runtime.path_entries) == 5


def test_macos_build_and_launcher_use_same_private_runtime_paths():
    root = Path(__file__).parents[1]
    swift = (root / "macos/ElrenMac.swift").read_text("utf-8")
    build = (root / "macos/build-macos-app.sh").read_text("utf-8")
    assert 'appendingPathComponent("node/openclaw")' in swift
    assert 'let runtimePaths = ["python/bin", "node/bin", "node", "cli/bin", "git/bin"]' in swift
    assert 'environment["PATH"]' in swift
    assert 'environment["PYTHONDONTWRITEBYTECODE"] = "1"' in swift
    assert 'environment["PIP_REQUIRE_VIRTUALENV"] = "true"' in swift
    assert "bundle_python.py" in build and "runtime_manifest.py" in build
    assert build.index("bundle_cli.py") < build.index("SIGN_ARGS=")
    assert build.index("bundle_git.py") < build.index("SIGN_ARGS=")
    assert '"${executable:t}" == "git-credential-manager"' in build
    assert "npm-cli.js" in build and "npx-cli.js" in build
    assert 'NPM_SOURCE="$(npm root -g)' not in build
    assert build.index("Use an official Node distribution") < build.index('mkdir -p "$CONTENTS/MacOS"')
    assert build.index("runtime_manifest.py") < build.index("codesign --verify --deep --strict")


def test_mac_checkbox_does_not_use_text_field_height():
    root = Path(__file__).parents[1]
    css = (root / "deepdesk/static/editorial-ui.css").read_text("utf-8")
    block = css.split('.form-grid .settings-checkbox input[type="checkbox"] {', 1)[1].split("}", 1)[0]
    assert "min-height: 16px" in block and "padding: 0" in block


@pytest.mark.parametrize("payload", [[], {"schema": 1, "bundle": "elren-portable-runtime", "components": [1]},
    {"schema": 1, "bundle": "elren-portable-runtime", "components": {"python": [1]}},
    {"schema": 1, "bundle": "elren-portable-runtime", "components": {"python": {"executable": "../outside"}}}])
def test_runtime_status_handles_malformed_manifest_without_crashing(tmp_path, payload):
    from deepdesk.main import portable_runtime_status

    (tmp_path / "work").mkdir()
    (tmp_path / "work/runtime-bundle.json").write_text(json.dumps(payload), encoding="utf-8")
    result = portable_runtime_status(tmp_path)
    assert not result["ready"]
    assert result["mode"] == "invalid-manifest"
