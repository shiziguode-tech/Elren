from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.subprocess_env import credential_safe_environment


class MacOSUITool(ToolPlugin):
    name = "macos_ui"
    description = (
        "Inspect and operate macOS applications through System Events Accessibility. "
        "The user must grant Elren Accessibility and Automation permission in System Settings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "list_apps", "list_windows", "activate", "click", "type", "close_window"],
            },
            "application": {"type": "string", "description": "Exact application process name"},
            "window": {"type": "string", "description": "Exact window title"},
            "control": {"type": "string", "description": "Accessible UI element description or title"},
            "text": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict[str, Any]) -> Risk:
        return Risk.SAFE if arguments.get("action") in {"status", "list_apps", "list_windows"} else Risk.MEDIUM

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"Run macOS Accessibility action: {arguments.get('action')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await asyncio.to_thread(self._execute_sync, arguments)

    @staticmethod
    def _run_script(source: str, *values: str) -> str:
        if sys.platform != "darwin":
            raise RuntimeError("macOS Accessibility is only available on macOS")
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e",
             "function run(argv) {\n" + source + "\n}", *values],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=credential_safe_environment(),
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            if "not allowed assistive access" in detail.lower() or "-1719" in detail:
                raise PermissionError(
                    "Grant Elren Accessibility permission in System Settings > Privacy & Security > Accessibility"
                )
            raise RuntimeError(detail or f"osascript exited {result.returncode}")
        return result.stdout.strip()

    def _execute_sync(self, arguments: dict[str, Any]) -> Any:
        action = arguments["action"]
        if action == "status":
            source = 'ObjC.import("ApplicationServices"); return JSON.stringify({trusted: $.AXIsProcessTrusted()})'
            return json.loads(self._run_script(source))
        if action == "list_apps":
            source = '''
const se = Application("System Events");
return JSON.stringify(se.applicationProcesses.whose({visible: true})().map(p => ({name:p.name(), frontmost:p.frontmost()})));
'''
            return {"applications": json.loads(self._run_script(source))}
        app = str(arguments.get("application") or "").strip()
        if not app:
            raise ValueError("application is required")
        if action == "list_windows":
            source = '''
const se=Application("System Events"), p=se.applicationProcesses.byName(argv[0]);
return JSON.stringify(p.windows().map(w => ({title:w.name(), position:w.position(), size:w.size()})));
'''
            return {"application": app, "windows": json.loads(self._run_script(source, app))}
        if action == "activate":
            source = 'Application(argv[0]).activate(); return JSON.stringify({ok:true})'
            return json.loads(self._run_script(source, app))
        window = str(arguments.get("window") or "").strip()
        if not window:
            raise ValueError("window is required")
        if action == "close_window":
            source = '''
const se=Application("System Events"), p=se.applicationProcesses.byName(argv[0]);
const w=p.windows.whose({name:argv[1]})()[0]; if(!w) throw new Error("Window not found"); w.actions.byName("AXClose").perform(); return JSON.stringify({ok:true});
'''
            return json.loads(self._run_script(source, app, window))
        control = str(arguments.get("control") or "").strip()
        if not control:
            raise ValueError("control is required")
        if action == "click":
            source = '''
const se=Application("System Events"), p=se.applicationProcesses.byName(argv[0]);
const w=p.windows.whose({name:argv[1]})()[0]; if(!w) throw new Error("Window not found"); const xs=w.entireContents().filter(x => { try { return x.name()===argv[2] || x.description()===argv[2]; } catch(e) { return false; }});
if(!xs.length) throw new Error("Control not found"); xs[0].actions.byName("AXPress").perform(); return JSON.stringify({ok:true});
'''
            return json.loads(self._run_script(source, app, window, control))
        if action == "type":
            source = '''
const se=Application("System Events"), p=se.applicationProcesses.byName(argv[0]);
const w=p.windows.whose({name:argv[1]})()[0]; if(!w) throw new Error("Window not found"); const xs=w.entireContents().filter(x => { try { return x.name()===argv[2] || x.description()===argv[2]; } catch(e) { return false; }});
if(!xs.length) throw new Error("Control not found"); xs[0].value=argv[3]; return JSON.stringify({ok:true, verified:String(xs[0].value())===argv[3]});
'''
            return json.loads(self._run_script(source, app, window, control, str(arguments.get("text") or "")))
        raise ValueError(f"Unsupported macOS UI action: {action}")
