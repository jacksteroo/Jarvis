"""Tests for communication health tools.

Uses mocked channel data — no real DBs required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# get_comms_health_summary
# ---------------------------------------------------------------------------

class TestCommsHealthSummary:
    def test_returns_summary_key(self):
        with patch(
            "subsystems.communications.contact_enricher.find_quiet_contacts",
            new_callable=AsyncMock,
            return_value={"quiet_contacts": [], "count": 0},
        ), patch(
            "agent.comms_health_tools._get_overdue_responses_impl",
            new_callable=AsyncMock,
            return_value={"overdue": [], "count": 0},
        ):
            from agent.comms_health_tools import execute_get_comms_health_summary
            result = asyncio.run(execute_get_comms_health_summary({}))
        assert "summary" in result
        assert "signals" in result

    def test_surfaces_quiet_contacts_in_signals(self):
        quiet = [
            {"name": "Alice", "channel": "imessage", "last_contact_at": "2026-01-01", "days_quiet": 20},
            {"name": "Bob", "channel": "whatsapp", "last_contact_at": "2025-12-01", "days_quiet": 42},
        ]
        with patch(
            "subsystems.communications.contact_enricher.find_quiet_contacts",
            new_callable=AsyncMock,
            return_value={"quiet_contacts": quiet, "count": 2},
        ), patch(
            "agent.comms_health_tools._get_overdue_responses_impl",
            new_callable=AsyncMock,
            return_value={"overdue": [], "count": 0},
        ):
            from agent.comms_health_tools import execute_get_comms_health_summary
            result = asyncio.run(execute_get_comms_health_summary({"quiet_days": 14}))
        assert result["quiet_contact_count"] == 2
        assert any("Alice" in s or "Bob" in s for s in result["signals"])

    def test_surfaces_overdue_in_signals(self):
        overdue = [
            {"from": "Carol", "channel": "imessage", "unread_count": 3, "last_message_at": None},
        ]
        with patch(
            "subsystems.communications.contact_enricher.find_quiet_contacts",
            new_callable=AsyncMock,
            return_value={"quiet_contacts": [], "count": 0},
        ), patch(
            "agent.comms_health_tools._get_overdue_responses_impl",
            new_callable=AsyncMock,
            return_value={"overdue": overdue, "count": 1},
        ):
            from agent.comms_health_tools import execute_get_comms_health_summary
            result = asyncio.run(execute_get_comms_health_summary({}))
        assert result["overdue_response_count"] == 1
        assert any("Carol" in s for s in result["signals"])

    def test_all_clear_when_nothing(self):
        with patch(
            "subsystems.communications.contact_enricher.find_quiet_contacts",
            new_callable=AsyncMock,
            return_value={"quiet_contacts": [], "count": 0},
        ), patch(
            "agent.comms_health_tools._get_overdue_responses_impl",
            new_callable=AsyncMock,
            return_value={"overdue": [], "count": 0},
        ):
            from agent.comms_health_tools import execute_get_comms_health_summary
            result = asyncio.run(execute_get_comms_health_summary({}))
        assert result["signals"] == []
        assert "good" in result["summary"].lower() or "clear" in result["summary"].lower()


# ---------------------------------------------------------------------------
# get_overdue_responses
# ---------------------------------------------------------------------------

class TestOverdueResponses:
    def test_returns_count_and_summary(self):
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=False,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ):
            from agent.comms_health_tools import execute_get_overdue_responses
            result = asyncio.run(execute_get_overdue_responses({}))
        assert "count" in result
        assert "summary" in result
        assert result["count"] == 0

    def test_aggregates_imessage_unread(self):
        convos = [
            {"display_name": "Alice", "identifier": "+1", "message_count": 5,
             "last_message_at": "2026-01-01T10:00:00", "unread_count": 3},
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
            from agent.comms_health_tools import execute_get_overdue_responses
            result = asyncio.run(execute_get_overdue_responses({}))
        assert result["count"] == 1
        assert result["overdue"][0]["from"] == "Alice"
        assert result["overdue"][0]["channel"] == "imessage"


# ---------------------------------------------------------------------------
# get_relationship_balance_report
# ---------------------------------------------------------------------------

class TestRelationshipBalance:
    def test_returns_balance_keys(self):
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=False,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=False,
        ), patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            from agent.comms_health_tools import execute_get_relationship_balance_report
            result = asyncio.run(execute_get_relationship_balance_report({}))
        assert "summary" in result

    def test_personal_heavy_gives_good_note(self):
        convos = [
            {"identifier": f"+{i}", "display_name": f"Person {i}",
             "message_count": 5, "last_message_at": "2026-01-01", "unread_count": 0}
            for i in range(8)
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
        ), patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            from agent.comms_health_tools import execute_get_relationship_balance_report
            result = asyncio.run(execute_get_relationship_balance_report({}))
        assert result["personal_contacts"] == 8
        assert "good" in result["balance_note"].lower() or "personal" in result["balance_note"].lower()


# ---------------------------------------------------------------------------
# get_comms_health_brief_section
# ---------------------------------------------------------------------------

class TestCommsHealthBriefSection:
    def test_returns_empty_when_all_clear(self):
        with patch(
            "agent.comms_health_tools.execute_get_comms_health_summary",
            new_callable=AsyncMock,
            return_value={"signals": [], "quiet_contact_count": 0, "overdue_response_count": 0,
                          "summary": "all clear"},
        ):
            from agent.comms_health_tools import get_comms_health_brief_section
            result = asyncio.run(get_comms_health_brief_section())
        assert result == ""

    def test_returns_section_when_signals_exist(self):
        with patch(
            "agent.comms_health_tools.execute_get_comms_health_summary",
            new_callable=AsyncMock,
            return_value={
                "signals": ["Haven't heard from: Alice, Bob"],
                "quiet_contact_count": 2,
                "overdue_response_count": 0,
                "summary": "Communication health signals: ...",
            },
        ):
            from agent.comms_health_tools import get_comms_health_brief_section
            result = asyncio.run(get_comms_health_brief_section())
        assert "Communication" in result or "Alice" in result

    def test_graceful_degradation_on_error(self):
        with patch(
            "agent.comms_health_tools.execute_get_comms_health_summary",
            new_callable=AsyncMock,
            side_effect=Exception("DB offline"),
        ):
            from agent.comms_health_tools import get_comms_health_brief_section
            result = asyncio.run(get_comms_health_brief_section())
        assert result == ""


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

class TestCommsHealthTools:
    def test_tool_schema_names(self):
        from agent.comms_health_tools import COMMS_HEALTH_TOOLS
        names = {t["function"]["name"] for t in COMMS_HEALTH_TOOLS}
        assert "get_comms_health_summary" in names
        assert "get_overdue_responses" in names
        assert "get_relationship_balance_report" in names

    def test_dispatcher_unknown_tool(self):
        from agent.comms_health_tools import execute_comms_health_tool
        result = asyncio.run(execute_comms_health_tool("unknown", {}))
        assert "error" in result
