"""Loads account/label configuration from config/local/accounts.json.

Never hardcode account names, labels, or personal addresses here.
All personal config lives in config/local/accounts.json (gitignored).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "local" / "accounts.json"


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime_ns: int) -> dict:
    with open(path) as f:
        return json.load(f)


def _load() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    return _load_cached(str(_CONFIG_PATH), _CONFIG_PATH.stat().st_mtime_ns)


def _normalize_calendar_account(account: str | None) -> str:
    normalized = (account or "").strip().lower()
    if not normalized or normalized in {"default", "personal"}:
        return "default"
    return normalized


def get_email_accounts() -> list[dict]:
    """Return list of configured email accounts: [{id, label, type}, ...]"""
    return _load().get("email", {}).get("accounts", [])


def get_email_account(account_id: str) -> dict | None:
    """Return the configured email account dict for an account id, if present."""
    for account in get_email_accounts():
        if account.get("id") == account_id:
            return account
    return None


def get_email_account_ids() -> list[str]:
    return [a["id"] for a in get_email_accounts()]


def get_email_label(account_id: str) -> str:
    """Return human label for an email account id."""
    for a in get_email_accounts():
        if a["id"] == account_id:
            return a.get("label", account_id)
    return account_id


def get_gmail_account_ids() -> list[str]:
    return [a["id"] for a in get_email_accounts() if a.get("type") == "gmail"]


def get_imap_account_ids() -> list[str]:
    return [a["id"] for a in get_email_accounts() if a.get("type") == "yahoo"]


def get_google_auth_account(account_id: str) -> str:
    """Return the shared Google auth account slug for a configured email account.

    By default the auth account matches the Pepper account id. When they diverge
    because the shared Google token was authorized under a different slug, set
    ``google_account`` (or legacy ``auth_account``) in ``config/local/accounts.json``.
    """
    account = get_email_account(account_id)
    if not account:
        return account_id
    return account.get("google_account") or account.get("auth_account") or account_id


def get_calendar_id_labels() -> dict[str, str]:
    """Return map of Google Calendar ID → friendly label (e.g. user@example.com → Work)."""
    return _load().get("calendar", {}).get("calendar_id_labels", {})


def get_calendar_account_labels() -> dict[str, str]:
    """Return map of account name → friendly label (e.g. business_name → Business Name)."""
    return _load().get("calendar", {}).get("account_labels", {})


def get_calendar_excluded_ids(account: str | None = None) -> set[str]:
    """Return calendar IDs Pepper should skip for the given Google account."""
    calendar = _load().get("calendar", {})
    excluded = set(calendar.get("excluded_calendar_ids", []))
    by_account = calendar.get("excluded_calendar_ids_by_account", {})
    excluded.update(by_account.get(_normalize_calendar_account(account), []))
    return excluded


def get_location(name: str) -> str:
    """Return a named location address (e.g. 'home', 'work')."""
    return _load().get("locations", {}).get(name, "")
