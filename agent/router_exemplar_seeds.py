"""Seed sources for the semantic router's exemplar table.

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. Three tiered source
iterators map durable artifacts to ``ExemplarSeed`` records that
``router_exemplars.load_exemplars`` ingests:

- **Platinum** — Phase 0 battery rows the LLM judge marked ``success ==
  False``. The judged ``expected_intent`` is the label the regex router
  *should* have produced; that's the highest-trust signal we have.
- **Gold** — Phase 0 battery rows the judge marked ``success == True``.
  Regex behaved correctly; ``expected_intent`` carries forward as a
  verified positive exemplar.
- **Silver** — Phase 1 ``routing_events`` rows where
  ``success_signal == 'confirmed'`` and ``regex_decision_intent`` is set.
  Inherits regex-router bias by construction; the platinum tier is meant
  to override silver in the eviction heuristic when they conflict.

Privacy: every input is local — JSONL files Pepper wrote and the local
``routing_events`` table. No data leaves the machine.

Idempotency lives in ``load_exemplars`` (unique partial index +
in-Python pre-check); these iterators are pure producers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import structlog
from sqlalchemy import select

from agent.models import RoutingEvent
from agent.router_exemplars import (
    DbFactory,
    EmbedFn,
    ExemplarSeed,
    LoadResult,
    load_exemplars,
)

logger = structlog.get_logger(__name__)


@dataclass
class BootstrapResult:
    platinum: LoadResult
    gold: LoadResult
    silver: LoadResult
    manual: LoadResult | None = None

    def as_dict(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {
            "platinum": self.platinum.as_dict(),
            "gold": self.gold.as_dict(),
            "silver": self.silver.as_dict(),
        }
        if self.manual is not None:
            out["manual"] = self.manual.as_dict()
        return out


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "router_exemplar_seeds_bad_jsonl_line",
                    path=str(path),
                    error=str(exc),
                )


def _row_to_seed(
    row: dict[str, Any],
    *,
    expect_success: bool,
    tier: str,
    source_path: Path,
) -> ExemplarSeed | None:
    """Map a Phase 0 battery-classification row to a seed.

    Returns ``None`` for rows that don't match the requested polarity
    (``expect_success``), are missing fields, or carry an empty intent.
    """
    verdict = row.get("verdict") or {}
    success = bool(verdict.get("success"))
    if success != expect_success:
        return None

    query = row.get("query")
    intent_label = row.get("expected_intent")
    if not isinstance(query, str) or not query.strip():
        return None
    if not isinstance(intent_label, str) or not intent_label.strip():
        return None

    battery_id = row.get("battery_id") or row.get("id") or "?"
    note = f"phase0_{tier}:{source_path.name}:{battery_id}"
    return ExemplarSeed(
        query=query.strip(),
        intent_label=intent_label.strip(),
        tier=tier,
        source_note=note,
    )


def iter_phase0_platinum(jsonl_path: Path | str) -> Iterator[ExemplarSeed]:
    """Yield platinum seeds from a Phase 0 battery_classification JSONL."""
    path = Path(jsonl_path)
    for row in _iter_jsonl(path):
        seed = _row_to_seed(
            row, expect_success=False, tier="platinum", source_path=path
        )
        if seed is not None:
            yield seed


def iter_manual_exemplars(jsonl_path: Path | str) -> Iterator[ExemplarSeed]:
    """Yield manual-tier seeds from a hand-labeled JSONL.

    Manual exemplars are 5-10 hand-authored queries per Phase 0 "Top 10
    pattern" (see ``logs/router_audit/audit_2026-04-27.md``). Each row
    carries ``query`` and ``intent_label`` (legacy ``intent`` accepted for
    older seed files); an optional ``pattern_id`` is folded into the
    source note so the audit trail points back at the pattern that
    motivated it.

    Manual is the never-evict tier from the Phase 4 retention rules.
    """
    path = Path(jsonl_path)
    for row in _iter_jsonl(path):
        query = row.get("query")
        intent_label = (
            row.get("intent_label")
            or row.get("intent")
            or row.get("expected_intent")
        )
        if not isinstance(query, str) or not query.strip():
            continue
        if not isinstance(intent_label, str) or not intent_label.strip():
            continue
        pattern_id = row.get("pattern_id")
        suffix = f"pattern_{pattern_id}" if pattern_id is not None else "ad_hoc"
        yield ExemplarSeed(
            query=query.strip(),
            intent_label=intent_label.strip(),
            tier="manual",
            source_note=f"manual:{path.name}:{suffix}",
        )


def iter_phase0_gold(jsonl_path: Path | str) -> Iterator[ExemplarSeed]:
    """Yield gold seeds from a Phase 0 battery_classification JSONL."""
    path = Path(jsonl_path)
    for row in _iter_jsonl(path):
        seed = _row_to_seed(
            row, expect_success=True, tier="gold", source_path=path
        )
        if seed is not None:
            yield seed


async def iter_phase1_silver(
    db_factory: DbFactory,
    *,
    limit: int | None = None,
) -> list[ExemplarSeed]:
    """Materialize silver seeds from confirmed Phase 1 routing_events.

    Returns a concrete list (not an async generator) because the caller
    streams it through ``load_exemplars`` which opens its own session.
    Pulling the rows up front keeps the read transaction short.
    """
    stmt = (
        select(
            RoutingEvent.id,
            RoutingEvent.query_text,
            RoutingEvent.regex_decision_intent,
        )
        .where(RoutingEvent.success_signal == "confirmed")
        .where(RoutingEvent.regex_decision_intent.isnot(None))
        .order_by(RoutingEvent.timestamp.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    seeds: list[ExemplarSeed] = []
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()

    for event_id, query_text, intent_label in rows:
        if not isinstance(query_text, str) or not query_text.strip():
            continue
        if not isinstance(intent_label, str) or not intent_label.strip():
            continue
        seeds.append(
            ExemplarSeed(
                query=query_text.strip(),
                intent_label=intent_label.strip(),
                tier="silver",
                source_note=f"phase1_silver:routing_events:{event_id}",
            )
        )
    return seeds


async def bootstrap_seeds(
    *,
    platinum_path: Path | str | None,
    gold_path: Path | str | None,
    db_factory: DbFactory,
    embed_fn: EmbedFn,
    silver_limit: int | None = None,
    manual_path: Path | str | None = None,
    dry_run: bool = False,
) -> BootstrapResult:
    """Run all three tiers through ``load_exemplars`` in order.

    Order is intentional: platinum first so its labels appear before any
    silver row that paraphrases the same query (the in-Python idempotent
    check matches on (query_text, intent_label, tier), so platinum/silver coexist
    even on identical text — but loading platinum first keeps the audit
    trail readable).
    """

    platinum_iter: Iterable[ExemplarSeed] = (
        iter_phase0_platinum(platinum_path) if platinum_path is not None else iter(())
    )
    gold_iter: Iterable[ExemplarSeed] = (
        iter_phase0_gold(gold_path) if gold_path is not None else iter(())
    )

    platinum_result = await load_exemplars(
        platinum_iter,
        db_factory=db_factory,
        embed_fn=embed_fn,
        dry_run=dry_run,
    )
    gold_result = await load_exemplars(
        gold_iter,
        db_factory=db_factory,
        embed_fn=embed_fn,
        dry_run=dry_run,
    )
    silver_seeds = await iter_phase1_silver(db_factory, limit=silver_limit)
    silver_result = await load_exemplars(
        silver_seeds,
        db_factory=db_factory,
        embed_fn=embed_fn,
        dry_run=dry_run,
    )

    manual_result: LoadResult | None = None
    if manual_path is not None:
        manual_result = await load_exemplars(
            iter_manual_exemplars(manual_path),
            db_factory=db_factory,
            embed_fn=embed_fn,
            dry_run=dry_run,
        )

    return BootstrapResult(
        platinum=platinum_result,
        gold=gold_result,
        silver=silver_result,
        manual=manual_result,
    )


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings
    from agent.llm import ModelClient

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_exemplar_seeds: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)

    result = await bootstrap_seeds(
        platinum_path=args.platinum,
        gold_path=args.gold,
        db_factory=factory,
        embed_fn=llm.embed_router,
        silver_limit=args.silver_limit,
        manual_path=args.manual,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap router_exemplars from Phase 0 + Phase 1 sources",
    )
    parser.add_argument(
        "--platinum",
        default="logs/router_audit/battery_classification_20260427T103956Z.jsonl",
        help="Phase 0 battery_classification JSONL (failures → platinum tier).",
    )
    parser.add_argument(
        "--gold",
        default="logs/router_audit/battery_classification_20260427T103956Z.jsonl",
        help="Phase 0 battery_classification JSONL (successes → gold tier).",
    )
    parser.add_argument(
        "--manual",
        default=None,
        help=(
            "Optional hand-authored JSONL of (query, intent_label) per Phase 0 "
            "Top 10 patterns. Off by default — Phase 2 iter 14 found the "
            "first canonical author conflicted with the eval-set encoding "
            "and dropped Gate 6 by 7pts. Operator must explicitly opt in "
            "after adjudication."
        ),
    )
    parser.add_argument(
        "--silver-limit",
        type=int,
        default=None,
        help="Optional cap on silver seeds pulled from routing_events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows that would be written without committing.",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
