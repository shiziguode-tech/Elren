"""Local synthetic files; no browser launch or network is needed for URI policy."""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool


@pytest_asyncio.fixture(autouse=True)
async def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("File URI policy regression must not access network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    "literal%20name.html", "literal%2520name.html", "中文 space # tag.html",
    "semi;colon.html", "mixed # %20 ;.html", "ordinary.html",
])
async def test_file_uri_validator_and_resource_route_keep_exact_identity(tmp_path, name):
    target = tmp_path / name
    target.write_text("<p>expected file</p>", encoding="utf-8")
    (tmp_path / "literal name.html").write_text("<p>wrong decoy</p>", encoding="utf-8")
    tool = BackgroundBrowserTool(workspace=tmp_path)
    context = ToolContext(task_id="file-integrity", workspace=str(tmp_path))
    uri = target.as_uri()
    assert tool._local_file_path(uri, context) == target
    assert tool._local_file_path(uri + "?mode=preview#part2", context) == target
    await tool._validate_navigation_url(uri, context)
    actions = []

    async def abort(reason):
        actions.append(("abort", reason))

    async def continue_request():
        actions.append(("continue", None))

    await tool._route_request(SimpleNamespace(abort=abort, continue_=continue_request),
                              SimpleNamespace(url=uri), context)
    assert actions == [("continue", None)]


def test_nonexistent_literal_percent_does_not_select_decoded_decoy(tmp_path):
    (tmp_path / "literal name.html").write_text("wrong file", encoding="utf-8")
    tool = BackgroundBrowserTool(workspace=tmp_path)
    with pytest.raises(FileNotFoundError):
        tool._local_file_path((tmp_path / "literal%20name.html").as_uri(), None)


@pytest.mark.parametrize("uri", [
    "file://server/share/preview.html", "file:////server/share/preview.html",
    "file:///%2F%2Fserver/share/preview.html", "file:relative.html", "file://user:pass@localhost/preview.html",
])
def test_remote_relative_and_credentials_rejected_before_filesystem_probe(tmp_path, monkeypatch, uri):
    tool = BackgroundBrowserTool(workspace=tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Denied file URL must not resolve a network path")

    monkeypatch.setattr("pathlib.Path.resolve", forbidden)
    with pytest.raises(PermissionError):
        tool._local_file_path(uri, None)


def test_localhost_outside_directory_and_missing_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "preview.html"
    inside.write_text("preview", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("boundary canary", encoding="utf-8")
    tool = BackgroundBrowserTool(workspace=workspace)
    assert tool._local_file_path(inside.as_uri().replace("file:///", "file://localhost/", 1), None) == inside
    with pytest.raises(PermissionError, match="outside"):
        tool._local_file_path(outside.as_uri(), None)
    with pytest.raises(ValueError, match="regular file"):
        tool._local_file_path(workspace.as_uri(), None)
    with pytest.raises(FileNotFoundError):
        tool._local_file_path((workspace / "missing.html").as_uri(), None)
