import asyncio
import base64
from pathlib import Path

import pytest

from deepdesk.audit import AuditLog
from deepdesk.deepseek import DeepSeekReply
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, runtime_system_prompt
from deepdesk.harness import recommended_tool_names, simple_mobile_task
from deepdesk.models import AgentProfile, AgentTask, ApprovalPolicy, Risk, TaskEvent, TaskStatus
from deepdesk.plugins import PluginRegistry, ToolContext
from deepdesk.plugins.base import ToolPlugin
from deepdesk.plugins.builtin.mobile_device import MobileDeviceTool

PROMPT = '用我的手机打开微信给联系人发送消息”你好，这是一次测试“'

def test_message_literal_does_not_activate_coding():
    prompt = runtime_system_prompt(PROMPT, AgentProfile.GENERAL)
    assert 'ANDROID MODULE' in prompt
    assert 'CODING MODULE' not in prompt
    assert recommended_tool_names(PROMPT, AgentProfile.GENERAL)[0] == 'mobile_device'
    assert 'CODING MODULE' in runtime_system_prompt('修复手机应用代码，添加测试', AgentProfile.GENERAL)

def test_only_single_mobile_actions_receive_short_task_checkpoint():
    assert simple_mobile_task(PROMPT, AgentProfile.GENERAL)
    assert not simple_mobile_task('用手机给所有联系人批量发送消息', AgentProfile.GENERAL)
    assert not simple_mobile_task('修复手机输入代码', AgentProfile.GENERAL)
    assert not simple_mobile_task(PROMPT, AgentProfile.CODER)

@pytest.mark.asyncio
async def test_observe_captures_and_interprets_once(tmp_path):
    class Bridge:
        async def command(self, action, arguments, **kwargs):
            assert action == 'screenshot'
            assert not arguments
            return {'image_base64': base64.b64encode(b'image').decode()}
    class Vision:
        calls = 0
        async def execute(self, args, context):
            self.calls += 1
            assert Path(args['image_path']).read_bytes() == b'image'
            assert args['question'] == 'Find the search button'
            assert args['image_path'] in context.authorized_read_paths
            return {'description': 'Search button at 100, 200', 'semantic_verified': True}
    vision = Vision()
    tool = MobileDeviceTool(Bridge(), tmp_path, vision)
    result = await tool.execute({'action': 'observe', 'question': 'Find the search button'}, ToolContext(task_id='test', workspace=str(tmp_path)))
    assert vision.calls == 1
    assert result['analysis']['semantic_verified']
    assert 'image_base64' not in result

@pytest.mark.asyncio
async def test_failed_analysis_keeps_captured_image(tmp_path):
    class Bridge:
        async def command(self, *args, **kwargs):
            return {'image_base64': base64.b64encode(b'image').decode()}
    class Vision:
        async def execute(self, *args):
            raise RuntimeError('Vision unavailable')
    result = await MobileDeviceTool(Bridge(), tmp_path, Vision()).execute({'action': 'observe'}, ToolContext(task_id='test', workspace=str(tmp_path)))
    assert Path(result['path']).exists()
    assert 'Vision unavailable' in result['analysis_error']
    assert 'analysis' not in result

@pytest.mark.asyncio
async def test_mobile_loop_enters_report_only_and_persists_report_first(tmp_path):
    class Phone(ToolPlugin):
        name = 'mobile_device'
        description = 'Phone'
        parameters = {'type': 'object', 'properties': {'action': {'type': 'string'}}}
        calls = 0
        def risk(self, args): return Risk.SAFE
        async def execute(self, args, context):
            self.calls += 1
            return {'connected': True}
    phone = Phone()
    registry = PluginRegistry()
    registry.register(phone)
    class Client:
        keys = []
        calls = 0
        async def chat(self, messages, tools):
            self.calls += 1
            if not tools:
                assert 'final report NOW' in messages[-1]['content']
                return DeepSeekReply(message={'role': 'assistant', 'content': '未完成：输入框不可访问，需要你点一下输入框。'}, usage={}, active_key='test')
            return DeepSeekReply(message={'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(self.calls), 'type': 'function', 'function': {'name': 'mobile_device', 'arguments': '{"action":"status"}'}}]}, usage={}, active_key='test')
    task = AgentTask(prompt=PROMPT, approval_policy=ApprovalPolicy.AUTONOMOUS)
    def emit(current, kind, data):
        if kind == 'assistant' and data.get('final'):
            assert current.status != TaskStatus.COMPLETED
        current.events.append(TaskEvent(type=kind, data=data))
    engine = AgentEngine(client=Client(), registry=registry, approvals=ApprovalGate(), human_actions=HumanActionGate(), audit=AuditLog(tmp_path/'audit.jsonl'), workspace=str(tmp_path), max_steps=None, emit=emit)
    await engine.run(task, asyncio.Event())
    assert phone.calls == 24
    assert '未完成' in task.result
    assert task.events[-2].type == 'assistant'
    assert task.events[-1].type == 'status'
