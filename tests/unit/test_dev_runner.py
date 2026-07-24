"""SP-RUFF-PASSED-TRUTHFUL: ``DevSessionResult.ruff_passed`` reflects a real ruff run.

``run_developer_session`` used to set ``ruff_passed = True`` unconditionally at
wrap-up, without any session-level ruff invocation preceding it. This module
drives the real wrap-up self-verify gate (real ``ruff`` subprocess, no mock of
our own flag-computation logic) over a tmp git repo whose tree carries a
genuine lint violation and asserts the reported flag is truthful.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.dev_runner import run_developer_session
from repoach.review.plan import ActionPlan, PlanStep, plan_relpath, render_plan_markdown
from repoach.review.spec import SPECS_DIR

_SPEC_MARKDOWN = """\
# SP-FOO — Fixture feature

Adds one fixture module for the ruff_passed truthfulness test.
"""

_COMMIT_MESSAGE = "feat(fixture): add fixture module"


def _seed_repo(tmp_path: Path, *, violation: bool) -> Path:
    """Build a tmp git repo whose committed tree already satisfies one plan step.

    The step's exact commit message is the repo's HEAD commit subject, so
    ``_step_already_committed`` short-circuits the step loop entirely — the
    session reaches wrap-up (pytest matrix, then self-verify's real
    ``run_ruff_gate`` call) without ever dispatching a Developer turn.
    """
    spec_dir = tmp_path / SPECS_DIR
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "2026-07-13_SP-FOO_fixture.md").write_text(_SPEC_MARKDOWN, encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )

    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "-m", "chore: init"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=False)

    src_dir = tmp_path / "src" / "repoach_fixture"
    src_dir.mkdir(parents=True, exist_ok=True)
    test_dir = tmp_path / "tests" / "unit"
    test_dir.mkdir(parents=True, exist_ok=True)

    if violation:
        module_body = "def helper():\n    return undefined_name\n"
    else:
        module_body = "def helper() -> int:\n    return 1\n"
    (src_dir / "mod.py").write_text(module_body, encoding="utf-8")
    (test_dir / "test_fixture_dummy.py").write_text(
        "def test_dummy_true() -> None:\n    assert True\n", encoding="utf-8"
    )

    for args in (
        ["add", "-A"],
        ["commit", "-q", "-m", _COMMIT_MESSAGE],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=False)

    step = PlanStep(
        index=1,
        title="add fixture module",
        action="Add the fixture module and its dummy test.",
        files=[
            "src/repoach_fixture/mod.py",
            "tests/unit/test_fixture_dummy.py",
            "tests/integration/test_fixture_flow.py",
        ],
        commit_message=_COMMIT_MESSAGE,
        done_when="dummy test passes",
        unit_tests=["tests/unit/test_fixture_dummy.py::test_dummy_true"],
    )
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="Fixture feature",
        summary="Add one fixture module.",
        steps=[step],
        integration_tests=["tests/integration/test_fixture_flow.py"],
    )
    target = tmp_path / plan_relpath("SP-FOO")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")
    return tmp_path


def _run_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, violation: bool):
    repo = _seed_repo(tmp_path, violation=violation)
    monkeypatch.setattr("repoach.review.dev_runner.ensure_branch", lambda *a, **kw: True)
    monkeypatch.setattr("repoach.review.dev_runner.render_repo_tree", lambda **kw: "")
    dev = MagicMock()
    return run_developer_session(
        "FOO",
        repo_root=repo,
        developer=dev,
        push=False,
        db_path=tmp_path / "test.db",
    ), dev


def test_ruff_passed_reflects_real_session_ruff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree with a real ruff violation at wrap-up yields ``ruff_passed is False``.

    A clean tree run of the same session reports ``ruff_passed is True``. The
    step is pre-satisfied on disk (matching commit already in git log) so the
    Developer is never dispatched — only the real wrap-up ``run_ruff_gate``
    call (via self-verify) determines the flag.
    """
    dirty_res, dirty_dev = _run_session(tmp_path / "dirty", monkeypatch, violation=True)
    dirty_dev.develop_step.assert_not_called()
    assert dirty_res.ruff_passed is False

    clean_res, clean_dev = _run_session(tmp_path / "clean", monkeypatch, violation=False)
    clean_dev.develop_step.assert_not_called()
    assert clean_res.ruff_passed is True
