"""
Phase 5.2 — Base MCP server for Pepper subsystems.

Provides a thin wrapper that converts existing Pepper tool definitions
(Anthropic function-calling format) into MCP-compatible tool servers.

Usage in a subsystem::

    from subsystems.mcp_base import create_subsystem_mcp_server

    server = create_subsystem_mcp_server(
        name="calendar",
        tools=CALENDAR_TOOLS,
        executor=execute_tool,
    )

    if __name__ == "__main__":
        server.run()
"""
from __future__ import annotations

import json
from typing import Any, Callable, Awaitable

from mcp.server import Server
from mcp.types import Tool, TextContent
import structlog

logger = structlog.get_logger()


def create_subsystem_mcp_server(
    name: str,
    tools: list[dict],
    executor: Callable[[str, dict], Awaitable[dict]],
) -> Server:
    """Create an MCP Server that exposes Pepper-format tools.

    Args:
        name: Subsystem name (e.g. "calendar", "communications-email")
        tools: Tool definitions in Anthropic function-calling format
        executor: async function(tool_name, args) -> result_dict
    """
    subsystem_name = name  # capture before inner function shadows it
    server = Server(f"pepper-{name}")

    # Parse tool definitions into MCP Tool objects
    _tool_defs: dict[str, dict] = {}
    for t in tools:
        fn = t.get("function", {})
        tool_name = fn.get("name", "")
        if tool_name:
            _tool_defs[tool_name] = fn

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        result = []
        for tool_name, fn in _tool_defs.items():
            result.append(Tool(
                name=tool_name,
                description=fn.get("description", ""),
                inputSchema=fn.get("parameters", {"type": "object", "properties": {}}),
            ))
        return result

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
        # NOTE: `name` here is the tool name; use `subsystem_name` for the subsystem.
        arguments = arguments or {}
        # Log argument keys only — never log values as they may contain personal data
        logger.info("mcp_subsystem_tool_call", subsystem=subsystem_name, tool=name,
                    arg_keys=sorted(arguments.keys()))
        try:
            result = await executor(name, arguments)
            return [TextContent(
                type="text",
                text=json.dumps(result),
            )]
        except Exception as e:
            logger.error("mcp_subsystem_tool_error", subsystem=subsystem_name, tool=name, error=str(e))
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(e)}),
            )]

    return server
