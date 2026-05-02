"""Phase 3 post-cutover soak-window rollup.

Reads ``logs/router_audit/soak_*.json`` artifacts produced by
``agent.router_soak_monitor`` and reports the cumulative state of the
3-day soak window. The migration plan's Phase 3 exit criterion is "3-day
soak passes all checks": this module is the verification logic that
turns a directory of hourly soak reports into a single PASS / INCOMPLETE
verdict.

Privacy: reads only structured JSON files already on disk. No DB access,
no Claude API, no external IO. The aggregate report contains counts +
ISO timestamps + file paths only — no query text or PII.

CLI:
  .venv/bin/python -m agent.router_soak_rollup [--cutover-at ISO]
                                                [--audit-dir DIR]
                                                [--window-hours N]
                                                [--json]

Exit codes:
  0  soak window complete (≥window-hours of contiguous PASS)
  1  soak incomplete (any FAIL/ROLLBACK or insufficient elapsed time)
  5  audit dir missing / unreadable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Cutover commit cd8cf23 landed 2026-04-29T07:09:00Z (per
# tasks_completed_this_phase[iter 124] and migration plan §Phase 3).
DEFAULT_CUTOVER_AT = datetime(2026, 4, 29, 7, 9, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW_HOURS = 72  # 3-day exit criterion
DEFAULT_AUDIT_DIR = Path("logs/router_audit")
BASELINE_FILENAME = "soak_baseline.json"


@dataclass
class RollupResult:
    cutover_at: str
    audit_dir: str
    window_hours: int
    file_count: int
    pass_count: int
    fail_count: int
    rollback_count: int
    earliest: str | None
    latest: str | None
    elapsed_hours: float
    contiguous_pass: bool
    soak_complete: bool
    soak_complete_reason: str
    files: list[dict[str, str]] = field(default_factory=list)


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _discover(audit_dir: Path) -> list[Path]:
    return sorted(
        p for p in audit_dir.glob("soak_*.json") if p.name != BASELINE_FILENAME
    )


def rollup(
    cutover_at: datetime,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> RollupResult:
    """Aggregate every post-cutover soak report under ``audit_dir``.

    A run counts toward the soak only if its ``generated_at`` is at or
    after ``cutover_at``. The window is "complete" when every counted
    report is PASS *and* the span (earliest → latest) is ≥ ``window_hours``.
    """

    files = _discover(audit_dir)
    summaries: list[dict[str, str]] = []
    earliest: datetime | None = None
    latest: datetime | None = None
    pass_count = fail_count = rollback_count = 0
    contiguous_pass = True
    saw_post_cutover = False

    for p in files:
        try:
            data: dict[str, Any] = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        gen_str = data.get("generated_at")
        if not isinstance(gen_str, str):
            continue
        try:
            gen = _parse_iso(gen_str)
        except ValueError:
            continue
        if gen < cutover_at:
            continue
        saw_post_cutover = True
        status = str(data.get("overall_status", "UNKNOWN"))
        if earliest is None or gen < earliest:
            earliest = gen
        if latest is None or gen > latest:
            latest = gen
        if status == "PASS":
            pass_count += 1
        elif status == "FAIL":
            fail_count += 1
            contiguous_pass = False
        elif status == "ROLLBACK":
            rollback_count += 1
            contiguous_pass = False
        else:
            contiguous_pass = False
        summaries.append({
            "file": str(p),
            "generated_at": gen_str,
            "status": status,
        })

    if saw_post_cutover and earliest is not None and latest is not None:
        elapsed = (latest - earliest).total_seconds() / 3600.0
    else:
        elapsed = 0.0

    soak_complete = (
        saw_post_cutover
        and contiguous_pass
        and pass_count > 0
        and elapsed >= window_hours
    )
    if soak_complete:
        reason = (
            f"PASS — {pass_count} clean checks span {elapsed:.1f}h "
            f"≥ {window_hours}h"
        )
    elif not saw_post_cutover:
        reason = "INCOMPLETE — no post-cutover soak results yet"
    elif not contiguous_pass:
        reason = (
            f"INCOMPLETE — {fail_count} FAIL + {rollback_count} ROLLBACK "
            f"observed; window not clean"
        )
    else:
        reason = (
            f"INCOMPLETE — only {elapsed:.1f}h of clean PASS, need ≥ "
            f"{window_hours}h"
        )

    return RollupResult(
        cutover_at=cutover_at.isoformat(),
        audit_dir=str(audit_dir),
        window_hours=window_hours,
        file_count=len(summaries),
        pass_count=pass_count,
        fail_count=fail_count,
        rollback_count=rollback_count,
        earliest=earliest.isoformat() if earliest else None,
        latest=latest.isoformat() if latest else None,
        elapsed_hours=round(elapsed, 2),
        contiguous_pass=contiguous_pass,
        soak_complete=soak_complete,
        soak_complete_reason=reason,
        files=summaries,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 3 soak-window rollup")
    ap.add_argument(
        "--cutover-at",
        default=DEFAULT_CUTOVER_AT.isoformat(),
        help="ISO-8601 cutover timestamp (default: 2026-04-29T07:09:00+00:00)",
    )
    ap.add_argument(
        "--audit-dir",
        default=str(DEFAULT_AUDIT_DIR),
        help="dir holding soak_*.json (default: logs/router_audit)",
    )
    ap.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help="soak window length in hours (default: 72)",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    args = ap.parse_args(argv)

    cutover_at = _parse_iso(args.cutover_at)
    audit_dir = Path(args.audit_dir)
    if not audit_dir.is_dir():
        print(f"ERROR: audit dir not found: {audit_dir}", file=sys.stderr)
        return 5

    res = rollup(cutover_at, audit_dir, args.window_hours)
    if args.json:
        print(json.dumps(asdict(res), indent=2, sort_keys=True))
    else:
        print(
            f"soak rollup since {res.cutover_at}: {res.file_count} files "
            f"(PASS={res.pass_count}, FAIL={res.fail_count}, "
            f"ROLLBACK={res.rollback_count}); elapsed={res.elapsed_hours:.1f}h; "
            f"complete={res.soak_complete} — {res.soak_complete_reason}"
        )
    return 0 if res.soak_complete else 1


if __name__ == "__main__":
    sys.exit(main())
