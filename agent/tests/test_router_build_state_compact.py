"""Unit tests for agent/router_build_state_compact.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import router_build_state_compact as compactor


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "state.json", tmp_path / "history.json"


def test_compact_archives_old_iters_and_keeps_inline_window(paths):
    state_p, hist_p = paths
    summaries = {f"_prev_run_summary_iter_{i}": f"iter {i} summary" for i in range(1, 11)}
    tasks = [{"task": f"t{i}", "completed_at": f"2026-01-{i:02d}"} for i in range(1, 9)]
    _write(state_p, {"phase": 2, "iteration": 11, "tasks_completed_this_phase": tasks, **summaries})

    result = compactor.compact(state_p, hist_p, inline_prev=3, inline_tasks=2)

    state = json.loads(state_p.read_text())
    inline_iters = [k for k in state if k.startswith("_prev_run_summary_iter_")]
    assert sorted(inline_iters) == [
        "_prev_run_summary_iter_10",
        "_prev_run_summary_iter_8",
        "_prev_run_summary_iter_9",
    ]
    assert len(state["tasks_completed_this_phase"]) == 2
    assert state["tasks_completed_this_phase"][0]["task"] == "t1"
    assert "_history_file" in state

    history = json.loads(hist_p.read_text())
    assert len(history["archived_iter_summaries"]) == 7
    assert "_prev_run_summary_iter_1" in history["archived_iter_summaries"]
    assert "_prev_run_summary_iter_10" not in history["archived_iter_summaries"]
    assert len(history["archived_tasks"]) == 6

    assert result["summaries_archived"] == 7
    assert result["tasks_archived"] == 6


def test_compact_is_idempotent(paths):
    state_p, hist_p = paths
    _write(
        state_p,
        {
            "phase": 2,
            "iteration": 3,
            "tasks_completed_this_phase": [{"task": "a"}],
            "_prev_run_summary_iter_1": "x",
            "_prev_run_summary_iter_2": "y",
        },
    )
    first = compactor.compact(state_p, hist_p, inline_prev=5, inline_tasks=5)
    second = compactor.compact(state_p, hist_p, inline_prev=5, inline_tasks=5)
    assert first["summaries_archived"] == 0
    assert second["summaries_archived"] == 0


def test_compact_handles_mixed_underscore_prefix(paths):
    """Both `_prev_run_summary_iter_*` and `prev_run_summary_iter_*` historically appear."""
    state_p, hist_p = paths
    _write(
        state_p,
        {
            "tasks_completed_this_phase": [],
            "prev_run_summary_iter_9": "old style",
            "_prev_run_summary_iter_10": "new style",
            "_prev_run_summary_iter_11": "newer",
        },
    )
    compactor.compact(state_p, hist_p, inline_prev=2, inline_tasks=5)
    state = json.loads(state_p.read_text())
    assert "prev_run_summary_iter_9" not in state
    assert "_prev_run_summary_iter_10" in state
    assert "_prev_run_summary_iter_11" in state
    history = json.loads(hist_p.read_text())
    assert "prev_run_summary_iter_9" in history["archived_iter_summaries"]


def test_compact_archives_legacy_keys(paths):
    """Pure-historical keys (`_legacy_*`, `_archived_*`, `last_run_summary_iter_*`,
    `_unblock_resolution_summary`, etc.) move to history.archived_legacy_keys
    so the state file stays under Read's 25k-token limit."""
    state_p, hist_p = paths
    _write(
        state_p,
        {
            "phase": 3,
            "iteration": 200,
            "tasks_completed_this_phase": [],
            "_legacy_iter121_run_summary": "old phase 2 detail",
            "_legacy_current_task_iter_119": "long string",
            "_archived_iter_127_full_summary": "more historical text",
            "last_run_summary_iter_98": "Gate 1 augmentation summary",
            "last_run_summary_iter_125_full": "soak monitor summary",
            "_unblock_resolution_summary": "resolved x",
            "_telegram_unblock_resolution_summary": "resolved y",
            "_unblock_resolved_at": "2026-04-28T18:15:00Z",
            "_telegram_unblock_resolved_iter120_at": "2026-04-29T05:51:18Z",
            "blocked": False,
            "phase_status": "in_progress",  # must NOT be archived
            "current_task": "keep me",  # must NOT be archived
        },
    )
    result = compactor.compact(state_p, hist_p)
    state = json.loads(state_p.read_text())
    history = json.loads(hist_p.read_text())

    archived = history["archived_legacy_keys"]
    assert "_legacy_iter121_run_summary" in archived
    assert "_legacy_current_task_iter_119" in archived
    assert "_archived_iter_127_full_summary" in archived
    assert "last_run_summary_iter_98" in archived
    assert "last_run_summary_iter_125_full" in archived
    assert "_unblock_resolution_summary" in archived
    assert "_telegram_unblock_resolution_summary" in archived
    assert "_unblock_resolved_at" in archived
    assert "_telegram_unblock_resolved_iter120_at" in archived

    # Live keys preserved.
    assert state["phase_status"] == "in_progress"
    assert state["current_task"] == "keep me"
    assert state["blocked"] is False

    # Archived keys removed from state.
    for k in archived:
        assert k not in state

    assert result["legacy_keys_archived"] == 9


def test_compact_legacy_archive_is_idempotent(paths):
    state_p, hist_p = paths
    _write(
        state_p,
        {
            "tasks_completed_this_phase": [],
            "_legacy_only": "x",
            "blocked": False,
        },
    )
    first = compactor.compact(state_p, hist_p)
    second = compactor.compact(state_p, hist_p)
    assert first["legacy_keys_archived"] == 1
    assert second["legacy_keys_archived"] == 0


def test_compact_appends_to_existing_history(paths):
    state_p, hist_p = paths
    _write(
        hist_p,
        {
            "archived_iter_summaries": {"_prev_run_summary_iter_1": "old"},
            "archived_tasks": [{"task": "old-task"}],
        },
    )
    _write(
        state_p,
        {
            "tasks_completed_this_phase": [{"task": "new"}],
            "_prev_run_summary_iter_2": "fresh",
        },
    )
    compactor.compact(state_p, hist_p, inline_prev=0, inline_tasks=0)
    history = json.loads(hist_p.read_text())
    assert set(history["archived_iter_summaries"].keys()) == {
        "_prev_run_summary_iter_1",
        "_prev_run_summary_iter_2",
    }
    assert {t["task"] for t in history["archived_tasks"]} == {"old-task", "new"}
