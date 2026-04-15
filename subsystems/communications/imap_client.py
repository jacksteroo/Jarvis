"""IMAP email client for Yahoo Mail (and other IMAP providers).

Credentials are stored in ~/.config/pepper/yahoo_credentials.json.
Yahoo requires an app-specific password — generate one at:
  https://login.yahoo.com/account/security/app-passwords

Only message headers are fetched — raw bodies never leave the machine.
"""

from __future__ import annotations

import email
import imaplib
import json
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "pepper"
YAHOO_CREDENTIALS_PATH = CONFIG_DIR / "yahoo_credentials.json"
LEGACY_CREDENTIALS_PATH = CONFIG_DIR / "email_credentials.json"

IMAP_PROVIDERS: dict[str, tuple[str, int]] = {
    "yahoo": ("imap.mail.yahoo.com", 993),
    "gmail": ("imap.gmail.com", 993),
    "outlook": ("outlook.office365.com", 993),
    "icloud": ("imap.mail.me.com", 993),
}


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def load_credentials(account_name: str) -> dict:
    """Load IMAP credentials for a named account from the config file."""
    creds_path = YAHOO_CREDENTIALS_PATH if YAHOO_CREDENTIALS_PATH.exists() else LEGACY_CREDENTIALS_PATH

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Yahoo credentials not found at {YAHOO_CREDENTIALS_PATH}.\n"
            "Run: python subsystems/communications/setup_auth.py"
        )
    with open(creds_path) as f:
        all_creds = json.load(f)

    if account_name not in all_creds:
        raise KeyError(
            f"Account '{account_name}' not found in {creds_path}.\n"
            "Run: python subsystems/communications/setup_auth.py"
        )
    return all_creds[account_name]


class ImapClient:
    """Read-only IMAP client. Fetches headers only — raw bodies are never read."""

    def __init__(self, account_name: str):
        self.account_name = account_name

    def _connect(self) -> imaplib.IMAP4_SSL:
        creds = load_credentials(self.account_name)
        provider = creds.get("provider", "yahoo")
        host, port = IMAP_PROVIDERS.get(provider, (creds.get("host", ""), int(creds.get("port", 993))))
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(creds["email"], creds["password"])
        return conn

    def _fetch_headers(self, conn: imaplib.IMAP4_SSL, msg_ids: list[bytes]) -> list[dict]:
        """Fetch header-only data for a list of message IDs."""
        results = []
        for msg_id in msg_ids:
            try:
                _, data = conn.fetch(msg_id, "(RFC822.HEADER FLAGS)")
                if not data or not data[0]:
                    continue
                raw_headers = data[0][1]
                msg = email.message_from_bytes(raw_headers)

                # Parse flags
                flags_str = data[0][0].decode() if isinstance(data[0][0], bytes) else str(data[0][0])
                unread = "\\Seen" not in flags_str

                results.append({
                    "id": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                    "from": _decode_header_value(msg.get("From", "")),
                    "to": _decode_header_value(msg.get("To", "")),
                    "subject": _decode_header_value(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "unread": unread,
                    "account": self.account_name,
                })
            except Exception:
                continue
        return results

    def get_recent_messages(self, count: int = 20, hours: int = 24) -> list[dict]:
        """Fetch recent message headers from the inbox."""
        since_date = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")
        conn = self._connect()
        try:
            conn.select("INBOX", readonly=True)
            _, message_ids = conn.search(None, f'SINCE "{since_date}"')
            ids = message_ids[0].split()
            # Newest first, limited to count
            ids = list(reversed(ids))[:count]
            return self._fetch_headers(conn, ids)
        finally:
            conn.logout()

    def search_messages(self, query: str, count: int = 10) -> list[dict]:
        """Search inbox by subject or sender keyword."""
        conn = self._connect()
        try:
            conn.select("INBOX", readonly=True)
            _, subj_ids = conn.search(None, f'SUBJECT "{query}"')
            _, from_ids = conn.search(None, f'FROM "{query}"')

            all_ids_set: set[bytes] = set(subj_ids[0].split()) | set(from_ids[0].split())
            ids = list(all_ids_set)[-count:]
            return self._fetch_headers(conn, ids)
        finally:
            conn.logout()

    def get_unread_count(self) -> int:
        """Return count of unseen messages in the inbox."""
        conn = self._connect()
        try:
            conn.select("INBOX", readonly=True)
            _, ids = conn.search(None, "UNSEEN")
            return len(ids[0].split())
        finally:
            conn.logout()
