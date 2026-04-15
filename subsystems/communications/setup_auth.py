#!/usr/bin/env python3
"""Interactive setup script for Pepper email accounts.

Handles:
  - Personal Google Mail + Calendar (shared OAuth2)
  - Work Google Mail + Calendar (shared OAuth2)
  - Yahoo Mail (IMAP app password)

Run: python subsystems/communications/setup_auth.py
"""

from __future__ import annotations

import json
from pathlib import Path

from subsystems.google_auth import CREDENTIALS_PATH as GOOGLE_CREDENTIALS_PATH
from subsystems.google_auth import get_credentials as get_google_credentials
from subsystems.google_auth import list_authorized_accounts as list_authorized_google_accounts
from subsystems.google_auth import token_path as google_token_path
from subsystems.calendar.client import CalendarClient
from subsystems.calendar.preferences import prompt_shared_calendar_selection

CONFIG_DIR = Path.home() / ".config" / "pepper"
YAHOO_CREDENTIALS_PATH = CONFIG_DIR / "yahoo_credentials.json"
LEGACY_CREDENTIALS_PATH = CONFIG_DIR / "email_credentials.json"


def setup_gmail(account_name: str, label: str) -> bool:
    """Authenticate a Google account for both Gmail and Calendar read access."""
    print(f"\n--- Setting up {label} Google account ({account_name}) ---")

    if not GOOGLE_CREDENTIALS_PATH.exists():
        print(f"\n[!] Google credentials file not found at: {GOOGLE_CREDENTIALS_PATH}")
        print("    This shared OAuth2 client credentials file is used for both Gmail and Google Calendar.")
        print("    Steps:")
        print("    1. Go to https://console.cloud.google.com/apis/credentials")
        print("    2. Ensure both Gmail API and Google Calendar API are enabled in your project")
        print("    3. Download the OAuth 2.0 Client credentials JSON")
        print(f"    4. Save it to: {GOOGLE_CREDENTIALS_PATH}")
        print("    Then re-run this script.")
        return False

    try:
        token_path = google_token_path(account_name)

        print(f"\nOpening browser for {label} Google authentication...")
        print(f"Please sign in with your {label.lower()} Google account.\n")

        creds = get_google_credentials(account_name)

        # Verify by fetching profile
        from googleapiclient.discovery import build
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email_addr = profile.get("emailAddress", "unknown")

        calendar_client = CalendarClient(account=account_name)
        calendars = calendar_client.list_calendars(include_excluded=True)
        if calendars:
            print(f"\nFound {len(calendars)} Google calendar(s):\n")
            for cal in calendars:
                access = cal.get("accessRole", "?")
                primary = " [primary]" if cal.get("primary") else ""
                print(f"  {cal['summary']:<40} ({access}){primary}")
            prompt_shared_calendar_selection(account_name, calendars)

        print(f"\n[OK] Authenticated as: {email_addr}")
        print(f"     Token saved to: {token_path}")
        return True

    except Exception as e:
        print(f"\n[ERROR] Gmail auth failed: {e}")
        return False


def choose_google_calendars(account_name: str, label: str) -> bool:
    """Prompt for which non-primary Google calendars Pepper should keep."""
    try:
        print(f"\n--- Choosing {label} Google calendars ({account_name}) ---")
        client = CalendarClient(account=account_name)
        calendars = client.list_calendars(include_excluded=True)

        if not calendars:
            print("[!] No Google calendars found for this account.")
            return False

        print(f"\nFound {len(calendars)} Google calendar(s):\n")
        for cal in calendars:
            access = cal.get("accessRole", "?")
            primary = " [primary]" if cal.get("primary") else ""
            print(f"  {cal['summary']:<40} ({access}){primary}")

        excluded_ids = prompt_shared_calendar_selection(account_name, calendars)
        if not any(not cal.get("primary") for cal in calendars):
            print("\nNo shared/subscribed calendars to choose from. Primary calendars stay enabled.")
        elif excluded_ids:
            print("\nUpdated calendar selection.")
        return True
    except Exception as e:
        print(f"\n[ERROR] Could not load Google calendars: {e}")
        return False


def setup_yahoo(account_name: str = "yahoo") -> bool:
    """Store Yahoo IMAP credentials (app-specific password)."""
    print(f"\n--- Setting up Yahoo Mail ({account_name}) ---")
    print()
    print("Yahoo requires an app-specific password for IMAP access.")
    print("Generate one at: https://login.yahoo.com/account/security/app-passwords")
    print("  1. Sign in to Yahoo")
    print("  2. Go to Account Security → App Passwords")
    print("  3. Create a new app password (name it 'Pepper')")
    print("  4. Copy the generated password (16 characters, no spaces)\n")

    email_addr = input("Yahoo email address: ").strip()
    if not email_addr:
        print("[!] Skipped.")
        return False

    password = input("App-specific password: ").strip()
    if not password:
        print("[!] Skipped.")
        return False

    # Test the connection
    print("\nTesting connection to imap.mail.yahoo.com:993 ...")
    try:
        import imaplib
        conn = imaplib.IMAP4_SSL("imap.mail.yahoo.com", 993)
        conn.login(email_addr, password)
        conn.logout()
        print("[OK] Connection successful.")
    except Exception as e:
        print(f"[ERROR] Could not connect: {e}")
        print("Check your email address and app password, then try again.")
        return False

    # Save credentials
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    all_creds: dict = {}
    if YAHOO_CREDENTIALS_PATH.exists():
        with open(YAHOO_CREDENTIALS_PATH) as f:
            all_creds = json.load(f)
    elif LEGACY_CREDENTIALS_PATH.exists():
        with open(LEGACY_CREDENTIALS_PATH) as f:
            all_creds = json.load(f)

    all_creds[account_name] = {
        "provider": "yahoo",
        "email": email_addr,
        "password": password,
    }

    YAHOO_CREDENTIALS_PATH.write_text(json.dumps(all_creds, indent=2))
    YAHOO_CREDENTIALS_PATH.chmod(0o600)  # Owner read/write only
    print(f"     Credentials saved to: {YAHOO_CREDENTIALS_PATH}")
    return True


def list_authorized_gmail_accounts() -> list[str]:
    accounts = []
    for account in list_authorized_google_accounts():
        accounts.append("personal" if account == "default" else account)
    return accounts


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pepper email account setup")
    parser.add_argument("--account", default=None, help="Google account name to authorize (e.g. 'work'). Runs interactive flow if omitted.")
    parser.add_argument("--list", action="store_true", help="List authorized Google accounts.")
    parser.add_argument("--choose-calendars", action="store_true", help="Review which Google calendars Pepper should keep for an already authorized account.")
    args = parser.parse_args()

    if args.list:
        accounts = list_authorized_gmail_accounts()
        if not accounts:
            print("No authorized Google accounts.")
        else:
            print("Authorized Google accounts (shared Gmail + Calendar tokens):")
            for acc in accounts:
                print(f"  {acc}  ({google_token_path(acc)})")
        return

    if args.account:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if args.choose_calendars:
            choose_google_calendars(args.account, args.account.capitalize())
            return
        success = setup_gmail(args.account, args.account.capitalize())
        if success:
            print(f"\nYou can now ask Pepper about {args.account} email and calendar.")
        return

    # Interactive flow
    print("=" * 60)
    print("Pepper Email Account Setup")
    print("=" * 60)
    print()
    print("This will connect your email accounts to Pepper.")
    print("Google auth now covers both Gmail and Google Calendar together.")
    print("Only message headers and snippets are ever read —")
    print("full email bodies stay on-device and are never transmitted.")
    print()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    results = {}

    # Personal Gmail
    ans = input("Set up personal Gmail? [Y/n]: ").strip().lower()
    if ans != "n":
        results["personal Gmail"] = setup_gmail("personal", "Personal")

    # Work Gmail
    ans = input("\nSet up work Gmail? [Y/n]: ").strip().lower()
    if ans != "n":
        results["work Gmail"] = setup_gmail("work", "Work")

    # Yahoo
    ans = input("\nSet up Yahoo Mail? [Y/n]: ").strip().lower()
    if ans != "n":
        results["Yahoo Mail"] = setup_yahoo("yahoo")

    # Summary
    print("\n" + "=" * 60)
    print("Setup Summary")
    print("=" * 60)
    for account, success in results.items():
        status = "OK" if success else "FAILED / Skipped"
        print(f"  {account}: {status}")

    if any(results.values()):
        print("\nYou can now ask Pepper about your emails.")
    else:
        print("\nNo accounts were set up. Run this script again when ready.")


if __name__ == "__main__":
    main()
