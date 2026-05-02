"""Tests for agent.router_soak_rollup."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.router_soak_rollup import (
    DEFAULT_WINDOW_HOURS,
    RollupResult,
    main,
    rollup,
)

CUTOVER = datetime(2026, 4, 29, 7, 9, 0, tzinfo=timezone.utc)


def _write(audit_dir: Path, name: str, generated_at: datetime, status: str) -> Path:
    p = audit_dir / name
    p.write_text(json.dumps({
        "generated_at": generated_at.isoformat(),
        "overall_status": status,
        "checks": [],
    }))
    return p


def test_empty_dir_returns_incomplete(tmp_path: Path) -> None:
    res = rollup(CUTOVER, tmp_path)
    assert isinstance(res, RollupResult)
    assert res.file_count == 0
    assert res.soak_complete is False
    assert "no post-cutover soak results" in res.soak_complete_reason


def test_only_pre_cutover_files_ignored(tmp_path: Path) -> None:
    pre = CUTOVER - timedelta(hours=2)
    _write(tmp_path, "soak_pre.json", pre, "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.file_count == 0
    assert res.soak_complete is False
    assert res.pass_count == 0
    assert "no post-cutover" in res.soak_complete_reason


def test_baseline_file_skipped(tmp_path: Path) -> None:
    # baseline file matches glob but must be skipped by the discoverer.
    p = tmp_path / "soak_baseline.json"
    p.write_text(json.dumps({"generated_at": CUTOVER.isoformat(), "overall_status": "PASS"}))
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(minutes=10), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.file_count == 1


def test_short_window_one_pass_incomplete(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.pass_count == 1
    assert res.contiguous_pass is True
    assert res.soak_complete is False
    assert "only" in res.soak_complete_reason


def test_full_window_clean_pass_completes(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(tmp_path, "soak_073.json", CUTOVER + timedelta(hours=73), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.pass_count == 2
    assert res.elapsed_hours == 72.0
    assert res.contiguous_pass is True
    assert res.soak_complete is True
    assert res.soak_complete_reason.startswith("PASS")


def test_fail_in_window_breaks_contiguity(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(tmp_path, "soak_010.json", CUTOVER + timedelta(hours=10), "FAIL")
    _write(tmp_path, "soak_073.json", CUTOVER + timedelta(hours=73), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.fail_count == 1
    assert res.contiguous_pass is False
    assert res.soak_complete is False
    assert "FAIL" in res.soak_complete_reason


def test_rollback_in_window_breaks_contiguity(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(tmp_path, "soak_050.json", CUTOVER + timedelta(hours=50), "ROLLBACK")
    res = rollup(CUTOVER, tmp_path)
    assert res.rollback_count == 1
    assert res.contiguous_pass is False
    assert res.soak_complete is False


def test_unknown_status_breaks_contiguity(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "WEIRD")
    res = rollup(CUTOVER, tmp_path)
    assert res.contiguous_pass is False
    assert res.soak_complete is False


def test_window_hours_param_respected(tmp_path: Path) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(tmp_path, "soak_005.json", CUTOVER + timedelta(hours=5), "PASS")
    res = rollup(CUTOVER, tmp_path, window_hours=4)
    assert res.elapsed_hours == 4.0
    assert res.soak_complete is True
    assert res.window_hours == 4


def test_malformed_json_skipped(tmp_path: Path) -> None:
    (tmp_path / "soak_bad.json").write_text("{not json")
    _write(tmp_path, "soak_good.json", CUTOVER + timedelta(hours=1), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.file_count == 1
    assert res.pass_count == 1


def test_missing_generated_at_skipped(tmp_path: Path) -> None:
    (tmp_path / "soak_no_ts.json").write_text(json.dumps({"overall_status": "PASS"}))
    _write(tmp_path, "soak_good.json", CUTOVER + timedelta(hours=1), "PASS")
    res = rollup(CUTOVER, tmp_path)
    assert res.file_count == 1


def test_cli_rc_complete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(tmp_path, "soak_073.json", CUTOVER + timedelta(hours=73), "PASS")
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "complete=True" in out


def test_cli_rc_incomplete(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(tmp_path),
    ])
    assert rc == 1


def test_cli_rc_missing_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(tmp_path / "does-not-exist"),
    ])
    assert rc == 5
    err = capsys.readouterr().err
    assert "audit dir not found" in err


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "soak_001.json", CUTOVER + timedelta(hours=1), "PASS")
    main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(tmp_path),
        "--json",
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["pass_count"] == 1
    assert parsed["soak_complete"] is False


def test_default_cutover_filters_real_artifacts() -> None:
    """Smoke: against the real logs/router_audit dir if present, the default
    cutover timestamp should not crash and should return a structured result."""
    real = Path("logs/router_audit")
    if not real.is_dir():
        pytest.skip("no real audit dir checked into worktree")
    res = rollup(CUTOVER, real)
    assert isinstance(res, RollupResult)
    assert res.window_hours == DEFAULT_WINDOW_HOURS
