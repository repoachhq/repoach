"""SP-DEV-PROMISE-TRAILING-NAME step 3 \u2014 end-to-end fan-out reconcile integration.

Drives the reconcile + step-gate + self-verify path in a throwaway git
repo with a truthful scripted fake Developer loop. Attempt 1 delivers
the fan-out (two classes with differently-named methods) and the step
gate refuses in-loop with feedback naming both absent selectors.
Attempt 2 adds the two promised names and the step passes; a
subsequent :func:`run_self_verify` with a truthful boundary-fake
``gate_judge`` finds no missing promised units.

Hermetic: no network, no real LLM, no ``.env`` reliance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.dev_runner import execute_plan_step
from ferova.review.devagent_loop import DevLoopResult
from ferova.review.devagent_selfverify import run_self_verify
from ferova.review.persistence import init_schema
from ferova.review.plan import ActionPlan, PlanStep
from ferova.review.spec import SpecPlan

_SPEC_ID = "SP-FANOUT-INT"

_FLOW_TEST = '"""Demo integration test."""\n\n\ndef test_flow() -> None:\n    assert 1 == 1\n'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a temp git repo with a seed commit and the spec doc."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "integration").mkdir(parents=True)
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "tests" / "integration" / "test_demo_flow.py").write_text(_FLOW_TEST, encoding="utf-8")
    (repo / "docs" / "specs" / f"2026-07-10_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} \u2014 fan-out reconcile\n\n## Why\n\nIntegration test.\n\n## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("fan-out reconcile demo\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init demo repo")
    _git(repo, "checkout", "-q", "-b", "develop")
    _git(repo, "checkout", "-q", "-b", "feat/sp-fanout-int-impl")
    init_schema(repo.parent / "test.db")
    return repo


def _one_step_plan(test_file: str, selectors: list[str]) -> ActionPlan:
    """Return a one-step plan whose step promises the given flat selectors."""
    step = PlanStep(
        index=1,
        title="Add the promised tests",
        files=[test_file, "tests/integration/test_demo_flow.py"],
        action="Write the test file with the promised test names.",
        commit_message="test(x): add promised tests",
        done_when="pytest exits 0",
        unit_tests=selectors,
    )
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Fan-out reconcile demo",
        summary="One-step fan-out reconcile demo.",
        steps=[step],
        integration_tests=["tests/integration/test_demo_flow.py::test_flow"],
    )


def _scripted_developer(test_file: str, attempts: list[str]) -> MagicMock:
    """Fake Developer that writes the test file per attempt.

    Mirrors the ``_ScriptedLoop`` pattern from the promise-delivery
    integration tests: each entry in *attempts* is the full file
    content written on that attempt; the last entry repeats if the
    runner calls more times than supplied.
    """
    state = {"i": 0}

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        idx = min(state["i"], len(attempts) - 1)
        target = Path(repo_root) / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(attempts[idx], encoding="utf-8")
        state["i"] += 1
        return DevLoopResult(text="fake summary", model_used="fake", turns=1, tokens_used=1)

    dev = MagicMock()
    dev.develop_step.side_effect = _step
    return dev


class TestFanoutReconcileIntegration:
    """End-to-end fan-out reconcile + step-gate + self-verify path."""

    def test_fanout_drift_refused_in_loop_then_self_corrects(self, tmp_path: Path) -> None:
        """The incident shape: two flat promises, two classes with differently-named methods.

        Attempt 1: the step gate refuses retryably with feedback naming
        both absent selectors and listing the delivered method names.
        Attempt 2: the Developer adds the two promised names; the gate
        passes and a subsequent ``run_self_verify`` (with a truthful
        boundary-fake ``gate_judge``) finds no missing promised units.
        """
        repo = _init_repo(tmp_path)
        test_file = "tests/unit/test_x.py"
        selectors = [
            f"{test_file}::test_rule_catalog_covers_every_validator",
            f"{test_file}::test_catalog_renders_numbered_sentences",
        ]
        plan = _one_step_plan(test_file, selectors)

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

        dev = _scripted_developer(test_file, [fanout_source, fixed_source])

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
        assert (
            "test_rule_catalog_covers_every_validator" in retry_brief
            and "test_catalog_renders_numbered_sentences" in retry_brief
        )
        assert "test_rule_a" in retry_brief or "test_render_a" in retry_brief

        committed = (repo / "tests" / "unit" / "test_x.py").read_text(encoding="utf-8")
        assert "def test_rule_catalog_covers_every_validator(" in committed
        assert "def test_catalog_renders_numbered_sentences(" in committed

        spec = SpecPlan(
            id=_SPEC_ID,
            file_path=Path(f"docs/specs/2026-07-10_{_SPEC_ID}_demo.md"),
            raw_markdown=(
                f"# {_SPEC_ID} \u2014 fan-out reconcile\n\n## Acceptance Criteria\n\n- works\n"
            ),
            title="Fan-out reconcile demo",
            summary="One-step fan-out reconcile demo.",
        )

        def _compliant_judge(_prompt: str) -> str:
            return '{"compliant": true, "reasons": "ok", "gaps": []}'

        self_verify = run_self_verify(
            repo,
            spec=spec,
            plan=plan,
            suite_green=True,
            judge=_compliant_judge,
        )

        assert self_verify.ok is True
        assert self_verify.mechanical_ok is True
        assert self_verify.coverage.covered is True
        assert self_verify.coverage.missing == []
        assert self_verify.judge.available is True
        assert self_verify.judge.compliant is True
