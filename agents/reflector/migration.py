"""Idempotent post-`create_all` SQL for the `reflections` table.

Mirrors the shape of `agent/traces/migration.py`: SQLAlchemy's
`create_all` cannot express partial HNSW indexes or a GIN index on a
uuid[] column, so they are applied here. Safe to re-run on every
startup.

This module is imported by `agents.reflector.main` and applied once
on reflector boot. It does NOT touch the `traces` table — that
remains the trace store's responsibility. The reflector's grants are
left to a follow-up (the operator-level note in the #38 PR also
applies here): until per-archetype Postgres roles land, the reflector
process connects with the same credentials as Pepper Core.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def apply_reflections_migration(conn: AsyncConnection) -> None:
    """Apply post-create_all DDL for the `reflections` table.

    Idempotent. Each statement uses `IF NOT EXISTS` so re-running on
    startup is a no-op after the first successful boot.
    """
    # Partial HNSW index: pgvector rejects nulls in HNSW. Reflections
    # may briefly land with NULL embedding (LLM produced text but the
    # embed call failed and we did not block on it). The partial
    # predicate keeps the index small and skips not-yet-embedded rows.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_reflections_embedding
            ON reflections USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
            """
        )
    )
    # GIN on parent_reflection_ids — supports the "find rollups that
    # contain this daily reflection" query #40 needs. Cheap to add now;
    # avoids a follow-up migration when #40 lands.
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS idx_reflections_parents
            ON reflections USING gin (parent_reflection_ids)
            """
        )
    )
