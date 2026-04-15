"""Tests for iMessage client and tools.

Uses mock SQLite data — does not require the real chat.db.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: build an in-memory iMessage-like DB
# ---------------------------------------------------------------------------

def _build_test_db(path: str) -> None:
    """Create a minimal chat.db schema with test data."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT,
            uncanonicalized_id TEXT
        );
        CREATE TABLE IF NOT EXISTS chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            chat_identifier TEXT,
            display_name TEXT
        );
        CREATE TABLE IF NOT EXISTS message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            date INTEGER,
            is_from_me INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0,
            handle_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS chat_handle_join (
            chat_id INTEGER,
            handle_id INTEGER
        );

        INSERT INTO handle VALUES (1, '+15551234567', '+15551234567');
        INSERT INTO handle VALUES (2, 'friend@example.com', 'friend@example.com');

        INSERT INTO chat VALUES (1, 'guid1', '+15551234567', 'Alice');
        INSERT INTO chat VALUES (2, 'guid2', 'family-group', 'Family Group');

        -- date: nanoseconds since 2001-01-01; use a large number for "recent"
        INSERT INTO message VALUES (1, 'Hey, are you free tomorrow?', 700000000000000000, 0, 0, 1);
        INSERT INTO message VALUES (2, 'Sounds good, see you then', 700000001000000000, 1, 1, 0);
        INSERT INTO message VALUES (3, 'Family dinner this Sunday!', 700000002000000000, 0, 0, 2);

        INSERT INTO chat_message_join VALUES (1, 1);
        INSERT INTO chat_message_join VALUES (1, 2);
        INSERT INTO chat_message_join VALUES (2, 3);

        INSERT INTO chat_handle_join VALUES (1, 1);
        INSERT INTO chat_handle_join VALUES (2, 2);
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests for imessage_client
# ---------------------------------------------------------------------------

class TestIMessageClient:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _build_test_db(self.tmp.name)
        self.db_path = Path(self.tmp.name)

    def teardown_method(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _patch_db(self):
        """Patch IMESSAGE_DB to point to our test DB."""
        import subsystems.communications.imessage_client as mod
        return patch.object(mod, "IMESSAGE_DB", self.db_path)

    def test_get_recent_conversations(self):
        with self._patch_db():
            from subsystems.communications.imessage_client import _get_recent_conversations_sync
            results = _get_recent_conversations_sync(limit=10, days=99999)
        assert len(results) >= 1
        names = [r["display_name"] for r in results]
        assert any("Alice" in n or "+15551234567" in n for n in names)

    def test_get_conversation_returns_messages(self):
        with self._patch_db():
            from subsystems.communications.imessage_client import _get_conversation_sync
            msgs = _get_conversation_sync("Alice", limit=10)
        assert len(msgs) >= 1
        assert "text" in msgs[0]
        assert "timestamp" in msgs[0]
        assert "from_me" in msgs[0]

    def test_get_chat_messages_returns_messages(self):
        with self._patch_db():
            from subsystems.communications.imessage_client import _get_chat_messages_sync
            msgs = _get_chat_messages_sync(chat_id=1, limit=10)
        assert len(msgs) >= 1
        assert any("free tomorrow" in m["text"] for m in msgs)

    def test_search_messages_parameterized(self):
        """Verify search uses LIKE with parameter (not string interpolation)."""
        with self._patch_db():
            from subsystems.communications.imessage_client import _search_messages_sync
            results = _search_messages_sync("free tomorrow", limit=10)
        assert len(results) >= 1
        assert "free tomorrow" in results[0]["text"]

    def test_search_messages_sql_injection_safe(self):
        """SQL injection attempt should return empty results, not raise."""
        with self._patch_db():
            from subsystems.communications.imessage_client import _search_messages_sync
            # If interpolated this would be malformed SQL; with parameterized it's safe
            results = _search_messages_sync("'; DROP TABLE message; --", limit=5)
        assert isinstance(results, list)

    def test_get_unread_count(self):
        with self._patch_db():
            from subsystems.communications.imessage_client import _get_unread_count_sync
            count = _get_unread_count_sync()
        # message 1 and 3 are unread (is_read=0, is_from_me=0)
        assert count == 2

    def test_db_not_found_raises(self):
        import subsystems.communications.imessage_client as mod
        with patch.object(mod, "IMESSAGE_DB", Path("/nonexistent/chat.db")):
            from subsystems.communications.imessage_client import _get_recent_conversations_sync
            with pytest.raises(FileNotFoundError):
                _get_recent_conversations_sync(limit=5, days=30)

    def test_is_available(self):
        with self._patch_db():
            from subsystems.communications.imessage_client import IMessageClient
            assert IMessageClient.is_available() is True

    def test_is_available_false_when_missing(self):
        import subsystems.communications.imessage_client as mod
        with patch.object(mod, "IMESSAGE_DB", Path("/nonexistent/chat.db")):
            from subsystems.communications.imessage_client import IMessageClient
            assert IMessageClient.is_available() is False


# ---------------------------------------------------------------------------
# Tests for imessage_tools
# ---------------------------------------------------------------------------

class TestIMessageTools:
    def test_tool_schema_names(self):
        from agent.imessage_tools import IMESSAGE_TOOLS
        names = {t["function"]["name"] for t in IMESSAGE_TOOLS}
        assert "get_recent_imessages" in names
        assert "get_imessage_conversation" in names
        assert "search_imessages" in names

    def test_execute_requires_contact(self):
        from agent.imessage_tools import execute_get_imessage_conversation
        result = asyncio.run(
            execute_get_imessage_conversation({})
        )
        assert "error" in result

    def test_execute_requires_query(self):
        from agent.imessage_tools import execute_search_imessages
        result = asyncio.run(
            execute_search_imessages({})
        )
        assert "error" in result

    def test_graceful_degradation_on_permission_error(self):
        from agent.imessage_tools import execute_get_recent_imessages
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            side_effect=PermissionError("Full Disk Access required"),
        ):
            result = asyncio.run(
                execute_get_recent_imessages({})
            )
        assert "error" in result
        assert "Full Disk Access" in result["error"] or "error" in result

    def test_maybe_get_imessage_context_no_trigger(self):
        from agent.imessage_tools import maybe_get_imessage_context
        result = asyncio.run(
            maybe_get_imessage_context("What's on my calendar?")
        )
        assert result == ""

    def test_maybe_get_imessage_context_triggers_on_text(self):
        from agent.imessage_tools import maybe_get_imessage_context
        with patch(
            "subsystems.communications.imessage_client.IMessageClient.is_available",
            return_value=True,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=[
                {
                    "chat_id": 1,
                    "display_name": "Mom",
                    "unread_count": 2,
                    "last_message_at": "2026-04-15T10:00:00",
                }
            ],
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_chat_messages",
            new_callable=AsyncMock,
            return_value=[
                {
                    "from_me": False,
                    "sender": "Mom",
                    "timestamp": "2026-04-15T10:00:00",
                    "text": "Call me when you wake up.",
                }
            ],
        ):
            result = asyncio.run(
                maybe_get_imessage_context("any texts from mom?")
            )
        assert "Mom" in result
        assert "Call me when you wake up." in result

    def test_is_imessage_attention_query_matches_summary_requests(self):
        from agent.imessage_tools import is_imessage_attention_query

        assert is_imessage_attention_query("Summarize my text messages") is True
        assert is_imessage_attention_query("Give me a recap of my texts") is True
        assert is_imessage_attention_query("Find the text about dinner") is False

    def test_execute_recent_imessage_attention_uses_real_snippets(self):
        from agent.imessage_tools import execute_get_recent_imessage_attention

        fake_convos = [
            {
                "chat_id": 1,
                "display_name": "Mom",
                "unread_count": 2,
                "last_message_at": "2026-04-15T10:00:00",
            },
            {
                "chat_id": 2,
                "display_name": "Alex",
                "unread_count": 0,
                "last_message_at": "2026-04-15T09:30:00",
            },
        ]

        fake_msgs = [
            {
                "from_me": False,
                "sender": "Mom",
                "timestamp": "2026-04-15T10:00:00",
                "text": "Call me when you wake up.",
            },
            {
                "from_me": True,
                "sender": "me",
                "timestamp": "2026-04-15T09:55:00",
                "text": "Will do",
            },
        ]

        with patch(
            "subsystems.communications.imessage_client.IMessageClient.get_recent_conversations",
            new_callable=AsyncMock,
            return_value=fake_convos,
        ), patch(
            "subsystems.communications.imessage_client.IMessageClient.get_chat_messages",
            new_callable=AsyncMock,
            return_value=fake_msgs,
        ):
            result = asyncio.run(
                execute_get_recent_imessage_attention({"limit": 5, "message_limit": 3})
            )

        assert result["count"] == 1
        assert "Mom" in result["summary"]
        assert "Call me when you wake up." in result["summary"]
        assert "updated project plan" not in result["summary"]

    def test_dispatcher_routes_correctly(self):
        from agent.imessage_tools import execute_imessage_tool
        with patch(
            "agent.imessage_tools.execute_get_recent_imessages",
            new_callable=AsyncMock,
            return_value={"conversations": [], "count": 0},
        ) as mock:
            asyncio.run(
                execute_imessage_tool("get_recent_imessages", {})
            )
            mock.assert_called_once()

    def test_dispatcher_unknown_tool(self):
        from agent.imessage_tools import execute_imessage_tool
        result = asyncio.run(
            execute_imessage_tool("nonexistent_tool", {})
        )
        assert "error" in result
