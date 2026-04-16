"""
Phase 5.2 — Communications subsystem as an MCP server.

Exposes email, iMessage, WhatsApp, Slack, contact enrichment,
and comms health tools via MCP.

Run standalone::

    python -m subsystems.communications.mcp_server
"""
from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server


async def _execute_comms_tool(name: str, args: dict) -> dict:
    """Route to existing communications tool implementations."""
    from agent.email_tools import (
        execute_get_recent_emails,
        execute_search_emails,
        execute_get_email_unread_counts,
    )
    from agent.imessage_tools import execute_imessage_tool
    from agent.whatsapp_tools import execute_whatsapp_tool
    from agent.slack_tools import execute_slack_tool
    from agent.contact_tools import execute_contact_tool
    from agent.comms_health_tools import execute_comms_health_tool

    # Email
    if name == "get_recent_emails":
        return await execute_get_recent_emails(args)
    elif name == "search_emails":
        return await execute_search_emails(args)
    elif name == "get_email_unread_counts":
        return await execute_get_email_unread_counts(args)

    # iMessage
    elif name in ("get_recent_imessages", "get_imessage_conversation", "search_imessages"):
        return await execute_imessage_tool(name, args)

    # WhatsApp
    elif name in (
        "get_recent_whatsapp_chats", "get_whatsapp_chat", "get_whatsapp_messages",
        "search_whatsapp", "get_whatsapp_groups",
    ):
        return await execute_whatsapp_tool(name, args)

    # Slack
    elif name in (
        "search_slack", "get_slack_channel_messages",
        "get_slack_deadlines", "list_slack_channels",
    ):
        return await execute_slack_tool(name, args)

    # Contacts
    elif name in ("get_contact_profile", "find_quiet_contacts", "search_contacts"):
        return await execute_contact_tool(name, args)

    # Comms health
    elif name in (
        "get_comms_health_summary", "get_overdue_responses",
        "get_relationship_balance_report",
    ):
        return await execute_comms_health_tool(name, args)

    return {"error": f"Unknown communications tool: {name}"}


def create_server():
    from agent.email_tools import EMAIL_TOOLS
    from agent.imessage_tools import IMESSAGE_TOOLS
    from agent.whatsapp_tools import WHATSAPP_TOOLS
    from agent.slack_tools import SLACK_TOOLS
    from agent.contact_tools import CONTACT_TOOLS
    from agent.comms_health_tools import COMMS_HEALTH_TOOLS
    from subsystems.mcp_base import create_subsystem_mcp_server

    all_tools = (
        EMAIL_TOOLS + IMESSAGE_TOOLS + WHATSAPP_TOOLS +
        SLACK_TOOLS + CONTACT_TOOLS + COMMS_HEALTH_TOOLS
    )
    return create_subsystem_mcp_server(
        name="communications",
        tools=all_tools,
        executor=_execute_comms_tool,
    )


async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
