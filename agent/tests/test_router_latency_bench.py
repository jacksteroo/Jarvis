"""Unit tests for agent/router_latency_bench.py."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.router_latency_bench import (
    BenchResult,
    DEFAULT_GATE_P95_MS,
    bench,
    load_queries,
    percentile,
    write_artifact,
)


# ── percentile ────────────────────────────────────────────────────────────────


def test_percentile_empty_returns_zero() -> None:
    assert percentile([], 50.0) == 0.0
    assert percentile([], 95.0) == 0.0


def test_percentile_single_value() -> None:
    assert percentile([42.0], 50.0) == 42.0
    assert percentile([42.0], 95.0) == 42.0


def test_percentile_linear_interp_matches_numpy_default() -> None:
    # 1..10 → p95 by linear interp = 9.55, p50 = 5.5
    values = [float(i) for i in range(1, 11)]
    assert percentile(values, 50.0) == pytest.approx(5.5)
    assert percentile(values, 95.0) == pytest.approx(9.55)
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 100.0) == 10.0


def test_percentile_unsorted_input() -> None:
    values = [10.0, 1.0, 5.0, 2.0, 8.0]
    assert percentile(values, 50.0) == 5.0


def test_percentile_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], -1.0)
    with pytest.raises(ValueError):
        percentile([1.0, 2.0], 101.0)


# ── load_queries ──────────────────────────────────────────────────────────────


def test_load_queries_reads_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "q.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"query": "first"}),
                json.dumps({"query": "second", "id": "x"}),
                json.dumps({"query": "first"}),  # dup
                "",  # blank
                "garbage",  # malformed
                json.dumps({"id": "no-query"}),
                json.dumps({"query": "  "}),  # whitespace
            ]
        ),
        encoding="utf-8",
    )
    assert load_queries(p) == ["first", "second"]


def test_load_queries_missing_file(tmp_path: Path) -> None:
    assert load_queries(tmp_path / "absent.jsonl") == []


# ── bench ─────────────────────────────────────────────────────────────────────


class _FakeRouter:
    """Async route_fn that sleeps a scripted duration per call."""

    def __init__(self, delays_ms: list[float]) -> None:
        self.delays_ms = list(delays_ms)
        self.calls: list[str] = []

    async def __call__(self, q: str) -> object:
        self.calls.append(q)
        if self.delays_ms:
            await asyncio.sleep(self.delays_ms.pop(0) / 1000.0)
        return object()


def test_bench_warmup_excluded_from_timings() -> None:
    # 3 warmup + 5 measured. Warmup delays must NOT appear in timings.
    delays = [200.0, 200.0, 200.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    fake = _FakeRouter(delays)

    result = asyncio.run(
        bench(
            route_fn=fake,
            queries=[f"q{i}" for i in range(8)],
            warmup=3,
        )
    )

    assert result.warmup == 3
    assert result.n == 5
    assert len(result.timings_ms) == 5
    # All measured timings should be ~5ms, well below warmup's 200ms.
    assert all(t < 100.0 for t in result.timings_ms)
    # Router was called for every query (warmup + measured).
    assert len(fake.calls) == 8


def test_bench_gate_pass_below_threshold() -> None:
    fake = _FakeRouter([1.0] * 10)
    result = asyncio.run(
        bench(
            route_fn=fake,
            queries=[f"q{i}" for i in range(10)],
            warmup=0,
            gate_p95_ms=DEFAULT_GATE_P95_MS,
        )
    )
    assert result.gate_passed is True
    assert result.p95_ms < DEFAULT_GATE_P95_MS


def test_bench_gate_fail_above_threshold() -> None:
    fake = _FakeRouter([300.0] * 5)
    result = asyncio.run(
        bench(
            route_fn=fake,
            queries=[f"q{i}" for i in range(5)],
            warmup=0,
            gate_p95_ms=DEFAULT_GATE_P95_MS,
        )
    )
    assert result.gate_passed is False
    assert result.p95_ms >= DEFAULT_GATE_P95_MS


def test_bench_records_failures_without_aborting() -> None:
    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, q: str) -> object:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transient")
            await asyncio.sleep(0.001)
            return object()

    fake = _Flaky()
    result = asyncio.run(
        bench(route_fn=fake, queries=["a", "b", "c"], warmup=0)
    )
    # 3 invocations, 1 failure → 2 timings recorded.
    assert fake.calls == 3
    assert result.n == 2


def test_bench_empty_queries_returns_zero_n() -> None:
    fake = _FakeRouter([])
    result = asyncio.run(bench(route_fn=fake, queries=[], warmup=3))
    assert result.n == 0
    assert result.warmup == 0  # warmup clamped to len(queries)
    assert result.gate_passed is False


def test_bench_warmup_larger_than_queries_clamps() -> None:
    fake = _FakeRouter([1.0, 1.0])
    result = asyncio.run(bench(route_fn=fake, queries=["a", "b"], warmup=99))
    assert result.warmup == 2
    assert result.n == 0


def test_bench_negative_warmup_raises() -> None:
    fake = _FakeRouter([1.0])
    with pytest.raises(ValueError):
        asyncio.run(bench(route_fn=fake, queries=["a"], warmup=-1))


# ── write_artifact ────────────────────────────────────────────────────────────


def test_write_artifact_round_trip(tmp_path: Path) -> None:
    result = BenchResult(
        n=3,
        warmup=1,
        timings_ms=[10.5, 20.5, 30.5],
        p50_ms=20.5,
        p95_ms=29.5,
        p99_ms=30.4,
        min_ms=10.5,
        max_ms=30.5,
        mean_ms=20.5,
        gate_p95_threshold_ms=DEFAULT_GATE_P95_MS,
        gate_passed=True,
    )
    ts = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)
    path = write_artifact(result, artifact_dir=tmp_path, timestamp=ts)

    assert path.exists()
    assert path.name == "latency_bench_20260428T120000Z.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["n"] == 3
    assert payload["warmup"] == 1
    assert payload["timings_ms"] == [10.5, 20.5, 30.5]
    assert payload["p95_ms"] == pytest.approx(29.5)
    assert payload["gate_passed"] is True
    assert "generated_at" in payload


def test_bench_result_as_dict_rounds() -> None:
    r = BenchResult(
        n=1,
        warmup=0,
        timings_ms=[12.34567],
        p50_ms=12.34567,
        p95_ms=12.34567,
        p99_ms=12.34567,
        min_ms=12.34567,
        max_ms=12.34567,
        mean_ms=12.34567,
        gate_passed=True,
    )
    d = r.as_dict()
    assert d["timings_ms"] == [12.3]
    assert d["p95_ms"] == 12.35
