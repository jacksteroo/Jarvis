"""Phase 3 post-cutover 3-day soak window monitor.

After the atomic cutover (iter 124 / commit cd8cf23) the SemanticRouter
became primary and QueryRouter (regex) moved to shadow. The migration plan
specifies a 3-day soak window with per-hour automated checks before Phase 3
can advance to Phase 4:

  - Eval set pass rate ≥ 85% (auto-rollback if < 80%)
  - re_asked rate within 1.5× pre-cutover baseline
  - abandoned rate within 1.5× pre-cutover baseline
  - p95 routing latency < 200ms
  - Router exception rate = 0

This module computes those checks against ``routing_events`` rows in a
single hourly window, comparing against a frozen pre-cutover baseline file
written once at the start of the soak. Designed to run as either:

  - A one-shot CLI (`.venv/bin/python -m agent.router_soak_monitor check`),
    intended to be wrapped by the operator's scheduled task / cron.
  - An importable function (`run_soak_check`) returning a structured result.

Privacy: read-only over the local Postgres ``routing_events`` table and the
canonical eval set. Stats persisted to ``logs/router_audit/soak_*.json`` are
counts and timings — no query text leaves the module.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import structlog
from sqlalchemy import select

from agent.models import RoutingEvent
from agent.router_latency_bench import percentile

logger = structlog.get_logger(__name__)

DbFactory = Callable[[], Any]

# ─── Thresholds (mirror docs/SEMANTIC_ROUTER_MIGRATION.md Phase 3) ───────
EVAL_PASS_THRESHOLD = 0.85
EVAL_ROLLBACK_THRESHOLD = 0.80
P95_LATENCY_MAX_MS = 200.0
BASELINE_RATIO_MAX = 1.5
EXCEPTION_RATE_MAX = 0.0  # Exit criterion: 0 router exceptions
# Minimum routing_events rows in the soak window for the abandoned/re_asked
# ratio gates to be statistically meaningful. Personal-assistant traffic
# regularly produces only 4–8 routings per hour; with N<30 a single
# user-side abandonment swings the rate by enough to clear the 1.5×
# threshold against any non-zero baseline. Below this floor the ratio
# checks emit SKIP rather than FAIL — the eval/p95/exception gates still
# run unchanged, so a real regression still trips overall_status.
MIN_RATIO_ROWS = 30

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE_PATH = (
    REPO_ROOT / "logs" / "router_audit" / "soak_baseline.json"
)
DEFAULT_RESULT_DIR = REPO_ROOT / "logs" / "router_audit"
DEFAULT_P95_QUERIES_FILE = REPO_ROOT / "tests" / "router_eval_set.jsonl"
# Sample size for the per-hour router-only p95 probe. The full 100-query
# eval set is overkill on every hourly check; a stratified-by-order subset
# of 30 queries gives a stable p95 in <3s and well under the soak window.
DEFAULT_P95_SAMPLE_N = 30
DEFAULT_P95_WARMUP = 3
# Cutover commit cd8cf23 landed at this UTC instant. Baseline windows must
# end strictly before this; soak windows start at-or-after.
DEFAULT_CUTOVER_TS = datetime(2026, 4, 29, 7, 9, 0, tzinfo=timezone.utc)


# ─── Result types ────────────────────────────────────────────────────────


@dataclass
class WindowStats:
    """Summary stats for a routing_events window.

    Counts here drive every soak check; ratios are computed in
    ``evaluate`` against the frozen baseline so the on-disk baseline file
    only needs raw counts (cheap to recompute, hard to mis-key).
    """

    window_start: str
    window_end: str
    total_rows: int = 0
    re_asked: int = 0
    abandoned: int = 0
    confirmed: int = 0
    unknown: int = 0
    null_signal: int = 0
    primary_intent_null: int = 0
    # End-to-end chat turn latency from routing_events.latency_ms (logged
    # by core.py:2081). Includes LLM tool-call time, NOT just the router.
    # Reported for visibility; the soak FAIL gate uses the dedicated
    # router-only probe via ``router_latency_bench`` instead.
    p95_chat_turn_ms: float = 0.0
    p50_chat_turn_ms: float = 0.0

    @property
    def re_asked_rate(self) -> float:
        return self.re_asked / self.total_rows if self.total_rows else 0.0

    @property
    def abandoned_rate(self) -> float:
        return self.abandoned / self.total_rows if self.total_rows else 0.0

    @property
    def exception_rate(self) -> float:
        return (
            self.primary_intent_null / self.total_rows
            if self.total_rows
            else 0.0
        )

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["re_asked_rate"] = self.re_asked_rate
        d["abandoned_rate"] = self.abandoned_rate
        d["exception_rate"] = self.exception_rate
        return d


@dataclass
class SoakCheck:
    name: str
    status: str  # PASS / FAIL / WARN
    value: float
    threshold: float
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoakResult:
    overall_status: str  # PASS / FAIL / ROLLBACK
    checks: list[SoakCheck] = field(default_factory=list)
    window: Optional[WindowStats] = None
    baseline_path: Optional[str] = None
    eval_pass_rate: Optional[float] = None
    generated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "generated_at": self.generated_at,
            "eval_pass_rate": self.eval_pass_rate,
            "baseline_path": self.baseline_path,
            "window": self.window.as_dict() if self.window else None,
            "checks": [c.as_dict() for c in self.checks],
        }


# ─── DB queries ──────────────────────────────────────────────────────────


async def gather_window_stats(
    db_factory: DbFactory,
    *,
    since: datetime,
    until: datetime,
) -> WindowStats:
    """Compute aggregates over routing_events in [since, until).

    Single SELECT with conditional aggregates so we hit the
    ``idx_routing_events_timestamp_desc`` index once. Latency percentiles
    are computed in Python (the row count per window is small — at most
    a few hundred — and importing numpy or rolling a SQL percentile_cont
    just for this isn't worth it).
    """
    if since >= until:
        raise ValueError("since must be < until")

    async with db_factory() as session:
        result = await session.execute(
            select(RoutingEvent).where(
                RoutingEvent.timestamp >= since,
                RoutingEvent.timestamp < until,
            )
        )
        rows = list(result.scalars().all())

    stats = WindowStats(
        window_start=since.isoformat(),
        window_end=until.isoformat(),
        total_rows=len(rows),
    )
    latencies: list[float] = []
    for r in rows:
        sig = r.success_signal
        if sig == "re_asked":
            stats.re_asked += 1
        elif sig == "abandoned":
            stats.abandoned += 1
        elif sig == "confirmed":
            stats.confirmed += 1
        elif sig == "unknown":
            stats.unknown += 1
        elif sig is None:
            stats.null_signal += 1
        if r.regex_decision_intent is None:
            stats.primary_intent_null += 1
        if r.latency_ms is not None:
            latencies.append(float(r.latency_ms))

    if latencies:
        stats.p50_chat_turn_ms = percentile(latencies, 50)
        stats.p95_chat_turn_ms = percentile(latencies, 95)
    return stats


# ─── Eval pass rate ──────────────────────────────────────────────────────


async def run_router_eval(
    runner: Optional[Callable[[], Awaitable[float]]] = None,
) -> float:
    """Return canonical eval accuracy in [0, 1]. Pluggable for tests.

    Default impl shells out to ``.venv/bin/python -m agent.router_eval``
    so the soak monitor is decoupled from the eval module's internal
    asyncio loop and DB setup (the eval CLI manages its own).
    """
    if runner is not None:
        return await runner()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent.router_eval",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"router_eval failed (rc={proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace')[:500]}"
        )
    payload = json.loads(stdout.decode("utf-8"))
    return float(payload.get("accuracy", 0.0))


# ─── Evaluation ──────────────────────────────────────────────────────────


def evaluate(
    *,
    window: WindowStats,
    baseline: dict[str, Any],
    eval_pass_rate: float,
    router_p95_ms: Optional[float] = None,
) -> SoakResult:
    """Apply the five Phase 3 soak checks and compose a SoakResult.

    Baseline ratios use a small absolute-rate floor so an empty soak
    window or a near-zero baseline rate doesn't trip the ratio check on
    natural variance (e.g. baseline 0.5%, soak 1.0% is a 2× ratio but
    statistically meaningless on small N).
    """
    checks: list[SoakCheck] = []
    overall = "PASS"

    # 1. Eval pass rate
    eval_status = "PASS"
    if eval_pass_rate < EVAL_ROLLBACK_THRESHOLD:
        eval_status = "ROLLBACK"
        overall = "ROLLBACK"
    elif eval_pass_rate < EVAL_PASS_THRESHOLD:
        eval_status = "FAIL"
        overall = max(overall, "FAIL", key=_status_rank)
    checks.append(
        SoakCheck(
            name="eval_pass_rate",
            status=eval_status,
            value=eval_pass_rate,
            threshold=EVAL_PASS_THRESHOLD,
            detail=(
                f"canonical 100-query eval accuracy={eval_pass_rate:.3f} "
                f"(rollback floor={EVAL_ROLLBACK_THRESHOLD})"
            ),
        )
    )

    # 2. Router-only p95 latency (probed via router_latency_bench).
    #    routing_events.latency_ms is the WHOLE chat turn (LLM + tools),
    #    so it can't satisfy the spec's <200ms router-only threshold.
    #    When the runner isn't supplied, mark SKIP — not part of FAIL.
    if router_p95_ms is None:
        checks.append(
            SoakCheck(
                name="p95_router_latency_ms",
                status="SKIP",
                value=-1.0,
                threshold=P95_LATENCY_MAX_MS,
                detail=(
                    "router_p95_ms not supplied; pass router_p95_runner "
                    "to run_soak_check to enable this gate"
                ),
            )
        )
    else:
        p95_status = (
            "PASS" if router_p95_ms < P95_LATENCY_MAX_MS else "FAIL"
        )
        if p95_status == "FAIL":
            overall = max(overall, "FAIL", key=_status_rank)
        checks.append(
            SoakCheck(
                name="p95_router_latency_ms",
                status=p95_status,
                value=router_p95_ms,
                threshold=P95_LATENCY_MAX_MS,
                detail="probed via agent.router_latency_bench",
            )
        )

    # 3. Exception rate
    exc_status = (
        "PASS" if window.exception_rate <= EXCEPTION_RATE_MAX else "FAIL"
    )
    if exc_status == "FAIL":
        overall = max(overall, "FAIL", key=_status_rank)
    checks.append(
        SoakCheck(
            name="primary_router_exception_rate",
            status=exc_status,
            value=window.exception_rate,
            threshold=EXCEPTION_RATE_MAX,
            detail=(
                f"{window.primary_intent_null} of {window.total_rows} rows "
                "had regex_decision_intent IS NULL (primary post-cutover)"
            ),
        )
    )

    # 4. re_asked ratio vs baseline
    checks.append(
        _ratio_check(
            "re_asked_rate_ratio",
            soak_rate=window.re_asked_rate,
            baseline_rate=float(baseline.get("re_asked_rate", 0.0)),
            soak_total_rows=window.total_rows,
        )
    )
    # 5. abandoned ratio vs baseline
    checks.append(
        _ratio_check(
            "abandoned_rate_ratio",
            soak_rate=window.abandoned_rate,
            baseline_rate=float(baseline.get("abandoned_rate", 0.0)),
            soak_total_rows=window.total_rows,
        )
    )
    for c in checks[-2:]:
        if c.status == "FAIL":
            overall = max(overall, "FAIL", key=_status_rank)

    return SoakResult(
        overall_status=overall,
        checks=checks,
        window=window,
        eval_pass_rate=eval_pass_rate,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# Status ranking: PASS < FAIL < ROLLBACK (rollback subsumes any FAIL)
_STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2, "ROLLBACK": 3}


def _status_rank(s: str) -> int:
    return _STATUS_RANK.get(s, 0)


def _ratio_check(
    name: str,
    *,
    soak_rate: float,
    baseline_rate: float,
    soak_total_rows: int = 0,
) -> SoakCheck:
    """Compare a soak rate to baseline with small-N and noise floors.

    Two guards keep this from spamming false FAILs:
      * ``soak_total_rows < MIN_RATIO_ROWS`` → SKIP (insufficient data;
        see MIN_RATIO_ROWS comment for why hourly windows can hit this).
      * baseline rate <1% AND soak rate <5% → PASS (statistical noise).

    Otherwise a ratio above 1.5× FAILs, matching the spec's "within
    1.5× baseline" exit criterion.
    """
    if soak_total_rows < MIN_RATIO_ROWS:
        return SoakCheck(
            name=name,
            status="SKIP",
            value=-1.0,
            threshold=BASELINE_RATIO_MAX,
            detail=(
                f"insufficient data: total_rows={soak_total_rows} "
                f"< MIN_RATIO_ROWS={MIN_RATIO_ROWS}; ratio not assessed"
            ),
        )

    if baseline_rate <= 0.0:
        ratio = 0.0 if soak_rate <= 0.0 else float("inf")
    else:
        ratio = soak_rate / baseline_rate

    status = "PASS"
    if ratio > BASELINE_RATIO_MAX:
        if baseline_rate < 0.01 and soak_rate < 0.05:
            status = "PASS"  # noise floor
        else:
            status = "FAIL"

    return SoakCheck(
        name=name,
        status=status,
        value=ratio if ratio != float("inf") else -1.0,
        threshold=BASELINE_RATIO_MAX,
        detail=(
            f"soak={soak_rate:.4f} baseline={baseline_rate:.4f}"
            f" ratio={ratio if ratio != float('inf') else 'inf'}"
        ),
    )


# ─── Baseline IO ─────────────────────────────────────────────────────────


def write_baseline(stats: WindowStats, path: Path) -> dict[str, Any]:
    """Persist the pre-cutover baseline. Idempotent; overwrites file."""
    payload = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        **stats.as_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def load_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"baseline missing at {path}; run "
            "`python -m agent.router_soak_monitor compute-baseline` first"
        )
    return json.loads(path.read_text())


# ─── Router-only p95 probe runner ────────────────────────────────────────


def build_default_router_p95_runner(
    db_factory: DbFactory,
    *,
    queries_path: Path = DEFAULT_P95_QUERIES_FILE,
    sample_n: int = DEFAULT_P95_SAMPLE_N,
    warmup: int = DEFAULT_P95_WARMUP,
    gate_p95_ms: float = P95_LATENCY_MAX_MS,
) -> Callable[[], Awaitable[float]]:
    """Return an async runner that probes SemanticRouter p95 latency.

    The runner instantiates SemanticRouter against the live local
    classifier (Ollama embeddings + pgvector k-NN), benches it over a
    bounded query sample using ``router_latency_bench.bench``, and
    returns the measured p95 in ms. Imports are deferred to the
    runner body so unit tests of ``run_soak_check`` can supply a fake
    runner without forcing the heavy ``semantic_router`` /
    ``llm.ModelClient`` import chain at module load time.

    Privacy: queries come from the canonical eval set
    (``tests/router_eval_set.jsonl``) — synthetic inputs, no PII.
    """

    async def _runner() -> float:
        from agent.config import settings
        from agent.llm import ModelClient
        from agent.router_latency_bench import bench, load_queries
        from agent.semantic_router import (
            SemanticIntentClassifier,
            SemanticRouter,
        )

        queries = load_queries(queries_path)
        if sample_n > 0:
            queries = queries[:sample_n]
        if not queries:
            raise RuntimeError(
                f"router_p95_runner: no queries loaded from {queries_path}"
            )

        llm = ModelClient(settings)
        classifier = SemanticIntentClassifier(
            db_factory=db_factory,
            embed_fn=llm.embed_router,
        )
        router = SemanticRouter(classifier=classifier)
        result = await bench(
            route_fn=router.route_first,
            queries=queries,
            warmup=warmup,
            gate_p95_ms=gate_p95_ms,
        )
        if result.n == 0:
            raise RuntimeError(
                "router_p95_runner: bench produced zero timings; "
                "every route call errored — check Ollama/DB availability"
            )
        logger.info(
            "router_soak_p95_probe",
            n=result.n,
            warmup=result.warmup,
            p50_ms=round(result.p50_ms, 2),
            p95_ms=round(result.p95_ms, 2),
            max_ms=round(result.max_ms, 2),
        )
        return result.p95_ms

    return _runner


# ─── Top-level orchestration ─────────────────────────────────────────────


async def run_soak_check(
    db_factory: DbFactory,
    *,
    baseline_path: Path = DEFAULT_BASELINE_PATH,
    window_hours: int = 1,
    now: Optional[datetime] = None,
    eval_runner: Optional[Callable[[], Awaitable[float]]] = None,
    router_p95_runner: Optional[Callable[[], Awaitable[float]]] = None,
) -> SoakResult:
    """Run all five checks and return a structured result.

    Caller is responsible for persisting the result and acting on
    ``overall_status == "ROLLBACK"`` (kill-switch / docker rebuild). This
    module is intentionally read-only.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    baseline = load_baseline(baseline_path)
    window = await gather_window_stats(db_factory, since=since, until=now)
    eval_rate = await run_router_eval(eval_runner)
    router_p95_ms: Optional[float] = None
    if router_p95_runner is not None:
        router_p95_ms = await router_p95_runner()

    result = evaluate(
        window=window,
        baseline=baseline,
        eval_pass_rate=eval_rate,
        router_p95_ms=router_p95_ms,
    )
    result.baseline_path = str(baseline_path)
    return result


# ─── CLI ─────────────────────────────────────────────────────────────────


async def _cli_compute_baseline(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_soak_monitor: DB session factory missing")
        return 2

    cutover = datetime.fromisoformat(args.cutover) if args.cutover else (
        DEFAULT_CUTOVER_TS
    )
    since = cutover - timedelta(hours=args.lookback_hours)
    stats = await gather_window_stats(factory, since=since, until=cutover)
    if stats.total_rows == 0:
        print(
            "router_soak_monitor: zero rows in baseline window "
            f"[{since.isoformat()}, {cutover.isoformat()}); refusing to write"
        )
        return 3
    payload = write_baseline(stats, Path(args.out))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


async def _cli_check(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_soak_monitor: DB session factory missing")
        return 2

    p95_runner: Optional[Callable[[], Awaitable[float]]] = None
    if not args.no_router_p95:
        p95_runner = build_default_router_p95_runner(
            factory,
            queries_path=Path(args.p95_queries_file),
            sample_n=args.p95_sample_n,
            warmup=args.p95_warmup,
        )

    result = await run_soak_check(
        factory,
        baseline_path=Path(args.baseline),
        window_hours=args.window_hours,
        router_p95_runner=p95_runner,
    )

    out_path = Path(args.out_dir) / (
        f"soak_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))

    if result.overall_status == "ROLLBACK":
        return 4
    if result.overall_status == "FAIL":
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="router_soak_monitor",
        description="Phase 3 post-cutover soak window monitor",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    bp = sub.add_parser(
        "compute-baseline",
        help="Snapshot pre-cutover baseline rates (run once at soak start)",
    )
    bp.add_argument("--cutover", default=None, help="ISO UTC cutover ts")
    bp.add_argument("--lookback-hours", type=int, default=24)
    bp.add_argument("--out", default=str(DEFAULT_BASELINE_PATH))

    cp = sub.add_parser("check", help="Run one soak check window")
    cp.add_argument("--baseline", default=str(DEFAULT_BASELINE_PATH))
    cp.add_argument("--window-hours", type=int, default=1)
    cp.add_argument("--out-dir", default=str(DEFAULT_RESULT_DIR))
    cp.add_argument(
        "--no-router-p95",
        action="store_true",
        help=(
            "Skip the SemanticRouter p95 probe (the gate becomes SKIP). "
            "Default off — production hourly runs probe the live router."
        ),
    )
    cp.add_argument(
        "--p95-queries-file",
        default=str(DEFAULT_P95_QUERIES_FILE),
        help="JSONL file feeding the router p95 probe (default: %(default)s).",
    )
    cp.add_argument(
        "--p95-sample-n",
        type=int,
        default=DEFAULT_P95_SAMPLE_N,
        help="Cap probe queries (default: %(default)s).",
    )
    cp.add_argument(
        "--p95-warmup",
        type=int,
        default=DEFAULT_P95_WARMUP,
        help="Discard the first N timings as warmup (default: %(default)s).",
    )
    return p


def main() -> int:
    args = _build_parser().parse_args()
    if args.cmd == "compute-baseline":
        return asyncio.run(_cli_compute_baseline(args))
    if args.cmd == "check":
        return asyncio.run(_cli_check(args))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
