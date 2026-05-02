"""Unit tests for agent.router_backfill (Phase 1 Task 3).

Covers JSONL parsing, idempotency, embedding-failure tolerance, and the
regex-router replay path. The DB session is mocked — real INSERTs are
exercised by the live e2e run; here we assert kwargs handed to
``session.add`` and the dedup ``select`` short-circuit.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.models import RoutingEvent
from agent.router_backfill import BackfillResult, _parse_timestamp, backfill_files


def test_parse_timestamp_handles_iso_and_z_suffix():
    assert _parse_timestamp("2026-04-27T08:19:06.652222+00:00") == datetime(
        2026, 4, 27, 8, 19, 6, 652222, tzinfo=timezone.utc
    )
    assert _parse_timestamp("2026-04-27T08:19:06Z") == datetime(
        2026, 4, 27, 8, 19, 6, tzinfo=timezone.utc
    )
    assert _parse_timestamp("not-a-date") is None
    assert _parse_timestamp(None) is None
    assert _parse_timestamp(12345) is None


def _make_factory(*, existing_keys: set[tuple] | None = None):
    """Mock async DB session.

    ``existing_keys`` — tuples of ``(query_text, session_id, timestamp_iso)``
    that the dedup ``select`` should treat as already present.
    """
    existing_keys = existing_keys or set()
    recorder = MagicMock()
    recorder.added = []
    recorder.commit_count = 0

    async def execute(stmt):
        # Recover the WHERE values without depending on SQLA internals: we
        # encoded them in the recorder's last "intent" via _row_already_present's
        # passed args. Simpler — peek at compiled params.
        compiled = stmt.compile()
        params = compiled.params
        key = (
            params.get("query_text_1"),
            params.get("user_session_id_1"),
            params.get("timestamp_1"),
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = 1 if key in existing_keys else None
        return result

    session = MagicMock()
    session.add = lambda obj: recorder.added.append(obj)
    session.execute = AsyncMock(side_effect=execute)

    async def _commit():
        recorder.commit_count += 1

    session.commit = AsyncMock(side_effect=_commit)

    @asynccontextmanager
    async def factory():
        yield session

    return factory, recorder


def _write_jsonl(tmp_path, name, rows):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


@pytest.mark.asyncio
async def test_backfill_inserts_rows_with_router_replay(tmp_path):
    rows = [
        {
            "timestamp": "2026-04-27T08:19:06.652222+00:00",
            "session_id": "sess-A",
            "channel": "HTTP API",
            "query": "what's on my calendar this week",
            "response": "...",
            "tool_calls": [{"name": "search_calendar", "arguments": {"days": 7}}],
            "latency_ms": 812,
            "model": "local/hermes-4.3-36b-tools:latest",
        },
        {
            "timestamp": "2026-04-27T08:20:00+00:00",
            "session_id": "sess-A",
            "query": "hello",
            "response": "hi",
            "tool_calls": [],
            "latency_ms": 11,
            "model": "local/hermes",
        },
    ]
    path = _write_jsonl(tmp_path, "2026-04-27.jsonl", rows)
    factory, rec = _make_factory()
    embed = AsyncMock(return_value=[0.1] * 1024)

    result = await backfill_files([path], db_factory=factory, embed_fn=embed)

    assert result.scanned == 2
    assert result.inserted == 2
    assert result.skipped_duplicate == 0
    assert result.skipped_invalid == 0
    assert rec.commit_count == 2
    assert all(isinstance(r, RoutingEvent) for r in rec.added)
    first = rec.added[0]
    assert first.query_text == "what's on my calendar this week"
    assert first.timestamp == datetime(2026, 4, 27, 8, 19, 6, 652222, tzinfo=timezone.utc)
    assert first.user_session_id == "sess-A"
    assert first.regex_decision_intent  # populated by QueryRouter.route
    assert first.regex_decision_confidence is not None
    assert first.tools_actually_called == [
        {"name": "search_calendar", "arguments": {"days": 7}}
    ]
    assert first.query_embedding == [0.1] * 1024
    assert first.llm_model == "local/hermes-4.3-36b-tools:latest"
    assert first.latency_ms == 812


@pytest.mark.asyncio
async def test_backfill_skips_duplicate_rows(tmp_path):
    ts = "2026-04-27T08:19:06.652222+00:00"
    rows = [{"timestamp": ts, "session_id": "s", "query": "q", "tool_calls": [], "latency_ms": 5, "model": "m"}]
    path = _write_jsonl(tmp_path, "d.jsonl", rows)

    parsed_ts = datetime.fromisoformat(ts)
    factory, rec = _make_factory(existing_keys={("q", "s", parsed_ts)})
    embed = AsyncMock()  # must not be called when dedup hits

    result = await backfill_files([path], db_factory=factory, embed_fn=embed)

    assert result.inserted == 0
    assert result.skipped_duplicate == 1
    assert rec.commit_count == 0
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_tolerates_embed_failure(tmp_path):
    rows = [
        {
            "timestamp": "2026-04-27T08:19:06+00:00",
            "session_id": "s",
            "query": "hi",
            "tool_calls": [],
            "latency_ms": 1,
            "model": "m",
        }
    ]
    path = _write_jsonl(tmp_path, "e.jsonl", rows)
    factory, rec = _make_factory()
    embed = AsyncMock(side_effect=RuntimeError("ollama down"))

    result = await backfill_files([path], db_factory=factory, embed_fn=embed)

    assert result.inserted == 1
    assert result.embed_failures == 1
    row = rec.added[0]
    assert row.query_embedding is None
    assert row.query_text == "hi"


@pytest.mark.asyncio
async def test_backfill_skips_invalid_rows(tmp_path):
    rows = [
        {"timestamp": "not-a-date", "query": "x", "session_id": "s"},
        {"timestamp": "2026-04-27T00:00:00+00:00", "query": "", "session_id": "s"},
        {"timestamp": "2026-04-27T00:00:00+00:00", "session_id": "s"},  # no query
        {"timestamp": "2026-04-27T00:00:00+00:00", "query": "ok", "session_id": "s",
         "tool_calls": [], "latency_ms": 1, "model": "m"},
    ]
    path = _write_jsonl(tmp_path, "i.jsonl", rows)
    # Also write a malformed line directly:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")

    factory, rec = _make_factory()
    embed = AsyncMock(return_value=[0.0] * 1024)

    result = await backfill_files([path], db_factory=factory, embed_fn=embed)

    assert result.scanned == 4  # bad json line skipped before counting
    assert result.skipped_invalid == 3
    assert result.inserted == 1
    assert rec.added[0].query_text == "ok"


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_commit(tmp_path):
    rows = [{"timestamp": "2026-04-27T00:00:00+00:00", "session_id": "s", "query": "q",
             "tool_calls": [], "latency_ms": 1, "model": "m"}]
    path = _write_jsonl(tmp_path, "dr.jsonl", rows)
    factory, rec = _make_factory()
    embed = AsyncMock(return_value=[0.0] * 1024)

    result = await backfill_files(
        [path], db_factory=factory, embed_fn=embed, dry_run=True
    )

    assert result.inserted == 1
    assert rec.added == []
    assert rec.commit_count == 0


@pytest.mark.asyncio
async def test_backfill_handles_missing_file(tmp_path):
    factory, rec = _make_factory()
    embed = AsyncMock()
    result = await backfill_files(
        [tmp_path / "missing.jsonl"], db_factory=factory, embed_fn=embed
    )
    assert isinstance(result, BackfillResult)
    assert result.scanned == 0
    assert result.inserted == 0
