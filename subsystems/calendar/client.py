"""Google Calendar API client.

Fetches all calendars the user has access to (owned, shared, subscribed)
and their events. Uses read-only OAuth credentials from auth.py.

Usage:
    from subsystems.calendar.client import CalendarClient

    client = CalendarClient()
    calendars = client.list_calendars()
    events = client.list_upcoming_events(days=7)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import build

from subsystems.calendar.auth import get_credentials
from subsystems.calendar.preferences import get_excluded_calendar_ids


class CalendarClient:
    def __init__(self, account: str | None = None) -> None:
        """
        Args:
            account: Google account name (e.g. "work"). None = default/personal account.
        """
        self._account = account
        creds = get_credentials(account)
        self._service = build("calendar", "v3", credentials=creds)

    # ------------------------------------------------------------------
    # Calendars
    # ------------------------------------------------------------------

    def list_calendars(self, include_excluded: bool = False) -> list[dict[str, Any]]:
        """Return all calendars the user can see: owned, shared, subscribed."""
        result = []
        page_token = None
        while True:
            resp = self._service.calendarList().list(pageToken=page_token).execute()
            result.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if include_excluded:
            return result

        excluded_ids = get_excluded_calendar_ids(self._account)
        if not excluded_ids:
            return result
        return [calendar for calendar in result if calendar.get("id", "") not in excluded_ids]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def list_upcoming_events(
        self,
        days: int = 7,
        calendar_ids: list[str] | None = None,
        max_per_calendar: int = 250,
    ) -> list[dict[str, Any]]:
        """Return events from now through *days* ahead across all (or specified) calendars."""
        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        if calendar_ids is None:
            calendar_ids = [cal["id"] for cal in self.list_calendars()]

        events: list[dict[str, Any]] = []
        for cal_id in calendar_ids:
            try:
                page_token = None
                while True:
                    resp = (
                        self._service.events()
                        .list(
                            calendarId=cal_id,
                            timeMin=time_min,
                            timeMax=time_max,
                            maxResults=max_per_calendar,
                            singleEvents=True,
                            orderBy="startTime",
                            pageToken=page_token,
                        )
                        .execute()
                    )
                    for event in resp.get("items", []):
                        event["_calendar_id"] = cal_id
                    events.extend(resp.get("items", []))
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as exc:  # noqa: BLE001
                # One bad calendar shouldn't break the whole fetch
                print(f"[calendar] skipping {cal_id}: {exc}")

        events.sort(key=_event_sort_key)
        return events

    def list_events_range(
        self,
        start: datetime,
        end: datetime,
        calendar_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return events between *start* and *end*."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        if calendar_ids is None:
            calendar_ids = [cal["id"] for cal in self.list_calendars()]

        events: list[dict[str, Any]] = []
        for cal_id in calendar_ids:
            try:
                page_token = None
                while True:
                    resp = (
                        self._service.events()
                        .list(
                            calendarId=cal_id,
                            timeMin=start.isoformat(),
                            timeMax=end.isoformat(),
                            maxResults=2500,
                            singleEvents=True,
                            orderBy="startTime",
                            pageToken=page_token,
                        )
                        .execute()
                    )
                    for event in resp.get("items", []):
                        event["_calendar_id"] = cal_id
                    events.extend(resp.get("items", []))
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as exc:  # noqa: BLE001
                print(f"[calendar] skipping {cal_id}: {exc}")

        events.sort(key=_event_sort_key)
        return events


def _event_sort_key(event: dict[str, Any]) -> str:
    start = event.get("start", {})
    return start.get("dateTime") or start.get("date") or ""
