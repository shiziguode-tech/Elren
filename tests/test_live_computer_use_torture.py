from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "qa-artifacts" / "live-computer-use-qa" / "run_qa.py"


def _runner_module():
    spec = importlib.util.spec_from_file_location("elren_live_computer_use_qa", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_computer_use_torture_contract_has_100_independent_cases(tmp_path: Path) -> None:
    runner = _runner_module()
    results = runner.run_contract_cases(tmp_path)
    case_ids = [item.case_id for item in results]
    assert len(results) >= 100
    assert len(case_ids) == len(set(case_ids))
    assert all(item.suite == "automated_contract" for item in results)
    assert all(item.status == "pass" for item in results)
    categories = {item.category for item in results}
    assert {
        "virtual_desktop_coordinates",
        "screen_change_verification",
        "metrics_and_latency",
        "tool_contract",
        "lease_concurrency_expiry",
        "stale_state_and_zero_input",
    } <= categories


def test_live_computer_use_qa_report_distinguishes_contract_real_and_unavailable(tmp_path: Path) -> None:
    runner = _runner_module()
    results = [
        runner.CaseResult("C-001", "automated_contract", "contract", "pass", 1.0),
        runner.CaseResult("R-001", "real_desktop", "click", "pass", 2.0, zero_misclick=True),
        runner.CaseResult("U-001", "unavailable", "platform", "skip", 0.0),
    ]
    json_path, markdown_path = runner.report(results, tmp_path)
    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")
    assert '"automated_contract"' in json_text
    assert '"real_desktop"' in json_text
    assert '"unavailable"' in json_text
    assert "Zero-misclick checks" in markdown_text
    assert "Real desktop latency" in markdown_text


def test_real_desktop_runner_enforces_100_scenario_minimum() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert 'if len(results) < 100:' in source
    assert 'raise AssertionError(f"Real desktop QA requires at least 100 scenario cases' in source
