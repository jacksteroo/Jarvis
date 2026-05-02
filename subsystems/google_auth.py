"""Shared Google OAuth helpers for Pepper.

One token per Google account authorizes both Gmail and Calendar read access.

Token layout:
  ~/.config/pepper/google_token.json          ← default/personal account
  ~/.config/pepper/google_token_work.json     ← named account
  ~/.config/pepper/google_token_business_name.json

Legacy Gmail-only token files (e.g. gmail_work_token.json) are still detected
for migration purposes, but new auth writes only the shared google_token*.json
files.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

CONFIG_DIR = Path.home() / ".config" / "pepper"
CREDENTIALS_PATH = CONFIG_DIR / "google_credentials.json"

_token_locks: dict[str, threading.Lock] = {}
_token_locks_mutex = threading.Lock()


def _normalize_account(account: str | None) -> str | None:
    normalized = (account or "").strip().lower()
    if not normalized or normalized in {"default", "personal"}:
        return None
    return normalized


def _shared_token_path(account: str | None) -> Path:
    normalized = _normalize_account(account)
    if normalized:
        return CONFIG_DIR / f"google_token_{normalized}.json"
    return CONFIG_DIR / "google_token.json"


def _legacy_gmail_token_path(account: str | None) -> Path:
    normalized = _normalize_account(account) or "personal"
    return CONFIG_DIR / f"gmail_{normalized}_token.json"


def _get_token_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _token_locks_mutex:
        if key not in _token_locks:
            _token_locks[key] = threading.Lock()
        return _token_locks[key]


def _load_credentials(path: Path) -> Credentials | None:
    if not path.exists():
        return None
    return Credentials.from_authorized_user_file(str(path), GOOGLE_SCOPES)


def _read_granted_scopes(path: Path) -> set[str]:
    if not path.exists():
        return set()

    data = json.loads(path.read_text())
    raw_scopes = data.get("scopes")
    if isinstance(raw_scopes, list):
        return {scope for scope in raw_scopes if isinstance(scope, str)}

    raw_scope = data.get("scope")
    if isinstance(raw_scope, str):
        return {scope for scope in raw_scope.split() if scope}

    return set()


def _has_required_scopes(creds: Credentials | None, path: Path) -> bool:
    if not creds:
        return False

    granted_scopes = _read_granted_scopes(path)
    if granted_scopes:
        return set(GOOGLE_SCOPES).issubset(granted_scopes)

    # Fallback for unexpected credential formats where the token file omits scope metadata.
    return bool(getattr(creds, "granted_scopes", None) and creds.has_scopes(GOOGLE_SCOPES))


def _load_best_available_credentials(account: str | None) -> tuple[Credentials | None, Path]:
    shared_path = _shared_token_path(account)
    legacy_path = _legacy_gmail_token_path(account)
    first_available: tuple[Credentials | None, Path] | None = None

    for candidate_path in (shared_path, legacy_path):
        creds = _load_credentials(candidate_path)
        if creds is None:
            continue
        if first_available is None:
            first_available = (creds, candidate_path)
        if _has_required_scopes(creds, candidate_path):
            return creds, candidate_path

    if first_available is not None:
        return first_available
    return None, shared_path


def get_credentials(account: str | None = None) -> Credentials:
    """Return valid Google OAuth credentials for the given account.

    The returned token must include both Gmail and Calendar read-only scopes.
    Old single-scope tokens trigger a one-time re-auth so Pepper ends up with a
    single shared token per account.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token_path = _shared_token_path(account)

    creds, source_path = _load_best_available_credentials(account)
    if creds and creds.valid and _has_required_scopes(creds, source_path):
        if source_path != token_path:
            token_path.write_text(creds.to_json())
        return creds

    with _get_token_lock(token_path):
        creds, source_path = _load_best_available_credentials(account)
        if creds and creds.valid and _has_required_scopes(creds, source_path):
            if source_path != token_path:
                token_path.write_text(creds.to_json())
            return creds

        if creds and creds.expired and creds.refresh_token and _has_required_scopes(creds, source_path):
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds

        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Google credentials not found at {CREDENTIALS_PATH}.\n"
                "Download OAuth credentials from Google Cloud Console:\n"
                "  https://console.cloud.google.com/apis/credentials\n"
                "Ensure both the Gmail API and Google Calendar API are enabled.\n"
                f"Then save the file to: {CREDENTIALS_PATH}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), GOOGLE_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        token_path.write_text(creds.to_json())
        return creds


def token_path(account: str | None = None) -> Path:
    """Return the canonical shared token path for the account."""
    return _shared_token_path(account)


def list_authorized_accounts() -> list[str]:
    """Return names of all authorized shared Google accounts."""
    accounts = []
    for path in CONFIG_DIR.glob("google_token*.json"):
        name = path.stem.removeprefix("google_token")
        accounts.append(name.lstrip("_") or "default")
    return sorted(accounts)
