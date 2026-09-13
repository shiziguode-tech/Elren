"""Project binding is host-selected, durable, and isolated from app state."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from deepdesk.audit import AuditLog
from deepdesk.config import Settings
from deepdesk.deepseek import DeepSeekReply
from deepdesk.egress_region import EgressRegion
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.main import create_app
from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, CreateTaskRequest, TaskStatus
from deepdesk.plugins import PluginRegistry, ToolContext
from deepdesk.plugins.builtin.filesystem import FileSystemTool
from deepdesk.plugins.builtin.shell import ShellTool
from deepdesk.task_projects import normalize_project_path
from deepdesk.task_store import TaskStore
from deepdesk.windows_sandbox import WindowsJobSandbox

POLICY, PROFILE = ApprovalPolicy.AUTONOMOUS, AgentProfile.GENERAL


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_project_compatible(value):
    assert normalize_project_path(value) == ""
    assert CreateTaskRequest(prompt="Hello", project_path=value).project_path == value
    assert AgentTask(prompt="Old persisted task").project_path == ""


def test_path_validation(tmp_path):
    repo = tmp_path / "repo 中文 with spaces"
    repo.mkdir()
    assert normalize_project_path(str(repo)) == str(repo.resolve())
    file = repo / "code.py"
    file.write_text("print(1)")
    for invalid in ("relative/path", r"\\untrusted-server\share\project", str(repo.anchor), str(Path.home()), str(file), str(repo / "missing"), str(repo / ".." / repo.name)):
        with pytest.raises(ValueError):
            normalize_project_path(invalid)


def test_link_path_rejected(tmp_path):
    target, link = tmp_path / "real", tmp_path / "redirect"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(ValueError, match="links or junctions"):
        normalize_project_path(str(link))


@pytest.mark.asyncio
async def test_project_api_local_origin_validation_and_no_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(_env_file=None, deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False,
        deepseek_api_key="", deepseek_backup_api_key=""))
    repo = tmp_path / "repo"
    repo.mkdir()
    headers = {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        result = await client.post("/api/projects/validate", json={"project_path": str(repo)}, headers=headers)
        assert result.status_code == 200
        assert result.json() == {"project_path": str(repo.resolve()), "name": "repo"}
        assert (await client.post("/api/projects/validate", json={"project_path": str(repo)})).status_code == 403
        assert (await client.post("/api/projects/validate", json={"project_path": str(repo)}, headers={**headers, "Origin": "http://localhost"})).status_code == 403
        assert (await client.post("/api/projects/validate", json={"project_path": "relative"}, headers=headers)).status_code == 422
        assert (await client.post("/api/projects/validate", json={"project_path": "x" * 4097}, headers=headers)).status_code == 422
        assert (await client.post("/api/tasks", json={"prompt": "Hello", "project_path": str(repo)})).status_code == 403
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.post("/api/projects/validate", json={"project_path": str(repo)}, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_task_creation_and_same_conversation_api_preserve_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(Settings(_env_file=None, deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False,
        deepseek_api_key="", deepseek_backup_api_key=""))
    manager = app.state.manager
    monkeypatch.setattr(manager.engine.client, "has_model_credentials", lambda _model: True)
    async def run(task, cancel):
        task.status = TaskStatus.COMPLETED
        task.result = "Synthetic API transport test"
    monkeypatch.setattr(manager.engine, "run", run)
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    headers = {"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        result = await client.post("/api/tasks", json={"prompt": "Read code", "project_path": str(repo)}, headers=headers)
        assert result.status_code == 200, result.text
        task = result.json()
        assert task["project_path"] == str(repo.resolve())
        await manager.background[task["id"]]
        denied = await client.post(f"/api/tasks/{task['id']}/continue", json={"prompt": "Continue", "project_path": str(other)}, headers=headers)
        assert denied.status_code == 409
        continued = await client.post(f"/api/tasks/{task['id']}/continue", json={"prompt": "Continue"}, headers=headers)
        assert continued.status_code == 200, continued.text
        assert continued.json()["id"] == task["id"]
        assert continued.json()["project_path"] == task["project_path"]
        await manager.background[task["id"]]
        assert manager.store.get(task["id"]).project_path == task["project_path"]


@pytest.mark.asyncio
async def test_two_real_engines_project_rules_files_and_continuation(tmp_path, monkeypatch):
    async def region(_self):
        return EgressRegion("US")
    monkeypatch.setattr("deepdesk.engine.EgressRegionDetector.detect", region)
    app_root = tmp_path / "application"
    app_root.mkdir()
    store = TaskStore(app_root / "tasks.sqlite")
    captured = {}

    class Provider:
        keys = []
        async def chat(self, messages, tools):
            project = "repo-alpha" if "RULE-ALPHA" in messages[0]["content"] else "repo-beta"
            captured[project] = messages.copy()
            if not any(message.get("role") == "tool" for message in messages):
                return DeepSeekReply(message={"role": "assistant", "content": None, "tool_calls": [{"id": "read", "type": "function", "function": {"name": "filesystem", "arguments": json.dumps({"action": "read", "path": "sample.txt"})}}]}, usage={}, active_key="synthetic")
            return DeepSeekReply(message={"role": "assistant", "content": "I read the requested local file."}, usage={}, active_key="synthetic")

    registry, approvals = PluginRegistry(), ApprovalGate()
    registry.register(FileSystemTool())
    engine = AgentEngine(client=Provider(), registry=registry, approvals=approvals,
        human_actions=HumanActionGate(), audit=AuditLog(app_root / "audit.jsonl"),
        workspace=str(app_root), max_steps=4, emit=lambda task, kind, data: manager.emit_async(task, kind, data))
    def cross_context(*args, **kwargs):
        raise AssertionError("Selected repositories must not retrieve global history")
    manager = TaskManager(engine, approvals, store=store, cross_context_provider=cross_context)
    tasks = []
    for suffix in ("alpha", "beta"):
        repo = tmp_path / f"repo-{suffix}"
        repo.mkdir()
        (repo / "AGENTS.md").write_text(f"RULE-{suffix.upper()}: keep the existing code style.")
        (repo / "sample.txt").write_text(f"ONLY-{suffix.upper()}")
        tasks.append(await manager.create_async("Read sample.txt with filesystem.read", POLICY, PROFILE,
            project_path=str(repo), model_preference="deepseek-v4-flash"))
    await asyncio.gather(*(manager.background[task.id] for task in tasks))
    for task, suffix in zip(tasks, ("alpha", "beta"), strict=True):
        assert task.status == TaskStatus.COMPLETED, task.error
        messages = captured[f"repo-{suffix}"]
        assert str(tmp_path / f"repo-{suffix}") in messages[0]["content"]
        assert f"ONLY-{suffix.upper()}" in json.dumps(messages)
        assert f"ONLY-{'BETA' if suffix == 'alpha' else 'ALPHA'}" not in json.dumps(messages)
        assert store.get(task.id).project_path == task.project_path
        assert not (Path(task.project_path) / "data").exists()
    continued = await manager.continue_from_async(store.get(tasks[0].id), "Read sample.txt again", POLICY, PROFILE)
    await manager.background[continued.id]
    assert continued.project_path == tasks[0].project_path
    assert store.get(continued.id).project_path == tasks[0].project_path
    assert store.build_cross_conversation_context("sample.txt again", source="web") == ("", [])


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Actual Windows Job process test")
async def test_shell_real_job_cwd_is_per_task_without_repo_runtime_files(tmp_path):
    app_root = tmp_path / "app"
    app_root.mkdir()
    tool = ShellTool(WindowsJobSandbox(app_root))
    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()
    results = await asyncio.gather(*(tool.execute({"command": "(Get-Location).Path"}, ToolContext(
        task_id=str(index), workspace=str(repo), application_workspace=str(app_root))) for index, repo in enumerate(repos)))
    for repo, result in zip(repos, results, strict=True):
        assert result["exit_code"] == 0 and not result["timed_out"], result
        assert str(repo.resolve()).casefold() in result["stdout"].strip().casefold()
        assert not (repo / "work").exists()
    assert tool.sandbox.workspace == app_root.resolve()
