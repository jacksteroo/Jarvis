"""Slack integration for Pepper.

Read-only access via a Slack Bot Token (OAuth).
Requires SLACK_BOT_TOKEN in .env.

Required OAuth scopes for the bot token:
  channels:read, channels:history, groups:read, groups:history,
  im:read, im:history, mpim:read, mpim:history, search:read

Setup:
  1. Create a Slack app at https://api.slack.com/apps
  2. Add the required scopes under "OAuth & Permissions"
  3. Install the app to your workspace
  4. Copy the "Bot User OAuth Token" (xoxb-...) to SLACK_BOT_TOKEN in .env

Privacy: message bodies are processed locally by default.
         Only summaries are sent to the frontier LLM.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()

# Patterns that indicate a deadline in conversational Slack messages
_DEADLINE_PATTERNS = [
    re.compile(r"\bdue\s+(by\s+|on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(r"\bby\s+eod\b", re.IGNORECASE),
    re.compile(r"\bby\s+end\s+of\s+(day|week|month|quarter)\b", re.IGNORECASE),
    re.compile(r"\bby\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE),
    re.compile(r"\bby\s+\d{1,2}[\/\-]\d{1,2}\b"),  # "by 3/15"
    re.compile(r"\bdue\s+\d{1,2}[\/\-]\d{1,2}\b"),  # "due 3/15"
    re.compile(r"\bneed\s+(this|it|them|these)\s+by\b", re.IGNORECASE),
    re.compile(r"\bdeadline\s+is\b", re.IGNORECASE),
    re.compile(r"\bship\s+by\b", re.IGNORECASE),
    re.compile(r"\bdeliver\s+by\b", re.IGNORECASE),
    re.compile(r"\bsubmit\s+by\b", re.IGNORECASE),
    re.compile(r"\bfinish\s+by\b", re.IGNORECASE),
    re.compile(r"\bcomplete\s+by\b", re.IGNORECASE),
    re.compile(r"\blaunch\s+by\b", re.IGNORECASE),
    re.compile(r"\basap\b", re.IGNORECASE),
    re.compile(r"\burgent\b", re.IGNORECASE),
    re.compile(r"\btime[\s-]sensitive\b", re.IGNORECASE),
]


def detect_deadlines(messages: list[dict]) -> list[dict]:
    """Extract messages containing deadline language.

    Args:
        messages: List of message dicts with at least 'text', 'sender', 'timestamp' keys.

    Returns:
        Filtered list of messages that contain deadline language, with a 'deadline_hints' key
        listing the matched patterns.
    """
    results = []
    for msg in messages:
        text = msg.get("text", "") or ""
        hints = []
        for pattern in _DEADLINE_PATTERNS:
            m = pattern.search(text)
            if m:
                hints.append(m.group(0))
        if hints:
            results.append({**msg, "deadline_hints": hints})
    return results


class SlackClient:
    """Read-only Slack API client using slack_sdk."""

    def __init__(self, token: str):
        from slack_sdk import WebClient
        self._client = WebClient(token=token)

    def list_channels(self, include_private: bool = False) -> list[dict]:
        """List accessible channels."""
        types = "public_channel,private_channel" if include_private else "public_channel"
        result = self._client.conversations_list(types=types, limit=200)
        channels = result.get("channels", [])
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "is_private": c.get("is_private", False),
                "member_count": c.get("num_members", 0),
                "topic": c.get("topic", {}).get("value", ""),
            }
            for c in channels
        ]

    def get_channel_messages(
        self, channel_id: str, limit: int = 50, days: int = 7
    ) -> list[dict]:
        """Fetch recent messages from a channel."""
        oldest = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).timestamp()

        result = self._client.conversations_history(
            channel=channel_id,
            limit=min(limit, 200),
            oldest=str(oldest),
        )
        messages = result.get("messages", [])
        return [self._format_message(m, channel_id) for m in messages if m.get("type") == "message"]

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        """Search Slack messages. Requires search:read scope."""
        result = self._client.search_messages(query=query, count=min(limit, 100))
        matches = result.get("messages", {}).get("matches", [])
        return [
            {
                "channel": m.get("channel", {}).get("name", ""),
                "channel_id": m.get("channel", {}).get("id", ""),
                "sender": m.get("username", m.get("user", "unknown")),
                "user_id": m.get("user", ""),
                "text": m.get("text", ""),
                "timestamp": self._ts_to_iso(m.get("ts")),
                "permalink": m.get("permalink", ""),
            }
            for m in matches
        ]

    def get_dms(self, limit: int = 20) -> list[dict]:
        """List recent DM conversations."""
        result = self._client.conversations_list(types="im", limit=limit)
        channels = result.get("channels", [])
        dms = []
        for c in channels:
            dms.append({
                "id": c["id"],
                "user_id": c.get("user", ""),
                "is_open": c.get("is_open", False),
                "last_read": self._ts_to_iso(c.get("last_read")),
            })
        return dms

    def _format_message(self, msg: dict, channel_id: str = "") -> dict:
        return {
            "channel_id": channel_id,
            "sender": msg.get("username", msg.get("user", "unknown")),
            "user_id": msg.get("user", ""),
            "text": msg.get("text", ""),
            "timestamp": self._ts_to_iso(msg.get("ts")),
            "thread_ts": msg.get("thread_ts"),
            "reply_count": msg.get("reply_count", 0),
        }

    @staticmethod
    def _ts_to_iso(ts: Optional[str]) -> Optional[str]:
        if not ts:
            return None
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return dt.isoformat()
        except (ValueError, OSError):
            return None
