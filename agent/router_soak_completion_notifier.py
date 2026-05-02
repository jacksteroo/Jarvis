"""Phase 3 soak-completion Telegram notifier.

Phase 3's exit criterion is "3-day soak passes all checks, kill-switch
verified, documentation merged, **Telegram notification sent**". Every
deliverable is in place except the final notification: when the rolling
soak window first clears the 72h-clean-PASS bar, the operator gets a
one-shot "PHASE 3 SOAK COMPLETE" Telegram. This module is that one-shot.

Idempotent. Safe to fire on every hourly soak tick; will only send the
notification once per soak window. State lives in a small flag file
(`logs/router_audit/phase3_soak_complete.json`) so a re-pinned cutover
(`--cutover-at` change after a real FAIL incident) can be re-armed by
deleting that flag file.

Privacy: reads only on-disk JSON soak reports + count/timestamp summary
the rollup already emits. The Telegram body contains counts, ISO
timestamps, and the rollup's own ``soak_complete_reason`` string. No
query text, no PII, no DB access.

CLI:
  .venv/bin/python -m agent.router_soak_completion_notifier
      [--cutover-at ISO] [--audit-dir DIR] [--window-hours N]
      [--flag-file PATH] [--dry-run]

Exit codes:
  0  notification sent OR already sent OR dry-run on a complete window
  1  soak incomplete (no notification needed yet)
  2  send failed (Telegram unreachable / no token)
  5  audit dir missing / unreadable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.router_soak_rollup import (
    DEFAULT_AUDIT_DIR,
    DEFAULT_CUTOVER_AT,
    DEFAULT_WINDOW_HOURS,
    RollupResult,
    rollup,
)

DEFAULT_FLAG_FILE = DEFAULT_AUDIT_DIR / "phase3_soak_complete.json"


@dataclass
class NotifyResult:
    soak_complete: bool
    already_notified: bool
    sent: bool
    flag_file: str
    rollup_summary: str


def _format_message(res: RollupResult) -> str:
    """Plain-text Telegram body — no Markdown so parse_mode=None is safe."""
    return (
        "✅ PHASE 3 SOAK COMPLETE — semantic router migration\n"
        f"clean PASS span: {res.elapsed_hours:.1f}h "
        f"(≥ {res.window_hours}h required)\n"
        f"reports: {res.file_count} (PASS={res.pass_count}, "
        f"FAIL={res.fail_count}, ROLLBACK={res.rollback_count})\n"
        f"window: {res.earliest} → {res.latest}\n"
        f"cutover ref: {res.cutover_at}\n"
        "next: confirm exit checklist, advance to Phase 4 (feedback loop)."
    )


def _read_flag(flag_file: Path) -> dict | None:
    if not flag_file.is_file():
        return None
    try:
        return json.loads(flag_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_flag(flag_file: Path, res: RollupResult, sent: bool) -> None:
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "sent": sent,
        "rollup": asdict(res),
    }
    flag_file.write_text(json.dumps(payload, indent=2, sort_keys=True))


async def _send(message: str) -> bool:
    # Reuse the kill-switch's one-shot push helper so soak-completion
    # alerts ride the exact same Telegram path as rollback alerts.
    from agent.router_kill_switch import send_alert

    return await send_alert(message)


def notify(
    cutover_at: datetime = DEFAULT_CUTOVER_AT,
    audit_dir: Path = DEFAULT_AUDIT_DIR,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    flag_file: Path = DEFAULT_FLAG_FILE,
    dry_run: bool = False,
) -> NotifyResult:
    res = rollup(cutover_at, audit_dir, window_hours)
    summary = res.soak_complete_reason

    if not res.soak_complete:
        return NotifyResult(
            soak_complete=False,
            already_notified=False,
            sent=False,
            flag_file=str(flag_file),
            rollup_summary=summary,
        )

    existing = _read_flag(flag_file)
    if existing is not None and existing.get("sent") is True:
        return NotifyResult(
            soak_complete=True,
            already_notified=True,
            sent=False,
            flag_file=str(flag_file),
            rollup_summary=summary,
        )

    if dry_run:
        return NotifyResult(
            soak_complete=True,
            already_notified=False,
            sent=False,
            flag_file=str(flag_file),
            rollup_summary=summary,
        )

    sent = asyncio.run(_send(_format_message(res)))
    _write_flag(flag_file, res, sent)
    return NotifyResult(
        soak_complete=True,
        already_notified=False,
        sent=sent,
        flag_file=str(flag_file),
        rollup_summary=summary,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 3 soak-completion Telegram notifier (one-shot)"
    )
    ap.add_argument("--cutover-at", default=DEFAULT_CUTOVER_AT.isoformat())
    ap.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    ap.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    ap.add_argument("--flag-file", default=str(DEFAULT_FLAG_FILE))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report verdict but do not Telegram or write flag",
    )
    args = ap.parse_args(argv)

    # Local import to avoid a circular at module-load time.
    from agent.router_soak_rollup import _parse_iso

    audit_dir = Path(args.audit_dir)
    if not audit_dir.is_dir():
        print(f"ERROR: audit dir not found: {audit_dir}", file=sys.stderr)
        return 5

    res = notify(
        cutover_at=_parse_iso(args.cutover_at),
        audit_dir=audit_dir,
        window_hours=args.window_hours,
        flag_file=Path(args.flag_file),
        dry_run=args.dry_run,
    )

    if not res.soak_complete:
        print(f"soak INCOMPLETE — {res.rollup_summary}")
        return 1
    if res.already_notified:
        print(f"soak COMPLETE — already notified (flag: {res.flag_file})")
        return 0
    if args.dry_run:
        print(f"soak COMPLETE — dry-run (would notify); flag: {res.flag_file}")
        return 0
    if res.sent:
        print(f"soak COMPLETE — Telegram notification sent; flag: {res.flag_file}")
        return 0
    print(
        f"soak COMPLETE — Telegram send FAILED (flag still written; rerun once "
        f"Telegram reachable). flag: {res.flag_file}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
