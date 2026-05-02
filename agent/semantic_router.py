"""Semantic intent classifier — Phase 2 read path on top of router_exemplars.

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. This module provides the
read-side of the semantic router: a k-NN classifier over labeled
exemplars (pgvector cosine distance against ``router_exemplars``).

Scope of this iteration
-----------------------

Intent classification only — *job 1* of the target architecture.
Slot extraction (``agent/slot_extractors.py``) and the higher-level
``SemanticRouter`` that fuses intent + slots into a ``RoutingDecision``
land in subsequent runs. Shadow-mode wiring into ``core.py`` is also a
later iteration; the regex router stays primary.

SemanticRouter facade
---------------------

The :class:`SemanticRouter` defined at the bottom of this module is the
Phase 2 read path users plug into ``core.py`` (in shadow mode) and Phase
3 promotes to primary. It composes the classifier with
``agent.multi_intent_splitter.split_multi_intent`` and
``agent.slot_extractors`` to turn a user message into one or more
``RoutingDecision`` instances — the exact shape the legacy
``QueryRouter`` emits, so shadow comparisons are field-aligned.

Algorithm (per migration plan §"k-NN classifier")
-------------------------------------------------

::

    k = 7
    distance = cosine via pgvector
    weighting = 1 / (epsilon + distance)   # epsilon = 0.05; near-zero
                                           # matches dominate noisy field
    confidence = sum(winning_weights) / sum(all_weights)
    ood: top-1 distance > 0.40 → ASK_CLARIFYING
    ambiguity: winner conf < 0.55 AND runner_up > 0.30 → ASK_CLARIFYING

Thresholds are starting values. They are tuned via shadow data on real
traffic in a later phase (see migration plan §"Bootstrap exemplars").

Privacy
-------

All embeddings are local (``qwen3-embedding:0.6b`` via Ollama). The
classifier never makes external API calls. Query text and embeddings
stay on the machine. The classifier reads from ``router_exemplars``,
never writes — feedback-driven exemplar growth is Phase 4.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy import select

from agent.models import RouterExemplar
from agent.multi_intent_splitter import split_multi_intent
from agent.query_router import ActionMode, IntentType, RoutingDecision
from agent.slot_extractors import (
    extract_entity_targets,
    extract_filesystem_path,
    extract_target_sources,
    extract_time_scope,
)

logger = structlog.get_logger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]
DbFactory = Callable[[], Any]


# ── Tunable thresholds ────────────────────────────────────────────────────────

#: Number of nearest neighbours retrieved for the vote.
K_NEIGHBOURS: int = 7

#: Distance-kernel epsilon. Vote weight is ``1 / (epsilon + distance)`` so a
#: near-zero-distance neighbour dominates farther noisy neighbours instead of
#: drowning in the field. Calibrated against the canonical eval set: with the
#: original ``1 / (1 + d)`` kernel, a self-match (d=0, weight 1.0) lost to six
#: mismatched neighbours at d≈0.3 (weight ≈0.77 each); see Phase 2 root-cause
#: analysis 2026-04-29.
KERNEL_EPSILON: float = 0.05

#: Cosine distance above which the top-1 neighbour is treated as
#: out-of-distribution. pgvector cosine distance is 1 - cosine_similarity.
OOD_DISTANCE_THRESHOLD: float = 0.40

#: Winner confidence below this triggers the ambiguity gate when the
#: runner-up is also competitive (see ``AMBIGUITY_RUNNER_UP_THRESHOLD``).
MIN_CONFIDENCE: float = 0.55

#: Runner-up confidence above this counts as a competing label for the
#: ambiguity gate.
AMBIGUITY_RUNNER_UP_THRESHOLD: float = 0.30

#: Bound on the in-process embedding cache. ``qwen3-embedding:0.6b`` is
#: ~80-150 ms per call; the cache turns repeats into ~0 ms.
EMBED_CACHE_MAX_ENTRIES: int = 2048


# ── Result dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Neighbour:
    """One nearest-neighbour row from ``router_exemplars``."""

    exemplar_id: int
    intent_label: str
    tier: str
    distance: float


@dataclass(frozen=True)
class ClassificationResult:
    """Output of ``SemanticIntentClassifier.classify``.

    ``intent_label`` is the winning label by weighted vote, or ``None``
    when the classifier defers (OOD, ambiguous, empty input, embed
    failure, empty exemplar table). ``should_clarify`` is the single
    boolean callers check to decide whether to ASK_CLARIFYING.
    """

    intent_label: str | None
    confidence: float
    top_distance: float
    runner_up_label: str | None
    runner_up_confidence: float
    is_ood: bool
    is_ambiguous: bool
    should_clarify: bool
    defer_reason: str | None
    neighbours: list[Neighbour] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_label": self.intent_label,
            "confidence": self.confidence,
            "top_distance": self.top_distance,
            "runner_up_label": self.runner_up_label,
            "runner_up_confidence": self.runner_up_confidence,
            "is_ood": self.is_ood,
            "is_ambiguous": self.is_ambiguous,
            "should_clarify": self.should_clarify,
            "defer_reason": self.defer_reason,
            "neighbour_count": len(self.neighbours),
        }


def _empty_result(*, defer_reason: str) -> ClassificationResult:
    return ClassificationResult(
        intent_label=None,
        confidence=0.0,
        top_distance=float("inf"),
        runner_up_label=None,
        runner_up_confidence=0.0,
        is_ood=False,
        is_ambiguous=False,
        should_clarify=True,
        defer_reason=defer_reason,
        neighbours=[],
    )


# ── Embedding cache ───────────────────────────────────────────────────────────


class _EmbeddingCache:
    """Bounded LRU cache keyed on ``sha256(query_text)``.

    The plan calls for caching repeat queries (``qwen3-embedding:0.6b``
    is ~80-150 ms locally). A small per-process LRU is enough — the
    classifier sees one query at a time and the live exemplar set is
    queried fresh on each call (no need to invalidate on inserts).
    """

    def __init__(self, max_entries: int = EMBED_CACHE_MAX_ENTRIES) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, list[float]] = OrderedDict()

    def get(self, query: str) -> list[float] | None:
        key = self._key(query)
        value = self._store.get(key)
        if value is None:
            return None
        self._store.move_to_end(key)
        return value

    def put(self, query: str, embedding: list[float]) -> None:
        if not embedding:
            return
        key = self._key(query)
        self._store[key] = embedding
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    @staticmethod
    def _key(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()


# ── Classifier ────────────────────────────────────────────────────────────────


class SemanticIntentClassifier:
    """k-NN intent classifier over ``router_exemplars`` (read-only).

    Construction takes a DB session factory (callable returning an
    ``async with``-able session) and an async embedding function — the
    same shape ``router_exemplars.load_exemplars`` accepts. Production
    callers wire ``ModelClient.embed_router``; tests pass a fake.

    The classifier is stateless aside from the embedding cache; sharing
    a single instance process-wide is the intended pattern.
    """

    def __init__(
        self,
        *,
        db_factory: DbFactory,
        embed_fn: EmbedFn,
        k: int = K_NEIGHBOURS,
        ood_distance_threshold: float = OOD_DISTANCE_THRESHOLD,
        min_confidence: float = MIN_CONFIDENCE,
        ambiguity_runner_up_threshold: float = AMBIGUITY_RUNNER_UP_THRESHOLD,
        cache: _EmbeddingCache | None = None,
    ) -> None:
        if k <= 0:
            raise ValueError("k must be positive")
        self._db_factory = db_factory
        self._embed_fn = embed_fn
        self._k = k
        self._ood = ood_distance_threshold
        self._min_conf = min_confidence
        self._ambig_runner_up = ambiguity_runner_up_threshold
        self._cache = cache if cache is not None else _EmbeddingCache()

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    async def classify(self, query: str) -> ClassificationResult:
        """Classify ``query`` against the live exemplar table.

        Defers (``should_clarify=True``, ``intent_label=None``) on:
          - Empty / whitespace-only ``query``.
          - Local embedder failure (``embed_fn`` raises).
          - Empty exemplar table (no neighbours returned).
          - Top-1 distance > ``ood_distance_threshold`` (OOD).
          - Winner confidence < ``min_confidence`` AND runner-up
            confidence > ``ambiguity_runner_up_threshold`` (ambiguous).

        On success: returns the highest-weighted label, with the full
        neighbour list and runner-up surfaced for shadow logging.
        """
        if not isinstance(query, str) or not query.strip():
            return _empty_result(defer_reason="empty_query")

        embedding = self._cache.get(query)
        if embedding is None:
            try:
                embedding = await self._embed_fn(query)
            except Exception as exc:  # noqa: BLE001 — local Ollama is best-effort
                logger.warning(
                    "semantic_router_embed_failed",
                    query_preview=query[:80],
                    error=str(exc),
                )
                return _empty_result(defer_reason="embed_failed")
            if not embedding:
                return _empty_result(defer_reason="embed_failed")
            self._cache.put(query, embedding)

        neighbours = await self._fetch_neighbours(embedding)
        if not neighbours:
            return _empty_result(defer_reason="no_exemplars")

        top_distance = neighbours[0].distance
        is_ood = top_distance > self._ood

        weights_by_label: dict[str, float] = {}
        for n in neighbours:
            weights_by_label[n.intent_label] = (
                weights_by_label.get(n.intent_label, 0.0)
                + 1.0 / (KERNEL_EPSILON + n.distance)
            )

        ranked = sorted(weights_by_label.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(weights_by_label.values()) or 1.0
        winner_label, winner_weight = ranked[0]
        winner_conf = winner_weight / total

        if len(ranked) > 1:
            runner_up_label, runner_up_weight = ranked[1]
            runner_up_conf = runner_up_weight / total
        else:
            runner_up_label = None
            runner_up_conf = 0.0

        is_ambiguous = (
            winner_conf < self._min_conf and runner_up_conf > self._ambig_runner_up
        )

        defer_reason: str | None = None
        if is_ood:
            defer_reason = "ood"
        elif is_ambiguous:
            defer_reason = "ambiguous"

        should_clarify = is_ood or is_ambiguous

        return ClassificationResult(
            intent_label=None if should_clarify else winner_label,
            confidence=winner_conf,
            top_distance=top_distance,
            runner_up_label=runner_up_label,
            runner_up_confidence=runner_up_conf,
            is_ood=is_ood,
            is_ambiguous=is_ambiguous,
            should_clarify=should_clarify,
            defer_reason=defer_reason,
            neighbours=neighbours,
        )

    async def _fetch_neighbours(self, embedding: list[float]) -> list[Neighbour]:
        distance = RouterExemplar.embedding.cosine_distance(embedding)
        stmt = (
            select(
                RouterExemplar.id,
                RouterExemplar.intent_label,
                RouterExemplar.tier,
                distance.label("distance"),
            )
            .where(RouterExemplar.embedding.is_not(None))
            .where(RouterExemplar.archived_at.is_(None))
            .order_by(distance)
            .limit(self._k)
        )
        async with self._db_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [
            Neighbour(
                exemplar_id=int(row_id),
                intent_label=str(label),
                tier=str(tier),
                distance=float(dist),
            )
            for row_id, label, tier, dist in rows
        ]


# ── SemanticRouter facade ─────────────────────────────────────────────────────


_INTENT_LABEL_TO_ENUM: dict[str, IntentType] = {
    "capability_check": IntentType.CAPABILITY_CHECK,
    "inbox_summary": IntentType.INBOX_SUMMARY,
    "action_items": IntentType.ACTION_ITEMS,
    "person_lookup": IntentType.PERSON_LOOKUP,
    "conversation_lookup": IntentType.CONVERSATION_LOOKUP,
    "schedule_lookup": IntentType.SCHEDULE_LOOKUP,
    "cross_source_triage": IntentType.CROSS_SOURCE_TRIAGE,
    "general_chat": IntentType.GENERAL_CHAT,
    "unsupported_capability": IntentType.UNSUPPORTED_CAPABILITY,
    "web_lookup": IntentType.WEB_LOOKUP,
}

#: Intents whose default action is to assemble an answer from already-loaded
#: context (life context, capability registry, prior turn) — no tool calls.
#: ``UNSUPPORTED_CAPABILITY`` is included as a defense-in-depth safety: even if a
#: query about an unintegrated subsystem (Health, Meal log, etc.) bypasses the
#: deterministic keyword intercept in core.py, this intent ensures the router
#: produces a context-only refusal rather than dispatching tools that don't exist.
_ANSWER_FROM_CONTEXT_INTENTS: frozenset[IntentType] = frozenset({
    IntentType.CAPABILITY_CHECK,
    IntentType.GENERAL_CHAT,
    IntentType.UNSUPPORTED_CAPABILITY,
})


def _intent_to_action_mode(intent: IntentType) -> ActionMode:
    if intent in _ANSWER_FROM_CONTEXT_INTENTS:
        return ActionMode.ANSWER_FROM_CONTEXT
    if intent == IntentType.UNKNOWN:
        return ActionMode.ASK_CLARIFYING_QUESTION
    return ActionMode.CALL_TOOLS


class SemanticRouter:
    """Phase 2 facade — fuses semantic intent classification with explicit slots.

    The router owns three deterministic pieces:

    * ``split_multi_intent`` — fragment the user message into independent
      intent-bearing clauses.
    * ``SemanticIntentClassifier`` — k-NN intent label per fragment.
    * ``slot_extractors`` — time scope, entity targets, target sources,
      filesystem path per fragment.

    Per fragment it produces one :class:`RoutingDecision`. Multi-intent
    queries return one decision per fragment (preserving order); single
    intents return a one-element list. Callers that only care about the
    primary intent can use :py:meth:`route_first`.

    The returned :class:`RoutingDecision` matches the legacy
    ``QueryRouter`` shape so shadow-mode logging in ``core.py`` can compare
    them field-by-field. ``confidence`` carries the classifier's weighted
    vote score; ``reasoning`` summarises the path taken (intent label,
    defer reason, fragment index).
    """

    def __init__(
        self,
        *,
        classifier: SemanticIntentClassifier,
    ) -> None:
        self._classifier = classifier

    async def route(
        self,
        user_message: str,
        recent_user_messages: list[str] | None = None,  # noqa: ARG002 — reserved for parity with QueryRouter
    ) -> list[RoutingDecision]:
        """Return one :class:`RoutingDecision` per intent fragment.

        Always returns a non-empty list. Empty / non-string input yields a
        single ASK_CLARIFYING_QUESTION decision so downstream code can
        treat the result uniformly.
        """
        if not isinstance(user_message, str) or not user_message.strip():
            return [self._empty_decision()]

        fragments = split_multi_intent(user_message)
        decisions: list[RoutingDecision] = []
        for index, fragment in enumerate(fragments):
            classification = await self._classifier.classify(fragment)
            decisions.append(
                self._build_decision(fragment, classification, fragment_index=index)
            )
        if not decisions:
            decisions.append(self._empty_decision())

        if len(decisions) > 1:
            logger.info(
                "semantic_router_multi",
                n_intents=len(decisions),
                intents=[d.intent_type.value for d in decisions],
                message_preview=user_message[:100],
            )
        return decisions

    async def route_first(
        self,
        user_message: str,
        recent_user_messages: list[str] | None = None,
    ) -> RoutingDecision:
        """Convenience wrapper returning the first fragment's decision."""
        decisions = await self.route(user_message, recent_user_messages)
        return decisions[0]

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _empty_decision() -> RoutingDecision:
        return RoutingDecision(
            intent_type=IntentType.UNKNOWN,
            target_sources=[],
            action_mode=ActionMode.ASK_CLARIFYING_QUESTION,
            confidence=0.0,
            reasoning="empty_query",
        )

    @staticmethod
    def _build_decision(
        fragment: str,
        result: ClassificationResult,
        *,
        fragment_index: int,
    ) -> RoutingDecision:
        time_scope = extract_time_scope(fragment)
        entity_targets = extract_entity_targets(fragment)
        target_sources = extract_target_sources(fragment)
        path = extract_filesystem_path(fragment)
        if path and "filesystem" not in target_sources:
            target_sources = [*target_sources, "filesystem"]

        if result.should_clarify or result.intent_label is None:
            intent_type = IntentType.UNKNOWN
            reasoning = (
                f"semantic_defer:{result.defer_reason or 'unknown'} "
                f"top_distance={result.top_distance:.3f} "
                f"runner_up={result.runner_up_label}"
            )
        else:
            intent_type = _INTENT_LABEL_TO_ENUM.get(
                result.intent_label, IntentType.GENERAL_CHAT
            )
            reasoning = (
                f"semantic:{result.intent_label} "
                f"conf={result.confidence:.3f} "
                f"top_distance={result.top_distance:.3f}"
            )

        action_mode = _intent_to_action_mode(intent_type)

        if intent_type == IntentType.CAPABILITY_CHECK and not target_sources:
            target_sources = ["all"]

        if fragment_index > 0:
            reasoning = f"fragment[{fragment_index}] {reasoning}"

        return RoutingDecision(
            intent_type=intent_type,
            target_sources=target_sources,
            action_mode=action_mode,
            time_scope=time_scope,
            entity_targets=entity_targets,
            needs_clarification=action_mode == ActionMode.ASK_CLARIFYING_QUESTION,
            confidence=result.confidence,
            reasoning=reasoning,
        )
