"""Twenty deterministic incident-injection regressions for Elren."""

from __future__ import annotations

import asyncio
import socket
import sys
from types import SimpleNamespace

import httpx
import psutil
import pytest

from deepdesk.deepseek import DeepSeekClient
from deepdesk.engine import ApprovalGate, HumanActionGate, TaskManager
from deepdesk.models import (
    AgentProfile,
    AgentTask,
    ApprovalPolicy,
    ApprovalRequest,
    HumanActionRequest,
    Risk,
    TaskStatus,
)
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.computer import ComputerTool
from deepdesk.plugins.builtin.computer_use import ComputerUseAgentTool
from deepdesk.plugins.builtin.process_manager import ProcessManagerTool
from deepdesk.plugins.builtin.web import WebTool
from deepdesk.scheduler import Scheduler, ScheduleRequest
from deepdesk.task_store import TaskStore
from deepdesk.windows_sandbox import WindowsJobSandbox


def _client(primary="primary", backup="backup"):
    return DeepSeekClient("https://api.example", "model", primary, backup, timeout=1)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.example/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("incident", request=request, response=response)


@pytest.mark.asyncio
async def test_incident_01_network_connection_drops_then_recovers(monkeypatch):
    client, calls = _client("primary", ""), 0

    async def request(*_args):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("network unplugged")
        return {"choices": [{"message": {"content": "recovered"}}]}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("deepdesk.deepseek.asyncio.sleep", no_sleep)
    assert (await client.chat([], [])).message["content"] == "recovered"
    assert calls == 3


@pytest.mark.asyncio
async def test_incident_02_incomplete_response_body_retries(monkeypatch):
    client, calls = _client("primary", ""), 0

    async def request(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("incomplete body")
        return {"choices": [{"message": {"content": "ok"}}]}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("deepdesk.deepseek.asyncio.sleep", no_sleep)
    assert (await client.chat([], [])).message["content"] == "ok"


@pytest.mark.asyncio
async def test_incident_03_primary_key_revoked_uses_backup(monkeypatch):
    client, keys = _client(), []

    async def request(_endpoint, key, *_args):
        keys.append(key)
        if key == "primary":
            raise _http_error(401)
        return {"choices": [{"message": {"content": "backup"}}]}

    monkeypatch.setattr(client, "_request", request)
    assert (await client.chat([], [])).message["content"] == "backup"
    assert keys == ["primary", "backup"]


@pytest.mark.asyncio
async def test_incident_04_rate_limit_then_recovers(monkeypatch):
    client, calls = _client("primary", ""), 0

    async def request(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(429)
        return {"choices": [{"message": {"content": "ok"}}]}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("deepdesk.deepseek.asyncio.sleep", no_sleep)
    assert (await client.chat([], [])).message["content"] == "ok"


@pytest.mark.asyncio
async def test_tool_call_without_visible_content_is_not_treated_as_empty(monkeypatch):
    client, calls = _client("primary", "backup"), 0
    tool_call = {
        "id": "call_feishu_1",
        "type": "function",
        "function": {
            "name": "feishu",
            "arguments": '{"action":"send_text","text":"1"}',
        },
    }

    async def request(*_args):
        nonlocal calls
        calls += 1
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tool_call],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    monkeypatch.setattr(client, "_request", request)

    reply = await client.chat([], [])

    assert calls == 1
    assert reply.message["tool_calls"] == [tool_call]
    assert reply.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_incident_05_dns_suddenly_unavailable(monkeypatch):
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.web.socket.getaddrinfo",
        lambda *_a, **_k: (_ for _ in ()).throw(socket.gaierror("offline")),
    )
    with pytest.raises(ValueError, match="Could not resolve"):
        await WebTool()._validate_public_url("https://example.com")


@pytest.mark.asyncio
async def test_incident_06_proxy_fake_ip_for_allowlisted_site(monkeypatch):
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.web.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.9.9", 443))],
    )
    await WebTool(["example.com"])._validate_public_url("https://example.com")


@pytest.mark.asyncio
async def test_incident_07_proxy_fake_ip_for_public_site_is_allowed_after_mode_detection(monkeypatch):
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.web.socket.getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.9.9", 443))],
    )
    await WebTool(["example.com"])._validate_public_url("https://public.example")


@pytest.mark.asyncio
async def test_incident_08_redirect_or_url_points_to_localhost():
    with pytest.raises(PermissionError):
        await WebTool(["localhost"])._validate_public_url("http://127.0.0.1:8765")


def test_incident_09_application_alias_missing(monkeypatch):
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.Path.is_file", lambda _p: False)
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.shutil.which", lambda _n: None)
    monkeypatch.setattr("deepdesk.plugins.builtin.process_manager.os.name", "posix")
    with pytest.raises(FileNotFoundError, match="Application not found"):
        ProcessManagerTool._resolve_application("vanished-app.exe")


def test_incident_10_process_closes_before_termination(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "deepdesk.plugins.builtin.process_manager.psutil.Process",
        lambda _pid: (_ for _ in ()).throw(psutil.NoSuchProcess(99999)),
    )
    with pytest.raises(psutil.NoSuchProcess):
        ProcessManagerTool._execute_sync(
            {"action": "terminate", "pid": 99999}, ToolContext(task_id="x", workspace=str(tmp_path))
        )


def test_incident_11_foreground_typing_is_blocked_while_user_works(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace())
    with pytest.raises(PermissionError, match="stealing the user's focus"):
        ComputerTool(tmp_path)._execute_sync({"action": "type", "text": "grok.com"})


def test_incident_12_background_window_closes_before_inspection(monkeypatch, tmp_path):
    class GoneWindow:
        def wait(self, *_a, **_k):
            raise RuntimeError("window disappeared")

    desktop = SimpleNamespace(window=lambda **_k: GoneWindow())
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_k: desktop))
    with pytest.raises(RuntimeError, match="disappeared"):
        ComputerUseAgentTool(tmp_path)._execute_sync({"action": "inspect", "window": "Gone"})


def test_incident_13_background_coordinate_click_cannot_steal_focus(monkeypatch, tmp_path):
    class Rect:
        left = top = 0
        right = bottom = 100

    window = SimpleNamespace(
        wait=lambda *_a, **_k: None,
        rectangle=lambda: Rect(),
        has_focus=lambda: False,
    )
    desktop = SimpleNamespace(window=lambda **_k: window)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_k: desktop))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(click=lambda *_a, **_k: None))
    with pytest.raises(RuntimeError, match="target is not foreground"):
        ComputerUseAgentTool(tmp_path)._execute_sync(
            {
                "action": "click",
                "window": "Background",
                "x": 10,
                "y": 10,
                "allow_user_input_interference": True,
            }
        )


def test_coordinate_click_requires_explicit_user_input_interference_permission(
    monkeypatch, tmp_path
):
    window = SimpleNamespace(wait=lambda *_a, **_k: None)
    desktop = SimpleNamespace(window=lambda **_k: window)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_k: desktop))
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(click=lambda *_a, **_k: None))
    with pytest.raises(PermissionError, match="real mouse"):
        ComputerUseAgentTool(tmp_path)._execute_sync(
            {"action": "click", "window": "Foreground", "x": 10, "y": 10}
        )


@pytest.mark.asyncio
async def test_incident_14_stop_while_waiting_for_approval():
    gate = ApprovalGate()
    request = ApprovalRequest(task_id="t", tool="shell", arguments={}, risk=Risk.HIGH, summary="x")
    waiting = asyncio.create_task(gate.request(request))
    await asyncio.sleep(0)
    gate.cancel_task("t")
    assert await waiting is False


@pytest.mark.asyncio
async def test_incident_15_stop_while_waiting_for_human_takeover():
    gate = HumanActionGate()
    request = HumanActionRequest(task_id="t", summary="x", instructions="y")
    waiting = asyncio.create_task(gate.request(request))
    await asyncio.sleep(0)
    gate.cancel_task("t")
    assert (await waiting)["completed"] is False


@pytest.mark.asyncio
async def test_incident_16_stop_cancels_long_running_agent():
    class Engine:
        async def run(self, task, cancel):
            task.status = TaskStatus.RUNNING
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED

    manager = TaskManager(Engine(), ApprovalGate())
    task = manager.create("long", ApprovalPolicy.BALANCED, AgentProfile.GENERAL)
    await asyncio.sleep(0)
    assert manager.cancel(task.id)
    await asyncio.sleep(0)
    assert task.status == TaskStatus.CANCELLED


def test_incident_17_host_restart_recovers_interrupted_task(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    store.save(AgentTask(prompt="interrupted", status=TaskStatus.RUNNING))
    restored = TaskStore(store.path).load_recent()[0]
    assert restored.status == TaskStatus.FAILED
    assert "重启" in restored.error


def test_restart_recovers_active_tasks_outside_recent_startup_cache(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    interrupted = AgentTask(
        prompt="old interrupted task",
        status=TaskStatus.RUNNING,
        created_at="2000-01-01T00:00:00Z",
        updated_at="2000-01-01T00:00:00Z",
    )
    store.save(interrupted)
    for index in range(5):
        store.save(
            AgentTask(
                prompt=f"new completed task {index}",
                status=TaskStatus.COMPLETED,
            )
        )

    TaskStore(store.path).load_recent(limit=1)
    recovered = TaskStore(store.path).get(interrupted.id)

    assert recovered is not None
    assert recovered.status == TaskStatus.FAILED
    restart_event = next(
        event for event in recovered.events if event.type == "restart_interrupted"
    )
    assert restart_event.data["previous_status"] == "running"


def test_incident_18_scheduler_restart_preserves_future_task(tmp_path):
    path = tmp_path / "cron.sqlite"
    scheduler = Scheduler(path, lambda *_a: "task")
    created = scheduler.create(
        ScheduleRequest(name="future", prompt="work", kind="at", expression="2099-01-01T00:00:00Z")
    )
    assert Scheduler(path, lambda *_a: "task").get(created["id"])["name"] == "future"


def test_channel_restart_recovery_is_not_limited_to_recent_task_cache(tmp_path):
    store = TaskStore(tmp_path / "channel-recovery.db")
    interrupted_ids = []
    for index in range(125):
        task = AgentTask(
            prompt=f"remote task {index}",
            source="telegram",
            remote_recipient_id=f"telegram-chat-{index}",
            remote_recipient_type="chat_id",
            status=TaskStatus.RUNNING,
        )
        interrupted_ids.append(task.id)
        store.save(task)

    # Startup deliberately loads only 100 cards, while recovery terminalizes
    # all 125 durable active records.
    assert len(TaskStore(store.path).load_recent(limit=100)) == 100
    recoverable = TaskStore(store.path).list_restart_interrupted("telegram")

    assert {task.id for task in recoverable} == set(interrupted_ids)
    assert {task.remote_recipient_id for task in recoverable} == {
        f"telegram-chat-{index}" for index in range(125)
    }


@pytest.mark.asyncio
async def test_incident_19_sandbox_timeout_terminates_job_tree(tmp_path):
    sandbox = WindowsJobSandbox(tmp_path)
    if not sandbox.status()["available"]:
        pytest.skip("Windows Job Object unavailable")
    result = await sandbox.run("Start-Sleep -Seconds 4", timeout_seconds=1)
    assert result["timed_out"] and result["exit_code"] == 1460


@pytest.mark.asyncio
async def test_incident_20_sandbox_rejects_credential_environment_access(tmp_path):
    sandbox = WindowsJobSandbox(tmp_path)
    with pytest.raises(PermissionError):
        await sandbox.run("Get-ChildItem Env:")
