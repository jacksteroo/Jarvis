#!/usr/bin/env python3
"""Google OAuth setup for shared Calendar + Gmail access.

Usage:
    python subsystems/calendar/setup_auth.py                      # default/personal account
    python subsystems/calendar/setup_auth.py --account work       # named account
    python subsystems/calendar/setup_auth.py --list             # list authorized accounts
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from subsystems.calendar.auth import CREDENTIALS_PATH, list_authorized_accounts, _token_path, get_credentials
from subsystems.calendar.client import CalendarClient
from subsystems.calendar.preferences import prompt_shared_calendar_selection


def choose_calendars(account: str | None) -> None:
    label = f"'{account}'" if account else "default/personal"

    print(f"Reviewing calendar selection for {label} account")
    print("=" * 50)

    print(f"\nFetching calendars for {label} account…")
    client = CalendarClient(account=account)
    calendars = client.list_calendars(include_excluded=True)

    print(f"\nFound {len(calendars)} calendar(s):\n")
    for cal in calendars:
        access = cal.get("accessRole", "?")
        primary = " [primary]" if cal.get("primary") else ""
        print(f"  {cal['summary']:<40} ({access}){primary}")

    excluded_ids = prompt_shared_calendar_selection(account, calendars)
    if not any(not cal.get("primary") for cal in calendars):
        print("\nNo shared/subscribed calendars to choose from. Primary calendars stay enabled.")
    elif excluded_ids:
        print("\nUpdated calendar selection.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google OAuth setup for shared Gmail + Calendar access")
    parser.add_argument("--account", default=None, help="Account name (e.g. 'work'). Omit for default/personal.")
    parser.add_argument("--list", action="store_true", help="List authorized accounts and exit.")
    parser.add_argument("--choose-calendars", action="store_true", help="Review which calendars Pepper should keep for an already authorized account.")
    args = parser.parse_args()

    if args.list:
        accounts = list_authorized_accounts()
        if not accounts:
            print("No authorized accounts.")
        else:
            print("Authorized accounts:")
            for acc in accounts:
                print(f"  {acc}  ({_token_path(None if acc == 'default' else acc)})")
        return

    account = args.account
    label = f"'{account}'" if account else "default/personal"

    if args.choose_calendars:
        choose_calendars(account)
        return

    print(f"Google OAuth Setup — {label} account")
    print("=" * 50)

    if not CREDENTIALS_PATH.exists():
        print(f"\nCredentials file not found: {CREDENTIALS_PATH}")
        print("\nSteps to create one:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create a project (or select an existing one)")
        print("  3. Enable both the Google Calendar API and Gmail API")
        print("  4. Create credentials → OAuth client ID → Desktop app")
        print(f"  5. Download the JSON and save it to:\n       {CREDENTIALS_PATH}")
        print("\nThen re-run this script.")
        sys.exit(1)

    token_path = _token_path(account)
    print(f"\nCredentials: {CREDENTIALS_PATH}")
    print(f"Token will be saved to: {token_path}")
    if account:
        print(f"\nA browser will open — sign in with your {account} Google account.")
    print("Opening browser for shared Google sign-in (Calendar + Gmail)…\n")

    creds = get_credentials(account)
    print(f"Token saved to: {token_path}")
    print(f"Expires: {creds.expiry} (will auto-refresh)")

    print(f"\nFetching calendars for {label} account…")
    client = CalendarClient(account=account)
    calendars = client.list_calendars(include_excluded=True)

    print(f"\nFound {len(calendars)} calendar(s):\n")
    for cal in calendars:
        access = cal.get("accessRole", "?")
        primary = " [primary]" if cal.get("primary") else ""
        print(f"  {cal['summary']:<40} ({access}){primary}")

    prompt_shared_calendar_selection(account, calendars)

    print(f"\nDone. {label} account is now authorized for both Calendar and Gmail.")


if __name__ == "__main__":
    main()
