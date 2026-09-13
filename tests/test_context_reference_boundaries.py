from __future__ import annotations

import pytest

from deepdesk.models import AgentTask, TaskStatus
from deepdesk.task_store import TaskStore


@pytest.mark.parametrize("prompt", [
    "Compare website themes and color palettes.",
    "Explain thermodynamics.",
    "不要把这次连接验收称为其他功能通过。",
    "列出其他品牌的路由器。",
])
def test_generic_words_do_not_force_latest_unrelated_chat(tmp_path, prompt):
    store = TaskStore(tmp_path / "tasks.db")
    previous = AgentTask(
        prompt="Describe the moons of Jupiter.", result="Europa has an icy surface.",
        status=TaskStatus.COMPLETED,
    )
    store.save(previous)
    assert store.build_cross_conversation_context(prompt) == ("", [])


@pytest.mark.parametrize("prompt", [
    "重试一下其他两项的生成", "其他两个呢？", "Retry the other two.",
    "Please finish them.", "What about those?",
])
def test_real_followup_still_recalls_latest_chat(tmp_path, prompt):
    store = TaskStore(tmp_path / "tasks.db")
    previous = AgentTask(
        prompt="Prepare a diagram, an image and a chart.",
        result="The diagram is ready; the image and chart are pending.",
        status=TaskStatus.COMPLETED,
    )
    store.save(previous)
    context, task_ids = store.build_cross_conversation_context(prompt)
    assert task_ids == [previous.id]
    assert "image and chart are pending" in context


def test_shared_qa_boilerplate_is_not_a_shared_task_subject(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.save(AgentTask(
        prompt=("这是 Mac 工具验收。实际创建 HTML 文件，全程使用后台浏览器。"
                "不联网搜索、不调用子智能体，报告真实结果。"),
        result="HTML 文件已创建，浏览器点击通过。未联网搜索。",
        status=TaskStatus.COMPLETED,
    ))
    assert store.build_cross_conversation_context(
        "这是 Mac 模型连接验收。只报告实际连接情况，不调用工具，不读文件，"
        "不联网搜索。不要把这次连接验收称为其他功能通过。"
    ) == ("", [])


def test_chinese_terms_do_not_join_across_unrelated_segments():
    assert "南京" not in TaskStore._context_terms("南 HTML 京")
    assert "南京" in TaskStore._context_terms("南京天气")
