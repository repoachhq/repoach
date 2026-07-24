"""SP-RUFF-PASSED-TRUTHFUL AC2 — real session-level ``ruff_passed`` over a real repo.

Drives :func:`run_developer_session` (the real dev_runner entrypoint) over a
REAL tmp git repository whose tree already satisfies its one plan step (the
session-resume path: the step's commit message already sits at HEAD, so the
Developer is never dispatched and the session goes straight to wrap-up).
No ruff invocation is monkeypatched anywhere — :func:`run_ruff_gate` runs the
real ``ruff`` binary against real files during the wrap-up self-verify gate.
A genuine lint violation (an undefined name, not autofixable) yields
``ruff_passed is False``; the clean-tree run reports ``True``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from repoach.review.dev_runner import run_developer_session
from repoach.review.plan import ActionPlan, PlanStep, plan_relpath, render_plan_markdown

_SPEC_ID = "SP-INT-RUFFPASSED"
_COMMIT_MESSAGE = "feat(demo): add ruff-passed fixture module"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _one_step_plan() -> ActionPlan:
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Ruff-passed truthfulness demo",
        summary="One pre-satisfied step; wrap-up must run a real session-level ruff check.",
        steps=[
            PlanStep(
                index=1,
                title="Add the fixture module",
                files=[
                    "src/ruffpassed_demo.py",
                    "tests/unit/test_ruffpassed_demo.py",
                    "tests/integration/test_ruffpassed_demo_flow.py",
                ],
                action="Add the fixture module and its dummy test.",
                commit_message=_COMMIT_MESSAGE,
                done_when="pytest tests/unit/test_ruffpassed_demo.py is green",
                unit_tests=["tests/unit/test_ruffpassed_demo.py::test_dummy_true"],
            )
        ],
        integration_tests=["tests/integration/test_ruffpassed_demo_flow.py"],
    )


def _build_repo(tmp_path: Path, *, violation: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-07-13_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo\n\n## Why\n\nIntegration fixture.\n\n"
        "## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "-m", "chore: init"],
    ):
        _git(repo, *args)

    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    if violation:
        module_body = "def broken():\n    return undefined_name\n"
    else:
        module_body = "def clean() -> int:\n    return 1\n"
    (repo / "src" / "ruffpassed_demo.py").write_text(module_body, encoding="utf-8")
    (repo / "tests" / "unit" / "test_ruffpassed_demo.py").write_text(
        "def test_dummy_true() -> None:\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", _COMMIT_MESSAGE)

    plan = _one_step_plan()
    target = repo / plan_relpath(_SPEC_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")
    return repo


def test_ruff_passed_reflects_real_session_ruff_integration(tmp_path: Path, monkeypatch) -> None:
    """No ruff monkeypatch anywhere: real ``run_ruff_gate`` over real files."""
    monkeypatch.setattr("repoach.review.dev_runner.ensure_branch", lambda *a, **kw: True)

    violated_repo = _build_repo(tmp_path / "violated", violation=True)
    violated_result = run_developer_session(
        _SPEC_ID,
        repo_root=violated_repo,
        developer=MagicMock(),
        push=False,
        db_path=tmp_path / "violated.db",
    )
    assert violated_result.ruff_passed is False

    clean_repo = _build_repo(tmp_path / "clean", violation=False)
    clean_result = run_developer_session(
        _SPEC_ID,
        repo_root=clean_repo,
        developer=MagicMock(),
        push=False,
        db_path=tmp_path / "clean.db",
    )
    assert clean_result.ruff_passed is True
