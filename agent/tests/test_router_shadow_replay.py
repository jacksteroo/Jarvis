"""Unit tests for agent.router_shadow_replay (Phase 2).

Mocks the DB session and SemanticRouter. Real DB writes are exercised
by the live e2e replay run inside the pepper container; here we assert
the selection contract (max-confidence pick across fragments), the
NULL-only filter (idempotency), and the failure-tolerance contract
(classifier exceptions leave shadow columns NULL).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.query_router import ActionMode, IntentType, RoutingDecision
from agent.router_shadow_replay import ReplayResult, replay


def _decision(intent: IntentType, confidence: float) -> RoutingDecision:
    return RoutingDecision(
        intent_type=intent,
        target_sources=[],
        action_mode=ActionMode.CALL_TOOLS,
        confidence=confidence,
        reasoning=f"test:{intent.value}",
    )


def _make_factory(rows: list[tuple[int, str | None]]):
    """Mock DB session factory.

    First-call ``execute`` returns the SELECT rows (one tuple per row).
    Subsequent calls are UPDATEs — we record the row id and the values
    bound to ``shadow_decision_intent`` / ``shadow_decision_confidence``.
    """
    recorder = MagicMock()
    recorder.updates = []  # list of (row_id, intent, confidence)
    recorder.commit_count = 0
    recorder.select_returned = False

    async def execute(stmt):
        compiled = stmt.compile()
        params = compiled.params
        result = MagicMock()
        if not recorder.select_returned:
            recorder.select_returned = True
            result.all.return_value = rows
            return result
        # UPDATE … WHERE id = :id_1 with values param_1, param_2.
        recorder.updates.append(
            (
                params.get("id_1"),
                params.get("shadow_decision_intent"),
                params.get("shadow_decision_confidence"),
            )
        )
        return result

    session = MagicMock()
    session.execute = AsyncMock(side_effect=execute)

    async def _commit():
        recorder.commit_count += 1

    session.commit = AsyncMock(side_effect=_commit)

    @asynccontextmanager
    async def factory():
        yield session

    return factory, recorder


def _make_router(by_query: dict[str, list[RoutingDecision] | Exception]):
    router = MagicMock()

    async def route(query: str):
        result = by_query[query]
        if isinstance(result, Exception):
            raise result
        return result

    router.route = AsyncMock(side_effect=route)
    return router


@pytest.mark.asyncio
async def test_picks_max_confidence_decision_across_fragments():
    factory, recorder = _make_factory([(1, "calendar and emails")])
    router = _make_router({
        "calendar and emails": [
            _decision(IntentType.SCHEDULE_LOOKUP, 0.55),
            _decision(IntentType.INBOX_SUMMARY, 0.82),
        ]
    })

    result = await replay(db_factory=factory, router=router)

    assert result.scanned == 1
    assert result.updated == 1
    assert recorder.updates == [(1, "inbox_summary", 0.82)]
    assert recorder.commit_count == 1


@pytest.mark.asyncio
async def test_skips_empty_query_text_without_calling_router():
    factory, recorder = _make_factory([(1, "   "), (2, None)])
    router = _make_router({})  # never called

    result = await replay(db_factory=factory, router=router)

    assert result.scanned == 2
    assert result.skipped_empty_query == 2
    assert result.updated == 0
    assert recorder.updates == []
    router.route.assert_not_called()


@pytest.mark.asyncio
async def test_classifier_exception_leaves_shadow_columns_null():
    factory, recorder = _make_factory([(7, "boom")])
    router = _make_router({"boom": RuntimeError("ollama down")})

    result = await replay(db_factory=factory, router=router)

    assert result.scanned == 1
    assert result.classifier_errors == 1
    assert result.updated == 0
    # No UPDATE issued → shadow columns stay NULL.
    assert recorder.updates == []
    assert recorder.commit_count == 0


@pytest.mark.asyncio
async def test_unknown_decision_counts_as_deferred_but_still_updates():
    """Classifier defer (OOD/ambiguous) yields IntentType.UNKNOWN; we
    still write the row so the scan is idempotent on re-run."""
    factory, recorder = _make_factory([(3, "xyzzy plover")])
    router = _make_router({
        "xyzzy plover": [_decision(IntentType.UNKNOWN, 0.0)]
    })

    result = await replay(db_factory=factory, router=router)

    assert result.scanned == 1
    assert result.updated == 1
    assert result.deferred == 1
    assert recorder.updates == [(3, "unknown", 0.0)]


@pytest.mark.asyncio
async def test_dry_run_skips_update_but_counts_match():
    factory, recorder = _make_factory([(5, "what's on my calendar")])
    router = _make_router({
        "what's on my calendar": [_decision(IntentType.SCHEDULE_LOOKUP, 0.91)]
    })

    result = await replay(db_factory=factory, router=router, dry_run=True)

    assert result.scanned == 1
    assert result.updated == 1
    assert recorder.updates == []
    assert recorder.commit_count == 0


@pytest.mark.asyncio
async def test_empty_decisions_list_counts_as_classifier_error():
    factory, recorder = _make_factory([(9, "edge")])
    router = _make_router({"edge": []})  # SemanticRouter contract says non-empty, but be defensive

    result = await replay(db_factory=factory, router=router)

    assert result.scanned == 1
    assert result.classifier_errors == 1
    assert result.updated == 0


def test_replay_result_as_dict_shape():
    r = ReplayResult(scanned=5, updated=4, skipped_empty_query=1, classifier_errors=0, deferred=2)
    assert r.as_dict() == {
        "scanned": 5,
        "updated": 4,
        "skipped_empty_query": 1,
        "classifier_errors": 0,
        "deferred": 2,
    }
