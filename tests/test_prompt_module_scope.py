import json

import pytest

from deepdesk.engine import AgentEngine, runtime_system_prompt
from deepdesk.harness import build_execution_brief
from deepdesk.models import AgentProfile, AgentTask


@pytest.mark.parametrize('prompt', ['Quick example: build a parser', 'Explain documentarium', 'A random example'])
def test_english_substrings_do_not_activate_unrelated_modules(prompt):
    result = runtime_system_prompt(prompt, AgentProfile.GENERAL)
    for header in ('UI, BROWSER, AND SPATIAL MODULE', 'BENCHMARK INTEGRITY MODULE', 'MEDIA MODULE'):
        assert header not in result


def test_local_test_suite_is_not_an_exam_or_web_search():
    result = runtime_system_prompt('Fix Python test suite and verify source code', AgentProfile.CODER)
    assert 'CODING MODULE' in result
    assert 'BENCHMARK INTEGRITY MODULE' not in result
    assert 'CURRENT-INFORMATION MODULE' not in result


def test_explicit_domains_keep_their_rules():
    result = runtime_system_prompt('Search latest news; benchmark JSON browser UI', AgentProfile.GENERAL)
    for header in ('CURRENT-INFORMATION MODULE', 'BENCHMARK INTEGRITY MODULE', 'MACHINE-READABLE OUTPUT MODULE', 'UI, BROWSER, AND SPATIAL MODULE'):
        assert header in result
    assert 'PROMPT-INJECTION DEFENSE' in result


def test_tool_guide_does_not_invent_another_instruction_priority():
    brief = build_execution_brief('repair a parser', AgentProfile.CODER, [])
    assert 'higher priority' not in brief
    assert 'PPTX' not in brief
    assert len(brief) < 800


@pytest.mark.parametrize('path', ['src/server.ts', 'scripts/build.js', 'lib/parser.mjs', 'cli.cjs'])
def test_backend_edits_do_not_force_browser_preview(path):
    task = AgentTask(prompt='Fix this TypeScript backend app on localhost', agent_profile=AgentProfile.CODER)
    messages = [{'role':'assistant', 'tool_calls':[{'id':'edit', 'function':{
        'name':'filesystem', 'arguments':json.dumps({'action':'edit','path':path})}}]}]
    assert not AgentEngine._requires_background_browser_preview(task, messages)


@pytest.mark.parametrize('path', ['static/app.js', 'frontend/main.ts', 'src/Card.tsx', 'index.html'])
def test_browser_source_edits_still_require_preview(path):
    task = AgentTask(prompt='Fix this', agent_profile=AgentProfile.CODER)
    messages = [{'role':'assistant', 'tool_calls':[{'id':'edit', 'function':{
        'name':'filesystem', 'arguments':json.dumps({'action':'edit','path':path})}}]}]
    assert AgentEngine._requires_background_browser_preview(task, messages)
