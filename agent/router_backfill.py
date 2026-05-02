"""Backfill ``routing_events`` from logs/chat_turns/<date>.jsonl.

Phase 1 Task 3 of docs/SEMANTIC_ROUTER_MIGRATION.md. The Phase 0 chat-turn
JSONL files are durable plaintext history; this module replays them into the
queryable ``routing_events`` table so Phase 2's exemplar mining and the
router-logs CLI can see pre-instrumentation traffic.

Privacy: the input JSONL is local-only (Pepper's own logs), embeddings are
generated locally (nomic-embed-text via Ollama). Nothing leaves the machine.

Idempotent: re-running over the same JSONL skips rows whose
``(query_text, user_session_id, timestamp)`` triple is already in the table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import structlog
from sqlalchemy import select

from agent.models import RoutingEvent
from agent.query_router import QueryRouter

logger = structlog.get_logger(__name__)

EmbedFn = Callable[[str], Awaitable[list[float]]]
DbFactory = Callable[[], Any]


@dataclass
class BackfillResult:
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


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        # JSONL writes ISO8601 with timezone; Python <3.11 needs ``Z`` swap.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_jsonl_rows(path: Path) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "router_backfill_skip_bad_jsonl",
                    path=str(path),
                    line=line_no,
                    error=str(exc),
                )


async def _row_already_present(session, *, query: str, session_id: str | None, ts: datetime) -> bool:
    """Match within a ±2s window on (query_text, session_id, timestamp).

    The inline writer's timestamp can drift up to ~100ms from the JSONL
    row's timestamp because the JSONL is stamped before embedding/DB IO
    and the DB write happens after. An exact-equality check would treat
    those as separate events and create duplicates. ±2s is large enough
    to absorb that drift and small enough that a user genuinely
    repeating the same query within the same session two seconds apart
    is implausible.
    """
    from datetime import timedelta
    window = timedelta(seconds=2)
    stmt = (
        select(RoutingEvent.id)
        .where(RoutingEvent.query_text == query)
        .where(RoutingEvent.timestamp >= ts - window)
        .where(RoutingEvent.timestamp <= ts + window)
    )
    if session_id is None:
        stmt = stmt.where(RoutingEvent.user_session_id.is_(None))
    else:
        stmt = stmt.where(RoutingEvent.user_session_id == session_id)
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def backfill_files(
    paths: list[Path],
    *,
    db_factory: DbFactory,
    embed_fn: EmbedFn,
    router: QueryRouter | None = None,
    dry_run: bool = False,
) -> BackfillResult:
    """Replay JSONL turn logs into ``routing_events``.

    Each row is re-routed through ``QueryRouter`` to recover the regex
    decision (intent/sources/confidence) the live Phase 1 Task 2 writer
    would have stamped if it had been live at the time. Embeddings are
    generated locally; an embed failure does not stop the row — it lands
    with ``query_embedding = NULL`` (mirrors the live writer's tolerance).
    """
    router = router or QueryRouter()
    result = BackfillResult()

    for path in paths:
        if not path.exists():
            logger.warning("router_backfill_missing_file", path=str(path))
            continue

        async with db_factory() as session:
            for row in _iter_jsonl_rows(path):
                result.scanned += 1
                query = row.get("query")
                ts = _parse_timestamp(row.get("timestamp"))
                if not isinstance(query, str) or not query.strip() or ts is None:
                    result.skipped_invalid += 1
                    continue

                session_id = (
                    row.get("session_id")
                    if isinstance(row.get("session_id"), str)
                    else None
                )

                if await _row_already_present(
                    session, query=query, session_id=session_id, ts=ts
                ):
                    result.skipped_duplicate += 1
                    continue

                decision = router.route(query)
                embedding: list[float] | None = None
                try:
                    embedding = await embed_fn(query)
                except Exception as exc:  # noqa: BLE001 — local Ollama is best-effort
                    result.embed_failures += 1
                    logger.warning(
                        "router_backfill_embed_failed",
                        query_preview=query[:80],
                        error=str(exc),
                    )

                tool_calls = row.get("tool_calls") or None
                latency_ms = row.get("latency_ms")
                if not isinstance(latency_ms, int):
                    latency_ms = None

                event = RoutingEvent(
                    timestamp=ts,
                    query_text=query,
                    query_embedding=embedding,
                    regex_decision_intent=decision.intent_type.value,
                    regex_decision_sources=list(decision.target_sources) or None,
                    regex_decision_confidence=decision.confidence,
                    tools_actually_called=tool_calls,
                    llm_model=row.get("model") if isinstance(row.get("model"), str) else None,
                    latency_ms=latency_ms,
                    user_session_id=session_id,
                )

                if dry_run:
                    result.inserted += 1
                    continue

                session.add(event)
                await session.commit()
                result.inserted += 1

    logger.info("router_backfill_done", **result.as_dict())
    return result


def _default_paths(since: str | None) -> list[Path]:
    log_dir = Path(__file__).resolve().parent.parent / "logs" / "chat_turns"
    if not log_dir.exists():
        return []
    files = sorted(log_dir.glob("*.jsonl"))
    if since:
        cutoff = since
        files = [p for p in files if p.stem >= cutoff]
    return files


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings
    from agent.llm import ModelClient

    paths = [Path(p) for p in args.files] if args.files else _default_paths(args.since)
    if not paths:
        print("router_backfill: no JSONL files matched")
        return 0

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_backfill: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)

    result = await backfill_files(
        paths,
        db_factory=factory,
        embed_fn=llm.embed_router,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill routing_events from logs/chat_turns/*.jsonl",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only process files whose YYYY-MM-DD stem is >= this value.",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Explicit JSONL paths (overrides --since).",
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
