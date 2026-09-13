from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate
from deepdesk.models import AgentTask, Risk, TaskEvent
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import ToolPlugin
from deepdesk.subagents import SubagentOrchestrator, load_subagent_catalog


async def engine_case(root, name, prompt, calls):
    class Tool(ToolPlugin):
        description = "Synthetic evidence fixture"
        parameters = {"type": "object", "properties": {}}

        def __init__(self):
            self.attempts = {}

        def risk(self, arguments):
            return Risk.SAFE

        async def execute(self, arguments, context):
            if self.name == "sandbox":
                command = arguments["command"]
                self.attempts[command] = self.attempts.get(command, 0) + 1
                return {"exit_code": int("test_api" in command and self.attempts[command] == 1)}
            if self.name == "background_browser":
                return {"url": arguments["url"], "foreground_used": False,
                        "browser_ui_opened": False, "isolated_profile": True}
            if self.name == "filesystem" and arguments.get("action") == "write":
                (Path(context.workspace) / arguments["path"]).write_text(arguments["content"], encoding="utf-8")
            return {"path": arguments["path"], "written": True}

    class Client:
        keys = []

        def __init__(self):
            self.sequence = iter(calls)

        async def chat(self, messages, tools):
            item = next(self.sequence)
            if isinstance(item, str):
                message = {"role": "assistant", "content": item}
            else:
                tool, args = item
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{len(messages)}", "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)}}]}
            return DeepSeekReply(message=message, usage={}, active_key="synthetic")

    async def no_network(self):
        return SimpleNamespace(country_code="UNKNOWN")

    registry = PluginRegistry()
    for tool_name in {item[0] for item in calls if not isinstance(item, str)}:
        tool = Tool()
        tool.name = tool_name
        registry.register(tool)
    task = AgentTask(prompt=prompt)
    engine = AgentEngine(client=Client(), registry=registry, approvals=ApprovalGate(),
                         human_actions=HumanActionGate(), audit=AuditLog(root / "audit.jsonl"),
                         workspace=str(root), max_steps=None,
                         emit=lambda task, kind, data: task.events.append(TaskEvent(type=kind, data=data)))
    with patch("deepdesk.engine.EgressRegionDetector.detect", no_network):
        await engine.run(task, asyncio.Event())
    return {"status": str(task.status), "warnings": [event.data for event in task.events if event.type == "verification_unresolved"],
            "preview_required": sum(event.type == "background_preview_required" for event in task.events)}


@pytest.mark.asyncio
async def test_unrelated_passing_test_preserves_failed_target(tmp_path):
    result = await engine_case(tmp_path, "different", "Fix the API and run its tests", [
        ("sandbox", {"command": "python -m pytest tests/test_api.py"}),
        ("sandbox", {"command": "python -m pytest tests/test_format.py"}),
        "Implemented and all tests passed.",
    ])
    assert result["warnings"], "Unrelated success erased the API test failure"


@pytest.mark.asyncio
async def test_same_target_passing_rerun_clears_failure(tmp_path):
    result = await engine_case(tmp_path, "rerun", "Fix the API and run its tests", [
        ("sandbox", {"command": "python -m pytest tests/test_api.py"}),
        ("sandbox", {"command": "python -m pytest tests/test_api.py"}),
        "Implemented and verified.",
    ])
    assert result["warnings"] == []
    assert result["status"] == "TaskStatus.COMPLETED"


@pytest.mark.asyncio
async def test_unrelated_or_stale_preview_cannot_complete_web_work(tmp_path):
    result = await engine_case(tmp_path, "preview", "Build a website project", [
        ("background_browser", {"action": "open", "url": "https://example.invalid/docs"}),
        ("filesystem", {"action": "write", "path": "app.html", "content": "broken"}),
        "Implemented and verified.",
        "Implemented and verified.",
        "Implemented and verified.",
    ])
    assert result["status"] != "TaskStatus.COMPLETED"
    assert result["preview_required"] > 0


def test_verification_identity_keeps_different_commands_separate():
    a = AgentEngine._verification_target("sandbox", {"command": "pytest tests/a.py"}, {})
    b = AgentEngine._verification_target("sandbox", {"command": "pytest tests/b.py"}, {})
    assert a != b
    assert a == AgentEngine._verification_target("sandbox", {"command": "pytest tests/a.py"}, {})
    assert "pytest" not in a


def test_semantic_review_resolves_only_its_screenshot(tmp_path):
    screenshot = str(tmp_path / "preview.png")
    captured = AgentEngine._verification_target("background_browser", {"action": "screenshot"},
                                                {"result": {"screenshot": screenshot}})
    reviewed = AgentEngine._verification_target("vision", {"image_path": screenshot}, {})
    other = AgentEngine._verification_target("vision", {"image_path": str(tmp_path / "other.png")}, {})
    assert captured == reviewed
    assert captured != other


@pytest.mark.asyncio
async def test_successful_local_preview_expires_after_file_edit(tmp_path):
    result = await engine_case(tmp_path, "stale", "Build a website project", [
        ("background_browser", {"action": "open", "url": "http://127.0.0.1:8080"}),
        ("filesystem", {"action": "write", "path": "app.html", "content": "broken"}),
        "Implemented and verified.", "Implemented and verified.", "Implemented and verified.",
    ])
    assert result["status"] != "TaskStatus.COMPLETED"
    assert result["preview_required"] > 0


def test_evidence_package_is_bounded_redacted_and_sourced():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "read-1", "function": {
            "name": "filesystem", "arguments": json.dumps({"action": "read", "path": "src/api.py"})}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "CODE_SENTINEL SECRET " + "x" * 50000},
        {"role": "system", "content": "HIDDEN_SYSTEM_SENTINEL"},
    ]
    package = AgentEngine._specialist_evidence(messages, lambda value: str(value).replace("SECRET", "[REDACTED]"))
    encoded = json.dumps(package)
    assert "CODE_SENTINEL" in encoded
    assert "src/api.py" in encoded and "read-1" in encoded
    assert "SECRET" not in encoded and "HIDDEN_SYSTEM_SENTINEL" not in encoded
    assert len(encoded) < 24000


@pytest.mark.asyncio
async def test_report_only_child_receives_explicit_evidence_without_tools():
    captured = {}

    class Client:
        async def chat(self, messages, tools):
            captured.update(messages=messages, tools=tools)
            return DeepSeekReply(message={"role": "assistant", "content": '{"summary":"Reviewed supplied evidence"}'}, usage={}, active_key="synthetic")

    async def audit(*args):
        pass

    preset = load_subagent_catalog()[0]
    await SubagentOrchestrator(Client()).delegate(
        AgentTask(prompt="Review code", active_model="synthetic"),
        [{"preset_id": preset.id, "assignment": "Review the supplied code"}],
        allowed_ids={preset.id}, cancel=asyncio.Event(), emit=lambda *args: None,
        audit=audit, redact=lambda value: value,
        evidence=[{"source": "filesystem read src/api.py", "excerpt": "CODE_SENTINEL"}],
    )
    assert "CODE_SENTINEL" in json.dumps(captured["messages"])
    assert captured["tools"] == []
    assert "untrusted" in captured["messages"][0]["content"]


def test_file_preview_must_point_inside_project(tmp_path):
    task = AgentTask(prompt="Build a website project")
    result = {"ok": True, "result": {"url": (tmp_path.parent / "other.html").as_uri()}}
    assert not AgentEngine._preview_matches_target(task, result, str(tmp_path))
    result["result"]["url"] = (tmp_path / "app.html").as_uri()
    assert AgentEngine._preview_matches_target(task, result, str(tmp_path))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8081/app", "http://127.0.0.1:8080/docs"])
def test_explicit_preview_url_rejects_other_local_port_or_path(tmp_path, url):
    task = AgentTask(prompt="Preview http://127.0.0.1:8080/app")
    assert not AgentEngine._preview_matches_target(task, {"result": {"url": url}}, str(tmp_path))


def test_explicit_preview_url_accepts_matching_origin_and_path(tmp_path):
    task = AgentTask(prompt="Preview http://localhost:80/app/")
    assert AgentEngine._preview_matches_target(task, {"result": {"url": "http://LOCALHOST/app?mode=test"}}, str(tmp_path))


def test_file_preview_tracks_successfully_written_html_only(tmp_path):
    edited = tmp_path / "app.html"
    unrelated = tmp_path / "old.html"
    edited.write_text("<html>new</html>", encoding="utf-8")
    unrelated.write_text("<html>old</html>", encoding="utf-8")
    task = AgentTask(prompt="Build a website project")
    tracked = {str(edited.resolve())}
    assert not AgentEngine._preview_matches_target(task, {"result": {"url": unrelated.as_uri()}}, str(tmp_path), tracked)
    assert AgentEngine._preview_matches_target(task, {"result": {"url": edited.as_uri()}}, str(tmp_path), tracked)
    # Existing projects with no changes still support read-only preview.
    assert AgentEngine._preview_matches_target(task, {"result": {"url": unrelated.as_uri()}}, str(tmp_path), set())


def test_only_successful_existing_project_html_enters_target_set(tmp_path):
    html = tmp_path / "app.html"
    html.write_text("<html></html>", encoding="utf-8")
    args = {"action": "write", "path": "app.html"}
    assert AgentEngine._html_mutation_target("filesystem", args, {"ok": True}, str(tmp_path)) == str(html.resolve())
    assert AgentEngine._html_mutation_target("filesystem", args, {"ok": False}, str(tmp_path)) is None
    assert AgentEngine._html_mutation_target("filesystem", {**args, "action": "read"}, {"ok": True}, str(tmp_path)) is None
    assert AgentEngine._html_mutation_target("filesystem", {**args, "path": "missing.html"}, {"ok": True}, str(tmp_path)) is None


@pytest.mark.asyncio
async def test_engine_rejects_other_file_after_successful_html_write(tmp_path):
    other = tmp_path / "unrelated.html"
    other.write_text("<html>old page</html>", encoding="utf-8")
    result = await engine_case(tmp_path, "wrong-file", "Build a website project", [
        ("filesystem", {"action": "write", "path": "app.html", "content": "<html>new page</html>"}),
        ("background_browser", {"action": "open", "url": other.as_uri()}),
        "Implemented and verified.", "Implemented and verified.", "Implemented and verified.",
    ])
    assert (tmp_path / "app.html").is_file()
    assert result["status"] != "TaskStatus.COMPLETED"
    assert result["preview_required"] > 0


@pytest.mark.parametrize("tool,args", [
    ("filesystem", {"action": "write", "path": "app.html"}),
    ("filesystem", {"action": "edit", "path": "src/main.tsx"}),
    ("shell", {"command": "npm run build"}),
])
def test_web_mutations_invalidate_previous_preview(tool, args):
    assert AgentEngine._invalidates_preview(tool, args, {"ok": True})
    assert not AgentEngine._invalidates_preview(tool, args, {"ok": False})
