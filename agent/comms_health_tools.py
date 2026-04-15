"""Communication health tools for Pepper.

Surfaces relationship health signals:
  - Who have you not responded to?
  - Who's been quiet that you should check in on?
  - Is there balance between personal and professional contacts?
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

COMMS_HEALTH_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_comms_health_summary",
            "description": (
                "Get a communication health summary: who have you not responded to, "
                "who's been reaching out, and relationship balance signals. "
                "Use when asked about your communication health, relationship status, "
                "or 'who should I reach out to?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "quiet_days": {
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
            "name": "get_overdue_responses",
            "description": (
                "Find messages that need a response — unread iMessages or WhatsApp messages "
                "that have been waiting more than 48 hours. "
                "Use when asked 'who am I ghosting?' or 'who needs a reply?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Flag messages unread for more than this many hours (default 48)",
                        "default": 48,
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
            "name": "get_relationship_balance_report",
            "description": (
                "Report on the balance of personal vs professional contacts you've been in touch with. "
                "Use when asked about relationship balance or whether you've been too focused on work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Look back this many days (default 30)",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
]


async def execute_get_comms_health_summary(args: dict) -> dict:
    quiet_days = int(args.get("quiet_days", 14))
    try:
        from subsystems.communications.contact_enricher import find_quiet_contacts
        quiet_result = await find_quiet_contacts(days=quiet_days)
        quiet = quiet_result.get("quiet_contacts", [])

        # Also check for unread messages across channels
        overdue = await _get_overdue_responses_impl(hours=48)

        signals = []
        if quiet:
            top_quiet = quiet[:3]
            names = ", ".join(c["name"] for c in top_quiet)
            signals.append(f"Haven't heard from: {names} (and {len(quiet) - len(top_quiet)} more)")
        if overdue.get("overdue"):
            top_overdue = overdue["overdue"][:3]
            signals.append(
                f"{len(overdue['overdue'])} message(s) awaiting your reply: "
                + ", ".join(f"{m['from']} ({m['channel']})" for m in top_overdue)
            )

        if not signals:
            summary = "Communication health looks good — inbox clear and contacts active."
        else:
            summary = "Communication health signals:\n" + "\n".join(f"• {s}" for s in signals)

        return {
            "signals": signals,
            "quiet_contact_count": len(quiet),
            "overdue_response_count": len(overdue.get("overdue", [])),
            "summary": summary,
        }
    except Exception as e:
        logger.error("comms_health_failed", error=str(e))
        return {"error": f"Communication health check failed: {e}"}


async def _get_overdue_responses_impl(hours: int) -> dict:
    """Gather overdue responses across all available channels."""
    overdue = []

    # iMessage unread
    try:
        from subsystems.communications.imessage_client import IMessageClient
        if IMessageClient.is_available():
            client = IMessageClient()
            convos = await client.get_recent_conversations(limit=30, days=30)
            for c in convos:
                if c["unread_count"] > 0:
                    overdue.append({
                        "from": c["display_name"],
                        "channel": "imessage",
                        "unread_count": c["unread_count"],
                        "last_message_at": c["last_message_at"],
                    })
    except Exception as e:
        logger.debug("overdue_imessage_skip", error=str(e))

    # WhatsApp unread
    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        if WhatsAppClient.is_available():
            client = WhatsAppClient()
            chats = await client.get_recent_chats(limit=30)
            for c in chats:
                if c["unread_count"] > 0:
                    overdue.append({
                        "from": c["name"],
                        "channel": "whatsapp",
                        "unread_count": c["unread_count"],
                        "last_message_at": c["last_message_at"],
                    })
    except Exception as e:
        logger.debug("overdue_whatsapp_skip", error=str(e))

    return {
        "overdue": overdue,
        "count": len(overdue),
    }


async def execute_get_overdue_responses(args: dict) -> dict:
    hours = int(args.get("hours", 48))
    try:
        result = await _get_overdue_responses_impl(hours=hours)
        overdue = result["overdue"]
        formatted = []
        for m in overdue:
            formatted.append(
                f"{m['from']} ({m['channel']}): {m['unread_count']} unread"
                + (f" — last: {m['last_message_at']}" if m.get("last_message_at") else "")
            )
        return {
            "overdue": overdue,
            "formatted": formatted,
            "count": len(overdue),
            "summary": (
                f"{len(overdue)} conversation(s) with unread messages awaiting your reply."
                if overdue else "No overdue responses — inbox clear."
            ),
        }
    except Exception as e:
        logger.error("overdue_responses_failed", error=str(e))
        return {"error": f"Overdue response check failed: {e}"}


async def execute_get_relationship_balance_report(args: dict) -> dict:
    days = int(args.get("days", 30))
    try:
        personal_channels = []
        work_channels = []

        # iMessage is mostly personal
        try:
            from subsystems.communications.imessage_client import IMessageClient
            if IMessageClient.is_available():
                client = IMessageClient()
                convos = await client.get_recent_conversations(limit=50, days=days)
                personal_channels.extend([
                    {"name": c["display_name"], "channel": "imessage", "messages": c["message_count"]}
                    for c in convos
                ])
        except Exception:
            pass

        # WhatsApp is mostly personal
        try:
            from subsystems.communications.whatsapp_client import WhatsAppClient
            if WhatsAppClient.is_available():
                client = WhatsAppClient()
                chats = await client.get_recent_chats(limit=50)
                personal_channels.extend([
                    {"name": c["name"], "channel": "whatsapp", "messages": 0}
                    for c in chats if not c["is_group"]
                ])
        except Exception:
            pass

        # Slack is mostly work
        import os
        if os.environ.get("SLACK_BOT_TOKEN"):
            work_channels.append({"name": "Slack (work)", "channel": "slack", "messages": 0})

        personal_count = len(personal_channels)
        work_count = len(work_channels)
        total = personal_count + work_count

        if total == 0:
            return {
                "summary": "Not enough data to assess relationship balance — no channels configured.",
                "personal_contacts": 0,
                "work_contacts": 0,
            }

        personal_pct = round(100 * personal_count / total) if total else 0
        work_pct = 100 - personal_pct

        if personal_pct >= 60:
            balance_note = "Good — personal connections are well-maintained."
        elif work_pct >= 70:
            balance_note = "Heads-up — most recent contact has been work-focused. Make time for personal connections."
        else:
            balance_note = "Balance looks reasonable between personal and work contacts."

        return {
            "personal_contacts": personal_count,
            "work_contacts": work_count,
            "personal_pct": personal_pct,
            "work_pct": work_pct,
            "days": days,
            "balance_note": balance_note,
            "summary": (
                f"Last {days} days: {personal_count} personal contact(s), {work_count} work channel(s). "
                f"{balance_note}"
            ),
        }
    except Exception as e:
        logger.error("relationship_balance_failed", error=str(e))
        return {"error": f"Relationship balance report failed: {e}"}


async def execute_comms_health_tool(name: str, args: dict) -> dict:
    if name == "get_comms_health_summary":
        return await execute_get_comms_health_summary(args)
    elif name == "get_overdue_responses":
        return await execute_get_overdue_responses(args)
    elif name == "get_relationship_balance_report":
        return await execute_get_relationship_balance_report(args)
    return {"error": f"Unknown comms health tool: {name}"}


async def get_comms_health_brief_section(quiet_days: int = 14) -> str:
    """Generate 1-2 line communication health snippet for morning brief.

    Returns empty string if no signals worth surfacing.
    """
    try:
        result = await execute_get_comms_health_summary({"quiet_days": quiet_days})
        signals = result.get("signals", [])
        if not signals:
            return ""
        # Surface max 2 items to keep brief tight
        lines = ["📱 **Communication**"]
        for s in signals[:2]:
            lines.append(f"• {s}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("comms_health_brief_failed", error=str(e))
        return ""
