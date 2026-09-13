"""Real ASGI routes and store, synthetic execution only; no provider calls."""
import asyncio

import httpx
import pytest

from deepdesk.config import Settings
from deepdesk.main import create_app
from deepdesk.models import AgentTask, TaskEvent, TaskStatus
from deepdesk.secret_storage import AesGcmProtector


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(TaskStatus))
async def test_continue_http_all_statuses(tmp_path, monkeypatch, status):
    # Test-only disposable vault: this route test must never touch the user's
    # Keychain/DPAPI or require interactive privacy approval on a build host.
    monkeypatch.setattr("deepdesk.secret_storage.select_secret_protector",
                        lambda: AesGcmProtector("conversation-test-only", b"t" * 32))
    app = create_app(Settings(_env_file=None, deepdesk_workspace=tmp_path,
                              deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False))
    manager = app.state.manager
    manager.on_task_created = None
    monkeypatch.setattr(manager.engine.client, "has_model_credentials", lambda _: True)
    release = asyncio.Event()

    async def synthetic_run(task, cancel, queue):
        task.status = TaskStatus.RUNNING
        await release.wait()
        task.status = TaskStatus.COMPLETED
        task.result = "Synthetic next answer"
        manager.emit(task, "assistant", {"content": task.result})

    monkeypatch.setattr(manager.engine, "run", synthetic_run)
    old = AgentTask(prompt="Original question", title="Original title", status=status,
                    result="Original answer", events=[TaskEvent(type="assistant", data={"content": "Original answer"})])
    manager.tasks[old.id] = old
    manager.store.save(old)
    # No lifespan: no platform listeners, discovery, or live application start.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
                                 headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}) as client:
        response = await client.post(f"/api/tasks/{old.id}/continue", json={"prompt": "Next question"})
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            assert response.status_code == 409
            assert manager.tasks[old.id] is old
            return
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["id"] == old.id and payload["title"] == old.title
        assert payload["conversation_turns"][0]["result"] == "Original answer"
        assert payload["events"] == []
        assert manager.store.list_page()[1] == 1
        worker = manager.background[old.id]
        try:
            duplicate = await client.post(f"/api/tasks/{old.id}/continue", json={"prompt": "Duplicate"})
            assert duplicate.status_code == 409
            assert manager.background[old.id] is worker
            release.set()
            await asyncio.wait_for(worker, 5)
            loaded = (await client.get(f"/api/tasks/{old.id}")).json()
            assert loaded["id"] == old.id and loaded["status"] == "completed"
            assert loaded["conversation_turns"][0]["prompt"] == "Original question"
            cursor = loaded["events"][-1]["id"]
            delta = (await client.get(f"/api/tasks/{old.id}", params={"after_event_id": cursor})).json()
            assert delta["event_delta"] is True
            assert "conversation_turns" not in delta
            assert manager.store.get(old.id).conversation_turns[0].result == "Original answer"
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
