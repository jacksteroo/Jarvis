"""Phase 2 Gate 3 latency benchmark — measure p95 of SemanticRouter.route().

Phase 2 exit criterion #3 of docs/SEMANTIC_ROUTER_MIGRATION.md requires:

    p95 embedding+search latency < 200ms (retuned for
    ``qwen3-embedding:0.6b``; original 150ms target assumed
    ``nomic-embed-text``).

``router_shadow_replay.py`` reports a per-row mean (~68ms in iter 99) but
mean hides the tail. Gate 3 is a *p95* threshold, so a dedicated bench
that records each call's wall time and computes percentiles is required.

Pipeline:

1. Load distinct queries from a JSONL file (default
   ``tests/router_eval_set.jsonl``). Each row's ``query`` field is taken
   verbatim. Distinct queries matter because the classifier caches
   embeddings by ``sha256(query)``; repeats would measure cache hits,
   not the real embed+search path Gate 3 cares about.
2. Run a small warmup pass (default 3 queries) to amortize Ollama
   model-load + connection-pool warmup. Warmup samples are discarded.
3. For each remaining query, time ``route_first`` end-to-end
   (``time.perf_counter``). The first fragment's decision is the one
   the live path acts on, so we measure the same path.
4. Compute p50/p95/p99/min/max/mean over the timings; emit a JSON
   artifact under ``logs/router_audit/`` and return the dataclass.

Privacy: read-only. Queries come from a local file; the only outbound
call is to local Ollama for embeddings; no Telegram, no Claude API, no
DB writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable

import structlog

from agent.semantic_router import RoutingDecision

logger = structlog.get_logger(__name__)

DEFAULT_GATE_P95_MS = 200.0
DEFAULT_WARMUP = 3
DEFAULT_QUERIES_FILE = Path("tests/router_eval_set.jsonl")
DEFAULT_ARTIFACT_DIR = Path("logs/router_audit")

RouteFn = Callable[[str], Awaitable[RoutingDecision]]


@dataclass
class BenchResult:
    n: int
    warmup: int
    timings_ms: list[float] = field(default_factory=list)
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    gate_p95_threshold_ms: float = DEFAULT_GATE_P95_MS
    gate_passed: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        # Round for readability; raw timings rounded to one decimal.
        d["timings_ms"] = [round(t, 1) for t in self.timings_ms]
        for k in ("p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "mean_ms"):
            d[k] = round(d[k], 2)
        return d


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile, matching numpy default.

    ``p`` is in [0, 100]. Returns 0.0 for an empty list. Implemented
    inline to avoid a numpy dep just for this one call.
    """
    if not values:
        return 0.0
    if not 0.0 <= p <= 100.0:
        raise ValueError("p must be in [0, 100]")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def load_queries(path: Path) -> list[str]:
    """Load distinct queries from a JSONL file.

    Each row must have a ``query`` field. Order is preserved, duplicates
    dropped (first occurrence wins). Returns an empty list if the file
    is missing or empty — the bench's ``run`` will then no-op.
    """
    if not path.exists():
        return []
    seen: set[str] = set()
    out: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            q = row.get("query")
            if not isinstance(q, str) or not q.strip() or q in seen:
                continue
            seen.add(q)
            out.append(q)
    return out


async def bench(
    *,
    route_fn: RouteFn,
    queries: Iterable[str],
    warmup: int = DEFAULT_WARMUP,
    gate_p95_ms: float = DEFAULT_GATE_P95_MS,
) -> BenchResult:
    """Time ``route_fn`` once per query and report percentiles.

    ``warmup`` queries from the head of ``queries`` are routed but not
    timed; this absorbs Ollama model-load and any one-shot connection
    pool warmup so the reported numbers reflect steady-state latency.
    """
    qs = list(queries)
    if warmup < 0:
        raise ValueError("warmup must be >= 0")

    warmup_actual = min(warmup, len(qs))
    for q in qs[:warmup_actual]:
        try:
            await route_fn(q)
        except Exception as exc:  # noqa: BLE001 — best-effort warmup
            logger.warning("router_latency_bench_warmup_error", error=str(exc))

    measured = qs[warmup_actual:]
    timings: list[float] = []
    for q in measured:
        t0 = time.perf_counter()
        try:
            await route_fn(q)
        except Exception as exc:  # noqa: BLE001 — record outliers, do not abort
            logger.warning("router_latency_bench_route_error", error=str(exc))
            continue
        timings.append((time.perf_counter() - t0) * 1000.0)

    if not timings:
        return BenchResult(
            n=0,
            warmup=warmup_actual,
            gate_p95_threshold_ms=gate_p95_ms,
            gate_passed=False,
        )

    p95 = percentile(timings, 95.0)
    return BenchResult(
        n=len(timings),
        warmup=warmup_actual,
        timings_ms=timings,
        p50_ms=percentile(timings, 50.0),
        p95_ms=p95,
        p99_ms=percentile(timings, 99.0),
        min_ms=min(timings),
        max_ms=max(timings),
        mean_ms=sum(timings) / len(timings),
        gate_p95_threshold_ms=gate_p95_ms,
        gate_passed=p95 < gate_p95_ms,
    )


def write_artifact(
    result: BenchResult,
    *,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    timestamp: datetime | None = None,
) -> Path:
    """Persist ``result`` as JSON under ``artifact_dir``. Returns the path."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    when = timestamp or datetime.now(timezone.utc)
    path = artifact_dir / f"latency_bench_{when.strftime('%Y%m%dT%H%M%SZ')}.json"
    payload = {"generated_at": when.isoformat(), **result.as_dict()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


async def _run_cli(args: argparse.Namespace) -> int:
    queries = load_queries(Path(args.queries_file))
    if args.limit is not None:
        queries = queries[: args.limit]
    if not queries:
        print(f"router_latency_bench: no queries loaded from {args.queries_file}")
        return 2

    from agent import db as db_module
    from agent.config import settings
    from agent.llm import ModelClient
    from agent.semantic_router import SemanticIntentClassifier, SemanticRouter

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_latency_bench: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)
    classifier = SemanticIntentClassifier(
        db_factory=factory,
        embed_fn=llm.embed_router,
    )
    router = SemanticRouter(classifier=classifier)

    result = await bench(
        route_fn=router.route_first,
        queries=queries,
        warmup=args.warmup,
        gate_p95_ms=args.gate_p95_ms,
    )

    artifact_path = write_artifact(result, artifact_dir=Path(args.artifact_dir))
    summary = result.as_dict()
    summary.pop("timings_ms", None)
    summary["artifact"] = str(artifact_path)
    print(json.dumps(summary, indent=2))
    return 0 if result.gate_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark SemanticRouter.route_first end-to-end (embed + k-NN) "
            "and report p50/p95/p99 versus Phase 2 Gate 3."
        ),
    )
    parser.add_argument(
        "--queries-file",
        default=str(DEFAULT_QUERIES_FILE),
        help="JSONL file with `query` field per row (default: %(default)s).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=DEFAULT_WARMUP,
        help="Discard the first N timings as warmup (default: %(default)s).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap total queries (default: all distinct queries in file).",
    )
    parser.add_argument(
        "--gate-p95-ms",
        type=float,
        default=DEFAULT_GATE_P95_MS,
        help="Pass/fail threshold on p95 in ms (default: %(default)s).",
    )
    parser.add_argument(
        "--artifact-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help="Directory for the JSON artifact (default: %(default)s).",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
