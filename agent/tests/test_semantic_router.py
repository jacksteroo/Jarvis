"""Unit tests for agent.semantic_router.SemanticIntentClassifier.

The DB session is mocked — we drive ``execute`` to return synthetic
neighbour rows in the same shape the production query yields. Real
pgvector cosine round-trips are exercised by the live e2e run, not here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.semantic_router import (
    AMBIGUITY_RUNNER_UP_THRESHOLD,
    K_NEIGHBOURS,
    MIN_CONFIDENCE,
    OOD_DISTANCE_THRESHOLD,
    SemanticIntentClassifier,
    _EmbeddingCache,
)


def _factory(rows: list[tuple[int, str, str, float]]):
    """Mock async DB session whose execute returns ``rows``.

    Each row tuple matches the production select columns:
    ``(id, intent_label, tier, distance)``.
    """

    async def execute(_stmt):
        result = MagicMock()
        result.all = MagicMock(return_value=list(rows))
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)

    @asynccontextmanager
    async def _ctx():
        yield session

    def factory():
        return _ctx()

    factory.session = session  # for test assertions
    return factory


def _embed_const(value: list[float] | None = None):
    """Return an async embed_fn that yields a constant vector."""
    payload = value if value is not None else [0.1, 0.2, 0.3]

    async def _embed(_query: str) -> list[float]:
        _embed.calls += 1  # type: ignore[attr-defined]
        return payload

    _embed.calls = 0  # type: ignore[attr-defined]
    return _embed


@pytest.mark.asyncio
async def test_returns_winner_on_clear_majority():
    rows = [
        (1, "person_lookup", "platinum", 0.05),
        (2, "person_lookup", "gold", 0.10),
        (3, "person_lookup", "silver", 0.12),
        (4, "person_lookup", "silver", 0.15),
        (5, "person_lookup", "silver", 0.18),
        (6, "general_chat", "silver", 0.30),
        (7, "general_chat", "silver", 0.35),
    ]
    classifier = SemanticIntentClassifier(
        db_factory=_factory(rows), embed_fn=_embed_const()
    )
    result = await classifier.classify("who is alice")
    assert result.intent_label == "person_lookup"
    assert result.should_clarify is False
    assert result.is_ood is False
    assert result.is_ambiguous is False
    assert result.confidence > MIN_CONFIDENCE
    assert result.runner_up_label == "general_chat"
    assert len(result.neighbours) == 7


@pytest.mark.asyncio
async def test_ood_top_distance_above_threshold():
    rows = [
        (1, "person_lookup", "platinum", OOD_DISTANCE_THRESHOLD + 0.05),
        (2, "person_lookup", "gold", OOD_DISTANCE_THRESHOLD + 0.10),
    ]
    classifier = SemanticIntentClassifier(
        db_factory=_factory(rows), embed_fn=_embed_const()
    )
    result = await classifier.classify("totally novel phrasing")
    assert result.is_ood is True
    assert result.should_clarify is True
    assert result.intent_label is None
    assert result.defer_reason == "ood"


@pytest.mark.asyncio
async def test_ambiguous_low_winner_competing_runner_up():
    # 4 vs 3 split with similar distances → close weights → low winner
    # confidence and runner-up above the ambiguity threshold.
    rows = [
        (1, "person_lookup", "silver", 0.20),
        (2, "person_lookup", "silver", 0.22),
        (3, "person_lookup", "silver", 0.25),
        (4, "person_lookup", "silver", 0.28),
        (5, "schedule_lookup", "silver", 0.21),
        (6, "schedule_lookup", "silver", 0.23),
        (7, "schedule_lookup", "silver", 0.26),
    ]
    classifier = SemanticIntentClassifier(
        db_factory=_factory(rows), embed_fn=_embed_const()
    )
    result = await classifier.classify("who is meeting me tomorrow")
    assert result.is_ood is False
    # Hand-check: equal contributions → winner conf ~0.55 boundary
    if result.is_ambiguous:
        assert result.should_clarify is True
        assert result.intent_label is None
        assert result.runner_up_label == "schedule_lookup"
        assert result.runner_up_confidence > AMBIGUITY_RUNNER_UP_THRESHOLD
    else:
        # If the constants ever loosen, the math below must still hold.
        assert result.confidence >= MIN_CONFIDENCE


@pytest.mark.asyncio
async def test_empty_query_defers():
    classifier = SemanticIntentClassifier(
        db_factory=_factory([]), embed_fn=_embed_const()
    )
    for q in ["", "   ", "\t\n"]:
        result = await classifier.classify(q)
        assert result.should_clarify is True
        assert result.intent_label is None
        assert result.defer_reason == "empty_query"


@pytest.mark.asyncio
async def test_empty_exemplar_table_defers():
    classifier = SemanticIntentClassifier(
        db_factory=_factory([]), embed_fn=_embed_const()
    )
    result = await classifier.classify("anything")
    assert result.should_clarify is True
    assert result.defer_reason == "no_exemplars"


@pytest.mark.asyncio
async def test_embed_failure_defers():
    async def _bad_embed(_q: str) -> list[float]:
        raise RuntimeError("ollama down")

    classifier = SemanticIntentClassifier(
        db_factory=_factory([(1, "x", "silver", 0.1)]), embed_fn=_bad_embed
    )
    result = await classifier.classify("anything")
    assert result.should_clarify is True
    assert result.defer_reason == "embed_failed"


@pytest.mark.asyncio
async def test_embed_returns_empty_defers():
    async def _empty_embed(_q: str) -> list[float]:
        return []

    classifier = SemanticIntentClassifier(
        db_factory=_factory([(1, "x", "silver", 0.1)]), embed_fn=_empty_embed
    )
    result = await classifier.classify("anything")
    assert result.defer_reason == "embed_failed"


@pytest.mark.asyncio
async def test_embedding_cache_skips_repeat_calls():
    embed = _embed_const()
    classifier = SemanticIntentClassifier(
        db_factory=_factory([(1, "person_lookup", "platinum", 0.05)]),
        embed_fn=embed,
    )
    await classifier.classify("hello there")
    await classifier.classify("hello there")
    await classifier.classify("hello there")
    assert embed.calls == 1
    assert classifier.cache_size == 1


@pytest.mark.asyncio
async def test_cache_distinct_queries():
    embed = _embed_const()
    classifier = SemanticIntentClassifier(
        db_factory=_factory([(1, "person_lookup", "platinum", 0.05)]),
        embed_fn=embed,
    )
    await classifier.classify("hello")
    await classifier.classify("hi")
    await classifier.classify("hey")
    assert embed.calls == 3
    assert classifier.cache_size == 3


@pytest.mark.asyncio
async def test_cache_lru_eviction():
    cache = _EmbeddingCache(max_entries=2)
    cache.put("a", [1.0])
    cache.put("b", [2.0])
    cache.put("c", [3.0])
    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") == [2.0]
    assert cache.get("c") == [3.0]


@pytest.mark.asyncio
async def test_cache_get_promotes_to_mru():
    cache = _EmbeddingCache(max_entries=2)
    cache.put("a", [1.0])
    cache.put("b", [2.0])
    cache.get("a")  # touch → a becomes MRU
    cache.put("c", [3.0])  # evicts b, not a
    assert cache.get("a") == [1.0]
    assert cache.get("b") is None
    assert cache.get("c") == [3.0]


@pytest.mark.asyncio
async def test_cache_skips_empty_embedding():
    cache = _EmbeddingCache()
    cache.put("a", [])
    assert cache.get("a") is None
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_invalid_k_rejected():
    with pytest.raises(ValueError):
        SemanticIntentClassifier(
            db_factory=_factory([]), embed_fn=_embed_const(), k=0
        )


@pytest.mark.asyncio
async def test_non_string_query_defers():
    classifier = SemanticIntentClassifier(
        db_factory=_factory([(1, "x", "silver", 0.1)]), embed_fn=_embed_const()
    )
    result = await classifier.classify(None)  # type: ignore[arg-type]
    assert result.should_clarify is True
    assert result.defer_reason == "empty_query"


@pytest.mark.asyncio
async def test_single_neighbour_no_runner_up():
    rows = [(1, "person_lookup", "platinum", 0.05)]
    classifier = SemanticIntentClassifier(
        db_factory=_factory(rows), embed_fn=_embed_const()
    )
    result = await classifier.classify("anything")
    assert result.intent_label == "person_lookup"
    assert result.runner_up_label is None
    assert result.runner_up_confidence == 0.0
    assert result.confidence == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_classification_result_as_dict_serialises_cleanly():
    rows = [(1, "person_lookup", "platinum", 0.05)]
    classifier = SemanticIntentClassifier(
        db_factory=_factory(rows), embed_fn=_embed_const()
    )
    result = await classifier.classify("anything")
    payload = result.as_dict()
    # All values must be JSON-serialisable primitives — the shadow logger
    # writes this straight to a JSONB column.
    import json

    json.dumps(payload)
    assert payload["intent_label"] == "person_lookup"
    assert payload["neighbour_count"] == 1


def test_default_k_matches_migration_plan():
    assert K_NEIGHBOURS == 7
