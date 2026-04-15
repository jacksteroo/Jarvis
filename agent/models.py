from __future__ import annotations

from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from agent.db import Base


class Conversation(Base):
    """Stores every exchange with Pepper, keyed by session."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(20))  # 'user' or 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    embedding: Mapped[Optional[list]] = mapped_column(Vector(768), nullable=True)


class MemoryEvent(Base):
    """Tiered memory: 'recall' (recent, verbatim) or 'archival' (compressed)."""

    __tablename__ = "memory_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # 'recall' or 'archival'
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    importance_score: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    accessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(Vector(768), nullable=True)


class LifeContextVersion(Base):
    """Immutable history of every version of the Life Context document.

    The system never overwrites — it appends a new version row each time
    the document changes, preserving full lineage.
    """

    __tablename__ = "life_context_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())


class AuditLog(Base):
    """Append-only log of all significant system events for the security layer."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
