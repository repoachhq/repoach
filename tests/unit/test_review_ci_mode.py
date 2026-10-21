"""Pin the JSON payload shape emitted by ``repoach review pr <N>``.

The :file:`.github/workflows/auto-review.yml` workflow and the
``auto_fix`` / ``auto_merge`` downstream gates consume this JSON via
``jq``.  Any field rename / removal therefore breaks the CI contract.
This test is the canonical schema definition (SP-AUTO-REVIEW-V2-MIGRATION).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from repoach.cli.review_cmds import review_app


@dataclass
class _FakeComment:
    severity: str = "minor"


@dataclass
class _FakeOutcome:
    role: Any
    verdict: Any
    summary: str = ""
    comments: list[_FakeComment] = field(default_factory=list)
    model_used: str = "fake/model"
    elapsed_s: float = 1.234
    tokens_used: int = 42
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _FakeTeam:
    pr_number: int = 999
    final_verdict: Any = None
    n_blockers: int = 0
    n_majors: int = 0
    posted_comments: int = 4
    posted_reviews: int = 2
    reviews: list[_FakeOutcome] = field(default_factory=list)
    spec_id: str | None = None
    spec_source: str | None = None
    spec_covered: bool | None = None
    spec_missing: bool = False
    posting_outcome: str | None = None
    refutations: list[Any] = field(default_factory=list)
    refuter_summary: str | None = None
    refused_writes: list[str] = field(default_factory=list)
    write_refusal: str | None = None
    head_sha: str | None = None
    tree: str | None = None
    worktree: str | None = None


_REQUIRED_TOP_LEVEL = {
    "pr_number",
    "final_verdict",
    "n_blockers",
    "n_majors",
    "posted_comments",
    "posted_reviews",
    "posting_outcome",
    "refused_writes",
    "write_refusal",
    "reviews",
}

_REQUIRED_REVIEW_ENTRY = {
    "role",
    "verdict",
    "n_comments",
    "n_blockers",
    "n_majors",
    "model_used",
    "elapsed_s",
    "tokens_used",
    "summary",
    "trace",
}


def _build_fake_team() -> _FakeTeam:
    """Build a fake :class:`TeamOutcome` covering every verdict + comment severity."""
    from repoach.review.reviewer import BotRole, ReviewVerdict

    outcomes = [
        _FakeOutcome(
            role=BotRole.ARCHITECT,
            verdict=ReviewVerdict.APPROVE,
            summary="ok",
        ),
        _FakeOutcome(
            role=BotRole.TESTER,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="fix tests",
            comments=[
                _FakeComment(severity="blocker"),
                _FakeComment(severity="major"),
                _FakeComment(severity="minor"),
            ],
        ),
    ]
    return _FakeTeam(
        pr_number=999,
        final_verdict=ReviewVerdict.REQUEST_CHANGES,
        n_blockers=1,
        n_majors=1,
        posted_comments=3,
        posted_reviews=2,
        reviews=outcomes,
    )


def test_review_pr_json_payload_matches_documented_schema() -> None:
    """``review pr <N>`` JSON payload exposes every key the workflow / safe_merge depend on."""
    import json

    runner = CliRunner()
    fake_team = _build_fake_team()
    with patch("repoach.cli.review_cmds.run_review", return_value=fake_team):
        result = runner.invoke(review_app, ["pr", "999", "--dry-run"])

    assert result.exit_code == 2, f"REQUEST_CHANGES must exit 2 ; got {result.exit_code}"
    payload = json.loads(result.stdout)

    missing = _REQUIRED_TOP_LEVEL - payload.keys()
    assert not missing, f"missing top-level fields: {missing}"
    assert payload["pr_number"] == 999
    assert payload["final_verdict"] == "REQUEST_CHANGES"
    assert payload["n_blockers"] == 1
    assert payload["n_majors"] == 1
    assert isinstance(payload["reviews"], list)
    assert len(payload["reviews"]) == 2

    for entry in payload["reviews"]:
        missing = _REQUIRED_REVIEW_ENTRY - entry.keys()
        assert not missing, f"missing review-entry fields: {missing}"
        assert entry["role"] in {"architect", "sentinel", "tester", "scribe"}
        assert entry["verdict"] in {"APPROVE", "REQUEST_CHANGES", "COMMENT"}

    tester = next(e for e in payload["reviews"] if e["role"] == "tester")
    assert tester["n_blockers"] == 1
    assert tester["n_majors"] == 1
    assert tester["n_comments"] == 3


def test_review_pr_dry_run_and_live_emit_identical_schema() -> None:
    """``--dry-run`` and live invocations must produce the same JSON keys.

    The only difference is the value of ``posted_comments`` /
    ``posted_reviews`` (0 on dry-run because nothing was posted) —
    every other field and the per-reviewer entry shape are identical,
    so the workflow's jq parsers and the local mirror consume the
    exact same contract.
    """
    import json

    runner = CliRunner()
    fake_team = _build_fake_team()

    with patch("repoach.cli.review_cmds.run_review", return_value=fake_team):
        dry_result = runner.invoke(review_app, ["pr", "999", "--dry-run"])
    fake_team_live = _build_fake_team()
    fake_team_live.posted_comments = 3
    fake_team_live.posted_reviews = 4
    with patch("repoach.cli.review_cmds.run_review", return_value=fake_team_live):
        live_result = runner.invoke(review_app, ["pr", "999"])

    dry_payload = json.loads(dry_result.stdout)
    live_payload = json.loads(live_result.stdout)

    assert dry_payload.keys() == live_payload.keys()
    assert dry_payload["reviews"][0].keys() == live_payload["reviews"][0].keys()
    for entry_dry, entry_live in zip(dry_payload["reviews"], live_payload["reviews"], strict=True):
        assert entry_dry.keys() == entry_live.keys()


def test_review_pr_exit_zero_on_approve_or_comment() -> None:
    """APPROVE and COMMENT verdicts must both exit 0 (only REQUEST_CHANGES exits 2)."""
    from repoach.review.reviewer import BotRole, ReviewVerdict

    runner = CliRunner()
    for verdict in (ReviewVerdict.APPROVE, ReviewVerdict.COMMENT):
        team = _FakeTeam(
            pr_number=42,
            final_verdict=verdict,
            n_blockers=0,
            n_majors=0,
            posted_comments=0,
            posted_reviews=4,
            reviews=[
                _FakeOutcome(role=BotRole.ARCHITECT, verdict=verdict, summary=""),
            ],
        )
        with patch("repoach.cli.review_cmds.run_review", return_value=team):
            result = runner.invoke(review_app, ["pr", "42", "--dry-run"])
        assert result.exit_code == 0, f"{verdict.value} must exit 0 ; got {result.exit_code}"


def test_review_pr_json_carries_each_finding_refutation_and_one_stderr_line() -> None:
    """Every finding the refuter saw is in ``findings`` with ``refutation = {state, reason}``."""
    import json

    from repoach.review.findings import ClaimType
    from repoach.review.refuter import JudgingReport, Refutation

    report = JudgingReport(
        outcomes=(
            Refutation(7, "a.py", 3, ClaimType.DESIGN, "refuted", "vague"),
            Refutation(
                8, "ci/x.sh", 1, ClaimType.SECURITY, "deferred", "evidence outside the diff"
            ),
            Refutation(
                9, "a.py", 5, ClaimType.MISSING_TEST, "unverified", "not yet mechanically verified"
            ),
        )
    )
    team = _build_fake_team()
    team.refutations = list(report.outcomes)
    team.refuter_summary = report.summary_line()
    with patch("repoach.cli.review_cmds.run_review", return_value=team):
        result = CliRunner().invoke(review_app, ["pr", "999", "--dry-run"])

    payload = json.loads(result.stdout)
    assert [f["refutation"] for f in payload["findings"]] == [
        {"state": "refuted", "reason": "vague"},
        {"state": "deferred", "reason": "evidence outside the diff"},
        {"state": "unverified", "reason": "not yet mechanically verified"},
    ]
    assert payload["findings"][1] == {
        "id": 8,
        "file": "ci/x.sh",
        "line": 1,
        "claim_type": "security",
        "refutation": {"state": "deferred", "reason": "evidence outside the diff"},
    }
    refuter_lines = [line for line in result.stderr.splitlines() if line.startswith("refuter:")]
    assert refuter_lines == [
        "refuter: judged 1, refuted 1, deferred 1 (evidence outside the diff), "
        "unverified 1 (not yet mechanically verified)"
    ]
