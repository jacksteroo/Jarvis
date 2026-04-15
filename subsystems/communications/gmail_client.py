"""Gmail API client for Pepper.

Uses the shared Google OAuth token for the account, which now authorizes both
Gmail and Calendar read access:
  ~/.config/pepper/google_token.json       ← personal/default account
  ~/.config/pepper/google_token_work.json  ← named account

Only message headers and snippets are fetched — raw bodies never leave the machine.
"""

from __future__ import annotations

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
