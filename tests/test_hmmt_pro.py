from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "work" / "benchmarks" / "hmmt_2026_pro" / "run_hmmt_pro.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("hmmt_pro_runner", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hmmt_runner_uses_official_short_prompt_and_forced_pro_max() -> None:
    module = load_runner()
    row = {"problem_idx": 1, "problem": "Find x.", "answer": "2"}
    assert module.prompt_for(row) == "Put your final answer within \\boxed{}.\n\nFind x."
    source = RUNNER.read_text("utf-8")
    assert 'PRO = "deepseek-v4-pro"' in source
    assert 'EFFORT = "max"' in source
    assert "client.bind_task_model(PRO)" in source
    assert "client.bind_task_reasoning_effort(EFFORT)" in source
    assert "reply.provider_model != PRO" in source


def test_hmmt_runner_uses_official_matharena_parser() -> None:
    module = load_runner()
    passed, answer, warning = module.score_answer(
        r"Reasoning. Final answer: \boxed{-\frac{1}{21}}.",
        r"-\frac{1}{21}",
    )
    assert passed is True
    assert answer == "-1/21"
    assert isinstance(warning, int)


def test_deepseek_effort_binding_is_context_local() -> None:
    from deepdesk.deepseek import DeepSeekClient

    client = DeepSeekClient("https://api.deepseek.com", "deepseek-v4-flash", "key", "")
    assert client._chat_payload([], [])["reasoning_effort"] == "high"
    token = client.bind_task_reasoning_effort("max")
    try:
        assert client._chat_payload([], [])["reasoning_effort"] == "max"
    finally:
        client.reset_task_reasoning_effort(token)
    assert client._chat_payload([], [])["reasoning_effort"] == "high"
    low_token = client.bind_task_reasoning_effort("low")
    try:
        assert client._chat_payload([], [])["reasoning_effort"] == "high"
    finally:
        client.reset_task_reasoning_effort(low_token)
    with pytest.raises(ValueError):
        client.bind_task_reasoning_effort("impossible")


@pytest.mark.asyncio
async def test_adjustable_concurrency_changes_without_cancelling_active_slots() -> None:
    module = load_runner()
    gate = module.AdjustableConcurrency(2)
    await gate.__aenter__()
    await gate.__aenter__()
    assert gate.active == 2

    await gate.set_limit(1)
    assert gate.limit == 1
    assert gate.active == 2
    await gate.__aexit__(None, None, None)
    await gate.__aexit__(None, None, None)
    assert gate.active == 0


def test_hmmt_state_includes_live_progress_and_sampling_disclosure(tmp_path: Path) -> None:
    module = load_runner()
    module.STATE = tmp_path / "state.json"
    module.REPORT = tmp_path / "report.md"
    module.write_outputs(
        {},
        33,
        "2026-08-07T00:00:00+0000",
        in_flight={"hmmt-001-s1": {"elapsed_seconds": 12.5}},
        workers=3,
    )

    state = json.loads(module.STATE.read_text("utf-8"))
    report = module.REPORT.read_text("utf-8")
    assert state["workers"] == 3
    assert state["in_flight"]["hmmt-001-s1"]["elapsed_seconds"] == 12.5
    assert state["official_matharena_default_runs_per_problem"] == 4
    assert state["local_samples_per_problem"] == 1
    assert "not a bit-for-bit identical run" in report
