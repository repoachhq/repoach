"""Lint-aware third-attempt tests for execute_plan_step (SP-DEV-STEP-LOOP-HARDEN)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import repoach.review.dev_runner as dev_runner
from repoach.review.dev_runner import execute_plan_step
from repoach.review.plan import ActionPlan, PlanStep


def _plan() -> ActionPlan:
    step = PlanStep(
        index=1,
        title="Add a module",
        files=["src/x.py", "tests/unit/test_x.py", "tests/integration/test_x.py"],
        action="Create the module.",
        commit_message="feat: x",
        done_when="green",
        unit_tests=["tests/unit/test_x.py::test_lint_failure_earns_a_third_attempt"],
    )
    return ActionPlan(
        spec_id="SP-X",
        title="t",
        summary="s",
        steps=[step],
        integration_tests=["tests/integration/test_x.py"],
    )


def _developer() -> MagicMock:
    from repoach.review.devagent_loop import DevLoopResult

    dev = MagicMock()
    dev.develop_step.return_value = DevLoopResult(
        text="s", model_used="fake", turns=1, tokens_used=0
    )
    return dev


def _neutralise_io(monkeypatch) -> None:
    monkeypatch.setattr(dev_runner, "record_coder_response", lambda *a, **k: None)
    monkeypatch.setattr(
        dev_runner, "_changed_paths", lambda *a, **k: ["src/x.py", "tests/unit/test_x.py"]
    )
    monkeypatch.setattr(dev_runner, "heal_inline_comments", lambda *a, **k: [])
    monkeypatch.setattr(dev_runner, "check_python_syntax", lambda *a, **k: (True, ""))


def _seq(*returns):
    """A side_effect returning each value in turn, repeating the last."""
    state = {"i": 0}

    def _fn(*a, **k):
        value = returns[min(state["i"], len(returns) - 1)]
        state["i"] += 1
        return value

    return _fn


def _run(dev: MagicMock, tmp_path: Path) -> None:
    execute_plan_step(
        _plan().steps[0],
        plan=_plan(),
        repo_root=tmp_path,
        developer=dev,
        repo_tree="",
        db=tmp_path / "x.db",
    )


def test_lint_failure_earns_a_third_attempt(tmp_path: Path, monkeypatch) -> None:
    _neutralise_io(monkeypatch)
    monkeypatch.setattr(dev_runner, "check_imports", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dev_runner, "run_ruff_gate", lambda *a, **k: (False, "ruff bad"))

    dev = _developer()
    _run(dev, tmp_path)

    assert dev.develop_step.call_count == 3


def test_lint_then_non_lint_stops_after_two_attempts(tmp_path: Path, monkeypatch) -> None:
    _neutralise_io(monkeypatch)
    monkeypatch.setattr(dev_runner, "check_imports", _seq((True, ""), (False, "import bad")))
    monkeypatch.setattr(dev_runner, "run_ruff_gate", _seq((False, "ruff bad")))

    dev = _developer()
    _run(dev, tmp_path)

    assert dev.develop_step.call_count == 2


def test_non_lint_then_lint_earns_a_third_attempt(tmp_path: Path, monkeypatch) -> None:
    _neutralise_io(monkeypatch)
    monkeypatch.setattr(
        dev_runner, "check_imports", _seq((False, "import bad"), (True, ""), (True, ""))
    )
    monkeypatch.setattr(dev_runner, "run_ruff_gate", _seq((False, "ruff bad")))

    dev = _developer()
    _run(dev, tmp_path)

    assert dev.develop_step.call_count == 3


def test_non_lint_failure_stops_after_two_attempts(tmp_path: Path, monkeypatch) -> None:
    _neutralise_io(monkeypatch)
    monkeypatch.setattr(dev_runner, "check_imports", lambda *a, **k: (False, "import bad"))

    dev = _developer()
    _run(dev, tmp_path)

    assert dev.develop_step.call_count == 2


def test_repo_lint_failure_also_earns_a_third_attempt(tmp_path: Path, monkeypatch) -> None:
    _neutralise_io(monkeypatch)
    monkeypatch.setattr(dev_runner, "check_imports", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dev_runner, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(dev_runner, "run_repo_lint_gates", lambda *a, **k: (False, "inline"))

    dev = _developer()
    _run(dev, tmp_path)

    assert dev.develop_step.call_count == 3
