"""Smoke tests for scripts/router-soak-hourly.sh.

The wrapper is a bash script that chains:
  python -m agent.router_soak_monitor check
  python -m agent.router_kill_switch --soak-result <latest>

These tests verify it (a) exists and is executable, (b) carries the
contractual invariants the kill-switch dispatch needs (the two module
names, the result-dir glob, the bypass envvar), (c) skips on bypass,
(d) errors out cleanly when no result file is produced, and (e) runs
end-to-end against fake `python` shims that simulate the soak monitor +
kill-switch and that forward CLI args.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "router-soak-hourly.sh"


def test_hook_exists_and_is_executable() -> None:
    assert HOOK.is_file(), f"missing wrapper at {HOOK}"
    assert os.access(HOOK, os.X_OK), f"wrapper not executable: {HOOK}"


def test_hook_carries_required_invariants() -> None:
    text = HOOK.read_text()
    # All three modules are wired in.
    assert "agent.router_soak_monitor" in text
    assert "agent.router_kill_switch" in text
    assert "agent.router_soak_completion_notifier" in text
    # Bypass envvars (documented in docs/SEMANTIC_ROUTER.md).
    assert "ROUTER_SOAK_SKIP" in text
    assert "ROUTER_SOAK_NOTIFY_SKIP" in text
    # Result dir convention matches router_soak_monitor's DEFAULT_RESULT_DIR.
    assert "logs/router_audit" in text
    assert "soak_*.json" in text
    # Forward extra CLI args to the kill-switch (e.g. --auto-rollback).
    assert '"$@"' in text


def _write_fake_python(
    venv: Path,
    *,
    soak_rc: int = 0,
    write_result: bool = True,
    capture_path: Path | None = None,
    kill_switch_rc: int = 0,
    notifier_rc: int = 1,
    notifier_capture_path: Path | None = None,
) -> Path:
    """Create a fake `.venv/bin/python` that simulates all three modules.

    - For `-m agent.router_soak_monitor check`: optionally writes a
      timestamped JSON to logs/router_audit/, exits with ``soak_rc``.
    - For `-m agent.router_kill_switch ...`: appends its argv to
      ``capture_path`` so the test can assert what got forwarded, exits
      ``kill_switch_rc``.
    - For `-m agent.router_soak_completion_notifier ...`: appends its
      argv to ``notifier_capture_path`` (when supplied), exits
      ``notifier_rc`` (default 1 = soak incomplete; matches reality).
    """
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "python"
    capture = str(capture_path) if capture_path else "/dev/null"
    notify_capture = (
        str(notifier_capture_path) if notifier_capture_path else "/dev/null"
    )

    # Write the soak-monitor body to a separate .py file so we don't have
    # to embed an f-string inside a bash heredoc.
    helper = bin_dir / "_fake_soak_writer.py"
    if write_result:
        helper.write_text(
            "import time, pathlib, json, os\n"
            "p = pathlib.Path(os.environ['REPO_ROOT']) / 'logs' / 'router_audit'\n"
            "p.mkdir(parents=True, exist_ok=True)\n"
            "f = p / ('soak_%d.json' % int(time.time()*1000))\n"
            "f.write_text(json.dumps({'overall_status':'PASS','checks':[]}))\n"
        )
    else:
        helper.write_text("pass\n")

    py.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$2" == "agent.router_soak_monitor" ]]; then\n'
        f'  /usr/bin/env python3 "{helper}"\n'
        f"  exit {soak_rc}\n"
        'elif [[ "$2" == "agent.router_kill_switch" ]]; then\n'
        f'  printf "%s\\n" "$@" >> "{capture}"\n'
        f"  exit {kill_switch_rc}\n"
        'elif [[ "$2" == "agent.router_soak_completion_notifier" ]]; then\n'
        f'  printf "%s\\n" "$@" >> "{notify_capture}"\n'
        f"  exit {notifier_rc}\n"
        "else\n"
        '  echo "unexpected python invocation: $@" >&2\n'
        "  exit 99\n"
        "fi\n"
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return py


def _run_hook(
    repo_root: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "REPO_ROOT": str(repo_root)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(HOOK), *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )


def _stage_repo(tmp_path: Path) -> Path:
    """Materialise a sandbox copy of the wrapper rooted at tmp_path."""
    repo = tmp_path / "fake_repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "router-soak-hourly.sh").write_text(HOOK.read_text())
    (repo / "scripts" / "router-soak-hourly.sh").chmod(0o755)
    return repo


def test_hook_bypass_envvar_short_circuits(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    # No fake python needed — bypass exits before invoking it.
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "ROUTER_SOAK_SKIP": "1"},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0
    assert "skipping" in out.stderr


def test_hook_errors_when_no_result_file(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    _write_fake_python(repo / ".venv", soak_rc=1, write_result=False)
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 5, out.stderr
    assert "no soak result file" in out.stderr


def test_hook_forwards_args_to_kill_switch(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    capture = repo / "kill_switch_argv.txt"
    _write_fake_python(repo / ".venv", soak_rc=0, capture_path=capture)
    out = subprocess.run(
        [
            str(repo / "scripts" / "router-soak-hourly.sh"),
            "--auto-rollback",
            "--no-notify",
        ],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    argv = capture.read_text()
    # The kill-switch saw the latest soak file path AND the forwarded flags.
    assert "--soak-result" in argv
    assert "logs/router_audit/soak_" in argv
    assert "--auto-rollback" in argv
    assert "--no-notify" in argv


def test_hook_runs_kill_switch_even_on_soak_fail(tmp_path: Path) -> None:
    """Soak FAIL (rc=1) must not prevent the kill-switch from firing —
    that is precisely the case the kill-switch exists for."""
    repo = _stage_repo(tmp_path)
    capture = repo / "kill_switch_argv.txt"
    _write_fake_python(repo / ".venv", soak_rc=1, capture_path=capture)
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    # Exit code is whatever the (fake) kill-switch returned (0 here),
    # NOT the soak monitor's rc — that's the contract.
    assert out.returncode == 0, out.stderr
    assert capture.exists() and "--soak-result" in capture.read_text()


def test_hook_runs_completion_notifier_after_kill_switch(tmp_path: Path) -> None:
    """The notifier must fire on every hourly tick so it can arm itself
    once the soak window first goes clean. Default rc=1 (incomplete) must
    NOT change the wrapper's exit code — the kill-switch's rc is the
    authoritative soak verdict."""
    repo = _stage_repo(tmp_path)
    ks_capture = repo / "kill_switch_argv.txt"
    notify_capture = repo / "notifier_argv.txt"
    _write_fake_python(
        repo / ".venv",
        soak_rc=0,
        capture_path=ks_capture,
        kill_switch_rc=0,
        notifier_rc=1,  # incomplete — most-common real-world case
        notifier_capture_path=notify_capture,
    )
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    # Wrapper exit code mirrors the kill-switch (0), NOT the notifier (1).
    assert out.returncode == 0, out.stderr
    assert notify_capture.exists(), "notifier was not invoked"
    # And the kill-switch was still invoked.
    assert ks_capture.exists() and "--soak-result" in ks_capture.read_text()


def test_hook_notifier_failure_does_not_clobber_kill_switch_rc(
    tmp_path: Path,
) -> None:
    """If the notifier itself errors (e.g. Telegram unreachable, rc=2),
    the wrapper still exits with the kill-switch's rc."""
    repo = _stage_repo(tmp_path)
    ks_capture = repo / "kill_switch_argv.txt"
    notify_capture = repo / "notifier_argv.txt"
    _write_fake_python(
        repo / ".venv",
        soak_rc=1,  # soak FAIL
        capture_path=ks_capture,
        kill_switch_rc=1,  # kill-switch dispatched FAIL alert
        notifier_rc=2,  # notifier send-failure
        notifier_capture_path=notify_capture,
    )
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1, out.stderr  # kill-switch rc, not notifier
    assert notify_capture.exists()


def test_hook_notifier_skip_envvar_bypasses_notifier(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    ks_capture = repo / "kill_switch_argv.txt"
    notify_capture = repo / "notifier_argv.txt"
    _write_fake_python(
        repo / ".venv",
        soak_rc=0,
        capture_path=ks_capture,
        kill_switch_rc=0,
        notifier_rc=1,
        notifier_capture_path=notify_capture,
    )
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={
            **os.environ,
            "REPO_ROOT": str(repo),
            "ROUTER_SOAK_NOTIFY_SKIP": "1",
        },
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    # Kill-switch still ran.
    assert ks_capture.exists() and "--soak-result" in ks_capture.read_text()
    # Notifier was bypassed — capture file never created.
    assert not notify_capture.exists()


def test_hook_errors_when_python_venv_missing(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    # No .venv created.
    out = subprocess.run(
        [str(repo / "scripts" / "router-soak-hourly.sh")],
        cwd=repo,
        env={**os.environ, "REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 5
    assert "python venv missing" in out.stderr
