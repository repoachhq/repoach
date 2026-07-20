"""Unit tests for findings_bridge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from repoach.review.findings import ClaimType, FindingStatus, Severity, fetch_findings
from repoach.review.findings_bridge import (
    LENS_DEFAULT_CLAIM_TYPE,
    SEVERITY_MAP,
    comment_to_finding,
    record_findings_for_outcomes,
)
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

_DIFF_TOUCHING_FOO = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


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


def test_content_cues_override_lens_default() -> None:
    security_comment = ReviewComment(
        file="src/foo.py",
        line=1,
        severity="blocker",
        body="This looks like an auth bypass vulnerability, not a test issue.",
    )
    finding = comment_to_finding(
        security_comment, role=BotRole.TESTER, pr_number=1, head_sha="sha", round_n=1
    )
    assert finding.claim_type == ClaimType.SECURITY

    test_comment = ReviewComment(
        file="src/bar.py",
        line=2,
        severity="major",
        body="There is missing test coverage for this branch.",
    )
    finding = comment_to_finding(
        test_comment, role=BotRole.SENTINEL, pr_number=1, head_sha="sha", round_n=1
    )
    assert finding.claim_type == ClaimType.MISSING_TEST


def test_no_cue_keeps_lens_default() -> None:
    comment = ReviewComment(
        file="src/baz.py",
        line=3,
        severity="nit",
        body="This could be tidied up a little.",
    )
    for role, expected in LENS_DEFAULT_CLAIM_TYPE.items():
        finding = comment_to_finding(comment, role=role, pr_number=1, head_sha="sha", round_n=1)
        assert finding.claim_type == expected


def test_lens_default_claim_types() -> None:
    assert LENS_DEFAULT_CLAIM_TYPE[BotRole.ARCHITECT] == ClaimType.DESIGN
    assert LENS_DEFAULT_CLAIM_TYPE[BotRole.SENTINEL] == ClaimType.SECURITY
    assert LENS_DEFAULT_CLAIM_TYPE[BotRole.TESTER] == ClaimType.MISSING_TEST
    assert LENS_DEFAULT_CLAIM_TYPE[BotRole.SCRIBE] == ClaimType.MISSING_DOCSTRING


def test_severity_mapping() -> None:
    assert SEVERITY_MAP["blocker"] == Severity.BLOCKING
    assert SEVERITY_MAP["major"] == Severity.BLOCKING
    assert SEVERITY_MAP["minor"] == Severity.ADVISORY
    assert SEVERITY_MAP["nit"] == Severity.ADVISORY
    comment = ReviewComment(file="f.py", line=1, severity="unknown", body="x")
    finding = comment_to_finding(
        comment, role=BotRole.ARCHITECT, pr_number=1, head_sha="sha", round_n=1
    )
    assert finding.severity == Severity.ADVISORY


def test_record_findings_round_trip(tmp_path: Path) -> None:
    comment_a = ReviewComment(file="src/foo.py", line=10, severity="blocker", body="Issue A")
    comment_b = ReviewComment(file="src/bar.py", line=20, severity="nit", body="Issue B")
    outcome = _make_outcome(BotRole.ARCHITECT, comments=[comment_a, comment_b])
    db = tmp_path / "findings.db"

    count = record_findings_for_outcomes(
        db, pr_number=42, head_sha="abc123", outcomes=[outcome], round_n=1, diff=""
    )

    assert count == 2
    findings = fetch_findings(db, pr_number=42)
    assert len(findings) == 2
    assert findings[0].finder == "architect"
    assert findings[0].claim_type == ClaimType.DESIGN
    assert findings[0].severity == Severity.BLOCKING
    assert findings[1].severity == Severity.ADVISORY
    assert findings[0].status == FindingStatus.PROPOSED
    assert findings[1].status == FindingStatus.PROPOSED


def test_unparsed_outcomes_skipped(tmp_path: Path) -> None:
    outcome = _make_outcome(
        BotRole.SCRIBE,
        comments=[],
        summary="[parse_failed:TRANSPORT] upstream refused the connection",
    )
    count = record_findings_for_outcomes(
        tmp_path / "findings.db",
        pr_number=1,
        head_sha="deadbeef",
        outcomes=[outcome],
        round_n=1,
        diff="",
    )
    assert count == 0


def test_none_head_sha_empty(tmp_path: Path) -> None:
    comment = ReviewComment(file="src/x.py", line=1, severity="minor", body="Check this")
    outcome = _make_outcome(BotRole.ARCHITECT, comments=[comment])
    db = tmp_path / "findings.db"

    record_findings_for_outcomes(
        db, pr_number=99, head_sha=None, outcomes=[outcome], round_n=1, diff=""
    )

    findings = fetch_findings(db, pr_number=99)
    assert len(findings) == 1
    assert findings[0].head_sha == ""


def test_off_diff_comment_skipped(tmp_path: Path) -> None:
    in_diff = ReviewComment(file="src/foo.py", line=3, severity="blocker", body="real")
    off_diff = ReviewComment(
        file="src/repoach/review/coder_loop.py",
        line=9,
        severity="blocker",
        body="hallucinated on an untouched file",
    )
    outcome = _make_outcome(BotRole.ARCHITECT, comments=[in_diff, off_diff])
    db = tmp_path / "findings.db"

    count = record_findings_for_outcomes(
        db, pr_number=1, head_sha="sha", outcomes=[outcome], round_n=1, diff=_DIFF_TOUCHING_FOO
    )

    assert count == 1
    findings = fetch_findings(db, pr_number=1)
    assert len(findings) == 1
    assert findings[0].file == "src/foo.py"


def test_off_diff_missing_test_skipped(tmp_path: Path) -> None:
    off_diff = ReviewComment(file="src/other.py", line=1, severity="major", body="add a test")
    outcome = _make_outcome(BotRole.TESTER, comments=[off_diff])
    db = tmp_path / "findings.db"

    count = record_findings_for_outcomes(
        db, pr_number=2, head_sha="sha", outcomes=[outcome], round_n=1, diff=_DIFF_TOUCHING_FOO
    )

    assert count == 0
    assert fetch_findings(db, pr_number=2) == []


def test_in_diff_comments_recorded(tmp_path: Path) -> None:
    comments = [
        ReviewComment(file="src/foo.py", line=3, severity="blocker", body="a"),
        ReviewComment(file="src/foo.py", line=8, severity="nit", body="b"),
    ]
    outcome = _make_outcome(BotRole.ARCHITECT, comments=comments)
    db = tmp_path / "findings.db"

    count = record_findings_for_outcomes(
        db, pr_number=3, head_sha="sha", outcomes=[outcome], round_n=1, diff=_DIFF_TOUCHING_FOO
    )

    assert count == len(comments)
    assert len(fetch_findings(db, pr_number=3)) == len(comments)


def test_empty_diff_disables_filter(tmp_path: Path) -> None:
    comments = [
        ReviewComment(file="src/foo.py", line=3, severity="blocker", body="a"),
        ReviewComment(file="src/elsewhere.py", line=1, severity="major", body="b"),
    ]
    outcome = _make_outcome(BotRole.ARCHITECT, comments=comments)
    db = tmp_path / "findings.db"

    count = record_findings_for_outcomes(
        db, pr_number=4, head_sha="sha", outcomes=[outcome], round_n=1, diff=""
    )

    assert count == len(comments)
    assert len(fetch_findings(db, pr_number=4)) == len(comments)


def test_unparsed_skipped_with_diff(tmp_path: Path) -> None:
    outcome = _make_outcome(BotRole.SCRIBE, comments=[], summary="[parse_failed:TRANSPORT] nope")
    count = record_findings_for_outcomes(
        tmp_path / "findings.db",
        pr_number=5,
        head_sha="sha",
        outcomes=[outcome],
        round_n=1,
        diff=_DIFF_TOUCHING_FOO,
    )
    assert count == 0


def test_off_diff_skip_emits_positive_event(tmp_path: Path) -> None:
    off_diff = ReviewComment(file="src/elsewhere.py", line=1, severity="blocker", body="x")
    outcome = _make_outcome(BotRole.ARCHITECT, comments=[off_diff])
    with patch("repoach.review.findings_bridge._log", MagicMock()) as mock_log:
        record_findings_for_outcomes(
            tmp_path / "findings.db",
            pr_number=7,
            head_sha="sha",
            outcomes=[outcome],
            round_n=1,
            diff=_DIFF_TOUCHING_FOO,
        )
    mock_log.info.assert_called_once_with(
        "findings_bridge.off_diff_skipped", pr_number=7, n_skipped=1
    )


def test_no_skip_no_log(tmp_path: Path) -> None:
    on_diff = ReviewComment(file="src/foo.py", line=3, severity="blocker", body="x")
    outcome = _make_outcome(BotRole.ARCHITECT, comments=[on_diff])
    with patch("repoach.review.findings_bridge._log", MagicMock()) as mock_log:
        record_findings_for_outcomes(
            tmp_path / "findings.db",
            pr_number=8,
            head_sha="sha",
            outcomes=[outcome],
            round_n=1,
            diff=_DIFF_TOUCHING_FOO,
        )
    mock_log.info.assert_not_called()
