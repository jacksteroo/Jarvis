from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, BigInteger, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
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


class RoutingEvent(Base):
    """One row per chat turn: the regex router's decision, the (future) shadow
    semantic router's decision, the tools the LLM actually called, latency, and a
    derived success-signal. Per docs/SEMANTIC_ROUTER_MIGRATION.md Phase 1.

    Shadow columns (`shadow_decision_*`) are added now for forward-compat with
    Phase 2; they remain NULL until the semantic classifier comes online.
    """

    __tablename__ = "routing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    query_embedding: Mapped[Optional[list]] = mapped_column(Vector(1024), nullable=True)
    regex_decision_intent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    regex_decision_sources: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text), nullable=True
    )
    regex_decision_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tools_actually_called: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success_signal: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    success_signal_set_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    shadow_decision_intent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shadow_decision_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    user_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)
    # Outbound channel message coordinates — currently used to map Telegram
    # message reactions back to the routing event so 👍/👎 lands as an
    # explicit success_signal. NULL when the channel doesn't expose
    # reactable message ids (e.g. the HTTP API).
    outbound_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    outbound_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class RouterExemplar(Base):
    """Labeled query → intent exemplar for the semantic router's k-NN.

    Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md. The bootstrap seed comes
    in four tiers (`platinum`, `gold`, `silver`, `manual`); Phase 4 adds
    real-time confirmed exemplars and nightly evictions on top.

    Additive: evicted exemplars are timestamp-archived (`archived_at`
    NOT NULL) rather than deleted, so historical analyses still see them.
    """

    __tablename__ = "router_exemplars"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent_label: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    tier: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    embedding: Mapped[Optional[list]] = mapped_column(Vector(1024), nullable=True)
    confirmation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class AuditLog(Base):
    """Append-only log of all significant system events for the security layer."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100))
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
