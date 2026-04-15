"""Contact enrichment for Pepper.

Cross-references contacts across all communication channels:
  - iMessage (~/Library/Messages/chat.db)
  - WhatsApp (~/Library/Application Support/WhatsApp/ChatStorage.sqlite)
  - Slack (API)
  - Email (Gmail/IMAP)

Builds richer profiles: last contact time, dominant channel, relationship health signals.
Stores contact metadata in PostgreSQL for persistence.

Privacy: all enrichment is local. Summaries only go to frontier LLM.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"[^\d+]")


def normalize_phone(phone: str) -> str:
    """Strip formatting from a phone number, keeping leading +."""
    if not phone:
        return ""
    stripped = _PHONE_RE.sub("", phone)
    if not stripped.startswith("+") and phone.startswith("+"):
        stripped = "+" + stripped
    return stripped


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# Cross-channel enrichment
# ---------------------------------------------------------------------------

async def _gather_imessage_data(identifier: str) -> Optional[dict]:
    """Fetch last-contact info from iMessage for a given identifier."""
    try:
        from subsystems.communications.imessage_client import IMessageClient
        if not IMessageClient.is_available():
            return None
        client = IMessageClient()
        convos = await client.get_recent_conversations(limit=50, days=365)
        for c in convos:
            if identifier.lower() in (c["identifier"] or "").lower() or \
               identifier.lower() in (c["display_name"] or "").lower():
                return {
                    "channel": "imessage",
                    "last_contact_at": c["last_message_at"],
                    "message_count_30d": c["message_count"],
                }
    except Exception as e:
        logger.debug("enricher_imessage_skip", error=str(e))
    return None


async def _gather_whatsapp_data(identifier: str) -> Optional[dict]:
    """Fetch last-contact info from WhatsApp for a given identifier."""
    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        if not WhatsAppClient.is_available():
            return None
        client = WhatsAppClient()
        chats = await client.get_recent_chats(limit=50)
        for c in chats:
            if identifier.lower() in (c["name"] or "").lower() or \
               identifier.lower() in (c["jid"] or "").lower():
                return {
                    "channel": "whatsapp",
                    "last_contact_at": c["last_message_at"],
                    "message_count_30d": 0,  # WA DB doesn't expose 30d count easily
                }
    except Exception as e:
        logger.debug("enricher_whatsapp_skip", error=str(e))
    return None


async def _gather_email_data(identifier: str) -> Optional[dict]:
    """Fetch last-contact info from email for a given identifier."""
    try:
        from agent.email_tools import execute_search_emails
        result = await execute_search_emails({"query": f"from:{identifier}", "count": 1})
        if not result.get("error") and result.get("count", 0) > 0:
            # Extract date from first email
            emails = result.get("emails", [])
            if emails:
                # Format: "DATE — FROM — SUBJECT..."
                first = emails[0]
                date_part = first.split(" — ")[0] if " — " in first else None
                return {
                    "channel": "email",
                    "last_contact_at": date_part,
                    "message_count_30d": result["count"],
                }
    except Exception as e:
        logger.debug("enricher_email_skip", error=str(e))
    return None


async def get_contact_profile(identifier: str) -> dict:
    """Build a multi-channel profile for a contact.

    Args:
        identifier: Name, phone number, email, or Slack handle to look up.

    Returns:
        Profile dict with channels, last_contact_at, dominant_channel, etc.
    """
    identifier = identifier.strip()
    if not identifier:
        return {"error": "identifier is required"}

    # Gather data from all available channels concurrently
    imessage_task = asyncio.create_task(_gather_imessage_data(identifier))
    whatsapp_task = asyncio.create_task(_gather_whatsapp_data(identifier))
    email_task = asyncio.create_task(_gather_email_data(identifier))

    channel_data = []
    for result in await asyncio.gather(
        imessage_task, whatsapp_task, email_task, return_exceptions=True
    ):
        if isinstance(result, dict) and result is not None:
            channel_data.append(result)

    if not channel_data:
        return {
            "identifier": identifier,
            "found": False,
            "channels": [],
            "summary": f"No contact data found for '{identifier}' across any channel.",
        }

    # Determine dominant channel (most messages in 30d)
    dominant = max(channel_data, key=lambda x: x.get("message_count_30d", 0))

    # Latest contact time across all channels
    timestamps = [
        c["last_contact_at"] for c in channel_data if c.get("last_contact_at")
    ]
    latest = max(timestamps) if timestamps else None

    profile = {
        "identifier": identifier,
        "found": True,
        "channels": [c["channel"] for c in channel_data],
        "dominant_channel": dominant["channel"],
        "last_contact_at": latest,
        "channel_details": channel_data,
        "summary": (
            f"Found '{identifier}' on {len(channel_data)} channel(s): "
            f"{', '.join(c['channel'] for c in channel_data)}. "
            f"Last contact: {latest or 'unknown'}. "
            f"Primary channel: {dominant['channel']}."
        ),
    }

    logger.debug("contact_profile_built", identifier=identifier[:30], channels=len(channel_data))
    return profile


async def find_quiet_contacts(days: int = 14) -> dict:
    """Find contacts who've been quiet (no messages in/out) for N days.

    Checks iMessage and WhatsApp for contacts with old last_message_at timestamps.
    """
    quiet = []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Check iMessage
    try:
        from subsystems.communications.imessage_client import IMessageClient
        if IMessageClient.is_available():
            client = IMessageClient()
            convos = await client.get_recent_conversations(limit=50, days=365)
            for c in convos:
                if c["last_message_at"]:
                    try:
                        last = datetime.fromisoformat(c["last_message_at"])
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if last < cutoff:
                            quiet.append({
                                "name": c["display_name"],
                                "channel": "imessage",
                                "last_contact_at": c["last_message_at"],
                                "days_quiet": (now - last).days,
                            })
                    except ValueError:
                        pass
    except Exception as e:
        logger.debug("quiet_contacts_imessage_skip", error=str(e))

    # Check WhatsApp
    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        if WhatsAppClient.is_available():
            client = WhatsAppClient()
            chats = await client.get_recent_chats(limit=50)
            for c in chats:
                if not c["is_group"] and c["last_message_at"]:
                    try:
                        last = datetime.fromisoformat(c["last_message_at"])
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if last < cutoff:
                            quiet.append({
                                "name": c["name"],
                                "channel": "whatsapp",
                                "last_contact_at": c["last_message_at"],
                                "days_quiet": (now - last).days,
                            })
                    except ValueError:
                        pass
    except Exception as e:
        logger.debug("quiet_contacts_whatsapp_skip", error=str(e))

    # Sort by days_quiet descending
    quiet.sort(key=lambda x: x["days_quiet"], reverse=True)
    logger.debug("quiet_contacts_found", count=len(quiet), days=days)
    return {
        "quiet_contacts": quiet,
        "count": len(quiet),
        "days_threshold": days,
        "summary": f"{len(quiet)} contact(s) have been quiet for more than {days} days.",
    }


async def search_contacts(query: str) -> dict:
    """Search for a contact by name across all channels."""
    query = query.strip().lower()
    if not query:
        return {"error": "query is required"}

    results = []

    # iMessage
    try:
        from subsystems.communications.imessage_client import IMessageClient
        if IMessageClient.is_available():
            client = IMessageClient()
            convos = await client.get_recent_conversations(limit=50, days=365)
            for c in convos:
                name = (c["display_name"] or "").lower()
                if query in name or query in (c["identifier"] or "").lower():
                    results.append({
                        "name": c["display_name"],
                        "identifier": c["identifier"],
                        "channel": "imessage",
                        "last_contact_at": c["last_message_at"],
                    })
    except Exception as e:
        logger.debug("contact_search_imessage_skip", error=str(e))

    # WhatsApp
    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        if WhatsAppClient.is_available():
            client = WhatsAppClient()
            chats = await client.get_recent_chats(limit=50)
            for c in chats:
                if query in (c["name"] or "").lower():
                    results.append({
                        "name": c["name"],
                        "identifier": c["jid"],
                        "channel": "whatsapp",
                        "last_contact_at": c["last_message_at"],
                    })
    except Exception as e:
        logger.debug("contact_search_whatsapp_skip", error=str(e))

    logger.debug("contact_search_done", query=query, count=len(results))
    return {
        "contacts": results,
        "count": len(results),
        "query": query,
        "summary": f"Found {len(results)} contact(s) matching '{query}'.",
    }
