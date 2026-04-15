"""Calendar selection preferences stored in config/local/accounts.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "local" / "accounts.json"


def _normalize_account(account: str | None) -> str:
    normalized = (account or "").strip().lower()
    if not normalized or normalized in {"default", "personal"}:
        return "default"
    return normalized


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def _write_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")


def get_excluded_calendar_ids(account: str | None = None) -> set[str]:
    config = _load_config()
    calendar = config.get("calendar", {})

    excluded = set(calendar.get("excluded_calendar_ids", []))
    by_account = calendar.get("excluded_calendar_ids_by_account", {})
    excluded.update(by_account.get(_normalize_account(account), []))
    return excluded


def save_excluded_calendar_ids(account: str | None, calendar_ids: list[str]) -> None:
    config = _load_config()
    calendar = config.setdefault("calendar", {})
    by_account = calendar.setdefault("excluded_calendar_ids_by_account", {})

    account_key = _normalize_account(account)
    normalized_ids = sorted({calendar_id for calendar_id in calendar_ids if calendar_id})

    if normalized_ids:
        by_account[account_key] = normalized_ids
    else:
        by_account.pop(account_key, None)

    if not by_account:
        calendar.pop("excluded_calendar_ids_by_account", None)

    if not calendar:
        config.pop("calendar", None)

    _write_config(config)


def prompt_shared_calendar_selection(
    account: str | None,
    calendars: list[dict[str, Any]],
    *,
    input_fn=input,
    print_fn=print,
) -> list[str]:
    """Prompt for which non-primary calendars to keep. Returns excluded ids."""
    shared_calendars = [cal for cal in calendars if not cal.get("primary", False)]
    if not shared_calendars:
        return []

    existing_excluded = get_excluded_calendar_ids(account)
    default_kept_indexes = [
        str(index)
        for index, calendar in enumerate(shared_calendars, start=1)
        if calendar.get("id", "") not in existing_excluded
    ]

    print_fn("\nShared/subscribed calendars found.")
    print_fn("Choose which ones Pepper should keep reading. Primary calendars stay enabled.\n")

    for index, calendar in enumerate(shared_calendars, start=1):
        calendar_id = calendar.get("id", "")
        summary = calendar.get("summary", calendar_id)
        access = calendar.get("accessRole", "?")
        status = "kept" if calendar_id not in existing_excluded else "removed"
        print_fn(f"  {index}. {summary} ({access}) [{status}]")

    default_prompt = ",".join(default_kept_indexes) if default_kept_indexes else "none"

    while True:
        raw = input_fn(
            f"\nKeep which shared calendars? [default: {default_prompt}, 'all' to keep all, 'none' to remove all]: "
        ).strip().lower()

        if not raw:
            keep_ids = {
                calendar.get("id", "")
                for calendar in shared_calendars
                if calendar.get("id", "") not in existing_excluded
            }
            break

        if raw == "all":
            keep_ids = {calendar.get("id", "") for calendar in shared_calendars}
            break

        if raw == "none":
            keep_ids = set()
            break

        try:
            selected_indexes = {
                int(part.strip())
                for part in raw.split(",")
                if part.strip()
            }
        except ValueError:
            print_fn("Please enter numbers like '1,3', 'all', 'none', or press Enter for the default.")
            continue

        if not selected_indexes.issubset(set(range(1, len(shared_calendars) + 1))):
            print_fn("One or more selections were out of range. Try again.")
            continue

        keep_ids = {
            shared_calendars[index - 1].get("id", "")
            for index in selected_indexes
        }
        break

    excluded_ids = sorted(
        calendar.get("id", "")
        for calendar in shared_calendars
        if calendar.get("id", "") and calendar.get("id", "") not in keep_ids
    )
    save_excluded_calendar_ids(account, excluded_ids)

    kept_count = len(shared_calendars) - len(excluded_ids)
    print_fn(
        f"Saved calendar selection: keeping {kept_count} shared calendar(s), removing {len(excluded_ids)}."
    )
    return excluded_ids
