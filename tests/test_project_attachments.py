"""Synthetic uploads exercise exact task grants without cloud calls or UI input."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from deepdesk.config import Settings
from deepdesk.main import create_app
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.document import DocumentTool
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.builtin.vision import VisionTool
from deepdesk.task_files import grant_task_screenshot, resolve_project_read


class LocalOCR:
    async def recognize(self, path, *_args):
        # Actually decode the uploaded PNG, never call a provider or desktop API.
        with Image.open(path) as image:
            assert image.size == (120, 60)
        return {"text": "SYNTHETIC PNG decoded", "source": "synthetic-local", "model": "fixture"}


@pytest.mark.asyncio
async def test_uploaded_project_attachments_read_only_and_other_tasks_denied(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(_env_file=None, deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False,
        deepseek_api_key="", deepseek_backup_api_key=""))
    repo = tmp_path / "repo"
    repo.mkdir()
    screenshot_root = app.state.settings.screenshot_dir
    stream = io.BytesIO()
    Image.new("RGB", (120, 60), "white").save(stream, "PNG")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    paths = []
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        for name, content in [("spec.txt", b"TASK-ATTACHMENT-ONLY"), ("mockup.png", stream.getvalue()),
                              ("other.txt", b"OTHER-TASK-PRIVATE"), ("other.png", stream.getvalue())]:
            response = await client.post("/api/uploads", content=content, headers={"X-Filename": name})
            assert response.status_code == 200, response.text
            paths.append(response.json()["path"])
    context = ToolContext("project-upload", str(repo), application_workspace=str(tmp_path),
                          authorized_read_paths=tuple(paths[:2]))
    fs, doc = FileSystemTool(), DocumentTool(screenshot_root / "uploads")
    vision = VisionTool(SimpleNamespace(), screenshot_root, windows_ocr=LocalOCR(), paddle_ocr=LocalOCR())
    assert "TASK-ATTACHMENT-ONLY" in str(await fs.execute({"action": "read", "path": paths[0]}, context))
    assert "TASK-ATTACHMENT-ONLY" in str(await doc.execute({"action": "inspect", "path": paths[0]}, context))
    result = await vision.execute({"image_path": paths[1], "question": "OCR text", "cloud_fallback": False}, context)
    assert result["cloud_used"] is False
    assert "PNG decoded" in result["description"]
    for tool, args in [
        (fs, {"action": "read", "path": paths[2]}),
        (doc, {"action": "inspect", "path": paths[2]}),
        (vision, {"image_path": paths[3], "question": "OCR", "cloud_fallback": False}),
        (fs, {"action": "write", "path": paths[0], "content": "MUTATED"}),
        (doc, {"action": "create", "path": paths[0], "paragraphs": ["MUTATED"]}),
        (fs, {"action": "list", "path": str(screenshot_root / "uploads")}),
    ]:
        with pytest.raises(PermissionError):
            await tool.execute(args, context)
    assert Path(paths[0]).read_text() == "TASK-ATTACHMENT-ONLY"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("APP-PRIVATE")
    for tool in (fs, doc):
        with pytest.raises(PermissionError):
            await tool.execute({"action": "read" if tool is fs else "inspect", "path": str(unrelated)}, context)
    unrelated_image = screenshot_root / "unrelated.png"
    unrelated_image.write_bytes(stream.getvalue())
    with pytest.raises(PermissionError):
        await vision.execute({"image_path": str(unrelated_image), "question": "OCR"}, context)
    # A host-produced screenshot can receive one exact task-local grant.
    context.authorized_read_paths = (*context.authorized_read_paths, str(unrelated_image.resolve()))
    assert (await vision.execute({"image_path": str(unrelated_image), "question": "OCR", "cloud_fallback": False}, context))["cloud_used"] is False
    assert not list(repo.iterdir()), "Attachments must not be copied into the repository"


def test_attachment_grants_do_not_authorize_parents_or_redirected_targets(tmp_path):
    repo, uploads = tmp_path / "repo", tmp_path / "uploads"
    repo.mkdir()
    uploads.mkdir()
    attachment, secret = uploads / "allowed.txt", tmp_path / "private.txt"
    attachment.write_text("allowed")
    secret.write_text("private")
    context = ToolContext("q", str(repo), application_workspace=str(tmp_path),
                          authorized_read_paths=(str(attachment.resolve()),))
    for invalid in (str(uploads), str(secret), "../private.txt"):
        with pytest.raises(PermissionError):
            resolve_project_read(invalid, context)
    attachment.unlink()
    try:
        attachment.symlink_to(secret)
    except OSError:
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(PermissionError):
        resolve_project_read(str(attachment), context)


@pytest.mark.parametrize("tool,result_key", [("computer", "path"), ("background_browser", "screenshot"), ("live_computer_use", "screenshot"), ("live_computer_use", "observation")])
def test_only_screenshot_tools_can_issue_exact_file_grants(tmp_path, tool, result_key):
    image = tmp_path / "generated.png"
    Image.new("RGB", (120, 60), "white").save(image)
    context = ToolContext("q", str(tmp_path / "repo"), application_workspace=str(tmp_path))
    grant_task_screenshot(context, "shell", {}, {"screenshot": str(image)})
    assert not context.authorized_read_paths
    result = {result_key: {"screenshot": str(image)} if result_key == "observation" else str(image)}
    grant_task_screenshot(context, tool, {"action": "screenshot"}, result)
    assert context.authorized_read_paths == (str(image.resolve()),)


@pytest.mark.asyncio
async def test_project_vision_rejects_non_image_repository_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "settings.txt"
    file.write_text("arbitrary repository text must not be uploaded as an image")
    tool = VisionTool(SimpleNamespace(), tmp_path, windows_ocr=LocalOCR(), paddle_ocr=LocalOCR())
    context = ToolContext("q", str(repo), application_workspace=str(tmp_path))
    with pytest.raises(PermissionError, match="raster"):
        await tool.execute({"image_path": str(file), "question": "OCR"}, context)
