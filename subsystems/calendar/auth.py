"""Google OAuth2 authentication for calendar access.

Calendar now shares one Google token per account with Gmail. Each shared token
authorizes both read-only Calendar and Gmail access:
  ~/.config/pepper/google_token.json            ← default/personal account
  ~/.config/pepper/google_token_work.json       ← work account
  ~/.config/pepper/google_token_business_name.json  ← named account
"""

from __future__ import annotations

from pathlib import Path

from google.oauth2.credentials import Credentials

from subsystems.google_auth import CREDENTIALS_PATH as _CREDENTIALS_PATH
from subsystems.google_auth import get_credentials as _get_shared_credentials
from subsystems.google_auth import list_authorized_accounts as _list_shared_accounts
from subsystems.google_auth import token_path


def _token_path(account: str | None) -> Path:
    return token_path(account)


CREDENTIALS_PATH = _CREDENTIALS_PATH


def get_credentials(account: str | None = None) -> Credentials:
    return _get_shared_credentials(account)


def list_authorized_accounts() -> list[str]:
    return _list_shared_accounts()
