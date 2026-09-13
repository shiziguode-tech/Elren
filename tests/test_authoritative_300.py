from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "work" / "benchmarks" / "authoritative300"
if not (SUITE / 'run_authoritative_300.py').is_file():
    import pytest
    pytest.skip('Optional internal benchmark runner/data are not distributed with source', allow_module_level=True)
sys.path.insert(0, str(SUITE))

from run_authoritative_300 import (
    bigcode_row_passed,
    bigcode_selective_id,
    extract_code,
    extract_mmlu_answer,
    normalize_bfcl_function,
    score_bfcl,
    score_bfcl_official,
)


def test_authoritative_manifest_has_fixed_balanced_allocation() -> None:
    rows = [json.loads(line) for line in (SUITE / "manifest.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 300
    assert len({row["case_id"] for row in rows}) == 300
    assert Counter(row["source"] for row in rows) == {
        "BFCL_V4": 100,
        "MMLU_PRO": 100,
        "BIGCODEBENCH_HARD": 100,
    }
    mmlu_categories = {row["category"] for row in rows if row["source"] == "MMLU_PRO"}
    assert len(mmlu_categories) == 14


def test_bfcl_adapted_scorer_accepts_allowed_value_and_rejects_extra_call() -> None:
    expected = [{"calculate": {"x": [2, 2.0], "unit": ["m", ""]}}]
    good = {
        "tool_calls": [
            {"function": {"name": "calculate", "arguments": '{"x": 2, "unit": "m"}'}}
        ]
    }
    extra = {
        "tool_calls": good["tool_calls"]
        + [{"function": {"name": "other", "arguments": "{}"}}]
    }
    assert score_bfcl(good, expected) is True
    assert score_bfcl(extra, expected) is False
    assert score_bfcl(
        {"tool_calls": [{"function": {"name": "math_hypot", "arguments": '{"x":4,"y":5}'}}]},
        [{"math.hypot": {"x": [4], "y": [5], "z": ["", 0]}}],
    ) is True
    assert score_bfcl(
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "integrate",
                        "arguments": '{"function":"x^3","budget":{"min":300000}}',
                    }
                }
            ]
        },
        [
            {
                "integrate": {
                    "function": ["x**3", "lambda x: x**3"],
                    "budget": [{"min": [300000]}],
                }
            }
        ],
    ) is True


def test_output_extractors_prefer_explicit_final_answer_and_largest_code_block() -> None:
    assert extract_mmlu_answer("A is tempting.\nAnswer: C") == "C"
    assert extract_code("```python\ndef task_func():\n    return 1\n```") == (
        "def task_func():\n    return 1"
    )


def test_bfcl_function_schema_is_normalized_like_official_openapi_adapter() -> None:
    normalized = normalize_bfcl_function(
        {
            "name": "math.factorial",
            "parameters": {
                "type": "dict",
                "properties": {
                    "values": {"type": "list", "items": {"type": "float"}}
                },
            },
        }
    )
    assert normalized["name"] == "math_factorial"
    assert normalized["parameters"]["type"] == "object"
    assert normalized["parameters"]["properties"]["values"] == {
        "type": "array",
        "items": {"type": "number"},
    }


def test_bfcl_official_scorer_preserves_nested_float_type_rules() -> None:
    functions = [
        {
            "name": "integrate",
            "parameters": {
                "type": "dict",
                "properties": {
                    "interval": {
                        "type": "array",
                        "items": {"type": "float"},
                    }
                },
                "required": ["interval"],
            },
        }
    ]
    ground_truth = [{"integrate": {"interval": [[1.0, 3.0]]}}]
    integer_nested = {
        "tool_calls": [
            {
                "function": {
                    "name": "integrate",
                    "arguments": '{"interval":[1,3]}',
                }
            }
        ]
    }
    passed, error = score_bfcl_official(
        integer_nested, ground_truth, functions, "simple_python"
    )
    assert passed is False
    assert error == "type_error:nested"


def test_retired_private_chat_theme_does_not_remain_in_frontend_assets() -> None:
    index = (ROOT / "deepdesk" / "static" / "index.html").read_text("utf-8")
    source = (ROOT / "deepdesk" / "static" / "app.js").read_text("utf-8")
    css = (ROOT / "deepdesk" / "static" / "composer-controls.css").read_text("utf-8")
    assert "privacyChatToggle" not in index
    assert "privacyMode" not in source
    assert "body.private-chat" not in css


def test_bigcode_remote_evaluator_retries_transient_network_failures() -> None:
    source = (SUITE / "run_authoritative_300.py").read_text("utf-8")
    assert "for attempt in range(5)" in source
    assert "BIGCODE REMOTE RETRY" in source
    assert 'httpx_kwargs={"timeout": 120.0}' in source


def test_bigcode_remote_adapter_matches_current_official_space_contract() -> None:
    assert bigcode_selective_id("BigCodeBench/100") == "100"
    assert bigcode_selective_id("100") == "100"
    assert bigcode_row_passed({"status": "pass"}) is True
    assert bigcode_row_passed({"status": "fail"}) is False
    assert bigcode_row_passed({"base": ["pass", []]}) is True
