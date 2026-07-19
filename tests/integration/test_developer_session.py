"""SP-DEV-PLAN-EXEC integration — a full plan-driven session, end to end.

Runs :func:`run_developer_session` over a REAL tmp git repository with
a two-step committed plan and a scripted Developer fake: step 1 lands
clean, step 2 fails its first attempt (broken syntax) and succeeds on
the retry. Asserts the branch history the architecture promises —
plan document first, then exactly one commit per green step — plus
the retry feedback loop and the session tracking fields.

This file is the plan-level integration test the action plan
promised; the CI stage that runs ``tests/integration/`` ships with
SP-INTEGRATION-STAGE.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from repoach.review.dev_runner import run_developer_session
from repoach.review.plan import ActionPlan, PlanStep, plan_relpath, render_plan_markdown

_SPEC_ID = "SP-INT-DEMO"

_MODULE_ONE = '"""Module one."""\n\nONE = 1\n'
_TEST_ONE = '"""Test one."""\n\n\ndef test_one() -> None:\n    assert 1 == 1\n'
_MODULE_TWO_BROKEN = "def two(:\n"
_MODULE_TWO_CLEAN = '"""Module two."""\n\nTWO = 2\n'
_TEST_TWO = '"""Test two."""\n\n\ndef test_two() -> None:\n    assert 2 == 2\n'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _two_step_plan() -> ActionPlan:
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Two-step demo",
        summary="Step one lands; step two needs its retry.",
        steps=[
            PlanStep(
                index=1,
                title="Add module one",
                files=["src/one.py", "tests/unit/test_one.py"],
                action="Create module one and its test.",
                commit_message="feat(demo): module one",
                done_when="pytest tests/unit/test_one.py is green",
                unit_tests=["tests/unit/test_one.py::test_one"],
            ),
            PlanStep(
                index=2,
                title="Add module two",
                files=[
                    "src/two.py",
                    "tests/unit/test_two.py",
                    "tests/integration/test_demo_absent.py",
                ],
                action="Create module two and its test.",
                commit_message="feat(demo): module two",
                done_when="pytest tests/unit/test_two.py is green",
                unit_tests=["tests/unit/test_two.py::test_two"],
            ),
        ],
        integration_tests=["tests/integration/test_demo_absent.py"],
    )


def _scripted_developer() -> MagicMock:
    """Fake Developer whose ``develop_step`` writes a scripted set per call.

    Three sequential calls model: step 1 (clean), step 2 attempt 1 (broken
    syntax → retry fix-forward), step 2 attempt 2 (clean). Each writes its files
    to disk, mirroring the agentic loop's ``write_file`` tool calls.
    """
    from repoach.review.devagent_loop import DevLoopResult

    attempts = [
        [("src/one.py", _MODULE_ONE), ("tests/unit/test_one.py", _TEST_ONE)],
        [("src/two.py", _MODULE_TWO_BROKEN), ("tests/unit/test_two.py", _TEST_TWO)],
        [("src/two.py", _MODULE_TWO_CLEAN), ("tests/unit/test_two.py", _TEST_TWO)],
    ]
    state = {"i": 0}

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        idx = min(state["i"], len(attempts) - 1)
        for rel, content in attempts[idx]:
            target = Path(repo_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        state["i"] += 1
        return DevLoopResult(text="scripted", model_used="fake", turns=1, tokens_used=0)

    dev = MagicMock()
    dev.develop_step.side_effect = _step
    return dev


def test_full_session_plan_first_one_commit_per_step_with_retry(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-06-07_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo\n\n## Why\n\nIntegration.\n\n## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
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

    plan = _two_step_plan()
    target = repo / plan_relpath(_SPEC_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")

    monkeypatch.setattr("repoach.review.dev_runner.ensure_branch", lambda *a, **kw: True)
    dev = _scripted_developer()

    result = run_developer_session(
        _SPEC_ID,
        repo_root=repo,
        developer=dev,
        push=False,
        db_path=tmp_path / "outside.db",
        judge=lambda prompt: '{"compliant": true, "reasons": "ok", "gaps": []}',
    )

    assert result.plan_committed is True
    assert result.steps_total == 2
    assert result.steps_completed == 2
    assert result.failed_step_index is None
    assert result.pytest_passed is True
    assert result.fixes_applied == 4
    assert dev.develop_step.call_count == 3

    retry_brief = dev.develop_step.call_args_list[2].kwargs["brief"]
    assert "Previous attempt failed its gates" in retry_brief
    assert "SyntaxError" in retry_brief

    subjects = _git(repo, "log", "--format=%s").splitlines()
    assert subjects[0] == "feat(demo): module two"
    assert subjects[1] == "feat(demo): module one"
    assert subjects[2] == f"docs(plan): {_SPEC_ID} action plan"
    assert subjects[3] == "chore: init"
