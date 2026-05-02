"""Unit tests for agent.router_exemplar_seeds (Phase 2 bootstrap sources).

Iterators are pure — they map JSONL rows / DB rows to ExemplarSeed
records. We assert mapping, polarity filters, and skip-on-bad-input
behavior. The end-to-end DB round-trip is verified by the live smoke
test recorded in the run notes; here we mock the session factory.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.router_exemplar_seeds import (
    iter_manual_exemplars,
    iter_phase0_gold,
    iter_phase0_platinum,
    iter_phase1_silver,
)
from agent.router_exemplars import ExemplarSeed


def _row(*, success: bool, query="hello", intent="schedule_lookup", **extra):
    base = {
        "battery_id": "test-01",
        "query": query,
        "expected_intent": intent,
        "verdict": {"success": success},
    }
    base.update(extra)
    return base


def _write_jsonl(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "battery.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return p


# ── iter_phase0_platinum / iter_phase0_gold ──────────────────────────────────


def test_platinum_yields_only_failures(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [
            _row(success=False, query="q1", intent="schedule_lookup"),
            _row(success=True, query="q2", intent="inbox_summary"),
            _row(success=False, query="q3", intent="person_lookup"),
        ],
    )
    seeds = list(iter_phase0_platinum(path))
    assert [s.query for s in seeds] == ["q1", "q3"]
    assert all(s.tier == "platinum" for s in seeds)
    assert all(s.source_note and "phase0_platinum" in s.source_note for s in seeds)


def test_gold_yields_only_successes(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [
            _row(success=False, query="q1"),
            _row(success=True, query="q2", intent="inbox_summary"),
            _row(success=True, query="q3", intent="cross_source_triage"),
        ],
    )
    seeds = list(iter_phase0_gold(path))
    assert [s.query for s in seeds] == ["q2", "q3"]
    assert all(s.tier == "gold" for s in seeds)


def test_seed_iterators_skip_missing_fields(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [
            {"battery_id": "x", "verdict": {"success": False}},  # no query/intent
            _row(success=False, query="", intent="schedule_lookup"),  # empty query
            _row(success=False, query="ok", intent=""),  # empty intent
            _row(success=False, query="good", intent="schedule_lookup"),
        ],
    )
    seeds = list(iter_phase0_platinum(path))
    assert [s.query for s in seeds] == ["good"]


def test_seed_iterators_tolerate_bad_jsonl_lines(tmp_path):
    path = tmp_path / "battery.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps(_row(success=False, query="ok")) + "\n")
        fh.write("\n")  # blank
        fh.write("{not closed\n")
    seeds = list(iter_phase0_platinum(path))
    assert [s.query for s in seeds] == ["ok"]


def test_seed_iterators_strip_whitespace(tmp_path):
    path = _write_jsonl(
        tmp_path,
        [_row(success=False, query="  spaced  ", intent="  schedule_lookup  ")],
    )
    seeds = list(iter_phase0_platinum(path))
    assert seeds[0].query == "spaced"
    assert seeds[0].intent_label == "schedule_lookup"


# ── iter_manual_exemplars ────────────────────────────────────────────────────


def _write_manual(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "manual.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return p


def test_manual_exemplars_yield_manual_tier(tmp_path):
    path = _write_manual(
        tmp_path,
        [
            {"query": "where is Susan staying?", "intent": "person_lookup", "pattern_id": 1},
            {"query": "any new emails today?", "intent": "inbox_summary"},
        ],
    )
    seeds = list(iter_manual_exemplars(path))
    assert [s.tier for s in seeds] == ["manual", "manual"]
    assert seeds[0].source_note == "manual:manual.jsonl:pattern_1"
    assert seeds[1].source_note == "manual:manual.jsonl:ad_hoc"


def test_manual_exemplars_skip_blank_or_missing(tmp_path):
    path = _write_manual(
        tmp_path,
        [
            {"query": "", "intent": "person_lookup"},
            {"query": "ok", "intent": ""},
            {"intent": "person_lookup"},
            {"query": "valid", "intent": "person_lookup", "pattern_id": 5},
        ],
    )
    seeds = list(iter_manual_exemplars(path))
    assert [s.query for s in seeds] == ["valid"]


def test_manual_exemplars_strip_whitespace(tmp_path):
    path = _write_manual(
        tmp_path,
        [{"query": "  trimmed  ", "intent": "  schedule_lookup  "}],
    )
    seeds = list(iter_manual_exemplars(path))
    assert seeds[0].query == "trimmed"
    assert seeds[0].intent_label == "schedule_lookup"


def test_manual_exemplars_accepts_expected_intent_alias(tmp_path):
    path = _write_manual(
        tmp_path,
        [{"query": "q", "expected_intent": "action_items", "pattern_id": 7}],
    )
    seeds = list(iter_manual_exemplars(path))
    assert seeds[0].intent_label == "action_items"
    assert seeds[0].source_note == "manual:manual.jsonl:pattern_7"


def test_manual_exemplars_prefers_intent_label_over_legacy_intent(tmp_path):
    """The JSONL key MUST be ``intent_label`` (matches the DB column).

    ``intent`` and ``expected_intent`` keys are accepted as a back-compat
    shim for older artefacts in ``backups/router/``. New seed files must
    use ``intent_label``. If both keys are present, ``intent_label`` wins.
    """
    path = _write_manual(
        tmp_path,
        [
            {"query": "modern", "intent_label": "schedule_lookup"},
            {"query": "legacy", "intent": "person_lookup"},
            {"query": "both", "intent_label": "inbox_summary", "intent": "person_lookup"},
        ],
    )
    seeds = list(iter_manual_exemplars(path))
    assert [s.intent_label for s in seeds] == [
        "schedule_lookup",
        "person_lookup",
        "inbox_summary",
    ]


def test_canonical_seed_files_use_intent_label_key():
    """Lint: every in-repo seed JSONL must use ``intent_label`` (not ``intent``).

    The DB column is ``intent_label``; the dataclass field is
    ``intent_label``; SQL queries say ``intent_label``. Keeping the JSONL
    key aligned closes the last vector for "wait, is the column called
    intent or intent_label?" drift.
    """
    repo_root = Path(__file__).resolve().parents[2]
    seed_files = sorted((repo_root / "tests").glob("router_*seeds*.jsonl"))
    seed_files += [repo_root / "tests" / "router_manual_exemplars.jsonl"]
    offenders: list[tuple[str, int]] = []
    for path in seed_files:
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "intent" in row and "intent_label" not in row:
                offenders.append((str(path.relative_to(repo_root)), lineno))
    assert not offenders, (
        "Seed JSONL files must use `intent_label` (the DB column name), not "
        f"`intent`. Offenders: {offenders}"
    )


def test_manual_exemplars_canonical_file_loads():
    """Sanity guard on the in-repo canonical manual JSONL."""
    repo_root = Path(__file__).resolve().parents[2]
    canonical = repo_root / "tests" / "router_manual_exemplars.jsonl"
    seeds = list(iter_manual_exemplars(canonical))
    assert len(seeds) >= 50
    assert all(s.tier == "manual" for s in seeds)
    valid = {
        "action_items", "capability_check", "conversation_lookup",
        "cross_source_triage", "general_chat", "inbox_summary",
        "person_lookup", "schedule_lookup", "unsupported_capability",
        "web_lookup",
    }
    bad = [s.intent_label for s in seeds if s.intent_label not in valid]
    assert not bad, f"unknown intent labels in canonical manual JSONL: {bad}"


# ── iter_phase1_silver ───────────────────────────────────────────────────────


def _silver_factory(rows: list[tuple[int, str, str]]):
    """Mock factory returning the supplied (id, query, intent) triples."""

    async def execute(stmt):
        scalar = MagicMock()
        scalar.all = MagicMock(return_value=rows)
        return scalar

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)

    @asynccontextmanager
    async def _ctx():
        yield session

    return MagicMock(side_effect=lambda: _ctx())


@pytest.mark.asyncio
async def test_silver_seeds_carry_intent_and_provenance():
    factory = _silver_factory(
        [
            (101, "what's on my calendar?", "schedule_lookup"),
            (102, "any new emails?", "inbox_summary"),
        ]
    )
    seeds = await iter_phase1_silver(factory)
    assert len(seeds) == 2
    assert all(isinstance(s, ExemplarSeed) for s in seeds)
    assert all(s.tier == "silver" for s in seeds)
    assert seeds[0].source_note == "phase1_silver:routing_events:101"
    assert seeds[1].intent_label == "inbox_summary"


@pytest.mark.asyncio
async def test_silver_seeds_skip_blank_rows():
    factory = _silver_factory(
        [
            (1, "   ", "schedule_lookup"),
            (2, "valid query", ""),
            (3, "", ""),
            (4, "good", "schedule_lookup"),
        ]
    )
    seeds = await iter_phase1_silver(factory)
    assert [s.query for s in seeds] == ["good"]


@pytest.mark.asyncio
async def test_silver_seeds_empty_input():
    factory = _silver_factory([])
    seeds = await iter_phase1_silver(factory)
    assert seeds == []
