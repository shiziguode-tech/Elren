from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.filesystem import FileSystemTool


def _context(root: Path) -> ToolContext:
    return ToolContext(task_id="synthetic-developer-integrity", workspace=str(root))


def _edit(tool, context, old="alpha = 1", new="alpha = 2"):
    return tool._execute_sync({"action": "edit", "path": "app.py", "edits": [
        {"old_text": old, "new_text": new},
    ]}, context)


def test_edit_rejects_user_save_between_read_and_replace(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_bytes(b"alpha = 1\nbeta = 1\n")
    user_bytes = b"alpha = 1\nbeta = 2 # user save\n"
    original_fsync = os.fsync

    def save_user_edit(fd):
        original_fsync(fd)
        target.write_bytes(user_bytes)

    monkeypatch.setattr(os, "fsync", save_user_edit)
    with pytest.raises(ValueError, match="changed.*edit|edit.*changed"):
        _edit(FileSystemTool(), _context(tmp_path))
    assert target.read_bytes() == user_bytes
    assert not list(tmp_path.glob(".*.elren-*.tmp"))


def test_edit_rejects_removed_target_instead_of_recreating(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_bytes(b"alpha = 1\n")
    original_fsync = os.fsync

    def remove_target(fd):
        original_fsync(fd)
        target.unlink()

    monkeypatch.setattr(os, "fsync", remove_target)
    with pytest.raises(ValueError, match="changed.*edit|edit.*changed"):
        _edit(FileSystemTool(), _context(tmp_path))
    assert not target.exists()


def test_simultaneous_elren_edits_preserve_both_changes(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_bytes(b"alpha = 1\nbeta = 1\n")
    reached = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original_fsync = os.fsync
    first_thread = []

    def pause_first(fd):
        original_fsync(fd)
        if threading.get_ident() == first_thread[0]:
            reached.set()
            assert release.wait(5)

    def first():
        first_thread.append(threading.get_ident())
        return _edit(FileSystemTool(), _context(tmp_path))

    def second():
        second_started.set()
        return _edit(FileSystemTool(), _context(tmp_path), "beta = 1", "beta = 2")

    monkeypatch.setattr(os, "fsync", pause_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert reached.wait(5)
        b = pool.submit(second)
        assert second_started.wait(5)
        try:
            with pytest.raises(TimeoutError):
                b.result(timeout=0.15)
        finally:
            release.set()
        assert a.result(timeout=5)["changed"]
        assert b.result(timeout=5)["changed"]
    assert target.read_bytes() == b"alpha = 2\nbeta = 2\n"


@pytest.mark.parametrize("action", ["search", "map", "list"])
@pytest.mark.parametrize("outside", [True, False])
def test_traversal_never_exposes_linked_secret_or_external_file(tmp_path, action, outside):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt" if outside else workspace / ".env"
    target.write_text("SYNTHETIC_MARKER_ONLY", encoding="utf-8")
    try:
        (workspace / "innocent.txt").symlink_to(target)
    except OSError:
        pytest.skip("Symbolic link creation unavailable")
    result = FileSystemTool()._execute_sync(
        {"action": action, "path": ".", "query": "SYNTHETIC"},
        _context(workspace),
    )
    assert "innocent.txt" not in str(result)
    assert "SYNTHETIC_MARKER_ONLY" not in str(result)


def test_search_retains_normal_in_scope_results(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("needle = 42\n", encoding="utf-8")
    result = FileSystemTool()._execute_sync(
        {"action": "search", "path": ".", "query": "needle"}, _context(tmp_path)
    )
    assert result["match_count"] == 1
    assert result["matches"][0]["line"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction behavior")
@pytest.mark.parametrize("action", ["search", "map", "list"])
def test_windows_directory_junction_cannot_expand_traversal_scope(tmp_path, action):
    import _winapi

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "other-repository"
    outside.mkdir()
    (outside / "unrelated.py").write_text("JUNCTION_SYNTHETIC_ONLY", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(workspace / "linked-directory"))
    result = FileSystemTool()._execute_sync(
        {"action": action, "path": ".", "query": "JUNCTION"}, _context(workspace)
    )
    assert "linked-directory" not in str(result)
    assert "JUNCTION_SYNTHETIC_ONLY" not in str(result)


def test_normal_internal_file_link_remains_searchable(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("NORMAL_INTERNAL_LINK", encoding="utf-8")
    try:
        (tmp_path / "alias.py").symlink_to(target)
    except OSError:
        pytest.skip("Symbolic link creation unavailable")
    result = FileSystemTool()._execute_sync(
        {"action": "search", "path": ".", "query": "NORMAL_INTERNAL"}, _context(tmp_path)
    )
    assert result["match_count"] == 2
