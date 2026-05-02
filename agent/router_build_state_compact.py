"""Compact docs/router_build_state.json so it stays under Read's token limit.

The build agent (peppers-semantic-router-build-agent) accumulates
`_prev_run_summary_iter_*` entries and a long `tasks_completed_this_phase`
list. Once the file crosses ~25k tokens the agent's `Read` tool refuses to
load it and the run can't orient.

This script archives anything beyond a small inline window into
`docs/router_build_state_history.json`. Run it from the build agent at the
end of every iteration (cheap; idempotent when nothing needs moving).

Thresholds:
- Inline keep: most-recent 5 `_prev_run_summary_iter_*` entries and the
  most-recent 5 `tasks_completed_this_phase` entries.
- Always archives older entries even if the file is small — keeps the
  inline window predictable run-over-run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "docs" / "router_build_state.json"
HISTORY_PATH = REPO_ROOT / "docs" / "router_build_state_history.json"

INLINE_PREV_SUMMARIES = 5
INLINE_TASKS = 5

ITER_KEY_RE = re.compile(r"^_?prev_run_summary_iter_(\d+)$")

# Pure-historical keys the orientation loop never consults. Matched keys are
# moved verbatim into history.archived_legacy_keys so the state file stays
# under Read's 25k-token limit.
LEGACY_KEY_RE = re.compile(
    r"^("
    r"_legacy_.*"
    r"|_archived_.*"
    r"|last_run_summary_iter_.*"
    r"|_unblock_resolution_summary"
    r"|_telegram_unblock_resolution_summary"
    r"|_unblock_resolved_at"
    r"|_telegram_unblock_resolved_iter\d+_at"
    r")$"
)


def _iter_index(key: str) -> int:
    m = ITER_KEY_RE.match(key)
    return int(m.group(1)) if m else -1


def compact(
    state_path: Path = STATE_PATH,
    history_path: Path = HISTORY_PATH,
    inline_prev: int = INLINE_PREV_SUMMARIES,
    inline_tasks: int = INLINE_TASKS,
) -> dict:
    state = json.loads(state_path.read_text())

    prev_keys = sorted(
        (k for k in state if ITER_KEY_RE.match(k)),
        key=_iter_index,
    )
    keep_keys = set(prev_keys[-inline_prev:]) if inline_prev > 0 else set()
    archive_keys = [k for k in prev_keys if k not in keep_keys]
    archived_summaries = {k: state.pop(k) for k in archive_keys}

    all_tasks = list(state.get("tasks_completed_this_phase", []))
    keep_tasks = all_tasks[:inline_tasks]
    archive_tasks = all_tasks[inline_tasks:]
    state["tasks_completed_this_phase"] = keep_tasks

    legacy_keys = [k for k in list(state) if LEGACY_KEY_RE.match(k)]
    archived_legacy = {k: state.pop(k) for k in legacy_keys}

    if archived_summaries or archive_tasks or archived_legacy:
        if history_path.exists():
            history = json.loads(history_path.read_text())
        else:
            history = {
                "_note": (
                    "Historical iter summaries and older tasks archived from "
                    "router_build_state.json. Read with offset/limit or jq."
                ),
                "archived_iter_summaries": {},
                "archived_tasks": [],
            }
        history.setdefault("archived_iter_summaries", {}).update(archived_summaries)
        history.setdefault("archived_tasks", []).extend(archive_tasks)
        history.setdefault("archived_legacy_keys", {}).update(archived_legacy)
        history["last_archive_at"] = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        history_path.write_text(json.dumps(history, indent=2))

    try:
        rel = history_path.relative_to(REPO_ROOT)
    except ValueError:
        rel = history_path
    state["_history_file"] = (
        f"{rel} (older _prev_run_summary_iter_* and "
        "tasks_completed_this_phase entries live here; auto-managed by "
        "agent/router_build_state_compact.py)"
    )

    state_path.write_text(json.dumps(state, indent=2))

    return {
        "summaries_archived": len(archived_summaries),
        "tasks_archived": len(archive_tasks),
        "legacy_keys_archived": len(archived_legacy),
        "summaries_inline": len(keep_keys),
        "tasks_inline": len(keep_tasks),
        "state_bytes": state_path.stat().st_size,
        "history_bytes": history_path.stat().st_size if history_path.exists() else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inline-prev", type=int, default=INLINE_PREV_SUMMARIES)
    parser.add_argument("--inline-tasks", type=int, default=INLINE_TASKS)
    args = parser.parse_args()
    result = compact(
        inline_prev=args.inline_prev,
        inline_tasks=args.inline_tasks,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
