"""OOD-detection eval for the semantic router (Phase 2 Gate 4).

Phase 2 of docs/SEMANTIC_ROUTER_MIGRATION.md: the semantic router must
defer (``ASK_CLARIFYING``) on ≥80% of a 20-query nonsense test set.
This module loads ``tests/router_ood_set.jsonl``, runs each query
through a classifier, and reports how many were deferred and why.

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

DEFAULT_GATE_THRESHOLD: float = 0.80
DEFAULT_OOD_SET_PATH: Path = Path("tests/router_ood_set.jsonl")


@dataclass
class OodCase:
    query: str
    note: str = ""


@dataclass
class OodMiss:
    query: str
    intent_label: str | None
    confidence: float


@dataclass
class OodReport:
    total: int = 0
    deferred: int = 0
    defer_breakdown: dict[str, int] = field(default_factory=dict)
    misses: list[OodMiss] = field(default_factory=list)

    @property
    def defer_rate(self) -> float:
        return self.deferred / self.total if self.total else 0.0

    def passes(self, threshold: float = DEFAULT_GATE_THRESHOLD) -> bool:
        return self.defer_rate >= threshold

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "deferred": self.deferred,
            "defer_rate": round(self.defer_rate, 4),
            "defer_breakdown": dict(self.defer_breakdown),
            "misses": [
                {
                    "query": m.query,
                    "intent_label": m.intent_label,
                    "confidence": round(m.confidence, 4),
                }
                for m in self.misses
            ],
        }


def load_ood_cases(path: Path = DEFAULT_OOD_SET_PATH) -> list[OodCase]:
    cases: list[OodCase] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        record = json.loads(line)
        cases.append(OodCase(query=record["query"], note=record.get("note", "")))
    return cases


ClassifyFn = Callable[[str], Awaitable[ClassificationResult]]


async def evaluate(cases: list[OodCase], classify_fn: ClassifyFn) -> OodReport:
    report = OodReport()
    for case in cases:
        report.total += 1
        result = await classify_fn(case.query)
        if result.should_clarify:
            report.deferred += 1
            reason = result.defer_reason or "unspecified"
            report.defer_breakdown[reason] = report.defer_breakdown.get(reason, 0) + 1
        else:
            report.misses.append(
                OodMiss(
                    query=case.query,
                    intent_label=result.intent_label,
                    confidence=result.confidence,
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
        print("router_ood_eval: DB session factory missing after init_db")
        return 2

    llm = ModelClient(settings)
    classifier = SemanticIntentClassifier(
        db_factory=factory,
        embed_fn=llm.embed_router,
    )

    cases = load_ood_cases(Path(args.path))
    report = await evaluate(cases, classifier.classify)

    payload = report.as_dict()
    payload["threshold"] = args.threshold
    payload["passed"] = report.passes(args.threshold)
    print(json.dumps(payload, indent=2))
    return 0 if report.passes(args.threshold) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate router OOD-defer rate against the nonsense test set.",
    )
    parser.add_argument(
        "--path",
        default=str(DEFAULT_OOD_SET_PATH),
        help=f"Path to OOD jsonl (default: {DEFAULT_OOD_SET_PATH}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_GATE_THRESHOLD,
        help=f"Pass threshold for defer rate (default: {DEFAULT_GATE_THRESHOLD}).",
    )
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
