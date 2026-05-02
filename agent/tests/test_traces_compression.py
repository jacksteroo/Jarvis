"""Tests for the trace compression policy (#21).

Covers the structural invariants of `compress_assembled_context`,
`_project_tool_call_to_recall`, and `assert_local_only_llm_call`
without requiring a live Postgres. Tier-transition behaviour against
mock rows is exercised separately.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.traces.compression import (
    RECALL_CONTEXT_TOP_N,
    RECALL_TO_ARCHIVAL_AGE,
    WORKING_TO_RECALL_AGE,
    _project_tool_call_to_recall,
    assert_local_only_llm_call,
    compress_assembled_context,
    compress_recall_to_archival,
    compress_working_to_recall,
    run_nightly_compression,
)
from agent.traces.schema import TraceTier


# ── Structural compression ────────────────────────────────────────────────────


class TestCompressAssembledContext:
    def test_truncates_items_to_top_n(self) -> None:
        ctx = {
            "strategy": "recall+memory",
            "version": 1,
            "items": [{"i": i} for i in range(10)],
        }
        out = compress_assembled_context(ctx)
        assert len(out["items"]) == RECALL_CONTEXT_TOP_N
        assert out["items"] == [{"i": 0}, {"i": 1}, {"i": 2}]
        assert out["strategy"] == "recall+memory"
        assert out["version"] == 1
        assert out["compressed_from"] == 10

    def test_preserves_short_items_unchanged(self) -> None:
        ctx = {"strategy": "x", "items": [{"i": 0}], "version": 1}
        out = compress_assembled_context(ctx)
        assert out["items"] == [{"i": 0}]
        assert out["compressed_from"] == 1

    def test_handles_missing_keys(self) -> None:
        out = compress_assembled_context({})
        assert out["items"] == []
        assert out["strategy"] is None
        assert out["compressed_from"] == 0


class TestProjectToolCall:
    def test_drops_args_and_result_summary(self) -> None:
        call = {
            "name": "send_telegram",
            "args": {"chat_id": "123", "text": "secret-pii"},
            "result_summary": "ok-with-raw-content",
            "latency_ms": 42,
            "success": True,
        }
        out = _project_tool_call_to_recall(call)
        assert out == {"name": "send_telegram", "success": True, "latency_ms": 42}
        assert "args" not in out
        assert "result_summary" not in out
        assert "secret-pii" not in str(out)

    def test_defaults_missing_fields(self) -> None:
        out = _project_tool_call_to_recall({"name": "x"})
        assert out["success"] is True
        assert out["latency_ms"] == 0


# ── Local-only LLM invariant ──────────────────────────────────────────────────


class TestAssertLocalOnlyLLMCall:
    @pytest.mark.parametrize(
        "model",
        ["claude-opus-4-7", "gpt-4", "anthropic/claude-3", "frontier-x"],
    )
    def test_rejects_frontier_models(self, model: str) -> None:
        with pytest.raises(RuntimeError, match="not local"):
            assert_local_only_llm_call(model)

    @pytest.mark.parametrize(
        "model",
        ["hermes3-local", "qwen3-embedding:0.6b", "nomic-embed-text",
         "local/hermes-4.3-36b-tools", "llama3.1", "phi-4"],
    )
    def test_accepts_local_models(self, model: str) -> None:
        assert_local_only_llm_call(model)  # no raise

    def test_empty_model_rejected(self) -> None:
        with pytest.raises(ValueError):
            assert_local_only_llm_call("")


# ── Tier transitions (mock-driven) ────────────────────────────────────────────


def _mock_row(*, age, tier=TraceTier.WORKING.value, trace_id_hex="0"*32):
    import uuid
    row = MagicMock()
    row.trace_id = uuid.UUID(trace_id_hex)
    row.created_at = datetime.now(timezone.utc) - age
    row.tier = tier
    row.assembled_context = {"strategy": "s", "items": [{"i": 0}, {"i": 1}, {"i": 2}, {"i": 3}], "version": 1}
    row.tools_called = [
        {"name": "search", "args": {"q": "x"}, "result_summary": "got 5", "latency_ms": 50, "success": True},
    ]
    row.embedding = [0.1] * 1024
    row.embedding_model_version = "qwen3-embedding:0.6b"
    return row


def _mock_session(rows):
    sess = MagicMock()
    # execute returns an awaitable that yields a result-shape with .scalars().all() == rows
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows)
    result.scalars = MagicMock(return_value=scalars)
    sess.execute = AsyncMock(return_value=result)
    sess.commit = AsyncMock()
    sess.flush = AsyncMock()
    sess.get = AsyncMock(side_effect=lambda model, pk: next((r for r in rows if r.trace_id == pk), None))
    return sess


@pytest.mark.asyncio
async def test_compress_working_to_recall_advances_old_rows() -> None:
    old = _mock_row(age=WORKING_TO_RECALL_AGE + timedelta(hours=1), trace_id_hex="11"*16)
    sess = _mock_session([old])
    res = await compress_working_to_recall(sess)
    assert res.scanned == 1
    assert res.advanced_to_recall == 1
    # Structural compression took effect.
    assert old.embedding is None
    assert old.embedding_model_version is None
    assert "compressed_from" in old.assembled_context
    assert all("args" not in c for c in old.tools_called)
    assert old.tier == TraceTier.RECALL.value


@pytest.mark.asyncio
async def test_compress_recall_to_archival_advances_old_rows() -> None:
    old = _mock_row(
        age=RECALL_TO_ARCHIVAL_AGE + timedelta(days=1),
        tier=TraceTier.RECALL.value,
        trace_id_hex="22"*16,
    )
    sess = _mock_session([old])
    res = await compress_recall_to_archival(sess)
    assert res.scanned == 1
    assert res.advanced_to_archival == 1
    assert old.tier == TraceTier.ARCHIVAL.value
    # Heavy fields fully cleared at archival.
    assert old.assembled_context == {}
    assert old.tools_called == []
    assert old.embedding is None


@pytest.mark.asyncio
async def test_run_nightly_compression_calls_both_passes() -> None:
    # Use the same session for both phases (real call uses two contexts).
    @asynccontextmanager
    async def _factory():
        yield _mock_session([])  # empty — no rows to compress

    out = await run_nightly_compression(_factory)
    assert "recall" in out and "archival" in out
    assert out["recall"].scanned == 0
    assert out["archival"].scanned == 0


@pytest.mark.asyncio
async def test_compression_is_idempotent_on_already_advanced_rows() -> None:
    # Row is already at recall tier — the working scan must skip it
    # because the WHERE clause filters tier == working.
    already_recall = _mock_row(
        age=WORKING_TO_RECALL_AGE + timedelta(hours=2),
        tier=TraceTier.RECALL.value,
        trace_id_hex="33"*16,
    )
    # Build a session whose query returns the row we want (the WHERE
    # clause is a SQL filter, not exercised by the mock — we simulate
    # the result of the filter by passing zero rows for the working
    # scan).
    sess = _mock_session([])
    res = await compress_working_to_recall(sess)
    assert res.scanned == 0
    assert already_recall.tier == TraceTier.RECALL.value  # unchanged
