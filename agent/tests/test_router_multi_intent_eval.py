"""Unit tests for ``agent.router_multi_intent_eval``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.router_multi_intent_eval import (
    DEFAULT_GATE_THRESHOLD,
    DEFAULT_TEST_SET_PATH,
    MultiIntentCase,
    MultiIntentReport,
    evaluate,
    load_cases,
)


def _write_jsonl(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "cases.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_load_cases_parses_query_and_fragments(tmp_path: Path):
    p = _write_jsonl(
        tmp_path,
        [
            '{"query": "a and b", "expected_fragments": ["a", "b"], "note": "n"}',
        ],
    )
    cases = load_cases(p)
    assert len(cases) == 1
    assert cases[0].query == "a and b"
    assert cases[0].expected_fragments == ["a", "b"]
    assert cases[0].note == "n"


def test_load_cases_skips_blank_lines(tmp_path: Path):
    p = _write_jsonl(
        tmp_path,
        [
            "",
            '{"query": "x", "expected_fragments": ["x"]}',
            "   ",
            '{"query": "y and z", "expected_fragments": ["y", "z"]}',
            "",
        ],
    )
    cases = load_cases(p)
    assert [c.query for c in cases] == ["x", "y and z"]


def test_load_cases_defaults_note_to_empty_string(tmp_path: Path):
    p = _write_jsonl(
        tmp_path,
        ['{"query": "x", "expected_fragments": ["x"]}'],
    )
    cases = load_cases(p)
    assert cases[0].note == ""


def test_evaluate_counts_correct_and_misses():
    cases = [
        MultiIntentCase(query="a and b", expected_fragments=["a", "b"]),
        MultiIntentCase(query="solo query", expected_fragments=["solo query"]),
        MultiIntentCase(query="x and y", expected_fragments=["wrong"]),
    ]
    report = evaluate(cases)
    assert report.total == 3
    assert report.correct == 2
    assert len(report.misses) == 1
    miss = report.misses[0]
    assert miss.query == "x and y"
    assert miss.expected == ["wrong"]
    assert miss.actual == ["x", "y"]


def test_evaluate_accuracy_zero_total_safe():
    report = evaluate([])
    assert report.total == 0
    assert report.accuracy == 0.0
    assert report.passes(0.0) is True


def test_passes_threshold_gate():
    cases = [
        MultiIntentCase(query="a and b", expected_fragments=["a", "b"]),
        MultiIntentCase(query="c and d", expected_fragments=["c", "d"]),
    ]
    report = evaluate(cases)
    assert report.passes(1.0) is True
    assert report.passes(0.5) is True


def test_as_dict_serializes_report():
    cases = [
        MultiIntentCase(query="a and b", expected_fragments=["a", "b"]),
        MultiIntentCase(
            query="x and y",
            expected_fragments=["wrong"],
            note="bad",
        ),
    ]
    report = evaluate(cases)
    payload = report.as_dict()
    assert payload["total"] == 2
    assert payload["correct"] == 1
    assert payload["accuracy"] == 0.5
    assert payload["misses"][0]["query"] == "x and y"
    assert payload["misses"][0]["expected"] == ["wrong"]
    assert payload["misses"][0]["actual"] == ["x", "y"]
    assert payload["misses"][0]["note"] == "bad"


def test_curated_test_set_meets_gate():
    """The shipped test set must clear Gate 5 (≥90%) against the
    real splitter — this is the regression guard for the gate itself."""
    path = Path(__file__).resolve().parents[2] / DEFAULT_TEST_SET_PATH
    cases = load_cases(path)
    assert len(cases) == 30, "Gate 5 spec requires 30 curated queries"
    report = evaluate(cases)
    assert report.passes(DEFAULT_GATE_THRESHOLD), (
        f"Gate 5 regression: accuracy={report.accuracy:.4f} "
        f"misses={[(m.query, m.actual) for m in report.misses]}"
    )


def test_default_threshold_matches_migration_plan():
    assert DEFAULT_GATE_THRESHOLD == pytest.approx(0.90)


def test_evaluate_uses_singleton_when_no_boundary():
    cases = [MultiIntentCase(query="hello world", expected_fragments=["hello world"])]
    report = evaluate(cases)
    assert report.correct == 1
    assert report.misses == []


def test_misses_preserve_note_for_diagnostics():
    cases = [
        MultiIntentCase(
            query="quoted \"plus one\" stays",
            expected_fragments=["WRONG"],
            note="quoted span",
        ),
    ]
    report = evaluate(cases)
    assert report.misses[0].note == "quoted span"
