"""Only an owner-private profile can include the validated desktop UI token."""

import zipfile
from pathlib import Path

import pytest

from launcher.build_macos_source_archive import build as build_macos
from launcher.build_release_archive import build, excluded

LANGUAGE_FILE = Path("data/desktop-ui-language.txt")


def make_package(tmp_path):
    package = tmp_path / "package"
    (package / "data").mkdir(parents=True)
    (package / "README.md").write_text("Isolated archive test", encoding="utf-8")
    return package


@pytest.mark.parametrize("language", [b"en", b"zh"])
def test_private_profile_preserves_exact_language_bytes_only(tmp_path, language):
    package = make_package(tmp_path)
    (package / LANGUAGE_FILE).write_bytes(language)
    (package / "data/unrelated-preference.txt").write_text("must remain local")
    (package / "data/desktop-ui-language.txt.bak").write_text("must remain local")
    destination = tmp_path / "private.zip"
    build(package, destination, include_private_keys=True, include_private_history=True)
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("package/data/desktop-ui-language.txt") == language
        assert "package/data/unrelated-preference.txt" not in archive.namelist()
        assert "package/data/desktop-ui-language.txt.bak" not in archive.namelist()


@pytest.mark.parametrize("keys,history", [(False, False), (True, False), (False, True)])
def test_language_requires_both_private_flags(tmp_path, keys, history):
    package = make_package(tmp_path)
    (package / LANGUAGE_FILE).write_bytes(b"en")
    assert excluded(LANGUAGE_FILE, include_private_keys=keys, include_private_history=history)
    destination = tmp_path / "partial-or-public.zip"
    build(package, destination, include_private_keys=keys, include_private_history=history)
    with zipfile.ZipFile(destination) as archive:
        assert "package/data/desktop-ui-language.txt" not in archive.namelist()


@pytest.mark.parametrize("language", [
    b"", b"EN", b"fr", b"en\n", b"zh\r\n", b" en", b"en ",
    b"\xef\xbb\xbfen", b"e\x00n\x00", b"en\x00", b"en" + b"x" * 10000,
])
def test_invalid_language_cannot_be_copied_into_private_archive(tmp_path, language):
    package = make_package(tmp_path)
    (package / LANGUAGE_FILE).write_bytes(language)
    destination = tmp_path / "private.zip"
    destination.write_bytes(b"existing artifact must survive failed build")
    with pytest.raises(ValueError, match="must contain exactly en or zh"):
        build(package, destination, include_private_keys=True, include_private_history=True)
    assert destination.read_bytes() == b"existing artifact must survive failed build"
    assert not list(tmp_path.glob("*.building"))


def test_older_private_profile_without_language_remains_supported(tmp_path):
    package = make_package(tmp_path)
    destination = tmp_path / "older-profile.zip"
    build(package, destination, include_private_keys=True, include_private_history=True)
    with zipfile.ZipFile(destination) as archive:
        assert "package/data/desktop-ui-language.txt" not in archive.namelist()


def test_private_language_path_must_be_a_file(tmp_path):
    package = make_package(tmp_path)
    (package / LANGUAGE_FILE).mkdir()
    with pytest.raises(ValueError, match="must be a regular file"):
        build(package, tmp_path / "invalid.zip", include_private_keys=True, include_private_history=True)


@pytest.mark.parametrize("language", [b"en", b"not a language token"])
def test_public_and_macos_archives_exclude_language_even_when_invalid(tmp_path, language):
    package = make_package(tmp_path)
    (package / LANGUAGE_FILE).write_bytes(language)
    for name, builder in (("public", build), ("macos", build_macos)):
        destination = tmp_path / f"{name}.zip"
        builder(package, destination)
        with zipfile.ZipFile(destination) as archive:
            assert all(not entry.endswith("/data/desktop-ui-language.txt") for entry in archive.namelist())
