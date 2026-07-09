"""Unit tests for the wrap-up attribution wiring helpers in dev_runner.

SP-DEV-WRAPUP-ATTRIBUTION step 2: :func:`_step_commits_for_plan` maps the
plan base and each step's commit onto the branch's git log, and
:func:`_collect_failing_wrapup_selectors` runs the full unit suite and
reports the selectors that failed outside the plan's promised set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ferova.review.dev_runner import (
    _collect_failing_wrapup_selectors,
    _step_commits_for_plan,
)
from ferova.review.plan import ActionPlan, PlanStep

_PASSING_TEST = '"""Demo test module."""\n\n\ndef test_ok() -> None:\n    assert 1 == 1\n'


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo*; returns combined stdout+stderr."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with an initial passing test on develop, then impl."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_demo.py").write_text(_PASSING_TEST, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init demo repo")
    _git(repo, "branch", "-m", "develop")
    _git(repo, "switch", "-c", "impl")
    return repo


def _one_step_plan() -> ActionPlan:
    """Two-step demo plan whose commit messages are matched against a real git log."""
    return ActionPlan(
        spec_id="SP-WRAPUP-DEMO",
        title="Demo plan",
        summary="Two-step demo.",
        steps=[
            PlanStep(
                index=1,
                title="Step one",
                files=[
                    "src/mini.py",
                    "tests/unit/test_demo.py",
                    "tests/integration/test_wrap_demo.py",
                ],
                action="Add the mini module.",
                commit_message="feat(demo): step one",
                done_when="pytest is green",
                unit_tests=["tests/unit/test_demo.py::test_ok"],
            ),
            PlanStep(
                index=2,
                title="Step two",
                files=["src/mini2.py"],
                action="Add the second mini module.",
                commit_message="feat(demo): step two",
                done_when="pytest is green",
                unit_tests=["tests/unit/test_demo.py::test_ok"],
            ),
        ],
        integration_tests=["tests/integration/test_wrap_demo.py"],
    )


def test_step_commits_for_plan_maps_base_and_steps_in_order(tmp_path: Path) -> None:
    """Base + each step's commit resolve, in plan order, matching real SHAs."""
    repo = _init_repo(tmp_path)
    merge_base_sha = _git(repo, "rev-parse", "develop")
    plan = _one_step_plan()

    (repo / "src" / "mini.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(demo): step one")
    step1_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "mini2.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(demo): step two")
    step2_sha = _git(repo, "rev-parse", "HEAD")

    commits = _step_commits_for_plan(repo, plan, "develop")

    assert [c.index for c in commits] == [0, 1, 2]
    assert commits[0].sha == merge_base_sha
    assert commits[1].sha == step1_sha
    assert commits[2].sha == step2_sha


def test_collect_failing_wrapup_selectors_excludes_promised(tmp_path: Path) -> None:
    """Only the unpromised failing selector is returned."""
    repo = _init_repo(tmp_path)
    (repo / "tests" / "unit" / "test_wrap_demo.py").write_text(
        '"""Wrap-up demo failures."""\n\n\n'
        "def test_a() -> None:\n    assert 1 == 2\n\n\n"
        "def test_b() -> None:\n    assert 1 == 2\n",
        encoding="utf-8",
    )

    failing = _collect_failing_wrapup_selectors(repo, {"tests/unit/test_wrap_demo.py::test_a"})

    assert "tests/unit/test_wrap_demo.py::test_b" in failing
    assert "tests/unit/test_wrap_demo.py::test_a" not in failing
