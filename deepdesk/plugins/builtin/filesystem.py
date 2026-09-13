from __future__ import annotations

import codecs
import ctypes
import fnmatch
import os
import re
import shutil
import sys
from ctypes import wintypes
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from deepdesk.command_safety import allows_permanent_delete
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread

# Bounded, process-wide stripes serialize our own read/modify/replace operations
# even when two tasks use separate tool instances. External editors are detected
# separately by the pre-commit snapshot check.
_MUTATION_LOCKS = tuple(RLock() for _ in range(64))


class _SHFileOperationW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class FileSystemTool(ToolPlugin):
    name = "filesystem"
    description = (
        "Read focused line ranges, list/search, apply exact atomic edits, create, or delete files "
        "inside the configured workspace. Prefer edit over rewriting a whole existing file. "
        "Delete moves items to the operating system Trash/Recycle Bin by default. Set permanent=true only when "
        "the current user message explicitly asks for permanent, irreversible deletion. Use "
        "search instead of shell/rg for repository text lookup, and read before changing a file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "map", "list", "read", "search", "edit", "write", "append", "mkdir", "delete"
                ],
            },
            "path": {"type": "string", "description": "Workspace-relative path"},
            "content": {"type": "string", "description": "Content for write/append"},
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional first 1-based line for a focused read",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional inclusive last 1-based line for a focused read",
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "description": (
                    "Atomic exact replacements applied in order. Every old_text must match exactly "
                    "expected_count times or no file change is written."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                        "expected_count": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
            "query": {"type": "string", "description": "Literal text or regex for search"},
            "glob": {
                "type": "string",
                "description": "Optional filename glob for search, e.g. *.py",
            },
            "regex": {"type": "boolean", "description": "Treat query as a regex"},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
            "max_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Maximum directory depth for the bounded map action",
            },
            "permanent": {
                "type": "boolean",
                "description": (
                    "Delete irreversibly instead of using the recycle bin. Must remain false or "
                    "omitted unless the current user message explicitly requests permanent deletion."
                ),
            },
        },
        "required": ["action", "path"],
        "additionalProperties": False,
    }

    _DOCUMENT_EXTENSIONS = {
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        if arguments.get("action") == "delete" and arguments.get("permanent"):
            return Risk.HIGH
        return (
            Risk.SAFE
            if arguments.get("action") in {"map", "list", "read", "search"}
            else Risk.MEDIUM
        )

    def summarize(self, arguments: dict[str, Any]) -> str:
        labels = {"edit": "编辑", "write": "写入", "append": "追加", "mkdir": "创建目录"}
        return f"{labels.get(arguments.get('action'), '访问')}工作区文件：{arguments.get('path')}"

    @staticmethod
    def _resolve(workspace: str, requested: str) -> Path:
        root = Path(workspace).resolve()
        path = (root / requested).resolve()
        if path != root and root not in path.parents:
            raise PermissionError("Path escapes the configured workspace")
        return path

    @staticmethod
    def _is_sensitive(path: Path) -> bool:
        lowered_parts = {part.lower() for part in path.parts}
        name = path.name.lower()
        sensitive_runtime_file = name.startswith(
            (
                "provider-keys.json",
                "provider-secrets.vault",
                "gateway.token",
                "openclaw.json",
                "deepdesk.db",
                "audit.jsonl",
                "feishu-listener-token",
                "mobile-device-secrets.vault",
                "mobile-devices.json",
            )
        )
        return (
            (name.startswith(".env") and name != ".env.example")
            or sensitive_runtime_file
            or bool(lowered_parts & {".ssh", ".aws", ".azure", "credentials"})
            or name.endswith((".pem", ".p12", ".pfx", ".key"))
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        # Atomic replace/trash operations cannot be forcibly interrupted safely.
        # Keep the task active while an already-started operation settles; only
        # then propagate Stop. No file may change after the worker is retired.
        return await run_owned_thread(self._execute_sync, arguments, context)

    def _execute_sync(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        if arguments.get("action") in {"edit", "write", "append", "mkdir", "delete"}:
            path = self._resolve(context.workspace, arguments["path"])
            lock = _MUTATION_LOCKS[hash(os.path.normcase(str(path))) % len(_MUTATION_LOCKS)]
            with lock:
                return self._execute_locked(arguments, context)
        return self._execute_locked(arguments, context)

    def _execute_locked(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        from deepdesk.task_files import is_project_context, resolve_project_read

        path = (
            resolve_project_read(arguments["path"], context)
            if action == "read" and is_project_context(context)
            else self._resolve(context.workspace, arguments["path"])
        )
        if context.filesystem_scope:
            scope = self._resolve(context.workspace, context.filesystem_scope)
            if path != scope and scope not in path.parents:
                raise PermissionError(
                    f"Path is outside the task's explicit filesystem scope: "
                    f"{context.filesystem_scope}"
                )
        if self._is_sensitive(path):
            raise PermissionError("Credential and secret files are not accessible to agent tools")
        if action == "delete":
            return self._delete(path, context, bool(arguments.get("permanent", False)))
        if action == "map":
            return self._map(path, arguments)
        if action == "list":
            if not path.exists():
                raise FileNotFoundError(str(path))
            items = []
            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[
                :500
            ]:
                if self._traversal_candidate(path, item) is None:
                    continue
                stat = item.stat()
                items.append(
                    {
                        "name": item.name,
                        "type": "directory" if item.is_dir() else "file",
                        "size": stat.st_size,
                    }
                )
            return {"path": str(path), "items": items}
        if action == "read":
            if path.suffix.lower() in self._DOCUMENT_EXTENSIONS:
                return {
                    "path": str(path),
                    "binary": True,
                    "size": path.stat().st_size,
                    "message": (
                        "This is a binary document. Use document.inspect to extract its "
                        "text and structure; filesystem.read intentionally does not expose "
                        "raw binary bytes."
                    ),
                }
            size = path.stat().st_size
            with path.open("rb") as stream:
                head = stream.read(4096)
            if b"\x00" in head:
                return {
                    "path": str(path),
                    "binary": True,
                    "size": size,
                    "message": "Binary file content is not returned by filesystem.read.",
                }
            if size > 5_000_000:
                if "end_line" not in arguments:
                    raise ValueError(
                        "Large text files require start_line and end_line, or filesystem.search; "
                        "the file was not loaded into memory"
                    )
                start_line = int(arguments.get("start_line") or 1)
                end_line = int(arguments["end_line"])
                if end_line < start_line:
                    raise ValueError("end_line must be greater than or equal to start_line")
                selected: list[str] = []
                more_lines = False
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if line_number > end_line:
                            more_lines = True
                            break
                        if line_number >= start_line:
                            selected.append(line)
                focused = "".join(selected)
                character_truncated = len(focused) > 100_000
                if character_truncated:
                    focused = focused[:100_000] + "\n[truncated]"
                return {
                    "path": str(path),
                    "content": focused,
                    "start_line": start_line,
                    "end_line": min(end_line, start_line + max(0, len(selected) - 1)),
                    "total_lines": None,
                    "more_lines": more_lines,
                    "truncated": character_truncated or more_lines,
                    "streamed": True,
                }
            raw = path.read_bytes()
            content = raw.decode("utf-8", errors="replace")
            lines = content.splitlines(keepends=True)
            start_line = int(arguments.get("start_line") or 1)
            end_line = int(arguments.get("end_line") or len(lines) or 1)
            if end_line < start_line:
                raise ValueError("end_line must be greater than or equal to start_line")
            focused = "".join(lines[start_line - 1:end_line])
            character_truncated = len(focused) > 100_000
            if character_truncated:
                focused = focused[:100_000] + "\n[truncated]"
            return {
                "path": str(path),
                "content": focused,
                "start_line": start_line,
                "end_line": min(end_line, len(lines)),
                "total_lines": len(lines),
                "truncated": character_truncated or end_line < len(lines),
            }
        if action == "search":
            return self._search(path, arguments)
        if action == "edit":
            return self._edit(path, arguments)
        if action == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
            return {"created": str(path)}
        content = arguments.get("content", "")
        path.parent.mkdir(parents=True, exist_ok=True)
        if action not in {"write", "append"}:
            raise ValueError(f"Unsupported filesystem action: {action}")
        # Write to a sibling and atomically replace the destination.  A model,
        # service, or PC crash can therefore leave either the previous complete
        # file or the new complete file, rather than a partially written one.
        temporary = path.with_name(f".{path.name}.elren-{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                if action == "append" and path.is_file():
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, handle, length=1024 * 1024)
                handle.write(str(content).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(path),
            "bytes": len(str(content).encode("utf-8")),
            "atomic": True,
        }

    def _traversal_candidate(self, root: Path, candidate: Path) -> Path | None:
        """Validate each visited target, not just the traversal's initial path.

        A harmless alias can point to a secret file or outside the requested
        subtree. Reparse directories must also be pruned before os.walk enters
        them (Windows junctions are not necessarily classified as symlinks).
        """
        try:
            resolved = candidate.resolve(strict=True)
            if resolved != root and root not in resolved.parents:
                return None
            if self._is_sensitive(candidate) or self._is_sensitive(resolved):
                return None
            return resolved
        except (OSError, RuntimeError):
            return None

    def _map(self, path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded, high-signal map of an unfamiliar workspace."""

        if not path.is_dir():
            raise NotADirectoryError(str(path))
        maximum = min(max(int(arguments.get("max_results", 200)), 1), 200)
        max_depth = min(max(int(arguments.get("max_depth", 4)), 1), 8)
        skipped = {
            ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
            ".gradle", ".kotlin", ".ruff_cache", "dist", "build", ".idea", ".vscode",
        }
        entrypoint_names = {
            "pyproject.toml", "package.json", "cargo.toml", "go.mod", "pom.xml",
            "build.gradle", "build.gradle.kts", "requirements.txt", "setup.py", "setup.cfg",
            "main.py", "app.py", "manage.py", "server.py", "index.html", "index.js", "index.ts",
            "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
            "dockerfile", "docker-compose.yml", "compose.yml", "readme.md", "agents.md",
        }
        file_rows: list[dict[str, Any]] = []
        directory_rows: list[str] = []
        entrypoints: list[str] = []
        test_paths: list[str] = []
        extensions: dict[str, int] = {}
        omitted = 0
        for current_root, directories, filenames in os.walk(path):
            current = Path(current_root)
            try:
                relative_root = current.relative_to(path)
            except ValueError:
                continue
            depth = 0 if relative_root == Path(".") else len(relative_root.parts)
            directories[:] = sorted(
                name for name in directories
                if name not in skipped and not name.startswith(".elren-")
                and self._traversal_candidate(path, current / name) is not None
            )
            if depth >= max_depth:
                omitted += len(directories)
                directories[:] = []
            for directory in directories:
                relative = (relative_root / directory).as_posix()
                directory_rows.append(relative)
                if directory.casefold() in {"test", "tests", "spec", "specs", "__tests__"}:
                    test_paths.append(relative)
            for filename in sorted(filenames):
                candidate = current / filename
                if self._traversal_candidate(path, candidate) is None:
                    continue
                relative = (relative_root / filename).as_posix()
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                suffix = candidate.suffix.casefold() or "[no extension]"
                extensions[suffix] = extensions.get(suffix, 0) + 1
                if filename.casefold() in entrypoint_names:
                    entrypoints.append(relative)
                if (
                    "test" in filename.casefold()
                    or any(part.casefold() in {"test", "tests", "spec", "specs", "__tests__"} for part in Path(relative).parts)
                ) and len(test_paths) < 40:
                    test_paths.append(relative)
                if len(file_rows) < maximum:
                    file_rows.append(
                        {"path": relative, "size": stat.st_size, "extension": suffix}
                    )
                else:
                    omitted += 1
        language_counts = [
            {"extension": extension, "files": count}
            for extension, count in sorted(
                extensions.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ]
        return {
            "root": str(path),
            "max_depth": max_depth,
            "entrypoints": list(dict.fromkeys(entrypoints))[:40],
            "test_paths": list(dict.fromkeys(test_paths))[:40],
            "languages_by_extension": language_counts,
            "directories": directory_rows[:maximum],
            "files": file_rows,
            "files_returned": len(file_rows),
            "items_omitted": omitted + max(0, len(directory_rows) - maximum),
            "bounded": True,
            "instruction": "Use focused filesystem.search/read calls on the relevant entries; do not read the whole repository.",
        }

    def _edit(self, path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.suffix.lower() in self._DOCUMENT_EXTENSIONS:
            raise ValueError("Binary documents must be edited with the document tool")
        before_stat = path.stat()
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            raise ValueError("Binary files cannot be edited as text")
        had_utf8_bom = raw.startswith(codecs.BOM_UTF8)
        try:
            original = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("The file is not valid UTF-8 text") from exc
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits is required for edit")
        updated = original
        applied: list[dict[str, int]] = []
        for index, edit in enumerate(edits, 1):
            if not isinstance(edit, dict):
                raise ValueError(f"edits[{index - 1}] must be an object")
            old_text = str(edit.get("old_text") or "")
            new_text = str(edit.get("new_text") or "")
            expected = int(edit.get("expected_count") or 1)
            if not old_text:
                raise ValueError(f"edits[{index - 1}].old_text must not be empty")
            actual = updated.count(old_text)
            if actual != expected:
                raise ValueError(
                    f"Edit {index} expected {expected} exact match(es), found {actual}; file unchanged"
                )
            updated = updated.replace(old_text, new_text, expected)
            applied.append(
                {
                    "index": index,
                    "replacements": expected,
                    "old_characters": len(old_text),
                    "new_characters": len(new_text),
                }
            )
        if updated == original:
            return {"path": str(path), "changed": False, "edits": applied, "atomic": True}
        output_bytes = (codecs.BOM_UTF8 if had_utf8_bom else b"") + updated.encode("utf-8")
        temporary = path.with_name(f".{path.name}.elren-{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(output_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic replace alone prevents partial files, not lost updates. An
            # editor may have saved (or removed/replaced) the target while we
            # prepared the sibling. Refuse a stale commit and keep its version.
            try:
                current_stat = path.stat()
                unchanged = (
                    (current_stat.st_dev, current_stat.st_ino, current_stat.st_mtime_ns, current_stat.st_size)
                    == (before_stat.st_dev, before_stat.st_ino, before_stat.st_mtime_ns, before_stat.st_size)
                    and path.read_bytes() == raw
                )
            except OSError:
                unchanged = False
            if not unchanged:
                raise ValueError(
                    "The file changed during edit; no edit was committed. Read the latest file and retry."
                )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(path),
            "changed": True,
            "edits": applied,
            "before_bytes": len(raw),
            "after_bytes": len(output_bytes),
            "atomic": True,
        }

    def _delete(self, path: Path, context: ToolContext, permanent: bool) -> dict[str, Any]:
        workspace_root = Path(context.workspace).resolve()
        if path == workspace_root:
            raise PermissionError("Deleting the configured workspace root is not allowed")
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(str(path))
        if permanent:
            if not allows_permanent_delete(context.user_prompt):
                raise PermissionError(
                    "Permanent deletion requires an explicit request in the current user message. "
                    "Use permanent=false to move the item to the operating system Trash."
                )
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            return {
                "deleted": str(path),
                "destination": "permanent",
                "recoverable": False,
            }

        destination = self._send_to_recycle_bin(path) or (
            "windows_recycle_bin" if os.name == "nt" else "operating_system_trash"
        )
        return {
            "deleted": str(path),
            "destination": destination,
            "recoverable": True,
        }

    @staticmethod
    def _send_to_recycle_bin(path: Path) -> str:
        if sys.platform == "darwin":
            trash = Path.home() / ".Trash"
            trash.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = trash / path.name
            if target.exists() or target.is_symlink():
                target = trash / f"{path.stem}-{uuid4().hex[:10]}{path.suffix}"
            shutil.move(str(path), str(target))
            return "macos_trash"
        if os.name != "nt":
            raise RuntimeError("The operating system Trash is unavailable on this platform")
        # SHFileOperationW expects a double-NUL-terminated path list. Python adds
        # the final terminator, so the explicit trailing NUL below creates two.
        operation = _SHFileOperationW()
        operation.wFunc = 3  # FO_DELETE
        operation.pFrom = str(path.resolve()) + "\0"
        operation.pTo = None
        operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO, no UI
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result != 0:
            raise OSError(result, f"Windows recycle-bin operation failed for {path}")
        if operation.fAnyOperationsAborted:
            raise OSError(f"Windows recycle-bin operation was cancelled for {path}")
        return "windows_recycle_bin"

    def _search(self, path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        if not query:
            raise ValueError("query is required for search")
        filename_glob = str(arguments.get("glob") or "*")
        case_sensitive = bool(arguments.get("case_sensitive", False))
        use_regex = bool(arguments.get("regex", False))
        maximum = min(max(int(arguments.get("max_results", 80)), 1), 200)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if use_regex else re.escape(query), flags)
        skipped_directories = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
        matches: list[dict[str, Any]] = []
        files_scanned = 0

        skipped_directories.update({".gradle", ".kotlin", ".ruff_cache", "dist", "build"})
        if not path.exists():
            raise FileNotFoundError(str(path))

        def candidate_files():
            if path.is_file():
                yield path
                return
            for root, directories, filenames in os.walk(path):
                root_path = Path(root)
                directories[:] = [
                    name for name in directories
                    if name not in skipped_directories
                    and self._traversal_candidate(path, root_path / name) is not None
                ]
                for filename in filenames:
                    if fnmatch.fnmatch(filename, filename_glob):
                        yield root_path / filename

        files_considered = 0
        scan_limit_reached = False
        for candidate in candidate_files():
            if len(matches) >= maximum:
                break
            files_considered += 1
            if files_considered > 20_000:
                scan_limit_reached = True
                break
            resolved = self._traversal_candidate(path if path.is_dir() else path.parent, candidate)
            if resolved is None:
                continue
            try:
                if resolved.stat().st_size > 2_000_000:
                    continue
                raw = resolved.read_bytes()
                if b"\x00" in raw[:4096]:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            files_scanned += 1
            for line_number, line in enumerate(text.splitlines(), 1):
                if not pattern.search(line):
                    continue
                matches.append(
                    {"path": str(candidate), "line": line_number, "text": line[:300]}
                )
                if len(matches) >= maximum:
                    break
        return {
            "path": str(path),
            "query": query,
            "matches": matches,
            "match_count": len(matches),
            "files_scanned": files_scanned,
            "files_considered": files_considered,
            "truncated": len(matches) >= maximum or scan_limit_reached,
            "scan_limit_reached": scan_limit_reached,
        }
