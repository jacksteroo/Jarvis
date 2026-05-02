"""Bootstrap loader skeleton for the semantic router's exemplar table.

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. Given a stream of seed
records (query, intent, tier, optional provenance note), this module
embeds each query locally with ``qwen3-embedding:0.6b`` and inserts it
into ``router_exemplars``. The k-NN classifier and shadow-mode wiring
land on top of this in subsequent runs.

Privacy: seeds are local-only inputs; embeddings are local Ollama
calls. No data leaves the machine.

Idempotent: the unique partial index on ``(query_text, intent_label,
tier) WHERE archived_at IS NULL`` makes re-runs safe — the loader
short-circuits when a live row with the same key already exists.

Skeleton scope: this run delivers the data path only — table, types,
loader, idempotency, embed-failure tolerance, and tests. Tier
hierarchy semantics (eviction priority, confirmation promotion,
nightly rebuild) are Phase 4 work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import structlog
from sqlalchemy import select

from agent.models import RouterExemplar

logger = structlog.get_logger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]
DbFactory = Callable[[], Any]

# Phase 2 bootstrap tiers per migration plan §"Bootstrap exemplars (tiered)".
# Order is descending trust — `platinum` > `gold` > `silver` > `manual`.
# `manual` is the never-evict tier from the Phase 4 retention rules.
VALID_TIERS: frozenset[str] = frozenset({"platinum", "gold", "silver", "manual"})


@dataclass(frozen=True)
class ExemplarSeed:
    """Seed row destined for ``router_exemplars``.

    Field names mirror the DB columns (`query_text`, `intent_label`, `tier`)
    so that Python ↔ SQL ↔ JSONL all use the same vocabulary. Earlier
    revisions exposed `intent` here while the column was `intent_label`,
    which trained both humans and LLMs to type `SELECT intent FROM
    router_exemplars` — that mismatch is what this rename closes.
    """

    query: str
    intent_label: str
    tier: str
    source_note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("ExemplarSeed.query must be non-empty str")
        if not isinstance(self.intent_label, str) or not self.intent_label.strip():
            raise ValueError("ExemplarSeed.intent_label must be non-empty str")
        if self.tier not in VALID_TIERS:
            raise ValueError(
                f"ExemplarSeed.tier must be one of {sorted(VALID_TIERS)}, "
                f"got {self.tier!r}"
            )

    @classmethod
    def from_dict(cls, raw: dict) -> "ExemplarSeed":
        """Build from a dict, accepting either ``intent_label`` or legacy ``intent``.

        JSONL seed files are migrating to ``intent_label`` to match the DB
        column. This shim keeps older artefacts (backups, third-party seed
        dumps) loadable. New writers MUST use ``intent_label``.
        """
        data = dict(raw)
        if "intent_label" not in data and "intent" in data:
            data["intent_label"] = data.pop("intent")
        elif "intent" in data:
            data.pop("intent", None)
        allowed = {"query", "intent_label", "tier", "source_note"}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class LoadResult:
    scanned: int = 0
    inserted: int = 0
    skipped_duplicate: int = 0
    skipped_invalid: int = 0
    embed_failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "inserted": self.inserted,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_invalid": self.skipped_invalid,
            "embed_failures": self.embed_failures,
        }


async def _row_already_present(session, *, seed: ExemplarSeed) -> bool:
    stmt = (
        select(RouterExemplar.id)
        .where(RouterExemplar.query_text == seed.query)
        .where(RouterExemplar.intent_label == seed.intent_label)
        .where(RouterExemplar.tier == seed.tier)
        .where(RouterExemplar.archived_at.is_(None))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def load_exemplars(
    seeds: Iterable[ExemplarSeed | dict],
    *,
    db_factory: DbFactory,
    embed_fn: EmbedFn,
    dry_run: bool = False,
) -> LoadResult:
    """Insert seed exemplars into ``router_exemplars``.

    Each seed is embedded with the caller-supplied ``embed_fn`` (typically
    ``ModelClient.embed_router``). Embed failures do not abort the run —
    the row lands with ``embedding = NULL`` and ``embed_failures``
    increments. Phase 2's nightly maintenance job is responsible for
    re-embedding nulls; the classifier ignores rows without embeddings.

    Idempotency: skips any seed whose ``(query_text, intent_label, tier)`` already
    has a non-archived row. The unique partial index in ``init_db`` is
    a belt-and-suspenders second line if the in-Python check races.

    `dict` inputs are coerced via ``ExemplarSeed(**dict)``; this lets
    callers stream JSONL without importing the dataclass.
    """
    result = LoadResult()

    async with db_factory() as session:
        for raw in seeds:
            result.scanned += 1
            try:
                seed = raw if isinstance(raw, ExemplarSeed) else ExemplarSeed.from_dict(raw)
            except (TypeError, ValueError) as exc:
                result.skipped_invalid += 1
                logger.warning(
                    "router_exemplars_skip_invalid",
                    raw_preview=str(raw)[:120],
                    error=str(exc),
                )
                continue

            if await _row_already_present(session, seed=seed):
                result.skipped_duplicate += 1
                continue

            embedding: list[float] | None = None
            try:
                embedding = await embed_fn(seed.query)
            except Exception as exc:  # noqa: BLE001 — local Ollama is best-effort
                result.embed_failures += 1
                logger.warning(
                    "router_exemplars_embed_failed",
                    query_preview=seed.query[:80],
                    intent_label=seed.intent_label,
                    tier=seed.tier,
                    error=str(exc),
                )

            row = RouterExemplar(
                query_text=seed.query,
                intent_label=seed.intent_label,
                tier=seed.tier,
                source_note=seed.source_note,
                embedding=embedding,
            )

            if dry_run:
                result.inserted += 1
                continue

            session.add(row)
            await session.commit()
            result.inserted += 1

    logger.info("router_exemplars_load_done", **result.as_dict())
    return result


async def count_by_tier(session) -> dict[str, int]:
    """Return live (non-archived) exemplar counts per tier — for ops/CLI."""
    from sqlalchemy import func as sql_func

    stmt = (
        select(RouterExemplar.tier, sql_func.count(RouterExemplar.id))
        .where(RouterExemplar.archived_at.is_(None))
        .group_by(RouterExemplar.tier)
    )
    rows = (await session.execute(stmt)).all()
    return {tier: int(count) for tier, count in rows}
