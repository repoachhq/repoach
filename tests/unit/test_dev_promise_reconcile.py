"""Tests for run_promised_tests promise-reconciliation logic.

Includes the end-to-end case: a step whose Developer delivers a
passing test under a mismatched name commits anyway, with the
``dev_runner.promised_tests_reconciled`` warning emitted — the plan is
a hint, the delivered tree is the truth (SP-DEV-PROMISE-RECONCILE).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from ferova.review.dev_runner import execute_plan_step, run_promised_tests
from ferova.review.persistence import init_schema
from ferova.review.plan import ActionPlan, PlanStep


def test_exact_promised_ids_stay_happy_path(tmp_path: Path) -> None:
    """Exact selector match returns green=True, reconciled=False."""
    (tmp_path / "test_example.py").write_text("def test_promised_function():\n    assert True\n")
    green, _tail, reconciled = run_promised_tests(
        tmp_path, ["test_example.py::test_promised_function"]
    )
    assert green is True
    assert reconciled is False


def test_mismatched_names_reconcile_to_delivered_tests(tmp_path: Path) -> None:
    """File-level fallback returns green=True, reconciled=True on name mismatch."""
    (tmp_path / "test_example.py").write_text(
        "def test_actual_delivered_function():\n    assert True\n"
    )
    green, _tail, reconciled = run_promised_tests(
        tmp_path, ["test_example.py::test_promised_function"]
    )
    assert green is True
    assert reconciled is True


@pytest.mark.parametrize(
    "file_content",
    [
        "def test_failing_function():\n    assert False\n",
        "",
    ],
    ids=["failing_test", "empty_file"],
)
def test_red_or_empty_delivered_tests_stay_red(tmp_path: Path, file_content: str) -> None:
    """Failing test and empty file both return (False, tail, False)."""
    (tmp_path / "test_example.py").write_text(file_content)
    green, _tail, reconciled = run_promised_tests(
        tmp_path, ["test_example.py::test_failing_function"]
    )
    assert green is False
    assert reconciled is False


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _git_commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
    )
    return len(out.stdout.strip().splitlines())


def test_step_commits_on_reconciled_tests(tmp_path: Path) -> None:
    """A mismatched-but-green delivery commits, with the reconcile warning."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")
    initial_commits = _git_commit_count(repo)

    step = PlanStep(
        index=1,
        title="Add the test",
        files=["tests/unit/test_x.py"],
        action="Write the test file.",
        commit_message="test(x): add promised test",
        done_when="pytest tests/unit/test_x.py exits 0",
        unit_tests=["tests/unit/test_x.py::test_promised_name"],
    )
    plan = ActionPlan(
        spec_id="SP-RECONCILE-TEST",
        title="Reconcile e2e",
        summary="One step delivering a renamed test.",
        steps=[step],
        integration_tests=[],
    )
    from ferova.review.devagent_loop import DevLoopResult

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / "tests" / "unit" / "test_x.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_delivered_name():\n    assert True\n", encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _step
    db = tmp_path / "t.db"
    init_schema(db)

    with capture_logs() as logs:
        outcome = execute_plan_step(
            step, plan=plan, repo_root=repo, developer=developer, repo_tree="", db=db
        )

    assert outcome.ok is True
    assert _git_commit_count(repo) == initial_commits + 1
    events = {entry["event"] for entry in logs}
    assert "dev_runner.promised_tests_reconciled" in events
