"""Read-only inspection CLI over ``routing_events``.

Phase 1 Task 6 of docs/SEMANTIC_ROUTER_MIGRATION.md. Lets the operator
slice the routing-events table from the host shell without poking SQL by
hand. Used during Phase 1 to monitor heuristic quality and during Phase 2+
to inspect shadow/regex divergence.

Modes (mutually exclusive):

* ``--histogram-by-intent`` — counts per ``regex_decision_intent``.
* ``--histogram-by-success-signal`` — proceed/re-ask/abandon rates derived
  from ``success_signal`` (NULL bucketed as ``unset``).
* ``--divergence`` — rows where the Phase 2+ shadow classifier disagreed
  with the regex router (both columns non-NULL, intents differ).
* ``--query "<text>"`` — k nearest embedded queries from the past via
  pgvector cosine distance. ``-k`` controls neighbours, default 10.

``--since YYYY-MM-DD`` (or any ISO8601 timestamp) bounds any of the above.
``--json`` switches the output to one JSON object/array on stdout for
piping into other tools.

Privacy: this CLI only reads the local ``routing_events`` table. The
``--query`` mode embeds the user-supplied query string locally via the
same Ollama-backed ``ModelClient.embed`` Pepper already uses. Nothing
leaves the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy import func, select

from agent.models import RoutingEvent

logger = structlog.get_logger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]
DbFactory = Callable[[], Any]


def _parse_since(raw: str) -> datetime:
    """Accept ``YYYY-MM-DD`` or any ISO8601 timestamp.

    Bare dates are interpreted as midnight UTC for predictable bounds.
    """
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be YYYY-MM-DD or ISO8601, got: {raw!r}"
        ) from exc


@dataclass
class _HistogramRow:
    bucket: str
    count: int

    def as_dict(self) -> dict:
        return {"bucket": self.bucket, "count": self.count}


async def histogram_by_intent(
    db_factory: DbFactory, *, since: datetime | None = None
) -> list[_HistogramRow]:
    column = RoutingEvent.regex_decision_intent
    stmt = select(func.coalesce(column, "<null>"), func.count()).group_by(column)
    if since is not None:
        stmt = stmt.where(RoutingEvent.timestamp >= since)
    stmt = stmt.order_by(func.count().desc())
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [_HistogramRow(bucket=str(b), count=int(c)) for b, c in rows]


async def histogram_by_success_signal(
    db_factory: DbFactory, *, since: datetime | None = None
) -> list[_HistogramRow]:
    column = RoutingEvent.success_signal
    stmt = select(func.coalesce(column, "unset"), func.count()).group_by(column)
    if since is not None:
        stmt = stmt.where(RoutingEvent.timestamp >= since)
    stmt = stmt.order_by(func.count().desc())
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [_HistogramRow(bucket=str(b), count=int(c)) for b, c in rows]


@dataclass
class _DivergenceRow:
    timestamp: datetime
    query_text: str
    regex_intent: str | None
    shadow_intent: str | None
    regex_confidence: float | None
    shadow_confidence: float | None

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "query_text": self.query_text,
            "regex_intent": self.regex_intent,
            "shadow_intent": self.shadow_intent,
            "regex_confidence": self.regex_confidence,
            "shadow_confidence": self.shadow_confidence,
        }


async def divergence(
    db_factory: DbFactory, *, since: datetime | None = None, limit: int = 200
) -> list[_DivergenceRow]:
    stmt = (
        select(
            RoutingEvent.timestamp,
            RoutingEvent.query_text,
            RoutingEvent.regex_decision_intent,
            RoutingEvent.shadow_decision_intent,
            RoutingEvent.regex_decision_confidence,
            RoutingEvent.shadow_decision_confidence,
        )
        .where(RoutingEvent.shadow_decision_intent.is_not(None))
        .where(RoutingEvent.regex_decision_intent.is_not(None))
        .where(
            RoutingEvent.shadow_decision_intent != RoutingEvent.regex_decision_intent
        )
        .order_by(RoutingEvent.timestamp.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(RoutingEvent.timestamp >= since)
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [
        _DivergenceRow(
            timestamp=ts,
            query_text=q,
            regex_intent=ri,
            shadow_intent=si,
            regex_confidence=rc,
            shadow_confidence=sc,
        )
        for ts, q, ri, si, rc, sc in rows
    ]


@dataclass
class _NeighbourRow:
    timestamp: datetime
    query_text: str
    regex_intent: str | None
    distance: float

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "query_text": self.query_text,
            "regex_intent": self.regex_intent,
            "distance": self.distance,
        }


async def nearest_queries(
    db_factory: DbFactory,
    embed_fn: EmbedFn,
    query: str,
    *,
    k: int = 10,
    since: datetime | None = None,
) -> list[_NeighbourRow]:
    embedding = await embed_fn(query)
    distance = RoutingEvent.query_embedding.cosine_distance(embedding)
    stmt = (
        select(
            RoutingEvent.timestamp,
            RoutingEvent.query_text,
            RoutingEvent.regex_decision_intent,
            distance.label("distance"),
        )
        .where(RoutingEvent.query_embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    )
    if since is not None:
        stmt = stmt.where(RoutingEvent.timestamp >= since)
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [
        _NeighbourRow(
            timestamp=ts, query_text=q, regex_intent=ri, distance=float(d)
        )
        for ts, q, ri, d in rows
    ]


def _format_histogram(rows: list[_HistogramRow], *, header: str) -> str:
    if not rows:
        return f"{header}\n  (no rows)"
    total = sum(r.count for r in rows) or 1
    width = max(len(r.bucket) for r in rows)
    lines = [header]
    for row in rows:
        pct = 100.0 * row.count / total
        lines.append(f"  {row.bucket:<{width}}  {row.count:>6}  ({pct:5.1f}%)")
    lines.append(f"  {'TOTAL':<{width}}  {total:>6}")
    return "\n".join(lines)


def _format_divergence(rows: list[_DivergenceRow]) -> str:
    if not rows:
        return "divergence: (no rows — shadow classifier likely not yet wired)"
    lines = ["divergence (most recent first):"]
    for r in rows:
        rc = "—" if r.regex_confidence is None else f"{r.regex_confidence:.2f}"
        sc = "—" if r.shadow_confidence is None else f"{r.shadow_confidence:.2f}"
        lines.append(
            f"  {r.timestamp.isoformat()}  "
            f"regex={r.regex_intent}({rc})  "
            f"shadow={r.shadow_intent}({sc})  "
            f"{r.query_text[:80]}"
        )
    return "\n".join(lines)


def _format_neighbours(rows: list[_NeighbourRow], *, query: str) -> str:
    if not rows:
        return f"nearest to {query!r}: (no embedded rows in scope)"
    lines = [f"nearest to {query!r}:"]
    for r in rows:
        lines.append(
            f"  d={r.distance:.4f}  {r.timestamp.isoformat()}  "
            f"intent={r.regex_intent}  {r.query_text[:80]}"
        )
    return "\n".join(lines)


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_logs: DB session factory missing after init_db")
        return 2

    since = args.since

    if args.histogram_by_intent:
        rows = await histogram_by_intent(factory, since=since)
        if args.json:
            print(json.dumps([r.as_dict() for r in rows], indent=2))
        else:
            print(_format_histogram(rows, header="histogram by regex_decision_intent:"))
        return 0

    if args.histogram_by_success_signal:
        rows = await histogram_by_success_signal(factory, since=since)
        if args.json:
            print(json.dumps([r.as_dict() for r in rows], indent=2))
        else:
            print(_format_histogram(rows, header="histogram by success_signal:"))
        return 0

    if args.divergence:
        rows = await divergence(factory, since=since, limit=args.limit)
        if args.json:
            print(json.dumps([r.as_dict() for r in rows], indent=2))
        else:
            print(_format_divergence(rows))
        return 0

    if args.query is not None:
        from agent.llm import ModelClient

        llm = ModelClient(settings)
        rows = await nearest_queries(
            factory, llm.embed_router, args.query, k=args.k, since=since
        )
        if args.json:
            print(json.dumps([r.as_dict() for r in rows], indent=2))
        else:
            print(_format_neighbours(rows, query=args.query))
        return 0

    print("router_logs: pick one of --histogram-by-intent, "
          "--histogram-by-success-signal, --divergence, --query")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Pepper's routing_events table.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--histogram-by-intent",
        action="store_true",
        help="Counts grouped by regex_decision_intent.",
    )
    mode.add_argument(
        "--histogram-by-success-signal",
        action="store_true",
        help="Counts grouped by derived success_signal (NULL → unset).",
    )
    mode.add_argument(
        "--divergence",
        action="store_true",
        help="Rows where shadow and regex classifiers disagree.",
    )
    mode.add_argument(
        "--query",
        metavar="TEXT",
        default=None,
        help="Find the k nearest embedded queries to TEXT (cosine distance).",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Bound results to timestamp >= this value (YYYY-MM-DD or ISO8601).",
    )
    parser.add_argument(
        "-k",
        type=int,
        default=10,
        help="Neighbours to return for --query (default 10).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Row cap for --divergence (default 200).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON document instead of formatted text.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
