"""
Phase 5.2 — Calendar subsystem as an MCP server.

Run standalone::

    python -m subsystems.calendar.mcp_server

Or connect via stdio from Pepper's MCP client.
"""
from __future__ import annotations

import asyncio
import sys

from mcp.server.stdio import stdio_server


async def _execute_calendar_tool(name: str, args: dict) -> dict:
    """Route to existing calendar tool implementations."""
    from agent.calendar_tools import (
        execute_get_upcoming_events,
        execute_get_calendar_events_range,
        execute_list_calendars,
    )
    if name == "get_upcoming_events":
        return await execute_get_upcoming_events(args)
    elif name == "get_calendar_events_range":
        return await execute_get_calendar_events_range(args)
    elif name == "list_calendars":
        return await execute_list_calendars()
    return {"error": f"Unknown calendar tool: {name}"}


def create_server():
    from agent.calendar_tools import CALENDAR_TOOLS
    from subsystems.mcp_base import create_subsystem_mcp_server
    return create_subsystem_mcp_server(
        name="calendar",
        tools=CALENDAR_TOOLS,
        executor=_execute_calendar_tool,
    )


async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
