"""Unit tests for spec_gap routing through the refuter (SP-CLAIM-TYPE-ROUTING step 2).

Ensures ClaimType.SPEC_GAP is a judged claim type so it has a route
through the refuter path instead of being a dead enum value.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from ferova.review.refuter import JUDGED_CLAIM_TYPES, judge_findings_for_pr


def _spec_gap_finding() -> Finding:
    """Return a proposed spec_gap finding for a single PR."""
    return Finding(
        pr_number=1,
        head_sha="abc1234",
        round=1,
        finder="architect",
        claim_type=ClaimType.SPEC_GAP,
        severity="blocking",
        file="src/m.py",
        line_start=2,
        line_end=2,
        claim="This file has no matching spec entry — spec gap",
        evidence_pointer="src/m.py:2",
    )


def _seed_file(repo: Path, rel: str = "src/m.py") -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")


def test_spec_gap_is_judged(tmp_path: Path) -> None:
    """A proposed spec_gap finding reaches the refuter path.

    Asserts that ClaimType.SPEC_GAP is in JUDGED_CLAIM_TYPES and that
    judge_findings_for_pr includes a spec_gap finding in its judged_targets
    list, verified via a stub judge_factory that records the prompts it was
    asked to judge.
    """
    assert ClaimType.SPEC_GAP in JUDGED_CLAIM_TYPES

    db = tmp_path / "f.db"
    init_findings_schema(db)
    _seed_file(tmp_path)
    record_finding(db, _spec_gap_finding())

    prompts: list[str] = []

    def _factory():
        def _judge(prompt: str) -> str:
            prompts.append(prompt)
            return '{"refuted": false, "reasoning": "real spec gap — no coverage"}'

        return _judge

    counts = judge_findings_for_pr(
        db, pr_number=1, repo_root=tmp_path, head_sha="dead123", judge_factory=_factory
    )

    assert counts == {"verified": 1, "refuted": 0, "deferred": 0}
    assert len(prompts) == 1
    assert "spec_gap" in prompts[0]

    verified = fetch_findings(db, 1, status=FindingStatus.VERIFIED)
    assert len(verified) == 1
    assert verified[0].claim_type == ClaimType.SPEC_GAP
    assert verified[0].verification_method == "refuter"
