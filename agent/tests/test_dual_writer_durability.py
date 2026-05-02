"""Phase 1 Task 4 — dual-writer durability invariant.

The chat() finally-block must persist a JSONL row even when the
routing_events background writer fails. This is the design invariant
that lets ``agent.router_backfill`` reconcile missed DB rows from the
plaintext source of truth.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent import chat_turn_logger
from agent.config import Settings
from agent.core import PepperCore


@pytest.fixture
def pepper(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_CONTEXT_PATH", str(tmp_path / "life.md"))
    (tmp_path / "life.md").write_text("# Life\n")
    config = Settings()
    return PepperCore(config, db_session_factory=None)


def _read_today_jsonl(log_dir: Path) -> list[dict]:
    path = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_jsonl_row_lands_when_routing_event_writer_raises(
    monkeypatch, tmp_path, pepper
):
    """If _log_routing_event raises, the JSONL row must still persist.

    This is the load-bearing invariant Task 4 documents: file-based logs
    are the durable source of truth; DB failure does not lose data
    because backfill replays the JSONL.
    """
    log_dir = tmp_path / "chat_turns"
    monkeypatch.setattr(chat_turn_logger, "_DEFAULT_LOG_DIR", log_dir)

    monkeypatch.setattr(
        pepper,
        "_chat_impl",
        AsyncMock(return_value="ok response"),
    )

    async def boom(**_kwargs):
        raise RuntimeError("simulated DB writer crash")

    monkeypatch.setattr(pepper, "_log_routing_event", boom)

    response = await pepper.chat(
        user_message="hello pepper",
        session_id="sess-dual",
        channel="HTTP API",
    )
    # Allow any background task scheduled by chat() to settle.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert response == "ok response"
    rows = _read_today_jsonl(log_dir)
    assert len(rows) == 1
    assert rows[0]["query"] == "hello pepper"
    assert rows[0]["session_id"] == "sess-dual"
    assert rows[0]["response"] == "ok response"


@pytest.mark.asyncio
async def test_chat_impl_exception_still_writes_jsonl(
    monkeypatch, tmp_path, pepper
):
    """Even when the inner _chat_impl itself raises, finally still logs."""
    log_dir = tmp_path / "chat_turns"
    monkeypatch.setattr(chat_turn_logger, "_DEFAULT_LOG_DIR", log_dir)

    async def explode(*_a, **_kw):
        raise RuntimeError("model failed")

    monkeypatch.setattr(pepper, "_chat_impl", explode)

    with pytest.raises(RuntimeError, match="model failed"):
        await pepper.chat(
            user_message="will this be logged",
            session_id="sess-explode",
            channel="HTTP API",
        )

    rows = _read_today_jsonl(log_dir)
    assert len(rows) == 1
    assert rows[0]["query"] == "will this be logged"
    # Empty response on error — still recorded for the audit trail.
    assert rows[0]["response"] == ""
