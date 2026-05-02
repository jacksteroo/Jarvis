"""Unit tests for agent.semantic_router.SemanticRouter (Phase 2 facade).

The facade composes the deterministic multi-intent splitter and slot
extractors with the embedding-driven classifier. We mock the classifier
to return synthetic ``ClassificationResult``s and verify that the facade
correctly:

* maps intent labels to ``IntentType``,
* picks the right ``ActionMode`` per intent,
* threads slot extractor outputs into the ``RoutingDecision``,
* defers to ASK_CLARIFYING_QUESTION when the classifier defers,
* fans out one decision per multi-intent fragment,
* handles empty / non-string input without raising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.query_router import ActionMode, IntentType
from agent.semantic_router import (
    ClassificationResult,
    SemanticRouter,
)


def _result(
    *,
    label: str | None,
    confidence: float = 0.85,
    top_distance: float = 0.12,
    runner_up_label: str | None = None,
    runner_up_confidence: float = 0.05,
    is_ood: bool = False,
    is_ambiguous: bool = False,
    defer_reason: str | None = None,
) -> ClassificationResult:
    should_clarify = is_ood or is_ambiguous or label is None
    return ClassificationResult(
        intent_label=label,
        confidence=confidence,
        top_distance=top_distance,
        runner_up_label=runner_up_label,
        runner_up_confidence=runner_up_confidence,
        is_ood=is_ood,
        is_ambiguous=is_ambiguous,
        should_clarify=should_clarify,
        defer_reason=defer_reason,
        neighbours=[],
    )


def _router_with(side_effect):
    """Build a SemanticRouter whose classifier.classify returns ``side_effect``.

    ``side_effect`` may be a single result (returned every call) or a
    list of results (consumed in order).
    """
    classifier = AsyncMock()
    if isinstance(side_effect, list):
        classifier.classify = AsyncMock(side_effect=side_effect)
    else:
        classifier.classify = AsyncMock(return_value=side_effect)
    return SemanticRouter(classifier=classifier), classifier


# ── single-intent paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schedule_lookup_routes_to_call_tools():
    router, _ = _router_with(_result(label="schedule_lookup", confidence=0.92))

    decisions = await router.route("what's on my calendar today?")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.intent_type == IntentType.SCHEDULE_LOOKUP
    assert d.action_mode == ActionMode.CALL_TOOLS
    assert "calendar" in d.target_sources
    assert d.time_scope == "today"
    assert d.confidence == pytest.approx(0.92)
    assert "semantic:schedule_lookup" in d.reasoning


@pytest.mark.asyncio
async def test_capability_check_answers_from_context_with_all_sources():
    router, _ = _router_with(_result(label="capability_check", confidence=0.81))

    decisions = await router.route("can you actually do anything useful?")

    d = decisions[0]
    assert d.intent_type == IntentType.CAPABILITY_CHECK
    assert d.action_mode == ActionMode.ANSWER_FROM_CONTEXT
    # No sources mentioned in fragment → broad capability check
    assert d.target_sources == ["all"]


@pytest.mark.asyncio
async def test_general_chat_routes_to_answer_from_context():
    router, _ = _router_with(_result(label="general_chat"))

    decisions = await router.route("how are you feeling today?")

    assert decisions[0].intent_type == IntentType.GENERAL_CHAT
    assert decisions[0].action_mode == ActionMode.ANSWER_FROM_CONTEXT


@pytest.mark.asyncio
async def test_person_lookup_carries_entity_targets():
    router, _ = _router_with(_result(label="person_lookup"))

    decisions = await router.route("did Sarah send me anything?")

    assert decisions[0].intent_type == IntentType.PERSON_LOOKUP
    assert "Sarah" in decisions[0].entity_targets


# ── slot extractor wiring ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_sources_extracted():
    router, _ = _router_with(_result(label="inbox_summary"))

    decisions = await router.route("summarize my unread emails")

    assert "email" in decisions[0].target_sources


@pytest.mark.asyncio
async def test_filesystem_path_appended_to_target_sources():
    router, _ = _router_with(_result(label="general_chat"))

    decisions = await router.route("can you read /tmp/notes.md?")

    assert "filesystem" in decisions[0].target_sources


# ── deferral paths ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ood_defers_to_ask_clarifying():
    router, _ = _router_with(
        _result(
            label=None,
            top_distance=0.6,
            is_ood=True,
            defer_reason="ood",
        )
    )

    decisions = await router.route("xyzzy plover frobnicate")

    d = decisions[0]
    assert d.intent_type == IntentType.UNKNOWN
    assert d.action_mode == ActionMode.ASK_CLARIFYING_QUESTION
    assert d.needs_clarification is True
    assert "semantic_defer:ood" in d.reasoning


@pytest.mark.asyncio
async def test_ambiguous_defers_to_ask_clarifying():
    router, _ = _router_with(
        _result(
            label=None,
            confidence=0.45,
            runner_up_label="schedule_lookup",
            runner_up_confidence=0.40,
            is_ambiguous=True,
            defer_reason="ambiguous",
        )
    )

    decisions = await router.route("calendar emails today")

    assert decisions[0].action_mode == ActionMode.ASK_CLARIFYING_QUESTION
    assert "semantic_defer:ambiguous" in decisions[0].reasoning


# ── input contract ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_string_returns_clarifying_decision():
    router, classifier = _router_with(_result(label="general_chat"))

    decisions = await router.route("")

    assert len(decisions) == 1
    assert decisions[0].intent_type == IntentType.UNKNOWN
    assert decisions[0].action_mode == ActionMode.ASK_CLARIFYING_QUESTION
    classifier.classify.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_only_returns_clarifying_decision():
    router, classifier = _router_with(_result(label="general_chat"))

    decisions = await router.route("   \n\t   ")

    assert decisions[0].action_mode == ActionMode.ASK_CLARIFYING_QUESTION
    classifier.classify.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_input_returns_clarifying_decision():
    router, _ = _router_with(_result(label="general_chat"))

    decisions = await router.route(None)  # type: ignore[arg-type]

    assert decisions[0].action_mode == ActionMode.ASK_CLARIFYING_QUESTION


# ── multi-intent fan-out ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_intent_query_produces_one_decision_per_fragment():
    router, classifier = _router_with(
        [
            _result(label="schedule_lookup"),
            _result(label="inbox_summary"),
        ]
    )

    decisions = await router.route(
        "what's on my calendar today and summarize my unread emails"
    )

    assert len(decisions) == 2
    assert decisions[0].intent_type == IntentType.SCHEDULE_LOOKUP
    assert decisions[1].intent_type == IntentType.INBOX_SUMMARY
    # Second fragment's reasoning is prefixed with its index for shadow logs
    assert decisions[1].reasoning.startswith("fragment[1]")
    assert classifier.classify.await_count == 2


@pytest.mark.asyncio
async def test_route_first_returns_only_primary_decision():
    router, _ = _router_with(
        [
            _result(label="schedule_lookup"),
            _result(label="inbox_summary"),
        ]
    )

    decision = await router.route_first(
        "what's on my calendar and check my email"
    )

    assert decision.intent_type == IntentType.SCHEDULE_LOOKUP


@pytest.mark.asyncio
async def test_singleton_fragment_yields_single_decision():
    router, classifier = _router_with(_result(label="general_chat"))

    decisions = await router.route("how are you?")

    assert len(decisions) == 1
    assert classifier.classify.await_count == 1


# ── intent-label mapping safety ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_intent_label_falls_back_to_general_chat():
    router, _ = _router_with(_result(label="not_a_real_intent"))

    decisions = await router.route("hello there")

    # Unknown labels degrade gracefully — they don't crash and they don't
    # promote themselves to ASK_CLARIFYING_QUESTION (the classifier's job).
    assert decisions[0].intent_type == IntentType.GENERAL_CHAT
    assert decisions[0].action_mode == ActionMode.ANSWER_FROM_CONTEXT
