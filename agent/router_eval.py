"""Router classification eval (Phase 2 Gate 6 / ongoing regression gate).

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md exit criterion 6:

    Phase 0's 100-query battery: ≥85% correct classification (becomes
    ongoing regression gate as ``tests/router_eval_set.jsonl``).

This module loads ``tests/router_eval_set.jsonl``, runs each query
through a classifier, and reports per-category and overall accuracy
versus the expected intent label. Deferrals (``should_clarify=True``)
count as misses against the gate — the regression gate measures
*correct classifications*, not OOD-defer behaviour (covered separately
by ``router_ood_eval.py``).

Privacy: the eval is offline. Embeddings come from the local
``qwen3-embedding:0.6b``; no external API calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from agent.semantic_router import ClassificationResult

DEFAULT_GATE_THRESHOLD: float = 0.85
DEFAULT_EVAL_SET_PATH: Path = Path("tests/router_eval_set.jsonl")


@dataclass
class EvalCase:
    id: str
    query: str
    expected_intent: str
    category: str = ""
    difficulty: int | None = None
    notes: str = ""


@dataclass
class EvalMiss:
    id: str
    query: str
    expected_intent: str
    actual_intent: str | None
    confidence: float
    deferred: bool
    defer_reason: str | None
    category: str = ""


@dataclass
class EvalReport:
    total: int = 0
    correct: int = 0
    deferred: int = 0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    misses: list[EvalMiss] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    def passes(self, threshold: float = DEFAULT_GATE_THRESHOLD) -> bool:
        return self.accuracy >= threshold

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "correct": self.correct,
            "deferred": self.deferred,
            "accuracy": round(self.accuracy, 4),
            "by_category": {
                cat: {
                    "total": stats["total"],
                    "correct": stats["correct"],
                    "accuracy": round(
                        stats["correct"] / stats["total"] if stats["total"] else 0.0,
                        4,
                    ),
                }
                for cat, stats in sorted(self.by_category.items())
            },
            "misses": [
                {
                    "id": m.id,
                    "query": m.query,
                    "expected": m.expected_intent,
                    "actual": m.actual_intent,
                    "confidence": round(m.confidence, 4),
                    "deferred": m.deferred,
                    "defer_reason": m.defer_reason,
                    "category": m.category,
                }
                for m in self.misses
            ],
        }


def load_cases(path: Path = DEFAULT_EVAL_SET_PATH) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        record = json.loads(line)
        cases.append(
            EvalCase(
                id=record["id"],
                query=record["query"],
                expected_intent=record["expected_intent"],
                category=record.get("category", ""),
                difficulty=record.get("difficulty"),
                notes=record.get("notes", ""),
            )
        )
    return cases


ClassifyFn = Callable[[str], Awaitable[ClassificationResult]]


async def evaluate(cases: list[EvalCase], classify_fn: ClassifyFn) -> EvalReport:
    report = EvalReport()
    for case in cases:
        report.total += 1
        cat_stats = report.by_category.setdefault(
            case.category or "uncategorised", {"total": 0, "correct": 0}
        )
        cat_stats["total"] += 1

        result = await classify_fn(case.query)
        deferred = bool(result.should_clarify)
        actual = result.intent_label
        if deferred:
            report.deferred += 1

        if not deferred and actual == case.expected_intent:
            report.correct += 1
            cat_stats["correct"] += 1
        else:
            report.misses.append(
                EvalMiss(
                    id=case.id,
                    query=case.query,
                    expected_intent=case.expected_intent,
                    actual_intent=actual,
                    confidence=result.confidence,
                    deferred=deferred,
                    defer_reason=result.defer_reason,
                    category=case.category,
                )
            )
    return report


async def _run_cli(args: argparse.Namespace) -> int:
    from agent import db as db_module
    from agent.config import settings
    from agent.llm import ModelClient
    from agent.semantic_router import SemanticIntentClassifier

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_eval: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)
    classifier = SemanticIntentClassifier(
        db_factory=factory,
        embed_fn=llm.embed_router,
    )

    cases = load_cases(Path(args.path))
    report = await evaluate(cases, classifier.classify)

    payload = report.as_dict()
    payload["threshold"] = args.threshold
    payload["passed"] = report.passes(args.threshold)
    print(json.dumps(payload, indent=2))
    return 0 if report.passes(args.threshold) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate router classification accuracy against the canonical eval set.",
    )
    parser.add_argument(
        "--path",
        default=str(DEFAULT_EVAL_SET_PATH),
        help=f"Path to eval jsonl (default: {DEFAULT_EVAL_SET_PATH}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_GATE_THRESHOLD,
        help=f"Pass threshold for accuracy (default: {DEFAULT_GATE_THRESHOLD}).",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
