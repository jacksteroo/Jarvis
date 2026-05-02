"""Tests for agent.router_soak_completion_notifier."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.router_soak_completion_notifier import (
    DEFAULT_FLAG_FILE,
    NotifyResult,
    main,
    notify,
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


def _seed_complete_window(audit_dir: Path, window_hours: int = 72) -> None:
    # Two reports spanning > window_hours, both PASS.
    _write(audit_dir, "soak_a.json", CUTOVER + timedelta(hours=1), "PASS")
    _write(
        audit_dir,
        "soak_b.json",
        CUTOVER + timedelta(hours=window_hours + 2),
        "PASS",
    )


def test_incomplete_window_returns_no_notification(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _write(audit, "soak_a.json", CUTOVER + timedelta(hours=1), "PASS")
    flag = tmp_path / "flag.json"

    res = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )

    assert isinstance(res, NotifyResult)
    assert res.soak_complete is False
    assert res.sent is False
    assert res.already_notified is False
    assert not flag.exists()


def test_dry_run_on_complete_window_does_not_send_or_write(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"

    res = notify(cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=True)

    assert res.soak_complete is True
    assert res.sent is False
    assert res.already_notified is False
    assert not flag.exists()


def test_complete_window_sends_and_writes_flag(monkeypatch, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"

    sent_messages: list[str] = []

    async def fake_send_alert(message: str) -> bool:
        sent_messages.append(message)
        return True

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    res = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )

    assert res.soak_complete is True
    assert res.sent is True
    assert res.already_notified is False
    assert flag.is_file()
    payload = json.loads(flag.read_text())
    assert payload["sent"] is True
    assert payload["rollup"]["soak_complete"] is True
    assert "PHASE 3 SOAK COMPLETE" in sent_messages[0]
    assert len(sent_messages) == 1


def test_already_notified_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"
    # Pre-seed a flag indicating prior successful send.
    flag.write_text(json.dumps({"sent": True, "notified_at": "x", "rollup": {}}))

    sent_messages: list[str] = []

    async def fake_send_alert(message: str) -> bool:
        sent_messages.append(message)
        return True

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    res = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )

    assert res.soak_complete is True
    assert res.already_notified is True
    assert res.sent is False
    assert sent_messages == []  # not re-sent


def test_send_failure_writes_flag_with_sent_false_for_retry(
    monkeypatch, tmp_path: Path
) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"

    async def fake_send_alert(message: str) -> bool:
        return False  # Telegram unreachable

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    res = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )

    assert res.soak_complete is True
    assert res.sent is False
    assert res.already_notified is False
    payload = json.loads(flag.read_text())
    assert payload["sent"] is False
    # Next run with send still failing should NOT mark already_notified.
    res2 = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )
    assert res2.already_notified is False


def test_corrupt_flag_is_treated_as_unsent(monkeypatch, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"
    flag.write_text("not-json{")

    async def fake_send_alert(message: str) -> bool:
        return True

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    res = notify(
        cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False
    )
    assert res.sent is True
    assert res.already_notified is False


def test_message_body_contains_no_pii_keywords(
    monkeypatch, tmp_path: Path
) -> None:
    """Privacy contract: the Telegram body is metric-only — no email,
    handle, phone, query text, or DB access."""
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"
    captured: list[str] = []

    async def fake_send_alert(message: str) -> bool:
        captured.append(message)
        return True

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    notify(cutover_at=CUTOVER, audit_dir=audit, flag_file=flag, dry_run=False)
    body = captured[0].lower()
    for needle in ("@gmail", "@pm.me", "@yahoo", "imessage", "imap", "password"):
        assert needle not in body


def test_cli_incomplete_returns_rc_1(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    flag = tmp_path / "flag.json"
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(audit),
        "--flag-file", str(flag),
    ])
    assert rc == 1


def test_cli_dry_run_complete_returns_rc_0(tmp_path: Path, capsys) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(audit),
        "--flag-file", str(flag),
        "--dry-run",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert not flag.exists()


def test_cli_missing_audit_dir_returns_rc_5(tmp_path: Path) -> None:
    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(tmp_path / "does-not-exist"),
        "--flag-file", str(tmp_path / "flag.json"),
    ])
    assert rc == 5


def test_cli_send_failure_returns_rc_2(monkeypatch, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"

    async def fake_send_alert(message: str) -> bool:
        return False

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(audit),
        "--flag-file", str(flag),
    ])
    assert rc == 2


def test_cli_already_notified_returns_rc_0(monkeypatch, tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    _seed_complete_window(audit)
    flag = tmp_path / "flag.json"
    flag.write_text(json.dumps({"sent": True, "notified_at": "x", "rollup": {}}))

    async def fake_send_alert(message: str) -> bool:
        raise AssertionError("must not be called")

    import agent.router_kill_switch as ks

    monkeypatch.setattr(ks, "send_alert", fake_send_alert)

    rc = main([
        "--cutover-at", CUTOVER.isoformat(),
        "--audit-dir", str(audit),
        "--flag-file", str(flag),
    ])
    assert rc == 0


def test_default_flag_file_lives_under_audit_dir() -> None:
    # Sanity: shipping default flag file path matches the audit-dir convention.
    assert DEFAULT_FLAG_FILE.name == "phase3_soak_complete.json"
    assert DEFAULT_FLAG_FILE.parent.name == "router_audit"
