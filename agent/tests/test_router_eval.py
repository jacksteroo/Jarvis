"""Unit tests for agent/router_eval.py (Phase 2 Gate 6 / regression gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.router_eval import (
    DEFAULT_EVAL_SET_PATH,
    EvalCase,
    EvalMiss,
    EvalReport,
    evaluate,
    load_cases,
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


def _deferred(reason: str = "ood") -> ClassificationResult:
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


def test_load_cases_returns_canonical_battery():
    cases = load_cases(DEFAULT_EVAL_SET_PATH)
    assert len(cases) == 100
    assert all(isinstance(c, EvalCase) for c in cases)
    assert all(c.query.strip() and c.expected_intent for c in cases)
    # All nine non-UNKNOWN intents in the SemanticRouter vocabulary should appear at
    # least once. ``unsupported_capability`` was added in Phase 2 iter 15 adjudication
    # to label queries about subsystems that aren't integrated yet (Health, Meal log).
    labels = {c.expected_intent for c in cases}
    assert labels == {
        "action_items",
        "capability_check",
        "conversation_lookup",
        "cross_source_triage",
        "general_chat",
        "inbox_summary",
        "person_lookup",
        "schedule_lookup",
        "unsupported_capability",
    }


def test_load_cases_skips_blank_lines(tmp_path: Path):
    p = tmp_path / "set.jsonl"
    p.write_text(
        '\n{"id": "a", "query": "first", "expected_intent": "general_chat"}\n\n'
        '{"id": "b", "query": "second", "expected_intent": "schedule_lookup", '
        '"category": "Cal", "difficulty": 2, "notes": "n"}\n',
        encoding="utf-8",
    )
    cases = load_cases(p)
    assert [c.id for c in cases] == ["a", "b"]
    assert cases[1].category == "Cal"
    assert cases[1].difficulty == 2
    assert cases[1].notes == "n"


def test_load_cases_defaults_optional_fields(tmp_path: Path):
    p = tmp_path / "set.jsonl"
    p.write_text(
        '{"id": "x", "query": "q", "expected_intent": "general_chat"}\n',
        encoding="utf-8",
    )
    [case] = load_cases(p)
    assert case.category == ""
    assert case.difficulty is None
    assert case.notes == ""


@pytest.mark.asyncio
async def test_evaluate_counts_correct_misses_and_defers():
    cases = [
        EvalCase(id="1", query="q1", expected_intent="schedule_lookup", category="Cal"),
        EvalCase(id="2", query="q2", expected_intent="schedule_lookup", category="Cal"),
        EvalCase(id="3", query="q3", expected_intent="inbox_summary", category="Email"),
        EvalCase(id="4", query="q4", expected_intent="inbox_summary", category="Email"),
        EvalCase(id="5", query="q5", expected_intent="general_chat", category="Chat"),
    ]
    outputs = [
        _classified("schedule_lookup"),                # correct
        _classified("inbox_summary", conf=0.7),        # wrong label
        _classified("inbox_summary"),                  # correct
        _deferred("ood"),                              # deferred → miss
        _classified("general_chat"),                   # correct
    ]
    pos = {"i": 0}

    async def classify(_q: str) -> ClassificationResult:
        out = outputs[pos["i"]]
        pos["i"] += 1
        return out

    report = await evaluate(cases, classify)
    assert report.total == 5
    assert report.correct == 3
    assert report.deferred == 1
    assert report.accuracy == pytest.approx(0.6)
    assert len(report.misses) == 2
    miss_ids = {m.id for m in report.misses}
    assert miss_ids == {"2", "4"}
    deferred_miss = next(m for m in report.misses if m.id == "4")
    assert deferred_miss.deferred is True
    assert deferred_miss.defer_reason == "ood"
    assert report.by_category == {
        "Cal": {"total": 2, "correct": 1},
        "Email": {"total": 2, "correct": 1},
        "Chat": {"total": 1, "correct": 1},
    }


@pytest.mark.asyncio
async def test_evaluate_uses_uncategorised_bucket_for_blank_category():
    cases = [EvalCase(id="x", query="q", expected_intent="general_chat")]

    async def classify(_q: str) -> ClassificationResult:
        return _classified("general_chat")

    report = await evaluate(cases, classify)
    assert report.by_category == {"uncategorised": {"total": 1, "correct": 1}}


def test_passes_uses_threshold():
    report = EvalReport(total=100, correct=85)
    assert report.passes(0.85)
    assert not report.passes(0.86)


def test_passes_handles_zero_total():
    report = EvalReport()
    assert report.accuracy == 0.0
    assert not report.passes()


def test_as_dict_serialises_categories_and_misses():
    report = EvalReport(
        total=3,
        correct=2,
        deferred=1,
        by_category={"Cal": {"total": 2, "correct": 2}, "Chat": {"total": 1, "correct": 0}},
    )
    report.misses.append(
        EvalMiss(
            id="3",
            query="q3",
            expected_intent="general_chat",
            actual_intent=None,
            confidence=0.0,
            deferred=True,
            defer_reason="ood",
            category="Chat",
        )
    )
    payload = report.as_dict()
    assert payload["total"] == 3
    assert payload["correct"] == 2
    assert payload["deferred"] == 1
    assert payload["accuracy"] == pytest.approx(0.6667, rel=1e-3)
    assert payload["by_category"]["Cal"] == {"total": 2, "correct": 2, "accuracy": 1.0}
    assert payload["by_category"]["Chat"] == {"total": 1, "correct": 0, "accuracy": 0.0}
    assert payload["misses"] == [
        {
            "id": "3",
            "query": "q3",
            "expected": "general_chat",
            "actual": None,
            "confidence": 0.0,
            "deferred": True,
            "defer_reason": "ood",
            "category": "Chat",
        }
    ]


@pytest.mark.asyncio
async def test_canonical_eval_set_loads_and_classifies_with_stub_classifier():
    """Regression guard: the shipped router_eval_set.jsonl must run cleanly
    through the evaluator with any classify_fn — no schema drift, no
    field-name surprises, no shape mismatches with ClassificationResult.
    """
    cases = load_cases(DEFAULT_EVAL_SET_PATH)
    assert len(cases) == 100

    async def always_general_chat(_q: str) -> ClassificationResult:
        return _classified("general_chat")

    report = await evaluate(cases, always_general_chat)
    assert report.total == 100
    # The stub classifier only matches the general_chat cases
    expected_correct = sum(1 for c in cases if c.expected_intent == "general_chat")
    assert report.correct == expected_correct
    assert report.deferred == 0
    payload = report.as_dict()
    assert "by_category" in payload
    assert sum(stats["total"] for stats in payload["by_category"].values()) == 100
