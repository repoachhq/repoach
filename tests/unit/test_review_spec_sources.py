"""SP-UNIVERSAL-PROJECT-CONFIG G4, AC6: the spec a review runs against, end to end.

Drives the real orchestrator (reviewers patched), ``review pr``,
``review gate``, ``config show`` and ``doctor`` against scratch
repositories: the ``--spec`` flag, a ``Spec:`` description line, a
missing spec and no spec at all. Kept apart from
``test_project_config.py`` because it reaches the review pipeline and
the forge models, which themselves read the project configuration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import repoach.core.config as core_config
from repoach.cli.main import app
from repoach.cli.review_cmds import review_app
from repoach.forge import Diff, MergeMethod, PullRequest
from repoach.project.config import ProjectConfig
from repoach.project.resolve import effective_rows, resolve_project_config
from repoach.review.auto_merge import GateEvaluation
from repoach.review.merge_gate import MergeDecision, MergeFacts
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.reviewer import ReviewVerdict

ENV_NAMES = (
    "REPOACH_FORGE",
    "REPOACH_INTEGRATION_BRANCH",
    "INTEGRATION_BRANCH",
    "REPOACH_RELEASE_BRANCH",
    "RELEASE_BRANCH",
    "REPOACH_MERGE_METHOD",
    "REPOACH_REMOVE_SOURCE_BRANCH",
    "REPOACH_REVIEW_BOT_LOGIN",
    "REVIEW_BOT_LOGIN",
    "REPOACH_PROVIDER",
    "REPOACH_DB_PATH",
    "REPOACH_LOGS_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    core_config._settings = None


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _repo(tmp_path: Path, origin: str | None = None, toml: str | None = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    if origin is not None:
        _git(root, "remote", "add", "origin", origin)
    if toml is not None:
        (root / "repoach.toml").write_text(toml, encoding="utf-8")
    return root


def _spec_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    specs = root / "docs/specs"
    specs.mkdir(parents=True)
    (specs / "2026-10-19_SP-YOOB-MR-CREATE_mr-helper.md").write_text("# MR helper\n\nBody.\n")
    return root


def _orchestrate(root: Path, body: str, flag: str | None) -> TeamOutcome:
    forge = MagicMock()
    forge.diff.return_value = Diff(text="diff --git a/x b/x\n", files=["x"])
    forge.pull_request.return_value = PullRequest(
        number=283, head_ref="feat/mr-create", base_ref="develop", body=body
    )
    forge.head_sha.return_value = "abc123"
    forge.comments.return_value = []
    orch = ReviewTeamOrchestrator(
        forge=forge, db_path=root / "review.db", post_to_github=False, repo_root=root
    )
    outcome = MagicMock(
        role=MagicMock(value="x"),
        verdict=MagicMock(value="APPROVE"),
        summary="ok",
        comments=[],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="",
    )
    with (
        patch("repoach.review.reviewer.AgentLoop", return_value=MagicMock()),
        patch("repoach.review.reviewer.Architect.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Sentinel.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Tester.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Scribe.review_diff", return_value=outcome),
    ):
        return orch.review_pr(283, spec_flag=flag)


class TestReviewSpecSource:
    def test_flag_is_reported(self, tmp_path: Path) -> None:
        team = _orchestrate(_spec_repo(tmp_path), "", "SP-YOOB-MR-CREATE")
        assert (team.spec_id, team.spec_source) == ("SP-YOOB-MR-CREATE", "flag")

    def test_description_line_is_reported(self, tmp_path: Path) -> None:
        team = _orchestrate(_spec_repo(tmp_path), "Spec: SP-YOOB-MR-CREATE\n", None)
        assert (team.spec_id, team.spec_source) == ("SP-YOOB-MR-CREATE", "description")

    def test_no_spec_is_not_a_refusal(self, tmp_path: Path) -> None:
        team = _orchestrate(_spec_repo(tmp_path), "measurement only", None)
        assert (team.spec_id, team.spec_source, team.spec_covered) == (None, None, None)

    def test_cli_payload_carries_spec_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _spec_repo(tmp_path)
        monkeypatch.chdir(root)
        team = TeamOutcome(
            pr_number=283,
            final_verdict=ReviewVerdict.COMMENT,
            n_blockers=0,
            n_majors=0,
            spec_id="SP-YOOB-MR-CREATE",
            spec_source="flag",
        )
        with patch("repoach.cli.review_cmds.run_review", return_value=team) as run:
            result = CliRunner().invoke(
                review_app, ["pr", "283", "--dry-run", "--spec", "SP-YOOB-MR-CREATE"]
            )
        assert result.exit_code == 0, result.output
        assert run.call_args.kwargs["spec"] == "SP-YOOB-MR-CREATE"
        payload = json.loads(result.stdout)
        assert payload["spec"] == "SP-YOOB-MR-CREATE"
        assert payload["spec_source"] == "flag"
        assert payload["spec_coverage"] == "unknown"

        team.spec_id = team.spec_source = None
        with patch("repoach.cli.review_cmds.run_review", return_value=team):
            result = CliRunner().invoke(review_app, ["pr", "283", "--dry-run"])
        payload = json.loads(result.stdout)
        assert result.exit_code == 0
        assert payload["spec"] is None
        assert payload["spec_coverage"] == "not_applicable"

    def test_missing_flag_spec_exits_2_before_any_forge_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(_spec_repo(tmp_path))
        with patch("repoach.cli.review_cmds.run_review") as run:
            result = CliRunner().invoke(review_app, ["pr", "283", "--dry-run", "--spec", "SP-X"])
        assert result.exit_code == 2
        assert "spec SP-X not found under docs/specs" in result.stderr
        run.assert_not_called()


class TestSurfaces:
    def test_config_show_tags_every_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, "git@gitlab.com:o/r.git", "[repoach]\nmerge_method = 'merge'\n")
        monkeypatch.chdir(root)
        result = CliRunner().invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        keyed = [line for line in result.stdout.splitlines() if " = " in line]
        rows = effective_rows(resolve_project_config(root))
        assert len(keyed) == sum(1 for _, value, _ in rows if value is not None)
        assert all(
            line.rsplit("# source: ", 1)[1] in {"default", "file", "env", "origin"}
            for line in keyed
        )
        assert 'merge_method = "merge"  # source: file' in result.stdout
        assert 'forge = "gitlab"  # source: origin' in result.stdout

    def test_doctor_prints_config_with_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, "https://github.com/o/r.git", "[repoach]\nspecs_dir = 'specs'\n")
        monkeypatch.chdir(root)
        result = CliRunner().invoke(app, ["doctor"])
        assert 'config.repoach.specs_dir="specs" (file)' in result.stdout
        assert 'config.repoach.forge="github" (origin)' in result.stdout
        assert 'config.repoach.plans_dir="docs/plans" (default)' in result.stdout

    def test_invalid_file_stops_project_commands_with_rc_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path, toml="[repoach]\nmerge_method = 'fast'\n")
        monkeypatch.chdir(root)
        with patch("repoach.cli.review_cmds.evaluate_merge_gate") as gate:
            result = CliRunner().invoke(app, ["review", "gate", "1"])
        assert result.exit_code == 2
        assert f"{root / 'repoach.toml'}:2" in result.stderr
        gate.assert_not_called()
        for command in (["config", "show"], ["doctor"]):
            assert CliRunner().invoke(app, command).exit_code == 2


class TestGateSpecSource:
    def _evaluation(self, body: str) -> GateEvaluation:
        facts = MergeFacts(
            head_sha="abc",
            ci_green=True,
            open_blocking_findings=0,
            spec_covered=False,
            spec_coverage_known=False,
            review_complete=True,
            review_integrity_known=True,
        )
        return GateEvaluation(
            head_sha="abc",
            ci_gate=MagicMock(green=True),
            facts=facts,
            decision=MergeDecision(merge=True, reasons=[]),
            pull_request=PullRequest(number=7, head_ref="docs/measure", body=body),
        )

    @pytest.mark.parametrize(
        ("body", "spec", "source", "coverage"),
        [
            ("Spec: SP-YOOB-MR-CREATE\n", "SP-YOOB-MR-CREATE", "description", "unknown"),
            ("measurement only\n", None, None, "not_applicable"),
            ("spec: SP-GHOST-1\n", "SP-GHOST-1", "description", "missing"),
            ("SPEC: sp-yoob-mr-create\n", "sp-yoob-mr-create", "description", "unknown"),
        ],
    )
    def test_gate_reports_spec_source_and_never_refuses_for_no_spec(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
        spec: str | None,
        source: str | None,
        coverage: str,
    ) -> None:
        monkeypatch.chdir(_spec_repo(tmp_path))
        evaluation = self._evaluation(body)
        with patch("repoach.cli.review_cmds.evaluate_merge_gate", return_value=evaluation):
            result = CliRunner().invoke(review_app, ["gate", "7"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert (payload["spec"], payload["spec_source"]) == (spec, source)
        assert payload["facts"]["spec_coverage"] == coverage
        assert payload["merge"] is True


def test_description_naming_a_missing_spec_warns_and_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _spec_repo(tmp_path)
    team = _orchestrate(root, "Spec: SP-GHOST\n", None)
    assert (team.spec_id, team.spec_source, team.spec_missing) == ("SP-GHOST", "description", True)
    assert team.spec_covered is None

    monkeypatch.chdir(root)
    with patch("repoach.cli.review_cmds.run_review", return_value=team):
        result = CliRunner().invoke(review_app, ["pr", "283", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "warning: spec SP-GHOST (from description) not found under docs/specs" in result.stderr
    payload = json.loads(result.stdout)
    assert (payload["spec"], payload["spec_coverage"]) == ("SP-GHOST", "missing")


def test_missing_description_spec_is_logged_as_a_warning(tmp_path: Path) -> None:
    root = _spec_repo(tmp_path)
    with patch("repoach.review.orchestrator._log") as log:
        _orchestrate(root, "Spec: SP-GHOST\n", None)
    warned = [call.args[0] for call in log.warning.call_args_list]
    assert "review_team.declared_spec_missing" in warned


def test_default_merge_method_is_the_forge_squash() -> None:
    """The configured default equals the method the merge used to hardcode."""
    assert MergeMethod(ProjectConfig().repoach.merge_method) is MergeMethod.SQUASH
