"""A reviewer that could not reach a model ends ERROR, never APPROVE.

SP-REVIEW-TRANSPORT-ERROR-VERDICT. Measured 2026-10-20 on a foreign
GitLab repository with the gateway unreachable: all four reviewers
ended ``[parse_failed:TRANSPORT]`` and each was reported ``APPROVE``
with ``[auto-promoted: nit-only]``. The model boundary here is a fake
agent loop whose ``run_oneshot`` raises the gateway's own transport
error, so the real reviewers, the real orchestrator and the real
GitHub forge protocol (over a stubbed ``gh`` transport) run unchanged.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from repoach.agent_engine.adapters import GatewayTransportError
from repoach.cli.review_cmds import EXIT_REVIEW_ERROR, review_app
from repoach.review import reviewer as reviewer_module
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.reviewer import (
    Architect,
    BotRole,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
)
from tests.unit.test_review_team import _StubGitHubForge


class _UnreachableGatewayLoop:
    """Agent-loop boundary fake: every call fails at the transport layer."""

    def __init__(self) -> None:
        self.calls = 0

    def run_oneshot(self, prompt: str, **_kwargs: Any) -> Any:
        self.calls += 1
        raise GatewayTransportError("connect refused: 127.0.0.1:8084")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the reviewer's real retry backoff waits."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)


def _install_unreachable_reviewers(monkeypatch: pytest.MonkeyPatch) -> _UnreachableGatewayLoop:
    loop = _UnreachableGatewayLoop()
    for name, cls in (
        ("Architect", Architect),
        ("Sentinel", Sentinel),
        ("Tester", reviewer_module.Tester),
        ("Scribe", Scribe),
    ):
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _cls=cls, **_kw: _cls(loop=loop),
        )
    return loop


def test_transport_exhaustion_yields_error_verdict() -> None:
    reviewer = Architect(loop=_UnreachableGatewayLoop())
    outcome = reviewer.review_diff("diff --git a/x.py b/x.py\n+x = 1\n", pr_number=1)
    assert outcome.verdict is ReviewVerdict.ERROR
    assert outcome.summary.startswith("[parse_failed:TRANSPORT]")


def test_transport_text_response_yields_error_verdict() -> None:
    reviewer = Architect(loop=_UnreachableGatewayLoop())
    verdict, summary, comments = reviewer._parse_response("Connection error.")
    assert verdict is ReviewVerdict.ERROR
    assert summary.startswith("[parse_failed:TRANSPORT]")
    assert comments == []


def test_unreachable_gateway_posts_nothing_and_ends_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _install_unreachable_reviewers(monkeypatch)
    forge = _StubGitHubForge()
    orch = ReviewTeamOrchestrator(
        forge=forge,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=4,
        repo_root=tmp_path,
    )
    team = orch.review_pr(pr_number=42)

    assert loop.calls > 0
    assert [o.verdict for o in team.reviews] == [ReviewVerdict.ERROR] * 4
    assert not any("auto-promoted" in o.summary for o in team.reviews)
    assert team.final_verdict is ReviewVerdict.ERROR
    assert team.posting_outcome == "SKIP_ERROR"
    assert team.posted_reviews == 0
    assert team.posted_comments == 0
    assert forge.posted_reviews == []
    assert forge.posted_comments == []
    assert forge.batch_calls == []
    assert forge.archive_calls == []


def test_one_errored_reviewer_makes_the_team_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _UnreachableGatewayLoop()

    class _ApprovingReviewer:
        def __init__(self, role: BotRole) -> None:
            self.role = role

        def review_diff(self, diff: str, **_kwargs: Any) -> ReviewerOutcome:
            return ReviewerOutcome(
                role=self.role,
                verdict=ReviewVerdict.APPROVE,
                summary="ok",
                comments=[],
                model_used="fake/model",
                elapsed_s=0.0,
                tokens_used=0,
                raw_response="{}",
            )

    monkeypatch.setattr("repoach.review.orchestrator.Architect", lambda **_kw: Architect(loop=loop))
    for name, role in (
        ("Sentinel", BotRole.SENTINEL),
        ("Tester", BotRole.TESTER),
        ("Scribe", BotRole.SCRIBE),
    ):
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _role=role, **_kw: _ApprovingReviewer(_role),
        )
    forge = _StubGitHubForge()
    team = ReviewTeamOrchestrator(
        forge=forge,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        repo_root=tmp_path,
    ).review_pr(pr_number=43)

    assert team.final_verdict is ReviewVerdict.ERROR
    assert forge.posted_reviews == []
    assert forge.batch_calls == []
    assert forge.archive_calls == []


def test_review_pr_cli_exits_3_on_error_verdict() -> None:
    team = TeamOutcome(
        pr_number=42,
        final_verdict=ReviewVerdict.ERROR,
        n_blockers=0,
        n_majors=0,
        reviews=[
            ReviewerOutcome(
                role=BotRole.ARCHITECT,
                verdict=ReviewVerdict.ERROR,
                summary="[parse_failed:TRANSPORT] all 3 attempts raised",
                comments=[],
                model_used="exhausted",
                elapsed_s=0.0,
                tokens_used=0,
                raw_response="",
            )
        ],
    )
    with patch("repoach.cli.review_cmds.run_review", return_value=team):
        result = CliRunner().invoke(review_app, ["pr", "42"])

    assert result.exit_code == EXIT_REVIEW_ERROR == 3
    payload = json.loads(result.stdout)
    assert payload["final_verdict"] == "ERROR"
    assert payload["reviews"][0]["verdict"] == "ERROR"
