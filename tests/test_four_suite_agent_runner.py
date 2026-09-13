import sys
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parents[1] / "work" / "benchmarks" / "four_suite"
if not (RUNNER_DIR / 'run_four_suites_agent.py').is_file():
    import pytest
    pytest.skip('Optional internal benchmark runner/data are not distributed with source', allow_module_level=True)
sys.path.insert(0, str(RUNNER_DIR))

import run_four_suites_agent as runner


def test_four_suite_agent_runner_uses_requested_official_rows():
    cases = runner.load_cases()

    assert {name: len(rows) for name, rows in cases.items()} == {
        "gpqa": 198,
        "humaneval": 164,
        "math500": 500,
        "mbpp": 374,
    }
    assert cases["mbpp"][0]["id"] == "mbpp-0601"
    assert cases["mbpp"][-1]["id"] == "mbpp-0974"


def test_humaneval_completion_extraction_preserves_leading_indentation():
    completion = "```python\n    return value + 1\n```"

    assert runner.extract_agent_code(completion).startswith("    return")
