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


    # ------------------------------------------------------------------
    # Event creation (writable calendars only)
    # ------------------------------------------------------------------

    # Google Calendar accessRole values that allow event creation.
    _WRITABLE_ROLES = {"owner", "writer"}

    def list_writable_calendars(self) -> list[dict[str, Any]]:
        """Subset of list_calendars() that the OAuth token can WRITE to.

        Use this for picking which calendar to create an event under — for
        example to distinguish the personal calendar from a 'Family shared'
        calendar that the user owns or has been granted writer access to.
        """
        out = []
        for cal in self.list_calendars(include_excluded=True):
            if cal.get("accessRole") in self._WRITABLE_ROLES:
                out.append({
                    "id": cal.get("id"),
                    "summary": cal.get("summary"),
                    "description": cal.get("description", ""),
                    "primary": bool(cal.get("primary")),
                    "access_role": cal.get("accessRole"),
                    "background_color": cal.get("backgroundColor"),
                    "time_zone": cal.get("timeZone"),
                })
        return out

    def create_event(
        self,
        *,
        calendar_id: str,
        summary: str,
        start: str,
        end: str,
        time_zone: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        all_day: bool = False,
        send_updates: str = "none",
    ) -> dict[str, Any]:
        """Create an event on `calendar_id`.

        Time formats:
          - all_day=False: `start` and `end` must be RFC3339 strings
            (e.g. '2026-05-12T15:00:00-07:00'). If you only have a naive
            datetime, supply `time_zone` (e.g. 'America/Los_Angeles') and
            pass start/end as 'YYYY-MM-DDTHH:MM:SS'.
          - all_day=True: `start` and `end` are date-only ('YYYY-MM-DD').

        send_updates: 'all' | 'externalOnly' | 'none' — whether to email
        invitees. Defaults to 'none' so the user is never surprised.
        """
        if not calendar_id:
            raise ValueError("calendar_id is required")
        if not summary:
            raise ValueError("summary is required")
        if not start or not end:
            raise ValueError("start and end are required")

        if all_day:
            start_field: dict[str, Any] = {"date": start}
            end_field: dict[str, Any] = {"date": end}
        else:
            start_field = {"dateTime": start}
            end_field = {"dateTime": end}
            if time_zone:
                start_field["timeZone"] = time_zone
                end_field["timeZone"] = time_zone

        body: dict[str, Any] = {
            "summary": summary,
            "start": start_field,
            "end": end_field,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees if a]

        created = (
            self._service.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates=send_updates)
            .execute()
        )
        return {
            "id": created.get("id"),
            "html_link": created.get("htmlLink"),
            "calendar_id": calendar_id,
            "summary": created.get("summary"),
            "start": created.get("start"),
            "end": created.get("end"),
        }


def _event_sort_key(event: dict[str, Any]) -> str:
    start = event.get("start", {})
    return start.get("dateTime") or start.get("date") or ""
