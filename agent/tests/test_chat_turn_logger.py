"""Unit tests for the per-turn JSONL chat logger (Phase 0 Task 2)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from agent import chat_turn_logger


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    return tmp_path / "chat_turns"


def _read_today_jsonl(tmp_log_dir: Path) -> list[dict]:
    path = tmp_log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writes_minimal_row_with_no_llm_call(tmp_log_dir: Path) -> None:
    chat_turn_logger.start_turn()
    chat_turn_logger.write_turn(
        query="who am i",
        response="You are Jack.",
        latency_ms=12,
        session_id="sess-1",
        channel="Telegram",
        log_dir=tmp_log_dir,
    )
    rows = _read_today_jsonl(tmp_log_dir)
    assert len(rows) == 1
    row = rows[0]
    assert row["query"] == "who am i"
    assert row["response"] == "You are Jack."
    assert row["latency_ms"] == 12
    assert row["session_id"] == "sess-1"
    assert row["channel"] == "Telegram"
    assert row["model"] is None
    assert row["tool_calls"] == []
    assert "timestamp" in row


def test_record_llm_populates_model_and_tools(tmp_log_dir: Path) -> None:
    chat_turn_logger.start_turn()
    chat_turn_logger.record_llm(
        model="local/hermes3",
        tool_calls=[
            {"function": {"name": "search_calendar", "arguments": {"days": 7}}},
            {"function": {"name": "lookup_contact", "arguments": {"name": "Jackie"}}},
        ],
    )
    chat_turn_logger.write_turn(
        query="meetings this week",
        response="You have 3.",
        latency_ms=420,
        session_id="sess-2",
        channel="HTTP API",
        log_dir=tmp_log_dir,
    )
    row = _read_today_jsonl(tmp_log_dir)[0]
    assert row["model"] == "local/hermes3"
    assert [c["name"] for c in row["tool_calls"]] == ["search_calendar", "lookup_contact"]
    assert row["tool_calls"][0]["arguments"] == {"days": 7}


def test_record_llm_truncates_long_string_args(tmp_log_dir: Path) -> None:
    chat_turn_logger.start_turn()
    long_text = "x" * 1200
    chat_turn_logger.record_llm(
        model="local/hermes3",
        tool_calls=[{"function": {"name": "web_search", "arguments": {"q": long_text}}}],
    )
    chat_turn_logger.write_turn(
        query="search the web",
        response="results",
        latency_ms=1,
        session_id="sess-3",
        channel="",
        log_dir=tmp_log_dir,
    )
    row = _read_today_jsonl(tmp_log_dir)[0]
    truncated = row["tool_calls"][0]["arguments"]["q"]
    assert truncated.endswith("…")
    assert len(truncated) < len(long_text)


def test_concurrent_turns_isolated_via_contextvar(tmp_log_dir: Path) -> None:
    """Two interleaved async turns must not cross-contaminate model/tool fields."""

    async def turn(model: str, tool: str, session: str) -> None:
        chat_turn_logger.start_turn()
        await asyncio.sleep(0)
        chat_turn_logger.record_llm(
            model=model,
            tool_calls=[{"function": {"name": tool, "arguments": {}}}],
        )
        await asyncio.sleep(0)
        chat_turn_logger.write_turn(
            query=f"q-{session}",
            response="ok",
            latency_ms=1,
            session_id=session,
            channel="",
            log_dir=tmp_log_dir,
        )

    async def main() -> None:
        await asyncio.gather(
            turn("local/hermes3", "tool_a", "s-A"),
            turn("local/qwen", "tool_b", "s-B"),
        )

    asyncio.run(main())
    rows = {r["session_id"]: r for r in _read_today_jsonl(tmp_log_dir)}
    assert rows["s-A"]["model"] == "local/hermes3"
    assert rows["s-A"]["tool_calls"][0]["name"] == "tool_a"
    assert rows["s-B"]["model"] == "local/qwen"
    assert rows["s-B"]["tool_calls"][0]["name"] == "tool_b"


def test_write_failure_swallowed(tmp_path: Path) -> None:
    """Logger never raises even if the directory is unwritable."""
    bad_dir = tmp_path / "nope"
    bad_dir.write_text("not a dir")
    chat_turn_logger.start_turn()
    chat_turn_logger.write_turn(
        query="x",
        response="y",
        latency_ms=0,
        session_id="s",
        channel="",
        log_dir=bad_dir,
    )


def test_record_llm_without_active_turn_is_noop() -> None:
    chat_turn_logger._CURRENT_TRACE.set(None)
    chat_turn_logger.record_llm(model="local/hermes3", tool_calls=[])


def test_record_routing_populates_trace() -> None:
    chat_turn_logger.start_turn()
    chat_turn_logger.record_routing(
        intent="email_summary",
        sources=["email"],
        confidence=0.92,
    )
    trace = chat_turn_logger.get_trace()
    assert trace is not None
    assert trace["routing"] == {
        "intent": "email_summary",
        "sources": ["email"],
        "confidence": 0.92,
    }


def test_record_routing_without_active_turn_is_noop() -> None:
    chat_turn_logger._CURRENT_TRACE.set(None)
    chat_turn_logger.record_routing(intent="x", sources=[], confidence=1.0)
    assert chat_turn_logger.get_trace() is None


def test_record_routing_handles_none_sources() -> None:
    chat_turn_logger.start_turn()
    chat_turn_logger.record_routing(intent="general_chat", sources=None, confidence=0.6)
    trace = chat_turn_logger.get_trace()
    assert trace is not None
    assert trace["routing"]["sources"] is None


def test_skips_tool_calls_with_no_name(tmp_log_dir: Path) -> None:
    chat_turn_logger.start_turn()
    chat_turn_logger.record_llm(
        model="local/hermes3",
        tool_calls=[
            {"function": {"name": "", "arguments": {}}},
            {"function": {"name": "good_tool", "arguments": {}}},
            {},
        ],
    )
    chat_turn_logger.write_turn(
        query="x", response="y", latency_ms=0,
        session_id="s", channel="", log_dir=tmp_log_dir,
    )
    row = _read_today_jsonl(tmp_log_dir)[0]
    assert [c["name"] for c in row["tool_calls"]] == ["good_tool"]
