#!/usr/bin/env bash
# Phase 3 hourly soak check + kill-switch dispatch + completion notifier.
#
# Runs the read-only soak monitor over the last hour of routing_events,
# feeds the result file into the kill-switch which sends a Telegram
# alert on FAIL/ROLLBACK and (when armed via ROUTER_KILL_SWITCH_CONFIRM
# + --auto-rollback) executes the rollback plan, then runs the one-shot
# soak-completion notifier which fires a single "PHASE 3 SOAK COMPLETE"
# Telegram message the first hour the rolling 72h window comes up clean.
#
# Exit codes mirror the kill-switch (the soak status is the authoritative
# signal; the completion notifier is best-effort and does not influence rc):
#   0 = PASS, 1 = FAIL (alert sent), 4 = ROLLBACK (plan + alert), 5 = setup error
#
# Forwards extra args to the kill-switch:
#   scripts/router-soak-hourly.sh --auto-rollback
#   scripts/router-soak-hourly.sh --no-notify
#
# Designed for the operator's scheduled-tasks runtime to fire hourly.
# Bypass: set ROUTER_SOAK_SKIP=1 to no-op (returns 0).
# Skip notifier only: set ROUTER_SOAK_NOTIFY_SKIP=1 (e.g. during drills).

set -uo pipefail

if [[ "${ROUTER_SOAK_SKIP:-0}" == "1" ]]; then
  echo "router-soak-hourly: ROUTER_SOAK_SKIP=1 set, skipping" >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
RESULT_DIR="$REPO_ROOT/logs/router_audit"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "router-soak-hourly: python venv missing at $PYTHON_BIN" >&2
  exit 5
fi

# Run the soak check. Don't `set -e` — we WANT to continue even when the
# monitor exits 1 (FAIL) or 4 (ROLLBACK), because that's exactly when the
# kill-switch needs to fire. We only abort if no result file is produced.
"$PYTHON_BIN" -m agent.router_soak_monitor check >/dev/null
SOAK_RC=$?

LATEST="$(ls -t "$RESULT_DIR"/soak_*.json 2>/dev/null | head -1 || true)"
if [[ -z "$LATEST" ]]; then
  echo "router-soak-hourly: no soak result file written (monitor rc=$SOAK_RC)" >&2
  exit 5
fi

"$PYTHON_BIN" -m agent.router_kill_switch --soak-result "$LATEST" "$@"
KS_RC=$?

# Best-effort one-shot completion notifier. Idempotent: it only sends a
# Telegram message the first time the rolling 72h soak window clears. Any
# non-zero rc here (incomplete window, send failure) is informational and
# must NOT clobber the kill-switch's exit code, which is the authoritative
# soak verdict the operator's scheduled task acts on.
if [[ "${ROUTER_SOAK_NOTIFY_SKIP:-0}" != "1" ]]; then
  "$PYTHON_BIN" -m agent.router_soak_completion_notifier >&2 || true
fi

exit "$KS_RC"
