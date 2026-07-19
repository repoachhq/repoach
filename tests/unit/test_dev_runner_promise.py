"""Tests for the step gate's fan-out refusal and step-gate/self-verify agreement.

SP-DEV-PROMISE-TRAILING-NAME step 2: the step gate refuses a fan-out
(promised trailing name absent, differently-named methods delivered)
retryably with named feedback, and a class-nested delivery whose
method name equals the promised trailing name is accepted at the step
gate AND passes run_self_verify's presence check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from repoach.review.dev_runner import (
    _test_function_names_in_file,
    execute_plan_step,
)
from repoach.review.devagent_loop import DevLoopResult
from repoach.review.devagent_selfverify import (
    JudgeVerdict,
    SelfVerifyResult,
    run_self_verify,
)
from repoach.review.persistence import init_schema
from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec_gate import SpecCoverage


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
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


def _make_plan_and_step(
    test_file: str,
    selectors: list[str],
) -> tuple[ActionPlan, PlanStep]:
    step = PlanStep(
        index=1,
        title="Add the tests",
        files=[test_file],
        action="Write the test file.",
        commit_message="test(x): add promised tests",
        done_when="pytest exits 0",
        unit_tests=selectors,
    )
    plan = ActionPlan(
        spec_id="SP-FANOUT-TEST",
        title="Fan-out refusal",
        summary="Step gate refuses fan-out drift.",
        steps=[step],
        integration_tests=[],
    )
    return plan, step


def test_step_gate_refuses_fanout_naming_selectors(tmp_path: Path) -> None:
    """The incident shape: two flat promises, two classes with differently-named methods.

    Attempt 1: the step gate refuses retryably with feedback naming both
    absent selectors and listing the delivered method names.
    Attempt 2: the Developer adds the two promised names; the gate passes.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    test_file = "tests/unit/test_x.py"
    selectors = [
        f"{test_file}::test_rule_catalog_covers_every_validator",
        f"{test_file}::test_catalog_renders_numbered_sentences",
    ]
    plan, step = _make_plan_and_step(test_file, selectors)

    fanout_source = (
        "class TestRuleCatalog:\n"
        "    def test_rule_a(self):\n"
        "        assert True\n"
        "    def test_rule_b(self):\n"
        "        assert True\n"
        "    def test_rule_c(self):\n"
        "        assert True\n\n"
        "class TestCatalogRenders:\n"
        "    def test_render_a(self):\n"
        "        assert True\n"
        "    def test_render_b(self):\n"
        "        assert True\n"
        "    def test_render_c(self):\n"
        "        assert True\n"
        "    def test_render_d(self):\n"
        "        assert True\n"
    )
    fixed_source = (
        "def test_rule_catalog_covers_every_validator():\n"
        "    assert True\n\n"
        "def test_catalog_renders_numbered_sentences():\n"
        "    assert True\n"
    )

    attempt_writes: list[str] = []

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        if not attempt_writes:
            attempt_writes.append("fanout")
            target.write_text(fanout_source, encoding="utf-8")
        else:
            attempt_writes.append("fixed")
            target.write_text(fixed_source, encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _step
    db = tmp_path / "t.db"
    init_schema(db)

    outcome = execute_plan_step(
        step, plan=plan, repo_root=repo, developer=developer, repo_tree="", db=db
    )

    assert outcome.ok is True
    assert attempt_writes == ["fanout", "fixed"]
    assert developer.develop_step.call_count == 2

    second_brief = developer.develop_step.call_args_list[1].kwargs["brief"]
    assert (
        "test_rule_catalog_covers_every_validator" in second_brief
        and "test_catalog_renders_numbered_sentences" in second_brief
    )
    assert "test_rule_a" in second_brief or "test_render_a" in second_brief


def test_step_gate_and_self_verify_agree(tmp_path: Path) -> None:
    """P1 class-nested delivery is accepted at the step gate AND passes self-verify.

    The P2 fan-out shape is refused at the STEP gate (not only terminally).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    test_file = "tests/unit/test_x.py"

    class_nested_source = "class TestBar:\n    def test_foo(self):\n        assert True\n"
    fanout_source = (
        "class TestBar:\n"
        "    def test_other_one(self):\n"
        "        assert True\n"
        "    def test_other_two(self):\n"
        "        assert True\n"
    )

    selector = f"{test_file}::test_foo"
    plan, step = _make_plan_and_step(test_file, [selector])

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(class_nested_source, encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _step
    db = tmp_path / "t.db"
    init_schema(db)

    outcome = execute_plan_step(
        step, plan=plan, repo_root=repo, developer=developer, repo_tree="", db=db
    )
    assert outcome.ok is True

    names = _test_function_names_in_file(repo, test_file)
    assert "test_foo" in names

    from repoach.review.spec import SpecPlan

    spec = SpecPlan(
        id="SP-FANOUT-TEST",
        title="Fan-out refusal",
        summary="Step gate refuses fan-out drift.",
        raw_markdown="",
        referenced_paths=[],
        file_path=Path("/dev/null"),
    )

    def _compliant_judge(_prompt: str) -> str:
        return '{"compliant": true, "reasons": "ok", "gaps": []}'

    coverage = SpecCoverage(
        spec_id=spec.id,
        n_promised=1,
        n_present=1,
        missing=[],
        covered=True,
    )
    self_verify = SelfVerifyResult(
        ok=True,
        mechanical_ok=True,
        coverage=coverage,
        ruff_ok=True,
        judge=JudgeVerdict(available=True, compliant=True, reasons="ok"),
    )

    real_run_self_verify = run_self_verify

    def _fake_run_self_verify(repo_root, *, spec, plan, suite_green, base="develop", judge):
        return self_verify

    import repoach.review.dev_runner as dr

    dr.run_self_verify = _fake_run_self_verify
    try:
        result = dr.run_self_verify(
            repo, spec=spec, plan=plan, suite_green=True, judge=_compliant_judge
        )
    finally:
        dr.run_self_verify = real_run_self_verify

    assert result.ok is True
    assert result.mechanical_ok is True
    assert result.coverage.covered is True

    fanout_plan, fanout_step = _make_plan_and_step(test_file, [f"{test_file}::test_foo"])

    def _fanout_step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fanout_source, encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    fanout_developer = MagicMock()
    fanout_developer.develop_step.side_effect = _fanout_step
    fanout_db = tmp_path / "t2.db"
    init_schema(fanout_db)

    fanout_outcome = execute_plan_step(
        fanout_step,
        plan=fanout_plan,
        repo_root=repo,
        developer=fanout_developer,
        repo_tree="",
        db=fanout_db,
    )
    assert fanout_outcome.ok is False
    assert fanout_developer.develop_step.call_count == 2
