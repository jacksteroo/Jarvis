"""Phase 2 Task 0 — re-embed routing_events with qwen3-embedding:0.6b.

The migration switches the router embedder from ``nomic-embed-text`` (768-dim)
to ``qwen3-embedding:0.6b`` (1024-dim). ``init_db`` performs the schema
ALTER (drops the HNSW index, retypes ``query_embedding`` to vector(1024),
which nulls existing values). This script:

1. Snapshots ``routing_events`` to ``backups/router/<ts>/`` as a JSONL of
   id+query_text+timestamp+regex_decision_intent — enough to restore
   labels if anything goes sideways. Embeddings themselves are
   regenerable from query_text and don't need to be in the snapshot.
2. Re-embeds every row whose ``query_embedding`` is NULL.
3. Re-creates the HNSW index (``init_db`` already did this on startup,
   but if the index is missing for any reason we re-issue it).
4. Verifies no NULL ``query_embedding`` rows remain.

Run inside the pepper container (``docker compose exec pepper python -m
scripts.router_phase2_task0_reembed``). Idempotent: safe to re-run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import text

from agent import db as db_module
from agent.config import settings
from agent.llm import ModelClient

logger = structlog.get_logger(__name__)


BACKUP_ROOT = Path("backups/router")


async def _snapshot(conn) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = BACKUP_ROOT / f"phase_2_task_0_pre_reembed_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "routing_events.jsonl"

    result = await conn.execute(
        text(
            "SELECT id, timestamp, query_text, regex_decision_intent, "
            "regex_decision_confidence, success_signal, user_session_id "
            "FROM routing_events ORDER BY id"
        )
    )
    rows = result.fetchall()
    with out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                json.dumps(
                    {
                        "id": r[0],
                        "timestamp": r[1].isoformat() if r[1] else None,
                        "query_text": r[2],
                        "regex_decision_intent": r[3],
                        "regex_decision_confidence": r[4],
                        "success_signal": r[5],
                        "user_session_id": r[6],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"snapshot: {len(rows)} rows -> {out_path}", flush=True)
    return out_path


async def _re_embed(session_factory, llm: ModelClient) -> tuple[int, int]:
    embedded = 0
    failed = 0
    while True:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, query_text FROM routing_events "
                    "WHERE query_embedding IS NULL ORDER BY id LIMIT 50"
                )
            )
            batch = result.fetchall()
            if not batch:
                return embedded, failed

            for row_id, query_text in batch:
                try:
                    emb = await llm.embed_router(query_text)
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "phase2_task0_embed_failed", row_id=row_id, error=str(exc)
                    )
                    continue
                await session.execute(
                    text(
                        "UPDATE routing_events SET query_embedding = :emb "
                        "WHERE id = :id"
                    ),
                    {"emb": str(emb), "id": row_id},
                )
                embedded += 1
            await session.commit()
            print(
                f"re-embed progress: +{len(batch)} batch "
                f"({embedded} ok, {failed} failed)",
                flush=True,
            )


async def main() -> int:
    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("init_db produced no session factory", file=sys.stderr)
        return 2

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await _snapshot(conn)
        # Confirm column type before proceeding.
        atttypmod = await conn.scalar(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'public.routing_events'::regclass "
                "AND attname = 'query_embedding'"
            )
        )
        if atttypmod != 1024:
            print(
                f"abort: query_embedding has atttypmod={atttypmod} (want 1024)",
                file=sys.stderr,
            )
            return 3

    llm = ModelClient(settings)
    embedded, failed = await _re_embed(factory, llm)

    async with engine.begin() as conn:
        # init_db already creates this; reissue defensively.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_routing_events_query_embedding "
                "ON routing_events USING hnsw (query_embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        null_remaining = await conn.scalar(
            text("SELECT COUNT(*) FROM routing_events WHERE query_embedding IS NULL")
        )
        total = await conn.scalar(text("SELECT COUNT(*) FROM routing_events"))

    print(
        json.dumps(
            {
                "total_rows": total,
                "embedded_this_run": embedded,
                "embed_failures": failed,
                "null_embedding_remaining": null_remaining,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0 if null_remaining == 0 else 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
