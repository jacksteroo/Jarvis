"""Backfill ``routing_events.shadow_decision_*`` by replaying SemanticRouter.

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. Shadow-mode wiring (iter 9)
populates ``shadow_decision_intent`` / ``shadow_decision_confidence`` on
every *new* routing event, but the 230+ pre-shadow rows already in the
table are still NULL. The Phase 2 exit-criterion analysis (agreement on
regex≥0.9 set, divergence sample for adjudication, 100-query battery)
needs shadow decisions on the full historical set.

This module re-runs ``SemanticRouter.route()`` on each historical
``query_text`` and writes the max-confidence decision into the shadow
columns — the same selection rule core.py uses on the live path.

Privacy: all work is local. Embeddings come from
``qwen3-embedding:0.6b`` via Ollama. No external calls.

Idempotent: only updates rows where ``shadow_decision_intent IS NULL``.
A re-run after a partial run picks up where it left off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy import select, update

from agent.models import RoutingEvent
from agent.semantic_router import SemanticRouter

logger = structlog.get_logger(__name__)

DbFactory = Callable[[], Any]


@dataclass
class ReplayResult:
    scanned: int = 0
    updated: int = 0
    skipped_empty_query: int = 0
    classifier_errors: int = 0
    deferred: int = 0  # routed but classifier asked for clarification → UNKNOWN

    def as_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "updated": self.updated,
            "skipped_empty_query": self.skipped_empty_query,
            "classifier_errors": self.classifier_errors,
            "deferred": self.deferred,
        }


async def replay(
    *,
    db_factory: DbFactory,
    router: SemanticRouter,
    limit: int | None = None,
    dry_run: bool = False,
) -> ReplayResult:
    """Replay SemanticRouter against rows where shadow columns are NULL.

    Selection rule mirrors ``PepperCore._log_routing_event``: take the
    fragment with max ``confidence`` across the multi-intent split. A
    classifier exception leaves the row's shadow columns unchanged
    (still NULL), matching the live "shadow failure → keep regex row"
    contract.
    """
    result = ReplayResult()

    async with db_factory() as session:
        stmt = (
            select(RoutingEvent.id, RoutingEvent.query_text)
            .where(RoutingEvent.shadow_decision_intent.is_(None))
            .order_by(RoutingEvent.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await session.execute(stmt)).all()

    for row_id, query_text in rows:
        result.scanned += 1
        if not isinstance(query_text, str) or not query_text.strip():
            result.skipped_empty_query += 1
            continue

        try:
            decisions = await router.route(query_text)
        except Exception as exc:  # noqa: BLE001 — local Ollama is best-effort
            result.classifier_errors += 1
            logger.warning(
                "router_shadow_replay_classify_failed",
                row_id=int(row_id),
                error=str(exc),
            )
            continue

        if not decisions:
            result.classifier_errors += 1
            continue

        primary = max(decisions, key=lambda d: d.confidence)
        intent_value = primary.intent_type.value
        confidence = float(primary.confidence)

        if intent_value == "unknown":
            result.deferred += 1

        if dry_run:
            result.updated += 1
            continue

        async with db_factory() as session:
            await session.execute(
                update(RoutingEvent)
                .where(RoutingEvent.id == int(row_id))
                .values(
                    shadow_decision_intent=intent_value,
                    shadow_decision_confidence=confidence,
                )
            )
            await session.commit()
        result.updated += 1

    logger.info("router_shadow_replay_done", **result.as_dict())
    return result


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings
    from agent.llm import ModelClient
    from agent.semantic_router import SemanticIntentClassifier

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_shadow_replay: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)
    classifier = SemanticIntentClassifier(
        db_factory=factory,
        embed_fn=llm.embed_router,
    )
    router = SemanticRouter(classifier=classifier)

    started = time.monotonic()
    result = await replay(
        db_factory=factory,
        router=router,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    elapsed = time.monotonic() - started

    payload: dict[str, Any] = {**result.as_dict(), "elapsed_sec": round(elapsed, 2)}
    if result.scanned:
        payload["mean_ms_per_row"] = round(elapsed * 1000.0 / result.scanned, 1)
    print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Replay SemanticRouter against routing_events with NULL shadow "
            "columns and write back the max-confidence decision."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum rows to replay (default: all NULL rows).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run classifier but skip the UPDATE; report counts only.",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
