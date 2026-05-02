"""Unit tests for agent.router_exemplars (Phase 2 SemanticRouter scaffold).

DB session is mocked — real INSERT/SELECT round-trips are exercised by
the live e2e run. Here we assert shape: validation, idempotency, embed
tolerance, dry-run, and stats helpers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.router_exemplars import (
    ExemplarSeed,
    LoadResult,
    VALID_TIERS,
    load_exemplars,
)


def _factory(*, existing_keys: set[tuple[str, str, str]] | None = None):
    """Mock async DB session with optional pre-existing dedup keys.

    Each tuple is ``(query, intent, tier)`` — matches what
    ``_row_already_present`` checks.
    """
    existing_keys = existing_keys or set()
    recorder = MagicMock()
    recorder.added = []
    recorder.commits = 0

    async def execute(stmt):
        compiled = stmt.compile()
        params = compiled.params
        # Param names follow SQLA's auto-generated ``query_text_1`` style.
        q = params.get("query_text_1")
        i = params.get("intent_label_1")
        t = params.get("tier_1")
        scalar = MagicMock()
        scalar.scalar_one_or_none = MagicMock(
            return_value=1 if (q, i, t) in existing_keys else None
        )
        return scalar

    async def commit():
        recorder.commits += 1

    def add(row):
        recorder.added.append(row)

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)
    session.commit = AsyncMock(side_effect=commit)
    session.add = MagicMock(side_effect=add)

    @asynccontextmanager
    async def _ctx():
        yield session

    factory = MagicMock(side_effect=lambda: _ctx())
    factory.recorder = recorder
    factory.session = session
    return factory


# ── ExemplarSeed validation ──────────────────────────────────────────────────


def test_exemplar_seed_accepts_all_valid_tiers():
    for tier in VALID_TIERS:
        seed = ExemplarSeed(query="q", intent_label="i", tier=tier)
        assert seed.tier == tier


def test_exemplar_seed_rejects_unknown_tier():
    with pytest.raises(ValueError, match="tier"):
        ExemplarSeed(query="q", intent_label="i", tier="bronze")


def test_exemplar_seed_rejects_empty_query_and_intent():
    with pytest.raises(ValueError, match="query"):
        ExemplarSeed(query="", intent_label="i", tier="gold")
    with pytest.raises(ValueError, match="query"):
        ExemplarSeed(query="   ", intent_label="i", tier="gold")
    with pytest.raises(ValueError, match="intent_label"):
        ExemplarSeed(query="q", intent_label="", tier="gold")


def test_exemplar_seed_rejects_non_string_inputs():
    with pytest.raises(ValueError):
        ExemplarSeed(query=None, intent_label="i", tier="gold")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExemplarSeed(query="q", intent_label=42, tier="gold")  # type: ignore[arg-type]


# ── load_exemplars happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_exemplars_inserts_each_seed_once():
    seeds = [
        ExemplarSeed("what's on my calendar?", "schedule_lookup", "gold"),
        ExemplarSeed("draft a reply to mom", "person_lookup", "platinum"),
    ]
    embed_fn = AsyncMock(side_effect=lambda q: [0.1] * 1024)
    factory = _factory()

    result = await load_exemplars(seeds, db_factory=factory, embed_fn=embed_fn)

    assert result.scanned == 2
    assert result.inserted == 2
    assert result.skipped_duplicate == 0
    assert result.embed_failures == 0
    assert factory.recorder.commits == 2
    assert len(factory.recorder.added) == 2
    # Embedding actually attached to each row
    for row in factory.recorder.added:
        assert row.embedding is not None
        assert len(row.embedding) == 1024


@pytest.mark.asyncio
async def test_load_exemplars_accepts_dict_seeds():
    seeds = [
        {"query": "find emails from Alice", "intent_label": "person_lookup", "tier": "silver",
         "source_note": "phase1_organic"},
    ]
    embed_fn = AsyncMock(return_value=[0.0] * 1024)
    factory = _factory()

    result = await load_exemplars(seeds, db_factory=factory, embed_fn=embed_fn)

    assert result.inserted == 1
    assert factory.recorder.added[0].source_note == "phase1_organic"


@pytest.mark.asyncio
async def test_load_exemplars_skips_invalid_dict_seed():
    seeds = [
        {"query": "ok", "intent_label": "x", "tier": "bronze"},  # bad tier
        {"query": "no_intent_field", "tier": "gold"},  # missing intent
    ]
    embed_fn = AsyncMock(return_value=[0.0] * 1024)
    factory = _factory()

    result = await load_exemplars(seeds, db_factory=factory, embed_fn=embed_fn)

    assert result.scanned == 2
    assert result.skipped_invalid == 2
    assert result.inserted == 0
    embed_fn.assert_not_called()


# ── Idempotency ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_exemplars_skips_duplicates():
    existing = {("dup query", "schedule_lookup", "gold")}
    seeds = [
        ExemplarSeed("dup query", "schedule_lookup", "gold"),
        ExemplarSeed("fresh query", "schedule_lookup", "gold"),
    ]
    embed_fn = AsyncMock(return_value=[0.0] * 1024)
    factory = _factory(existing_keys=existing)

    result = await load_exemplars(seeds, db_factory=factory, embed_fn=embed_fn)

    assert result.skipped_duplicate == 1
    assert result.inserted == 1
    # Only the fresh row was embedded
    assert embed_fn.await_count == 1


# ── Embed failure tolerance ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_exemplars_continues_on_embed_failure():
    seeds = [
        ExemplarSeed("first", "schedule_lookup", "gold"),
        ExemplarSeed("second", "schedule_lookup", "gold"),
    ]

    calls = {"n": 0}

    async def flaky_embed(q: str) -> list[float]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ollama down")
        return [0.0] * 1024

    factory = _factory()

    result = await load_exemplars(seeds, db_factory=factory, embed_fn=flaky_embed)

    assert result.scanned == 2
    assert result.inserted == 2  # both rows still landed
    assert result.embed_failures == 1
    assert factory.recorder.added[0].embedding is None
    assert factory.recorder.added[1].embedding is not None


# ── Dry-run ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_exemplars_dry_run_does_not_commit():
    seeds = [ExemplarSeed("q", "schedule_lookup", "gold")]
    embed_fn = AsyncMock(return_value=[0.0] * 1024)
    factory = _factory()

    result = await load_exemplars(
        seeds, db_factory=factory, embed_fn=embed_fn, dry_run=True
    )

    assert result.inserted == 1
    assert factory.recorder.commits == 0
    assert factory.recorder.added == []


# ── LoadResult shape ─────────────────────────────────────────────────────────


def test_load_result_as_dict_has_expected_keys():
    keys = LoadResult().as_dict().keys()
    assert set(keys) == {
        "scanned", "inserted", "skipped_duplicate", "skipped_invalid", "embed_failures"
    }
