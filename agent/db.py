from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Base — all models inherit from this
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Lazy engine / session factory — initialised once at startup via init_db()
# ---------------------------------------------------------------------------

_engine = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


async def init_db(config=None) -> None:
    """Create tables and pgvector extension.  Call once at application startup."""
    global _engine, _session_factory

    if config is None:
        from agent.config import settings as config  # type: ignore[assignment]

    _engine = create_async_engine(
        config.POSTGRES_URL,
        echo=False,
        pool_pre_ping=True,
    )

    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async with _engine.begin() as conn:
        # 1. Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # 2. Create all tables defined via Base
        await conn.run_sync(Base.metadata.create_all)

        # 3. HNSW indexes for embedding columns (idempotent)
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memory_events_embedding "
                "ON memory_events USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_conversations_embedding "
                "ON conversations USING hnsw (embedding vector_cosine_ops) "
                "WITH (m=16, ef_construction=64)"
            )
        )


def get_engine():
    """Return the initialised SQLAlchemy async engine.

    Raises RuntimeError if init_db() has not been called yet.
    """
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _engine


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Async generator providing a DB session — use as a FastAPI dependency."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        yield session
