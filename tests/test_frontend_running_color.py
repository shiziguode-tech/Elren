from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "deepdesk" / "static"


def test_running_dots_are_green_without_recoloring_queued():
    history = (STATIC / "history.css").read_text(encoding="utf-8")
    editorial = (STATIC / "editorial-ui.css").read_text(encoding="utf-8")
    assert ".history-status.running { background: #4d9364; }" in history
    assert ".history-status.queued { background: var(--accent); }" in history
    assert "--editorial-positive: #4d9364;" in editorial
    assert ".history-status.running { background: var(--editorial-positive); }" in editorial
    assert ".history-status.queued { background: var(--editorial-accent); }" in editorial
    assert '.subagent-item[data-status="running"] .subagent-item-status::before {\n  background: var(--editorial-positive);\n}' in editorial
    assert '.subagent-item[data-status="queued"] .subagent-item-status::before {\n  background: var(--editorial-warning);\n}' in editorial
