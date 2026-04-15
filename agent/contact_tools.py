"""Contact enrichment tool definitions for Pepper core.

Follows the same pattern as email_tools.py.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

CONTACT_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_contact_profile",
            "description": (
                "Look up a contact across all communication channels (iMessage, WhatsApp, email). "
                "Returns last contact time, which channel is used most, and relationship signals. "
                "Use when asked 'how are things with [person]?' or 'when did I last talk to [person]?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "Contact's name, phone number, email address, or Slack handle",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "find_quiet_contacts",
            "description": (
                "Find contacts who have been quiet (no messages) for more than N days. "
                "Use when asked 'who haven't I talked to lately?' or 'who should I check in with?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Flag contacts quiet for more than this many days (default 14)",
                        "default": 14,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_contacts",
            "description": (
                "Search for a contact by name across all channels. "
                "Use to find contact details before using get_contact_profile."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name or partial name to search for",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


async def execute_get_contact_profile(args: dict) -> dict:
    identifier = args.get("identifier", "")
    if not identifier:
        return {"error": "identifier is required"}
    try:
        from subsystems.communications.contact_enricher import get_contact_profile
        return await get_contact_profile(identifier)
    except Exception as e:
        logger.error("contact_profile_failed", error=str(e))
        return {"error": f"Contact lookup failed: {e}"}


async def execute_find_quiet_contacts(args: dict) -> dict:
    days = int(args.get("days", 14))
    try:
        from subsystems.communications.contact_enricher import find_quiet_contacts
        return await find_quiet_contacts(days=days)
    except Exception as e:
        logger.error("quiet_contacts_failed", error=str(e))
        return {"error": f"Quiet contacts lookup failed: {e}"}


async def execute_search_contacts(args: dict) -> dict:
    query = args.get("query", "")
    if not query:
        return {"error": "query is required"}
    try:
        from subsystems.communications.contact_enricher import search_contacts
        return await search_contacts(query=query)
    except Exception as e:
        logger.error("contact_search_failed", error=str(e))
        return {"error": f"Contact search failed: {e}"}


async def execute_contact_tool(name: str, args: dict) -> dict:
    if name == "get_contact_profile":
        return await execute_get_contact_profile(args)
    elif name == "find_quiet_contacts":
        return await execute_find_quiet_contacts(args)
    elif name == "search_contacts":
        return await execute_search_contacts(args)
    return {"error": f"Unknown contact tool: {name}"}
