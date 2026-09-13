from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from macos import bundle_cli

MACHO = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"fixture" * 4


def fixtures(tmp_path, monkeypatch, executable=MACHO):
    cache = tmp_path / "downloads"
    cache.mkdir()
    for name in bundle_cli.ASSETS:
        path = cache / name
        if name == "jq-macos-arm64":
            path.write_bytes(executable)
        elif name.endswith(".zip"):
            with zipfile.ZipFile(path, "w") as source:
                source.writestr("gh_2.97.0_macOS_arm64/bin/gh", executable)
                source.writestr("gh_2.97.0_macOS_arm64/LICENSE", "GitHub license")
        else:
            entries = {"jq-1.8.2/COPYING": b"jq license"} if name.startswith("jq") else {
                "ripgrep/rg": executable, "ripgrep/COPYING": b"license",
                "ripgrep/LICENSE-MIT": b"MIT license", "ripgrep/UNLICENSE": b"Unlicense",
            }
            with tarfile.open(path, "w:gz") as source:
                for member, data in entries.items():
                    info = tarfile.TarInfo(member)
                    info.size = len(data)
                    source.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(bundle_cli, "ASSETS", {
        name: (url, hashlib.sha256((cache / name).read_bytes()).hexdigest())
        for name, (url, _) in bundle_cli.ASSETS.items()
    })
    return cache


def test_cli_bundle_is_complete_with_licenses_and_does_not_overwrite(tmp_path, monkeypatch):
    cache = fixtures(tmp_path, monkeypatch)
    target = bundle_cli.install(cache, tmp_path / "native")
    for tool in ("jq", "rg", "gh"):
        assert (target / "bin" / tool).read_bytes() == MACHO
    assert len(list((target / "licenses").rglob("*LICENSE*"))) == 3
    assert (target / "licenses/jq/COPYING").read_text() == "jq license"
    assert json.loads((target / "UPSTREAM.json").read_text())["versions"] == bundle_cli.VERSIONS
    with pytest.raises(FileExistsError):
        bundle_cli.install(cache, tmp_path / "native")


def test_cli_bundle_wrong_hash_writes_nothing(tmp_path, monkeypatch):
    cache = fixtures(tmp_path, monkeypatch)
    (cache / "jq-macos-arm64").write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA-256"):
        bundle_cli.install(cache, tmp_path / "native")
    assert not (tmp_path / "native").exists()


def test_cli_bundle_rejects_wrong_architecture(tmp_path, monkeypatch):
    cache = fixtures(tmp_path, monkeypatch, executable=b"x86 or script")
    with pytest.raises(ValueError, match="arm64 Mach-O"):
        bundle_cli.install(cache, tmp_path / "native")
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "root\\escape"])
@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_cli_archive_rejects_unsafe_paths(tmp_path, name, kind):
    path = tmp_path / ("archive.zip" if kind == "zip" else "archive.tar.gz")
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as source:
            source.writestr(name, b"bad")
    else:
        with tarfile.open(path, "w:gz") as source:
            source.addfile(tarfile.TarInfo(name))
    # Windows zipfile normalizes backslashes while constructing ZipInfo; this
    # case remains rejected because no requested regular file exists.
    with pytest.raises(ValueError, match="Unsafe archive|Missing, ambiguous"):
        bundle_cli.member_bytes(path, "rg")


def test_cli_path_validation_rejects_backslashes_before_extraction():
    with pytest.raises(ValueError, match="Unsafe archive"):
        bundle_cli.safe_member("root\\escape")


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_cli_archive_rejects_selected_symlink(tmp_path, kind):
    path = tmp_path / ("archive.zip" if kind == "zip" else "archive.tar.gz")
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as source:
            info = zipfile.ZipInfo("bin/rg")
            info.external_attr = 0o120777 << 16
            source.writestr(info, "elsewhere")
    else:
        with tarfile.open(path, "w:gz") as source:
            info = tarfile.TarInfo("bin/rg")
            info.type = tarfile.SYMTYPE
            info.linkname = "elsewhere"
            source.addfile(info)
    with pytest.raises(ValueError, match="non-regular"):
        bundle_cli.member_bytes(path, "rg")


def test_cli_fetch_verified_cache_is_offline(tmp_path, monkeypatch):
    cache = fixtures(tmp_path, monkeypatch)
    def no_network(*args, **kwargs):
        pytest.fail("verified cache must not redownload")
    monkeypatch.setattr(bundle_cli.urllib.request, "urlopen", no_network)
    bundle_cli.fetch(cache)


def test_clipboard_description_and_summary_are_cross_platform():
    from deepdesk.plugins.builtin.clipboard import ClipboardTool
    tool = ClipboardTool()
    assert "macOS" in tool.description
    for action in ("read", "write", "clear"):
        assert "Windows" not in tool.summarize({"action": action})
