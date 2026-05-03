"""Persistence layer for the `reflections` table.

Append-only, like `traces`. Each daily/weekly/monthly reflection is
its own row; the hierarchy is recorded in `parent_reflection_ids`.
The repository surface mirrors `agent.traces.repository` discipline:
no `update_*` / `delete_*` methods.

Design notes:

- The embedding is `qwen3-embedding:0.6b` at 1024 dims, matching the
  trace store. Per the issue spec, "embedded the same way traces are
  per Q1 resolution." The reflector regenerates embeddings on its own
  (it does not borrow trace embeddings).
- Reflection text is `RAW_PERSONAL` — the operator's interior voice
  shaped from raw trace content. It must never leave the box.
- `tier` is a string column to leave room for #40's `daily | weekly
  | monthly` extension without a new migration.
- `parent_reflection_ids` is a Postgres `uuid[]` so the hierarchy
  query is a single GIN-indexed lookup; #40 adds the index.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy.exc import IntegrityError
from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, deferred, mapped_column

from agent.db import Base

logger = structlog.get_logger(__name__)

# Mirrors `agent.traces.schema.EMBEDDING_DIM`. Kept as a local copy so
# the lint can remain comfortable that this module's import surface is
# narrow and so a future change to one embedding dim does not silently
# bend the other.
REFLECTION_EMBEDDING_DIM: int = 1024
REFLECTION_EMBEDDING_MODEL_DEFAULT: str = "qwen3-embedding:0.6b"

# Public tier names. Daily is the only tier shipped in #39; weekly +
# monthly are added in #40 and use the same column.
TIER_DAILY: str = "daily"
TIER_WEEKLY: str = "weekly"
TIER_MONTHLY: str = "monthly"

_VALID_TIERS: frozenset[str] = frozenset({TIER_DAILY, TIER_WEEKLY, TIER_MONTHLY})

MAX_QUERY_LIMIT: int = 1000


class DuplicateReflectionError(Exception):
    """Raised by `ReflectionRepository.append` when the
    `(tier, window_start)` uniqueness constraint fires.

    Operationally: the reflector got a second NOTIFY for the same
    local day (or a re-run after a partial failure). The right
    response is to log and skip — the previous run already produced
    the reflection for this window.
    """

    def __init__(self, tier: str, window_start: datetime) -> None:
        super().__init__(
            f"reflection already exists for tier={tier!r} "
            f"window_start={window_start.isoformat()}"
        )
        self.tier = tier
        self.window_start = window_start


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_reflection_id() -> str:
    return str(_uuid.uuid4())


# ── Dataclass contract ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reflection:
    """One persisted reflection — daily, weekly, or monthly.

    Frozen so a constructed `Reflection` is the row that will be
    persisted; no in-place mutation between construction and append.
    """

    text: str
    window_start: datetime
    window_end: datetime
    tier: str = TIER_DAILY
    reflection_id: str = field(default_factory=_new_reflection_id)
    created_at: datetime = field(default_factory=_utcnow)
    previous_reflection_id: Optional[str] = None
    parent_reflection_ids: Optional[list[str]] = None
    trace_count: int = 0
    model_used: str = ""
    prompt_version: str = "unversioned"
    embedding: Optional[list[float]] = None
    embedding_model_version: Optional[str] = None
    # Free-form bag for downstream archetypes (#41 pattern detector,
    # #42 eval) to attach structured side-data without a new column or
    # an LLM re-parse. Reserved keys agreed in this PR:
    #   - "voice_violations": list[str] populated by `prompt.voice_violations`
    #   - "trace_truncated": bool — set when the prompt cap was hit
    metadata_: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.tier not in _VALID_TIERS:
            raise ValueError(
                f"reflection tier must be one of {sorted(_VALID_TIERS)}, "
                f"got {self.tier!r}"
            )
        if self.window_end < self.window_start:
            raise ValueError("reflection window_end is before window_start")
        if self.embedding is not None and len(self.embedding) != REFLECTION_EMBEDDING_DIM:
            raise ValueError(
                f"reflection embedding must have dim {REFLECTION_EMBEDDING_DIM}, "
                f"got {len(self.embedding)}"
            )


# ── ORM mapping ──────────────────────────────────────────────────────────────


class ReflectionRow(Base):
    """Storage projection for `Reflection`. Append-only at the app layer."""

    __tablename__ = "reflections"

    reflection_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid.uuid4,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    tier: Mapped[str] = mapped_column(String(16), nullable=False, default=TIER_DAILY)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    previous_reflection_id: Mapped[Optional[_uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    # Postgres uuid[] — used by #40 to walk the hierarchy. A weekly
    # rollup's parent_reflection_ids holds the 7 daily IDs; a monthly
    # rollup holds the 4 weekly IDs.
    parent_reflection_ids: Mapped[Optional[list[_uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
    )

    trace_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_used: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="unversioned",
    )

    embedding: Mapped[Optional[list[float]]] = deferred(
        mapped_column(Vector(REFLECTION_EMBEDDING_DIM), nullable=True),
    )
    embedding_model_version: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        Index("idx_reflections_created_at", "created_at"),
        Index("idx_reflections_tier_created_at", "tier", "created_at"),
        Index("idx_reflections_window", "window_start", "window_end"),
        # One reflection per (tier, window_start). A re-run for the
        # same day collides at INSERT and the reflector logs + skips
        # rather than producing duplicate dailies that #40's rollup
        # would have to dedupe by hand.
        UniqueConstraint(
            "tier",
            "window_start",
            name="uq_reflections_tier_window_start",
        ),
    )


# ── Mapping helpers ──────────────────────────────────────────────────────────


def _reflection_to_row(r: Reflection) -> ReflectionRow:
    return ReflectionRow(
        reflection_id=_uuid.UUID(r.reflection_id),
        created_at=r.created_at,
        tier=r.tier,
        window_start=r.window_start,
        window_end=r.window_end,
        text=r.text,
        previous_reflection_id=(
            _uuid.UUID(r.previous_reflection_id)
            if r.previous_reflection_id is not None
            else None
        ),
        parent_reflection_ids=(
            [_uuid.UUID(p) for p in r.parent_reflection_ids]
            if r.parent_reflection_ids is not None
            else None
        ),
        trace_count=r.trace_count,
        model_used=r.model_used,
        prompt_version=r.prompt_version,
        embedding=r.embedding,
        embedding_model_version=r.embedding_model_version,
        metadata_=dict(r.metadata_),
    )


def _row_to_reflection(row: ReflectionRow) -> Reflection:
    return Reflection(
        reflection_id=str(row.reflection_id),
        created_at=row.created_at,
        tier=row.tier,
        window_start=row.window_start,
        window_end=row.window_end,
        text=row.text,
        previous_reflection_id=(
            str(row.previous_reflection_id)
            if row.previous_reflection_id is not None
            else None
        ),
        parent_reflection_ids=(
            [str(p) for p in row.parent_reflection_ids]
            if row.parent_reflection_ids is not None
            else None
        ),
        trace_count=row.trace_count,
        model_used=row.model_used,
        prompt_version=row.prompt_version,
        embedding=row.embedding,
        embedding_model_version=row.embedding_model_version,
        metadata_=dict(row.metadata_ or {}),
    )


# ── Repository ───────────────────────────────────────────────────────────────


class ReflectionRepository:
    """Append-only repository for reflections.

    Public surface: `append`, `get_by_id`, `latest`, `query`. No
    `update_*`, no `delete_*`. The lint test
    `agent/tests/test_reflections_repository.py` asserts via
    `hasattr` that disallowed methods do not exist.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, reflection: Reflection) -> Reflection:
        row = _reflection_to_row(reflection)
        self._session.add(row)
        try:
            await self._session.flush()
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateReflectionError(
                reflection.tier, reflection.window_start
            ) from exc
        logger.info(
            "reflection_appended",
            reflection_id=reflection.reflection_id,
            tier=reflection.tier,
            trace_count=reflection.trace_count,
        )
        return reflection

    async def get_by_id(self, reflection_id: str) -> Optional[Reflection]:
        stmt = select(ReflectionRow).where(
            ReflectionRow.reflection_id == _uuid.UUID(reflection_id),
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_reflection(row) if row is not None else None

    async def latest(self, tier: str = TIER_DAILY) -> Optional[Reflection]:
        """Return the most recent reflection of the given tier, or None.

        The reflector calls this with `tier=daily` to find "yesterday's
        reflection" so it can include continuity in the next prompt.
        """
        if tier not in _VALID_TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        stmt = (
            select(ReflectionRow)
            .where(ReflectionRow.tier == tier)
            .order_by(ReflectionRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _row_to_reflection(row) if row is not None else None

    async def query(
        self,
        *,
        tier: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
    ) -> Sequence[Reflection]:
        """Return reflections matching the filters, newest first."""
        if tier is not None and tier not in _VALID_TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        limit = max(1, min(limit, MAX_QUERY_LIMIT))

        stmt = (
            select(ReflectionRow)
            .order_by(ReflectionRow.created_at.desc())
            .limit(limit)
        )
        if tier is not None:
            stmt = stmt.where(ReflectionRow.tier == tier)
        if since is not None:
            stmt = stmt.where(ReflectionRow.created_at >= since)
        if until is not None:
            stmt = stmt.where(ReflectionRow.created_at <= until)

        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [_row_to_reflection(r) for r in rows]
