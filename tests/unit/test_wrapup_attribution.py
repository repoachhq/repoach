"""Unit tests for the pure step-attribution helper (SP-DEV-WRAPUP-ATTRIBUTION)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from repoach.review.wrapup_attribution import (
    AttributionOutcome,
    StepCommit,
    attribute_failure_to_step,
)

_PASSING_TEST = '"""Demo test module."""\n\n\ndef test_ok() -> None:\n    assert 1 == 1\n'
_FAILING_TEST = '"""Demo test module."""\n\n\ndef test_ok() -> None:\n    assert 1 == 2\n'


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo*; returns combined stdout+stderr."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with an initial passing test_demo.py::test_ok."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_demo.py").write_text(_PASSING_TEST, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init demo repo")
    return repo


def _real_run_selector(repo: Path) -> Callable[[str, str], bool]:
    """Return a ``(sha, selector) -> bool`` closure over a real git worktree.

    Adds a detached worktree at *sha*, runs ``python -m pytest -q <selector>``
    inside it, removes the worktree, and returns whether the run exited zero.
    A failed ``git worktree add`` is propagated as a raised ``RuntimeError``
    so the fail-closed path in :func:`attribute_failure_to_step` is exercised
    by callers that pass a runner touching real infrastructure.
    """

    def _run(sha: str, selector: str) -> bool:
        worktree_dir = repo.parent / f"wt-{uuid.uuid4().hex[:8]}"
        add_proc = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), sha],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {(add_proc.stdout + add_proc.stderr).strip()}"
            )
        try:
            pytest_proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", selector],
                cwd=worktree_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            return pytest_proc.returncode == 0
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_dir)],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            shutil.rmtree(worktree_dir, ignore_errors=True)

    return _run


def test_attribution_names_introducing_step(tmp_path: Path) -> None:
    """Step 2 rewrites test_demo.py to fail — attribution names step 2."""
    repo = _init_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: step one, unrelated change")
    step1_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "tests" / "unit" / "test_demo.py").write_text(_FAILING_TEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: step two, breaks test_ok")
    step2_sha = _git(repo, "rev-parse", "HEAD")

    step_commits = [
        StepCommit(0, "plan base", base_sha),
        StepCommit(1, "step one", step1_sha),
        StepCommit(2, "step two", step2_sha),
    ]

    result = attribute_failure_to_step(
        "tests/unit/test_demo.py::test_ok",
        step_commits,
        run_selector=_real_run_selector(repo),
    )

    assert result.status == "introduced_by_step"
    assert result.step is not None
    assert result.step.index == 2


def test_attribution_reports_pre_existing_failure(tmp_path: Path) -> None:
    """Base commit's test_ok already fails — pre_existing, no step blamed."""
    repo = _init_repo(tmp_path)
    (repo / "tests" / "unit" / "test_demo.py").write_text(_FAILING_TEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: base already broken")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "unrelated1.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: step one, never fixes it")
    step1_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "unrelated2.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: step two, never fixes it either")
    step2_sha = _git(repo, "rev-parse", "HEAD")

    step_commits = [
        StepCommit(0, "plan base", base_sha),
        StepCommit(1, "step one", step1_sha),
        StepCommit(2, "step two", step2_sha),
    ]

    result = attribute_failure_to_step(
        "tests/unit/test_demo.py::test_ok",
        step_commits,
        run_selector=_real_run_selector(repo),
    )

    assert result.status == "pre_existing"
    assert result.step is not None
    assert result.step.index == 0


def test_attribution_runner_error_fails_closed() -> None:
    """A run_selector that raises yields an error outcome, no exception escapes."""

    def _explode(sha: str, selector: str) -> bool:
        raise RuntimeError("worktree exploded")

    step_commits = [StepCommit(0, "plan base", "deadbeef")]

    result = attribute_failure_to_step(
        "tests/unit/test_demo.py::test_ok",
        step_commits,
        run_selector=_explode,
    )

    assert isinstance(result, AttributionOutcome)
    assert result.status == "error"
    assert result.error is not None
    assert "worktree exploded" in result.error
