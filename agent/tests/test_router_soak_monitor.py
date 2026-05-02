"""Unit tests for agent.router_soak_monitor.

The soak monitor is exercised against an in-memory mock async session
factory so we don't need a live Postgres. Each test isolates one
behavior of the five soak checks: eval threshold, p95 latency, exception
rate, re_asked ratio, abandoned ratio.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from agent import router_soak_monitor as soak
from agent.models import RoutingEvent


CUTOVER = datetime(2026, 4, 29, 7, 9, 0, tzinfo=timezone.utc)


def _make_factory(rows: list[RoutingEvent]):
    """Mock async session factory.

    The monitor issues exactly one SELECT (rows in window). We don't try
    to parse the WHERE — the test caller is expected to pre-filter ``rows``
    to whatever window it wants to verify against.
    """

    @asynccontextmanager
    async def _ctx():
        class _Sess:
            async def execute(self, _stmt):
                class _R:
                    def scalars(self_):
                        class _S:
                            def all(self__):
                                return list(rows)

                        return _S()

                return _R()

        yield _Sess()

    return _ctx


def _row(
    ts: datetime,
    *,
    signal: str | None = None,
    primary_intent: str | None = "general_chat",
    latency_ms: int | None = 50,
) -> RoutingEvent:
    return RoutingEvent(
        timestamp=ts,
        query_text="q",
        regex_decision_intent=primary_intent,
        latency_ms=latency_ms,
        success_signal=signal,
        user_session_id="s1",
    )


# ─── gather_window_stats ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_window_stats_aggregates_counts_and_p95():
    now = CUTOVER + timedelta(hours=2)
    since = now - timedelta(hours=1)
    rows = [
        _row(since + timedelta(minutes=5), signal="re_asked", latency_ms=50),
        _row(since + timedelta(minutes=10), signal="abandoned", latency_ms=80),
        _row(
            since + timedelta(minutes=15),
            signal=None,
            primary_intent=None,
            latency_ms=100,
        ),
        _row(since + timedelta(minutes=20), signal="confirmed", latency_ms=300),
    ]
    stats = await soak.gather_window_stats(
        _make_factory(rows), since=since, until=now
    )
    assert stats.total_rows == 4
    assert stats.re_asked == 1
    assert stats.abandoned == 1
    assert stats.confirmed == 1
    assert stats.null_signal == 1
    assert stats.primary_intent_null == 1
    assert stats.p50_chat_turn_ms == 90.0
    assert 250.0 < stats.p95_chat_turn_ms <= 300.0
    assert stats.re_asked_rate == 0.25
    assert stats.exception_rate == 0.25


@pytest.mark.asyncio
async def test_gather_window_stats_rejects_inverted_window():
    with pytest.raises(ValueError):
        await soak.gather_window_stats(
            _make_factory([]),
            since=CUTOVER + timedelta(hours=2),
            until=CUTOVER,
        )


# ─── evaluate (the five checks) ──────────────────────────────────────────


def _baseline(re_asked=0.10, abandoned=0.05) -> dict:
    return {
        "total_rows": 200,
        "re_asked_rate": re_asked,
        "abandoned_rate": abandoned,
    }


def _good_window() -> soak.WindowStats:
    # total_rows ≥ MIN_RATIO_ROWS so the ratio checks stay active in
    # tests that aren't specifically exercising the small-N SKIP path.
    return soak.WindowStats(
        window_start="t0",
        window_end="t1",
        total_rows=40,
        re_asked=4,
        abandoned=2,
        confirmed=20,
        unknown=2,
        null_signal=12,
        primary_intent_null=0,
        p50_chat_turn_ms=40.0,
        p95_chat_turn_ms=120.0,
    )


def test_evaluate_pass_when_all_thresholds_met():
    r = soak.evaluate(window=_good_window(), baseline=_baseline(), eval_pass_rate=0.95)
    assert r.overall_status == "PASS"
    assert {c.name for c in r.checks} == {
        "eval_pass_rate",
        "p95_router_latency_ms",
        "primary_router_exception_rate",
        "re_asked_rate_ratio",
        "abandoned_rate_ratio",
    }
    # Router p95 SKIPped when no probe is supplied; rest must PASS.
    for c in r.checks:
        if c.name == "p95_router_latency_ms":
            assert c.status == "SKIP"
        else:
            assert c.status == "PASS", (c.name, c.detail)


def test_evaluate_eval_below_85_fails_but_above_80_does_not_rollback():
    r = soak.evaluate(window=_good_window(), baseline=_baseline(), eval_pass_rate=0.83)
    assert r.overall_status == "FAIL"
    eval_check = next(c for c in r.checks if c.name == "eval_pass_rate")
    assert eval_check.status == "FAIL"


def test_evaluate_eval_below_80_triggers_rollback():
    r = soak.evaluate(window=_good_window(), baseline=_baseline(), eval_pass_rate=0.78)
    assert r.overall_status == "ROLLBACK"
    eval_check = next(c for c in r.checks if c.name == "eval_pass_rate")
    assert eval_check.status == "ROLLBACK"


def test_evaluate_router_p95_over_200ms_fails():
    r = soak.evaluate(
        window=_good_window(),
        baseline=_baseline(),
        eval_pass_rate=0.95,
        router_p95_ms=250.0,
    )
    assert r.overall_status == "FAIL"
    p95 = next(c for c in r.checks if c.name == "p95_router_latency_ms")
    assert p95.status == "FAIL"


def test_evaluate_router_p95_under_threshold_passes():
    r = soak.evaluate(
        window=_good_window(),
        baseline=_baseline(),
        eval_pass_rate=0.95,
        router_p95_ms=120.0,
    )
    p95 = next(c for c in r.checks if c.name == "p95_router_latency_ms")
    assert p95.status == "PASS"
    assert r.overall_status == "PASS"


def test_evaluate_router_p95_skip_when_runner_absent():
    r = soak.evaluate(
        window=_good_window(),
        baseline=_baseline(),
        eval_pass_rate=0.95,
    )
    p95 = next(c for c in r.checks if c.name == "p95_router_latency_ms")
    assert p95.status == "SKIP"
    # SKIP must not poison overall PASS.
    assert r.overall_status == "PASS"


def test_evaluate_exception_rate_nonzero_fails():
    w = _good_window()
    w.primary_intent_null = 1  # 1/20 = 5%
    r = soak.evaluate(window=w, baseline=_baseline(), eval_pass_rate=0.95)
    assert r.overall_status == "FAIL"
    exc = next(
        c for c in r.checks if c.name == "primary_router_exception_rate"
    )
    assert exc.status == "FAIL"


def test_evaluate_re_asked_ratio_over_1_5x_fails():
    w = _good_window()
    # baseline re_asked 10%; soak 20% → ratio 2.0 → FAIL.
    # total_rows ≥ MIN_RATIO_ROWS so the small-N SKIP guard doesn't mask
    # the real-signal FAIL we want to assert here.
    w.re_asked = 8
    w.total_rows = 40
    r = soak.evaluate(window=w, baseline=_baseline(re_asked=0.10), eval_pass_rate=0.95)
    ratio = next(c for c in r.checks if c.name == "re_asked_rate_ratio")
    assert ratio.status == "FAIL"
    assert r.overall_status == "FAIL"


def test_evaluate_ratio_skipped_when_window_below_min_ratio_rows():
    # Personal-assistant traffic regularly produces 4–8 routings/hour;
    # under MIN_RATIO_ROWS the abandoned/re_asked ratio gates emit SKIP
    # so a single user-side abandonment doesn't fire a FAIL on noise.
    w = _good_window()
    w.total_rows = soak.MIN_RATIO_ROWS - 1
    w.abandoned = 2  # 2 / (MIN_RATIO_ROWS-1) is well above 1.5× any baseline
    w.re_asked = 2
    r = soak.evaluate(
        window=w, baseline=_baseline(re_asked=0.10, abandoned=0.05), eval_pass_rate=0.95
    )
    re_asked_check = next(c for c in r.checks if c.name == "re_asked_rate_ratio")
    abandoned_check = next(c for c in r.checks if c.name == "abandoned_rate_ratio")
    assert re_asked_check.status == "SKIP"
    assert abandoned_check.status == "SKIP"
    assert "MIN_RATIO_ROWS" in re_asked_check.detail
    # Eval/p95/exception gates still run — overall stays PASS when those PASS.
    assert r.overall_status == "PASS"


def test_evaluate_re_asked_within_1_5x_passes():
    w = _good_window()
    # baseline 10%; soak 13% → ratio 1.3 → PASS
    w.re_asked = 13
    w.total_rows = 100
    r = soak.evaluate(window=w, baseline=_baseline(re_asked=0.10), eval_pass_rate=0.95)
    ratio = next(c for c in r.checks if c.name == "re_asked_rate_ratio")
    assert ratio.status == "PASS"


def test_evaluate_noise_floor_suppresses_false_positive_when_baseline_tiny():
    # baseline 0.5%, soak 2% — ratio is 4× but absolute soak rate 2% < 5%
    # is statistical noise; should PASS rather than spam FAIL.
    w = _good_window()
    w.re_asked = 2
    w.total_rows = 100
    r = soak.evaluate(
        window=w, baseline=_baseline(re_asked=0.005), eval_pass_rate=0.95
    )
    ratio = next(c for c in r.checks if c.name == "re_asked_rate_ratio")
    assert ratio.status == "PASS"


def test_evaluate_zero_baseline_with_nonzero_soak_above_floor_fails():
    w = _good_window()
    w.abandoned = 10
    w.total_rows = 100  # soak 10% absolute, > noise floor
    r = soak.evaluate(window=w, baseline=_baseline(abandoned=0.0), eval_pass_rate=0.95)
    ratio = next(c for c in r.checks if c.name == "abandoned_rate_ratio")
    # Zero baseline with material soak rate is a real signal; ratio set
    # to -1.0 marker (inf) and the check should FAIL.
    assert ratio.status == "FAIL"


# ─── Baseline IO ─────────────────────────────────────────────────────────


def test_write_then_load_baseline_roundtrip(tmp_path):
    stats = _good_window()
    path = tmp_path / "baseline.json"
    written = soak.write_baseline(stats, path)
    assert path.exists()
    loaded = soak.load_baseline(path)
    assert loaded["re_asked_rate"] == stats.re_asked_rate
    assert loaded["abandoned_rate"] == stats.abandoned_rate
    assert loaded["total_rows"] == stats.total_rows
    assert "computed_at" in written


def test_load_baseline_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        soak.load_baseline(tmp_path / "nope.json")


# ─── End-to-end orchestration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_soak_check_returns_pass_when_all_clean(tmp_path):
    # Pre-write a baseline file
    baseline_path = tmp_path / "baseline.json"
    soak.write_baseline(
        soak.WindowStats(
            window_start="t0", window_end="t1", total_rows=200,
            re_asked=20, abandoned=10,
        ),
        baseline_path,
    )

    # Build a soak window with healthy stats — re_asked/abandoned within
    # 1.5× baseline so the ratio checks pass. Baseline = 10% / 5%; soak = 10% / 5%.
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)
    rows = (
        [_row(now - timedelta(minutes=i), signal="confirmed", latency_ms=60)
         for i in range(1, 18)]
        + [_row(now - timedelta(minutes=18), signal="re_asked", latency_ms=80),
           _row(now - timedelta(minutes=19), signal="re_asked", latency_ms=80),
           _row(now - timedelta(minutes=20), signal="abandoned", latency_ms=90)]
    )

    async def fake_eval():
        return 0.97

    result = await soak.run_soak_check(
        _make_factory(rows),
        baseline_path=baseline_path,
        window_hours=1,
        now=now,
        eval_runner=fake_eval,
    )
    assert result.overall_status == "PASS"
    assert result.eval_pass_rate == pytest.approx(0.97)
    assert result.window.total_rows == 20
    # Result serializes cleanly to JSON
    payload = json.dumps(result.as_dict())
    assert "overall_status" in payload


@pytest.mark.asyncio
async def test_build_default_router_p95_runner_returns_p95_from_bench(
    monkeypatch, tmp_path
):
    """The default p95 runner wires SemanticRouter into bench() and
    returns the measured p95. We stub the import targets so we don't
    need a live Ollama / pgvector — the contract is: build a router,
    call bench, return BenchResult.p95_ms."""
    queries_path = tmp_path / "router_eval.jsonl"
    queries_path.write_text(
        "\n".join(
            json.dumps({"query": f"q-{i}"}) for i in range(8)
        )
    )

    captured = {}

    class _FakeClassifier:
        def __init__(self, *a, **kw):
            captured["classifier_kw"] = kw

    class _FakeRouter:
        def __init__(self, *, classifier):
            captured["router_classifier"] = classifier

        async def route_first(self, q):  # pragma: no cover — bench timing
            return None

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        async def embed_router(self, text):  # pragma: no cover
            return [0.0]

    async def _fake_bench(*, route_fn, queries, warmup, gate_p95_ms):
        captured["bench_kwargs"] = {
            "warmup": warmup,
            "gate_p95_ms": gate_p95_ms,
            "n_queries_in": len(list(queries)),
        }
        from agent.router_latency_bench import BenchResult
        return BenchResult(
            n=5,
            warmup=warmup,
            timings_ms=[40, 50, 60, 70, 90],
            p50_ms=60.0,
            p95_ms=86.0,
            p99_ms=89.0,
            min_ms=40.0,
            max_ms=90.0,
            mean_ms=62.0,
            gate_p95_threshold_ms=gate_p95_ms,
            gate_passed=True,
        )

    import sys
    import types

    sr_mod = types.ModuleType("agent.semantic_router")
    sr_mod.SemanticIntentClassifier = _FakeClassifier
    sr_mod.SemanticRouter = _FakeRouter
    sr_mod.RoutingDecision = object
    monkeypatch.setitem(sys.modules, "agent.semantic_router", sr_mod)

    llm_mod = types.ModuleType("agent.llm")
    llm_mod.ModelClient = _FakeLLM
    monkeypatch.setitem(sys.modules, "agent.llm", llm_mod)

    bench_mod = types.ModuleType("agent.router_latency_bench")
    from agent.router_latency_bench import (
        BenchResult as _BR,
        load_queries as _lq,
    )
    bench_mod.BenchResult = _BR
    bench_mod.bench = _fake_bench
    bench_mod.load_queries = _lq
    monkeypatch.setitem(sys.modules, "agent.router_latency_bench", bench_mod)

    runner = soak.build_default_router_p95_runner(
        _make_factory([]),
        queries_path=queries_path,
        sample_n=5,
        warmup=2,
    )
    p95 = await runner()
    assert p95 == 86.0
    # Sample cap honored: 5 of the 8 queries fed to bench
    assert captured["bench_kwargs"]["n_queries_in"] == 5
    assert captured["bench_kwargs"]["warmup"] == 2
    assert captured["bench_kwargs"]["gate_p95_ms"] == soak.P95_LATENCY_MAX_MS
    # Classifier got the same factory the runner was built with
    assert "db_factory" in captured["classifier_kw"]


@pytest.mark.asyncio
async def test_build_default_router_p95_runner_raises_on_empty_queries(
    tmp_path,
):
    """If the queries file is empty / missing, the runner must fail
    loudly — silently returning 0.0 would falsely PASS the p95 check."""
    queries_path = tmp_path / "empty.jsonl"
    queries_path.write_text("")
    runner = soak.build_default_router_p95_runner(
        _make_factory([]),
        queries_path=queries_path,
        sample_n=5,
        warmup=0,
    )
    with pytest.raises(RuntimeError, match="no queries loaded"):
        await runner()


@pytest.mark.asyncio
async def test_run_soak_check_uses_router_p95_runner_when_supplied(tmp_path):
    """End-to-end: a supplied runner lifts the p95 gate off SKIP and
    the result reflects the runner's measurement."""
    baseline_path = tmp_path / "baseline.json"
    soak.write_baseline(
        soak.WindowStats(
            window_start="t0", window_end="t1", total_rows=200,
            re_asked=20, abandoned=10,
        ),
        baseline_path,
    )
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)

    async def fake_eval():
        return 0.95

    async def fake_p95():
        return 130.0

    result = await soak.run_soak_check(
        _make_factory([_row(now - timedelta(minutes=5), signal="confirmed")]),
        baseline_path=baseline_path,
        window_hours=1,
        now=now,
        eval_runner=fake_eval,
        router_p95_runner=fake_p95,
    )
    p95 = next(c for c in result.checks if c.name == "p95_router_latency_ms")
    assert p95.status == "PASS"
    assert p95.value == 130.0


@pytest.mark.asyncio
async def test_run_soak_check_rollback_on_eval_floor_breach(tmp_path):
    baseline_path = tmp_path / "baseline.json"
    soak.write_baseline(
        soak.WindowStats(
            window_start="t0", window_end="t1", total_rows=200,
            re_asked=20, abandoned=10,
        ),
        baseline_path,
    )
    now = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)

    async def crashing_eval():
        return 0.70

    result = await soak.run_soak_check(
        _make_factory([_row(now - timedelta(minutes=5))]),
        baseline_path=baseline_path,
        window_hours=1,
        now=now,
        eval_runner=crashing_eval,
    )
    assert result.overall_status == "ROLLBACK"
