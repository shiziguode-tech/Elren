"""Exact read-only attachment grants for repository tasks."""
from __future__ import annotations

import stat
from pathlib import Path

from deepdesk.plugins.base import ToolContext


def grant_task_screenshot(context: ToolContext, tool: str, arguments: dict, result: dict) -> None:
    if tool not in {"computer", "background_browser", "live_computer_use"}:
        return
    observation = result.get("observation") if tool == "live_computer_use" else None
    screenshot = (observation or result).get("screenshot") if isinstance(observation or result, dict) else None
    if tool == "computer" and arguments.get("action") == "screenshot":
        screenshot = result.get("path")
    if isinstance(screenshot, str) and Path(screenshot).is_file():
        context.authorized_read_paths = tuple(dict.fromkeys([
            *context.authorized_read_paths, str(Path(screenshot).resolve()),
        ]))


def is_project_context(context: ToolContext) -> bool:
    return bool(context.application_workspace) and (
        Path(context.workspace).resolve() != Path(context.application_workspace).resolve()
    )


def resolve_project_read(requested: str, context: ToolContext) -> Path:
    root = Path(context.workspace).resolve()
    raw = Path(requested)
    path = (root / raw).resolve()
    if path == root or root in path.parents:
        return path
    # Grants are host-canonical absolute filenames, never directory grants.
    # Do not resolve the grant again: a replaced link must not expand access.
    grants = {Path(item) for item in context.authorized_read_paths}
    if not raw.is_absolute() or raw not in grants or path != raw:
        raise PermissionError("File is outside this project and is not an attachment authorized for this task")
    for item in [*reversed(raw.parents), raw]:
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError("Authorized attachment paths cannot be redirected")
    if not path.is_file():
        raise PermissionError("Attachment grants apply only to individual existing files")
    return path
