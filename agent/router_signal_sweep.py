"""Batch sweep that grades historical ``routing_events`` rows.

Phase 1 Task 5 wired the success-signal heuristic into the live ``chat()``
path, but that path only sweeps up to 20 prior rows per turn and is
session-scoped. Rows that accumulated before the heuristic shipped — or
in sessions that never come back — stay ``NULL`` indefinitely. The Phase 1
exit criterion ("≥30% of events have non-``unknown`` ``success_signal``")
cannot be reached without grading those backlog rows.

This module walks every session with un-graded rows and applies the same
pure heuristic from ``agent.success_signal``: a follow-up within 30 min
becomes ``re_asked``/``confirmed``, an unanswered turn beyond 60 min
becomes ``abandoned``/``unknown``. Intermediate (30–60 min) rows are
skipped so the live path can resolve them later.

Privacy: read-only over local Postgres + local JSONL turn log. Nothing
leaves the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import structlog
from sqlalchemy import select

from agent import success_signal
from agent.models import RoutingEvent

logger = structlog.get_logger(__name__)

DbFactory = Callable[[], Any]
JsonlLookupFn = Callable[[str, str, datetime], str | None]


@dataclass
class SweepResult:
    sessions: int = 0
    rows_seen: int = 0
    re_asked: int = 0
    confirmed: int = 0
    abandoned: int = 0
    unknown: int = 0
    skipped_ambiguous: int = 0
    skipped_no_response: int = 0
    by_session: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "rows_seen": self.rows_seen,
            "re_asked": self.re_asked,
            "confirmed": self.confirmed,
            "abandoned": self.abandoned,
            "unknown": self.unknown,
            "skipped_ambiguous": self.skipped_ambiguous,
            "skipped_no_response": self.skipped_no_response,
        }


def default_jsonl_lookup(
    session_id: str, query: str, row_timestamp: datetime
) -> str | None:
    """Mirror of ``PepperCore._lookup_jsonl_response`` for the sweep.

    Kept as a module-level function so the sweep does not depend on a live
    ``PepperCore`` instance (which would also pull in LLM clients, MCP, and
    the rest of the agent stack just to read a text file).
    """
    repo_root = Path(__file__).resolve().parent.parent
    log_dir = repo_root / "logs" / "chat_turns"
    candidate_dates = {
        (row_timestamp + timedelta(days=delta)).strftime("%Y-%m-%d")
        for delta in (-1, 0, 1)
    }
    best_response: str | None = None
    best_diff = float("inf")
    for date_str in candidate_dates:
        path = log_dir / f"{date_str}.jsonl"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("session_id") != session_id:
                        continue
                    if row.get("query") != query:
                        continue
                    ts_raw = row.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    diff = abs((ts - row_timestamp).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best_response = row.get("response") or ""
        except OSError:
            continue
    return best_response


async def sweep_all_sessions(
    db_factory: DbFactory,
    *,
    now: datetime | None = None,
    jsonl_lookup: JsonlLookupFn = default_jsonl_lookup,
) -> SweepResult:
    """Apply the success-signal heuristic to every un-graded row.

    For each session, rows are processed in chronological order. Row N's
    follow-up is row N+1 in the same session if it exists; otherwise row N
    is "terminal" and only graded once the 60-min abandonment window has
    closed (relative to ``now``).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    result = SweepResult()

    async with db_factory() as session:
        sid_rows = (
            await session.execute(
                select(RoutingEvent.user_session_id)
                .where(RoutingEvent.success_signal.is_(None))
                .where(RoutingEvent.user_session_id.is_not(None))
                .group_by(RoutingEvent.user_session_id)
            )
        ).all()
        session_ids = [s for (s,) in sid_rows]

    for sid in session_ids:
        async with db_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(RoutingEvent)
                        .where(RoutingEvent.user_session_id == sid)
                        .order_by(RoutingEvent.timestamp.asc())
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                continue
            session_changed = 0
            for idx, row in enumerate(rows):
                if row.success_signal is not None:
                    continue
                result.rows_seen += 1
                next_row = rows[idx + 1] if idx + 1 < len(rows) else None
                signal: str | None = None
                if next_row is not None:
                    age_min = (
                        next_row.timestamp - row.timestamp
                    ).total_seconds() / 60.0
                    if age_min <= success_signal.RE_ASK_WINDOW_MIN:
                        signal = success_signal.derive_followup_signal(
                            row.query_text or "",
                            next_row.query_text or "",
                            age_min,
                        )
                        if signal is None:
                            result.skipped_ambiguous += 1
                    elif age_min > success_signal.ABANDON_WINDOW_MIN:
                        # Long gap to next turn — treat row as terminal.
                        gap_to_now = (
                            now - row.timestamp
                        ).total_seconds() / 60.0
                        prior_response = jsonl_lookup(
                            sid, row.query_text or "", row.timestamp
                        )
                        signal = success_signal.derive_terminal_signal(
                            prior_response, gap_to_now
                        )
                        if signal is None and prior_response is None:
                            result.skipped_no_response += 1
                else:
                    gap_to_now = (now - row.timestamp).total_seconds() / 60.0
                    if gap_to_now > success_signal.ABANDON_WINDOW_MIN:
                        prior_response = jsonl_lookup(
                            sid, row.query_text or "", row.timestamp
                        )
                        signal = success_signal.derive_terminal_signal(
                            prior_response, gap_to_now
                        )
                        if signal is None and prior_response is None:
                            result.skipped_no_response += 1
                if signal is None:
                    continue
                row.success_signal = signal
                row.success_signal_set_at = now
                session_changed += 1
                if signal == "re_asked":
                    result.re_asked += 1
                elif signal == "confirmed":
                    result.confirmed += 1
                elif signal == "abandoned":
                    result.abandoned += 1
                elif signal == "unknown":
                    result.unknown += 1
            if session_changed:
                await session.commit()
                result.sessions += 1
                result.by_session[sid] = session_changed

    return result


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_signal_sweep: DB session factory missing after init_db")
        return 2

    result = await sweep_all_sessions(factory)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        d = result.as_dict()
        print("success-signal sweep:")
        for k, v in d.items():
            print(f"  {k:<22} {v}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-shot sweep that grades un-graded routing_events rows.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
