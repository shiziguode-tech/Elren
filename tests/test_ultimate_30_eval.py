from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ultimate_30_is_complete_unique_and_software_heavy():
    cases = load("ultimate30_cases_test", "work/evals/ultimate30_cases.py").CASES
    assert len(cases) == 30
    assert len({case.case_id for case in cases}) == 30
    assert sum(case.domain.startswith("software") for case in cases) == 24
    assert all(case.expected[0] == f"ULTIMATE30_{case.case_id}_OK" for case in cases)
    assert all(all(tool in case.prompt for tool in case.required_tools) for case in cases)
    assert Counter(case.domain for case in cases)["capstone"] == 1


def test_ultimate_30_covers_cross_tool_and_human_paths():
    cases = load("ultimate30_cases_coverage", "work/evals/ultimate30_cases.py").CASES
    covered = {tool for case in cases for tool in case.required_tools}
    assert {
        "filesystem", "sandbox", "mcp", "background_browser", "provider_web_search",
        "computer_use", "process_manager", "windows_ui", "request_human_action",
    } <= covered
    assert any(len(case.required_tools) >= 4 for case in cases)
    assert any(case.human_mode == "problem" for case in cases)


def test_ultimate_30_runner_has_no_whole_task_deadline_and_checkpoints():
    source = (ROOT / "work/evals/run_ultimate_30.py").read_text("utf-8")
    assert "No whole-task wall-clock deadline" in source
    assert "deadline =" not in source
    assert "write_reports(results" in source
    assert "--resume" in source


def test_ultimate_30_preparer_writes_all_specs(tmp_path, monkeypatch):
    module = load("prepare_ultimate30_test", "work/evals/prepare_ultimate_30.py")
    monkeypatch.setattr(module, "FIXTURE", tmp_path)
    module.prepare()
    assert all((tmp_path / f"U{i:02d}" / "SPEC.md").is_file() for i in range(1, 25))
    assert (tmp_path / "U27" / "SPEC.md").is_file()
    assert (tmp_path / "U30" / "events.jsonl").is_file()
