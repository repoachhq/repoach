"""SP-DEV-WRAPUP-ATTRIBUTION end-to-end integration test.

Exercises :func:`ferova.review.dev_runner.repair_wrapup_failures` against a
real throwaway git repository and a real pytest invocation: a two-step plan
whose second step breaks an unpromised unit test, attributed to that step
via a real git-worktree selector runner, then repaired by a fake Developer
that lands a fully-passing rewrite.  Hermetic: no network, no LLM, no
reliance on a ``.env`` file — the only external process invoked is git and
the repo's own pytest.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.dev_runner import DevSessionResult, repair_wrapup_failures
from ferova.review.devagent_loop import DevLoopResult
from ferova.review.persistence import init_schema
from ferova.review.plan import ActionPlan, PlanStep

_WRAP_E2E_GREEN = (
    '"""Wrap-up end-to-end demo module — both tests pass."""\n\n\n'
    "def test_a() -> None:\n    assert 1 == 1\n\n\n"
    "def test_b() -> None:\n    assert 1 == 1\n"
)

_WRAP_E2E_BROKEN = (
    '"""Wrap-up end-to-end demo module — step 2 rewrite breaks test_b."""\n\n\n'
    "def test_a() -> None:\n    assert 1 == 1\n\n\n"
    "def test_b() -> None:\n    assert 1 == 2\n"
)

_WRAP_E2E_FIXED = (
    '"""Wrap-up end-to-end demo module — repaired by the fake Developer."""\n\n\n'
    "def test_a() -> None:\n    assert 1 == 1\n\n\n"
    "def test_b() -> None:\n    assert 1 == 1\n"
)

_MARKER_MODULE = '"""Unrelated marker module touched by step 1 — a no-op relative to step 2."""\n\nMARKER = "wrap-e2e"\n'

_MARKER_TEST = (
    '"""Unit test for the step-1 marker module."""\n\n\n'
    "def test_marker() -> None:\n"
    "    from tests.unit.wrap_e2e_marker import MARKER\n\n"
    '    assert MARKER == "wrap-e2e"\n'
)


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo*; returns combined stdout+stderr."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    """Build a throwaway git repo: a ``develop`` branch with a passing wrap-e2e test."""
    repo = tmp_path / "repo"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "unit" / "test_wrap_e2e.py").write_text(_WRAP_E2E_GREEN, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init wrap-e2e demo repo")
    _git(repo, "branch", "-m", "develop")
    _git(repo, "switch", "-c", "impl")
    return repo


def _two_step_plan() -> ActionPlan:
    """Two-step plan: step 1 is a no-op marker, step 2 breaks the unpromised ``test_b``."""
    return ActionPlan(
        spec_id="SP-WRAPUP-E2E-DEMO",
        title="Wrap-up attribution end-to-end demo",
        summary="Step 2 regresses an unpromised test; the wrap-up path must attribute and repair it.",
        steps=[
            PlanStep(
                index=1,
                title="Add an unrelated marker module",
                files=[
                    "tests/unit/wrap_e2e_marker.py",
                    "tests/unit/test_wrap_e2e_marker.py",
                ],
                action="Add a marker module unrelated to the wrap-e2e test suite, plus its unit test.",
                commit_message="feat(wrape2e): add unrelated marker module",
                done_when="pytest tests/unit/test_wrap_e2e_marker.py is green",
                unit_tests=["tests/unit/test_wrap_e2e_marker.py::test_marker"],
            ),
            PlanStep(
                index=2,
                title="Rewrite the wrap-e2e test module",
                files=["tests/unit/test_wrap_e2e.py"],
                action="Rewrite the wrap-e2e test module, promising only test_a.",
                commit_message="fix(wrape2e): rewrite wrap-e2e test module",
                done_when="pytest tests/unit/test_wrap_e2e.py::test_a is green",
                unit_tests=["tests/unit/test_wrap_e2e.py::test_a"],
            ),
        ],
        integration_tests=[],
    )


def _fake_wrapup_repair_developer() -> MagicMock:
    """Fake Developer that repairs the wrap-e2e module only for a wrap-up repair dispatch.

    Mirrors the real Developer's ``develop_step`` keyword shape.  Only a
    ``spec_id`` containing ``"wrapup-repair"`` (the marker
    :func:`ferova.review.dev_runner.repair_wrapup_failures` stamps on every
    repair dispatch) triggers the write — any other call is a wiring bug.
    """
    dev = MagicMock()

    def _step(
        *,
        brief: str,
        repo_root: Path,
        allowed_paths: list[str],
        repo_tree: str = "",
        spec_id: str | None = None,
    ) -> DevLoopResult:
        assert spec_id is not None and "wrapup-repair" in spec_id
        (Path(repo_root) / "tests" / "unit" / "test_wrap_e2e.py").write_text(
            _WRAP_E2E_FIXED, encoding="utf-8"
        )
        return DevLoopResult(text="repaired", model_used="stub", turns=1, tokens_used=0)

    dev.develop_step.side_effect = _step
    return dev


def test_cross_cutting_breakage_attributed_and_repaired(tmp_path: Path) -> None:
    """Full attribution + repair: step 2's breakage of an unpromised test gets fixed.

    Builds a throwaway repo with ``develop`` (both wrap-e2e tests green) and
    an ``impl`` branch carrying two step commits, the second of which
    breaks ``test_b`` without promising it.  Calling
    :func:`repair_wrapup_failures` directly attributes the failure to step 2,
    dispatches one bounded repair to the fake Developer, and lands a
    ``fix(wrapup): ...`` commit once the selector is green again.
    """
    repo = _init_repo(tmp_path)
    plan = _two_step_plan()

    (repo / "tests" / "unit" / "wrap_e2e_marker.py").write_text(_MARKER_MODULE, encoding="utf-8")
    (repo / "tests" / "unit" / "test_wrap_e2e_marker.py").write_text(_MARKER_TEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", plan.steps[0].commit_message)

    (repo / "tests" / "unit" / "test_wrap_e2e.py").write_text(_WRAP_E2E_BROKEN, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", plan.steps[1].commit_message)

    db_path = tmp_path / "test.db"
    init_schema(db_path)

    fake_dev = _fake_wrapup_repair_developer()
    result = DevSessionResult(spec_id=plan.spec_id)

    outcome = repair_wrapup_failures(
        repo,
        plan,
        dev=fake_dev,
        db=db_path,
        base="develop",
        failing_selectors=["tests/unit/test_wrap_e2e.py::test_b"],
        result=result,
    )

    assert outcome is None

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/unit/test_wrap_e2e.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    log = _git(repo, "log", "--format=%s")
    assert any(
        line == "fix(wrapup): tests/unit/test_wrap_e2e.py::test_b broken by step 2"
        for line in log.splitlines()
    ), log
