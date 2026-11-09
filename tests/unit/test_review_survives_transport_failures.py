"""A short transport outage costs time, not the review (SP-REVIEW-SURVIVES-TRANSPORT-FAILURES).

Measured 2026-11-04 on PR #288, bot run 41916625939 attempt 1: the
runner host lost its network for about 100 s, the proxy's breaker
answered ``502 ... breaker-tripped`` to every call of that minute, the
sentinel ended ``[parse_failed:TRANSPORT] all 3 attempts raised —
last=GatewayTransportError``, two findings were deferred ``judge call
failed: GatewayTransportError`` and ``review pr`` exited 3. The model
boundary here is a fake provider client that raises the gateway's own
transport error on a fake clock; the real ``AgentLoop``, reviewers,
refuter, CLI and GitHub forge protocol (over the merge-wait fixture
``gh``) run unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.agent_engine.transport_window import configured_transport_window_s, window_spent
from repoach.cli.review_cmds import EXIT_REVIEW_ERROR, review_app
from repoach.llm.capability import CAPABILITY_TO_ALIAS, CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import AgentResponse, TextBlock, Usage
from repoach.project.config import ConfigError
from repoach.project.resolve import resolve_project_config
from repoach.providers.errors import GatewayError, GatewayTransportError
from repoach.providers.usage import reviewer_scope, served_by_line, usage_run
from repoach.review.orchestrator import TeamOutcome
from repoach.review.refuter import Refutation, refute_finding
from repoach.review.review_retry import (
    REVIEW_RETRY_OUTCOME,
    dispatch_review_retry,
    incomplete_cause,
    record_retry_ran,
)
from repoach.review.reviewer import Architect, BotRole, ReviewerOutcome, ReviewVerdict
from tests.merge_wait_fakes import FixturePR, MergeWaitFixtureForge
from tests.refuter_fakes import blocking_finding, git_tree, judge_reply

HEAD = "cc595508ce42447f2857b58f32e243dde0cd0fe0"
NOW = datetime(2026, 11, 4, 21, 41, tzinfo=UTC)
TRANSPORT_SUMMARY = "[parse_failed:TRANSPORT] all 3 attempts raised — last=GatewayTransportError"


@pytest.fixture(autouse=True)
def _window_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the default 300 s window and keep the loop constructible without ``.env``."""
    monkeypatch.setenv("REPOACH_TRANSPORT_RETRY_WINDOW_S", "300")
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.configured_transport_window_s",
        configured_transport_window_s,
    )
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


class _Clock:
    """A monotonic clock that only moves when the loop sleeps or a call takes time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


class _Outage:
    """A provider client that fails ``failures`` times, then answers (``None``: never answers)."""

    name = "gateway"

    def __init__(
        self,
        clock: _Clock,
        failures: int | None,
        *,
        error: Exception | None = None,
        call_s: float = 0.0,
        text: str = "done",
    ) -> None:
        self.models = dict(CAPABILITY_TO_ALIAS)
        self.clock = clock
        self.failures = failures
        self.error = error
        self.call_s = call_s
        self.text = text
        self.calls = 0

    def call(self, **_kwargs: Any) -> AgentResponse:
        self.calls += 1
        self.clock.now += self.call_s
        if self.failures is None or self.calls <= self.failures:
            raise self.error or GatewayTransportError(
                'proxy returned 502: {"detail":"All 1 provider candidate(s) are currently '
                'breaker-tripped"}',
                status=502,
            )
        return AgentResponse(
            schema_version="1",
            capability="opus",
            stop_reason="end_turn",
            model_used="fake/opus",
            content=[TextBlock(text=self.text)],
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            elapsed_ms=10,
            trace=[],
        )


def _loop(client: _Outage, clock: _Clock) -> AgentLoop:
    loop = AgentLoop(capability=CapabilityTier.OPUS)
    loop._client = client
    loop._clock = clock
    loop._sleep = clock.sleep
    return loop


def test_a_transport_failure_shorter_than_the_window_is_retried_until_it_answers() -> None:
    clock = _Clock()
    client = _Outage(clock, failures=4, call_s=2.0)
    with usage_run("review pr", pr_number=288) as run:
        output = _loop(client, clock).run_oneshot("judge this")

    assert output.text == "done"
    assert client.calls == 5
    assert clock.waits == [5.0, 10.0, 20.0, 40.0]
    rows = run.snapshot()
    assert len(rows) == 5
    assert [bool(row.error) for row in rows] == [True, True, True, True, False]
    assert all("GatewayTransportError" in (row.error or "") for row in rows[:4])


def test_a_failure_longer_than_the_window_ends_the_call_as_before() -> None:
    clock = _Clock()
    client = _Outage(clock, failures=None, call_s=1.0)
    with pytest.raises(GatewayTransportError) as raised:
        _loop(client, clock).run_oneshot("judge this")

    assert type(raised.value) is GatewayTransportError
    assert window_spent(raised.value)
    assert sum(clock.waits) + client.calls * 1.0 <= 300.0 + 1.0
    assert clock.waits[:6] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert client.calls == len(clock.waits) + 1


@pytest.mark.parametrize(
    "error",
    [
        GatewayError("proxy returned 400: bad request"),
        GatewayTransportError("proxy returned 429: slow down", status=429),
        GatewayTransportError("proxy returned 408: request timeout", status=408),
    ],
    ids=["400", "429", "408"],
)
def test_a_4xx_or_a_non_transport_error_is_never_retried(error: Exception) -> None:
    clock = _Clock()
    client = _Outage(clock, failures=None, error=error)
    with pytest.raises(GatewayError) as raised:
        _loop(client, clock).run_oneshot("judge this")

    assert raised.value is error
    assert client.calls == 1
    assert clock.waits == []
    assert not window_spent(raised.value)


def test_a_closed_window_makes_one_attempt() -> None:
    clock = _Clock()
    client = _Outage(clock, failures=None)
    loop = _loop(client, clock)
    loop._transport_window_s = 0.0
    with pytest.raises(GatewayTransportError) as raised:
        loop.run_oneshot("judge this")

    assert client.calls == 1
    assert not window_spent(raised.value)


def test_a_reviewer_spends_one_window_and_reports_the_same_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    client = _Outage(clock, failures=None, call_s=1.0)
    monkeypatch.setattr("time.sleep", clock.sleep)
    reviewer = Architect(loop=_loop(client, clock))

    outcome = reviewer.review_diff("diff --git a/x.py b/x.py\n+x = 1\n", pr_number=288)

    assert outcome.verdict is ReviewVerdict.ERROR
    assert outcome.summary == TRANSPORT_SUMMARY
    assert sum(clock.waits) <= 300.0
    assert client.calls == len(clock.waits) + 1


def test_the_judge_after_the_window_defers_with_the_same_reason(tmp_path: Path) -> None:
    root = git_tree(tmp_path / "head", {"src/app.py": "def run():\n    return eval(input())\n"})
    finding = blocking_finding(
        "eval of user input", file="src/app.py", line=2, head=HEAD, pr_number=288
    )
    clock = _Clock()
    loop = _loop(_Outage(clock, failures=None, call_s=1.0), clock)

    status, reason = refute_finding(
        finding, repo_root=root, judge=lambda prompt: loop.run_oneshot(prompt).text
    )

    assert reason == "judge call failed: GatewayTransportError"
    assert status.value == "proposed"

    clock = _Clock()
    answering = _Outage(
        clock, failures=3, text=judge_reply("verified", "eval runs the user's input")
    )
    loop = _loop(answering, clock)
    status, _ = refute_finding(
        finding, repo_root=root, judge=lambda prompt: loop.run_oneshot(prompt).text
    )
    assert status.value == "verified"
    assert answering.calls == 4


def _outcome(verdict: ReviewVerdict, summary: str) -> ReviewerOutcome:
    return ReviewerOutcome(role=BotRole.SENTINEL, verdict=verdict, summary=summary)


def _deferral(reason: str) -> Refutation:
    return Refutation(
        finding_id=1,
        file="src/repoach/review/base_ref.py",
        line_start=344,
        claim_type="missing_test",
        state="deferred",
        reason=reason,
    )


@pytest.mark.parametrize(
    ("verdict", "reviews", "refutations", "cause"),
    [
        (ReviewVerdict.APPROVE, [], [], None),
        (
            ReviewVerdict.ERROR,
            [_outcome(ReviewVerdict.ERROR, TRANSPORT_SUMMARY)],
            [_deferral("judge call failed: GatewayTransportError")] * 2,
            "transport",
        ),
        (
            ReviewVerdict.ERROR,
            [_outcome(ReviewVerdict.ERROR, "_(bot crashed: GatewayChainExhausted — all down)_")],
            [],
            "transport",
        ),
        (
            ReviewVerdict.ERROR,
            [_outcome(ReviewVerdict.ERROR, TRANSPORT_SUMMARY)],
            [_deferral("evidence outside the diff")],
            "other",
        ),
        (
            ReviewVerdict.ERROR,
            [
                _outcome(
                    ReviewVerdict.ERROR,
                    "[parse_failed:TRANSPORT] provider refused, not retried — ProviderAuthError: 401",
                )
            ],
            [],
            "other",
        ),
        (
            ReviewVerdict.ERROR,
            [_outcome(ReviewVerdict.ERROR, "[parse_failed:MALFORMED] x")],
            [],
            "other",
        ),
        (ReviewVerdict.ERROR, [], [], "other"),
    ],
    ids=[
        "complete",
        "transport-288",
        "crash-chain-exhausted",
        "other-deferral",
        "refused-credential",
        "malformed",
        "no-errored-reviewer",
    ],
)
def test_incomplete_cause_is_transport_only_when_every_failure_is_transport(
    verdict: ReviewVerdict,
    reviews: list[ReviewerOutcome],
    refutations: list[Refutation],
    cause: str | None,
) -> None:
    assert incomplete_cause(verdict, reviews, refutations) == cause


def test_review_pr_still_exits_3_and_names_the_transport_cause() -> None:
    team = TeamOutcome(
        pr_number=288,
        final_verdict=ReviewVerdict.ERROR,
        n_blockers=0,
        n_majors=0,
        head_sha=HEAD,
        reviews=[_outcome(ReviewVerdict.ERROR, TRANSPORT_SUMMARY)],
        refutations=[_deferral("judge call failed: GatewayTransportError")],
    )
    with patch("repoach.cli.review_cmds.run_review", return_value=team):
        result = CliRunner().invoke(review_app, ["pr", "288"])

    assert result.exit_code == EXIT_REVIEW_ERROR == 3
    payload = json.loads(result.stdout)
    assert payload["final_verdict"] == "ERROR"
    assert payload["incomplete_cause"] == "transport"
    assert payload["head_sha"] == HEAD
    assert payload["reviews"][0]["summary"] == TRANSPORT_SUMMARY


def _forge(head: str = HEAD, *, state: str = "OPEN") -> MergeWaitFixtureForge:
    return MergeWaitFixtureForge(
        [FixturePR(288, head, "fix/base-ref-must-resolve", state=state)], now=NOW
    )


def test_one_retry_is_dispatched_then_the_head_is_never_retried_again() -> None:
    fixture = _forge()

    first = dispatch_review_retry(
        fixture.forge, 288, head_sha=HEAD, failed_run="41916625939", now=NOW
    )
    assert first.dispatched
    assert fixture.dispatches == ["workflow_run auto-review.yml pr_number=288 retry_of=41916625939"]

    again = dispatch_review_retry(
        fixture.forge, 288, head_sha=HEAD, failed_run="41916625940", now=NOW
    )
    assert not again.dispatched
    assert again.line == (
        "review retry: cc59550 was already retried (auto-review:41916625939), not retried again"
    )
    assert len(fixture.dispatches) == 1


def test_the_attempts_comment_names_both_runs() -> None:
    fixture = _forge()
    dispatch_review_retry(fixture.forge, 288, head_sha=HEAD, failed_run="41916625939", now=NOW)
    line = record_retry_ran(
        fixture.forge,
        288,
        head_sha=HEAD,
        run_id="41920000001",
        retry_of="41916625939",
        result="success",
        now=NOW,
    )

    assert line == (
        "review retry: run 41920000001 retried run 41916625939 at cc59550: success (recorded)"
    )
    comment = fixture.attempts_comment()
    assert comment is not None
    attempts = json.loads(str(comment["body"]).split("```json\n", 1)[1].split("\n```", 1)[0])[
        "attempts"
    ]
    assert [a["outcome"] for a in attempts] == [REVIEW_RETRY_OUTCOME] * 2
    assert [a["trigger"] for a in attempts] == ["auto-review:41916625939", "dispatch:41920000001"]
    assert "41916625939" in attempts[1]["reason"] and "41920000001" in attempts[1]["reason"]


def test_a_moved_head_or_a_closed_pr_is_not_retried() -> None:
    moved = _forge(head="d" * 40)
    decision = dispatch_review_retry(
        moved.forge, 288, head_sha=HEAD, failed_run="41916625939", now=NOW
    )
    assert not decision.dispatched
    assert "the head moved from cc59550 to ddddddd" in decision.line
    assert moved.dispatches == []

    closed = _forge(state="MERGED")
    decision = dispatch_review_retry(
        closed.forge, 288, head_sha=HEAD, failed_run="41916625939", now=NOW
    )
    assert not decision.dispatched
    assert decision.line == "review retry: #288 is merged, not retried"
    assert closed.dispatches == []
    assert closed.attempts_comment() is None


def test_review_retry_cli_dispatches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _forge()
    monkeypatch.setattr("repoach.cli.review_cmds.select_forge", lambda **_kw: fixture.forge)
    args = ["retry", "288", "--head", HEAD, "--run", "41916625939"]

    first = CliRunner().invoke(review_app, args)
    second = CliRunner().invoke(review_app, args)

    assert first.exit_code == 0, first.output
    assert "retry dispatched at cc59550" in first.output
    assert second.exit_code == 0, second.output
    assert "not retried again" in second.output
    assert len(fixture.dispatches) == 1


def test_the_window_comes_from_the_environment_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repoach.toml").write_text(
        "[provider]\ntransport_retry_window_s = 120\n", encoding="utf-8"
    )
    monkeypatch.delenv("REPOACH_TRANSPORT_RETRY_WINDOW_S")
    resolved = resolve_project_config(tmp_path)
    assert resolved.config.provider.transport_retry_window_s == 120
    assert resolved.source("provider.transport_retry_window_s") == "file"

    monkeypatch.setenv("REPOACH_TRANSPORT_RETRY_WINDOW_S", "45")
    resolved = resolve_project_config(tmp_path)
    assert resolved.config.provider.transport_retry_window_s == 45
    assert resolved.source("provider.transport_retry_window_s") == "env"

    (tmp_path / "repoach.toml").write_text(
        "[provider]\ntransport_retry_window_s = true\n", encoding="utf-8"
    )
    monkeypatch.delenv("REPOACH_TRANSPORT_RETRY_WINDOW_S")
    with pytest.raises(ConfigError):
        resolve_project_config(tmp_path)
    (tmp_path / "repoach.toml").write_text("[provider]\n", encoding="utf-8")
    monkeypatch.setenv("REPOACH_TRANSPORT_RETRY_WINDOW_S", "1801")
    with pytest.raises(ConfigError, match="REPOACH_TRANSPORT_RETRY_WINDOW_S"):
        resolve_project_config(tmp_path)


def test_the_report_names_which_model_served_each_role_and_what_failed() -> None:
    clock = _Clock()
    with usage_run("review pr", pr_number=288) as run:
        with reviewer_scope("Sentinel"), pytest.raises(GatewayTransportError):
            _loop(_Outage(clock, failures=None, call_s=1.0), clock).run_oneshot("review")
        with reviewer_scope("Refuter"):
            _loop(_Outage(clock, failures=1), clock).run_oneshot("judge")

    line = served_by_line(run)
    assert line.startswith("providers: Sentinel none served, ")
    assert "failed (GatewayTransportError)" in line
    assert line.endswith("; Refuter fake/opus, 1 failed (GatewayTransportError)")
