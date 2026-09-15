from pathlib import Path

import pytest

from deepdesk.engine import AgentEngine
from deepdesk.models import AgentTask


def extensions(task):
    return AgentEngine._requested_artifact_extensions(AgentEngine._artifact_request_text(task))


@pytest.mark.parametrize("prompt", ["现在可以吗", "继续", "好了", "retry"])
def test_mobile_continuation_ignores_document_tool_description(prompt):
    task = AgentTask(prompt=prompt,
        continuation_instructions=['用我的手机打开微信给王剑发送消息“你好，这是一次测试”'],
        context_prompt='tool_search: document can create PDF Word PPT Excel files')
    assert extensions(task) == set()


def test_real_artifact_request_survives_continue():
    task = AgentTask(prompt="继续", continuation_instructions=["制作一个 HTML 网页", "继续"],
                     context_prompt="工具输出 create PDF Word PPT Excel")
    assert extensions(task) == {".html"}


def test_new_non_artifact_task_replaces_old_file_contract():
    task = AgentTask(prompt="打开微信发消息", continuation_instructions=["生成PDF", "继续"])
    assert extensions(task) == set()


def test_legacy_mixed_context_is_not_user_authority():
    assert extensions(AgentTask(prompt="继续", context_prompt="create PDF Word PPT Excel")) == set()


def test_run_uses_trusted_selector_for_completion_check():
    source = (Path(__file__).resolve().parents[1] / "deepdesk/engine.py").read_text("utf-8")
    assert "requested_artifact_extensions = self._requested_artifact_extensions(\n            self._artifact_request_text(task)" in source
