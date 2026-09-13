"""Real durable SQLite and ASGI routes; no model/OS permission calls."""
import json

import httpx
import pytest

from deepdesk.config import Settings
from deepdesk.conversation_export import export_conversation
from deepdesk.main import create_app
from deepdesk.models import AgentTask, ConversationTurn, TaskEvent, TaskStatus
from deepdesk.secret_storage import AesGcmProtector
from deepdesk.task_store import TaskStore


def test_pin_survives_worker_writes_restart_filter_and_delete(tmp_path):
    path = tmp_path / "tasks.db"
    store = TaskStore(path)
    old = AgentTask(prompt="old needle", updated_at="2020-01-01", status=TaskStatus.FAILED)
    new = AgentTask(prompt="new", updated_at="2026-01-01")
    store.save(old)
    store.save(new)
    assert store.set_pinned(old.id, True)
    store.save(old)  # Running worker has stale/no sidebar metadata.
    store = TaskStore(path)
    page, total = store.list_page(limit=1)
    assert total == 2 and page[0].id == old.id and page[0].pinned
    assert store.list_page(offset=1)[0][0].id == new.id
    assert store.list_page(query="needle", status="failed")[0][0].pinned
    assert store.set_pinned(old.id, False)
    assert store.list_page()[0][0].id == new.id
    store.set_pinned(old.id, True)
    store.delete(old.id)
    assert not store.set_pinned(old.id, True)
    with store._connect() as connection:
        assert not connection.execute("SELECT * FROM task_pins").fetchall()


def task_fixture():
    return AgentTask(prompt="现在的问题", title="示例", result="final", status=TaskStatus.COMPLETED,
        context_prompt="PRIVATE SYSTEM", remote_recipient_id="PRIVATE ROUTE",
        conversation_turns=[ConversationTurn(prompt="之前的问题", result="之前的回答",
            status=TaskStatus.COMPLETED, created_at="2026-01-01", updated_at="2026-01-01")],
        events=[TaskEvent(type="tool_result", data={"result": "PRIVATE TOOL"}),
                TaskEvent(type="user_message_queued", data={"content": "follow-up"}),
                TaskEvent(type="assistant", data={"content": "final"})])


def test_export_contains_visible_turns_only_and_no_duplicate_final():
    task = task_fixture()
    result = export_conversation(task, "json")
    document = json.loads(result)
    assert len(document["turns"]) == 2
    assert document["turns"][1]["messages"] == [
        {"role": "user", "content": "现在的问题"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "final"}]
    assert "PRIVATE" not in result
    markdown = export_conversation(task, "markdown")
    assert "之前的回答" in markdown and "### User" in markdown and "PRIVATE" not in markdown
    with pytest.raises(ValueError):
        export_conversation(task, "html")


@pytest.mark.asyncio
async def test_real_routes_pin_and_redacted_export(tmp_path, monkeypatch):
    monkeypatch.setattr("deepdesk.secret_storage.select_secret_protector",
                        lambda: AesGcmProtector("export-test", b"t" * 32))
    app = create_app(Settings(_env_file=None, deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False, deepdesk_feishu_enabled=False))
    store = app.state.manager.store
    task = task_fixture()
    task.remote_recipient_id = ""
    store.save(task)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        response = await client.patch(f"/api/tasks/{task.id}/pin", json={"pinned": True})
        assert response.status_code == 200 and response.json()["pinned"]
        listing = await client.get("/api/tasks")
        assert listing.json()["tasks"][0]["pinned"] is True
        for bad in ("true", 1, None):
            assert (await client.patch(f"/api/tasks/{task.id}/pin", json={"pinned": bad})).status_code == 422
        for format in ("json", "markdown"):
            response = await client.get(f"/api/tasks/{task.id}/export?format={format}")
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("attachment;")
            assert "no-store" in response.headers["cache-control"]
            assert "PRIVATE" not in response.text and "之前的回答" in response.text
        assert (await client.get(f"/api/tasks/{task.id}/export?format=html")).status_code == 422
        assert (await client.get("/api/tasks/missing/export")).status_code == 404
        assert (await client.patch("/api/tasks/missing/pin", json={"pinned": True})).status_code == 404
