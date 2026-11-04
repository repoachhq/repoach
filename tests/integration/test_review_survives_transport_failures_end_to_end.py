"""#288's outage replayed through the whole review: inside the window it completes, past it it exits 3.

SP-REVIEW-SURVIVES-TRANSPORT-FAILURES AC1. The runner host of bot run
41916625939 lost its network for about 100 s and the proxy's breaker
answered 502 for its 120 s cool-down after that. Here each reviewer's
real ``AgentLoop`` talks to a fake provider that answers that 502 until
its own fake clock passes the outage, then approves; the real reviewers,
orchestrator, CLI and GitHub forge protocol (over a stubbed ``gh``
transport) run unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.agent_engine.transport_window import configured_transport_window_s
from repoach.cli.review_cmds import EXIT_REVIEW_ERROR, review_app
from repoach.llm.capability import CAPABILITY_TO_ALIAS, CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import AgentResponse, TextBlock, Usage
from repoach.providers.errors import GatewayTransportError
from repoach.review import reviewer as reviewer_module
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.reviewer import Architect, ReviewVerdict, Scribe, Sentinel
from tests.unit.test_review_team import _StubGitHubForge

APPROVE = json.dumps({"verdict": "APPROVE", "summary": "looks right", "comments": []})


class _OutageProvider:
    """Answers the breaker's 502 until its clock passes ``outage_s``, then approves."""

    name = "gateway"

    def __init__(self, outage_s: float) -> None:
        self.models = dict(CAPABILITY_TO_ALIAS)
        self.outage_s = outage_s
        self.now = 0.0
        self.calls = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def call(self, **_kwargs: Any) -> AgentResponse:
        self.calls += 1
        self.now += 1.0
        if self.now < self.outage_s:
            raise GatewayTransportError(
                'proxy returned 502: {"detail":"All 1 provider candidate(s) are currently '
                'breaker-tripped"}',
                status=502,
            )
        return AgentResponse(
            schema_version="1",
            capability="sonnet",
            stop_reason="end_turn",
            model_used="fake/sonnet",
            content=[TextBlock(text=APPROVE)],
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            elapsed_ms=10,
            trace=[],
        )


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)


def _install(monkeypatch: pytest.MonkeyPatch, outage_s: float) -> list[_OutageProvider]:
    providers: list[_OutageProvider] = []

    def build(cls: type) -> Any:
        provider = _OutageProvider(outage_s)
        providers.append(provider)
        loop = AgentLoop(capability=CapabilityTier.SONNET)
        loop._client = provider
        loop._clock = provider.clock
        loop._sleep = provider.sleep
        return cls(loop=loop)

    for name, cls in (
        ("Architect", Architect),
        ("Sentinel", Sentinel),
        ("Tester", reviewer_module.Tester),
        ("Scribe", Scribe),
    ):
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}", lambda _cls=cls, **_kw: build(_cls)
        )
    return providers


def _review(tmp_path: Path, forge: _StubGitHubForge):
    def review(pr_number: int, **kwargs: Any) -> TeamOutcome:
        return ReviewTeamOrchestrator(
            forge=forge,
            db_path=tmp_path / "review.db",
            post_to_github=kwargs["post"],
            repo_root=tmp_path,
        ).review_pr(pr_number)

    return review


def test_288s_outage_inside_the_window_costs_time_and_the_review_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = _install(monkeypatch, outage_s=220.0)
    forge = _StubGitHubForge()

    with patch("repoach.cli.review_cmds.run_review", _review(tmp_path, forge)):
        result = CliRunner().invoke(review_app, ["pr", "288"])

    payload = json.loads(result.stdout)
    assert [r["verdict"] for r in payload["reviews"]] == ["APPROVE"] * 4
    assert payload["final_verdict"] != ReviewVerdict.ERROR.value
    assert payload["incomplete_cause"] is None
    assert result.exit_code != EXIT_REVIEW_ERROR
    assert len(providers) == 4
    assert all(p.calls > 1 and 220.0 <= p.now <= 300.0 + 2.0 for p in providers)


def test_an_outage_past_the_window_still_exits_3_and_names_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = _install(monkeypatch, outage_s=10_000.0)
    forge = _StubGitHubForge()

    with patch("repoach.cli.review_cmds.run_review", _review(tmp_path, forge)):
        result = CliRunner().invoke(review_app, ["pr", "288"])

    assert result.exit_code == EXIT_REVIEW_ERROR == 3, result.output
    payload = json.loads(result.stdout)
    assert payload["final_verdict"] == "ERROR"
    assert payload["incomplete_cause"] == "transport"
    assert {r["summary"] for r in payload["reviews"]} == {
        "[parse_failed:TRANSPORT] all 3 attempts raised — last=GatewayTransportError"
    }
    assert all(p.now <= 300.0 + 2.0 for p in providers)
    assert forge.posted_reviews == []
