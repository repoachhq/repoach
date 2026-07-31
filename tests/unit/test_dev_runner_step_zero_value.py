"""SP-DEV-STEP-SATISFIED-COMMIT — the step gate refuses a zero-value commit.

A step whose promised tests were ALREADY strictly green on the tree
BEFORE its Developer loop ran, and whose own commit touches nothing
beyond the promised test file(s), contributes no substantive diff: an
earlier step (or round) already delivered the real work. The pre-fix
gate credited that step anyway whenever the exact promised selectors
resolved (the ``reconciled=False`` branch, which ran no equivalent
check). These are the discriminating regression tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from repoach.review.dev_runner import execute_plan_step
from repoach.review.devagent_loop import DevLoopResult
from repoach.review.persistence import init_schema
from repoach.review.plan import ActionPlan, PlanStep

_TEST_FILE = "tests/unit/test_thing.py"
_SOURCE_FILE = "src/thing.py"
_SOURCE_INITIAL = "def thing() -> int:\n    return 1\n"
_INTEGRATION_FILE = "tests/integration/test_thing_integration.py"
_INTEGRATION_BODY = "def test_integration_thing():\n    assert True\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _init_repo(repo: Path, *, test_body: str) -> None:
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "integration").mkdir(parents=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    (repo / _SOURCE_FILE).write_text(_SOURCE_INITIAL, encoding="utf-8")
    (repo / _TEST_FILE).write_text(test_body, encoding="utf-8")
    (repo / _INTEGRATION_FILE).write_text(_INTEGRATION_BODY, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")


def _plan(step: PlanStep) -> ActionPlan:
    return ActionPlan(
        spec_id="SP-DEV-STEP-SATISFIED-COMMIT-TEST",
        title="Zero-value step gate",
        summary="One step exercising the already-green baseline check.",
        steps=[step],
        integration_tests=[f"{_INTEGRATION_FILE}::test_integration_thing"],
    )


def _step() -> PlanStep:
    return PlanStep(
        index=1,
        title="Touch the promised test",
        files=[_TEST_FILE, _SOURCE_FILE, _INTEGRATION_FILE],
        action="Edit the promised test and its source file.",
        commit_message="fix(thing): implement thing",
        done_when="pytest tests/unit/test_thing.py exits 0",
        unit_tests=[f"{_TEST_FILE}::test_thing"],
    )


def _run(repo: Path, tmp_path: Path, step: PlanStep, plan: ActionPlan, developer: MagicMock):
    db = tmp_path / "t.db"
    init_schema(db)
    return execute_plan_step(
        step, plan=plan, repo_root=repo, developer=developer, repo_tree="", db=db
    )


def test_zero_value_step_refused_when_tests_already_green_before_step_ran(
    tmp_path: Path,
) -> None:
    """Baseline strictly green + commit confined to the promised test file → refused."""
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        test_body="def test_thing():\n    assert True\n",
    )

    def _cosmetic_touch(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / _TEST_FILE
        target.write_text("def test_thing():\n    assert 1 == 1\n", encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _cosmetic_touch

    step = _step()
    outcome = _run(repo, tmp_path, step, _plan(step), developer)

    assert outcome.ok is False
    assert "tests/unit/test_thing.py::test_thing" in outcome.reason
    on_disk_source = (repo / _SOURCE_FILE).read_text(encoding="utf-8")
    assert on_disk_source == _SOURCE_INITIAL
    commit_subjects = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert step.commit_message not in commit_subjects.splitlines()


def test_step_commits_normally_when_baseline_green_but_source_file_also_changed(
    tmp_path: Path,
) -> None:
    """G3: a green baseline never blocks a step that also changes a real file."""
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        test_body="def test_thing():\n    assert True\n",
    )
    new_source = "def thing() -> int:\n    return 2\n"

    def _real_change(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        Path(repo_root, _SOURCE_FILE).write_text(new_source, encoding="utf-8")
        target = Path(repo_root) / _TEST_FILE
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _real_change

    step = _step()
    outcome = _run(repo, tmp_path, step, _plan(step), developer)

    assert outcome.ok is True
    on_disk_source = (repo / _SOURCE_FILE).read_text(encoding="utf-8")
    assert on_disk_source == new_source
    committed_files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert _SOURCE_FILE in committed_files


def test_step_with_red_baseline_and_test_only_diff_still_commits(tmp_path: Path) -> None:
    """G4/NG4: a genuinely red-to-green test-only fix is never flagged."""
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        test_body="def test_thing():\n    assert False\n",
    )

    def _fix_assertion(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / _TEST_FILE
        target.write_text("def test_thing():\n    assert True\n", encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _fix_assertion

    step = _step()
    outcome = _run(repo, tmp_path, step, _plan(step), developer)

    assert outcome.ok is True
