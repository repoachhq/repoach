"""Integration smoke test for the findings_bridge (SP-FINDER-OUTPUT)."""

from __future__ import annotations

from pathlib import Path

from ferova.review.findings import ClaimType, FindingStatus, Severity, fetch_findings
from ferova.review.findings_bridge import record_findings_for_outcomes
from ferova.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict


def _make_outcome(
    role: BotRole,
    comments: list[ReviewComment] | None = None,
    summary: str = "ok",
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary=summary,
        comments=comments or [],
    )


def test_smoke_bridge_records_valid_skips_garbage(tmp_path: Path) -> None:
    architect_outcome = _make_outcome(
        BotRole.ARCHITECT,
        comments=[
            ReviewComment(file="src/module.py", line=5, severity="blocker", body="Blocker issue"),
            ReviewComment(file="src/module.py", line=15, severity="nit", body="Nit issue"),
        ],
    )
    scribe_outcome = _make_outcome(
        BotRole.SCRIBE,
        comments=[],
        summary="[parse_failed:TRANSPORT] upstream refused the connection",
    )

    db = tmp_path / "findings.db"
    result = record_findings_for_outcomes(
        db,
        pr_number=101,
        head_sha="sha999",
        outcomes=[architect_outcome, scribe_outcome],
        round_n=1,
        diff="",
    )

    assert result == 2

    findings = fetch_findings(db, pr_number=101)
    assert len(findings) == 2
    for f in findings:
        assert f.finder == "architect"
        assert f.claim_type == ClaimType.DESIGN
        assert f.status == FindingStatus.PROPOSED

    severities = {f.severity for f in findings}
    assert Severity.BLOCKING in severities
    assert Severity.ADVISORY in severities
