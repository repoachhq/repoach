"""SP-DEV-PLAN-EXEC — the plan-driven step executor.

Drives :func:`execute_plan_step` and the plan-first session against REAL git
repos in tmp (init + commits are exercised for real; only the Developer/Planner
agents are fakes). SP-DEVAGENT-LOOP rewires the executor onto the agentic loop, so
the fake Developer now *writes files to disk* per attempt (mirroring the
``write_file`` tool calls the real loop makes) and returns a ``DevLoopResult``;
a red gate retries fix-forward and an exhausted step leaves its work in place
(no destructive revert).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.dev_runner import (
    DevSessionResult,
    _step_already_committed,
    build_step_brief,
    commit_paths,
    execute_plan_step,
    load_or_produce_plan,
    run_developer_session,
    step_preflight_complete,
)
from ferova.review.devagent_loop import DevLoopResult
from ferova.review.persistence import init_schema
from ferova.review.plan import (
    ActionPlan,
    PlanStep,
    plan_relpath,
    render_plan_markdown,
)

_SPEC_ID = "SP-EXEC-DEMO"

_CLEAN_MODULE = '"""Demo module."""\n\nVALUE = 1\n'
_CLEAN_TEST = '"""Demo test."""\n\n\ndef test_value() -> None:\n    assert 1 == 1\n'
_GREEN_INTEGRATION = '"""Integration test."""\n\n\ndef test_flow() -> None:\n    assert True\n'
_BROKEN_MODULE = "def broken(:\n"
_LINT_FAILING_MODULE = '"""Mini."""\n\nVALUE = undefined_name\n'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-06-07_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo\n\n## Why\n\nTesting.\n\n## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("demo repo\n", encoding="utf-8")
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


def _one_step_plan(*, integration_tests: list[str] | None = None, **step_overrides) -> ActionPlan:
    """One-step demo plan; integration promises are auto-added to the contract.

    SP-PLAN-CONTRACT-LINTS made undeliverable integration promises a
    validation error, so the default promise must live in the step's
    files; pass ``integration_tests=[]`` for a docs-only fixture.
    """
    if integration_tests is None:
        integration_tests = ["tests/integration/test_demo_flow.py"]
    step = {
        "index": 1,
        "title": "Add the demo module",
        "files": ["src/mini.py", "tests/unit/test_mini.py"],
        "action": "Create the module and its test.",
        "commit_message": "feat(demo): add mini module",
        "done_when": "pytest tests/unit/test_mini.py is green",
        "unit_tests": ["tests/unit/test_mini.py::test_value"],
    }
    step.update(step_overrides)
    promised_files = [sel.split("::", 1)[0] for sel in integration_tests]
    step["files"] = [*step["files"], *[f for f in promised_files if f not in step["files"]]]
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Demo plan",
        summary="One-step demo.",
        steps=[PlanStep(**step)],
        integration_tests=integration_tests,
    )


def _seed_plan(repo: Path, plan: ActionPlan) -> None:
    target = repo / plan_relpath(plan.spec_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")


def _developer_writing(attempts: list[list[tuple[str, str]]]) -> MagicMock:
    """Fake Developer whose ``develop_step`` writes files per attempt.

    Each attempt is a list of ``(repo-relative path, content)`` the simulated
    agentic loop writes to disk (mirroring ``write_file`` tool calls), then returns
    a :class:`DevLoopResult`. The fake bypasses the real tools so the runner's own
    post-loop gates and contract guard can be exercised directly. The last attempt
    repeats if the runner calls more times than supplied.
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


def _good_attempt() -> list[tuple[str, str]]:
    return [("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", _CLEAN_TEST)]


def _compliant_judge(prompt: str) -> str:
    return '{"compliant": true, "reasons": "ok", "gaps": []}'


def _developer_scripted(steps: list) -> MagicMock:
    """Fake Developer playing *steps* in order on each ``develop_step`` call.

    Each step is either a list of ``(path, content)`` writes (mirroring the loop's
    tool calls) or a pre-built :class:`DevLoopResult` (e.g. an ``error`` result that
    models a brain/proxy outage). The last step repeats if called more often.
    """
    state = {"i": 0}

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        item = steps[min(state["i"], len(steps) - 1)]
        state["i"] += 1
        if isinstance(item, DevLoopResult):
            return item
        for rel, content in item:
            target = Path(repo_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return DevLoopResult(text="fake summary", model_used="fake", turns=1, tokens_used=1)

    dev = MagicMock()
    dev.develop_step.side_effect = _step
    return dev


class TestLoadOrGeneratePlan:
    def test_load_or_generate_plan(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        from ferova.review.spec import load_spec

        spec = load_spec(_SPEC_ID, root=repo)
        loaded, error = load_or_produce_plan(spec, repo_root=repo)
        assert error is None
        assert loaded == plan

    def test_absent_plan_produced_by_injected_planner(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        from ferova.review.spec import load_spec

        spec = load_spec(_SPEC_ID, root=repo)

        class _FakePlanner:
            def plan(self, *, spec_id, spec_markdown, repo_tree):
                return plan, None, {"tool_calls": [], "turns": 1}

        loaded, error = load_or_produce_plan(spec, repo_root=repo, planner=_FakePlanner())
        assert error is None
        assert loaded == plan
        assert (repo / plan_relpath(_SPEC_ID)).is_file()

    def test_planning_failure_is_loud(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        from ferova.review.spec import load_spec

        spec = load_spec(_SPEC_ID, root=repo)

        class _FailingPlanner:
            def plan(self, *, spec_id, spec_markdown, repo_tree):
                return None, "no payload", {}

        loaded, error = load_or_produce_plan(spec, repo_root=repo, planner=_FailingPlanner())
        assert loaded is None
        assert error is not None
        assert "planning failed" in error


class TestExecutePlanStep:
    def test_execute_plan_step(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_writing([_good_attempt()])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is True
        assert outcome.fixes_applied == 2
        last_subject = _git(repo, "log", "-1", "--format=%s")
        assert last_subject == "feat(demo): add mini module"
        assert (repo / "src" / "mini.py").is_file()

    def test_out_of_contract_change_is_rejected(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_writing([[("src/other.py", _CLEAN_MODULE)]])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "outside its contract" in outcome.reason
        assert dev.develop_step.call_count == 1
        assert _git(repo, "log", "-1", "--format=%s") == "chore: init demo repo"

    def test_red_gate_retries_fix_forward_and_leaves_work_in_place(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        broken = [("src/mini.py", _BROKEN_MODULE), ("tests/unit/test_mini.py", _CLEAN_TEST)]
        dev = _developer_writing([broken, broken])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "failed after retry" in outcome.reason
        assert dev.develop_step.call_count == 2
        retry_brief = dev.develop_step.call_args_list[1].kwargs["brief"]
        assert "Previous attempt failed its gates" in retry_brief
        assert (repo / "src" / "mini.py").is_file()
        assert _git(repo, "log", "-1", "--format=%s") == "chore: init demo repo"

    def test_no_writes_twice_is_a_failure(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_writing([[], []])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "without writing any file" in outcome.reason

    def test_fix_forward_accumulates_across_attempts_without_revert(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            files=["src/a.py", "src/b.py", "tests/unit/test_ab.py"],
            unit_tests=["tests/unit/test_ab.py::test_ab"],
        )
        clean_a = '"""A."""\n\nA = 1\n'
        broken_b = '"""B."""\n\nB = undefined_name\n'
        clean_b = '"""B."""\n\nB = 2\n'
        test_ab = '"""Test ab."""\n\n\ndef test_ab() -> None:\n    assert True\n'
        dev = _developer_scripted(
            [
                [("src/a.py", clean_a), ("src/b.py", broken_b), ("tests/unit/test_ab.py", test_ab)],
                [("src/b.py", clean_b)],
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
        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert "src/a.py" in committed
        assert "src/b.py" in committed
        assert (repo / "src" / "a.py").read_text() == clean_a


class TestAgenticErrorPath:
    def test_loop_error_then_recover(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_scripted(
            [DevLoopResult(error="proxy agentic loop failed: chain exhausted"), _good_attempt()]
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
        assert "agentic loop failed" in retry_brief

    def test_persistent_loop_error_fails_without_third_attempt(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_scripted(
            [DevLoopResult(error="proxy agentic loop failed: chain exhausted")]
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert dev.develop_step.call_count == 2
        assert "failed after retry" in outcome.reason


class TestContractBackstop:
    def test_gate_introduced_out_of_contract_change_refused(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_scripted([_good_attempt()])

        def _ruff_that_escapes(repo_root, **kwargs):
            (repo_root / "src" / "escaped.py").write_text('"""E."""\n\nE = 1\n', encoding="utf-8")
            return True, ""

        monkeypatch.setattr("ferova.review.dev_runner.run_ruff_gate", _ruff_that_escapes)

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "outside the contract" in outcome.reason
        assert _git(repo, "log", "-1", "--format=%s") == "chore: init demo repo"


class TestSessionWrapup:
    def test_session_wrapup(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        assert result.plan_committed is True
        assert result.steps_total == 1
        assert result.steps_completed == 1
        assert result.failed_step_index is None
        assert result.pytest_passed is True
        assert result.no_op_reason == "dry-run: push=False"
        assert result.decomposed is False
        assert result.sub_spec_ids == []
        assert (repo / "docs" / "specs" / f"2026-06-07_{_SPEC_ID}_demo.md").is_file()
        subjects = _git(repo, "log", "--format=%s").splitlines()
        assert subjects[0] == "feat(demo): add mini module"
        assert subjects[1] == f"docs(plan): {_SPEC_ID} action plan"

    def test_failed_step_stops_session_and_keeps_plan_commit(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        broken = [("src/mini.py", _BROKEN_MODULE)]
        dev = _developer_writing([broken, broken])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
        )

        assert result.failed_step_index == 1
        assert result.steps_completed == 0
        assert result.pushed is False
        subjects = _git(repo, "log", "--format=%s").splitlines()
        assert subjects[0] == f"docs(plan): {_SPEC_ID} action plan"


class TestGateAndSessionEdges:
    def test_ruff_gate_failure_triggers_retry(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        lint_failing = [
            ("src/mini.py", _LINT_FAILING_MODULE),
            ("tests/unit/test_mini.py", _CLEAN_TEST),
        ]
        dev = _developer_writing([lint_failing, lint_failing, lint_failing])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert dev.develop_step.call_count == 3
        retry_brief = dev.develop_step.call_args_list[1].kwargs["brief"]
        assert "ruff gate" in retry_brief

    def test_session_push_failure(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(
            "ferova.review.dev_runner.push_branch",
            lambda *a, **kw: (False, "git push failed: timeout"),
        )
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=True,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        assert result.pushed is False
        assert result.steps_completed == 1
        assert result.no_op_reason is not None
        assert "push failed" in result.no_op_reason

    def test_invalid_committed_plan_returns_error(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        from ferova.review.spec import load_spec

        target = repo / plan_relpath(_SPEC_ID)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# not a plan\n\nno marker here\n", encoding="utf-8")
        spec = load_spec(_SPEC_ID, root=repo)

        loaded, error = load_or_produce_plan(spec, repo_root=repo)

        assert loaded is None
        assert error is not None
        assert "committed plan is invalid" in error

    def test_plan_commit_failure_stops_session(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(
            "ferova.review.dev_runner.commit_plan_document",
            lambda *a, **kw: (False, "permission denied"),
        )
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
        )

        assert result.plan_committed is False
        assert result.no_op_reason is not None
        assert "plan commit failed" in result.no_op_reason
        assert result.steps_total == 1
        dev.develop_step.assert_not_called()

    def test_flag_like_selector_refused_by_runner(self, tmp_path: Path) -> None:
        from ferova.review.dev_runner import run_pytest_selectors

        repo = _init_repo(tmp_path)
        ok, tail = run_pytest_selectors(repo, ["--pdb", "tests/unit"])
        assert ok is False
        assert "refused flag-like" in tail

    def test_run_pytest_selectors_scrubs_secret_env(self, tmp_path: Path, monkeypatch) -> None:
        import ferova.review.dev_runner as dr

        monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "live-secret")
        monkeypatch.setenv("FEROVA_DB_PATH", "data/x.db")
        captured: dict[str, object] = {}

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _capture(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _Proc()

        monkeypatch.setattr(dr.subprocess, "run", _capture)
        dr.run_pytest_selectors(tmp_path, ["tests/unit"])

        env = captured["env"]
        assert env is not None
        assert "FEROVA_OPENROUTER_API_KEY" not in env
        assert env.get("FEROVA_DB_PATH") == "data/x.db"

    def test_omitted_in_contract_promise_is_retried_and_heals(self, tmp_path: Path) -> None:
        """An unwritten promised test inside the contract retries like any gate.

        SP-USAGE-REASONING-SPLIT dispatch 1 was finalized mid-thought one
        write_file short of green; the old unconditional stop threw the
        attempt away even though the step's own contract could create the
        missing file.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        dev = _developer_writing(
            [
                [("src/mini.py", _CLEAN_MODULE)],
                [("tests/unit/test_mini.py", _CLEAN_TEST)],
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

    def test_promise_outside_the_contract_stops_without_retry(self, tmp_path: Path) -> None:
        """A promise only an EARLIER step could create is terminal when absent."""
        repo = _init_repo(tmp_path)
        creator = PlanStep(
            index=1,
            title="Create the shared test",
            files=["tests/unit/test_mini.py", "tests/integration/test_demo_flow.py"],
            action="Create the shared test file.",
            commit_message="test(demo): shared test",
            done_when="pytest tests/unit/test_mini.py is green",
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        promiser = PlanStep(
            index=2,
            title="Use the shared test",
            files=["src/mini.py"],
            action="Create the module.",
            commit_message="feat(demo): add mini module",
            done_when="pytest tests/unit/test_mini.py is green",
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        plan = ActionPlan(
            spec_id=_SPEC_ID,
            title="Demo plan",
            summary="Two-step demo.",
            steps=[creator, promiser],
            integration_tests=["tests/integration/test_demo_flow.py"],
        )
        dev = _developer_writing([[("src/mini.py", _CLEAN_MODULE)]])

        outcome = execute_plan_step(
            plan.steps[1],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "promises tests that do not exist" in outcome.reason
        assert dev.develop_step.call_count == 1
        assert (repo / "src" / "mini.py").is_file()


class TestSelfVerifyGate:
    def test_failed_self_verify_blocks_push(self, tmp_path: Path, monkeypatch) -> None:
        import types

        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(
            "ferova.review.dev_runner.run_self_verify",
            lambda *a, **k: types.SimpleNamespace(ok=False, reasons=["judge: not compliant — gap"]),
        )
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=True,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        assert result.self_verified is False
        assert result.pushed is False
        assert result.no_op_reason and "self-verify" in result.no_op_reason

    def test_passing_self_verify_sets_flag(self, tmp_path: Path, monkeypatch) -> None:
        import types

        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(
            "ferova.review.dev_runner.run_self_verify",
            lambda *a, **k: types.SimpleNamespace(ok=True, reasons=[]),
        )
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        assert result.self_verified is True
        assert result.no_op_reason == "dry-run: push=False"

    def test_real_gate_compliant_judge_passes(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        _git(repo, "branch", "develop")
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(
            "ferova.review.dev_runner.push_branch", lambda *a, **kw: (True, "pushed")
        )
        calls: list[str] = []

        def _judge(prompt: str) -> str:
            calls.append(prompt)
            return '{"compliant": true, "reasons": "ok", "gaps": []}'

        dev = _developer_writing([_good_attempt()])
        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=True,
            db_path=repo.parent / "test.db",
            judge=_judge,
        )

        assert calls, "the real gate must actually invoke the judge"
        assert result.self_verified is True
        assert result.pushed is True

    def test_real_gate_noncompliant_judge_blocks(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        _git(repo, "branch", "develop")
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        pushed = {"called": False}

        def _push(*a, **kw):
            pushed["called"] = True
            return True, "pushed"

        monkeypatch.setattr("ferova.review.dev_runner.push_branch", _push)
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=True,
            db_path=repo.parent / "test.db",
            judge=lambda p: '{"compliant": false, "reasons": "G1 missing", "gaps": ["G1"]}',
        )

        assert result.self_verified is False
        assert result.pushed is False
        assert result.no_op_reason and "self-verify" in result.no_op_reason
        assert pushed["called"] is False


class TestDecomposeWiring:
    _GOVERNED_PARENT = (
        "---\n"
        "id: SP-MULTI\n"
        "title: Multi-owns parent\n"
        "version: 0.1\n"
        "status: draft\n"
        "author: agent\n"
        "\n"
        "owns:\n"
        "  code:\n"
        "    - src/multi_a.py\n"
        "    - src/multi_b.py\n"
        "  resources: []\n"
        "\n"
        "depends_on: []\n"
        "provides_to: []\n"
        "constraints: {}\n"
        "---\n"
        "# SP-MULTI — parent\n\n## Why\n\nTwo features.\n"
    )

    def _init_governed_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "docs" / "specs").mkdir(parents=True)
        (repo / "docs" / "specs" / "2026-06-28_SP-MULTI_demo.md").write_text(
            self._GOVERNED_PARENT, encoding="utf-8"
        )
        (repo / ".gitignore").write_text(
            "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
        )
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "T")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: init")
        init_schema(repo.parent / "test.db")
        return repo

    def test_governed_multi_owns_spec_decomposes_and_develops_each(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import json

        import ferova.review.dev_runner as dr

        repo = self._init_governed_repo(tmp_path)
        proposal = json.dumps(
            {
                "sub_specs": [
                    {
                        "id": "SP-MULTI-1",
                        "title": "A",
                        "summary": "a",
                        "owns_code": ["src/multi_a.py"],
                        "owns_resources": [],
                        "depends_on": [],
                        "body": "## Goals\n- a\n",
                    },
                    {
                        "id": "SP-MULTI-2",
                        "title": "B",
                        "summary": "b",
                        "owns_code": ["src/multi_b.py"],
                        "owns_resources": [],
                        "depends_on": ["SP-MULTI-1"],
                        "body": "## Goals\n- b\n",
                    },
                ]
            }
        )
        monkeypatch.setattr(dr, "ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(dr, "push_branch", lambda *a, **kw: (True, "pushed"))
        developed: list[str] = []

        def _fake_develop_one(spec, **kwargs):
            developed.append(spec.id)
            return None

        monkeypatch.setattr(dr, "_develop_one_spec", _fake_develop_one)

        result = run_developer_session(
            "SP-MULTI",
            repo_root=repo,
            developer=MagicMock(),
            push=True,
            db_path=repo.parent / "test.db",
            decomposer=lambda prompt: proposal,
            judge=_compliant_judge,
        )

        assert developed == ["SP-MULTI-1", "SP-MULTI-2"]
        assert result.pushed is True
        assert result.decomposed is True
        assert result.sub_spec_ids == ["SP-MULTI-1", "SP-MULTI-2"]
        subjects = _git(repo, "log", "--format=%s")
        assert "docs(decompose): split SP-MULTI into 2 sub-specs (supersedes SP-MULTI)" in subjects
        assert (repo / "docs" / "specs" / "2026-06-28_SP-MULTI-1_sub.md").is_file()
        assert (repo / "docs" / "specs" / "2026-06-28_SP-MULTI-2_sub.md").is_file()

        parent = repo / "docs" / "specs" / "2026-06-28_SP-MULTI_demo.md"
        assert not parent.exists()
        assert "2026-06-28_SP-MULTI_demo.md" not in _git(repo, "ls-files")
        head_tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
        assert "2026-06-28_SP-MULTI_demo.md" not in head_tree
        assert "2026-06-28_SP-MULTI-1_sub.md" in head_tree
        assert "2026-06-28_SP-MULTI-2_sub.md" in head_tree

        from ferova.arch import load_registry

        registry = load_registry(repo / "docs" / "specs")
        assert registry.disjointness_violations() == []

    def test_real_multi_sub_spec_development_in_order(self, tmp_path: Path, monkeypatch) -> None:
        import json

        import ferova.review.dev_runner as dr

        repo = self._init_governed_repo(tmp_path)
        _git(repo, "branch", "develop")
        proposal = json.dumps(
            {
                "sub_specs": [
                    {
                        "id": "SP-MULTI-2",
                        "title": "B",
                        "summary": "b",
                        "owns_code": ["src/multi_b.py"],
                        "owns_resources": [],
                        "depends_on": ["SP-MULTI-1"],
                        "body": "## Goals\n- b\n",
                    },
                    {
                        "id": "SP-MULTI-1",
                        "title": "A",
                        "summary": "a",
                        "owns_code": ["src/multi_a.py"],
                        "owns_resources": [],
                        "depends_on": [],
                        "body": "## Goals\n- a\n",
                    },
                ]
            }
        )

        class _PerSpecPlanner:
            def plan(self, *, spec_id, spec_markdown, repo_tree):
                suffix = spec_id.rsplit("-", 1)[-1]
                code = f"src/multi_{'a' if suffix == '1' else 'b'}.py"
                test = f"tests/unit/test_multi_{suffix}.py"
                integration = f"tests/integration/test_int_{suffix}.py"
                plan = ActionPlan(
                    spec_id=spec_id,
                    title=f"plan {spec_id}",
                    summary="s",
                    steps=[
                        PlanStep(
                            index=1,
                            title="impl",
                            files=[code, test, integration],
                            action="create the module and its test",
                            commit_message=f"feat: {spec_id}",
                            done_when="green",
                            unit_tests=[f"{test}::test_it"],
                        )
                    ],
                    integration_tests=[f"{integration}::test_it"],
                )
                return plan, None, {}

        def _contract_dev() -> MagicMock:
            dev = MagicMock()

            def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
                for rel in allowed_paths:
                    path = Path(repo_root) / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if "test" in path.name:
                        path.write_text(
                            '"""t."""\n\n\ndef test_it() -> None:\n    assert True\n',
                            encoding="utf-8",
                        )
                    else:
                        path.write_text('"""m."""\n\nVALUE = 1\n', encoding="utf-8")
                return DevLoopResult(text="ok", model_used="fake", turns=1, tokens_used=1)

            dev.develop_step.side_effect = _step
            return dev

        monkeypatch.setattr(dr, "ensure_branch", lambda *a, **kw: True)
        monkeypatch.setattr(dr, "push_branch", lambda *a, **kw: (True, "pushed"))

        result = run_developer_session(
            "SP-MULTI",
            repo_root=repo,
            developer=_contract_dev(),
            push=True,
            db_path=repo.parent / "test.db",
            decomposer=lambda prompt: proposal,
            judge=_compliant_judge,
            planner=_PerSpecPlanner(),
        )

        assert result.steps_total == 2
        assert result.steps_completed == 2
        assert result.self_verified is True
        assert result.pushed is True
        subjects = _git(repo, "log", "--format=%s")
        assert "feat: SP-MULTI-1" in subjects
        assert "feat: SP-MULTI-2" in subjects
        a_pos = subjects.index("feat: SP-MULTI-1")
        b_pos = subjects.index("feat: SP-MULTI-2")
        assert a_pos > b_pos

    def test_failed_decompose_stops_session(self, tmp_path: Path, monkeypatch) -> None:
        import ferova.review.dev_runner as dr

        repo = self._init_governed_repo(tmp_path)
        monkeypatch.setattr(dr, "ensure_branch", lambda *a, **kw: True)
        called = {"n": 0}

        def _fake_develop_one(spec, **kwargs):
            called["n"] += 1
            return None

        monkeypatch.setattr(dr, "_develop_one_spec", _fake_develop_one)

        result = run_developer_session(
            "SP-MULTI",
            repo_root=repo,
            developer=MagicMock(),
            push=False,
            db_path=repo.parent / "test.db",
            decomposer=lambda prompt: "not a valid proposal",
            judge=_compliant_judge,
        )

        assert called["n"] == 0
        assert result.pushed is False
        assert result.no_op_reason and "decompose failed" in result.no_op_reason


class TestDevSessionResultFields:
    def test_dev_session_result_fields(self) -> None:
        result = DevSessionResult(spec_id="SP-X")
        assert result.steps_total == 0
        assert result.steps_completed == 0
        assert result.failed_step_index is None
        assert result.plan_committed is False


class TestStepBrief:
    def test_brief_carries_contract_and_done_when(self) -> None:
        plan = _one_step_plan()
        brief = build_step_brief(plan, plan.steps[0])
        assert "step 1/1" in brief
        assert "`src/mini.py`" in brief
        assert "pytest tests/unit/test_mini.py is green" in brief
        assert "Previous attempt" not in brief


class TestPreExistingWorktreeFiles:
    def test_execute_plan_step_ignores_pre_existing_paths(self, tmp_path: Path) -> None:
        """Foreign files present at session start neither escape nor get committed.

        Attempt-1 of the SP-FINDINGS-BRIDGE-DOCFIX dogfood: an
        uncommitted spec draft and a stray lockfile were attributed to
        the step and the whole session was refused; git add -A would
        then have swept them into the step commit.
        """
        repo = _init_repo(tmp_path)
        (repo / "stray.lock").write_text("foreign\n", encoding="utf-8")
        plan = _one_step_plan()
        dev = _developer_writing([_good_attempt()])

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
            pre_existing=frozenset({"stray.lock"}),
        )

        assert outcome.ok is True
        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert "stray.lock" not in committed
        assert "?? stray.lock" in _git(repo, "status", "--porcelain")


class TestCommitPaths:
    def test_stages_only_the_named_paths(self, tmp_path: Path) -> None:
        """The step commit carries exactly its own files, nothing foreign."""
        repo = _init_repo(tmp_path)
        (repo / "wanted.txt").write_text("in\n", encoding="utf-8")
        (repo / "foreign.txt").write_text("out\n", encoding="utf-8")
        ok, _ = commit_paths(repo, ["wanted.txt"], "chore: targeted commit")
        assert ok is True
        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert committed == ["wanted.txt"]
        assert "?? foreign.txt" in _git(repo, "status", "--porcelain")

    def test_empty_paths_is_nothing_to_commit(self, tmp_path: Path) -> None:
        """An empty path list refuses without touching git."""
        repo = _init_repo(tmp_path)
        ok, detail = commit_paths(repo, [], "chore: empty")
        assert ok is False
        assert detail == "nothing to commit"

    def test_missing_path_reports_the_git_add_failure(self, tmp_path: Path) -> None:
        """A nonexistent path surfaces the git add error verbatim."""
        repo = _init_repo(tmp_path)
        ok, detail = commit_paths(repo, ["does-not-exist.txt"], "chore: broken")
        assert ok is False
        assert "git add failed" in detail


class TestStepResume:
    def test_committed_step_subject_is_detected(self, tmp_path: Path) -> None:
        """A commit whose subject matches the step's message marks it done."""
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        step = plan.steps[0]
        assert _step_already_committed(repo, step) is False
        (repo / "seed.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", step.commit_message)
        assert _step_already_committed(repo, step) is True

    def test_subject_detected_beyond_the_git_output_tail_cap(self, tmp_path: Path) -> None:
        """The newest subject survives a long branch log.

        _run_git's 400-char tail cap (meant for error tails fed to
        models) beheaded the newest subjects once a session branch grew
        past ~7 commits: SP-AGENT-THINKING-CONTROL re-paid a 683k-token
        dispatch for a step whose commit sat at the top of the log
        (2026-07-04). The subject list must be read uncapped.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        step = plan.steps[0]
        for index in range(8):
            (repo / f"filler_{index}.txt").write_text("x\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(
                repo,
                "commit",
                "-m",
                f"chore(filler-{index}): a deliberately verbose subject line padding "
                "the log well past the four-hundred-character tail cap",
            )
        (repo / "seed.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", step.commit_message)
        assert _step_already_committed(repo, step) is True


class TestStepPreflightPredicate:
    _FAILING_TEST = '"""Demo test."""\n\n\ndef test_value() -> None:\n    assert 1 == 2\n'

    def test_preflight_predicate_returns_false_when_a_contract_file_is_missing(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        step = plan.steps[0]
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")

        assert step_preflight_complete(repo, plan, step) is False

    def test_preflight_predicate_returns_false_on_empty_selectors(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(files=["docs/note.md"], unit_tests=[], integration_tests=[])
        step = plan.steps[0]
        (repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")

        assert step_preflight_complete(repo, plan, step) is False

    def test_preflight_predicate_returns_true_when_files_and_tests_green(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            _GREEN_INTEGRATION, encoding="utf-8"
        )

        assert step_preflight_complete(repo, plan, step) is True

    def test_preflight_predicate_returns_false_when_promised_test_fails(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(self._FAILING_TEST, encoding="utf-8")

        assert step_preflight_complete(repo, plan, step) is False

    def test_preflight_groups_selectors_per_test_tree(self, tmp_path: Path) -> None:
        """Same-basename unit and integration promises preflight green.

        A single combined pytest invocation dies on the rootdir
        module-name collision when tests/unit and tests/integration
        promise the same basename — SP-AGENT-THINKING-CONTROL step 4's
        fully-delivered work preflighted red for that reason alone
        (2026-07-04). Selectors run per tree, mirroring CI.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            files=["src/mini.py", "tests/unit/test_same_name.py"],
            unit_tests=["tests/unit/test_same_name.py::test_value"],
            integration_tests=["tests/integration/test_same_name.py::test_value"],
        )
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_same_name.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_same_name.py").write_text(
            _CLEAN_TEST, encoding="utf-8"
        )

        assert step_preflight_complete(repo, plan, step) is True

    def test_preflight_rejects_missing_node_id_masked_by_a_bare_file(self, tmp_path: Path) -> None:
        """A bare-file promise must not mask absent promised node ids.

        pytest exits 0 when a missing ``file::node`` selector shares an
        invocation with the same file listed bare — that masked
        SP-BUDGET-RETRY-FIXES step 3 (three promised tests certified
        complete while none existed, 2026-07-04). Node ids are resolved
        statically before pytest runs.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            files=["src/mini.py", "tests/unit/test_mini.py"],
            unit_tests=["tests/unit/test_mini.py::test_not_written_yet"],
            integration_tests=["tests/integration/test_mini.py::test_value"],
        )
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")

        assert step_preflight_complete(repo, plan, step) is False

    def test_preflight_predicate_rejects_a_reconciled_green(self, tmp_path: Path) -> None:
        """Unrelated green tests in the promised file are not completion proof.

        The file-level reconciliation fallback certified two untouched
        SP-AGENT-THINKING-CONTROL steps as complete because an earlier
        step's schema tests in the same promised file were green
        (2026-07-04): the promised selectors themselves did not exist.
        Preflight demands a strict per-selector green.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_selector_that_does_not_exist"]
        )
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            _GREEN_INTEGRATION, encoding="utf-8"
        )

        assert step_preflight_complete(repo, plan, step) is False

    def test_preflight_predicate_attributes_integration_selectors_by_file(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            files=[
                "src/mini.py",
                "tests/unit/test_mini.py",
                "tests/integration/test_demo_flow.py",
            ],
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        step = plan.steps[0]
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        red_integration = (
            '"""Integration test."""\n\n\ndef test_flow() -> None:\n    assert False\n'
        )
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            red_integration, encoding="utf-8"
        )

        assert step_preflight_complete(repo, plan, step) is False

        green_integration = (
            '"""Integration test."""\n\n\ndef test_flow() -> None:\n    assert True\n'
        )
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            green_integration, encoding="utf-8"
        )

        assert step_preflight_complete(repo, plan, step) is True

        unrelated_plan = _one_step_plan(files=["docs/note.md"], unit_tests=[], integration_tests=[])
        unrelated_step = unrelated_plan.steps[0]
        (repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")

        assert step_preflight_complete(repo, unrelated_plan, unrelated_step) is False


class TestSessionPreflight:
    """SP-DEV-STEP-PREFLIGHT — wiring the predicate into the session loop."""

    def test_preflight_completes_a_green_step_for_zero_tokens(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        _git(repo, "branch", "develop")
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            _GREEN_INTEGRATION, encoding="utf-8"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: pre-seed mini module")
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        assert result.steps_completed == 1
        dev.develop_step.assert_not_called()

        from sqlalchemy import create_engine, text

        engine = create_engine(f"sqlite:///{repo.parent / 'test.db'}")
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT model_used, tokens_used FROM pr_coder_responses")
            ).fetchall()
        assert any(row[0] == "preflight" and row[1] == 0 for row in rows)

    def test_preflight_dispatches_when_a_contract_file_is_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: pre-seed test only")
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        dev.develop_step.assert_called()
        assert result.steps_completed == 1

    def test_preflight_commits_uncommitted_green_work(self, tmp_path: Path, monkeypatch) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan()
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            _GREEN_INTEGRATION, encoding="utf-8"
        )
        dev = _developer_writing([_good_attempt()])

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        dev.develop_step.assert_not_called()
        assert result.steps_completed == 1
        subjects = _git(repo, "log", "--format=%s").splitlines()
        assert "feat(demo): add mini module" in subjects

    def test_preflight_attributes_integration_selectors_by_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            files=[
                "src/mini.py",
                "tests/unit/test_mini.py",
                "tests/integration/test_demo_flow.py",
            ],
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        _seed_plan(repo, plan)
        monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
        (repo / "src" / "mini.py").write_text(_CLEAN_MODULE, encoding="utf-8")
        (repo / "tests" / "unit" / "test_mini.py").write_text(_CLEAN_TEST, encoding="utf-8")
        (repo / "tests" / "integration").mkdir(parents=True, exist_ok=True)
        red_integration = (
            '"""Integration test."""\n\n\ndef test_flow() -> None:\n    assert False\n'
        )
        (repo / "tests" / "integration" / "test_demo_flow.py").write_text(
            red_integration, encoding="utf-8"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: pre-seed red integration")

        green_integration = (
            '"""Integration test."""\n\n\ndef test_flow() -> None:\n    assert True\n'
        )

        def _fix_integration(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
            (Path(repo_root) / "tests" / "integration" / "test_demo_flow.py").write_text(
                green_integration, encoding="utf-8"
            )
            return DevLoopResult(text="fixed", model_used="fake", turns=1, tokens_used=1)

        dev = MagicMock()
        dev.develop_step.side_effect = _fix_integration

        result = run_developer_session(
            _SPEC_ID,
            repo_root=repo,
            developer=dev,
            push=False,
            db_path=repo.parent / "test.db",
            judge=_compliant_judge,
        )

        dev.develop_step.assert_called()
        assert result.steps_completed == 1


class TestPromisedTestGateG1G2:
    """SP-DEV-PROMISE-DELIVERY — G1 (untouched-file refusal) and G2 (mechanical rename)."""

    _DRIFTED_TEST = '"""Demo test."""\n\n\ndef test_drifted() -> None:\n    assert 1 == 1\n'
    _PROMISED_TEST = '"""Demo test."""\n\n\ndef test_value() -> None:\n    assert 1 == 1\n'
    _TWO_CANDIDATES = (
        '"""Demo test."""\n\n'
        "def test_x() -> None:\n    assert 1 == 1\n\n"
        "def test_y() -> None:\n    assert 1 == 1\n"
    )

    def test_untouched_promised_file_reconciliation_is_retried(self, tmp_path: Path) -> None:
        """AC1 — two dispatches: first writes only source, second writes the tests."""
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        (repo / "tests" / "unit" / "test_mini.py").write_text(self._DRIFTED_TEST, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "chore: pre-seed drifted test")
        dev = _developer_writing(
            [
                [("src/mini.py", _CLEAN_MODULE)],
                [("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", self._PROMISED_TEST)],
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
        assert (repo / "tests" / "unit" / "test_mini.py").read_text() == self._PROMISED_TEST

    def test_touched_file_with_drifted_name_is_renamed_to_promise(self, tmp_path: Path) -> None:
        """AC2 — single drifted test in a touched file, step green in one attempt."""
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=["tests/unit/test_mini.py::test_value"],
        )
        dev = _developer_writing(
            [[("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", self._DRIFTED_TEST)]]
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

    def test_ambiguous_drift_absent_names_refused(self, tmp_path: Path) -> None:
        """SP-DEV-PROMISE-TRAILING-NAME — two missing + two candidates, step refused.

        Was SP-DEV-PROMISE-DELIVERY AC3 (reconciled-accept). The trailing-name
        unification reverses that: a fan-out whose promised trailing names are
        absent from the touched file is refused at the step gate with feedback
        naming the absent selectors, so the session self-corrects in-loop instead
        of dying terminally at self-verify.
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(
            unit_tests=[
                "tests/unit/test_mini.py::test_a",
                "tests/unit/test_mini.py::test_b",
            ],
        )
        dev = _developer_writing(
            [[("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", self._TWO_CANDIDATES)]]
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert dev.develop_step.call_count >= 2
        retry_brief = dev.develop_step.call_args_list[1].kwargs["brief"]
        assert "tests/unit/test_mini.py::test_a" in retry_brief
        assert "tests/unit/test_mini.py::test_b" in retry_brief

    def test_red_rename_rerun_restores_the_drifted_file(self, tmp_path: Path) -> None:
        """A rename whose strict re-run stays red is rolled back before retry.

        The first implementation set the retry feedback but left the
        mechanically-renamed red file on disk — the semantic judge
        refused the push over exactly this (2026-07-05). File A's
        one-to-one drift renames; file B's promised selector stays
        missing (ambiguous), so the strict re-run is red and A must
        come back to its delivered content.
        """
        repo = _init_repo(tmp_path)
        drifted_a = '"""A."""\n\n\ndef test_drifted(self=None):\n    assert True\n'
        two_candidates_b = (
            '"""B."""\n\n\ndef test_x():\n    assert True\n\n\ndef test_y():\n    assert True\n'
        )
        plan = _one_step_plan(
            files=["src/mini.py", "tests/unit/test_mini.py", "tests/unit/test_mini_b.py"],
            unit_tests=[
                "tests/unit/test_mini.py::test_value",
                "tests/unit/test_mini_b.py::test_promised_b",
            ],
        )
        dev = _developer_writing(
            [
                [
                    ("src/mini.py", _CLEAN_MODULE),
                    ("tests/unit/test_mini.py", drifted_a),
                    ("tests/unit/test_mini_b.py", two_candidates_b),
                ]
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

        assert outcome.ok is False
        assert "did not make the promised selectors green" in outcome.reason
        restored = (repo / "tests" / "unit" / "test_mini.py").read_text(encoding="utf-8")
        assert "def test_drifted(" in restored
        assert "def test_value(" not in restored

    def test_rename_error_falls_back_to_retryable_gate(self, tmp_path: Path, monkeypatch) -> None:
        """A failed mechanical rename retries with the G1 feedback.

        The first implementation folded the error outcome into the
        ambiguous one and proceeded via reconciled-accept — the second
        judge gap (2026-07-05).
        """
        repo = _init_repo(tmp_path)
        plan = _one_step_plan(unit_tests=["tests/unit/test_mini.py::test_value"])
        dev = _developer_writing(
            [[("src/mini.py", _CLEAN_MODULE), ("tests/unit/test_mini.py", self._DRIFTED_TEST)]]
        )
        monkeypatch.setattr(
            "ferova.review.dev_runner._attempt_mechanical_rename",
            lambda *args, **kwargs: ("error", ""),
        )

        outcome = execute_plan_step(
            plan.steps[0],
            plan=plan,
            repo_root=repo,
            developer=dev,
            repo_tree="src/",
            db=repo.parent / "test.db",
        )

        assert outcome.ok is False
        assert "mechanical rename failed" in outcome.reason
        assert dev.develop_step.call_count >= 2


def test_preflight_integration_test_file_exists() -> None:
    """Guard the preflight integration test against accidental deletion."""
    target = Path(__file__).parents[2] / "tests" / "integration" / "test_dev_runner_preflight.py"
    assert target.is_file()
    assert "def test_preflight_skip_path_end_to_end" in target.read_text(encoding="utf-8")
