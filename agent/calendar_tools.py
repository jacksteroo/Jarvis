"""Calendar tool definitions and helpers for Pepper core.

Follows the same pattern as web_search.py / routing.py:
  - CALENDAR_TOOLS: Anthropic tool-schema list for the LLM
  - Helper functions called by PepperCore._execute_tool and _maybe_get_calendar_context
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from agent.query_intents import CALENDAR_QUERY_TERMS, infer_calendar_days, is_source_query

logger = structlog.get_logger()


def _event_sort_key(event: dict[str, Any]) -> str:
    start = event.get("start", {})
    return start.get("dateTime") or start.get("date") or ""


def _event_dedup_key(event: dict[str, Any]) -> tuple[str, str, str]:
    """Cross-account dedup key for the same event mirrored on multiple calendars.

    Title + start + end is the strongest signal we have without relying on
    iCalUID (which doesn't always survive Google's cross-account sync).
    """
    title = (event.get("summary") or "").strip().lower()
    start = event.get("start", {})
    end = event.get("end", {})
    start_iso = start.get("dateTime") or start.get("date") or ""
    end_iso = end.get("dateTime") or end.get("date") or ""
    return (title, start_iso, end_iso)


def _is_all_day(event: dict[str, Any]) -> bool:
    return "date" in event.get("start", {}) and "dateTime" not in event.get("start", {})


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = _event_dedup_key(ev)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out

def _calendar_id_labels() -> dict[str, str]:
    from agent.accounts import get_calendar_id_labels
    return get_calendar_id_labels()


def _calendar_account_labels() -> dict[str, str]:
    from agent.accounts import get_calendar_account_labels
    return get_calendar_account_labels()

CALENDAR_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_upcoming_events",
            "description": (
                "Fetch upcoming calendar events across ALL of the user's Google accounts and calendars "
                "(personal, work, business name, partner company, shared, subscribed). "
                "Always call without calendar_filter unless the user explicitly asks for one specific calendar. "
                "Use when asked about schedule, meetings, what's coming up, or availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days ahead to look (default 7, max 90)",
                        "default": 7,
                    },
                    "calendar_filter": {
                        "type": "string",
                        "description": (
                            "ONLY use when the user explicitly asks to narrow to ONE specific calendar "
                            "(e.g. 'show me only my work calendar'). "
                            "Do NOT pass this for default schedule queries — omitting it returns all calendars "
                            "across all accounts, which is always preferred."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_calendar_events_range",
            "description": (
                "Fetch calendar events between two specific dates. Use this for any query "
                "about the past (e.g. 'what did I do last October', '18 months ago', "
                "'in Q3 2024') or for future ranges beyond 90 days. Accepts ISO date strings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start of the range, ISO 8601 date or datetime (e.g. '2024-10-01' or '2024-10-01T00:00:00')",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End of the range, ISO 8601 date or datetime (e.g. '2024-10-31' or '2024-10-31T23:59:59')",
                    },
                    "calendar_filter": {
                        "type": "string",
                        "description": (
                            "ONLY use when the user explicitly asks to narrow to ONE specific calendar. "
                            "Omit to query all calendars across all accounts (always preferred for default queries)."
                        ),
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "list_calendars",
            "description": "List all Google Calendars the user has access to.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "list_writable_calendars",
            "description": (
                "List calendars the user can CREATE events on, across all Google accounts. "
                "Always call this BEFORE draft_calendar_event so you choose the right calendar_id "
                "(personal vs Family shared vs Work etc.). Returns calendar_id, account, summary, "
                "primary flag, accessRole, and time_zone for each writable calendar."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_calendar_event",
            "description": (
                "Queue a new calendar event for the user to approve. ALWAYS call list_writable_calendars "
                "first to get the right calendar_id (personal vs Family shared vs Work). The event "
                "is NOT created until the user approves the draft from Telegram or the status panel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Google account that owns the calendar (e.g. 'default', 'work'). Match the 'account' field returned by list_writable_calendars.",
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "The calendar id from list_writable_calendars (e.g. 'primary', or 'family123@group.calendar.google.com').",
                    },
                    "summary": {"type": "string", "description": "Event title."},
                    "start": {
                        "type": "string",
                        "description": (
                            "Start time. RFC3339 datetime (e.g. '2026-05-12T15:00:00-07:00') for timed "
                            "events, or YYYY-MM-DD when all_day=true."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "End time. RFC3339 datetime for timed events, or YYYY-MM-DD (exclusive) "
                            "when all_day=true."
                        ),
                    },
                    "time_zone": {
                        "type": "string",
                        "description": "IANA TZ (e.g. 'America/Los_Angeles'). Required if start/end are naive datetimes.",
                    },
                    "all_day": {"type": "boolean", "description": "If true, treat start/end as date-only. Default false."},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of attendee email addresses.",
                    },
                    "send_updates": {
                        "type": "string",
                        "enum": ["all", "externalOnly", "none"],
                        "description": "Whether to email invitees on creation. Default 'none'.",
                    },
                },
                "required": ["calendar_id", "summary", "start", "end"],
            },
        },
    },
]


def _get_client(account: str | None = None):
    """Lazy import to avoid import errors when Google libs aren't installed."""
    from subsystems.calendar.client import CalendarClient
    return CalendarClient(account=account)


def _get_all_clients():
    """Return (clients, skipped_warnings) for every authorized Google account."""
    from subsystems.calendar.auth import list_authorized_accounts
    from subsystems.calendar.client import CalendarClient
    accounts = list_authorized_accounts()
    clients = []
    skipped: list[str] = []
    for acc in accounts:
        account_arg = None if acc == "default" else acc
        try:
            clients.append((acc, CalendarClient(account=account_arg)))
        except Exception as e:
            logger.warning("calendar_account_skipped", account=acc, error=str(e))
            err_str = str(e)
            if "invalid_grant" in err_str:
                skipped.append(f"{acc}: token expired — re-run setup_auth to reconnect")
            else:
                skipped.append(f"{acc}: {e}")
    return clients, skipped


def _calendar_label(cal: dict[str, Any]) -> str:
    """Return a human-friendly label for a calendar."""
    cal_id = cal.get("id", "")
    return _calendar_id_labels().get(cal_id, cal.get("summary", cal_id))


def _format_event(
    event: dict[str, Any],
    calendars: list[dict[str, Any]],
    *,
    timezone_name: str | None = None,
) -> str:
    start = event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date") or ""
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is not None:
                target_tz = ZoneInfo(timezone_name) if timezone_name else None
                dt = dt.astimezone(target_tz) if target_tz else dt.astimezone()
            time_str = dt.strftime("%-I:%M %p %Z").strip() or dt.strftime("%-I:%M %p")
            date_str = dt.strftime("%a %b %-d")
        else:
            date_str = dt_str
            time_str = "all day"
    except ValueError:
        date_str = dt_str
        time_str = ""

    summary = event.get("summary", "(no title)")
    cal_id = event.get("_calendar_id", "")
    _id_labels = _calendar_id_labels()
    cal_label = _id_labels.get(cal_id, "")
    cal_name = next(
        (c.get("summary", "") for c in calendars if c.get("id") == cal_id), cal_label
    )
    location = event.get("location", "")
    attendees = event.get("attendees", [])
    attendee_names = [
        a.get("displayName") or a.get("email", "") for a in attendees if not a.get("self")
    ]

    parts = [f"{date_str} {time_str} — {summary}"]
    if cal_name:
        parts.append(f"  Calendar: {cal_name}")
    if location:
        parts.append(f"  Location: {location}")
    if attendee_names:
        parts.append(f"  With: {', '.join(attendee_names[:5])}")
    return "\n".join(parts)


async def execute_get_upcoming_events(args: dict) -> dict:
    days = min(int(args.get("days", 7)), 90)
    cal_filter = (args.get("calendar_filter") or "").lower()
    timezone_name = args.get("timezone_name") or None
    exclude_allday = bool(args.get("exclude_allday", False))

    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        if not account_clients:
            msg = "No authorized Google accounts found. Run setup_auth.py first."
            if skipped:
                msg += " (" + "; ".join(skipped) + ")"
            return {"error": msg}

        all_events: list[dict[str, Any]] = []
        all_calendars: list[dict[str, Any]] = []

        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            # Tag each calendar with its account
            for cal in calendars:
                cal["_account"] = acc_name
            all_calendars.extend(calendars)

            if cal_filter:
                cal_ids = [
                    c["id"] for c in calendars
                    if cal_filter in c.get("summary", "").lower()
                    or cal_filter in _calendar_id_labels().get(c["id"], "").lower()
                    or cal_filter in acc_name.lower()
                ]
            else:
                cal_ids = None

            events = await asyncio.to_thread(client.list_upcoming_events, days, cal_ids)
            for e in events:
                e["_account"] = acc_name
            all_events.extend(events)

        all_events = _dedupe_events(all_events)
        if exclude_allday:
            all_events = [e for e in all_events if not _is_all_day(e)]
        all_events.sort(key=_event_sort_key)

        if not all_events:
            result: dict = {"events": [], "summary": f"No events in the next {days} days."}
            if skipped:
                result["warnings"] = skipped
            return result

        formatted = [
            _format_event(e, all_calendars, timezone_name=timezone_name) for e in all_events
        ]
        result = {
            "events": formatted,
            "count": len(all_events),
            "days": days,
            "summary": f"{len(all_events)} event(s) in the next {days} days.",
        }
        if skipped:
            result["warnings"] = skipped
        return result
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("calendar_fetch_failed", error=str(e))
        return {"error": f"Calendar fetch failed: {e}"}


async def execute_get_calendar_events_range(args: dict) -> dict:
    start_str = args.get("start_date", "")
    end_str = args.get("end_date", "")
    cal_filter = (args.get("calendar_filter") or "").lower()
    timezone_name = args.get("timezone_name") or None
    exclude_allday = bool(args.get("exclude_allday", False))

    if not start_str or not end_str:
        return {"error": "start_date and end_date are required."}

    try:
        # Parse dates — accept date-only (YYYY-MM-DD) or full datetime strings
        def _parse(s: str) -> datetime:
            s = s.strip()
            if len(s) == 10:  # date-only
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        start = _parse(start_str)
        end = _parse(end_str)
        # If end is date-only, include the full day
        if len(end_str.strip()) == 10:
            end = end.replace(hour=23, minute=59, second=59)
    except ValueError as e:
        return {"error": f"Invalid date format: {e}. Use ISO 8601 (e.g. '2024-10-01')."}

    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        if not account_clients:
            msg = "No authorized Google accounts found. Run setup_auth.py first."
            if skipped:
                msg += " (" + "; ".join(skipped) + ")"
            return {"error": msg}

        all_events: list[dict[str, Any]] = []
        all_calendars: list[dict[str, Any]] = []

        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            for cal in calendars:
                cal["_account"] = acc_name
            all_calendars.extend(calendars)

            if cal_filter:
                cal_ids = [
                    c["id"] for c in calendars
                    if cal_filter in c.get("summary", "").lower()
                    or cal_filter in _calendar_id_labels().get(c["id"], "").lower()
                    or cal_filter in acc_name.lower()
                ]
            else:
                cal_ids = None

            events = await asyncio.to_thread(client.list_events_range, start, end, cal_ids)
            for e in events:
                e["_account"] = acc_name
            all_events.extend(events)

        all_events = _dedupe_events(all_events)
        if exclude_allday:
            all_events = [e for e in all_events if not _is_all_day(e)]
        all_events.sort(key=_event_sort_key)

        if not all_events:
            result: dict = {
                "events": [],
                "summary": f"No events found between {start_str} and {end_str}.",
            }
            if skipped:
                result["warnings"] = skipped
            return result

        formatted = [
            _format_event(e, all_calendars, timezone_name=timezone_name) for e in all_events
        ]
        result = {
            "events": formatted,
            "count": len(all_events),
            "start_date": start_str,
            "end_date": end_str,
            "summary": f"{len(all_events)} event(s) between {start_str} and {end_str}.",
        }
        if skipped:
            result["warnings"] = skipped
        return result
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("calendar_range_fetch_failed", error=str(e))
        return {"error": f"Calendar range fetch failed: {e}"}


async def execute_list_calendars() -> dict:
    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        result = []
        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            for cal in calendars:
                entry = {
                    "name": cal.get("summary", ""),
                    "id": cal.get("id", ""),
                    "access": cal.get("accessRole", ""),
                    "primary": cal.get("primary", False),
                    "account": acc_name,
                }
                _id_labels = _calendar_id_labels()
                if cal["id"] in _id_labels:
                    entry["label"] = _id_labels[cal["id"]]
                result.append(entry)
        out: dict = {"calendars": result, "count": len(result)}
        if skipped:
            out["warnings"] = skipped
        return out
    except Exception as e:
        logger.error("list_calendars_failed", error=str(e))
        return {"error": f"Could not list calendars: {e}"}


async def execute_list_writable_calendars() -> dict:
    """Subset of list_calendars showing only owner/writer roles, for picking a target."""
    try:
        account_clients, skipped = await asyncio.to_thread(_get_all_clients)
        result: list[dict] = []
        for acc_name, client in account_clients:
            try:
                writable = await asyncio.to_thread(client.list_writable_calendars)
            except Exception as exc:
                logger.warning("writable_calendars_failed", account=acc_name, error=str(exc))
                skipped.append(f"{acc_name}: {exc}")
                continue
            for cal in writable:
                result.append({**cal, "account": acc_name})
        out: dict = {"calendars": result, "count": len(result)}
        if skipped:
            out["warnings"] = skipped
        return out
    except Exception as e:
        logger.error("list_writable_calendars_failed", error=str(e))
        return {"error": f"Could not list writable calendars: {e}"}


async def execute_create_calendar_event(args: dict, db_factory=None) -> dict:
    """Create a calendar event. Reachable only via PendingActionsQueue.approve."""
    account = (args.get("account") or "default").strip() or "default"
    calendar_id = (args.get("calendar_id") or "").strip()
    summary = (args.get("summary") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    time_zone = (args.get("time_zone") or "").strip() or None
    all_day = bool(args.get("all_day", False))
    description = args.get("description") or None
    location = args.get("location") or None
    attendees = args.get("attendees") or None
    send_updates = args.get("send_updates") or "none"

    if not calendar_id:
        return {"error": "create_calendar_event: 'calendar_id' is required"}
    if not summary or not start or not end:
        return {"error": "create_calendar_event: summary, start, end are required"}

    try:
        account_arg = None if account in ("default", "personal") else account
        from subsystems.calendar.client import CalendarClient
        client = CalendarClient(account=account_arg)
        created = await asyncio.to_thread(
            client.create_event,
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            time_zone=time_zone,
            description=description,
            location=location,
            attendees=attendees if isinstance(attendees, list) else None,
            all_day=all_day,
            send_updates=send_updates,
        )
        # Best-effort audit log
        if db_factory:
            try:
                from agent.models import AuditLog
                async with db_factory() as session:
                    session.add(AuditLog(
                        event_type="create_calendar_event",
                        details=(
                            f"account={account} calendar_id={calendar_id} "
                            f"summary={summary!r} start={start} end={end} "
                            f"id={created.get('id')} link={created.get('html_link')}"
                        )[:4000],
                    ))
                    await session.commit()
            except Exception as exc:
                logger.warning("calendar_create_audit_failed", error=str(exc))
        return {"ok": True, **created}
    except Exception as exc:
        logger.error("create_calendar_event_failed", account=account, calendar_id=calendar_id, error=str(exc))
        if db_factory:
            try:
                from agent.models import AuditLog
                async with db_factory() as session:
                    session.add(AuditLog(
                        event_type="create_calendar_event_failed",
                        details=f"account={account} calendar_id={calendar_id} error={exc}"[:4000],
                    ))
                    await session.commit()
            except Exception:
                pass
        return {"error": f"create_calendar_event failed: {exc}"}


def execute_draft_calendar_event(args: dict, *, pending_actions) -> dict:
    """Queue a calendar-event draft. Mirrors the send_tools draft pattern."""
    send_args = {
        "account": (args.get("account") or "default"),
        "calendar_id": args.get("calendar_id", ""),
        "summary": args.get("summary", ""),
        "start": args.get("start", ""),
        "end": args.get("end", ""),
        "time_zone": args.get("time_zone", ""),
        "all_day": bool(args.get("all_day", False)),
        "description": args.get("description", ""),
        "location": args.get("location", ""),
        "attendees": args.get("attendees", []) or [],
        "send_updates": args.get("send_updates", "none"),
    }
    action = pending_actions.queue("create_calendar_event", send_args, args.get("summary", ""))
    return {
        "ok": True,
        "queued": True,
        "channel": "calendar",
        "action_id": action.id,
        "preview": action.preview,
        "message": "Calendar event queued for your approval.",
    }


async def detect_calendar_conflicts(start_date: str, end_date: str) -> str:
    """Deterministically detect overlapping timed events and return a conflict report."""
    def _parse(s: str) -> datetime:
        s = s.strip()
        if len(s) == 10:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    try:
        account_clients, _ = await asyncio.to_thread(_get_all_clients)
        if not account_clients:
            return "No authorized Google accounts found."

        start = _parse(start_date)
        end = _parse(end_date)
        all_events: list[dict] = []
        all_calendars: list[dict] = []

        for acc_name, client in account_clients:
            calendars = await asyncio.to_thread(client.list_calendars)
            for cal in calendars:
                cal["_account"] = acc_name
            all_calendars.extend(calendars)
            events = await asyncio.to_thread(client.list_events_range, start, end)
            for e in events:
                e["_account"] = acc_name
            all_events.extend(events)

        _id_labels = _calendar_id_labels()
        seen_keys: set[tuple] = set()
        timed: list[dict] = []
        for e in all_events:
            dt_start = e.get("start", {}).get("dateTime")
            dt_end = e.get("end", {}).get("dateTime")
            if not dt_start or not dt_end:
                continue
            title = e.get("summary", "(no title)")
            start_dt = datetime.fromisoformat(dt_start)
            end_dt = datetime.fromisoformat(dt_end)
            # Deduplicate events synced across multiple calendar accounts
            dedup_key = (title.lower().strip(), start_dt.isoformat())
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            cal_id = e.get("_calendar_id", "")
            cal_name = next(
                (c.get("summary", "") for c in all_calendars if c.get("id") == cal_id),
                _id_labels.get(cal_id, ""),
            )
            timed.append({
                "title": title,
                "start": start_dt,
                "end": end_dt,
                "calendar": cal_name,
            })

        conflicts = [
            (timed[i], timed[j])
            for i in range(len(timed))
            for j in range(i + 1, len(timed))
            if timed[i]["start"] < timed[j]["end"] and timed[j]["start"] < timed[i]["end"]
            # Skip "Unavailable" blocking entries that are mirrors of other events
            and not (timed[i]["title"] in ("Unavailable", "Out of office")
                     or timed[j]["title"] in ("Unavailable", "Out of office"))
        ]

        if not conflicts:
            return "No scheduling conflicts detected on your calendar this week."

        local_tz = datetime.now().astimezone().tzinfo
        lines = [f"{len(conflicts)} scheduling conflict(s) this week:"]
        for a, b in conflicts:
            a_local = a["start"].astimezone(local_tz)
            b_local = b["start"].astimezone(local_tz)
            day = a_local.strftime("%a %b %-d")
            a_time = a_local.strftime("%-I:%M %p")
            b_time = b_local.strftime("%-I:%M %p")
            lines.append(
                f"- {day}: \"{a['title']}\" ({a_time}, {a['calendar']}) "
                f"overlaps \"{b['title']}\" ({b_time}, {b['calendar']})"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error("conflict_detection_failed", error=str(e))
        return f"Could not check for calendar conflicts: {e}"


async def _maybe_get_calendar_context(
    user_message: str,
    *,
    timezone_name: str | None,
) -> str:
    """Proactively inject upcoming events when the query is schedule-related."""
    if not is_source_query(user_message, CALENDAR_QUERY_TERMS, extra_terms=("today", "tomorrow", "this week", "next week", "coming up")):
        return ""

    # Determine look-ahead window from message
    days = infer_calendar_days(user_message, default=7)

    try:
        import re as _re
        normalized = user_message.lower()
        tz = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
        now_local = datetime.now(tz)
        not_today = bool(_re.search(r"\b(?:not|isn['’]?t|but not)\s+today\b", normalized))
        has_today = (("today" in normalized) or ("tonight" in normalized)) and not not_today
        has_tomorrow = "tomorrow" in normalized

        common_args = {"timezone_name": timezone_name} if timezone_name else {}

        if has_tomorrow and not has_today:
            start = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat(), **common_args}
            )
            heading = "Calendar events for tomorrow:"
        elif has_today and not has_tomorrow:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat(), **common_args}
            )
            heading = "Calendar events for today:"
        elif has_today and has_tomorrow:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=2) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat(), **common_args}
            )
            heading = "Calendar events for today and tomorrow:"
        else:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=days) - timedelta(seconds=1)
            result = await execute_get_calendar_events_range(
                {"start_date": start.isoformat(), "end_date": end.isoformat(), **common_args}
            )
            heading = f"Calendar events for the next {days} day(s), including today:"
        if "error" in result or not result.get("events"):
            return ""
        lines = [heading]
        lines.extend(result["events"])
        logger.debug("calendar_context_injected", count=result["count"])
        return "\n".join(lines)
    except Exception as e:
        logger.warning("calendar_proactive_failed", error=str(e))
        return ""


async def maybe_get_calendar_context(
    user_message: str,
    *,
    timezone_name: str | None = None,
) -> str:
    return await _maybe_get_calendar_context(
        user_message,
        timezone_name=timezone_name,
    )
