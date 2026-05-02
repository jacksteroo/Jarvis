"""Unit tests for agent/router_ood_eval.py (Phase 2 Gate 4 tooling)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.router_ood_eval import (
    DEFAULT_OOD_SET_PATH,
    OodCase,
    OodReport,
    evaluate,
    load_ood_cases,
)
from agent.semantic_router import ClassificationResult


def _classified(label: str, conf: float = 0.9) -> ClassificationResult:
    return ClassificationResult(
        intent_label=label,
        confidence=conf,
        top_distance=0.1,
        runner_up_label=None,
        runner_up_confidence=0.0,
        is_ood=False,
        is_ambiguous=False,
        should_clarify=False,
        defer_reason=None,
        neighbours=[],
    )


def _deferred(reason: str) -> ClassificationResult:
    return ClassificationResult(
        intent_label=None,
        confidence=0.0,
        top_distance=0.5,
        runner_up_label=None,
        runner_up_confidence=0.0,
        is_ood=reason == "ood",
        is_ambiguous=reason == "ambiguous",
        should_clarify=True,
        defer_reason=reason,
        neighbours=[],
    )


def test_load_ood_cases_returns_twenty_records():
    cases = load_ood_cases(DEFAULT_OOD_SET_PATH)
    assert len(cases) == 20
    assert all(isinstance(c, OodCase) for c in cases)
    assert all(c.query.strip() for c in cases)


def test_load_ood_cases_skips_blank_lines(tmp_path: Path):
    p = tmp_path / "set.jsonl"
    p.write_text(
        '\n{"query": "first"}\n\n{"query": "second", "note": "n"}\n',
        encoding="utf-8",
    )
    cases = load_ood_cases(p)
    assert [c.query for c in cases] == ["first", "second"]
    assert cases[1].note == "n"


@pytest.mark.asyncio
async def test_evaluate_counts_defers_and_misses():
    cases = [OodCase(query=f"q{i}") for i in range(5)]
    outputs = [
        _deferred("ood"),
        _deferred("ood"),
        _deferred("ambiguous"),
        _classified("schedule_lookup", conf=0.7),
        _classified("inbox_summary", conf=0.6),
    ]
    pos = {"i": 0}

    async def classify(_q: str) -> ClassificationResult:
        out = outputs[pos["i"]]
        pos["i"] += 1
        return out

    report = await evaluate(cases, classify)
    assert report.total == 5
    assert report.deferred == 3
    assert report.defer_breakdown == {"ood": 2, "ambiguous": 1}
    assert report.defer_rate == pytest.approx(0.6)
    assert len(report.misses) == 2
    assert {m.intent_label for m in report.misses} == {"schedule_lookup", "inbox_summary"}


@pytest.mark.asyncio
async def test_evaluate_handles_unspecified_defer_reason():
    cases = [OodCase(query="q")]

    async def classify(_q: str) -> ClassificationResult:
        return ClassificationResult(
            intent_label=None,
            confidence=0.0,
            top_distance=0.5,
            runner_up_label=None,
            runner_up_confidence=0.0,
            is_ood=False,
            is_ambiguous=False,
            should_clarify=True,
            defer_reason=None,
            neighbours=[],
        )

    report = await evaluate(cases, classify)
    assert report.defer_breakdown == {"unspecified": 1}


def test_passes_uses_threshold():
    report = OodReport(total=20, deferred=16)
    assert report.passes(0.80)
    assert not report.passes(0.81)


def test_passes_handles_zero_total():
    report = OodReport()
    assert report.defer_rate == 0.0
    assert not report.passes()


def test_as_dict_round_trips_breakdown_and_misses():
    report = OodReport(
        total=2,
        deferred=1,
        defer_breakdown={"ood": 1},
    )
    from agent.router_ood_eval import OodMiss

    report.misses.append(OodMiss(query="hi", intent_label="general_chat", confidence=0.9))
    payload = report.as_dict()
    assert payload["total"] == 2
    assert payload["deferred"] == 1
    assert payload["defer_rate"] == 0.5
    assert payload["defer_breakdown"] == {"ood": 1}
    assert payload["misses"] == [
        {"query": "hi", "intent_label": "general_chat", "confidence": 0.9}
    ]
