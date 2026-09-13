from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_cases_module():
    path = ROOT / "work" / "evals" / "hell100_cases.py"
    spec = importlib.util.spec_from_file_location("hell100_cases", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hell100_distribution_and_oracles_are_complete():
    cases = _load_cases_module().CASES
    assert len(cases) == 100
    assert len({case.case_id for case in cases}) == 100
    assert len({case.expected[1] for case in cases}) == 100
    assert set(Counter(case.language for case in cases).values()) == {5}
    assert Counter(case.domain for case in cases) == {
        "repository": 20,
        "research": 20,
        "mcp": 20,
        "desktop": 20,
        "resilience": 20,
    }
    assert sum(case.ultra_long for case in cases) == 10
    assert all(case.expected[0] in case.prompt for case in cases)
    assert all(case.expected[1] in case.prompt for case in cases)
    assert all(all(tool in case.prompt for tool in case.required_tools) for case in cases)
    assert all(all(tool in case.prompt for tool in case.forbidden_tools) for case in cases)


def test_hell100_exercises_every_declared_agent_capability():
    cases = _load_cases_module().CASES
    covered = {tool for case in cases for tool in case.required_tools}
    assert {
        "filesystem",
        "sandbox",
        "web",
        "provider_web_search",
        "background_browser",
        "mcp",
        "skills",
        "openclaw",
        "computer",
        "computer_use",
        "vision",
        "windows_ui",
        "process_manager",
        "request_human_action",
        "memory",
        "cron",
    } <= covered


def test_hell100_runner_has_no_whole_task_deadline():
    runner = (ROOT / "work" / "evals" / "run_hell_100.py").read_text("utf-8")
    assert "No whole-task hard timeout" in runner
    assert "deadline =" not in runner
