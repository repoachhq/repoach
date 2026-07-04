"""SP-DEV-CC-WIRE — exploration backend threaded through `develop`.

Pins that ``--explore-via`` / ``--cc-model`` reach
``run_planner_session`` through ``run_developer_session`` and
``load_or_produce_plan``, that a committed plan makes the flags inert
(planning is never invoked), and that a bad backend value exits 2.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ferova.cli.review_cmds import review_app
from ferova.review.dev_runner import DevSessionResult, load_or_produce_plan
from ferova.review.persistence import init_schema
from ferova.review.plan import ActionPlan, PlanStep, plan_relpath, render_plan_markdown
from ferova.review.spec import load_spec

_SPEC_ID = "SP-CCWIRE-DEMO"


def _result(**overrides) -> DevSessionResult:
    """Build a :class:`DevSessionResult` with field parity for CLI payload tests."""
    base = {"spec_id": "SP-X", "ruff_passed": True, "pytest_passed": True}
    base.update(overrides)
    return DevSessionResult(**base)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-06-08_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo\n\n## Why\n\nWiring.\n\n## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    init_schema(repo.parent / "test.db")
    return repo


def _plan() -> ActionPlan:
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Demo",
        summary="One step.",
        steps=[
            PlanStep(
                index=1,
                title="Add module",
                files=[
                    "src/mini.py",
                    "tests/unit/test_mini.py",
                    "tests/integration/test_demo.py",
                ],
                action="Create it.",
                commit_message="feat(demo): mini",
                done_when="pytest tests/unit/test_mini.py passes",
                unit_tests=["tests/unit/test_mini.py"],
            )
        ],
        integration_tests=["tests/integration/test_demo.py"],
    )


class TestLoadOrProduceForwarding:
    def test_explore_via_and_cc_model_forwarded_to_planner(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        spec = load_spec(_SPEC_ID, root=repo)
        captured: dict[str, object] = {}

        class _Outcome:
            written = True
            error = None

        def fake_session(spec_id, **kwargs):
            captured.update(kwargs)
            target = repo / plan_relpath(_SPEC_ID)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_plan_markdown(_plan()), encoding="utf-8")
            return _Outcome()

        with patch("ferova.review.planner.run_planner_session", side_effect=fake_session):
            plan, error = load_or_produce_plan(
                spec, repo_root=repo, explore_via="claude_cli", cc_model="opus"
            )

        assert error is None
        assert plan is not None
        assert captured["explore_via"] == "claude_cli"
        assert captured["cc_model"] == "opus"

    def test_committed_plan_makes_flags_inert(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        target = repo / plan_relpath(_SPEC_ID)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_plan_markdown(_plan()), encoding="utf-8")
        spec = load_spec(_SPEC_ID, root=repo)

        def fake_session(*a, **kw):
            raise AssertionError("planning must not run when a plan is committed")

        with patch("ferova.review.planner.run_planner_session", side_effect=fake_session):
            plan, error = load_or_produce_plan(spec, repo_root=repo, explore_via="claude_cli")

        assert error is None
        assert plan is not None


class TestDevelopCli:
    def test_bad_explore_via_exits_2(self) -> None:
        runner = CliRunner()
        result = runner.invoke(review_app, ["develop", "SP-X", "--explore-via", "telepathy"])
        assert result.exit_code == 2

    def test_develop_forwards_backend_to_session(self, tmp_path: Path) -> None:
        runner = CliRunner()
        captured: dict[str, object] = {}

        def fake_session(spec_id, **kwargs):
            captured.update(kwargs)
            return _result(no_op_reason="dry-run: push=False")

        with patch("ferova.cli.review_cmds.run_developer_session", side_effect=fake_session):
            runner.invoke(
                review_app,
                [
                    "develop",
                    "SP-X",
                    "--no-push",
                    "--explore-via",
                    "claude_cli",
                    "--cc-model",
                    "opus",
                ],
            )

        assert captured["explore_via"] == "claude_cli"
        assert captured["cc_model"] == "opus"

    def test_develop_decomposed_opens_pr_from_result_fields(self) -> None:
        runner = CliRunner()
        captured_pr: dict[str, object] = {}

        def fake_session(spec_id, **kwargs):
            return _result(
                spec_id="SP-MULTI",
                branch="feat/sp-multi-impl",
                pushed=True,
                self_verified=True,
                steps_total=2,
                steps_completed=2,
                decomposed=True,
                sub_spec_ids=["SP-MULTI-1", "SP-MULTI-2"],
                pr_title="feat(sp-multi): Multi",
                pr_summary="the parent",
                no_op_reason="",
            )

        def fake_open_pr(**kwargs):
            captured_pr.update(kwargs)
            return "https://github.test/pr/1"

        with (
            patch("ferova.cli.review_cmds.run_developer_session", side_effect=fake_session),
            patch("ferova.cli.review_cmds.open_pr", side_effect=fake_open_pr),
        ):
            result = runner.invoke(review_app, ["develop", "SP-MULTI"])

        assert captured_pr["spec_id"] == "SP-MULTI"
        assert captured_pr["title"] == "feat(sp-multi): Multi"
        assert captured_pr["summary"] == "the parent"
        assert "SP-MULTI-1" in str(captured_pr["spec_ref"])
        assert captured_pr["branch"] == "feat/sp-multi-impl"
        payload = json.loads(result.stdout[result.stdout.index("{") :])
        assert payload["decomposed"] is True
        assert payload["sub_spec_ids"] == ["SP-MULTI-1", "SP-MULTI-2"]
        assert payload["self_verified"] is True
        assert payload["steps_total"] == 2
        assert payload["pr_url"] == "https://github.test/pr/1"
