"""Unit tests for spec_gap routing through the refuter (SP-CLAIM-TYPE-ROUTING step 2).

Ensures ClaimType.SPEC_GAP is a judged claim type so it has a route
through the refuter path instead of being a dead enum value.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
    record_verification_attempt,
)
from repoach.review.refuter import _MAX_JUDGED, JUDGED_CLAIM_TYPES, judge_findings_for_pr


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
            return 'VERDICT: {"refuted": false, "reasoning": "real spec gap — no coverage"}'

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


def _missing_test_finding() -> Finding:
    """Return a proposed missing_test finding for a single PR."""
    return Finding(
        pr_number=1,
        head_sha="abc1234",
        round=1,
        finder="tester",
        claim_type=ClaimType.MISSING_TEST,
        severity="blocking",
        file="src/m.py",
        line_start=2,
        line_end=2,
        claim="No test covers this branch",
        evidence_pointer="src/m.py:2",
    )


def test_mechanical_deferral_falls_back_to_judge(tmp_path: Path) -> None:
    """A proposed missing_test finding with verify_attempts=1 lands in the judged target list.

    The mechanical verifier deferred this finding (no checkable symbol),
    so verify_attempts was incremented to 1. The refuter judge fallback
    should pick it up in the same run and the stubbed judge decision
    should persist its status.
    """
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _seed_file(tmp_path)
    fid = record_finding(db, _missing_test_finding())
    record_verification_attempt(
        db,
        fid,
        method="symbol_search",
        result="no checkable symbol",
        checked_at_sha="abc1234",
    )

    prompts: list[str] = []

    def _factory():
        def _judge(prompt: str) -> str:
            prompts.append(prompt)
            return 'VERDICT: {"refuted": false, "reasoning": "real missing test"}'

        return _judge

    counts = judge_findings_for_pr(
        db, pr_number=1, repo_root=tmp_path, head_sha="dead123", judge_factory=_factory
    )

    assert counts == {"verified": 1, "refuted": 0, "deferred": 0}
    assert len(prompts) == 1
    assert "missing_test" in prompts[0]

    verified = fetch_findings(db, 1, status=FindingStatus.VERIFIED)
    assert len(verified) == 1
    assert verified[0].claim_type == ClaimType.MISSING_TEST
    assert verified[0].verification_method == "refuter"


def test_judge_fallback_respects_cap(tmp_path: Path) -> None:
    """With _MAX_JUDGED judged-type findings already queued, a deferred mechanical finding does not extend the list beyond _MAX_JUDGED.

    The primary judged claim types fill the cap; the fallback target
    must be deferred (not judged) so the per-run judge budget is not
    exceeded.
    """
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _seed_file(tmp_path)

    for i in range(_MAX_JUDGED):
        record_finding(
            db,
            Finding(
                pr_number=1,
                head_sha="abc1234",
                round=1,
                finder="architect",
                claim_type=ClaimType.DESIGN,
                severity="blocking",
                file="src/m.py",
                line_start=2,
                line_end=2,
                claim=f"design finding {i}",
                evidence_pointer="src/m.py:2",
            ),
        )

    fid = record_finding(db, _missing_test_finding())
    record_verification_attempt(
        db,
        fid,
        method="symbol_search",
        result="no checkable symbol",
        checked_at_sha="abc1234",
    )

    prompts: list[str] = []

    def _factory():
        def _judge(prompt: str) -> str:
            prompts.append(prompt)
            return 'VERDICT: {"refuted": false, "reasoning": "should not be called"}'

        return _judge

    counts = judge_findings_for_pr(
        db, pr_number=1, repo_root=tmp_path, head_sha="dead123", judge_factory=_factory
    )

    assert len(prompts) == _MAX_JUDGED
    assert counts["deferred"] >= 1
    assert counts["verified"] + counts["refuted"] == _MAX_JUDGED

    proposed = fetch_findings(db, 1, status=FindingStatus.PROPOSED)
    assert len(proposed) == 1
    assert proposed[0].claim_type == ClaimType.MISSING_TEST
