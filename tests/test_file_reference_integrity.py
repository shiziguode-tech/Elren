"""Artifact URLs and literal remote references must retain exact file identity."""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio

from deepdesk.config import Settings
from deepdesk.control_files import deactivate_control_file_guard
from deepdesk.main import _referenced_output_paths, create_app


@pytest_asyncio.fixture
async def artifact_app(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("No real network is allowed in file integrity tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    app = create_app(Settings(
        _env_file=None, deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False,
        deepseek_api_key="", deepseek_backup_api_key="",
    ))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        try:
            yield client, tmp_path
        finally:
            deactivate_control_file_guard(tmp_path)


@pytest.mark.asyncio
async def test_actual_list_urls_return_exact_bytes_uncached_and_cached(artifact_app):
    client, workspace = artifact_app
    outputs = workspace / "outputs"
    names = (
        "fragment.txt", "fragment.txt#new.txt", "percent name.txt", "percent%20name.txt",
        "中文 空格 #100%.txt", "literal%2520name.txt", "draft (2) [final].txt",
        "O'Brien & final+result.txt",
    )
    expected = {name: f"ONLY THE EXACT FILE: {index} {name}".encode() for index, name in enumerate(names)}
    for name, payload in expected.items():
        (outputs / name).write_bytes(payload)
    folder = outputs / "目录 #100%"
    folder.mkdir()
    child = folder / "子文件 %20.txt"
    child.write_bytes(b"synthetic nested file")

    first = await client.get("/api/artifacts")
    assert first.status_code == 200
    initial = first.json()
    assert initial["summary_pending"] is True
    first_record = next(row for row in initial["artifacts"] if row["name"] == names[0])
    assert "summary_pending" in first_record  # Proves the uncached response branch.

    deadline = asyncio.get_running_loop().time() + 5
    while True:
        response = await client.get("/api/artifacts")
        cached = response.json()
        if not cached["summary_pending"]:
            break
        assert asyncio.get_running_loop().time() < deadline, "Owned index refresh did not finish"
        await asyncio.sleep(0.01)
    cached_record = next(row for row in cached["artifacts"] if row["name"] == names[0])
    assert "summary_pending" not in cached_record  # Proves the scan/cache response branch.

    for listing in (initial, cached):
        for name, payload in expected.items():
            record = next(row for row in listing["artifacts"] if row["name"] == name)
            assert record["url"] == "/api/artifacts/" + quote(name, safe="")
            downloaded = await client.get(record["url"])
            assert downloaded.status_code == 200
            assert downloaded.content == payload
        directory = next(row for row in listing["artifacts"] if row["name"] == folder.name)
        assert directory["kind"] == "folder" and directory["url"] is None

    nested_url = "/api/artifacts/" + "/".join(quote(part, safe="") for part in child.relative_to(outputs).parts)
    assert (await client.get(nested_url)).content == b"synthetic nested file"
    assert (await client.get("/api/artifacts/" + quote(folder.name, safe=""))).status_code == 404
    (workspace / "outside.txt").write_bytes(b"do not return")
    escaped = await client.get("/api/artifacts/%2e%2e/outside.txt")
    assert escaped.status_code == 403 and escaped.content != b"do not return"


def selected(workspace: Path, result: str, *, attachments=(), max_bytes=1024, max_results=10):
    return _referenced_output_paths(
        SimpleNamespace(result=result, attachments=attachments), workspace, ("txt", ".pdf"),
        max_bytes=max_bytes, max_results=max_results,
    )


@pytest.mark.parametrize("name", [
    "report.txt.v2.txt", "report.txt 中文 新版.txt", "report.txt%20new.txt",
    "report.txt#revision.txt", "report.txt(final)[2].txt", "report.txt'OBrien.txt",
    "report.txt).v2.txt", "report.txt].v2.txt",
    "report.txt`new.txt",
])
@pytest.mark.parametrize("formatting", ["absolute", "relative", "code", "angle-link", "link", "double-quote", "single-quote"])
def test_full_literal_reference_wins_over_real_old_prefix(tmp_path, name, formatting):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    old = outputs / "report.txt"
    wanted = outputs / name
    old.write_bytes(b"WRONG OLD RESULT")
    wanted.write_bytes(b"CORRECT NEW RESULT")
    literal = str(wanted)
    result = {
        "absolute": literal, "relative": "outputs/" + name, "code": f"`{literal}`",
        "angle-link": f"[download](<{literal}>)", "link": f"[download]({literal})",
        "double-quote": f'"{literal}"', "single-quote": f"'{literal}'",
    }[formatting]
    found = selected(tmp_path, result)
    assert found == [wanted.resolve()]
    assert found[0].read_bytes() == b"CORRECT NEW RESULT"


@pytest.mark.parametrize("tail", [".v2.txt", ".v2.bin", "#new.bin", "'new.bin", ",new.bin"])
def test_missing_or_unsupported_full_reference_never_falls_back_to_prefix(tmp_path, tail):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.txt").write_bytes(b"OLD RESULT MUST NOT BE SENT")
    assert selected(tmp_path, str(outputs / ("report.txt" + tail))) == []


def test_nested_extension_directories_multiple_links_and_literal_percent(tmp_path):
    folder = tmp_path / "outputs" / "draft.txt" / "中文 (目录)"
    folder.mkdir(parents=True)
    files = [folder / "final report.txt", folder / "literal%20name.txt"]
    for index, path in enumerate(files):
        path.write_bytes(str(index).encode())
    result = " ".join(f"[download](<{path}>)" for path in files)
    assert selected(tmp_path, result) == [path.resolve() for path in files]
    assert selected(tmp_path, "\n".join(str(path) for path in files), max_results=1) == [files[0].resolve()]


def test_outputs_boundary_input_directory_size_and_duplicate_filters_preserved(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    accepted = outputs / "new.txt"
    accepted.write_bytes(b"correct")
    uploaded = outputs / "input.txt"
    uploaded.write_bytes(b"input must not be resent")
    private = tmp_path / "private.txt"
    private.write_bytes(b"private must not be sent")
    directory = outputs / "folder.txt"
    directory.mkdir()
    huge = outputs / "huge.txt"
    huge.write_bytes(b"x" * 1025)
    result = "\n".join(map(str, (private, uploaded, directory, huge, accepted, accepted)))
    assert selected(tmp_path, result, attachments=["outputs/input.txt"]) == [accepted.resolve()]


def test_uri_formats_are_not_promoted_to_automatic_outbound_attachments(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    plain = outputs / "plain.txt"
    spaced = outputs / "space name.txt"
    plain.write_bytes(b"literal only")
    spaced.write_bytes(b"literal only")
    for reference in (plain.as_uri(), spaced.as_uri(), quote(spaced.as_posix(), safe="/:"),
                      "https://example.invalid" + plain.as_posix()):
        assert selected(tmp_path, f"[download]({reference})") == []
    assert selected(tmp_path, str(spaced)) == [spaced.resolve()]
