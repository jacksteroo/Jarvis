"""Unit tests for PepperCore._log_routing_event (Phase 1 Task 2).

Verifies that the per-turn routing_events row is assembled correctly from a
chat_turn_logger trace snapshot and the local embedding call. The DB session
is mocked — we only assert the RoutingEvent kwargs handed to ``session.add``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.config import Settings
from agent.core import PepperCore
from agent.models import RoutingEvent
from agent.query_router import ActionMode, IntentType, RoutingDecision


def _make_db_factory():
    """Return (factory, recorder). recorder.added is the list of orm objects."""
    recorder = MagicMock()
    recorder.added = []
    recorder.committed = False

    session = MagicMock()
    session.add = lambda obj: recorder.added.append(obj)
    session.commit = AsyncMock(side_effect=lambda: setattr(recorder, "committed", True))

    @asynccontextmanager
    async def factory():
        yield session

    return factory, recorder


@pytest.fixture
def pepper(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_CONTEXT_PATH", str(tmp_path / "life.md"))
    (tmp_path / "life.md").write_text("# Life\n")
    config = Settings()
    factory, recorder = _make_db_factory()
    pepper = PepperCore(config, db_session_factory=factory)
    pepper._recorder = recorder  # type: ignore[attr-defined]
    return pepper


def _stub_shadow_router(pepper, decisions):
    """Replace pepper._router (legacy QueryRouter — the shadow post-cutover).

    Phase 3 cutover inverted the shadow direction: SemanticRouter is now
    primary, QueryRouter runs in shadow. The writer pulls its
    shadow_decision_* columns from QueryRouter.route_multi().
    """
    stub = MagicMock()
    stub.route_multi = MagicMock(return_value=decisions)
    pepper._router = stub
    return stub


@pytest.mark.asyncio
async def test_log_routing_event_persists_full_row(pepper):
    pepper.llm.embed_router = AsyncMock(return_value=[0.1] * 1024)
    _stub_shadow_router(
        pepper,
        [
            RoutingDecision(
                intent_type=IntentType.SCHEDULE_LOOKUP,
                target_sources=["calendar"],
                action_mode=ActionMode.CALL_TOOLS,
                confidence=0.81,
                reasoning="semantic:schedule_lookup conf=0.810",
            )
        ],
    )
    trace = {
        "model": "local/hermes3",
        "tool_calls": [{"name": "search_calendar", "arguments": {"days": 7}}],
        "routing": {
            "intent": "calendar_query",
            "sources": ["calendar"],
            "confidence": 0.95,
        },
    }
    await pepper._log_routing_event(
        query="what's on my calendar this week",
        session_id="sess-1",
        latency_ms=812,
        trace=trace,
    )
    rec = pepper._recorder
    assert rec.committed is True
    assert len(rec.added) == 1
    row = rec.added[0]
    assert isinstance(row, RoutingEvent)
    assert row.query_text == "what's on my calendar this week"
    assert row.query_embedding == [0.1] * 1024
    assert row.regex_decision_intent == "calendar_query"
    assert row.regex_decision_sources == ["calendar"]
    assert row.regex_decision_confidence == 0.95
    assert row.tools_actually_called == [
        {"name": "search_calendar", "arguments": {"days": 7}}
    ]
    assert row.llm_model == "local/hermes3"
    assert row.latency_ms == 812
    assert row.user_session_id == "sess-1"
    assert row.shadow_decision_intent == "schedule_lookup"
    assert row.shadow_decision_confidence == pytest.approx(0.81)


@pytest.mark.asyncio
async def test_log_routing_event_tolerates_embed_failure(pepper):
    pepper.llm.embed_router = AsyncMock(side_effect=RuntimeError("ollama down"))
    # Phase 3 cutover: shadow is now QueryRouter — stub it to return [] so
    # the shadow columns stay NULL for this test's assertion.
    stub_router = MagicMock()
    stub_router.route_multi = MagicMock(return_value=[])
    pepper._router = stub_router
    trace = {"model": None, "tool_calls": [], "routing": None}
    await pepper._log_routing_event(
        query="hi",
        session_id="sess-2",
        latency_ms=5,
        trace=trace,
    )
    row = pepper._recorder.added[0]
    assert row.query_embedding is None
    assert row.regex_decision_intent is None
    assert row.regex_decision_sources is None
    assert row.regex_decision_confidence is None
    assert row.tools_actually_called is None
    assert row.user_session_id == "sess-2"
    assert row.shadow_decision_intent is None
    assert row.shadow_decision_confidence is None
    assert pepper._recorder.committed is True


@pytest.mark.asyncio
async def test_log_routing_event_no_db_factory_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFE_CONTEXT_PATH", str(tmp_path / "life.md"))
    (tmp_path / "life.md").write_text("# Life\n")
    config = Settings()
    pepper = PepperCore(config, db_session_factory=None)
    pepper.llm.embed_router = AsyncMock()  # must not be called
    await pepper._log_routing_event(
        query="x", session_id="s", latency_ms=1, trace={},
    )
    pepper.llm.embed_router.assert_not_called()
    assert pepper._semantic_router is None  # no db_factory → no shadow router


@pytest.mark.asyncio
async def test_log_routing_event_picks_max_confidence_shadow(pepper):
    """Multi-intent: shadow row stores the highest-confidence fragment."""
    pepper.llm.embed_router = AsyncMock(return_value=[0.0] * 1024)
    stub = _stub_shadow_router(
        pepper,
        [
            RoutingDecision(
                intent_type=IntentType.SCHEDULE_LOOKUP,
                target_sources=["calendar"],
                action_mode=ActionMode.CALL_TOOLS,
                confidence=0.42,
                reasoning="r1",
            ),
            RoutingDecision(
                intent_type=IntentType.INBOX_SUMMARY,
                target_sources=["email"],
                action_mode=ActionMode.CALL_TOOLS,
                confidence=0.88,
                reasoning="r2",
            ),
        ],
    )
    await pepper._log_routing_event(
        query="calendar today and unread emails",
        session_id="sess-multi",
        latency_ms=10,
        trace={"routing": None},
    )
    stub.route_multi.assert_called_once_with(
        "calendar today and unread emails", pepper._capability_registry
    )
    row = pepper._recorder.added[0]
    assert row.shadow_decision_intent == "inbox_summary"
    assert row.shadow_decision_confidence == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_log_routing_event_shadow_failure_leaves_columns_null(pepper):
    """Shadow routing exceptions never block the regex routing row."""
    pepper.llm.embed_router = AsyncMock(return_value=[0.0] * 1024)
    stub = MagicMock()
    stub.route_multi = MagicMock(side_effect=RuntimeError("classifier exploded"))
    pepper._router = stub
    await pepper._log_routing_event(
        query="x",
        session_id="sess-boom",
        latency_ms=1,
        trace={"routing": {"intent": "general_chat", "sources": None, "confidence": 0.6}},
    )
    row = pepper._recorder.added[0]
    assert row.shadow_decision_intent is None
    assert row.shadow_decision_confidence is None
    assert row.regex_decision_intent == "general_chat"
    assert pepper._recorder.committed is True


@pytest.mark.asyncio
async def test_log_routing_event_swallows_db_failure(pepper):
    pepper.llm.embed_router = AsyncMock(return_value=[0.0] * 1024)
    boom_factory_called = {"n": 0}

    @asynccontextmanager
    async def boom_factory():
        boom_factory_called["n"] += 1
        raise RuntimeError("db unavailable")
        yield  # pragma: no cover

    pepper.db_factory = boom_factory  # type: ignore[assignment]
    # Must not raise.
    await pepper._log_routing_event(
        query="x", session_id="s", latency_ms=1, trace={"routing": None},
    )
    assert boom_factory_called["n"] == 1
