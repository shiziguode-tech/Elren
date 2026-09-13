from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepdesk.control_files import ControlFileGuard, active_control_file_guard
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin

MAX_SKILL_BYTES = 100_000
MAX_SKILL_DEPTH = 4
MAX_SKILLS = 500
_SKIPPED_SKILL_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}


class SkillsTool(ToolPlugin):
    name = "skills"
    description = (
        "Discover and read workspace SKILL.md packages compatible with OpenClaw/Codex style skills. "
        "Use list first, then read the exact skill needed for the task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "read"]},
            "skill": {"type": "string", "description": "Skill name returned by list"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        credential_status: Callable[[str], dict[str, bool]] | None = None,
    ) -> None:
        self.credential_status = credential_status or (lambda _skill: {})

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        application_workspace = context.application_workspace or context.workspace
        guard = active_control_file_guard(application_workspace)
        skills = self._discover(Path(application_workspace), guard=guard)
        action = arguments["action"]
        if action == "list":
            return {
                "skills": [
                    {
                        "name": name,
                        "description": self._description(path, guard=guard),
                        "path": str(path),
                        "credentials": self.credential_status(name),
                    }
                    for name, path in sorted(skills.items())
                ]
            }
        if action == "read":
            name = arguments.get("skill", "")
            path = skills.get(name)
            if not path:
                raise KeyError(f"Skill not found: {name}")
            content = guard.trusted_bytes(path) if guard is not None else path.read_bytes()
            if content is None:
                raise PermissionError("Skill is not present in the trusted startup snapshot")
            if len(content) > MAX_SKILL_BYTES:
                raise ValueError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
            return {
                "name": name,
                "path": str(path),
                "content": content.decode("utf-8"),
                "credentials": self.credential_status(name),
            }
        raise ValueError(f"Unsupported skills action: {action}")

    @staticmethod
    def _discover(
        workspace: Path, guard: ControlFileGuard | None = None
    ) -> dict[str, Path]:
        """Discover nested skill packages without walking arbitrary cache trees.

        Workspace-owned skills take precedence over ``.deepdesk`` skills, and
        shallower packages take precedence over deeper bundled copies.  Sorting
        every candidate first makes duplicate-name handling deterministic on
        Windows and other filesystems with implementation-defined directory
        enumeration order.
        """
        roots = (workspace / "skills", workspace / ".deepdesk" / "skills")
        found: dict[str, Path] = {}
        for root in roots:
            if guard is None and not root.is_dir():
                continue
            root = root.resolve()
            candidates: list[Path] = []
            source_paths = (
                [path for path in guard.trusted_skill_files() if root in path.parents]
                if guard is not None
                else list(root.rglob("SKILL.md"))
            )
            for path in source_paths:
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                directories = relative.parts[:-1]
                if guard is not None:
                    resolved = path
                else:
                    try:
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(root.resolve())
                    except (OSError, ValueError):
                        continue
                if (
                    (guard is None and not path.is_file())
                    or not directories
                    or len(directories) > MAX_SKILL_DEPTH
                    or any(
                        part.startswith(".") or part in _SKIPPED_SKILL_DIRECTORIES
                        for part in directories
                    )
                ):
                    continue
                candidates.append(resolved)
            candidates.sort(
                key=lambda path: (
                    len(path.relative_to(root).parts),
                    path.relative_to(root).as_posix().casefold(),
                )
            )
            for path in candidates:
                name = path.parent.name
                found.setdefault(name, path.resolve())
                if len(found) >= MAX_SKILLS:
                    return found
        return found

    @staticmethod
    def _description(path: Path, guard: ControlFileGuard | None = None) -> str:
        try:
            raw = guard.trusted_bytes(path) if guard is not None else path.read_bytes()
            if raw is None:
                return ""
            content = raw[:8000].decode("utf-8")
        except (OSError, UnicodeError):
            return ""
        frontmatter = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if frontmatter:
            match = re.search(
                r"^description:\s*[\"']?(.*?)[\"']?\s*$", frontmatter[1], re.MULTILINE
            )
            if match:
                return match.group(1).strip()
        for line in content.splitlines():
            text = line.strip().lstrip("#").strip()
            if text and not text.startswith("---"):
                return text[:240]
        return ""
