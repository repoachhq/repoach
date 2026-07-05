"""SP-DEV-PROMISE-DELIVERY integration — end-to-end promise-delivery gate flow.

Exercises :func:`execute_plan_step` against a real temp git repo with a
scripted fake Developer: nominal exact-match, G1 untouched-file refusal,
and G2 single-drift mechanical rename.  The unit suite
(``TestPromisedTestGateG1G2``) already covers the AC1-AC3 behaviour;
this file guards against regressions that only a real git worktree +
pytest invocation can catch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.dev_runner import execute_plan_step
from ferova.review.devagent_loop import DevLoopResult
from ferova.review.persistence import init_schema
from ferova.review.plan import ActionPlan, PlanStep

_SPEC_ID = "SP-PROM-INT"

_CLEAN_MODULE = '"""Demo module."""\n\nVALUE = 1\n'
_PROMISED_TEST = '"""Demo test."""\n\n\ndef test_value() -> None:\n    assert 1 == 1\n'
_DRIFTED_TEST = '"""Demo test."""\n\n\ndef test_drifted() -> None:\n    assert 1 == 1\n'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a temp git repo with the spec doc and an initial commit."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-07-05_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — promise delivery\n\n## Why\n\nIntegration test.\n\n"
        "## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("promise delivery demo\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init demo repo")
    init_schema(repo.parent / "test.db")
    return repo


def _one_step_plan(**overrides: object) -> ActionPlan:
    """Return a one-step plan with the overrides applied to the step dict."""
    step: dict[str, object] = {
        "index": 1,
        "title": "Add the demo module",
        "files": ["src/mini.py", "tests/unit/test_mini.py", "tests/integration/test_demo_flow.py"],
        "action": "Create the module and its test.",
        "commit_message": "feat(demo): add mini module",
        "done_when": "pytest tests/unit/test_mini.py is green",
        "unit_tests": ["tests/unit/test_mini.py"],
    }
    step.update(overrides)
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Promise delivery demo",
        summary="One-step demo.",
        steps=[PlanStep(**step)],
        integration_tests=["tests/integration/test_demo_flow.py"],
    )


def _scripted_developer(attempts: list[list[tuple[str, str]]]) -> MagicMock:
    """Fake Developer that writes files per attempt, mirroring the loop's tool calls.

    Each attempt is a list of ``(repo-relative path, content)`` tuples.  The
    last attempt repeats if the runner calls more times than supplied.
    """
    state = {"i": 0}

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        idx = min(state["i"], len(attempts) - 1)
        for rel, content in attempts[idx]:
            target = Path(repo_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        state["i"] += 1
        return DevLoopResult(text="fake summary", model_used="fake", turns=1, tokens_used=1)

    dev = MagicMock()
    dev.develop_step.side_effect = _step
    return dev


class TestPromiseDeliveryIntegration:
    """End-to-end promise-delivery gate flow against a real temp git repo."""

    def test_nominal_exact_names_green_first_attempt(self, tmp_path: Path) -> None:
        """Nominal case: Developer writes the promised test with exact names, step green in one attempt."""
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        dev = _scripted_developer(
            [[("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", _PROMISED_TEST)]]
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is True
        assert dev.develop_step.call_count == 1
        committed = (repo / "tests" / "unit" / "test_mini.py").read_text()
        assert "def test_value(" in committed
        assert "def test_drifted(" not in committed

    def test_untouched_promised_file_retries_then_green(self, tmp_path: Path) -> None:
        """G1 case: first write only source while pre-existing drifted test is green,
        first attempt returns ok=False with gate_feedback naming missing selectors,
        second attempt writes the exact test and goes green.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        (repo / "tests" / "unit" / "test_mini.py").write_text(_DRIFTED_TEST, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: pre-seed drifted test")
        dev = _scripted_developer(
            [
                [("src/mini.py", _CLEAN_MODULE)],
                [("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", _PROMISED_TEST)],
            ]
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is True
        assert dev.develop_step.call_count == 2
        retry_brief = dev.develop_step.call_args_list[1].kwargs["brief"]
        assert "promised-test gate: reconciled green but the loop did not touch" in retry_brief
        assert "tests/unit/test_mini.py::test_value" in retry_brief
        assert (repo / "tests" / "unit" / "test_mini.py").read_text() == _PROMISED_TEST

    def test_single_drift_renamed_and_green(self, tmp_path: Path) -> None:
        """G2 case: Developer writes the promised file with a single drifted test name,
        step green in one attempt and the promised node id is present in the committed file.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        dev = _scripted_developer(
            [[("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", _DRIFTED_TEST)]]
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is True
        assert dev.develop_step.call_count == 1
        committed = (repo / "tests" / "unit" / "test_mini.py").read_text()
        assert "def test_value(" in committed
        assert "def test_drifted(" not in committed
