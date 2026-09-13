from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "work" / "benchmarks" / "professional_pro"
if not (HERE / 'run_professional_pro.py').is_file():
    import pytest
    pytest.skip('Optional internal benchmark runner/data are not distributed with source', allow_module_level=True)
sys.path.insert(0, str(HERE))

from run_professional_pro import (
    BIGCODE_INSTRUCTION,
    MMLU_INSTRUCTION,
    PRO,
    Result,
    extract_mmlu_answer_official,
    format_mmlu_prompt,
    mmlu_official_score,
)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").split("\n") if line]


def test_professional_manifests_cover_both_full_official_suites() -> None:
    mmlu = read_jsonl(HERE / "mmlu-pro-official.jsonl")
    bigcode = read_jsonl(HERE / "bigcodebench-hard-official.jsonl")
    validation = json.loads((HERE / "mmlu-pro-validation.json").read_text("utf-8"))
    assert len(mmlu) == 12_032
    assert len({row["source_id"] for row in mmlu}) == 12_032
    assert len({row["category"] for row in mmlu}) == 14
    assert len(bigcode) == 148
    assert len({row["source_id"] for row in bigcode}) == 148
    assert len(validation) == 14
    assert {len(examples) for examples in validation.values()} == {5}


def test_mmlu_prompt_and_extractor_follow_official_api_protocol() -> None:
    case = {
        "category": "math",
        "payload": {
            "question": "Target?",
            "options": ["one", "two"],
            "answer": "B",
            "answer_index": 1,
        },
    }
    validation = {
        "math": [
            {
                "question": f"Demo {index}?",
                "options": ["one", "two"],
                "cot_content": "A: Let's think step by step. The answer is (A)",
            }
            for index in range(5)
        ]
    }
    prompt = format_mmlu_prompt(case, validation)
    assert prompt.startswith(MMLU_INSTRUCTION.format(category="math"))
    assert prompt.count("Question:") == 6
    assert prompt.endswith("Answer: Let's think step by step.\n\n")
    assert extract_mmlu_answer_official("reasoning\nthe answer is (C)") == "C"
    assert extract_mmlu_answer_official("reasoning\nAnswer: D") == "D"


def test_mmlu_official_score_is_separate_and_resume_deterministic() -> None:
    manifest = [
        {
            "case_id": "one",
            "category": "math",
            "payload": {"answer": "A", "options": list("ABCDEFGHIJ")},
        },
        {
            "case_id": "two",
            "category": "math",
            "payload": {"answer": "B", "options": list("ABCDEFGHIJ")},
        },
    ]
    results = {
        "one": Result("one", "MMLU_PRO", "1", "math", True, "answered", PRO, 1, "x", answer="A"),
        "two": Result("two", "MMLU_PRO", "2", "math", None, "unparsed_response", PRO, 1, "y"),
    }
    first = mmlu_official_score(manifest, results)
    second = mmlu_official_score(manifest, results)
    assert first == second
    assert first["scored"] == 2
    assert first["unparsed_random_fallbacks"] == 1


def test_professional_runner_forces_pro_and_uses_official_bigcode_pipeline() -> None:
    source = (HERE / "run_professional_pro.py").read_text("utf-8")
    assert 'client.bind_task_model(PRO)' in source
    assert 'client.effective_model != PRO' in source
    assert 'reply.provider_model != PRO' in source
    assert '"provider_model": reply.provider_model or "not-reported"' in source
    assert "forced-Pro invariant violated" in source
    assert '"mmlu-pro-official-state.json"' in source
    assert '"bigcodebench-hard-official-state.json"' in source
    assert "official_bigcode_sanitize" in source
    assert "calibrated=True" in source
    assert 'selective_evaluate=""' in source
    assert "pass_k=\"1\"" in source
    assert BIGCODE_INSTRUCTION.startswith("Please provide a self-contained Python script")
