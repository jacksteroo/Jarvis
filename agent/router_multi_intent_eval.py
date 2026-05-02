"""Multi-intent split-accuracy eval (Phase 2 Gate 5).

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md: ``split_multi_intent`` must
match the expected fragment list on ≥90% of a 30-query test set.

The eval is fully offline and deterministic — no DB, no LLM, no
external API calls. It loads ``tests/router_multi_intent_set.jsonl`` and
compares ``split_multi_intent(query)`` to ``expected_fragments`` per row.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from agent.multi_intent_splitter import split_multi_intent

DEFAULT_GATE_THRESHOLD: float = 0.90
DEFAULT_TEST_SET_PATH: Path = Path("tests/router_multi_intent_set.jsonl")


@dataclass
class MultiIntentCase:
    query: str
    expected_fragments: list[str]
    note: str = ""


@dataclass
class MultiIntentMiss:
    query: str
    expected: list[str]
    actual: list[str]
    note: str


@dataclass
class MultiIntentReport:
    total: int = 0
    correct: int = 0
    misses: list[MultiIntentMiss] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def passes(self, threshold: float = DEFAULT_GATE_THRESHOLD) -> bool:
        return self.accuracy >= threshold

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy": round(self.accuracy, 4),
            "misses": [
                {
                    "query": m.query,
                    "expected": list(m.expected),
                    "actual": list(m.actual),
                    "note": m.note,
                }
                for m in self.misses
            ],
        }


def load_cases(path: Path = DEFAULT_TEST_SET_PATH) -> list[MultiIntentCase]:
    cases: list[MultiIntentCase] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        record = json.loads(line)
        cases.append(
            MultiIntentCase(
                query=record["query"],
                expected_fragments=list(record["expected_fragments"]),
                note=record.get("note", ""),
            )
        )
    return cases


def evaluate(cases: list[MultiIntentCase]) -> MultiIntentReport:
    report = MultiIntentReport()
    for case in cases:
        report.total += 1
        actual = split_multi_intent(case.query)
        if actual == case.expected_fragments:
            report.correct += 1
        else:
            report.misses.append(
                MultiIntentMiss(
                    query=case.query,
                    expected=list(case.expected_fragments),
                    actual=list(actual),
                    note=case.note,
                )
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate multi-intent split accuracy against the curated test set.",
    )
    parser.add_argument(
        "--path",
        default=str(DEFAULT_TEST_SET_PATH),
        help=f"Path to multi-intent jsonl (default: {DEFAULT_TEST_SET_PATH}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_GATE_THRESHOLD,
        help=f"Pass threshold for split accuracy (default: {DEFAULT_GATE_THRESHOLD}).",
    )
    args = parser.parse_args()

    cases = load_cases(Path(args.path))
    report = evaluate(cases)
    payload = report.as_dict()
    payload["threshold"] = args.threshold
    payload["passed"] = report.passes(args.threshold)
    print(json.dumps(payload, indent=2))
    return 0 if report.passes(args.threshold) else 1


if __name__ == "__main__":
    raise SystemExit(main())
