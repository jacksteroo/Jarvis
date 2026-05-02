"""Unit tests for agent.router_logs (Phase 1 Task 6).

The DB is mocked: each test injects a fake async session whose ``execute``
returns a hand-rolled result with ``.all()`` so we exercise the query
shapes and result mapping without standing up Postgres. The live e2e run
is the integration check.
"""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.router_logs import (
    _format_divergence,
    _format_histogram,
    _format_neighbours,
    _HistogramRow,
    _NeighbourRow,
    _DivergenceRow,
    _parse_since,
    divergence,
    histogram_by_intent,
    histogram_by_success_signal,
    main,
    nearest_queries,
)


def _factory_returning(rows):
    """Build a db_factory whose session.execute returns ``rows`` from .all()."""
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


def test_parse_since_accepts_bare_date():
    got = _parse_since("2026-04-27")
    assert got == datetime(2026, 4, 27)


def test_parse_since_accepts_iso_with_z():
    got = _parse_since("2026-04-27T08:00:00Z")
    assert got == datetime(2026, 4, 27, 8, 0, 0, tzinfo=timezone.utc)


def test_parse_since_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_since("not-a-date")


# ---------------------------------------------------------------------------
# histogram_by_intent / by_success_signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_histogram_by_intent_maps_rows():
    factory, _ = _factory_returning([("calendar", 5), ("knowledge", 2)])
    rows = await histogram_by_intent(factory)
    assert rows == [
        _HistogramRow(bucket="calendar", count=5),
        _HistogramRow(bucket="knowledge", count=2),
    ]


@pytest.mark.asyncio
async def test_histogram_by_intent_passes_since_filter():
    factory, session = _factory_returning([])
    since = datetime(2026, 4, 1, tzinfo=timezone.utc)
    await histogram_by_intent(factory, since=since)
    stmt = session.execute.await_args.args[0]
    compiled = stmt.compile()
    # The since timestamp should appear among the compiled params.
    assert any(v == since for v in compiled.params.values())


@pytest.mark.asyncio
async def test_histogram_by_success_signal_maps_rows():
    factory, _ = _factory_returning([("unset", 100), ("confirmed", 12)])
    rows = await histogram_by_success_signal(factory)
    assert [r.bucket for r in rows] == ["unset", "confirmed"]
    assert [r.count for r in rows] == [100, 12]


# ---------------------------------------------------------------------------
# divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_divergence_maps_rows():
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    factory, _ = _factory_returning(
        [(ts, "what's on my calendar", "calendar", "knowledge", 0.8, 0.6)]
    )
    rows = await divergence(factory)
    assert len(rows) == 1
    assert rows[0].regex_intent == "calendar"
    assert rows[0].shadow_intent == "knowledge"
    assert rows[0].regex_confidence == 0.8


@pytest.mark.asyncio
async def test_divergence_respects_limit_and_since():
    factory, session = _factory_returning([])
    since = datetime(2026, 4, 1, tzinfo=timezone.utc)
    await divergence(factory, since=since, limit=42)
    stmt = session.execute.await_args.args[0]
    compiled = stmt.compile()
    # LIMIT lands as a bind parameter.
    assert 42 in compiled.params.values()
    assert any(v == since for v in compiled.params.values())


# ---------------------------------------------------------------------------
# nearest_queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nearest_queries_embeds_then_orders_by_distance():
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    factory, session = _factory_returning(
        [(ts, "what's on my calendar today", "calendar", 0.12)]
    )
    embed_fn = AsyncMock(return_value=[0.1] * 1024)

    rows = await nearest_queries(factory, embed_fn, "calendar today", k=3)

    embed_fn.assert_awaited_once_with("calendar today")
    assert len(rows) == 1
    assert rows[0].distance == pytest.approx(0.12)
    assert rows[0].regex_intent == "calendar"

    stmt = session.execute.await_args.args[0]
    compiled = stmt.compile()
    # k → LIMIT bind
    assert 3 in compiled.params.values()


@pytest.mark.asyncio
async def test_nearest_queries_propagates_embed_failure():
    factory, _ = _factory_returning([])
    embed_fn = AsyncMock(side_effect=RuntimeError("ollama down"))
    with pytest.raises(RuntimeError, match="ollama down"):
        await nearest_queries(factory, embed_fn, "x", k=1)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def test_format_histogram_empty():
    assert "(no rows)" in _format_histogram([], header="h:")


def test_format_histogram_renders_percentages():
    rows = [
        _HistogramRow(bucket="calendar", count=3),
        _HistogramRow(bucket="knowledge", count=1),
    ]
    out = _format_histogram(rows, header="counts:")
    assert "calendar" in out
    assert "75.0%" in out
    assert "TOTAL" in out
    assert "4" in out


def test_format_divergence_empty_hints_at_phase_2():
    out = _format_divergence([])
    assert "shadow" in out.lower()


def test_format_divergence_renders_row():
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    rows = [
        _DivergenceRow(
            timestamp=ts,
            query_text="some query",
            regex_intent="calendar",
            shadow_intent="knowledge",
            regex_confidence=0.8,
            shadow_confidence=None,
        )
    ]
    out = _format_divergence(rows)
    assert "calendar" in out and "knowledge" in out
    # None confidence renders as em dash placeholder
    assert "—" in out


def test_format_neighbours_empty():
    out = _format_neighbours([], query="hi")
    assert "no embedded rows" in out


def test_format_neighbours_renders_row():
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    rows = [
        _NeighbourRow(
            timestamp=ts,
            query_text="hello world",
            regex_intent="chat",
            distance=0.0123,
        )
    ]
    out = _format_neighbours(rows, query="hi")
    assert "0.0123" in out
    assert "hello world" in out


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_main_requires_a_mode():
    # argparse exits with SystemExit(2) when the required group is missing.
    with pytest.raises(SystemExit):
        main([])


def test_main_dispatches_histogram_by_intent(monkeypatch, capsys):
    factory, _ = _factory_returning([("calendar", 7)])

    async def fake_init(_settings):
        return None

    import agent.db as db_module

    monkeypatch.setattr(db_module, "init_db", fake_init)
    monkeypatch.setattr(db_module, "_session_factory", factory)

    rc = main(["--histogram-by-intent", "--json"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == [{"bucket": "calendar", "count": 7}]


def test_main_dispatches_query_text_through_embed(monkeypatch, capsys):
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    factory, _ = _factory_returning([(ts, "hello", "chat", 0.05)])

    async def fake_init(_settings):
        return None

    import agent.db as db_module
    import agent.llm as llm_module

    monkeypatch.setattr(db_module, "init_db", fake_init)
    monkeypatch.setattr(db_module, "_session_factory", factory)

    fake_client = MagicMock()
    fake_client.embed_router = AsyncMock(return_value=[0.0] * 1024)
    monkeypatch.setattr(llm_module, "ModelClient", lambda _settings: fake_client)

    rc = main(["--query", "hello there", "-k", "1", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["query_text"] == "hello"
    assert out[0]["distance"] == pytest.approx(0.05)
    fake_client.embed_router.assert_awaited_once_with("hello there")


def test_main_returns_2_when_session_factory_missing(monkeypatch, capsys):
    async def fake_init(_settings):
        return None

    import agent.db as db_module

    monkeypatch.setattr(db_module, "init_db", fake_init)
    monkeypatch.setattr(db_module, "_session_factory", None)

    rc = main(["--histogram-by-intent"])
    assert rc == 2
    assert "DB session factory missing" in capsys.readouterr().out
