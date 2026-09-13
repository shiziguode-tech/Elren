from __future__ import annotations

import asyncio
import errno
import os
import threading
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from test_loopback_web_security import _isolated_app

from deepdesk.upload_storage import save_upload


@pytest.mark.parametrize("filename", [
    "NUL.txt", "CON.txt", "AUX.txt", "COM1.txt", "LPT9.txt",
    "nul.backup.txt", "COM¹.txt", "中文 & score.txt",
])
def test_upload_reserved_windows_names_roundtrip(tmp_path, filename):
    client = TestClient(_isolated_app(tmp_path), raise_server_exceptions=False)
    content = b"synthetic attachment, not a device"
    response = client.post("/api/uploads", content=content,
                           headers={"X-Filename": quote(filename, safe="")})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["name"] == filename
    assert Path(payload["path"]).is_file()
    assert Path(payload["path"]).read_bytes() == content
    assert client.get(payload["url"]).content == content


@pytest.mark.parametrize("language", ["en", "zh"])
def test_failed_upload_does_not_publish_partial_file(tmp_path, monkeypatch, language):
    client = TestClient(_isolated_app(tmp_path), raise_server_exceptions=False)
    original = Path.write_bytes

    def disk_full(path, content):
        if "uploads" in path.parts:
            original(path, bytes(content[:3]))
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return original(path, content)

    monkeypatch.setattr(Path, "write_bytes", disk_full)
    def full_on_flush(_descriptor):
        raise OSError(errno.ENOSPC, "synthetic disk full")
    monkeypatch.setattr(os, "fsync", full_on_flush)
    response = client.post("/api/uploads", content=b"complete attachment",
                           headers={"X-Filename": "qa.txt", "Accept-Language": language})
    upload_root = tmp_path / "screenshots" / "uploads"
    assert not list(upload_root.iterdir()), "Failed request left an attachment behind"
    assert response.status_code == 507, response.text
    assert response.json()["detail"]["code"] == "upload_storage_full"


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("error,code", [
    (PermissionError(errno.EACCES, "synthetic denied"), "upload_storage_denied"),
    (OSError(errno.EROFS, "synthetic read-only"), "upload_storage_denied"),
    (OSError(errno.EIO, "synthetic I/O error"), "upload_storage_failed"),
])
def test_storage_errors_are_localized_and_preserve_other_uploads(tmp_path, monkeypatch, language, error, code):
    client = TestClient(_isolated_app(tmp_path), raise_server_exceptions=False)
    root = tmp_path / "screenshots" / "uploads"
    previous = root / "previous.txt"
    previous.write_bytes(b"previous complete upload")

    def fail(target, payload):
        # Exercise failures after the destination was written too.
        target.write_bytes(payload[:3])
        raise error

    monkeypatch.setattr("deepdesk.upload_storage._write_upload", fail)
    response = client.post("/api/uploads", content=b"synthetic new upload", headers={
        "X-Filename": "new.txt", "Accept-Language": language,
    })
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert ("附件" in detail["message"]) == (language == "zh")
    assert list(root.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous complete upload"


def test_directory_collision_never_removes_existing_upload(tmp_path):
    target = tmp_path / "existing" / "score.txt"
    target.parent.mkdir()
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        save_upload(target, b"replacement")
    assert target.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.parametrize("simulate_old_blocking_route", [False, True])
async def test_slow_upload_does_not_block_other_http_requests(tmp_path, monkeypatch, simulate_old_blocking_route):
    app = _isolated_app(tmp_path)
    entered = threading.Event()
    released = threading.Event()
    real_save = save_upload

    def slow_save(target, payload):
        entered.set()
        assert released.wait(3), "test safety timer did not release the write"
        real_save(target, payload)

    monkeypatch.setattr("deepdesk.main.save_upload", slow_save)
    if simulate_old_blocking_route:
        async def blocking(function, *args):
            return function(*args)
        monkeypatch.setattr("deepdesk.main.run_owned_thread", blocking)

    # An OS thread releases even the deliberately blocked negative control.
    timer = threading.Timer(0.7, released.set)
    timer.start()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        pending = asyncio.create_task(client.post("/api/uploads", content=b"complete", headers={"X-Filename": "slow.txt"}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set(), (pending.result().text if pending.done() else "route not entered")
            responsive_while_writing = not released.is_set()
            response = await client.get("/")
            responsive_while_writing &= response.status_code == 200 and not released.is_set()
            assert responsive_while_writing is not simulate_old_blocking_route
        finally:
            released.set()
            timer.cancel()
            result = await pending
            timer.join()
        assert result.status_code == 200
        assert Path(result.json()["path"]).read_bytes() == b"complete"


@pytest.mark.skipif(os.name != "nt", reason="real Windows file sharing semantics")
def test_windows_file_lock_failure_cleans_only_this_upload(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    from deepdesk.upload_storage import _write_upload

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def locked_save(target, payload):
        handle = kernel.CreateFileW(str(target), 0x40000000, 0, None, 2, 0x80, None)
        assert handle != ctypes.c_void_p(-1).value
        try:
            _write_upload(target, payload)
        finally:
            kernel.CloseHandle(handle)

    monkeypatch.setattr("deepdesk.upload_storage._write_upload", locked_save)
    target = tmp_path / "中文 & lock" / "score.txt"
    with pytest.raises(PermissionError):
        save_upload(target, b"new contents")
    assert not target.parent.exists()


def test_attachment_storage_does_not_require_ntfs_dacl(tmp_path, monkeypatch):
    # Capability simulation, not a claim of a physical exFAT volume test.
    def unsupported(_path):
        raise OSError("This filesystem does not support NTFS DACLs")
    monkeypatch.setattr("deepdesk.secret_storage._restrict_file_to_current_user", unsupported)
    target = tmp_path / "portable" / "score.txt"
    save_upload(target, b"portable attachment")
    assert target.read_bytes() == b"portable attachment"
