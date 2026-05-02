"""Unit tests for the success_signal heuristic (Phase 1 Task 5).

Pure-function tests for agent.success_signal plus integration tests for
PepperCore._process_success_signals that walk routing_events rows and
update the prior turn based on the current follow-up.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agent import success_signal
from agent.config import Settings
from agent.core import PepperCore
from agent.models import RoutingEvent


# ---------- pure functions ------------------------------------------------


def test_keyword_overlap_identical_queries_returns_one():
    assert success_signal.keyword_overlap("show my calendar", "show my calendar") == 1.0


def test_keyword_overlap_disjoint_queries_returns_zero():
    assert success_signal.keyword_overlap("calendar tomorrow", "weather forecast") == 0.0


def test_keyword_overlap_strips_stopwords():
    # "what is my" are all stopwords/short — only "calendar" / "schedule" count
    a = "what is my calendar"
    b = "what is my schedule"
    assert success_signal.keyword_overlap(a, b) == 0.0


def test_keyword_overlap_partial_match_jaccard():
    # tokens(a) = {calendar, tomorrow}; tokens(b) = {calendar, today}
    # inter=1, union=3 → 1/3 ≈ 0.333
    a = "calendar tomorrow"
    b = "calendar today"
    assert pytest.approx(success_signal.keyword_overlap(a, b), abs=1e-3) == 1 / 3


def test_keyword_overlap_empty_input_returns_zero():
    assert success_signal.keyword_overlap("", "anything") == 0.0
    assert success_signal.keyword_overlap("anything", "") == 0.0


def test_has_refusal_markers_detects_common_phrasings():
    assert success_signal.has_refusal_or_error_markers("I don't know who that is.")
    assert success_signal.has_refusal_or_error_markers("Sorry, no results.")
    assert success_signal.has_refusal_or_error_markers("Unable to fetch calendar.")
    assert success_signal.has_refusal_or_error_markers("Error: timeout")


def test_has_refusal_markers_returns_false_for_normal_response():
    assert not success_signal.has_refusal_or_error_markers(
        "You have 3 events: standup, lunch, and a 1:1 with Jane."
    )


def test_has_refusal_markers_handles_empty():
    assert not success_signal.has_refusal_or_error_markers("")


def test_derive_followup_re_asked_high_overlap_within_window():
    assert (
        success_signal.derive_followup_signal(
            "what events do I have tomorrow",
            "show me events tomorrow please",
            minutes_between=5,
        )
        == "re_asked"
    )


def test_derive_followup_confirmed_low_overlap_within_window():
    assert (
        success_signal.derive_followup_signal(
            "what events do I have tomorrow",
            "remind me to buy milk later",
            minutes_between=10,
        )
        == "confirmed"
    )


def test_derive_followup_ambiguous_returns_none():
    # tokens(prior)={calendar,events,tomorrow}
    # tokens(current)={calendar,events,brunch,lunch}
    # inter=2 union=5 → 0.40 — sits in the 0.30-0.50 ambiguous band
    result = success_signal.derive_followup_signal(
        "calendar events tomorrow",
        "calendar events brunch lunch",
        minutes_between=15,
    )
    assert result is None


def test_derive_followup_outside_window_returns_none():
    assert (
        success_signal.derive_followup_signal(
            "calendar", "calendar", minutes_between=45
        )
        is None
    )


def test_derive_terminal_abandoned_when_short_response_and_old():
    assert (
        success_signal.derive_terminal_signal(
            "ok.", minutes_since=120
        )
        == "abandoned"
    )


def test_derive_terminal_abandoned_when_refusal_and_old():
    long_refusal = "I'm sorry, I don't know how to answer that question right now."
    assert (
        success_signal.derive_terminal_signal(long_refusal, minutes_since=90)
        == "abandoned"
    )


def test_derive_terminal_unknown_when_normal_response_and_old():
    body = "Here's your morning brief: 3 meetings, 12 unread emails, and clear weather."
    assert success_signal.derive_terminal_signal(body, minutes_since=200) == "unknown"


def test_derive_terminal_returns_none_inside_window():
    assert success_signal.derive_terminal_signal("ok", minutes_since=30) is None
    assert success_signal.derive_terminal_signal("ok", minutes_since=60) is None


def test_derive_terminal_returns_none_when_response_missing():
    assert success_signal.derive_terminal_signal(None, minutes_since=120) is None


# ---------- _process_success_signals integration -------------------------


def _make_db_factory(rows: list[RoutingEvent]):
    """Mock factory that returns a session whose execute() yields ``rows``."""
    recorder = MagicMock()
    recorder.commits = 0

    async def commit():
        recorder.commits += 1

    scalars_obj = MagicMock()
    scalars_obj.all.return_value = rows
    result_obj = MagicMock()
    result_obj.scalars.return_value = scalars_obj

    async def execute(_stmt):
        return result_obj

    session = MagicMock()
    session.execute = execute
    session.commit = commit

    @asynccontextmanager
    async def factory():
        yield session

    return factory, recorder


@pytest.fixture
def pepper(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_CONTEXT_PATH", str(tmp_path / "life.md"))
    (tmp_path / "life.md").write_text("# Life\n")
    config = Settings()
    return PepperCore(config, db_session_factory=None)


@pytest.mark.asyncio
async def test_process_signals_marks_prior_turn_re_asked(pepper):
    now = datetime.now(timezone.utc)
    prior = RoutingEvent(
        query_text="what events do I have tomorrow",
        user_session_id="s1",
        timestamp=now - timedelta(minutes=5),
        success_signal=None,
    )
    factory, _ = _make_db_factory([prior])
    pepper.db_factory = factory  # type: ignore[assignment]
    await pepper._process_success_signals(
        session_id="s1",
        current_query="show events tomorrow please",
        current_response="Here you go.",
        current_timestamp=now,
    )
    assert prior.success_signal == "re_asked"
    assert prior.success_signal_set_at == now


@pytest.mark.asyncio
async def test_process_signals_marks_prior_turn_confirmed(pepper):
    now = datetime.now(timezone.utc)
    prior = RoutingEvent(
        query_text="what events do I have tomorrow",
        user_session_id="s1",
        timestamp=now - timedelta(minutes=10),
        success_signal=None,
    )
    factory, _ = _make_db_factory([prior])
    pepper.db_factory = factory  # type: ignore[assignment]
    await pepper._process_success_signals(
        session_id="s1",
        current_query="remind me to buy milk later",
        current_response="Reminder set.",
        current_timestamp=now,
    )
    assert prior.success_signal == "confirmed"


@pytest.mark.asyncio
async def test_process_signals_leaves_ambiguous_band_null(pepper):
    now = datetime.now(timezone.utc)
    prior = RoutingEvent(
        query_text="calendar tomorrow",
        user_session_id="s1",
        timestamp=now - timedelta(minutes=45),  # 30 < age < 60 → ambiguous
        success_signal=None,
    )
    factory, recorder = _make_db_factory([prior])
    pepper.db_factory = factory  # type: ignore[assignment]
    await pepper._process_success_signals(
        session_id="s1",
        current_query="anything else",
        current_response="ok",
        current_timestamp=now,
    )
    assert prior.success_signal is None
    assert recorder.commits == 0  # nothing changed → no commit


@pytest.mark.asyncio
async def test_process_signals_marks_abandoned_via_jsonl_lookup(
    pepper, monkeypatch
):
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(minutes=120)
    # Stub the JSONL lookup; the lookup helper itself has its own coverage.
    monkeypatch.setattr(
        PepperCore,
        "_lookup_jsonl_response",
        lambda self, **_: "ok.",  # short → triggers abandoned
    )

    prior = RoutingEvent(
        query_text="weather",
        user_session_id="s1",
        timestamp=old_ts,
        success_signal=None,
    )
    factory, _ = _make_db_factory([prior])
    pepper.db_factory = factory  # type: ignore[assignment]
    await pepper._process_success_signals(
        session_id="s1",
        current_query="something new entirely",
        current_response="reply",
        current_timestamp=now,
    )
    assert prior.success_signal == "abandoned"


@pytest.mark.asyncio
async def test_process_signals_marks_unknown_when_old_and_normal_response(pepper, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        PepperCore,
        "_lookup_jsonl_response",
        lambda self, **_: "Here is a perfectly normal informative response with several useful sentences for the user.",
    )
    prior = RoutingEvent(
        query_text="brief me",
        user_session_id="s1",
        timestamp=now - timedelta(minutes=180),
        success_signal=None,
    )
    factory, _ = _make_db_factory([prior])
    pepper.db_factory = factory  # type: ignore[assignment]
    await pepper._process_success_signals(
        session_id="s1",
        current_query="thanks",
        current_response="np",
        current_timestamp=now,
    )
    assert prior.success_signal == "unknown"


@pytest.mark.asyncio
async def test_process_signals_no_db_factory_is_noop(pepper):
    pepper.db_factory = None  # type: ignore[assignment]
    # Must not raise.
    await pepper._process_success_signals(
        session_id="s1",
        current_query="x",
        current_response="y",
        current_timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_process_signals_swallows_db_failure(pepper):
    @asynccontextmanager
    async def boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    pepper.db_factory = boom  # type: ignore[assignment]
    # Must not raise.
    await pepper._process_success_signals(
        session_id="s1",
        current_query="x",
        current_response="y",
        current_timestamp=datetime.now(timezone.utc),
    )


# ---------- _lookup_jsonl_response ---------------------------------------


def test_lookup_jsonl_response_finds_matching_row(pepper, tmp_path, monkeypatch):
    ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
    log_dir = tmp_path / "logs" / "chat_turns"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-04-27.jsonl").write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "session_id": "sX",
                "query": "calendar",
                "response": "you have 3 meetings",
            }
        )
        + "\n"
    )
    # Redirect the helper's repo_root resolution to tmp_path by patching
    # the __file__ attribute it derives from.
    monkeypatch.setattr("agent.core.__file__", str(tmp_path / "agent" / "core.py"))
    result = pepper._lookup_jsonl_response(
        session_id="sX",
        query="calendar",
        row_timestamp=ts,
    )
    assert result == "you have 3 meetings"


def test_lookup_jsonl_response_returns_none_when_no_match(pepper):
    # Helper degrades gracefully on cold cache (no matching JSONL row).
    ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
    result = pepper._lookup_jsonl_response(
        session_id="never-existed",
        query="never-asked",
        row_timestamp=ts,
    )
    assert result is None
