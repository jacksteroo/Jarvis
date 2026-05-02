"""Unit tests for agent.router_adjudication (Phase 2).

Covers the pure logic: stratified sampling, message formatting,
JSONL artifact write, and the SELECT-shape contract via a mocked
DB factory. Live Telegram push is exercised manually (live e2e
section in the iteration notes).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.router_adjudication import (
    AdjudicationCase,
    AdjudicationVerdict,
    GATE2_REGEX_MAX,
    GATE2_SEMANTIC_MIN,
    evaluate_gate2,
    fetch_divergence_rows,
    format_batch_messages,
    load_sample_artifact,
    parse_reply_text,
    stratified_sample,
    write_gate2_result,
    write_sample_artifact,
)


def _case(
    event_id: int,
    query: str = "q",
    regex: str | None = "regex_a",
    shadow: str | None = "shadow_b",
    rc: float | None = 1.0,
    sc: float | None = 0.6,
) -> AdjudicationCase:
    return AdjudicationCase(
        event_id=event_id,
        timestamp=datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
        query_text=query,
        regex_intent=regex,
        shadow_intent=shadow,
        regex_confidence=rc,
        shadow_confidence=sc,
    )


def test_stratified_sample_respects_n_when_pool_is_smaller():
    cases = [_case(i) for i in range(5)]
    out = stratified_sample(cases, n=50)
    assert len(out) == 5
    assert [c.event_id for c in out] == [0, 1, 2, 3, 4]


def test_stratified_sample_returns_empty_when_n_zero():
    assert stratified_sample([_case(1)], n=0) == []


def test_stratified_sample_returns_empty_when_pool_empty():
    assert stratified_sample([], n=10) == []


def test_stratified_sample_covers_every_pair_when_room_allows():
    pool: list[AdjudicationCase] = []
    pairs = [
        ("schedule_lookup", "unknown"),
        ("conversation_lookup", "unknown"),
        ("cross_source_triage", "inbox_summary"),
        ("action_items", "inbox_summary"),
    ]
    next_id = 1
    for regex, shadow in pairs:
        for _ in range(10):
            pool.append(_case(next_id, regex=regex, shadow=shadow))
            next_id += 1

    out = stratified_sample(pool, n=12)
    assert len(out) == 12
    seen_pairs = {(c.regex_intent, c.shadow_intent) for c in out}
    assert seen_pairs == set(pairs)
    # Result is sorted by event_id for stable display ordering.
    assert [c.event_id for c in out] == sorted(c.event_id for c in out)


def test_stratified_sample_caps_to_pool_size_per_pair():
    # One pair has 100 rows, three pairs have 1 row each.
    pool = [
        _case(i, regex="big", shadow="x") for i in range(1, 101)
    ] + [
        _case(101, regex="rare1", shadow="x"),
        _case(102, regex="rare2", shadow="x"),
        _case(103, regex="rare3", shadow="x"),
    ]
    out = stratified_sample(pool, n=10)
    assert len(out) == 10
    # Each rare pair contributes its one row; big pair fills the rest.
    rare_ids = {101, 102, 103}
    chosen_ids = {c.event_id for c in out}
    assert rare_ids.issubset(chosen_ids)


def test_stratified_sample_caps_at_n_when_more_pairs_than_slots():
    # 24 cohorts, each with 1 row. n=10 must yield exactly 10 cases.
    pool = [
        _case(i, regex=f"r{i}", shadow=f"s{i}") for i in range(1, 25)
    ]
    out = stratified_sample(pool, n=10)
    assert len(out) == 10


def test_stratified_sample_is_deterministic():
    pool = [
        _case(i, regex="a", shadow="b") for i in range(1, 21)
    ] + [
        _case(i, regex="c", shadow="d") for i in range(21, 41)
    ]
    a = stratified_sample(pool, n=15)
    b = stratified_sample(pool, n=15)
    assert [c.event_id for c in a] == [c.event_id for c in b]


def test_format_batch_messages_chunks_and_carries_event_id():
    cases = [_case(100 + i, query=f"query {i}") for i in range(7)]
    msgs = format_batch_messages(cases, chunk_size=3)
    assert len(msgs) == 3  # 3 + 3 + 1
    assert "batch 1/3" in msgs[0]
    assert "batch 3/3" in msgs[2]
    for case in cases:
        marker = f"[#{case.event_id}]"
        assert any(marker in m for m in msgs), f"missing {marker} in any chunk"
    # Reply protocol explained.
    assert "A (regex)" in msgs[0]
    assert "B (shadow)" in msgs[0]
    assert "N (neither)" in msgs[0]


def test_format_batch_messages_handles_empty_input():
    msgs = format_batch_messages([])
    assert len(msgs) == 1
    assert "no divergence rows" in msgs[0].lower()


def test_format_batch_messages_truncates_very_long_queries():
    cases = [_case(1, query="x" * 10_000)]
    msgs = format_batch_messages(cases)
    assert "…" in msgs[0]
    # Telegram caps at 4096; we want to be safely under.
    assert all(len(m) < 4000 for m in msgs)


def test_format_batch_messages_handles_none_confidences():
    cases = [_case(1, rc=None, sc=None)]
    msgs = format_batch_messages(cases)
    assert "regex  → regex_a (—)" in msgs[0]
    assert "shadow → shadow_b (—)" in msgs[0]


def test_write_sample_artifact_emits_one_line_per_case(tmp_path: Path):
    cases = [_case(1), _case(2, query="hello")]
    fixed = datetime(2026, 4, 28, 9, 30, tzinfo=timezone.utc)
    path = write_sample_artifact(cases, out_dir=tmp_path, now=fixed)

    assert path.parent == tmp_path
    assert "20260428T093000Z" in path.name
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["event_id"] == 1
    assert parsed[1]["query_text"] == "hello"
    assert parsed[0]["regex_intent"] == "regex_a"


@pytest.mark.asyncio
async def test_fetch_divergence_rows_maps_columns():
    rows = [
        (
            42,
            datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
            "did Sarah send me anything",
            "person_lookup",
            "conversation_lookup",
            1.0,
            0.57,
        ),
    ]
    session = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    session.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def factory():
        yield session

    cases = await fetch_divergence_rows(factory)
    assert len(cases) == 1
    assert cases[0].event_id == 42
    assert cases[0].regex_intent == "person_lookup"
    assert cases[0].shadow_intent == "conversation_lookup"
    assert cases[0].shadow_confidence == pytest.approx(0.57)


# ---------------------------------------------------------------------------
# Reply ingestion + Gate 2 evaluation
# ---------------------------------------------------------------------------


def test_parse_reply_text_handles_canonical_lines():
    text = """
    100 A
    101 B
    102 N
    """
    out = parse_reply_text(text)
    assert [(v.event_id, v.verdict) for v in out] == [
        (100, "regex"),
        (101, "shadow"),
        (102, "neither"),
    ]


def test_parse_reply_text_tolerates_hash_punctuation_case_and_junk():
    text = "\n".join(
        [
            "#100 - a",
            "#101: B",
            "102. n  -- corrected later",
            "103   B",
            "noise line",
            "999",  # missing verdict — skipped
            "abc D",  # invalid letter — skipped
        ]
    )
    verdicts = {v.event_id: v.verdict for v in parse_reply_text(text)}
    assert verdicts == {
        100: "regex",
        101: "shadow",
        102: "neither",
        103: "shadow",
    }


def test_parse_reply_text_last_verdict_wins_on_repeated_id():
    text = "100 A\n100 B\n"
    verdicts = parse_reply_text(text)
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "shadow"


def test_load_sample_artifact_round_trip(tmp_path: Path):
    cases = [
        _case(1, query="alpha"),
        _case(2, query="beta", regex="x", shadow="y", rc=None, sc=None),
    ]
    fixed = datetime(2026, 4, 28, 9, 30, tzinfo=timezone.utc)
    path = write_sample_artifact(cases, out_dir=tmp_path, now=fixed)
    loaded = load_sample_artifact(path)
    assert [c.event_id for c in loaded] == [1, 2]
    assert loaded[1].regex_confidence is None
    assert loaded[1].shadow_confidence is None
    assert loaded[0].query_text == "alpha"


def test_evaluate_gate2_passes_when_thresholds_met():
    # 10 cases; verdicts: 7 shadow, 2 regex, 1 neither → 70% / 20%
    cases = [_case(i) for i in range(1, 11)]
    verdicts = (
        [AdjudicationVerdict(i, "shadow", "") for i in range(1, 8)]
        + [AdjudicationVerdict(i, "regex", "") for i in range(8, 10)]
        + [AdjudicationVerdict(10, "neither", "")]
    )
    result = evaluate_gate2(cases, verdicts)
    assert result.adjudicated == 10
    assert result.shadow_correct == 7
    assert result.regex_correct == 2
    assert result.neither == 1
    assert result.shadow_correct_pct == pytest.approx(0.7)
    assert result.regex_correct_pct == pytest.approx(0.2)
    assert result.semantic_target_met
    assert result.regex_target_met
    assert result.gate_passed
    assert result.unadjudicated_event_ids == []
    assert result.unknown_event_ids == []


def test_evaluate_gate2_fails_when_semantic_below_target():
    cases = [_case(i) for i in range(1, 11)]
    # 60% shadow correct — under 65% floor.
    verdicts = (
        [AdjudicationVerdict(i, "shadow", "") for i in range(1, 7)]
        + [AdjudicationVerdict(i, "regex", "") for i in range(7, 11)]
    )
    result = evaluate_gate2(cases, verdicts)
    assert not result.semantic_target_met
    # 40% regex — over 35% ceiling.
    assert not result.regex_target_met
    assert not result.gate_passed


def test_evaluate_gate2_partial_adjudication_tracks_missing_ids():
    cases = [_case(i) for i in range(1, 6)]
    verdicts = [
        AdjudicationVerdict(1, "shadow", ""),
        AdjudicationVerdict(3, "regex", ""),
    ]
    result = evaluate_gate2(cases, verdicts)
    assert result.adjudicated == 2
    assert result.unadjudicated_event_ids == [2, 4, 5]
    # Percentages computed over the adjudicated subset.
    assert result.shadow_correct_pct == pytest.approx(0.5)
    assert result.regex_correct_pct == pytest.approx(0.5)


def test_evaluate_gate2_flags_verdicts_for_ids_outside_sample():
    cases = [_case(1)]
    verdicts = [
        AdjudicationVerdict(1, "shadow", ""),
        AdjudicationVerdict(999, "regex", ""),
    ]
    result = evaluate_gate2(cases, verdicts)
    assert result.unknown_event_ids == [999]
    assert result.adjudicated == 1
    assert result.shadow_correct == 1
    assert result.regex_correct == 0


def test_evaluate_gate2_zero_adjudicated_does_not_pass():
    cases = [_case(1)]
    result = evaluate_gate2(cases, verdicts=[])
    assert result.adjudicated == 0
    assert result.shadow_correct_pct == 0.0
    assert result.regex_correct_pct == 0.0
    assert not result.gate_passed


def test_evaluate_gate2_thresholds_match_migration_plan():
    assert GATE2_SEMANTIC_MIN == pytest.approx(0.65)
    assert GATE2_REGEX_MAX == pytest.approx(0.35)


def test_write_gate2_result_serialises_pass_fail(tmp_path: Path):
    cases = [_case(i) for i in range(1, 4)]
    verdicts = [
        AdjudicationVerdict(1, "shadow", ""),
        AdjudicationVerdict(2, "shadow", ""),
        AdjudicationVerdict(3, "regex", ""),
    ]
    result = evaluate_gate2(cases, verdicts)
    fixed = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    path = write_gate2_result(result, out_dir=tmp_path, now=fixed)
    assert "20260428T120000Z" in path.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["adjudicated"] == 3
    assert payload["shadow_correct"] == 2
    assert payload["thresholds"]["semantic_min"] == pytest.approx(GATE2_SEMANTIC_MIN)
    assert payload["thresholds"]["regex_max"] == pytest.approx(GATE2_REGEX_MAX)
