"""Unit tests for agent.router_kill_switch.

Covers: rollback plan generation, on-disk artifacts, the confirm-token
guard on execute_rollback, the FAIL/ROLLBACK/PASS dispatch in
handle_soak_result, and the Telegram alert formatter.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import router_kill_switch as ks
from agent.router_soak_monitor import SoakCheck, SoakResult


def _make_result(status: str, *, eval_rate: float = 0.9) -> SoakResult:
    return SoakResult(
        overall_status=status,
        checks=[
            SoakCheck(
                name="eval_pass_rate",
                status="PASS" if eval_rate >= 0.85 else "FAIL",
                value=eval_rate,
                threshold=0.85,
                detail="",
            ),
            SoakCheck(
                name="primary_router_exception_rate",
                status="PASS",
                value=0.0,
                threshold=0.0,
                detail="",
            ),
        ],
        eval_pass_rate=eval_rate,
        generated_at="2026-04-29T00:00:00+00:00",
    )


# ─── plan_rollback ───────────────────────────────────────────────────────


def test_plan_rollback_includes_git_and_docker(tmp_path: Path):
    result = _make_result("ROLLBACK", eval_rate=0.5)
    plan = ks.plan_rollback(result, repo_root=tmp_path)
    cmds = plan.commands
    # Two commands: git checkout + docker compose rebuild.
    assert len(cmds) == 2
    assert cmds[0][0] == "git" and "checkout" in cmds[0]
    assert ks.PRE_CUTOVER_SHA in cmds[0]
    assert cmds[1][:3] == ["docker", "compose", "up"]
    assert "--build" in cmds[1]


def test_plan_rollback_records_snapshot_when_present(tmp_path: Path):
    snap = tmp_path / "backups" / "router" / "phase_3_pre_cutover_X"
    snap.mkdir(parents=True)
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    assert plan.snapshot_path is not None
    assert plan.snapshot_path.endswith("phase_3_pre_cutover_X")


def test_write_plan_creates_json_and_executable_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    plan_path = ks.write_plan(plan)
    assert plan_path.exists()
    payload = json.loads(plan_path.read_text())
    assert payload["pre_cutover_sha"] == ks.PRE_CUTOVER_SHA
    sh = plan_path.parent / "rollback.sh"
    assert sh.exists()
    assert sh.stat().st_mode & 0o111  # executable
    body = sh.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "docker compose up -d --build" in body


# ─── execute_rollback guard ──────────────────────────────────────────────


def test_execute_rollback_refuses_without_confirm_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    monkeypatch.delenv(ks.CONFIRM_TOKEN_ENV, raising=False)
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    ks.write_plan(plan)
    with pytest.raises(PermissionError):
        ks.execute_rollback(plan)


def test_execute_rollback_runs_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    ks.write_plan(plan)
    seen: list[list[str]] = []

    def fake_run(cmd, **_):
        seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    out = ks.execute_rollback(
        plan,
        confirm_token=ks.EXPECTED_CONFIRM_TOKEN,
        runner=fake_run,
    )
    assert out.executed is True
    assert len(seen) == 2
    assert all(e["returncode"] == 0 for e in out.execution_log)


def test_execute_rollback_halts_on_first_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    ks.write_plan(plan)

    def fake_run(cmd, **_):
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    with pytest.raises(RuntimeError, match="rollback halted"):
        ks.execute_rollback(
            plan,
            confirm_token=ks.EXPECTED_CONFIRM_TOKEN,
            runner=fake_run,
        )
    # Plan json should still have been written with execution_log entry
    payload = json.loads((Path(plan.plan_dir) / "plan.json").read_text())
    assert payload["executed"] is True
    assert payload["execution_log"][0]["returncode"] == 2


# ─── format_alert ────────────────────────────────────────────────────────


def test_format_alert_pass_rollback_fail_icons():
    for st, want_icon in [("PASS", "✅"), ("FAIL", "⚠️"), ("ROLLBACK", "🛑")]:
        msg = ks.format_alert(_make_result(st))
        assert want_icon in msg
        assert st in msg


def test_format_alert_includes_plan_path(tmp_path: Path):
    plan = ks.plan_rollback(_make_result("ROLLBACK"), repo_root=tmp_path)
    msg = ks.format_alert(_make_result("ROLLBACK"), plan=plan)
    assert plan.plan_dir in msg
    assert "NOT executed" in msg
    plan.executed = True
    assert "EXECUTED" in ks.format_alert(_make_result("ROLLBACK"), plan=plan)


def test_format_alert_does_not_leak_query_text():
    msg = ks.format_alert(_make_result("ROLLBACK"))
    # Sanity: no markers that could carry PII (this module never gets one,
    # but the test pins the contract).
    forbidden = ["@gmail", "@pm.me", "imessage", "whatsapp", "query_text"]
    assert not any(f in msg for f in forbidden)


# ─── handle_soak_result dispatch ─────────────────────────────────────────


def test_handle_pass_does_nothing(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []

    async def fake_send(m):
        sent.append(m)
        return True

    monkeypatch.setattr(ks, "send_alert", fake_send)
    out = asyncio.run(ks.handle_soak_result(_make_result("PASS")))
    assert out["alert_sent"] is False
    assert sent == []


def test_handle_fail_alerts_no_plan(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []

    async def fake_send(m):
        sent.append(m)
        return True

    monkeypatch.setattr(ks, "send_alert", fake_send)
    out = asyncio.run(ks.handle_soak_result(_make_result("FAIL")))
    assert out["alert_sent"] is True
    assert out["plan"] is None
    assert "FAIL" in sent[0]


def test_handle_rollback_writes_plan_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    sent: list[str] = []

    async def fake_send(m):
        sent.append(m)
        return True

    monkeypatch.setattr(ks, "send_alert", fake_send)
    out = asyncio.run(
        ks.handle_soak_result(_make_result("ROLLBACK", eval_rate=0.5))
    )
    assert out["plan"] is not None
    assert out["executed"] is False  # auto_rollback default False
    assert out["alert_sent"] is True
    assert "ROLLBACK" in sent[0]
    # Plan dir must have been materialised to disk.
    plan_dir = Path(out["plan"]["plan_dir"])
    assert (plan_dir / "plan.json").exists()
    assert (plan_dir / "rollback.sh").exists()


# ─── drill_rollback ──────────────────────────────────────────────────────


def _drill_runner_factory(*, sha_rc: int = 0, syntax_rc: int = 0):
    """Build a fake subprocess.run for drill_rollback that only handles the
    two read-only commands the drill issues (bash -n, git cat-file -e)."""

    def fake_run(cmd, **_):
        if cmd[:2] == ["bash", "-n"]:
            return SimpleNamespace(returncode=syntax_rc, stdout="", stderr="")
        if "cat-file" in cmd:
            return SimpleNamespace(returncode=sha_rc, stdout="", stderr="")
        raise AssertionError(
            f"drill must not invoke arbitrary subprocess: {cmd}"
        )

    return fake_run


def test_drill_rollback_writes_audit_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "audit")
    snap = tmp_path / "backups" / "router" / "phase_3_pre_cutover_X"
    snap.mkdir(parents=True)
    report = ks.drill_rollback(
        repo_root=tmp_path, runner=_drill_runner_factory()
    )
    assert report["overall_status"] == "PASS"
    assert report["executed"] is False
    assert report["telegram_sent"] is False
    drill_dir = Path(report["drill_dir"])
    assert (drill_dir / "drill_report.json").exists()
    assert (drill_dir / "plan.json").exists()
    assert (drill_dir / "rollback.sh").exists()
    step_names = [s["step"] for s in report["steps"]]
    assert step_names == [
        "plan_compose",
        "artifacts_written",
        "bash_syntax_check",
        "pre_cutover_sha_exists",
        "snapshot_dir_exists",
    ]
    assert all(s["status"] == "PASS" for s in report["steps"])


def test_drill_rollback_fails_when_sha_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "audit")
    (tmp_path / "backups" / "router" / "phase_3_pre_cutover_X").mkdir(
        parents=True
    )
    report = ks.drill_rollback(
        repo_root=tmp_path, runner=_drill_runner_factory(sha_rc=1)
    )
    assert report["overall_status"] == "FAIL"
    sha_step = next(
        s for s in report["steps"] if s["step"] == "pre_cutover_sha_exists"
    )
    assert sha_step["status"] == "FAIL"


def test_drill_rollback_fails_when_snapshot_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "audit")
    report = ks.drill_rollback(
        repo_root=tmp_path, runner=_drill_runner_factory()
    )
    assert report["overall_status"] == "FAIL"
    snap_step = next(
        s for s in report["steps"] if s["step"] == "snapshot_dir_exists"
    )
    assert snap_step["status"] == "FAIL"


def test_drill_rollback_does_not_execute_or_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "audit")
    (tmp_path / "backups" / "router" / "phase_3_pre_cutover_X").mkdir(
        parents=True
    )

    sent: list[str] = []

    async def fake_send(m):
        sent.append(m)
        return True

    monkeypatch.setattr(ks, "send_alert", fake_send)
    forbidden_seen: list[list[str]] = []

    def strict_runner(cmd, **kw):
        if cmd[:2] == ["bash", "-n"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "cat-file" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        forbidden_seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    report = ks.drill_rollback(repo_root=tmp_path, runner=strict_runner)
    assert report["overall_status"] == "PASS"
    assert sent == []
    assert forbidden_seen == [], (
        f"drill must not run docker/git checkout: {forbidden_seen}"
    )


def test_handle_rollback_unarmed_auto_rollback_does_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(ks, "ROLLBACK_DIR", tmp_path / "ra")
    monkeypatch.delenv(ks.CONFIRM_TOKEN_ENV, raising=False)

    async def fake_send(_m):
        return True

    monkeypatch.setattr(ks, "send_alert", fake_send)
    out = asyncio.run(
        ks.handle_soak_result(
            _make_result("ROLLBACK", eval_rate=0.5),
            auto_rollback=True,
            # no confirm_token, no env → executor refuses, plan still written
        )
    )
    assert out["plan"] is not None
    assert out["executed"] is False
