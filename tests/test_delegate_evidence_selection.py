"""Delegated reviews retain actual verification despite later housekeeping."""
from __future__ import annotations

import json

from deepdesk.engine import AgentEngine


def tool(messages, call_id, name, arguments, result):
    messages.extend([
        {"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
    ])


def test_four_housekeeping_results_do_not_evict_passing_tests_or_source():
    messages = []
    tool(messages, "code", "filesystem", {"action": "read", "path": "cart.py"}, {"ok": True, "result": {"content": "CART_SOURCE"}})
    tool(messages, "tests", "shell", {"command": "python -m unittest discover"}, {"ok": True, "result": {"exit_code": 0, "stdout": "Ran 4 tests. OK. CART_TESTS_PASS"}})
    for index in range(4):
        tool(messages, f"housekeeping-{index}", "shell", {"command": "git status --short"}, {"ok": True, "result": {"exit_code": 0, "stdout": "status only"}})
    evidence = AgentEngine._specialist_evidence(messages, str)
    assert len(evidence) <= 4
    text = json.dumps(evidence)
    assert "CART_TESTS_PASS" in text and "CART_SOURCE" in text
    assert "tests" in text and "tool_call_id" in text


def test_latest_failed_and_passing_targets_retained_with_recent_diff():
    messages = []
    tool(messages, "failed", "shell", {"command": "pytest tests/test_checkout.py"}, {"ok": False, "result": {"exit_code": 1, "stdout": "CHECKOUT_FAIL"}})
    tool(messages, "passed", "sandbox", {"command": "pytest tests/test_cart.py"}, {"ok": True, "result": {"exit_code": 0, "stdout": "CART_PASS"}})
    tool(messages, "diff", "shell", {"command": "git diff -- cart.py"}, {"ok": True, "result": {"exit_code": 0, "stdout": "RECENT_DIFF"}})
    for index in range(8):
        tool(messages, f"ls-{index}", "filesystem", {"action": "list", "path": "."}, {"ok": True, "result": []})
    text = json.dumps(AgentEngine._specialist_evidence(messages, str))
    assert "CHECKOUT_FAIL" in text and "CART_PASS" in text and "RECENT_DIFF" in text


def test_excerpts_keep_test_summary_tail_and_explicit_truncation():
    messages = []
    tool(messages, "long-test", "shell", {"command": "pytest"}, {"ok": True, "result": {"exit_code": 0, "stdout": "PROGRESS " * 2000 + "FINAL 42 passed"}})
    evidence = AgentEngine._specialist_evidence(messages, str)
    assert "FINAL 42 passed" in evidence[0]["excerpt"]
    assert "truncated" in evidence[0]["excerpt"].lower()
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 16000


def test_only_matched_current_tool_results_no_hidden_or_unmatched_history():
    messages = [{"role": "system", "content": "HIDDEN_SENTINEL"}, {"role": "user", "content": "OTHER_HISTORY"},
                {"role": "tool", "tool_call_id": "unmatched", "content": "UNMATCHED_SENTINEL"}]
    tool(messages, "read", "filesystem", {"action": "read", "path": "SECRET.txt"}, {"ok": True, "result": {"content": "SECRET"}})
    evidence = AgentEngine._specialist_evidence(messages, lambda value: str(value).replace("SECRET", "[REDACTED]"))
    text = json.dumps(evidence)
    assert all(term not in text for term in ("HIDDEN_SENTINEL", "OTHER_HISTORY", "UNMATCHED_SENTINEL", "SECRET"))
    assert "REDACTED" in text


def test_latest_same_target_pass_replaces_old_failure():
    messages = []
    for call_id, code, text in [("old", 1, "OLD_FAILURE"), ("new", 0, "NEW_PASS")]:
        tool(messages, call_id, "shell", {"command": "pytest tests/test_cart.py"}, {"ok": code == 0, "result": {"exit_code": code, "stdout": text}})
    for index in range(4):
        tool(messages, str(index), "filesystem", {"action": "list", "path": "."}, {"ok": True})
    text = json.dumps(AgentEngine._specialist_evidence(messages, str))
    assert "NEW_PASS" in text and "OLD_FAILURE" not in text


def test_json_escaped_content_is_bounded_and_retains_truncation_marker():
    messages = []
    for index in range(6):
        tool(messages, str(index), "filesystem", {"action": "read", "path": "code.py"}, {"ok": True, "result": {"content": "\x00\\\"" * 10000}})
    evidence = AgentEngine._specialist_evidence(messages, str)
    assert len(evidence) == 4
    assert len(json.dumps(evidence, ensure_ascii=False)) < 15000
    assert all("truncated" in item["excerpt"] for item in evidence)
