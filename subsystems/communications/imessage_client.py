"""iMessage reader for Pepper.

Reads from ~/Library/Messages/chat.db (local SQLite — no API required).
All data processed entirely locally — never transmitted externally.

Requirements:
  - macOS Full Disk Access must be granted to Terminal/Python
  - Read-only access only

chat.db schema (simplified):
  message   (ROWID, text, date [ns since 2001-01-01], is_from_me, handle_id, cache_roomnames)
  handle    (ROWID, id [phone/email], uncanonicalized_id)
  chat      (ROWID, guid, chat_identifier, display_name)
  chat_message_join  (chat_id, message_id)
  chat_handle_join   (chat_id, handle_id)
"""

from __future__ import annotations

import asyncio
import os as _os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# macOS iMessage DB path
# When running in Docker, the host ~/Library/Messages is mounted at /data/messages
IMESSAGE_DB = (
    Path("/data/messages/chat.db")
    if _os.environ.get("RUNNING_IN_DOCKER") and Path("/data/messages/chat.db").exists()
    else Path.home() / "Library" / "Messages" / "chat.db"
)

# Apple epoch offset: iMessage dates are ns since 2001-01-01
_APPLE_EPOCH = datetime(2001, 1, 1)


def _apple_ts_to_dt(ns: Optional[int]) -> Optional[str]:
    """Convert Apple nanosecond timestamp to ISO datetime string."""
    if not ns:
        return None
    try:
        dt = _APPLE_EPOCH + timedelta(seconds=ns / 1e9)
        return dt.isoformat()
    except (OverflowError, OSError):
        return None


def _open_db() -> sqlite3.Connection:
    """Open chat.db in read-only mode. Raises PermissionError with helpful message."""
    if not IMESSAGE_DB.exists():
        raise FileNotFoundError(
            f"iMessage DB not found at {IMESSAGE_DB}. "
            "Ensure iMessage is set up on this Mac."
        )
    try:
        uri = f"file:{IMESSAGE_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        raise PermissionError(
            f"Cannot open iMessage DB: {e}. "
            "Grant Full Disk Access to Terminal (System Settings → Privacy & Security → Full Disk Access)."
        ) from e


def _get_recent_conversations_sync(limit: int, days: int) -> list[dict]:
    """Synchronous implementation for asyncio.to_thread."""
    conn = _open_db()
    try:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff_ns = int(
            (now_utc - _APPLE_EPOCH - timedelta(days=days)).total_seconds() * 1e9
        )
        cursor = conn.execute(
            """
            SELECT
                c.ROWID AS chat_id,
                c.chat_identifier,
                COALESCE(c.display_name, '') AS display_name,
                COUNT(m.ROWID) AS message_count,
                MAX(m.date) AS last_message_date,
                SUM(CASE WHEN m.is_read = 0 AND m.is_from_me = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM chat c
            JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            JOIN message m ON cmj.message_id = m.ROWID
            WHERE m.date > ?
            GROUP BY c.ROWID
            ORDER BY last_message_date DESC
            LIMIT ?
            """,
            (cutoff_ns, limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "chat_id": r["chat_id"],
                "identifier": r["chat_identifier"],
                "display_name": r["display_name"] or r["chat_identifier"],
                "message_count": r["message_count"],
                "unread_count": r["unread_count"],
                "last_message_at": _apple_ts_to_dt(r["last_message_date"]),
            })
        logger.debug("imessage_conversations_fetched", count=len(results), days=days)
        return results
    finally:
        conn.close()


def _get_conversation_sync(contact_identifier: str, limit: int) -> list[dict]:
    """Fetch messages with a specific contact. Matches by chat_identifier or display_name."""
    conn = _open_db()
    try:
        cursor = conn.execute(
            """
            SELECT
                m.ROWID,
                m.is_from_me,
                m.date,
                m.text,
                h.id AS handle_id
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.chat_identifier LIKE ?
               OR c.display_name LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (f"%{contact_identifier}%", f"%{contact_identifier}%", limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "message_id": r["ROWID"],
                "from_me": bool(r["is_from_me"]),
                "sender": "me" if r["is_from_me"] else (r["handle_id"] or contact_identifier),
                "timestamp": _apple_ts_to_dt(r["date"]),
                # text excluded from logs for privacy — returned to local agent only
                "has_text": bool(r["text"]),
                "text": r["text"] or "",
            })
        logger.debug("imessage_conversation_fetched", contact=contact_identifier[:20], count=len(results))
        return results
    finally:
        conn.close()


def _get_chat_messages_sync(chat_id: int, limit: int) -> list[dict]:
    """Fetch messages for a specific chat row id."""
    conn = _open_db()
    try:
        cursor = conn.execute(
            """
            SELECT
                m.ROWID,
                m.is_from_me,
                m.date,
                m.text,
                h.id AS handle_id
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE cmj.chat_id = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "message_id": r["ROWID"],
                "from_me": bool(r["is_from_me"]),
                "sender": "me" if r["is_from_me"] else (r["handle_id"] or "unknown"),
                "timestamp": _apple_ts_to_dt(r["date"]),
                "has_text": bool(r["text"]),
                "text": r["text"] or "",
            })
        logger.debug("imessage_chat_messages_fetched", chat_id=chat_id, count=len(results))
        return results
    finally:
        conn.close()


def _search_messages_sync(query: str, limit: int) -> list[dict]:
    """Full-text search across iMessage history using LIKE (parameterized)."""
    conn = _open_db()
    try:
        cursor = conn.execute(
            """
            SELECT
                m.ROWID,
                m.is_from_me,
                m.date,
                m.text,
                c.chat_identifier,
                COALESCE(c.display_name, c.chat_identifier) AS chat_name,
                h.id AS handle_id
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (f"%{query}%", limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "message_id": r["ROWID"],
                "chat": r["chat_name"],
                "from_me": bool(r["is_from_me"]),
                "sender": "me" if r["is_from_me"] else (r["handle_id"] or "unknown"),
                "timestamp": _apple_ts_to_dt(r["date"]),
                "text": r["text"] or "",
            })
        logger.debug("imessage_search_complete", query_len=len(query), count=len(results))
        return results
    finally:
        conn.close()


def _get_unread_count_sync() -> int:
    """Count unread incoming messages."""
    conn = _open_db()
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM message WHERE is_read = 0 AND is_from_me = 0"
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


# AppleScript template for sending an iMessage. The {to} field accepts a phone
# number, email, or chat GUID; the message body is double-quoted at template-
# substitution time after escaping. Targeting a chat GUID lets us reply to
# group chats as well as 1:1 conversations.
_OSA_SEND_BUDDY = '''\
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{to}" of targetService
    send "{body}" to targetBuddy
end tell
'''

_OSA_SEND_CHAT = '''\
tell application "Messages"
    set targetChat to a reference to text chat id "{guid}"
    send "{body}" to targetChat
end tell
'''


def _osa_escape(s: str) -> str:
    """Escape a string for safe interpolation inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_imessage_sync(to: str, body: str, chat_guid: str | None = None) -> dict:
    """Send an iMessage via osascript. Raises on subprocess failure.

    If chat_guid is provided, target the existing chat (group or 1:1 by GUID).
    Otherwise treat `to` as a phone number / email / handle.
    """
    if not body:
        raise ValueError("iMessage body cannot be empty")
    if chat_guid:
        script = _OSA_SEND_CHAT.format(guid=_osa_escape(chat_guid), body=_osa_escape(body))
    else:
        if not to:
            raise ValueError("iMessage recipient is required when chat_guid is not provided")
        script = _OSA_SEND_BUDDY.format(to=_osa_escape(to), body=_osa_escape(body))

    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"osascript send failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    logger.info("imessage_sent", to=(chat_guid or to)[:40], length=len(body))
    return {"ok": True, "to": chat_guid or to, "length": len(body)}


class IMessageClient:
    """Async wrapper around the local iMessage SQLite database."""

    async def get_recent_conversations(self, limit: int = 20, days: int = 30) -> list[dict]:
        return await asyncio.to_thread(_get_recent_conversations_sync, limit, days)

    async def get_conversation(self, contact_identifier: str, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(_get_conversation_sync, contact_identifier, limit)

    async def get_chat_messages(self, chat_id: int, limit: int = 50) -> list[dict]:
        return await asyncio.to_thread(_get_chat_messages_sync, chat_id, limit)

    async def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        return await asyncio.to_thread(_search_messages_sync, query, limit)

    async def get_unread_count(self) -> int:
        return await asyncio.to_thread(_get_unread_count_sync)

    async def send(self, to: str, body: str, chat_guid: str | None = None) -> dict:
        """Send an iMessage. Either supply `to` (phone/email) or `chat_guid` (for groups)."""
        return await asyncio.to_thread(_send_imessage_sync, to, body, chat_guid)

    @staticmethod
    def is_available() -> bool:
        return IMESSAGE_DB.exists()
