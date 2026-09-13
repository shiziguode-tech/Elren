from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest

from deepdesk import audiveris_tessdata as data
from launcher import build_macos_source_archive as mac_archive


@pytest.fixture
def fixture_data(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Network forbidden in offline dependency tests")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    source = tmp_path / "source"
    source.mkdir()
    lock = json.loads(data.LOCK_PATH.read_text("utf-8"))
    for name in lock["files"]:
        payload = ("isolated-fixture-" + name).encode()
        (source / name).write_bytes(payload)
        lock["files"][name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    fixture_lock = tmp_path / data.LOCK_PATH.name
    fixture_lock.write_text(json.dumps(lock), "utf-8")
    monkeypatch.setattr(data, "LOCK_PATH", fixture_lock)
    return source


def test_install_is_offline_and_idempotent(fixture_data, tmp_path):
    home = tmp_path / "runtime"
    actual = data.install_tessdata(fixture_data, home)
    before = {p.name: p.read_bytes() for p in actual.iterdir()}
    assert actual == data.validate_tessdata(home)
    assert data.install_tessdata(fixture_data, home) == actual
    assert before == {p.name: p.read_bytes() for p in actual.iterdir()}
    assert {p.name for p in actual.iterdir()} == {
        "eng.traineddata", "chi_sim.traineddata", "chi_tra.traineddata", "LICENSE", data.LOCK_PATH.name,
    }


@pytest.mark.parametrize("name", ["eng.traineddata", "chi_sim.traineddata", "chi_tra.traineddata", "LICENSE", "audiveris_tessdata.lock.json"])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_missing_or_corrupt_dependencies_fail_closed(fixture_data, tmp_path, name, failure):
    home = tmp_path / "runtime"
    target = data.install_tessdata(fixture_data, home) / name
    if failure == "missing":
        target.unlink()
    else:
        contents = target.read_bytes()
        target.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
    with pytest.raises(ValueError):
        data.validate_tessdata(home)
    with pytest.raises(ValueError):
        data.install_tessdata(fixture_data, home)


def test_invalid_source_never_touches_existing_runtime(fixture_data, tmp_path):
    home = tmp_path / "runtime"
    home.mkdir()
    sentinel = home / "keep.txt"
    sentinel.write_text("existing runtime")
    (fixture_data / "eng.traineddata").write_bytes(b"LSTM-only or corrupt replacement")
    with pytest.raises(ValueError):
        data.install_tessdata(fixture_data, home)
    assert list(home.iterdir()) == [sentinel]


def test_extra_data_is_not_silently_allowed(fixture_data, tmp_path):
    home = tmp_path / "runtime"
    destination = data.install_tessdata(fixture_data, home)
    (destination / "unreviewed.traineddata").write_bytes(b"other")
    with pytest.raises(ValueError, match="Unexpected"):
        data.validate_tessdata(home)


def test_linked_model_is_rejected(fixture_data, tmp_path):
    home = tmp_path / "runtime"
    destination = data.install_tessdata(fixture_data, home)
    target = destination / "eng.traineddata"
    target.unlink()
    try:
        target.symlink_to(fixture_data / "eng.traineddata")
    except OSError:
        pytest.skip("OS does not permit test symlinks")
    with pytest.raises(ValueError, match="Redirected"):
        data.validate_tessdata(home)


def test_mac_source_collects_exactly_one_portable_data_copy(fixture_data, tmp_path):
    source = tmp_path / "product"
    (source / "deepdesk").mkdir(parents=True)
    (source / "deepdesk/audiveris_tessdata.py").write_text("# music enabled")
    runtime = source / data.RELATIVE_DATA.parent
    data.install_tessdata(fixture_data, runtime)
    (runtime / "runtime/bin").mkdir(parents=True)
    (runtime / "runtime/bin/java.exe").write_bytes(b"must not ship")
    (runtime / "app").mkdir()
    (runtime / "app/audiveris.jar").write_bytes(b"must not ship")
    archive = tmp_path / "fixture-source.zip"
    mac_archive.build(source, archive)
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        runtime_names = [name for name in names if "/work/" in name]
        assert len(runtime_names) == 5
        assert not any(name.endswith((".exe", ".jar")) for name in names)
        assert len([name for name in names if name.endswith("eng.traineddata")]) == 1
        assert zipped.testzip() is None
        for name in runtime_names:
            assert zipped.read(name) == (source / Path(name).relative_to(mac_archive.ARCHIVE_ROOT)).read_bytes()


def test_music_enabled_mac_source_requires_offline_data(tmp_path):
    source = tmp_path / "product"
    (source / "deepdesk").mkdir(parents=True)
    (source / "deepdesk/audiveris_tessdata.py").write_text("# music enabled")
    with pytest.raises(ValueError, match="Missing offline"):
        mac_archive.build(source, tmp_path / "missing.zip")
    assert not (tmp_path / "missing.zip").exists()


def test_frozen_mac_build_explicitly_carries_the_trusted_lock():
    root = Path(__file__).resolve().parents[1]
    script = (root / "macos/build-macos-app.sh").read_text("utf-8")
    assert '--add-data "$ROOT/deepdesk/audiveris_tessdata.lock.json:deepdesk"' in script


@pytest.mark.skipif(os.name != "nt", reason="Windows installer call-chain")
def test_windows_installer_preflights_and_preserves_data_on_force(fixture_data, tmp_path):
    root = Path(__file__).resolve().parents[1]
    product = tmp_path / "product"
    for relative in ("launcher", "deepdesk", ".venv/Scripts", "work/tool-runtime"):
        (product / relative).mkdir(parents=True, exist_ok=True)
    for relative in ("launcher/install-jianpu-runtime.ps1", "launcher/audiveris-AGPL-3.0-LICENSE.txt",
                     "deepdesk/audiveris_tessdata.py", ".venv/Scripts/python.exe", ".venv/pyvenv.cfg"):
        shutil.copyfile(root / relative, product / relative)
    shutil.copyfile(data.LOCK_PATH, product / "deepdesk" / data.LOCK_PATH.name)
    (product / "work/tool-runtime/manifest.json").write_text('{"components":[],"tools":[]}', "utf-8")
    source = tmp_path / "reviewed-fixture-app"
    (source / "app").mkdir(parents=True)
    (source / "runtime/bin").mkdir(parents=True)
    (source / "runtime/bin/java.exe").write_bytes(b"fixture-only-never-executed")
    (source / "app/Audiveris.cfg").write_text("jpackage.app-version=5.11.0")
    with zipfile.ZipFile(source / "app/audiveris.jar", "w") as jar:
        jar.writestr("res/basic-classifier.zip", b"fixture-classifier")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(product / "launcher/install-jianpu-runtime.ps1"), "-Source", str(source)]
    first = subprocess.run([*command, "-TessdataSource", str(fixture_data)], capture_output=True,
                           text=True, errors="replace", timeout=30, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    target = product / "work/tool-runtime/native/audiveris"
    assert data.validate_tessdata(target)
    # Rebuild consumes the existing runtime as its default offline data source.
    second = subprocess.run([*command, "-Force"], capture_output=True,
                            text=True, errors="replace", timeout=30, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert data.validate_tessdata(target)
    before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    (fixture_data / "eng.traineddata").write_bytes(b"broken")
    failure = subprocess.run([*command, "-Force", "-TessdataSource", str(fixture_data)],
                             capture_output=True, text=True, errors="replace", timeout=30, check=False)
    assert failure.returncode != 0
    assert before == {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
