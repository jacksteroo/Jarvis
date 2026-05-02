"""
Phase 5.4 — Pepper as MCP Server.

Exposes a configurable subset of Pepper's tools to external MCP clients
(Claude Desktop, Claude Code, Cursor, etc.).

Access control:
  - Tool allowlist configured in config/mcp_server_access.yaml
  - NEVER_EXPOSE list blocks all raw personal data tools regardless of config
  - Default (when config is absent): calendar read + web search only

Security model:
  MCP uses stdio transport — there is no per-request API key auth. Security
  relies on OS-level process isolation: only the process that launches this
  server can communicate with it. Do NOT expose stdio MCP servers over a
  network socket without adding an authentication layer at the transport level.

Run standalone::

    python -m agent.mcp_server

The server communicates over stdio (standard MCP transport).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import structlog
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = structlog.get_logger()

# Default tools exposed when no access config is specified.
# Conservative: read-only, no personal message content.
DEFAULT_ALLOWED_TOOLS = frozenset({
    "get_upcoming_events",
    "get_calendar_events_range",
    "list_calendars",
    "search_web",
})

# Tools that are NEVER exposed externally regardless of config.
# These handle raw personal data or have write side-effects.
NEVER_EXPOSE = frozenset({
    # Raw personal data tools (messages, email bodies, memory)
    "get_recent_imessages", "get_imessage_conversation", "search_imessages",
    "get_recent_whatsapp_chats", "get_whatsapp_chat", "get_whatsapp_messages",
    "search_whatsapp", "get_whatsapp_groups",
    "get_recent_emails", "search_emails",
    "search_slack", "get_slack_channel_messages",
    "search_memory", "save_memory",
    # Trace substrate (Epic 01 #25) — every trace row contains the full
    # input/output/tool_call args of a turn. Forward-defends future MCP
    # exposure of the in-process /traces route (#24).
    "query_traces", "get_trace", "search_traces",
    "find_similar_traces", "get_trace_by_id", "list_traces",
    # Write side-effects — require user approval, must never be exposed externally
    "update_life_context",
    "mark_commitment_complete",
})


def _load_access_config() -> frozenset[str]:
    """Load tool allowlist from config/mcp_server_access.yaml.

    Falls back to DEFAULT_ALLOWED_TOOLS if config doesn't exist.
    """
    config_path = Path(__file__).parent.parent / "config" / "mcp_server_access.yaml"
    if not config_path.exists():
        return DEFAULT_ALLOWED_TOOLS

    with open(config_path) as f:
        data = yaml.safe_load(f)

    if not data or not data.get("allowed_tools"):
        return DEFAULT_ALLOWED_TOOLS

    requested = set(data["allowed_tools"])
    # Strip out any tools from the NEVER_EXPOSE list
    violations = requested & NEVER_EXPOSE
    if violations:
        logger.warning(
            "mcp_server_access_blocked",
            blocked_tools=sorted(violations),
            reason="Tools in NEVER_EXPOSE cannot be exposed externally",
        )
    return frozenset(requested - NEVER_EXPOSE)


def create_pepper_mcp_server(pepper_core: Any | None = None) -> Server:
    """Create an MCP server exposing Pepper's tools.

    Args:
        pepper_core: Optional PepperCore instance. If None, all tool calls
            return an error — useful for testing the server without a running core.
    """
    server = Server("pepper")
    allowed_tools = _load_access_config()

    # Build the tool registry from Pepper's native tools.
    # Import lazily and degrade gracefully if a module is unavailable.
    all_tools: list[dict] = []
    _tool_modules = [
        ("agent.memory_tools", "MEMORY_TOOLS"),
        ("agent.calendar_tools", "CALENDAR_TOOLS"),
        ("agent.email_tools", "EMAIL_TOOLS"),
        ("agent.contact_tools", "CONTACT_TOOLS"),
        ("agent.comms_health_tools", "COMMS_HEALTH_TOOLS"),
    ]
    for module_path, attr in _tool_modules:
        try:
            mod = importlib.import_module(module_path)
            all_tools += getattr(mod, attr, [])
        except ImportError as e:
            logger.warning("mcp_server_tool_import_failed", module=module_path, error=str(e))
    tool_registry: dict[str, dict] = {}
    for t in all_tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        if name in allowed_tools:
            tool_registry[name] = fn

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description=fn.get("description", ""),
                inputSchema=fn.get("parameters", {"type": "object", "properties": {}}),
            )
            for name, fn in tool_registry.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None = None) -> list[TextContent]:
        if name not in tool_registry:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Tool '{name}' is not available or not allowed"}
            ))]

        arguments = arguments or {}
        # Log argument keys only — never log values as they may contain personal data
        logger.info("mcp_server_tool_call", tool=name, arg_keys=sorted(arguments.keys()))

        try:
            if pepper_core:
                result = await pepper_core._execute_tool(name, arguments)
            else:
                result = {"error": "Pepper core not initialized"}
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as e:
            logger.error("mcp_server_tool_error", tool=name, error=str(e))
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return server


async def main():
    """Run Pepper as a standalone MCP server.

    Note: when launched standalone (not embedded in PepperCore), tools that
    require the Pepper core to be running will return an error. For full
    functionality, embed this server in PepperCore using
    create_pepper_mcp_server(pepper_core=core).
    """
    logger.info(
        "pepper_mcp_server_starting",
        note="Standalone mode — tools requiring PepperCore will return errors",
    )
    server = create_pepper_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
