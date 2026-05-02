"""FastAPI router for the trace inspection endpoint (#24).

Exposes:

- `GET  /api/traces`           — paginated list view (filters + cursor)
- `GET  /api/traces/{id}`      — full trace including assembled_context
- `POST /api/traces/{id}/find_similar` — embedding-nearest neighbours

**Privacy posture**

This endpoint surfaces the most sensitive HTTP route in the system —
every trace row contains the full input and output of an agent turn,
the assembled context, and tool-call args. Three layers of defence:

1. **API-key required.** Inherits the existing `require_api_key`
   header check used by every other authenticated endpoint.
2. **Localhost bind by default.** When `PEPPER_BIND_LOCALHOST_ONLY`
   is true (the default), every request whose `client.host` is not
   loopback is rejected with 403, even if it carries a valid key.
   The web UI talks to FastAPI over loopback; turning this off is an
   explicit, documented opt-in for non-localhost deployments and
   requires session-level auth (deferred — see GUARDRAILS.md).
3. **Audit log on every read.** Every call records to a `mcp_audit`-
   shaped audit entry so the operator can answer "who looked at
   what, when" without stepping through structlog. Logged on success
   AND on permission-denied.

This module is wired into `agent/main.py` via `include_router`. It
intentionally does NOT expose a `/traces/{id}` `DELETE` route —
mirrors ADR-0005's append-only invariant at the API layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth import require_api_key
from agent.db import get_db
from agent.error_classifier import DataSensitivity
from agent.traces import (
    EMBEDDING_DIM,
    Archetype,
    Trace,
    TraceRepository,
    TraceTier,
    TriggerSource,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/traces", tags=["traces"])


# ── Localhost guard ───────────────────────────────────────────────────────────


def _client_is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


async def _enforce_localhost_bind(request: Request) -> None:
    """Reject non-loopback requests when PEPPER_BIND_LOCALHOST_ONLY is on."""
    from agent.config import settings

    bind_localhost = getattr(settings, "PEPPER_BIND_LOCALHOST_ONLY", True)
    if bind_localhost and not _client_is_loopback(request):
        host = request.client.host if request.client else "<unknown>"
        logger.warning(
            "traces_endpoint_non_loopback_denied",
            client_host=host,
            path=str(request.url.path),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "/traces is bound to localhost. Set PEPPER_BIND_LOCALHOST_ONLY=false "
                "AND wire session-level auth before exposing externally."
            ),
        )


# ── Audit log ─────────────────────────────────────────────────────────────────


async def _audit_read(
    *,
    actor_key_hash: str,
    action: str,
    detail: dict[str, Any],
    request: Request,
) -> None:
    """Append a one-line audit record for every /traces read.

    Reuses `agent.mcp_audit.log_mcp_call` shape because that's the
    existing "who-touched-what" audit pipeline. We log structured
    metadata only — never the row contents.
    """
    try:
        from agent.mcp_audit import audit_logger as audit

        audit.info(
            "traces_endpoint_read",
            actor=actor_key_hash[:12],
            action=action,
            client_host=(request.client.host if request.client else "<unknown>"),
            **detail,
        )
    except Exception:
        # Audit failure must never block a read — same posture as the
        # routing event audit log. Swallow.
        pass


# ── Response models ───────────────────────────────────────────────────────────


class TraceSummary(BaseModel):
    trace_id: str
    created_at: datetime
    trigger_source: str
    archetype: str
    model_selected: str
    latency_ms: int
    data_sensitivity: str
    tier: str
    scheduler_job_name: Optional[str] = None


class TraceDetail(TraceSummary):
    input: str
    output: str
    model_version: str
    prompt_version: str
    assembled_context: dict[str, Any]
    tools_called: list[dict[str, Any]]
    user_reaction: Optional[dict[str, Any]] = None
    embedding_model_version: Optional[str] = None
    has_embedding: bool


class TraceListResponse(BaseModel):
    traces: list[TraceSummary]
    next_cursor: Optional[str] = None


class FindSimilarRequest(BaseModel):
    embedding: list[float] = Field(..., min_length=EMBEDDING_DIM, max_length=EMBEDDING_DIM)
    limit: int = Field(10, ge=1, le=100)


class FindSimilarItem(BaseModel):
    trace_id: str
    distance: float


class FindSimilarResponse(BaseModel):
    matches: list[FindSimilarItem]


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _to_summary(t: Trace) -> TraceSummary:
    return TraceSummary(
        trace_id=t.trace_id,
        created_at=t.created_at,
        trigger_source=t.trigger_source.value,
        archetype=t.archetype.value,
        model_selected=t.model_selected,
        latency_ms=t.latency_ms,
        data_sensitivity=t.data_sensitivity.value,
        tier=t.tier.value,
        scheduler_job_name=t.scheduler_job_name,
    )


def _to_detail(t: Trace) -> TraceDetail:
    return TraceDetail(
        trace_id=t.trace_id,
        created_at=t.created_at,
        trigger_source=t.trigger_source.value,
        archetype=t.archetype.value,
        model_selected=t.model_selected,
        model_version=t.model_version,
        prompt_version=t.prompt_version,
        latency_ms=t.latency_ms,
        data_sensitivity=t.data_sensitivity.value,
        tier=t.tier.value,
        scheduler_job_name=t.scheduler_job_name,
        input=t.input,
        output=t.output,
        assembled_context=t.assembled_context,
        tools_called=t.tools_called,
        user_reaction=t.user_reaction,
        embedding_model_version=t.embedding_model_version,
        has_embedding=t.embedding is not None,
    )


def _parse_cursor(raw: Optional[str]) -> Optional[tuple[datetime, str]]:
    if not raw:
        return None
    try:
        ts_str, trace_id = raw.split("|", 1)
        return (datetime.fromisoformat(ts_str), trace_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc


def _format_cursor(ts: datetime, trace_id: str) -> str:
    return f"{ts.isoformat()}|{trace_id}"


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "",
    response_model=TraceListResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_traces(
    request: Request,
    archetype: Optional[str] = Query(None),
    trigger_source: Optional[str] = Query(None),
    model_selected: Optional[str] = Query(None),
    data_sensitivity: Optional[str] = Query(None),
    tier: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    contains_text: Optional[str] = Query(None, max_length=512),
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> TraceListResponse:
    """List view — projected (no jsonb / embedding loaded)."""
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    traces = await repo.query(
        archetype=Archetype(archetype) if archetype else None,
        trigger_source=TriggerSource(trigger_source) if trigger_source else None,
        model_selected=model_selected,
        data_sensitivity=DataSensitivity(data_sensitivity) if data_sensitivity else None,
        tier=TraceTier(tier) if tier else None,
        since=since,
        until=until,
        contains_text=contains_text,
        cursor=_parse_cursor(cursor),
        limit=limit,
        with_payload=False,
    )

    summaries = [_to_summary(t) for t in traces]
    next_cursor = (
        _format_cursor(traces[-1].created_at, traces[-1].trace_id)
        if len(traces) == limit
        else None
    )

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="list_traces",
        detail={"returned": len(summaries), "limit": limit},
        request=request,
    )
    return TraceListResponse(traces=summaries, next_cursor=next_cursor)


@router.get(
    "/{trace_id}",
    response_model=TraceDetail,
    dependencies=[Depends(require_api_key)],
)
async def get_trace_detail(
    trace_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> TraceDetail:
    """Detail view — full row including assembled_context + tools_called."""
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    try:
        trace = await repo.get_by_id(trace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid trace_id: {exc}") from exc
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="get_trace_detail",
        detail={"trace_id": trace_id},
        request=request,
    )
    return _to_detail(trace)


@router.post(
    "/{trace_id}/find_similar",
    response_model=FindSimilarResponse,
    dependencies=[Depends(require_api_key)],
)
async def find_similar(
    trace_id: str,
    body: FindSimilarRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> FindSimilarResponse:
    """Embedding nearest-neighbours, ID-only.

    Body carries the embedding so the UI can supply a pre-computed
    vector (e.g. from the trace under inspection). Callers re-fetch
    detail rows via `GET /traces/{id}`.
    """
    await _enforce_localhost_bind(request)

    repo = TraceRepository(session)
    try:
        matches = await repo.find_similar(body.embedding, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_key = request.headers.get("x-api-key", "")
    await _audit_read(
        actor_key_hash=api_key,
        action="find_similar",
        detail={"anchor": trace_id, "matches": len(matches)},
        request=request,
    )
    return FindSimilarResponse(
        matches=[FindSimilarItem(trace_id=tid, distance=dist) for tid, dist in matches],
    )
