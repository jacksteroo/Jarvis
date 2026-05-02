"""Tests for the calendar helper functions added for dedup, all-day filtering,
and timezone-aware event formatting."""
from __future__ import annotations

from agent.calendar_tools import (
    _dedupe_events,
    _format_event,
    _is_all_day,
)


def test_dedupe_events_collapses_cross_account_duplicates():
    events = [
        {
            "summary": "Validator Sync",
            "start": {"dateTime": "2026-04-29T18:00:00+00:00"},
            "end": {"dateTime": "2026-04-29T18:30:00+00:00"},
            "_account": "default",
            "_calendar_id": "primary",
        },
        {
            "summary": "Validator Sync",
            "start": {"dateTime": "2026-04-29T18:00:00+00:00"},
            "end": {"dateTime": "2026-04-29T18:30:00+00:00"},
            "_account": "work",
            "_calendar_id": "work-primary",
        },
        {
            "summary": "Validator Sync ",  # trailing whitespace, same event
            "start": {"dateTime": "2026-04-29T18:00:00+00:00"},
            "end": {"dateTime": "2026-04-29T18:30:00+00:00"},
            "_account": "shared",
            "_calendar_id": "shared",
        },
        {
            "summary": "Different Event",
            "start": {"dateTime": "2026-04-29T19:00:00+00:00"},
            "end": {"dateTime": "2026-04-29T19:30:00+00:00"},
        },
    ]
    out = _dedupe_events(events)
    titles = [e["summary"].strip() for e in out]
    assert titles == ["Validator Sync", "Different Event"]


def test_dedupe_events_keeps_distinct_times():
    """Same title at different times should NOT be deduped."""
    events = [
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-04-29T15:00:00+00:00"},
            "end": {"dateTime": "2026-04-29T15:15:00+00:00"},
        },
        {
            "summary": "Standup",
            "start": {"dateTime": "2026-04-30T15:00:00+00:00"},
            "end": {"dateTime": "2026-04-30T15:15:00+00:00"},
        },
    ]
    out = _dedupe_events(events)
    assert len(out) == 2


def test_is_all_day_detects_date_only_starts():
    timed = {"start": {"dateTime": "2026-04-29T15:00:00+00:00"}}
    allday = {"start": {"date": "2026-04-29"}}
    assert _is_all_day(allday) is True
    assert _is_all_day(timed) is False


def test_format_event_uses_provided_timezone():
    """An event with a UTC start should render in the requested IANA TZ."""
    event = {
        "summary": "Platform sync",
        "start": {"dateTime": "2026-04-28T16:30:00+00:00"},
        "end": {"dateTime": "2026-04-28T17:00:00+00:00"},
        "_calendar_id": "primary",
    }
    rendered = _format_event(event, calendars=[], timezone_name="America/Los_Angeles")
    # 16:30 UTC = 09:30 PDT
    assert "9:30 AM" in rendered
    assert "PDT" in rendered or "PST" in rendered
    assert "Platform sync" in rendered


def test_format_event_falls_back_when_timezone_missing():
    event = {
        "summary": "No-TZ event",
        "start": {"dateTime": "2026-04-28T16:30:00+00:00"},
    }
    # Should not raise even when timezone_name is None.
    rendered = _format_event(event, calendars=[])
    assert "No-TZ event" in rendered
