from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from launcher.build_macos_source_archive import iter_macos_source_paths
from launcher.build_release_archive import PUBLIC_ALLOWED_ROOT_ENTRIES
from macos import provision_ocr_models

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "ACKNOWLEDGEMENTS.md", "LICENSE_PENDING.md")


def test_source_archive_retains_referenced_license_and_notices(tmp_path):
    for name in (*NOTICES, "SECURITY.md", "CONTRIBUTING.md"):
        (tmp_path / name).write_text("notice", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text("utf-8"), "utf-8")
    (tmp_path / "private-notes.txt").write_text("private", "utf-8")
    paths = {relative.as_posix() for _, relative in iter_macos_source_paths(tmp_path)}
    metadata = tomllib.loads((tmp_path / "pyproject.toml").read_text("utf-8"))
    assert metadata["project"]["license"]["file"] in paths
    assert set(NOTICES) <= paths
    assert "private-notes.txt" not in paths
    assert all(name.lower() in PUBLIC_ALLOWED_ROOT_ENTRIES for name in NOTICES)


def test_native_macos_copies_top_level_notices():
    script = (ROOT / "macos/build-macos-app.sh").read_text("utf-8")
    assert "for NOTICE in " + " ".join(NOTICES) in script
    assert 'cp "$ROOT/$NOTICE" "$RESOURCES/bundle/$NOTICE"' in script


def test_cloud_build_provisions_before_native_build():
    workflow = (ROOT / ".github/workflows/build-macos.yml").read_text("utf-8")
    assert workflow.index("Provision pinned Audiveris") < workflow.index("- name: Build Elren")
    assert "17491af8b6d40153b031dd1f0815e37213f0999ef23f10586af46706e59b2eb6" in workflow
    assert "-readonly -nobrowse" in workflow
    assert "--check-only" in workflow
    assert "provision_ocr_models.py --download-to" in workflow
    assert "pull_request_target" not in workflow


def test_model_cache_is_verified_before_any_install(tmp_path, monkeypatch):
    cache, package = tmp_path / "cache", tmp_path / "package"
    (cache / "models").mkdir(parents=True)
    package.mkdir()
    for name in provision_ocr_models.MODEL_SHA256:
        (cache / "models" / name).write_bytes(b"invalid")
    with pytest.raises(ValueError, match="SHA-256"):
        provision_ocr_models.install(cache, package)
    assert not (package / "models").exists()


def test_model_cache_install_is_offline_and_verifies_result(tmp_path, monkeypatch):
    cache, package = tmp_path / "cache", tmp_path / "package"
    (cache / "models").mkdir(parents=True)
    package.mkdir()
    from macos import check_ocr_models
    hashes = {name: hashlib.sha256(name.encode()).hexdigest() for name in provision_ocr_models.MODEL_SHA256}
    monkeypatch.setattr(check_ocr_models, "MODEL_SHA256", hashes)
    for name in hashes:
        (cache / "models" / name).write_bytes(name.encode())
    def no_network(*args, **kwargs):
        pytest.fail("offline install attempted network access")
    monkeypatch.setattr(provision_ocr_models, "open_model", no_network)
    provision_ocr_models.install(cache, package)
    assert check_ocr_models.model_hashes(package) == hashes


def test_model_download_rejects_http_redirect_before_network():
    handler = provision_ocr_models.HTTPSOnlyRedirect()
    with pytest.raises(ValueError, match="insecure"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/model")


def test_model_download_failure_preserves_cache_and_cleans_temporary(tmp_path, monkeypatch):
    import io

    models = tmp_path / "models"
    models.mkdir()
    name = next(iter(provision_ocr_models.MODEL_URLS))
    target = models / name
    target.write_bytes(b"existing corrupt cache")
    response = io.BytesIO(b"invalid download")
    response.url = "https://example.com/model"
    monkeypatch.setattr(provision_ocr_models, "open_model", lambda url: response)
    with pytest.raises(ValueError, match="checksum mismatch"):
        provision_ocr_models.download(tmp_path)
    assert target.read_bytes() == b"existing corrupt cache"
    assert list(models.iterdir()) == [target]


def test_webview_sdk_notices_survive_public_source_export():
    paths = {relative.as_posix() for _, relative in iter_macos_source_paths(ROOT)}
    for name in ("LICENSE.txt", "NOTICE.txt", "UPSTREAM.md"):
        assert f"launcher/webview2/{name}" in paths


def test_full_lint_gates_exclude_build_environment_not_first_party_tests():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    exclusions = metadata["tool"]["ruff"]["exclude"]
    assert ".ci-venv" in exclusions
    assert "deepdesk/vendor" in exclusions
    assert "tests" not in exclusions
    for name in ("core-ci.yml", "build-macos.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text("utf-8")
        assert "-m ruff check ." in workflow


def test_gradle_notice_matches_committed_distribution_version():
    import re

    properties = (ROOT / "mobile/ElrenMobile/gradle/wrapper/gradle-wrapper.properties").read_text("utf-8")
    version = re.search(r"gradle-([\d.]+)-bin\.zip", properties).group(1)
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text("utf-8")
    assert f"configured for Gradle\n  {version} " in notices
