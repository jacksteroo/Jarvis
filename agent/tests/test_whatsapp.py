"""Tests for WhatsApp client and tools.

Uses mock SQLite data and export file parsing — does not require the real WhatsApp DB.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: build a minimal WhatsApp-like DB
# ---------------------------------------------------------------------------

def _build_test_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY,
            ZCONTACTJID TEXT,
            ZPARTNERNAME TEXT,
            ZUNREADCOUNT INTEGER DEFAULT 0,
            ZLASTMESSAGEDATE REAL
        );
        CREATE TABLE IF NOT EXISTS ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZTEXT TEXT,
            ZMESSAGEDATE REAL,
            ZISFROMME INTEGER DEFAULT 0,
            ZCHATSESSION INTEGER,
            ZFROMJID TEXT
        );
        CREATE TABLE IF NOT EXISTS ZWAGROUPMEMBER (
            Z_PK INTEGER PRIMARY KEY,
            ZMEMBERJID TEXT,
            ZCHATSESSION INTEGER
        );

        -- Personal chats
        INSERT INTO ZWACHATSESSION VALUES (1, '15551234567@s.whatsapp.net', 'Alice', 3, 700000000.0);
        INSERT INTO ZWACHATSESSION VALUES (2, '15559876543@s.whatsapp.net', 'Bob', 0, 699000000.0);
        -- Group chat
        INSERT INTO ZWACHATSESSION VALUES (3, 'family-group-id@g.us', 'Family Group', 5, 701000000.0);

        -- Messages
        INSERT INTO ZWAMESSAGE VALUES (1, 'Hey, dinner tomorrow?', 700000000.0, 0, 1, '15551234567@s.whatsapp.net');
        INSERT INTO ZWAMESSAGE VALUES (2, 'Sounds great!', 700000001.0, 1, 1, NULL);
        INSERT INTO ZWAMESSAGE VALUES (3, 'Family reunion this weekend!', 701000000.0, 0, 3, '15551111111@s.whatsapp.net');


        -- Group members
        INSERT INTO ZWAGROUPMEMBER VALUES (1, '15551111111@s.whatsapp.net', 3);
        INSERT INTO ZWAGROUPMEMBER VALUES (2, '15552222222@s.whatsapp.net', 3);
        INSERT INTO ZWAGROUPMEMBER VALUES (3, '15553333333@s.whatsapp.net', 3);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests for whatsapp_client
# ---------------------------------------------------------------------------

class TestWhatsAppClient:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        _build_test_db(self.tmp.name)
        self.db_path = Path(self.tmp.name)

    def teardown_method(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_get_recent_chats(self):
        from subsystems.communications.whatsapp_client import _get_recent_chats_sync
        results = _get_recent_chats_sync(limit=10, db_path=self.db_path)
        assert len(results) == 3
        names = [r["name"] for r in results]
        assert "Family Group" in names

    def test_get_recent_chats_identifies_groups(self):
        from subsystems.communications.whatsapp_client import _get_recent_chats_sync
        results = _get_recent_chats_sync(limit=10, db_path=self.db_path)
        groups = [r for r in results if r["is_group"]]
        personal = [r for r in results if not r["is_group"]]
        assert len(groups) == 1
        assert groups[0]["name"] == "Family Group"
        assert len(personal) == 2

    def test_get_chat_messages(self):
        from subsystems.communications.whatsapp_client import _get_chat_messages_sync
        msgs = _get_chat_messages_sync(chat_id=1, limit=10, db_path=self.db_path)
        assert len(msgs) == 2
        assert any("dinner tomorrow" in m["text"] for m in msgs)

    def test_search_messages_parameterized(self):
        from subsystems.communications.whatsapp_client import _search_messages_sync
        results = _search_messages_sync("dinner", limit=10, db_path=self.db_path)
        assert len(results) >= 1
        assert "dinner" in results[0]["text"]

    def test_search_sql_injection_safe(self):
        from subsystems.communications.whatsapp_client import _search_messages_sync
        results = _search_messages_sync("'; DROP TABLE ZWAMESSAGE; --", limit=5, db_path=self.db_path)
        assert isinstance(results, list)

    def test_get_group_chats(self):
        from subsystems.communications.whatsapp_client import _get_group_chats_sync
        groups = _get_group_chats_sync(db_path=self.db_path)
        assert len(groups) == 1
        assert groups[0]["member_count"] == 3
        assert groups[0]["name"] == "Family Group"

    def test_db_not_found_raises(self):
        from subsystems.communications.whatsapp_client import _get_recent_chats_sync
        with pytest.raises(FileNotFoundError):
            _get_recent_chats_sync(limit=5, db_path=Path("/nonexistent/ChatStorage.sqlite"))

    def test_is_available(self):
        from subsystems.communications.whatsapp_client import WhatsAppClient
        assert WhatsAppClient.is_available(self.db_path) is True

    def test_is_available_false(self):
        from subsystems.communications.whatsapp_client import WhatsAppClient
        assert WhatsAppClient.is_available(Path("/nonexistent/file.sqlite")) is False


# ---------------------------------------------------------------------------
# Tests for export file parsing
# ---------------------------------------------------------------------------

class TestExportParsing:
    def test_parse_standard_format(self):
        from subsystems.communications.whatsapp_client import parse_export_file
        content = (
            "1/15/24, 3:45 PM - Alice: Hey there!\n"
            "1/15/24, 3:46 PM - Bob: Hi Alice!\n"
            "1/15/24, 3:47 PM - Alice: How are you?\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            msgs = parse_export_file(tmp)
            assert len(msgs) == 3
            assert msgs[0]["sender"] == "Alice"
            assert msgs[0]["text"] == "Hey there!"
        finally:
            tmp.unlink(missing_ok=True)

    def test_parse_missing_file(self):
        from subsystems.communications.whatsapp_client import parse_export_file
        result = parse_export_file(Path("/nonexistent/export.txt"))
        assert result == []

    def test_parse_skips_system_messages(self):
        from subsystems.communications.whatsapp_client import parse_export_file
        content = (
            "1/15/24, 3:45 PM - Messages and calls are end-to-end encrypted.\n"
            "1/15/24, 3:46 PM - Alice: Hello!\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            tmp = Path(f.name)
        try:
            msgs = parse_export_file(tmp)
            # System message line won't match the regex (no "sender: text" format)
            assert all(m["sender"] != "Messages and calls are end-to-end encrypted." for m in msgs)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tests for whatsapp_tools
# ---------------------------------------------------------------------------

class TestWhatsAppTools:
    def test_tool_schema_names(self):
        from agent.whatsapp_tools import WHATSAPP_TOOLS
        names = {t["function"]["name"] for t in WHATSAPP_TOOLS}
        assert "get_recent_whatsapp_chats" in names
        assert "get_whatsapp_chat" in names
        assert "search_whatsapp" in names
        assert "get_whatsapp_groups" in names

    def test_execute_chat_requires_chat_id(self):
        from agent.whatsapp_tools import execute_get_whatsapp_chat
        result = asyncio.run(execute_get_whatsapp_chat({}))
        assert "error" in result

    def test_execute_search_requires_query(self):
        from agent.whatsapp_tools import execute_search_whatsapp
        result = asyncio.run(execute_search_whatsapp({}))
        assert "error" in result

    def test_graceful_degradation_file_not_found(self):
        from agent.whatsapp_tools import execute_get_recent_whatsapp_chats
        with patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.get_recent_chats",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("DB not found"),
        ):
            result = asyncio.run(execute_get_recent_whatsapp_chats({}))
        assert "error" in result

    def test_maybe_get_whatsapp_context_no_trigger(self):
        from agent.whatsapp_tools import maybe_get_whatsapp_context
        result = asyncio.run(maybe_get_whatsapp_context("What's on my calendar?"))
        assert result == ""

    def test_maybe_get_whatsapp_context_triggers(self):
        from agent.whatsapp_tools import maybe_get_whatsapp_context
        fake_chats = [
            {"chat_id": 1, "name": "Family", "unread_count": 3, "is_group": True, "last_message_at": None},
            {"chat_id": 2, "name": "Bob", "unread_count": 0, "is_group": False, "last_message_at": None},
        ]
        fake_msgs = [
            {"from_me": False, "sender": "+1234", "timestamp": "2026-04-15T10:00", "text": "Hey there"},
        ]
        with patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.get_recent_chats",
            new_callable=AsyncMock,
            return_value=fake_chats,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.get_chat_messages",
            new_callable=AsyncMock,
            return_value=fake_msgs,
        ):
            result = asyncio.run(maybe_get_whatsapp_context("any WhatsApp messages?"))
        # real chat names present, real message text present, unread count present
        assert "Family" in result
        assert "3" in result
        assert "Hey there" in result

    def test_execute_recent_whatsapp_attention_uses_real_snippets(self):
        from agent.whatsapp_tools import execute_get_recent_whatsapp_attention

        fake_chats = [
            {
                "chat_id": 1,
                "name": "Family",
                "unread_count": 2,
                "is_group": True,
                "last_message_at": "2026-04-15T10:03:00",
            },
            {
                "chat_id": 2,
                "name": "Bob",
                "unread_count": 0,
                "is_group": False,
                "last_message_at": "2026-04-15T09:58:00",
            },
        ]
        fake_msgs = [
            {
                "from_me": False,
                "sender": "Alice",
                "timestamp": "2026-04-15T10:02:00",
                "text": "Can you look at the dinner reservation?",
            },
            {
                "from_me": True,
                "sender": "me",
                "timestamp": "2026-04-15T09:57:00",
                "text": "On it",
            },
        ]

        with patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.get_recent_chats",
            new_callable=AsyncMock,
            return_value=fake_chats,
        ), patch(
            "subsystems.communications.whatsapp_client.WhatsAppClient.get_chat_messages",
            new_callable=AsyncMock,
            return_value=fake_msgs,
        ):
            result = asyncio.run(
                execute_get_recent_whatsapp_attention({"limit": 5, "message_limit": 3})
            )

        assert result["count"] == 1
        assert "Family" in result["summary"]
        assert "Can you look at the dinner reservation?" in result["summary"]
        assert "updated project plan" not in result["summary"]

    def test_dispatcher_unknown_tool(self):
        from agent.whatsapp_tools import execute_whatsapp_tool
        result = asyncio.run(execute_whatsapp_tool("nonexistent", {}))
        assert "error" in result

    def test_dispatcher_legacy_messages_alias(self):
        from agent.whatsapp_tools import execute_whatsapp_tool
        with patch(
            "agent.whatsapp_tools.execute_get_whatsapp_chat",
            new_callable=AsyncMock,
            return_value={"messages": [], "summary": "ok"},
        ) as mock_execute:
            result = asyncio.run(execute_whatsapp_tool("get_whatsapp_messages", {"chat_id": 1}))
        mock_execute.assert_awaited_once_with({"chat_id": 1})
        assert result["summary"] == "ok"
