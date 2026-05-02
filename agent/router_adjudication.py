"""Phase 2 divergence-adjudication tooling.

Phase 2 exit criterion #2 of docs/SEMANTIC_ROUTER_MIGRATION.md requires the
user to adjudicate ~50 shadow/regex divergence cases via Telegram (~40 min
batch). This module surfaces that batch.

Pipeline:

1. Sample divergence rows from ``routing_events`` — rows where the regex
   router and the shadow semantic router disagreed. Stratified by
   ``(regex_intent, shadow_intent)`` pair so the batch covers every
   divergence cohort, not just the most common ones.
2. Format as numbered Telegram messages (each chunk fits comfortably
   under Telegram's 4096-char limit). Each case shows the query plus the
   two competing routings; the user replies with ``<id> A`` (regex
   correct), ``<id> B`` (shadow correct), or ``<id> N`` (neither).
3. Persist the sample to ``logs/router_audit/adjudication_sample_*.jsonl``
   so a later run can map the user's freeform replies (read from
   ``docker compose logs pepper`` per the build prompt's Step 4a) back to
   specific routing_event rows.

CLI:

* ``python -m agent.router_adjudication --print`` — sample, format,
  print to stdout, and write the JSONL artifact (default; no Telegram
  side effect). Useful for dry-runs and inspection.
* ``python -m agent.router_adjudication --send`` — same as ``--print``
  plus push the formatted batch via :class:`JARViSTelegramBot` to the
  allowed user.

Privacy: only reads the local ``routing_events`` table and writes a
local JSONL file. The Telegram bot is the only outbound channel and it
goes to the operator's own ``TELEGRAM_ALLOWED_USER_IDS`` — same trust
boundary as Pepper's normal chat output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import structlog
from sqlalchemy import select

from agent.models import RoutingEvent

logger = structlog.get_logger(__name__)

DbFactory = Callable[[], Any]

# Telegram caps single messages at 4096 chars. Leaving headroom for a
# header and the parse-mode escape overhead, ~10 cases per chunk fits
# comfortably even for long queries.
DEFAULT_CHUNK_SIZE = 10
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_ARTIFACT_DIR = Path("logs/router_audit")

# Phase 2 exit criterion #2 thresholds (docs/SEMANTIC_ROUTER_MIGRATION.md):
# semantic correct ≥ 65% AND regex correct ≤ 35%.
GATE2_SEMANTIC_MIN = 0.65
GATE2_REGEX_MAX = 0.35

Verdict = Literal["regex", "shadow", "neither"]
_VERDICT_MAP: dict[str, Verdict] = {
    "A": "regex",
    "B": "shadow",
    "N": "neither",
}
# One id followed by an A/B/N letter, tolerating a leading "#", whitespace,
# a separator (-, :, ., or whitespace), and trailing junk on the line.
_REPLY_LINE_RE = re.compile(
    r"^\s*#?\s*(?P<id>\d+)\s*[-:.\s]+\s*(?P<verdict>[ABNabn])\b",
)


@dataclass(frozen=True)
class AdjudicationCase:
    """One divergence row queued for user adjudication."""

    event_id: int
    timestamp: datetime
    query_text: str
    regex_intent: str | None
    shadow_intent: str | None
    regex_confidence: float | None
    shadow_confidence: float | None

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "query_text": self.query_text,
            "regex_intent": self.regex_intent,
            "shadow_intent": self.shadow_intent,
            "regex_confidence": self.regex_confidence,
            "shadow_confidence": self.shadow_confidence,
        }


async def fetch_divergence_rows(
    db_factory: DbFactory, *, since: datetime | None = None
) -> list[AdjudicationCase]:
    """Pull every divergence row in scope (no sampling yet)."""

    stmt = (
        select(
            RoutingEvent.id,
            RoutingEvent.timestamp,
            RoutingEvent.query_text,
            RoutingEvent.regex_decision_intent,
            RoutingEvent.shadow_decision_intent,
            RoutingEvent.regex_decision_confidence,
            RoutingEvent.shadow_decision_confidence,
        )
        .where(RoutingEvent.shadow_decision_intent.is_not(None))
        .where(RoutingEvent.regex_decision_intent.is_not(None))
        .where(
            RoutingEvent.shadow_decision_intent
            != RoutingEvent.regex_decision_intent
        )
        .order_by(RoutingEvent.id.asc())
    )
    if since is not None:
        stmt = stmt.where(RoutingEvent.timestamp >= since)
    async with db_factory() as session:
        rows = (await session.execute(stmt)).all()
    return [
        AdjudicationCase(
            event_id=int(row[0]),
            timestamp=row[1],
            query_text=row[2],
            regex_intent=row[3],
            shadow_intent=row[4],
            regex_confidence=row[5],
            shadow_confidence=row[6],
        )
        for row in rows
    ]


def stratified_sample(
    cases: Iterable[AdjudicationCase], n: int
) -> list[AdjudicationCase]:
    """Stratified by (regex_intent, shadow_intent) pair.

    Deterministic given the input order: groups are sorted by descending
    population (so common cohorts get more slots) with a stable
    tiebreak, then within a group rows are taken in id order at evenly
    spaced indices. No RNG — re-running the same query gives the same
    sample, which matters for reproducibility when the user comes back
    to the batch hours later.
    """

    if n <= 0:
        return []

    groups: dict[tuple[str | None, str | None], list[AdjudicationCase]] = {}
    for case in cases:
        key = (case.regex_intent, case.shadow_intent)
        groups.setdefault(key, []).append(case)

    if not groups:
        return []

    total = sum(len(g) for g in groups.values())
    if total <= n:
        # Take everything, ordered by id for stable display.
        flat = [c for g in groups.values() for c in g]
        flat.sort(key=lambda c: c.event_id)
        return flat

    # Allocate slots: at most `n` groups get a floor of 1; the rest get 0.
    # Groups are prioritised by descending population so the most common
    # divergence cohorts always make the cut.
    keys = sorted(
        groups.keys(),
        key=lambda k: (-len(groups[k]), str(k[0] or ""), str(k[1] or "")),
    )
    base: dict[tuple[str | None, str | None], int] = {k: 0 for k in keys}
    if len(keys) >= n:
        # More cohorts than slots — give each top cohort exactly 1.
        for k in keys[:n]:
            base[k] = 1
    else:
        for k in keys:
            base[k] = max(1, len(groups[k]) * n // total)
            if base[k] > len(groups[k]):
                base[k] = len(groups[k])
        used = sum(base.values())
        # Top up shortfalls by walking the largest groups first.
        i = 0
        while used < n and any(base[k] < len(groups[k]) for k in keys):
            k = keys[i % len(keys)]
            if base[k] < len(groups[k]):
                base[k] += 1
                used += 1
            i += 1
        # Trim overshoot from the smallest groups first.
        if used > n:
            for k in sorted(keys, key=lambda k: len(groups[k])):
                while base[k] > 1 and used > n:
                    base[k] -= 1
                    used -= 1
                if used <= n:
                    break

    picked: list[AdjudicationCase] = []
    for k in keys:
        slots = base[k]
        if slots <= 0:
            continue
        rows = sorted(groups[k], key=lambda c: c.event_id)
        if slots >= len(rows):
            picked.extend(rows)
            continue
        # Evenly spaced indices across the group: deterministic and
        # avoids only-recent or only-oldest bias.
        step = len(rows) / slots
        idxs = sorted({min(len(rows) - 1, int(i * step)) for i in range(slots)})
        # Top up if rounding collisions dropped any slots.
        i = 0
        while len(idxs) < slots and i < len(rows):
            if i not in idxs:
                idxs.append(i)
            i += 1
        idxs.sort()
        picked.extend(rows[i] for i in idxs)

    picked.sort(key=lambda c: c.event_id)
    return picked


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_batch_messages(
    cases: list[AdjudicationCase], *, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> list[str]:
    """Render the sample into Telegram-sized text messages.

    Plain-text (no Markdown) so the operator's reply parsing — which
    will key on the numeric ``event_id`` plus a single A/B/N letter —
    isn't perturbed by escape characters.
    """

    if not cases:
        return ["router adjudication: no divergence rows in scope."]

    chunk_size = max(1, chunk_size)
    chunks: list[list[AdjudicationCase]] = [
        cases[i : i + chunk_size] for i in range(0, len(cases), chunk_size)
    ]
    messages: list[str] = []
    total = len(cases)
    for idx, chunk in enumerate(chunks, start=1):
        header = (
            f"Router adjudication batch {idx}/{len(chunks)} "
            f"({len(chunk)} of {total} cases)\n"
            "Reply with one line per case: <id> A (regex), <id> B (shadow), "
            "<id> N (neither).\n"
        )
        body_lines: list[str] = []
        for case in chunk:
            rc = "—" if case.regex_confidence is None else f"{case.regex_confidence:.2f}"
            sc = (
                "—"
                if case.shadow_confidence is None
                else f"{case.shadow_confidence:.2f}"
            )
            body_lines.append(
                f"\n[#{case.event_id}] {_truncate(case.query_text, 220)}\n"
                f"  A) regex  → {case.regex_intent} ({rc})\n"
                f"  B) shadow → {case.shadow_intent} ({sc})"
            )
        messages.append(header + "".join(body_lines))
    return messages


def write_sample_artifact(
    cases: list[AdjudicationCase],
    *,
    out_dir: Path = DEFAULT_ARTIFACT_DIR,
    now: datetime | None = None,
) -> Path:
    """Persist the sample to JSONL for later reply-mapping.

    Each line is one :class:`AdjudicationCase` as a JSON object. The
    filename embeds a UTC timestamp so multiple batches don't clobber.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"adjudication_sample_{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case.as_dict()) + "\n")
    return path


@dataclass(frozen=True)
class AdjudicationVerdict:
    """One operator verdict for a divergence case."""

    event_id: int
    verdict: Verdict
    raw: str


def parse_reply_text(text: str) -> list[AdjudicationVerdict]:
    """Parse the operator's free-form Telegram reply into verdicts.

    Tolerant by design: lines we don't recognise are skipped, not raised.
    The operator may reply across multiple Telegram messages copied into
    one buffer, may include the leading ``#`` we used in the prompt, and
    may use any of ``-`` ``:`` ``.`` or whitespace as a separator. Last
    verdict wins if the same id appears more than once (operator
    correction).
    """

    seen: dict[int, AdjudicationVerdict] = {}
    for raw_line in text.splitlines():
        match = _REPLY_LINE_RE.match(raw_line)
        if match is None:
            continue
        event_id = int(match.group("id"))
        letter = match.group("verdict").upper()
        seen[event_id] = AdjudicationVerdict(
            event_id=event_id,
            verdict=_VERDICT_MAP[letter],
            raw=raw_line.rstrip(),
        )
    return list(seen.values())


def load_sample_artifact(path: Path) -> list[AdjudicationCase]:
    """Re-hydrate the JSONL artifact written by ``write_sample_artifact``."""

    cases: list[AdjudicationCase] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cases.append(
                AdjudicationCase(
                    event_id=int(row["event_id"]),
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    query_text=row["query_text"],
                    regex_intent=row.get("regex_intent"),
                    shadow_intent=row.get("shadow_intent"),
                    regex_confidence=row.get("regex_confidence"),
                    shadow_confidence=row.get("shadow_confidence"),
                )
            )
    return cases


@dataclass(frozen=True)
class Gate2Result:
    """Phase 2 exit criterion #2 outcome."""

    sample_size: int
    adjudicated: int
    regex_correct: int
    shadow_correct: int
    neither: int
    unadjudicated_event_ids: list[int]
    unknown_event_ids: list[int]  # verdicts for ids not in the sample
    regex_correct_pct: float
    shadow_correct_pct: float
    semantic_target_met: bool
    regex_target_met: bool
    gate_passed: bool

    def as_dict(self) -> dict:
        return {
            "sample_size": self.sample_size,
            "adjudicated": self.adjudicated,
            "regex_correct": self.regex_correct,
            "shadow_correct": self.shadow_correct,
            "neither": self.neither,
            "unadjudicated_event_ids": list(self.unadjudicated_event_ids),
            "unknown_event_ids": list(self.unknown_event_ids),
            "regex_correct_pct": self.regex_correct_pct,
            "shadow_correct_pct": self.shadow_correct_pct,
            "semantic_target_met": self.semantic_target_met,
            "regex_target_met": self.regex_target_met,
            "gate_passed": self.gate_passed,
            "thresholds": {
                "semantic_min": GATE2_SEMANTIC_MIN,
                "regex_max": GATE2_REGEX_MAX,
            },
        }


def evaluate_gate2(
    cases: Iterable[AdjudicationCase],
    verdicts: Iterable[AdjudicationVerdict],
    *,
    semantic_min: float = GATE2_SEMANTIC_MIN,
    regex_max: float = GATE2_REGEX_MAX,
) -> Gate2Result:
    """Compute Gate 2 metrics from the sample + operator verdicts.

    Percentages are over the *adjudicated* count (ids actually answered),
    not the full sample. ``unadjudicated_event_ids`` surfaces what's
    still missing so the next run can re-prompt the operator.
    """

    sample_ids: list[int] = [c.event_id for c in cases]
    sample_set = set(sample_ids)
    verdict_by_id: dict[int, Verdict] = {}
    unknown: list[int] = []
    for v in verdicts:
        if v.event_id not in sample_set:
            unknown.append(v.event_id)
            continue
        verdict_by_id[v.event_id] = v.verdict

    regex_correct = sum(1 for v in verdict_by_id.values() if v == "regex")
    shadow_correct = sum(1 for v in verdict_by_id.values() if v == "shadow")
    neither = sum(1 for v in verdict_by_id.values() if v == "neither")
    adjudicated = len(verdict_by_id)
    unadjudicated = sorted(sample_set - verdict_by_id.keys())

    if adjudicated == 0:
        regex_pct = 0.0
        shadow_pct = 0.0
    else:
        regex_pct = regex_correct / adjudicated
        shadow_pct = shadow_correct / adjudicated

    semantic_ok = shadow_pct >= semantic_min
    regex_ok = regex_pct <= regex_max
    return Gate2Result(
        sample_size=len(sample_ids),
        adjudicated=adjudicated,
        regex_correct=regex_correct,
        shadow_correct=shadow_correct,
        neither=neither,
        unadjudicated_event_ids=unadjudicated,
        unknown_event_ids=sorted(set(unknown)),
        regex_correct_pct=regex_pct,
        shadow_correct_pct=shadow_pct,
        semantic_target_met=semantic_ok,
        regex_target_met=regex_ok,
        gate_passed=adjudicated > 0 and semantic_ok and regex_ok,
    )


def write_gate2_result(
    result: Gate2Result,
    *,
    out_dir: Path = DEFAULT_ARTIFACT_DIR,
    now: datetime | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"adjudication_gate2_{stamp}.json"
    path.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    return path


def _run_ingest(args: argparse.Namespace) -> int:
    sample_path = Path(args.sample)
    replies_path = Path(args.ingest_replies)
    if not sample_path.exists():
        print(f"router_adjudication: sample artifact not found: {sample_path}")
        return 4
    if not replies_path.exists():
        print(f"router_adjudication: replies file not found: {replies_path}")
        return 4

    cases = load_sample_artifact(sample_path)
    verdicts = parse_reply_text(replies_path.read_text(encoding="utf-8"))
    result = evaluate_gate2(cases, verdicts)
    out = write_gate2_result(result, out_dir=Path(args.out_dir))

    print(
        f"router_adjudication ingest: sample={result.sample_size} "
        f"adjudicated={result.adjudicated} "
        f"regex_correct={result.regex_correct} "
        f"shadow_correct={result.shadow_correct} "
        f"neither={result.neither}"
    )
    print(
        f"  semantic_correct_pct={result.shadow_correct_pct:.3f} "
        f"(target ≥ {GATE2_SEMANTIC_MIN}) → "
        f"{'PASS' if result.semantic_target_met else 'FAIL'}"
    )
    print(
        f"  regex_correct_pct={result.regex_correct_pct:.3f} "
        f"(target ≤ {GATE2_REGEX_MAX}) → "
        f"{'PASS' if result.regex_target_met else 'FAIL'}"
    )
    print(f"  Gate 2: {'PASSED' if result.gate_passed else 'FAILED'}")
    if result.unadjudicated_event_ids:
        print(
            f"  unadjudicated ids ({len(result.unadjudicated_event_ids)}): "
            f"{result.unadjudicated_event_ids[:20]}"
            f"{'…' if len(result.unadjudicated_event_ids) > 20 else ''}"
        )
    if result.unknown_event_ids:
        print(
            f"  ignored verdicts for ids not in sample: {result.unknown_event_ids}"
        )
    print(f"  artifact: {out}")
    return 0


async def _run_cli(args: argparse.Namespace) -> int:
    if args.ingest_replies:
        return _run_ingest(args)

    from agent import db as db_module
    from agent.config import settings

    await db_module.init_db(settings)
    factory = db_module._session_factory
    if factory is None:
        print("router_adjudication: DB session factory missing after init_db")
        return 2

    pool = await fetch_divergence_rows(factory, since=args.since)
    sample = stratified_sample(pool, args.n)
    if not sample:
        print(
            f"router_adjudication: no divergence rows "
            f"(pool={len(pool)}, requested={args.n})."
        )
        return 0

    messages = format_batch_messages(sample, chunk_size=args.chunk_size)
    artifact = write_sample_artifact(sample, out_dir=Path(args.out_dir))

    print(
        f"router_adjudication: sampled {len(sample)} of {len(pool)} "
        f"divergence rows; artifact={artifact}"
    )
    for msg in messages:
        print("\n" + ("─" * 60))
        print(msg)

    if args.send:
        from agent.config import settings as live_settings
        from agent.telegram_bot import JARViSTelegramBot

        token = live_settings.TELEGRAM_BOT_TOKEN
        if not token:
            print(
                "router_adjudication: --send requested but TELEGRAM_BOT_TOKEN is unset"
            )
            return 3
        bot = JARViSTelegramBot(token=token, pepper_core=None, config=live_settings)
        # send_message uses the underlying Bot, which we initialise lazily
        # inside start(); for a one-shot push we mirror its setup minus
        # the polling loop.
        from telegram import Bot

        bot._bot = Bot(token=token)
        for msg in messages:
            await bot.send_message(msg, parse_mode=None)
        logger.info(
            "router_adjudication_sent",
            chunks=len(messages),
            cases=len(sample),
            artifact=str(artifact),
        )

    return 0


def _parse_since(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be YYYY-MM-DD or ISO8601, got: {raw!r}"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sample shadow/regex divergence rows and stage them for "
        "Telegram adjudication (Phase 2 exit criterion #2).",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Sample size (default {DEFAULT_SAMPLE_SIZE}).",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="Bound to rows with timestamp >= this value (YYYY-MM-DD or ISO8601).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Cases per Telegram message (default {DEFAULT_CHUNK_SIZE}).",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_ARTIFACT_DIR),
        help=f"Where to write the JSONL artifact (default {DEFAULT_ARTIFACT_DIR}).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Also push the formatted batch via Telegram. Without this flag, "
        "the CLI is a dry-run that only prints + writes the artifact.",
    )
    parser.add_argument(
        "--ingest-replies",
        default=None,
        help="Path to a text file containing the operator's reply lines "
        "(<id> A|B|N). When set, the CLI switches to ingest mode: parse "
        "replies, join with --sample, and emit a Gate 2 verdict artifact. "
        "Bypasses DB/Telegram.",
    )
    parser.add_argument(
        "--sample",
        default=None,
        help="Path to the JSONL artifact previously written by a sample run. "
        "Required with --ingest-replies.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.ingest_replies and not args.sample:
        print("router_adjudication: --ingest-replies requires --sample")
        return 2
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
