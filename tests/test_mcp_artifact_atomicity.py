"""Actual Node/MCP file replacement, with injected rename failures in QA files."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from deepdesk.mcp_runtime import MCPRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [True, False])
async def test_mcp_replace_preserves_old_file_until_rename_succeeds(tmp_path, failure):
    root = Path(__file__).resolve().parents[1]
    source = Path(os.environ.get("ELREN_MCP_TEST_SERVER", root / "work/openclaw-runtime/mcp-servers/elren-workspace.mjs"))
    runtime = MCPRuntime(tmp_path)
    runtime.node = os.environ.get("ELREN_MCP_TEST_NODE", runtime.node)
    if not runtime.node or not (source.parent.parent / "node_modules/@modelcontextprotocol/sdk").is_dir():
        pytest.skip("Installed MCP SDK and Node required")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    old = outputs / "保留 原文件.txt"
    old.write_text("ORIGINAL CONTENT", encoding="utf-8")
    # Keep the temporary wrapper beside the existing reviewed server so Node
    # resolves its already installed SDK. Never modify the server or modules.
    with tempfile.TemporaryDirectory(prefix="atomic-mcp-qa-", dir=source.parent) as directory:
        wrapper = Path(directory) / "server.mjs"
        injected = (
            "import fs from 'node:fs/promises';\n"
            "fs.rename = async () => { const e = new Error('Synthetic rename failure'); e.code='EIO'; throw e; };\n"
            if failure else ""
        )
        wrapper.write_text(injected + "await import(" + json.dumps(source.as_uri()) + ");\n", encoding="utf-8")
        runtime.server = wrapper
        result = await runtime.call_tool("write_artifact", {"path": old.name, "content": "REPLACEMENT CONTENT"})
    if failure:
        assert result.get("isError") is True
        assert old.exists(), "Failed replacement deleted the previous artifact"
        assert old.read_text("utf-8") == "ORIGINAL CONTENT"
    else:
        assert not result.get("isError")
        assert old.read_text("utf-8") == "REPLACEMENT CONTENT"
    assert sorted(file.name for file in outputs.iterdir()) == [old.name]
