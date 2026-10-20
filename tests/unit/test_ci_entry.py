"""SP-UNIVERSAL-CI-ENTRY: PR resolution, forge detection, posting mode, refusals.

The environment is always a plain ``dict``: nothing here reads the
process environment, so each case states exactly what the CI provided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.ci_entry.advisory import (
    EXIT_CONFIG,
    CiReviewPlan,
    CiReviewReport,
    Outcome,
    plan_ci_review,
    review_mode,
    run_ci_review,
)
from repoach.ci_entry.context import (
    PR_ENV_SOURCES,
    InvalidPullRequestError,
    NoPullRequestError,
    detect_ci_forge,
    resolve_pr_number,
)
from repoach.ci_entry.gateway import GatewayHandle


def _event(tmp_path: Path, payload: object) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestResolvePrNumber:
    def test_flag_outranks_every_variable(self, tmp_path: Path) -> None:
        env = {
            "REPOACH_PR_NUMBER": "1",
            "CI_MERGE_REQUEST_IID": "2",
            "GITHUB_EVENT_PATH": _event(tmp_path, {"pull_request": {"number": 3}}),
            "GITHUB_REF": "refs/pull/4/merge",
        }
        assert resolve_pr_number(9, env) == (9, "flag")

    def test_order_of_the_four_variables(self, tmp_path: Path) -> None:
        env = {
            "REPOACH_PR_NUMBER": "1",
            "CI_MERGE_REQUEST_IID": "2",
            "GITHUB_EVENT_PATH": _event(tmp_path, {"pull_request": {"number": 3}}),
            "GITHUB_REF": "refs/pull/4/merge",
        }
        expected = [(1, "REPOACH_PR_NUMBER"), (2, "CI_MERGE_REQUEST_IID")]
        expected += [(3, "GITHUB_EVENT_PATH"), (4, "GITHUB_REF")]
        for number, source in expected:
            assert resolve_pr_number(None, env) == (number, source)
            env.pop(source)
        with pytest.raises(NoPullRequestError) as info:
            resolve_pr_number(None, env)
        assert info.value.looked_at == PR_ENV_SOURCES

    def test_blank_variable_counts_as_unset(self) -> None:
        assert resolve_pr_number(None, {"REPOACH_PR_NUMBER": " ", "CI_MERGE_REQUEST_IID": "5"}) == (
            5,
            "CI_MERGE_REQUEST_IID",
        )

    def test_issue_comment_on_a_pull_request(self, tmp_path: Path) -> None:
        path = _event(tmp_path, {"issue": {"number": 12, "pull_request": {"url": "x"}}})
        assert resolve_pr_number(None, {"GITHUB_EVENT_PATH": path}) == (12, "GITHUB_EVENT_PATH")

    def test_issue_comment_on_a_plain_issue_is_not_a_pr(self, tmp_path: Path) -> None:
        path = _event(tmp_path, {"issue": {"number": 12}})
        with pytest.raises(NoPullRequestError) as info:
            resolve_pr_number(None, {"GITHUB_EVENT_PATH": path})
        assert "not a pull request" in str(info.value)

    def test_workflow_dispatch_input(self, tmp_path: Path) -> None:
        path = _event(tmp_path, {"inputs": {"pr_number": "33"}})
        assert resolve_pr_number(None, {"GITHUB_EVENT_PATH": path}) == (33, "GITHUB_EVENT_PATH")

    def test_unreadable_event_falls_back_to_the_ref(self, tmp_path: Path) -> None:
        env = {
            "GITHUB_EVENT_PATH": str(tmp_path / "missing.json"),
            "GITHUB_REF": "refs/pull/8/merge",
        }
        assert resolve_pr_number(None, env) == (8, "GITHUB_REF")

    def test_branch_ref_is_named(self) -> None:
        with pytest.raises(NoPullRequestError) as info:
            resolve_pr_number(None, {"GITHUB_REF": "refs/heads/main"})
        assert "refs/heads/main" in str(info.value)

    def test_non_numeric_iid_is_invalid_not_absent(self) -> None:
        with pytest.raises(InvalidPullRequestError) as info:
            resolve_pr_number(None, {"CI_MERGE_REQUEST_IID": "abc"})
        assert str(info.value) == "not a PR number: 'abc' from CI_MERGE_REQUEST_IID"

    def test_pr_zero_is_invalid(self) -> None:
        with pytest.raises(InvalidPullRequestError) as info:
            resolve_pr_number(0, {})
        assert str(info.value) == "not a PR number: 0 from --pr"

    @pytest.mark.parametrize(
        ("pr", "env", "reason"),
        [
            (
                None,
                {"CI_MERGE_REQUEST_IID": "abc"},
                "not a PR number: 'abc' from CI_MERGE_REQUEST_IID",
            ),
            (0, {}, "not a PR number: 0 from --pr"),
            (None, {"REPOACH_PR_NUMBER": "-3"}, "not a PR number: '-3' from REPOACH_PR_NUMBER"),
        ],
    )
    def test_invalid_number_is_a_config_error(
        self, pr: int | None, env: dict[str, str], reason: str
    ) -> None:
        report = plan_ci_review({"GITLAB_CI": "true", **env}, pr_override=pr, strict=True)
        assert isinstance(report, CiReviewReport)
        assert (report.outcome, report.reason, report.exit_code) == (
            Outcome.CONFIG_ERROR,
            reason,
            EXIT_CONFIG,
        )


class TestDetectForge:
    def test_gitlab(self) -> None:
        assert detect_ci_forge({"GITLAB_CI": "true"}) == ("gitlab", "GITLAB_CI")

    def test_github(self) -> None:
        assert detect_ci_forge({"GITHUB_ACTIONS": "true"}) == ("github", "GITHUB_ACTIONS")

    def test_neither(self) -> None:
        assert detect_ci_forge({}) == (None, "none")


class TestReviewMode:
    @pytest.mark.parametrize("value", ["", "0", "true", "yes", "2", " 1x"])
    def test_anything_but_exactly_one_is_a_dry_run(self, value: str) -> None:
        assert review_mode({"REPOACH_ADVISORY_POST": value}, post=False, dry_run=False) == "dry-run"

    def test_one_posts(self) -> None:
        assert review_mode({"REPOACH_ADVISORY_POST": "1"}, post=False, dry_run=False) == "post"

    def test_flags_win_over_the_variable(self) -> None:
        assert review_mode({"REPOACH_ADVISORY_POST": "1"}, post=False, dry_run=True) == "dry-run"
        assert review_mode({}, post=True, dry_run=False) == "post"

    def test_post_and_dry_run_together_is_a_config_error(self) -> None:
        env = {"GITLAB_CI": "true", "CI_MERGE_REQUEST_IID": "3"}
        strict = plan_ci_review(env, post=True, dry_run=True, strict=True)
        advisory = plan_ci_review(env, post=True, dry_run=True)
        assert isinstance(strict, CiReviewReport) and isinstance(advisory, CiReviewReport)
        assert strict.outcome == Outcome.CONFIG_ERROR
        assert strict.exit_code == EXIT_CONFIG
        assert advisory.exit_code == 0


class TestGitLabBotIdentity:
    def _plan(self) -> CiReviewPlan:
        planned = plan_ci_review(
            {
                "GITLAB_CI": "true",
                "CI_MERGE_REQUEST_IID": "3",
                "GITLAB_TOKEN": "t",
                "REPOACH_LLM_PROXY_BASE_URL": "http://gw",
                "REPOACH_ANTHROPIC_AUTH_TOKEN": "s",
            },
            post=True,
            strict=True,
        )
        assert isinstance(planned, CiReviewPlan)
        return planned

    def test_empty_bot_username_refuses_posting(self) -> None:
        calls: list[str] = []

        def runner(plan: CiReviewPlan, env: dict[str, str]) -> tuple[int, str]:
            calls.append("review")
            return 0, "{}"

        def starter(env: object) -> GatewayHandle:
            raise AssertionError("no gateway in gateway mode")

        report = run_ci_review(
            self._plan(),
            {},
            pr_state=lambda plan: calls.append("state") or "open",
            review_runner=runner,
            gateway_starter=starter,
            bot_username="",
        )
        assert report.outcome == Outcome.CONFIG_ERROR
        assert "bot username required" in report.reason
        assert report.exit_code == EXIT_CONFIG
        assert calls == []
