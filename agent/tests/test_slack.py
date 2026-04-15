"""Tests for Slack client and tools.

Uses mocked slack_sdk responses — does not require a real Slack token.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Deadline detection tests (pure logic, no mock needed)
# ---------------------------------------------------------------------------

class TestDeadlineDetection:
    def _msgs(self, texts: list[str]) -> list[dict]:
        return [{"text": t, "sender": "alice", "timestamp": "2026-01-01T10:00:00"} for t in texts]

    def test_detects_due_friday(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["This is due Friday"])
        result = detect_deadlines(msgs)
        assert len(result) == 1
        assert any("Friday" in h for h in result[0]["deadline_hints"])

    def test_detects_by_eod(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["Need this by EOD"])
        result = detect_deadlines(msgs)
        assert len(result) == 1

    def test_detects_end_of_week(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["Please finish by end of week"])
        result = detect_deadlines(msgs)
        assert len(result) == 1

    def test_detects_ship_by(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["We need to ship by 3/15"])
        result = detect_deadlines(msgs)
        assert len(result) == 1

    def test_detects_asap(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["Need this ASAP"])
        result = detect_deadlines(msgs)
        assert len(result) == 1

    def test_detects_urgent(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["This is urgent"])
        result = detect_deadlines(msgs)
        assert len(result) == 1

    def test_no_deadline_in_normal_message(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs([
            "Good morning everyone!",
            "Did anyone watch the game last night?",
            "Lunch at noon?",
        ])
        result = detect_deadlines(msgs)
        assert result == []

    def test_multiple_patterns_in_one_message(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["This is urgent and due Friday — need this by EOD"])
        result = detect_deadlines(msgs)
        assert len(result) == 1
        assert len(result[0]["deadline_hints"]) >= 2

    def test_mixed_messages(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs([
            "Hey what's up",
            "The report is due Monday",
            "Coffee anyone?",
            "Submit by 12/31",
        ])
        result = detect_deadlines(msgs)
        assert len(result) == 2

    def test_deadline_hints_preserved_in_original_keys(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["Due Friday please"])
        result = detect_deadlines(msgs)
        assert result[0]["sender"] == "alice"
        assert result[0]["timestamp"] == "2026-01-01T10:00:00"

    def test_case_insensitive(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = self._msgs(["DUE FRIDAY", "by EOD", "By End Of Day"])
        result = detect_deadlines(msgs)
        assert len(result) == 3

    def test_empty_text(self):
        from subsystems.communications.slack_client import detect_deadlines
        msgs = [{"text": None, "sender": "alice", "timestamp": ""}]
        result = detect_deadlines(msgs)
        assert result == []


# ---------------------------------------------------------------------------
# SlackClient tests (mocked WebClient)
# ---------------------------------------------------------------------------

def _make_mock_webclient():
    mock = MagicMock()
    mock.conversations_list.return_value = {
        "channels": [
            {"id": "C001", "name": "general", "is_private": False, "num_members": 10, "topic": {"value": "General chat"}},
            {"id": "C002", "name": "engineering", "is_private": False, "num_members": 5, "topic": {"value": ""}},
        ]
    }
    mock.conversations_history.return_value = {
        "messages": [
            {"type": "message", "user": "U001", "username": "alice", "text": "Hello team", "ts": "1700000000.000000"},
            {"type": "message", "user": "U002", "username": "bob", "text": "Due Friday: ship the feature", "ts": "1700001000.000000"},
        ]
    }
    mock.search_messages.return_value = {
        "messages": {
            "matches": [
                {
                    "channel": {"id": "C001", "name": "general"},
                    "username": "alice",
                    "user": "U001",
                    "text": "deployment deadline",
                    "ts": "1700000000.000000",
                    "permalink": "https://slack.com/archives/C001/p123",
                }
            ]
        }
    }
    return mock


class TestSlackClient:
    def _make_client(self):
        from subsystems.communications.slack_client import SlackClient
        client = SlackClient.__new__(SlackClient)
        client._client = _make_mock_webclient()
        return client

    def test_list_channels(self):
        client = self._make_client()
        channels = client.list_channels()
        assert len(channels) == 2
        assert channels[0]["name"] == "general"
        assert channels[0]["id"] == "C001"

    def test_get_channel_messages(self):
        client = self._make_client()
        msgs = client.get_channel_messages("C001", limit=10, days=7)
        assert len(msgs) == 2
        assert msgs[0]["text"] == "Hello team"

    def test_search_messages(self):
        client = self._make_client()
        results = client.search_messages("deployment")
        assert len(results) == 1
        assert results[0]["channel"] == "general"

    def test_ts_to_iso(self):
        from subsystems.communications.slack_client import SlackClient
        iso = SlackClient._ts_to_iso("1700000000.000000")
        assert "2023" in iso  # Nov 2023

    def test_ts_to_iso_none(self):
        from subsystems.communications.slack_client import SlackClient
        assert SlackClient._ts_to_iso(None) is None


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------

class TestSlackTools:
    def test_tool_schema_names(self):
        from agent.slack_tools import SLACK_TOOLS
        names = {t["function"]["name"] for t in SLACK_TOOLS}
        assert "search_slack" in names
        assert "get_slack_channel_messages" in names
        assert "get_slack_deadlines" in names
        assert "list_slack_channels" in names

    def test_no_token_returns_error(self):
        from agent.slack_tools import execute_search_slack
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            result = asyncio.run(execute_search_slack({"query": "test"}))
        assert "error" in result
        assert "SLACK_BOT_TOKEN" in result["error"]

    def test_search_requires_query(self):
        from agent.slack_tools import execute_search_slack
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = asyncio.run(execute_search_slack({}))
        assert "error" in result

    def test_channel_messages_requires_channel_id(self):
        from agent.slack_tools import execute_get_slack_channel_messages
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = asyncio.run(execute_get_slack_channel_messages({}))
        assert "error" in result

    def test_deadlines_requires_channel_id(self):
        from agent.slack_tools import execute_get_slack_deadlines
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = asyncio.run(execute_get_slack_deadlines({}))
        assert "error" in result

    def test_search_slack_success(self):
        from agent.slack_tools import execute_search_slack
        mock_client = MagicMock()
        mock_client.search_messages.return_value = [
            {"channel": "general", "sender": "alice", "text": "deadline today", "timestamp": "2026-01-01T10:00:00"},
        ]
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}), \
             patch("agent.slack_tools._get_client", return_value=mock_client):
            result = asyncio.run(execute_search_slack({"query": "deadline"}))
        assert result["count"] == 1
        assert "deadline" in result["messages"][0]

    def test_get_slack_deadlines_filters_correctly(self):
        from agent.slack_tools import execute_get_slack_deadlines
        mock_client = MagicMock()
        mock_client.get_channel_messages.return_value = [
            {"sender": "alice", "text": "Good morning!", "timestamp": "2026-01-01T09:00:00", "user_id": "U001"},
            {"sender": "bob", "text": "This is due Friday please", "timestamp": "2026-01-01T10:00:00", "user_id": "U002"},
            {"sender": "carol", "text": "Need this by EOD", "timestamp": "2026-01-01T11:00:00", "user_id": "U003"},
        ]
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}), \
             patch("agent.slack_tools._get_client", return_value=mock_client):
            result = asyncio.run(execute_get_slack_deadlines({"channel_id": "C001"}))
        assert result["count"] == 2

    def test_maybe_get_slack_context_no_trigger(self):
        from agent.slack_tools import maybe_get_slack_context
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = asyncio.run(maybe_get_slack_context("What's the weather?"))
        assert result == ""

    def test_maybe_get_slack_context_triggers_on_deadline(self):
        from agent.slack_tools import maybe_get_slack_context
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = asyncio.run(maybe_get_slack_context("What deadlines do I have from Slack?"))
        assert result != ""

    def test_maybe_get_slack_context_no_token(self):
        from agent.slack_tools import maybe_get_slack_context
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            result = asyncio.run(maybe_get_slack_context("check slack deadlines"))
        assert result == ""

    def test_dispatcher_unknown_tool(self):
        from agent.slack_tools import execute_slack_tool
        result = asyncio.run(execute_slack_tool("unknown_tool", {}))
        assert "error" in result
