from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from deepdesk.models import Risk
from deepdesk.platform_paths import windows_program_files, windows_root
from deepdesk.plugins.base import ToolContext, ToolPlugin, run_owned_thread
from deepdesk.subprocess_env import credential_safe_environment

_SCRIPT_OR_COMMAND_LAUNCHERS = {
    "7z",
    "at",
    "bash",
    "bun",
    "bitsadmin",
    "certutil",
    "cmstp",
    "control",
    "cscript",
    "cmd",
    "cmake",
    "command",
    "composer",
    "curl",
    "deno",
    "dotnet",
    "fish",
    "forfiles",
    "git",
    "hg",
    "hh",
    "installutil",
    "java",
    "javac",
    "jshell",
    "lua",
    "make",
    "msbuild",
    "msdt",
    "mshta",
    "msiexec",
    "ninja",
    "node",
    "nodejs",
    "npm",
    "npx",
    "odbcconf",
    "perl",
    "pcalua",
    "php",
    "pip",
    "pip3",
    "pnpm",
    "poetry",
    "powershell",
    "pwsh",
    "py",
    "python",
    "python3",
    "pythonw",
    "regasm",
    "reg",
    "regedit",
    "regsvcs",
    "regsvr32",
    "robocopy",
    "ruby",
    "rundll32",
    "schtasks",
    "sc",
    "sh",
    "svn",
    "tar",
    "tclsh",
    "uv",
    "wget",
    "wmic",
    "wscript",
    "xcopy",
    "yarn",
    "zsh",
    "osascript",
    "terminal",
    "iterm",
    "iterm2",
    "script editor",
    "automator",
}


def _executable_stem(path: str) -> str:
    name = Path(path).name.casefold()
    for suffix in (".exe", ".com", ".cmd", ".bat", ".ps1", ".sh", ".app"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _is_script_or_command_launcher(path: str) -> bool:
    stem = _executable_stem(path)
    if stem in _SCRIPT_OR_COMMAND_LAUNCHERS:
        return True
    return bool(
        re.fullmatch(
            r"(?:python|pythonw|python3|node|nodejs|php|ruby|perl|pip|pip3)[0-9.\-_]*",
            stem,
        )
    )


class ProcessManagerTool(ToolPlugin):
    name = "process_manager"
    description = (
        "List running processes, launch an application without a shell, or terminate one by PID. "
        "On Windows, launch also correlates newly-created top-level window handles so packaged "
        "apps can be controlled without shell/PID guessing. Termination is high risk and protected "
        "by approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "launch", "terminate"]},
            "application": {"type": "string", "description": "Executable path/name for launch"},
            "arguments": {"type": "array", "items": {"type": "string"}},
            "pid": {"type": "integer", "minimum": 1},
            "name_filter": {"type": "string"},
            "background": {
                "type": "boolean",
                "description": "Launch without activating the app window when supported; default true",
            },
            "track_window": {
                "type": "boolean",
                "description": "Correlate top-level windows created by launch; Windows default true",
            },
            "window_wait_seconds": {
                "type": "number",
                "minimum": 0,
                "maximum": 10,
                "description": "Maximum window-correlation wait; default 3 seconds",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        action = arguments.get("action")
        if action == "list":
            return Risk.SAFE
        if action == "terminate":
            return Risk.HIGH
        return Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        action = arguments.get("action")
        if action == "launch":
            return f"启动应用：{arguments.get('application')}"
        if action == "terminate":
            return f"终止进程 PID {arguments.get('pid')}"
        return "查看正在运行的进程"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await run_owned_thread(self._execute_sync, arguments, context)

    @staticmethod
    def _resolve_application(application: str) -> str:
        if sys.platform == "darwin":
            requested = str(application).strip()
            aliases = {"edge": "Microsoft Edge", "msedge": "Microsoft Edge", "chrome": "Google Chrome",
                       "notepad": "TextEdit", "textedit": "TextEdit", "safari": "Safari", "finder": "Finder"}
            name = aliases.get(requested.casefold(), requested)
            candidate = Path(name).expanduser()
            candidates = [candidate]
            if not candidate.is_absolute() and len(candidate.parts) == 1:
                leaf = name if name.endswith(".app") else name + ".app"
                candidates += [root / leaf for root in (
                    Path("/Applications"), Path.home() / "Applications", Path("/System/Applications"),
                    Path("/System/Applications/Utilities"), Path("/System/Library/CoreServices"),
                )]
            for candidate in candidates:
                if candidate.suffix == ".app" and (candidate / "Contents/Info.plist").is_file():
                    return str(candidate.resolve())
                if candidate.is_file():
                    return str(candidate.resolve())
            resolved = shutil.which(name)
            if resolved:
                return resolved
            raise FileNotFoundError(f"Application not found: {requested}")
        aliases = {
            "msedge": "msedge.exe",
            "edge": "msedge.exe",
            "microsoft edge": "msedge.exe",
            "chrome": "chrome.exe",
            "google chrome": "chrome.exe",
            "notepad": "notepad.exe",
        }
        application = aliases.get(str(application).strip().lower(), str(application).strip())
        candidate = Path(application).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(application)
        if resolved:
            return resolved
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA")
            program_roots = windows_program_files()
            system_root = windows_root()
            known: dict[str, list[Path]] = {
                "msedge.exe": [
                    *(root / "Microsoft/Edge/Application/msedge.exe" for root in program_roots),
                    *(
                        [Path(local_app_data) / "Microsoft/Edge/Application/msedge.exe"]
                        if local_app_data
                        else []
                    ),
                ],
                "chrome.exe": [
                    *(root / "Google/Chrome/Application/chrome.exe" for root in program_roots),
                    *(
                        [Path(local_app_data) / "Google/Chrome/Application/chrome.exe"]
                        if local_app_data
                        else []
                    ),
                ],
                "notepad.exe": [system_root / "System32/notepad.exe"] if system_root else [],
            }
            for path in known.get(application.lower(), []):
                if path.is_file():
                    return str(path)
        raise FileNotFoundError(f"Application not found: {application}")

    @staticmethod
    def _visible_windows() -> dict[int, dict[str, Any]]:
        if os.name != "nt":
            return {}
        try:
            from pywinauto import Desktop

            result = {}
            for window in Desktop(backend="uia").windows():
                try:
                    title = (window.window_text() or "").strip()
                    if not title or not window.is_visible():
                        continue
                    result[int(window.handle)] = {
                        "handle": int(window.handle),
                        "title": title,
                        "process_id": int(window.process_id()),
                        "class_name": str(window.element_info.class_name or ""),
                    }
                except Exception:
                    continue
            return result
        except Exception:
            return {}

    @staticmethod
    def _execute_sync(arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "list":
            name_filter = arguments.get("name_filter", "").lower()
            items = []
            for process in psutil.process_iter(["pid", "name", "status"]):
                try:
                    info = process.info
                    if name_filter and name_filter not in (info.get("name") or "").lower():
                        continue
                    items.append(info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return {"processes": sorted(items, key=lambda item: item["pid"])[:300]}
        if action == "launch":
            application = arguments.get("application")
            if not application:
                raise ValueError("application is required for launch")
            resolved_application = ProcessManagerTool._resolve_application(application)
            if _is_script_or_command_launcher(resolved_application):
                raise PermissionError(
                    "process_manager cannot launch command shells, script interpreters, package "
                    "managers, build runners, or file-transfer utilities. Run scripts through "
                    "shell/sandbox so the host owns and terminates the complete process tree."
                )
            if sys.platform == "darwin" and Path(resolved_application).suffix == ".app":
                bundle = Path(resolved_application)
                with (bundle / "Contents/Info.plist").open("rb") as stream:
                    metadata = plistlib.load(stream)
                binary = str(metadata.get("CFBundleExecutable") or "")
                if not binary or Path(binary).name != binary or _is_script_or_command_launcher(binary):
                    raise PermissionError("process_manager cannot launch script/command app bundles")
                executable = (bundle / "Contents/MacOS" / binary).resolve(strict=True)
                executable.relative_to(bundle.resolve())
                background = bool(arguments.get("background", True))
                command = ["/usr/bin/open", *(["-g"] if background else []), "-a", str(bundle)]
                if arguments.get("arguments"):
                    command += ["--args", *arguments["arguments"]]
                result = subprocess.run(command, cwd=context.workspace, env=credential_safe_environment(),
                                        capture_output=True, text=True, timeout=30, check=False)
                if result.returncode:
                    raise RuntimeError(f"macOS application launch failed: {result.stderr.strip()[:500]}")
                return {"application": application, "resolved_application": resolved_application,
                        "launch_requested": True, "pid": None, "background_requested": background,
                        "created_windows": [], "window_tracking": {"enabled": False,
                            "note": "LaunchServices may reuse an existing app; use macos_ui to verify its window, not the transient open PID."}}
            startupinfo = None
            creationflags = 0
            background = bool(arguments.get("background", True))
            track_window = bool(arguments.get("track_window", os.name == "nt"))
            windows_before = ProcessManagerTool._visible_windows() if track_window else {}
            if os.name == "nt" and background:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                [resolved_application, *arguments.get("arguments", [])],
                cwd=context.workspace,
                shell=False,
                env=credential_safe_environment(),
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            created_windows: list[dict[str, Any]] = []
            if track_window:
                wait_seconds = float(arguments.get("window_wait_seconds", 3))
                deadline = time.monotonic() + wait_seconds
                while True:
                    windows_after = ProcessManagerTool._visible_windows()
                    created_windows = [
                        {
                            **details,
                            "correlation": "appeared_after_this_launch",
                        }
                        for handle, details in windows_after.items()
                        if handle not in windows_before
                    ]
                    if created_windows or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
            return {
                "pid": process.pid,
                "application": application,
                "resolved_application": resolved_application,
                "background_requested": background,
                "created_windows": created_windows,
                "window_tracking": {
                    "enabled": track_window,
                    "wait_seconds": float(arguments.get("window_wait_seconds", 3)),
                    "note": (
                        "Use each returned handle directly with windows_ui. The window process_id "
                        "may differ from launcher pid for packaged applications."
                    ),
                },
            }
        if action == "terminate":
            pid = int(arguments.get("pid", 0))
            protected = {os.getpid(), os.getppid()}
            if pid in protected:
                raise PermissionError("Elren cannot terminate itself or its parent process")
            process = psutil.Process(pid)
            name = process.name()
            process.terminate()
            return {"pid": pid, "name": name, "termination_requested": True}
        raise ValueError(f"Unsupported process action: {action}")
