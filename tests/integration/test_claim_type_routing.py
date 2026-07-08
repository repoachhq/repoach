"""Integration test for end-to-end claim type routing (SP-CLAIM-TYPE-ROUTING).

Exercises the full pipeline: content cue classification in the bridge,
fail-closed routing in the merge gate, and the decision outcome.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.findings import (
    ClaimType,
    init_findings_schema,
    record_finding,
    record_review_integrity,
)
from ferova.review.findings_bridge import comment_to_finding
from ferova.review.merge_gate import compute_merge_decision, gather_merge_facts
from ferova.review.reviewer import BotRole, ReviewComment


def test_content_cue_routes_to_correct_verifier_and_gate(tmp_path: Path) -> None:
    """A Tester comment with a race cue classifies as broken_behavior and blocks merge."""
    db = tmp_path / "findings.db"
    init_findings_schema(db)

    comment = ReviewComment(
        file="src/module.py",
        line=10,
        severity="blocker",
        body="this branch can drop the lock — race under concurrent writes",
    )

    finding = comment_to_finding(
        comment,
        role=BotRole.TESTER,
        pr_number=42,
        head_sha="abc123",
        round_n=1,
    )

    assert finding.claim_type == ClaimType.BROKEN_BEHAVIOR
    assert finding.claim_type != ClaimType.MISSING_TEST

    record_finding(db, finding)
    record_review_integrity(
        db,
        pr_number=42,
        head_sha="abc123",
        n_reviewers=4,
        n_unparsed=0,
    )

    facts = gather_merge_facts(
        db,
        pr_number=42,
        repo_root=tmp_path,
        head_sha="abc123",
        ci_green=True,
    )

    assert len(facts.blocking_unverified) == 1
    assert "broken_behavior" in facts.blocking_unverified[0]

    decision = compute_merge_decision(facts)
    assert decision.merge is False
    assert any("unverified blocking" in r for r in decision.reasons)
