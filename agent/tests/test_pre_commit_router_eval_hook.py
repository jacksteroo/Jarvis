"""Smoke tests for scripts/git-hooks/pre-commit-router-eval.

The hook itself is a bash script; these tests verify it (a) exists and is
executable, (b) carries the contractual invariants the migration spec
requires (threshold 0.85, the canonical pattern list), and (c) actually
short-circuits via the SKIP_ROUTER_EVAL bypass and the no-router-files-
staged path inside a throwaway git repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "git-hooks" / "pre-commit-router-eval"


def test_hook_exists_and_is_executable() -> None:
    assert HOOK.is_file(), f"missing hook at {HOOK}"
    assert os.access(HOOK, os.X_OK), f"hook not executable: {HOOK}"


def test_hook_carries_required_invariants() -> None:
    text = HOOK.read_text()
    # Phase 3 exit criterion: pass threshold is 0.85.
    assert "--threshold 0.85" in text
    # Module the hook delegates to.
    assert "agent.router_eval" in text
    # Bypass envvar must be honored, exact name (documented in SEMANTIC_ROUTER.md).
    assert "SKIP_ROUTER_EVAL" in text
    # Spot-check a couple of pattern entries that gate which files trigger the run.
    for needle in (
        "agent/semantic_router",
        "agent/query_router",
        "tests/router_eval_set",
    ):
        assert needle in text, f"hook missing path pattern for {needle}"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_hook_skip_envvar_short_circuits(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {**os.environ, "SKIP_ROUTER_EVAL": "1"}
    proc = subprocess.run(
        [str(HOOK)], cwd=repo, env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipping" in proc.stdout.lower()


def test_hook_no_router_files_exits_clean(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Stage an unrelated file; hook should ignore and exit 0.
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    proc = subprocess.run(
        [str(HOOK)], cwd=repo, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    # Silent path → no eval output.
    assert "router_eval" not in proc.stdout


def test_hook_router_file_attempts_eval(tmp_path: Path) -> None:
    """When a router-relevant path is staged, the hook must attempt to invoke
    the eval (we don't have the project venv inside the throwaway repo, so it
    should fail with the venv-missing message — confirming the gate path was
    reached rather than skipped silently).
    """
    if shutil.which("git") is None:
        pytest.skip("git missing")
    repo = _make_repo(tmp_path)
    target_dir = repo / "agent"
    target_dir.mkdir()
    target = target_dir / "semantic_router.py"
    target.write_text("# stub\n")
    subprocess.run(["git", "add", "agent/semantic_router.py"], cwd=repo, check=True)
    proc = subprocess.run(
        [str(HOOK)], cwd=repo, capture_output=True, text=True
    )
    # Must not be the silent-success no-op (rc=0 with no eval mention).
    combined = proc.stdout + proc.stderr
    assert (
        "router_eval" in combined
        or "venv" in combined
        or proc.returncode != 0
    ), f"hook silently skipped router-file diff: rc={proc.returncode} out={combined!r}"
