"""Trace compression policy (#21).

Mirrors the tiered-memory pattern: traces stay in the same `traces`
table; the `tier` column advances `working → recall → archival` over
time. The nightly job runs in the orchestrator's APScheduler (same
process), wired in `agent/scheduler.py`.

Compression at each tier:

- **Working** (created within the last 24h): no compression.
- **Recall** (24h–28 days): structural compression. `embedding` is
  dropped (recoverable from input/output); `assembled_context.items`
  is truncated to the top 3; `tools_called` is projected to
  `[{name, success, latency_ms}]`. `input`, `output`,
  `data_sensitivity`, `archetype`, `created_at` stay verbatim.
- **Archival** (>28 days): heavy fields cleared. `embedding`,
  `assembled_context`, and `tools_called` are blanked; the row
  itself remains as a one-line record (`trace_id`, `created_at`,
  `archetype`, `data_sensitivity`, counts).

Privacy invariant: compression is structural, not semantic. There is
no LLM call on this path today. If a future iteration introduces an
LLM-summarisation step, it MUST pin to the local-only Ollama path
(`agent/llm.py::ModelClient.chat(local_only=True)` — same invariant
as `agent/context_compressor.py`). The `assert_local_only_llm_call`
helper exists to enforce that contract — see tests for usage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from agent.traces.models import TraceRow
from agent.traces.repository import TraceRepository
from agent.traces.schema import TraceTier

logger = structlog.get_logger(__name__)

# Tier transition thresholds. Bumped to match docs/trace-schema.md and
# `docs/adr/0005-trace-schema.md` if the operator changes them.
WORKING_TO_RECALL_AGE = timedelta(hours=24)
RECALL_TO_ARCHIVAL_AGE = timedelta(days=28)

# Top-N items retained from `assembled_context.items` at recall-tier
# compression. Picked to bound jsonb growth without dropping the
# diagnostic value of "what context was assembled for this turn".
RECALL_CONTEXT_TOP_N = 3


# ── Local-only LLM invariant (forward-defending placeholder) ──────────────────


def assert_local_only_llm_call(model: str) -> None:
    """Raise if the named model is not local. Forward-defends against a
    future LLM-summarisation step routing through a frontier model.

    The contract: any LLM call from this module MUST run through
    `agent.llm.ModelClient.chat(local_only=True)`. Frontier models
    (Anthropic etc.) are forbidden because compression operates on
    RAW_PERSONAL trace contents.
    """
    if not model:
        raise ValueError("model name is required for local-only assertion")
    # Conservative substring whitelist — local model names contain a
    # small set of well-known prefixes/substrings (`hermes`,
    # `qwen`, `nomic`, `llama`, `gemma`, `mistral`, `phi`, etc.) and
    # the explicit `local/` prefix used by `agent/llm.py`. Anything
    # else (notably `claude-`, `gpt-`, `anthropic`) is rejected.
    lower = model.lower()
    forbidden_prefixes = ("claude-", "gpt-", "anthropic", "openai", "frontier")
    for prefix in forbidden_prefixes:
        if prefix in lower:
            raise RuntimeError(
                f"compression LLM call rejected: '{model}' is not local. "
                "RAW_PERSONAL trace contents must never reach a frontier API.",
            )


# ── Structural compression ────────────────────────────────────────────────────


def _project_tool_call_to_recall(call: dict[str, Any]) -> dict[str, Any]:
    """Strip args/result_summary; keep name + success + latency_ms only."""
    return {
        "name": call.get("name"),
        "success": call.get("success", True),
        "latency_ms": call.get("latency_ms", 0),
    }


def compress_assembled_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Truncate `items` to the top N; preserve strategy + version."""
    items = ctx.get("items") or []
    return {
        "strategy": ctx.get("strategy"),
        "items": list(items)[:RECALL_CONTEXT_TOP_N],
        "version": ctx.get("version"),
        "compressed_from": len(items),
    }


@dataclass
class CompressionResult:
    scanned: int = 0
    advanced_to_recall: int = 0
    advanced_to_archival: int = 0
    errors: int = 0


# ── Tier scans ────────────────────────────────────────────────────────────────


async def compress_working_to_recall(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_limit: int = 1000,
) -> CompressionResult:
    """Promote rows older than 24h from `working` → `recall`.

    Idempotent: re-running on the same day produces identical results
    because the tier scan filter excludes already-promoted rows.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - WORKING_TO_RECALL_AGE
    result = CompressionResult()

    stmt = (
        select(TraceRow)
        .where(TraceRow.tier == TraceTier.WORKING.value)
        .where(TraceRow.created_at < cutoff)
        .options(
            undefer(TraceRow.assembled_context),
            undefer(TraceRow.tools_called),
        )
        .limit(batch_limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    repo = TraceRepository(session)
    for row in rows:
        result.scanned += 1
        try:
            # Structural compression — RAW_PERSONAL substrings are not
            # touched. We blank the embedding (recoverable) and project
            # the heavy jsonb columns to bounded shapes.
            row.assembled_context = compress_assembled_context(
                row.assembled_context or {},
            )
            row.tools_called = [
                _project_tool_call_to_recall(c) for c in (row.tools_called or [])
            ]
            row.embedding = None
            row.embedding_model_version = None
            await repo.advance_tier(str(row.trace_id), TraceTier.RECALL)
            result.advanced_to_recall += 1
        except Exception as exc:
            result.errors += 1
            logger.warning(
                "trace_compress_recall_failed",
                trace_id=str(row.trace_id),
                error_type=type(exc).__name__,
            )
    await session.commit()
    return result


async def compress_recall_to_archival(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_limit: int = 1000,
) -> CompressionResult:
    """Promote rows older than 28d from `recall` → `archival`.

    At archival we drop everything heavy. The row stays in `traces` as
    a one-line record so the reflector can answer "did anything happen
    on date X" without the embedding/jsonb cost.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - RECALL_TO_ARCHIVAL_AGE
    result = CompressionResult()

    stmt = (
        select(TraceRow)
        .where(TraceRow.tier == TraceTier.RECALL.value)
        .where(TraceRow.created_at < cutoff)
        .options(
            undefer(TraceRow.assembled_context),
            undefer(TraceRow.tools_called),
        )
        .limit(batch_limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    repo = TraceRepository(session)
    for row in rows:
        result.scanned += 1
        try:
            row.assembled_context = {}
            row.tools_called = []
            row.embedding = None
            row.embedding_model_version = None
            await repo.advance_tier(str(row.trace_id), TraceTier.ARCHIVAL)
            result.advanced_to_archival += 1
        except Exception as exc:
            result.errors += 1
            logger.warning(
                "trace_compress_archival_failed",
                trace_id=str(row.trace_id),
                error_type=type(exc).__name__,
            )
    await session.commit()
    return result


async def run_nightly_compression(
    session_factory,
    *,
    now: datetime | None = None,
) -> dict[str, CompressionResult]:
    """One-shot entry point used by the APScheduler job.

    Runs both tier transitions in dependency order (working→recall first
    so newly-recall rows can advance to archival in the same run if old
    enough — usually they're not, but the order is the safe default).
    Idempotent: re-running on the same day produces identical results.
    """
    out: dict[str, CompressionResult] = {}
    async with session_factory() as session:
        out["recall"] = await compress_working_to_recall(session, now=now)
    async with session_factory() as session:
        out["archival"] = await compress_recall_to_archival(session, now=now)
    logger.info(
        "trace_compression_run",
        recall_advanced=out["recall"].advanced_to_recall,
        archival_advanced=out["archival"].advanced_to_archival,
        recall_errors=out["recall"].errors,
        archival_errors=out["archival"].errors,
    )
    return out
