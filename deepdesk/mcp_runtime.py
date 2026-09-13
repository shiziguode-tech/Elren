from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.tool_runtime import packaged_workspace_root


class MCPRuntime:
    """Real stdio MCP client for the bundled Elren workspace server."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.node = shutil.which("node") or ""
        self.server = (
            self.workspace
            / "work"
            / "openclaw-runtime"
            / "mcp-servers"
            / "elren-workspace.mjs"
        )
        package = packaged_workspace_root(self.workspace)
        if package != self.workspace:
            # Frozen macOS code and npm dependencies remain in the signed app;
            # MCP artifacts still belong to the writable Application Support root.
            native = package / "work" / "tool-runtime" / "native" / "node"
            executable = native / "bin" / "node"
            self.node = str(executable) if executable.is_file() else ""
            self.server = native / "mcp-servers" / "elren-workspace.mjs"
        self.last_error = ""
        self.last_protocol = ""
        self.last_server: dict[str, Any] = {}

    async def status(self, probe: bool = False) -> dict[str, Any]:
        configured = bool(self.node and self.server.is_file())
        # Preserve a real failed probe until a later real probe succeeds.  A
        # cheap status poll must not turn "unreachable" back into "ready" just
        # because the executable and script still exist on disk.
        ready = configured and not self.last_error
        tools: list[dict[str, Any]] = []
        if probe and configured:
            try:
                result = await self.request("tools/list", {})
                tools = result.get("tools", [])
                self.last_error = ""
                ready = True
            except Exception as exc:
                ready = False
                self.last_error = f"{type(exc).__name__}: {exc}"
        return {
            "configured": configured,
            "ready": ready,
            "transport": "stdio",
            "server": "elren-workspace",
            "protocol": self.last_protocol,
            "server_info": self.last_server,
            "tool_count": len(tools) if probe and ready else None,
            "tools": [tool.get("name") for tool in tools],
            "last_error": self.last_error,
        }

    async def list_tools(self) -> dict[str, Any]:
        return await self.request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.node or not self.server.is_file():
            raise RuntimeError("Bundled MCP server runtime is not installed")
        environment = credential_safe_environment(
            {
                "ELREN_WORKSPACE": str(self.workspace),
                # Compatibility for extensions created before the Elren rename.
                "DEEPDESK_WORKSPACE": str(self.workspace),
            }
        )
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            self.node,
            str(self.server),
            cwd=self.server.parent.parent,
            env=environment,
            creationflags=flags,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise RuntimeError("MCP subprocess pipes were not created")
        try:
            await self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "elren", "version": "1.0"},
                    },
                },
            )
            initialized = await self._response(process, 1)
            init_result = initialized.get("result", {})
            self.last_protocol = init_result.get("protocolVersion", "")
            self.last_server = init_result.get("serverInfo", {})
            await self._send(
                process,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            await self._send(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
            )
            response = await self._response(process, 2)
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "MCP request failed"))
            return response.get("result", {})
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                process.kill()
                await process.wait()

    @staticmethod
    async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise RuntimeError("MCP server stdin is unavailable")
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()

    @staticmethod
    async def _response(process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
        if process.stdout is None:
            raise RuntimeError("MCP server stdout is unavailable")
        for _ in range(50):
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            if not line:
                raise RuntimeError("MCP server closed stdout before responding")
            message = json.loads(line.decode("utf-8"))
            if message.get("id") == request_id:
                return message
        raise RuntimeError("MCP response limit exceeded")
