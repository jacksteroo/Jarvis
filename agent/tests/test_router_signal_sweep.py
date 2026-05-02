"""Unit tests for agent.router_signal_sweep.

The sweep walks all sessions with un-graded routing_events rows and grades
them with the same heuristic the live ``chat()`` path uses. Tested against
in-memory ``RoutingEvent`` objects so the heuristic logic is exercised
without needing Postgres.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from agent import router_signal_sweep
from agent.models import RoutingEvent


def _make_factory(rows: list[RoutingEvent]):
    """Mock async session factory backed by an in-memory row list.

    Mimics the SELECT shapes the sweep uses:
    - distinct user_session_id where success_signal IS NULL
    - per-session select * ordered by timestamp asc
    """
    commit_count = {"n": 0}

    def execute_for(stmt):
        compiled = str(stmt).lower()
        if "group by" in compiled and "user_session_id" in compiled:
            sids = sorted(
                {
                    r.user_session_id
                    for r in rows
                    if r.success_signal is None and r.user_session_id is not None
                }
            )
            class _R:
                def all(self_):
                    return [(s,) for s in sids]
            return _R()
        # Per-session full-row select. We don't try to parse the WHERE; the
        # sweep calls one execute per session, and we infer which session by
        # looking at compiled bind params via the statement's compile().
        from sqlalchemy.dialects import postgresql
        compiled_obj = stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        sql = str(compiled_obj).lower()
        target_sid = None
        for r in rows:
            if r.user_session_id and f"'{r.user_session_id.lower()}'" in sql:
                target_sid = r.user_session_id
                break
        session_rows = sorted(
            (r for r in rows if r.user_session_id == target_sid),
            key=lambda r: r.timestamp,
        )

        class _Scalars:
            def all(self_):
                return list(session_rows)

        class _R:
            def scalars(self_):
                return _Scalars()
        return _R()

    class _Session:
        async def execute(self_, stmt):
            return execute_for(stmt)

        async def commit(self_):
            commit_count["n"] += 1

    @asynccontextmanager
    async def factory():
        yield _Session()

    return factory, commit_count


@pytest.mark.asyncio
async def test_sweep_grades_followup_pair_within_window():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="what events do I have tomorrow",
            timestamp=now - timedelta(minutes=10),
            success_signal=None,
        ),
        RoutingEvent(
            id=2,
            user_session_id="s1",
            query_text="show events tomorrow please",
            timestamp=now - timedelta(minutes=8),
            success_signal=None,
        ),
    ]
    factory, commits = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: None
    )
    # Row 1 follow-up overlap is high → re_asked. Row 2 is terminal but
    # less than 60 min old → skipped (signal is None).
    assert rows[0].success_signal == "re_asked"
    assert rows[1].success_signal is None
    assert res.re_asked == 1
    assert res.confirmed == 0
    assert commits["n"] == 1


@pytest.mark.asyncio
async def test_sweep_grades_followup_confirmed_when_topic_changes():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="what events do I have tomorrow",
            timestamp=now - timedelta(minutes=200),
            success_signal=None,
        ),
        RoutingEvent(
            id=2,
            user_session_id="s1",
            query_text="remind me to buy milk later",
            timestamp=now - timedelta(minutes=195),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: "Here is a fully formed informative response containing several useful sentences for the user.",
    )
    assert rows[0].success_signal == "confirmed"
    # Row 2 is terminal, > 60min old, and we provided a non-short response →
    # unknown.
    assert rows[1].success_signal == "unknown"
    assert res.confirmed == 1
    assert res.unknown == 1


@pytest.mark.asyncio
async def test_sweep_marks_terminal_abandoned_via_jsonl_short_response():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="weather",
            timestamp=now - timedelta(minutes=180),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: "ok."
    )
    assert rows[0].success_signal == "abandoned"
    assert res.abandoned == 1


@pytest.mark.asyncio
async def test_sweep_skips_ambiguous_30_to_60min_gap():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="calendar tomorrow",
            timestamp=now - timedelta(minutes=200),
            success_signal=None,
        ),
        RoutingEvent(
            id=2,
            user_session_id="s1",
            query_text="anything else",
            # 45-min gap to the prior row → ambiguous (>30, <=60), skipped.
            timestamp=now - timedelta(minutes=155),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: "Long enough informative response body here for unknown classification thanks."
    )
    # Row 1: gap 45m → ambiguous → None (sweep skips). Row 2 is terminal,
    # >60min old → graded.
    assert rows[0].success_signal is None
    assert rows[1].success_signal == "unknown"


@pytest.mark.asyncio
async def test_sweep_skips_terminal_when_response_lookup_fails():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="weather",
            timestamp=now - timedelta(minutes=180),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: None
    )
    assert rows[0].success_signal is None
    assert res.skipped_no_response == 1


@pytest.mark.asyncio
async def test_sweep_skips_recent_terminal_inside_window():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="s1",
            query_text="calendar",
            timestamp=now - timedelta(minutes=20),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: "ok."
    )
    assert rows[0].success_signal is None
    assert res.rows_seen == 1


@pytest.mark.asyncio
async def test_sweep_handles_multiple_sessions_independently():
    now = datetime(2026, 4, 27, 19, 0, tzinfo=timezone.utc)
    rows = [
        RoutingEvent(
            id=1,
            user_session_id="sA",
            query_text="weather",
            timestamp=now - timedelta(minutes=180),
            success_signal=None,
        ),
        RoutingEvent(
            id=2,
            user_session_id="sB",
            query_text="schedule",
            timestamp=now - timedelta(minutes=180),
            success_signal=None,
        ),
    ]
    factory, _ = _make_factory(rows)
    res = await router_signal_sweep.sweep_all_sessions(
        factory, now=now, jsonl_lookup=lambda *_: "ok."
    )
    assert rows[0].success_signal == "abandoned"
    assert rows[1].success_signal == "abandoned"
    assert res.sessions == 2


def test_default_jsonl_lookup_returns_none_when_no_logs(tmp_path, monkeypatch):
    # Redirect repo_root by monkeypatching __file__ → uses tmp_path with no logs/.
    fake_module_dir = tmp_path / "agent"
    fake_module_dir.mkdir()
    monkeypatch.setattr(
        router_signal_sweep,
        "__file__",
        str(fake_module_dir / "router_signal_sweep.py"),
    )
    out = router_signal_sweep.default_jsonl_lookup(
        "s1", "weather", datetime(2026, 4, 27, tzinfo=timezone.utc)
    )
    assert out is None


def test_default_jsonl_lookup_finds_matching_response(tmp_path, monkeypatch):
    # Build a fake repo layout: <tmp>/agent/ + <tmp>/logs/chat_turns/<date>.jsonl
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    log_dir = tmp_path / "logs" / "chat_turns"
    log_dir.mkdir(parents=True)
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    (log_dir / "2026-04-27.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "query": "weather",
                "timestamp": ts.isoformat(),
                "response": "Sunny and 72.",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        router_signal_sweep,
        "__file__",
        str(agent_dir / "router_signal_sweep.py"),
    )
    out = router_signal_sweep.default_jsonl_lookup("s1", "weather", ts)
    assert out == "Sunny and 72."
