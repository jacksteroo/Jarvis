"""Tests for the /traces FastAPI route (#24).

Covers:

- Localhost-bind enforcement (rejects non-loopback when the setting is on,
  allows it when off).
- API-key dependency wires up correctly.
- list view returns projected summaries; detail view returns the full payload.
- find_similar validates embedding dimension at the request layer (Pydantic
  Field constraints).
- The router does NOT register a DELETE on traces (append-only at the
  HTTP layer, mirrors ADR-0005).

The DB session is mocked via FastAPI dependency overrides so these tests
do not require a live Postgres.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.auth import require_api_key
from agent.db import get_db
from agent.error_classifier import DataSensitivity
from agent.traces import Archetype, Trace, TraceTier, TriggerSource
from agent.traces.http import router


def _build_app(*, repo_query=None, repo_get=None) -> FastAPI:
    """Build a FastAPI app with the traces router and dependency overrides."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _fake_session():
        yield "fake-session"  # never used because we patch the repository

    async def _fake_auth():
        return "test-api-key"

    app.dependency_overrides[get_db] = _fake_session
    app.dependency_overrides[require_api_key] = _fake_auth
    return app


def _trace(**overrides) -> Trace:
    base = dict(
        trace_id=str(uuid.uuid4()),
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        trigger_source=TriggerSource.USER,
        archetype=Archetype.ORCHESTRATOR,
        input="hello",
        output="world",
        model_selected="hermes3-local",
        latency_ms=42,
        data_sensitivity=DataSensitivity.LOCAL_ONLY,
        tier=TraceTier.WORKING,
    )
    base.update(overrides)
    return Trace(**base)


# ── Localhost-bind enforcement ────────────────────────────────────────────────


class TestLocalhostBind:
    def test_loopback_request_passes_through(self) -> None:
        # Patch the loopback check directly because TestClient's
        # client.host is "testclient", not 127.0.0.1.
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg, \
             patch("agent.traces.http._client_is_loopback", return_value=True):
            cfg.PEPPER_BIND_LOCALHOST_ONLY = True
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[_trace()])
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 200, r.text

    def test_non_loopback_request_with_bind_on_returns_403(self) -> None:
        app = _build_app()
        with patch("agent.config.settings") as cfg, \
             patch("agent.traces.http._client_is_loopback", return_value=False):
            cfg.PEPPER_BIND_LOCALHOST_ONLY = True
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 403

    def test_disabled_bind_lets_non_loopback_in(self) -> None:
        # When the operator opts out, the route stops checking client.host.
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[])
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
            assert r.status_code == 200


# ── List view ────────────────────────────────────────────────────────────────


class TestListView:
    def test_returns_projected_summaries(self) -> None:
        app = _build_app()
        traces = [
            _trace(input="user question", output="assistant reply"),
            _trace(
                trigger_source=TriggerSource.SCHEDULER,
                scheduler_job_name="morning_brief",
                input="brief",
                output="brief output",
            ),
        ]
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=traces)
            with TestClient(app) as client:
                r = client.get("/api/traces", headers={"x-api-key": "k"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["traces"]) == 2
        # Summaries do NOT include input/output fields.
        for t in body["traces"]:
            assert "input" not in t
            assert "output" not in t
            assert "assembled_context" not in t
        assert body["traces"][1]["scheduler_job_name"] == "morning_brief"

    def test_filters_propagate_to_repository(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.query = AsyncMock(return_value=[])
            with TestClient(app) as client:
                r = client.get(
                    "/api/traces"
                    "?archetype=orchestrator&trigger_source=scheduler"
                    "&data_sensitivity=local_only&tier=working"
                    "&contains_text=foo&limit=25",
                    headers={"x-api-key": "k"},
                )
            assert r.status_code == 200
            kwargs = repo.query.await_args.kwargs
            assert kwargs["archetype"] is Archetype.ORCHESTRATOR
            assert kwargs["trigger_source"] is TriggerSource.SCHEDULER
            assert kwargs["data_sensitivity"] is DataSensitivity.LOCAL_ONLY
            assert kwargs["tier"] is TraceTier.WORKING
            assert kwargs["contains_text"] == "foo"
            assert kwargs["limit"] == 25
            assert kwargs["with_payload"] is False


# ── Detail view ──────────────────────────────────────────────────────────────


class TestDetailView:
    def test_returns_full_payload(self) -> None:
        app = _build_app()
        t = _trace(input="hello", output="world")
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=t)
            with TestClient(app) as client:
                r = client.get(f"/api/traces/{t.trace_id}", headers={"x-api-key": "k"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["input"] == "hello"
        assert body["output"] == "world"
        assert body["assembled_context"] == {}
        assert body["has_embedding"] is False

    def test_404_when_missing(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.get_by_id = AsyncMock(return_value=None)
            with TestClient(app) as client:
                r = client.get(
                    f"/api/traces/{uuid.uuid4()}", headers={"x-api-key": "k"}
                )
        assert r.status_code == 404


# ── Append-only at the HTTP layer ────────────────────────────────────────────


class TestNoMutationRoutes:
    def test_router_has_no_delete_routes(self) -> None:
        # Mirrors ADR-0005's append-only invariant at the HTTP API layer.
        for route in router.routes:
            methods = getattr(route, "methods", set()) or set()
            assert "DELETE" not in methods, (
                f"unexpected DELETE on {route.path}: {methods}"
            )

    def test_router_only_exposes_documented_paths(self) -> None:
        paths = sorted({getattr(r, "path", "") for r in router.routes})
        # Empty string entries come from internal routes; filter.
        public = [p for p in paths if p.startswith("/")]
        assert public == [
            "/traces",
            "/traces/{trace_id}",
            "/traces/{trace_id}/find_similar",
        ]


# ── find_similar validation ──────────────────────────────────────────────────


class TestFindSimilar:
    def test_rejects_wrong_embedding_dimension(self) -> None:
        app = _build_app()
        with patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/find_similar",
                    headers={"x-api-key": "k"},
                    json={"embedding": [0.0] * 16, "limit": 5},
                )
        # Pydantic constraint at request time → 422.
        assert r.status_code == 422

    def test_returns_id_only_matches(self) -> None:
        app = _build_app()
        with patch("agent.traces.http.TraceRepository") as RepoClass, \
             patch("agent.config.settings") as cfg:
            cfg.PEPPER_BIND_LOCALHOST_ONLY = False  # tests use TestClient which is not loopback
            repo = RepoClass.return_value
            repo.find_similar = AsyncMock(
                return_value=[("11111111-1111-1111-1111-111111111111", 0.12)],
            )
            with TestClient(app) as client:
                r = client.post(
                    f"/api/traces/{uuid.uuid4()}/find_similar",
                    headers={"x-api-key": "k"},
                    json={"embedding": [0.0] * 1024, "limit": 5},
                )
        assert r.status_code == 200
        body = r.json()
        assert body["matches"][0]["trace_id"] == "11111111-1111-1111-1111-111111111111"
        assert body["matches"][0]["distance"] == pytest.approx(0.12)
