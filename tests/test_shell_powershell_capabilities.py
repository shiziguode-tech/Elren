from __future__ import annotations

import os

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.shell import ShellTool


def test_shell_schema_documents_windows_powershell_51_chain_limit():
    text = ShellTool.description + str(ShellTool.parameters)
    assert "5.1" in text
    assert "&&" in text and "||" in text
    assert "separate" in text.casefold()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows PowerShell")
@pytest.mark.asyncio
@pytest.mark.parametrize("operator", ["&&", "||"])
async def test_real_powershell_parser_failure_has_actionable_hint(tmp_path, operator):
    command = f"Write-Output 'ELREN_FIRST' {operator} Write-Output 'ELREN_SECOND'"
    result = await ShellTool().execute(
        {"command": command, "timeout_seconds": 10},
        ToolContext(workspace=str(tmp_path), task_id="shell-capability", user_prompt="Check harmless shell parsing"),
    )
    assert result["exit_code"] != 0
    assert "InvalidEndOfLine" in result["stderr"]
    assert "ELREN_FIRST" not in result["stdout"]
    assert result["error_code"] == "unsupported_powershell_chain_operator"
    assert "separate" in result["recovery_hint"].casefold()
    assert "not rewritten" in result["recovery_hint"]


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows PowerShell")
@pytest.mark.asyncio
async def test_quoted_chain_symbols_are_not_rewritten_or_diagnosed(tmp_path):
    result = await ShellTool().execute(
        {"command": "Write-Output 'literal && ||'", "timeout_seconds": 10},
        ToolContext(workspace=str(tmp_path), task_id="shell-literal", user_prompt="Print a literal string"),
    )
    assert result["exit_code"] == 0
    assert "literal && ||" in result["stdout"]
    assert "error_code" not in result
