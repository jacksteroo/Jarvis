"""Gmail API client for Pepper.

Uses the shared Google OAuth token for the account, which now authorizes
Gmail read + send and Calendar read:
  ~/.config/pepper/google_token.json       ← personal/default account
  ~/.config/pepper/google_token_work.json  ← named account

Reads: only message headers and snippets are fetched — raw bodies never leave
the machine. Sends: only invoked by the pending-actions queue after explicit
user approval.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import make_msgid

from googleapiclient.discovery import build

from subsystems.google_auth import get_credentials


class GmailClient:
    """Read-only Gmail API client. Fetches headers and snippets only — never full bodies."""

    def __init__(self, account_name: str):
        self.account_name = account_name
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = get_credentials(self.account_name)
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def get_profile(self) -> dict:
        return self._get_service().users().getProfile(userId="me").execute()

    def get_recent_messages(self, count: int = 20, hours: int = 24) -> list[dict]:
        """Fetch recent message headers + snippets from the inbox."""
        import time
        service = self._get_service()

        after_ts = int(time.time()) - hours * 3600
        result = service.users().messages().list(
            userId="me",
            q=f"after:{after_ts} in:inbox",
            maxResults=min(count, 50),
        ).execute()

        messages = result.get("messages", [])
        detailed = []

        for msg in messages:
            full = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()

            headers = {
                h["name"]: h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            label_ids = full.get("labelIds", [])
            detailed.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": full.get("snippet", ""),
                "unread": "UNREAD" in label_ids,
                "starred": "STARRED" in label_ids,
                "account": self.account_name,
            })

        return detailed

    def search_messages(self, query: str, count: int = 10) -> list[dict]:
        """Search Gmail using Gmail's query syntax (e.g. 'from:boss@co.com subject:report')."""
        service = self._get_service()
        result = service.users().messages().list(
            userId="me", q=query, maxResults=min(count, 50)
        ).execute()

        messages = result.get("messages", [])
        detailed = []

        for msg in messages:
            full = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()

            headers = {
                h["name"]: h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            detailed.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": full.get("snippet", ""),
                "unread": "UNREAD" in full.get("labelIds", []),
                "account": self.account_name,
            })

        return detailed

    def get_unread_count(self) -> int:
        """Return the number of unread messages in the inbox."""
        service = self._get_service()
        result = service.users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=1
        ).execute()
        return result.get("resultSizeEstimate", 0)

    def _resolve_thread_headers(self, in_reply_to_id: str) -> tuple[str | None, str | None, str | None]:
        """Look up the original message's Message-ID + References + threadId so a
        reply lands in the same Gmail thread. Returns (message_id_header,
        references_header, gmail_thread_id). Any may be None if lookup fails."""
        try:
            full = self._get_service().users().messages().get(
                userId="me",
                id=in_reply_to_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"],
            ).execute()
        except Exception:
            return None, None, None
        headers = {
            h["name"].lower(): h["value"]
            for h in full.get("payload", {}).get("headers", [])
        }
        msg_id = headers.get("message-id")
        refs = headers.get("references", "") or ""
        new_refs = (refs + " " + msg_id).strip() if msg_id else (refs or None)
        return msg_id, new_refs, full.get("threadId")

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to_id: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
    ) -> dict:
        """Send a plain-text email. Returns {'id', 'threadId'} from the Gmail API.

        If in_reply_to_id is supplied, the reply is threaded via In-Reply-To /
        References headers and the Gmail threadId.
        """
        service = self._get_service()
        from_addr = self.get_profile().get("emailAddress", "")

        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["From"] = from_addr
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        msg.set_content(body)

        thread_id: str | None = None
        if in_reply_to_id:
            parent_msgid, refs, thread_id = self._resolve_thread_headers(in_reply_to_id)
            if parent_msgid:
                msg["In-Reply-To"] = parent_msgid
            if refs:
                msg["References"] = refs

        raw = base64.urlsafe_b64encode(bytes(msg)).decode()
        payload: dict = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        sent = service.users().messages().send(userId="me", body=payload).execute()
        return {"id": sent.get("id"), "threadId": sent.get("threadId"), "from": from_addr}
