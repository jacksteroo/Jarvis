"""Tests for contact enricher and tools.

Uses mocked channel clients — no real DBs required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

class TestNormalizers:
    def test_normalize_phone_strips_formatting(self):
        from subsystems.communications.contact_enricher import normalize_phone
        assert normalize_phone("+1 (555) 123-4567") == "+15551234567"
        assert normalize_phone("555.123.4567") == "5551234567"
        assert normalize_phone("+447700123456") == "+447700123456"

    def test_normalize_phone_empty(self):
        from subsystems.communications.contact_enricher import normalize_phone
        assert normalize_phone("") == ""

    def test_normalize_email_lowercase(self):
        from subsystems.communications.contact_enricher import normalize_email
        assert normalize_email("Alice@Example.COM") == "alice@example.com"
        assert normalize_email("  bob@test.io  ") == "bob@test.io"


# ---------------------------------------------------------------------------
# get_contact_profile
# ---------------------------------------------------------------------------

class TestGetContactProfile:
    def _make_imessage_convos(self):
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        return [
            {
                "identifier": "+15551234567",
                "display_name": "Alice",
                "message_count": 12,
                "last_message_at": two_days_ago,
                "unread_count": 0,
            }
        ]

    def test_profile_found_on_imessage(self):
        convos = self._make_imessage_convos()
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=convos,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ), patch(
            "agent.email_tools.execute_search_emails",
            new_callable=AsyncMock,
            return_value={"error": "not configured"},
        ):
            result = asyncio.run(
                __import__(
                    "subsystems.communications.contact_enricher",
                    fromlist=["get_contact_profile"],
                ).get_contact_profile("Alice")
            )
        assert result["found"] is True
        assert "imessage" in result["channels"]
        assert result["dominant_channel"] == "imessage"

    def test_profile_not_found(self):
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=False,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ), patch(
            "agent.email_tools.execute_search_emails",
            new_callable=AsyncMock,
            return_value={"error": "no results"},
        ):
            from subsystems.communications.contact_enricher import get_contact_profile
            result = asyncio.run(get_contact_profile("Nonexistent Person"))
        assert result["found"] is False
        assert result["channels"] == []

    def test_profile_empty_identifier(self):
        from subsystems.communications.contact_enricher import get_contact_profile
        result = asyncio.run(get_contact_profile(""))
        assert "error" in result


# ---------------------------------------------------------------------------
# find_quiet_contacts
# ---------------------------------------------------------------------------

class TestFindQuietContacts:
    def test_finds_contacts_older_than_threshold(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        convos = [
            {"identifier": "+15551", "display_name": "Old Contact", "message_count": 5,
             "last_message_at": old_date, "unread_count": 0},
            {"identifier": "+15552", "display_name": "Recent Contact", "message_count": 10,
             "last_message_at": recent_date, "unread_count": 0},
        ]
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=convos,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from subsystems.communications.contact_enricher import find_quiet_contacts
            result = asyncio.run(find_quiet_contacts(days=14))
        assert result["count"] == 1
        assert result["quiet_contacts"][0]["name"] == "Old Contact"

    def test_returns_sorted_by_days_quiet(self):
        d30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        d60 = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        convos = [
            {"identifier": "+1", "display_name": "30 Days", "message_count": 1,
             "last_message_at": d30, "unread_count": 0},
            {"identifier": "+2", "display_name": "60 Days", "message_count": 1,
             "last_message_at": d60, "unread_count": 0},
        ]
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=convos,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from subsystems.communications.contact_enricher import find_quiet_contacts
            result = asyncio.run(find_quiet_contacts(days=14))
        assert result["count"] == 2
        assert result["quiet_contacts"][0]["days_quiet"] > result["quiet_contacts"][1]["days_quiet"]

    def test_empty_when_all_channels_unavailable(self):
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=False,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from subsystems.communications.contact_enricher import find_quiet_contacts
            result = asyncio.run(find_quiet_contacts(days=14))
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# search_contacts
# ---------------------------------------------------------------------------

class TestSearchContacts:
    def test_search_requires_query(self):
        from subsystems.communications.contact_enricher import search_contacts
        result = asyncio.run(search_contacts(""))
        assert "error" in result

    def test_search_finds_imessage_contact(self):
        convos = [
            {"identifier": "+15551234567", "display_name": "Alice Wonderland",
             "message_count": 5, "last_message_at": "2026-01-01T10:00:00", "unread_count": 0},
        ]
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=convos,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from subsystems.communications.contact_enricher import search_contacts
            result = asyncio.run(search_contacts("alice"))
        assert result["count"] == 1
        assert result["contacts"][0]["name"] == "Alice Wonderland"

    def test_search_case_insensitive(self):
        convos = [
            {"identifier": "+1", "display_name": "Bob Smith",
             "message_count": 3, "last_message_at": "2026-01-01T09:00:00", "unread_count": 0},
        ]
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=convos,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from subsystems.communications.contact_enricher import search_contacts
            result = asyncio.run(search_contacts("BOB"))
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------

class TestContactTools:
    def test_tool_schema_names(self):
        from agent.contact_tools import CONTACT_TOOLS
        names = {t["function"]["name"] for t in CONTACT_TOOLS}
        assert "get_contact_profile" in names
        assert "find_quiet_contacts" in names
        assert "search_contacts" in names

    def test_execute_profile_requires_identifier(self):
        from agent.contact_tools import execute_get_contact_profile
        result = asyncio.run(execute_get_contact_profile({}))
        assert "error" in result

    def test_execute_search_requires_query(self):
        from agent.contact_tools import execute_search_contacts
        result = asyncio.run(execute_search_contacts({}))
        assert "error" in result

    def test_dispatcher_unknown_tool(self):
        from agent.contact_tools import execute_contact_tool
        result = asyncio.run(execute_contact_tool("unknown", {}))
        assert "error" in result

    def test_execute_find_quiet_uses_default_days(self):
        with patch(
            "subsystems.communications.contact_enricher.find_quiet_contacts",
            new_callable=AsyncMock,
            return_value={"quiet_contacts": [], "count": 0, "days_threshold": 14, "summary": ""},
        ) as mock:
            from agent.contact_tools import execute_find_quiet_contacts
            asyncio.run(execute_find_quiet_contacts({}))
            mock.assert_called_once_with(days=14)
