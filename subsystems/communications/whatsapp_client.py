"""WhatsApp reader for Pepper.

Primary: reads from the local WhatsApp SQLite database.
  macOS path: ~/Library/Application Support/WhatsApp/ChatStorage.sqlite

Fallback: parses WhatsApp chat export .txt files (File > Export Chat in the app).

All data processed entirely locally — never transmitted externally.

Note: WhatsApp Desktop on Mac stores its database at the path above.
      The schema differs from the iOS backup (iOS uses a different format).
      Full Disk Access may be required.

WhatsApp Desktop DB schema (simplified):
  ZWAMESSAGE     (Z_PK, ZTEXT, ZMESSAGEDATE, ZISFROMME, ZCHATSESSION, ZFROMJID)
  ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZUNREADCOUNT, ZLASTMESSAGEDATE)
  ZWAGROUPMEMBER (Z_PK, ZMEMBERJID, ZSESSION)
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

import os as _os

# Primary DB location — Docker mount takes priority when available
WHATSAPP_DB = (
    Path("/data/whatsapp/ChatStorage.sqlite")
    if _os.environ.get("RUNNING_IN_DOCKER") and Path("/data/whatsapp/ChatStorage.sqlite").exists()
    else Path.home()
    / "Library"
    / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared"
    / "ChatStorage.sqlite"
)

# Apple/WhatsApp epoch: seconds since 2001-01-01 (same as iMessage)
_APPLE_EPOCH = datetime(2001, 1, 1)


def _wa_ts_to_dt(ts: Optional[float]) -> Optional[str]:
    """Convert WhatsApp timestamp (seconds since 2001-01-01) to ISO string.

    WhatsApp's DB occasionally contains sentinel/corrupt values (e.g. 284012568000)
    that are orders of magnitude too large. Guard against these by rejecting anything
    that resolves outside a sane window (2007 – 2 years from now).
    """
    if ts is None:
        return None
    try:
        dt = _APPLE_EPOCH + timedelta(seconds=float(ts))
        # Sanity check: WhatsApp was founded 2009; reject clearly bogus future dates
        if dt.year < 2007 or dt.year > 2035:
            return None
        return dt.isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _open_db(path: Path = WHATSAPP_DB) -> sqlite3.Connection:
    """Open WhatsApp DB in read-only mode."""
    if not path.exists():
        raise FileNotFoundError(
            f"WhatsApp DB not found at {path}. "
            "Ensure WhatsApp Desktop is installed and has been opened at least once. "
            "Alternatively, export chats from WhatsApp > Settings > Chat > Export Chat."
        )
    try:
        # immutable=1 bypasses WAL lock acquisition so we don't block on a live WhatsApp process
        uri = f"file:{path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        raise PermissionError(
            f"Cannot open WhatsApp DB: {e}. "
            "Grant Full Disk Access to Terminal (System Settings → Privacy & Security → Full Disk Access)."
        ) from e


# ---------------------------------------------------------------------------
# Sync implementations (run via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _get_recent_chats_sync(limit: int, db_path: Path = WHATSAPP_DB) -> list[dict]:
    conn = _open_db(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT
                s.Z_PK AS chat_id,
                s.ZCONTACTJID AS jid,
                COALESCE(s.ZPARTNERNAME, s.ZCONTACTJID) AS name,
                s.ZUNREADCOUNT AS unread_count,
                s.ZLASTMESSAGEDATE AS last_message_date,
                CASE WHEN s.ZCONTACTJID LIKE '%@g.us' THEN 1 ELSE 0 END AS is_group
            FROM ZWACHATSESSION s
            ORDER BY s.ZLASTMESSAGEDATE DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "chat_id": r["chat_id"],
                "jid": r["jid"],
                "name": r["name"],
                "unread_count": r["unread_count"] or 0,
                "last_message_at": _wa_ts_to_dt(r["last_message_date"]),
                "is_group": bool(r["is_group"]),
            })
        logger.debug("whatsapp_chats_fetched", count=len(results))
        return results
    finally:
        conn.close()


def _get_chat_messages_sync(chat_id: int, limit: int, db_path: Path = WHATSAPP_DB) -> list[dict]:
    conn = _open_db(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT
                m.Z_PK AS message_id,
                m.ZTEXT AS text,
                m.ZMESSAGEDATE AS date,
                m.ZISFROMME AS is_from_me,
                m.ZFROMJID AS from_jid
            FROM ZWAMESSAGE m
            WHERE m.ZCHATSESSION = ?
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "message_id": r["message_id"],
                "from_me": bool(r["is_from_me"]),
                "sender": "me" if r["is_from_me"] else (r["from_jid"] or "unknown"),
                "timestamp": _wa_ts_to_dt(r["date"]),
                "text": r["text"] or "",
            })
        logger.debug("whatsapp_messages_fetched", chat_id=chat_id, count=len(results))
        return results
    finally:
        conn.close()


def _search_messages_sync(query: str, limit: int, db_path: Path = WHATSAPP_DB) -> list[dict]:
    conn = _open_db(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT
                m.Z_PK AS message_id,
                m.ZTEXT AS text,
                m.ZMESSAGEDATE AS date,
                m.ZISFROMME AS is_from_me,
                m.ZFROMJID AS from_jid,
                COALESCE(s.ZPARTNERNAME, s.ZCONTACTJID) AS chat_name
            FROM ZWAMESSAGE m
            JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            WHERE m.ZTEXT LIKE ?
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT ?
            """,
            (f"%{query}%", limit),
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "message_id": r["message_id"],
                "chat": r["chat_name"],
                "from_me": bool(r["is_from_me"]),
                "sender": "me" if r["is_from_me"] else (r["from_jid"] or "unknown"),
                "timestamp": _wa_ts_to_dt(r["date"]),
                "text": r["text"] or "",
            })
        logger.debug("whatsapp_search_done", query_len=len(query), count=len(results))
        return results
    finally:
        conn.close()


def _get_group_chats_sync(db_path: Path = WHATSAPP_DB) -> list[dict]:
    conn = _open_db(db_path)
    try:
        # Count members per group
        cursor = conn.execute(
            """
            SELECT
                s.Z_PK AS chat_id,
                COALESCE(s.ZPARTNERNAME, s.ZCONTACTJID) AS name,
                s.ZCONTACTJID AS jid,
                s.ZLASTMESSAGEDATE AS last_message_date,
                COUNT(gm.Z_PK) AS member_count
            FROM ZWACHATSESSION s
            LEFT JOIN ZWAGROUPMEMBER gm ON gm.ZCHATSESSION = s.Z_PK
            WHERE s.ZCONTACTJID LIKE '%@g.us'
            GROUP BY s.Z_PK
            ORDER BY s.ZLASTMESSAGEDATE DESC
            """,
        )
        rows = cursor.fetchall()
        results = []
        for r in rows:
            results.append({
                "chat_id": r["chat_id"],
                "name": r["name"],
                "jid": r["jid"],
                "member_count": r["member_count"],
                "last_message_at": _wa_ts_to_dt(r["last_message_date"]),
            })
        logger.debug("whatsapp_groups_fetched", count=len(results))
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fallback: parse WhatsApp .txt export
# ---------------------------------------------------------------------------

_EXPORT_LINE_RE = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\s*[-–]\s*([^:]+):\s*(.+)$",
    re.IGNORECASE,
)


def parse_export_file(path: Path) -> list[dict]:
    """Parse a WhatsApp .txt chat export file into structured messages."""
    messages = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        logger.warning("whatsapp_export_read_failed", path=str(path), error=str(e))
        return []

    for line in lines:
        m = _EXPORT_LINE_RE.match(line.strip())
        if m:
            date_str, time_str, sender, text = m.groups()
            messages.append({
                "timestamp": f"{date_str} {time_str}",
                "sender": sender.strip(),
                "text": text.strip(),
                "from_me": False,  # can't determine from export
            })
    return messages


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------

_DB_TIMEOUT = 6.0  # seconds before giving up on a locked DB


class WhatsAppClient:
    """Async wrapper around the local WhatsApp SQLite database.

    WhatsApp Desktop holds an OS-level exclusive lock on ChatStorage.sqlite while
    running. All methods use asyncio.wait_for so they fail fast with a clear error
    instead of hanging indefinitely.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or WHATSAPP_DB

    async def get_recent_chats(self, limit: int = 20) -> list[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_get_recent_chats_sync, limit, self._db_path),
                timeout=_DB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise PermissionError(
                "WhatsApp DB is locked by the running WhatsApp process. "
                "Quit WhatsApp and retry, or grant Full Disk Access to Terminal."
            )

    async def get_chat_messages(self, chat_id: int, limit: int = 50) -> list[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_get_chat_messages_sync, chat_id, limit, self._db_path),
                timeout=_DB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise PermissionError("WhatsApp DB is locked by the running WhatsApp process.")

    async def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_search_messages_sync, query, limit, self._db_path),
                timeout=_DB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise PermissionError("WhatsApp DB is locked by the running WhatsApp process.")

    async def get_group_chats(self) -> list[dict]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_get_group_chats_sync, self._db_path),
                timeout=_DB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise PermissionError("WhatsApp DB is locked by the running WhatsApp process.")

    @staticmethod
    def is_available(db_path: Optional[Path] = None) -> bool:
        return (db_path or WHATSAPP_DB).exists()

    @staticmethod
    def parse_export(path: Path) -> list[dict]:
        return parse_export_file(path)
