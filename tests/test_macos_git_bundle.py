from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from macos import bundle_git

MACHO = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"fixture"


def fixture_cache(tmp_path, monkeypatch, executable=MACHO, extra=None, omit=None):
    cache = tmp_path / "downloads"
    cache.mkdir()
    with tarfile.open(cache / bundle_git.ARCHIVE, "w:gz") as archive:
        for relative in bundle_git.REQUIRED:
            if relative == omit:
                continue
            member = tarfile.TarInfo(relative)
            member.mode = 0o755
            member.size = len(executable)
            archive.addfile(member, io.BytesIO(executable))
        if extra is not None:
            archive.addfile(extra)
    for name in bundle_git.ASSETS:
        if name != bundle_git.ARCHIVE:
            (cache / name).write_text("license", encoding="utf-8")
    monkeypatch.setattr(bundle_git, "ASSETS", {
        name: (url, hashlib.sha256((cache / name).read_bytes()).hexdigest())
        for name, (url, _) in bundle_git.ASSETS.items()
    })
    return cache


def test_git_installs_verified_files_and_licenses_without_overwrite(tmp_path, monkeypatch):
    cache = fixture_cache(tmp_path, monkeypatch)
    target = bundle_git.install(cache, tmp_path / "native")
    assert (target / "bin/git").read_text() == bundle_git.LAUNCHER
    assert (target / "libexec/git-core/git").read_bytes() == MACHO
    assert len(list((target / "licenses").iterdir())) == 3
    assert json.loads((target / "ELREN-UPSTREAM.json").read_text())["version"] == "2.53.0"
    with pytest.raises(FileExistsError):
        bundle_git.install(cache, tmp_path / "native")


def test_git_hash_failure_writes_nothing(tmp_path, monkeypatch):
    cache = fixture_cache(tmp_path, monkeypatch)
    (cache / "git-COPYING").write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA-256"):
        bundle_git.install(cache, tmp_path / "native")
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize("name", ["../escape", "/bin/escape", "bin/../../escape",
                                 "bin\\escape", "C:/escape", "unexpected/file", "bin/git"])
def test_git_rejects_unsafe_or_duplicate_paths_without_partial_runtime(tmp_path, monkeypatch, name):
    cache = fixture_cache(tmp_path, monkeypatch, extra=tarfile.TarInfo(name))
    with pytest.raises(ValueError, match="Unsafe Git archive path"):
        bundle_git.install(cache, tmp_path / "native")
    assert list((tmp_path / "native").iterdir()) == []


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_git_rejects_escaping_links_and_devices(tmp_path, monkeypatch, kind):
    extra = tarfile.TarInfo("bin/unsafe")
    extra.type = kind
    extra.linkname = "/outside"
    cache = fixture_cache(tmp_path, monkeypatch, extra=extra)
    with pytest.raises(tarfile.FilterError):
        bundle_git.install(cache, tmp_path / "native")
    assert list((tmp_path / "native").iterdir()) == []


def test_git_accepts_internal_relative_link_in_preflight(tmp_path):
    member = tarfile.TarInfo("libexec/git-core/git-commit")
    member.type = tarfile.SYMTYPE
    member.linkname = "git"
    bundle_git.validate_members([member], tmp_path)


@pytest.mark.parametrize("mode", ["wrong_arch", "missing_helper"])
def test_git_rejects_incomplete_runtime_without_partial_install(tmp_path, monkeypatch, mode):
    cache = fixture_cache(tmp_path, monkeypatch,
                          executable=b"wrong" if mode == "wrong_arch" else MACHO,
                          omit="libexec/git-core/git-lfs" if mode == "missing_helper" else None)
    with pytest.raises((ValueError, FileNotFoundError)):
        bundle_git.install(cache, tmp_path / "native")
    assert list((tmp_path / "native").iterdir()) == []


def test_git_archive_has_size_and_entry_limits(tmp_path):
    member = tarfile.TarInfo("bin/git")
    member.size = 601 * 1024**2
    with pytest.raises(ValueError, match="size limits"):
        bundle_git.validate_members([member], tmp_path)
    member.size = 0
    with pytest.raises(ValueError, match="size limits"):
        bundle_git.validate_members([member] * 20001, tmp_path)


def test_git_verified_cache_requires_no_network(tmp_path, monkeypatch):
    from macos import bundle_cli
    cache = fixture_cache(tmp_path, monkeypatch)
    def no_network(*args, **kwargs):
        pytest.fail("verified cache must remain offline")
    monkeypatch.setattr(bundle_cli.urllib.request, "urlopen", no_network)
    bundle_cli.fetch(cache, bundle_git.ASSETS)
