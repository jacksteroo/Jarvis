"""Schema tests for the routing_events table (Phase 1 Task 1).

These tests validate the SQLAlchemy model's column shape and the index DDL we
issue in `init_db`. They run without a live Postgres — schema definition is
inspected via SQLAlchemy's metadata.
"""

from __future__ import annotations

import inspect

from sqlalchemy import ARRAY, DateTime, Float, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

from agent import db as db_module
from agent.models import RoutingEvent


EXPECTED_COLUMNS = {
    "id": Integer,
    "timestamp": DateTime,
    "query_text": Text,
    "query_embedding": Vector,
    "regex_decision_intent": Text,
    "regex_decision_sources": ARRAY,
    "regex_decision_confidence": Float,
    "tools_actually_called": JSONB,
    "llm_model": Text,
    "latency_ms": Integer,
    "success_signal": Text,
    "success_signal_set_at": DateTime,
    "shadow_decision_intent": Text,
    "shadow_decision_confidence": Float,
    "user_session_id": Text,
}


def test_table_name_is_routing_events():
    assert RoutingEvent.__tablename__ == "routing_events"


def test_all_expected_columns_present():
    cols = {c.name for c in RoutingEvent.__table__.columns}
    assert cols == set(EXPECTED_COLUMNS), f"column mismatch: {cols ^ set(EXPECTED_COLUMNS)}"


def test_column_types_match_spec():
    for name, expected_type in EXPECTED_COLUMNS.items():
        col = RoutingEvent.__table__.columns[name]
        assert isinstance(col.type, expected_type), (
            f"{name}: got {type(col.type).__name__}, expected {expected_type.__name__}"
        )


def test_query_embedding_is_1024_dim():
    """Phase 2 Task 0: switched router embedder from nomic-embed-text (768)
    to qwen3-embedding:0.6b (1024)."""
    col = RoutingEvent.__table__.columns["query_embedding"]
    assert col.type.dim == 1024


def test_required_nullability():
    cols = RoutingEvent.__table__.columns
    # Per spec: id, timestamp, query_text are required; everything else nullable.
    assert cols["query_text"].nullable is False
    assert cols["timestamp"].nullable is False
    for name in EXPECTED_COLUMNS:
        if name in {"id", "timestamp", "query_text"}:
            continue
        assert cols[name].nullable is True, f"{name} should be nullable"


def test_btree_indexes_on_filter_columns():
    """success_signal and user_session_id need quick filter access; timestamp is
    covered by the explicit DESC index issued in init_db."""
    indexed = {c.name for c in RoutingEvent.__table__.columns if c.index}
    assert {"success_signal", "user_session_id"}.issubset(indexed)


def test_init_db_creates_hnsw_and_desc_indexes():
    """init_db source must issue the routing_events HNSW + timestamp-DESC indexes."""
    source = inspect.getsource(db_module.init_db)
    assert "idx_routing_events_query_embedding" in source
    assert "USING hnsw (query_embedding vector_cosine_ops)" in source
    assert "idx_routing_events_timestamp_desc" in source
    assert "(timestamp DESC)" in source
