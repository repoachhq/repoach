"""Unit tests for SP-MERGE-GATE-SHADOW — the pure evidence-first gate.

The decision function is pinned on every condition; fact-gathering is
pinned against a real ledger + tmp head: mechanical blocking findings
are re-verified on disk at head (their stored status is a hint), judged
blocking findings count only when verified AND fresh at this head, and
settled findings never count.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.diff_scoper import ScopedDiff
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    init_findings_schema,
    record_finding,
    record_review_integrity,
    update_finding_status,
)
from repoach.review.findings_bridge import record_oversized_findings
from repoach.review.merge_gate import (
    _JUDGED_TYPES,
    MergeFacts,
    compute_merge_decision,
    gather_merge_facts,
    summarise_ledger_facts,
    verdict_from_facts,
)
from repoach.review.refuter import JUDGED_CLAIM_TYPES
from repoach.review.reviewer import ReviewVerdict
from repoach.review.spec_gate import SpecCoverage, record_spec_coverage


def _facts(**over: object) -> MergeFacts:
    base = {
        "head_sha": "head123",
        "ci_green": True,
        "open_blocking_findings": 0,
        "spec_covered": True,
        "spec_coverage_known": True,
        "review_complete": True,
        "review_integrity_known": True,
    }
    base.update(over)
    return MergeFacts(**base)


def test_decision_merges_when_all_green() -> None:
    decision = compute_merge_decision(_facts())
    assert decision.merge is True
    assert decision.reasons == []


def test_decision_blocks_on_each_condition() -> None:
    assert compute_merge_decision(_facts(head_sha="")).merge is False
    assert compute_merge_decision(_facts(ci_green=False)).merge is False
    assert compute_merge_decision(_facts(open_blocking_findings=2)).merge is False
    assert compute_merge_decision(_facts(spec_covered=False)).merge is False
    assert compute_merge_decision(_facts(review_integrity_known=False)).merge is False
    assert compute_merge_decision(_facts(review_complete=False)).merge is False


def test_verdict_from_facts_approves_clean_ledger() -> None:
    assert verdict_from_facts(_facts()) is ReviewVerdict.APPROVE


def test_verdict_from_facts_requests_changes_on_open_blocking() -> None:
    assert verdict_from_facts(_facts(open_blocking_findings=1)) is ReviewVerdict.REQUEST_CHANGES


def test_verdict_from_facts_requests_changes_on_incomplete_review() -> None:
    assert verdict_from_facts(_facts(review_complete=False)) is ReviewVerdict.REQUEST_CHANGES


def test_verdict_from_facts_ignores_ci_and_spec() -> None:
    assert verdict_from_facts(_facts(ci_green=False)) is ReviewVerdict.APPROVE
    assert verdict_from_facts(_facts(spec_covered=False)) is ReviewVerdict.APPROVE


def test_decision_ignores_coverage_when_unknown() -> None:
    decision = compute_merge_decision(_facts(spec_covered=False, spec_coverage_known=False))
    assert decision.merge is True


def _finding(
    claim_type: ClaimType,
    *,
    severity: str = "blocking",
    file: str = "src/m.py",
    claim: str = "smell",
) -> Finding:
    return Finding(
        pr_number=1,
        head_sha="head123",
        round=1,
        finder="architect",
        claim_type=claim_type,
        severity=severity,
        file=file,
        line_start=1,
        line_end=1,
        claim=claim,
        evidence_pointer=f"{file}:1",
    )


def _to_stuck(db: Path, fid: int, *, checked_at_sha: str = "head123") -> None:
    for status in (FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.STUCK):
        update_finding_status(
            db, fid, status, verification_method="refuter", checked_at_sha=checked_at_sha
        )


def test_gather_judged_stuck_finding_blocks(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    sid = record_finding(db, _finding(ClaimType.SECURITY, claim="unfixed vuln"))
    _to_stuck(db, sid, checked_at_sha="head123")

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 1
    assert compute_merge_decision(facts).merge is False


def test_gather_mechanical_stuck_finding_still_reverifies_and_blocks(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fid = record_finding(
        db,
        _finding(ClaimType.MISSING_TEST, file="tests/test_absent.py", claim="missing test_absent"),
    )
    _to_stuck(db, fid)

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 1


def test_summarise_ledger_counts_stuck(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fid = record_finding(db, _finding(ClaimType.DESIGN, claim="stuck design"))
    _to_stuck(db, fid, checked_at_sha="head123")

    facts = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert facts.open_blocking_findings == 1
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES


def test_gather_reverifies_mechanical_findings_at_head(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_present():\n    assert True\n", encoding="utf-8"
    )
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="missing test_present"))
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="missing test_absent_now"))

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 0
    assert len(facts.blocking_unverified) == 2


def test_gather_judged_counts_fresh_and_stale(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fresh_id = record_finding(db, _finding(ClaimType.DESIGN, claim="fresh"))
    stale_id = record_finding(db, _finding(ClaimType.SECURITY, claim="stale"))
    update_finding_status(
        db,
        fresh_id,
        FindingStatus.VERIFIED,
        verification_method="refuter",
        checked_at_sha="head123",
    )
    update_finding_status(
        db, stale_id, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="old999"
    )

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 2


def test_gather_skips_settled_and_advisory(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    refuted_id = record_finding(db, _finding(ClaimType.DESIGN, claim="hallucinated"))
    update_finding_status(db, refuted_id, FindingStatus.REFUTED, verification_method="refuter")
    advisory_id = record_finding(db, _finding(ClaimType.DESIGN, severity="advisory", claim="nit"))
    update_finding_status(
        db,
        advisory_id,
        FindingStatus.VERIFIED,
        verification_method="refuter",
        checked_at_sha="head123",
    )

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 0


def test_incomplete_review_not_approved(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 0
    assert facts.review_complete is False
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES
    assert compute_merge_decision(facts).merge is False


def test_gather_review_integrity_fresh_and_complete(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=0)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_integrity_known is True
    assert facts.review_complete is True
    assert compute_merge_decision(facts).merge is True


def test_gather_review_integrity_unparsed_blocks(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=2)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_complete is False
    assert compute_merge_decision(facts).merge is False


def test_gather_review_integrity_stale_head_unknown(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="old999", n_reviewers=4, n_unparsed=0)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_integrity_known is False
    assert compute_merge_decision(facts).merge is False


def test_gather_reads_spec_coverage(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_spec_coverage(
        db,
        pr_number=1,
        head_sha="head123",
        coverage=SpecCoverage(
            spec_id="SP-X", n_promised=2, n_present=1, missing=["t::x"], covered=False
        ),
    )
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.spec_coverage_known is True
    assert facts.spec_covered is False
    assert compute_merge_decision(facts).merge is False


def test_summarise_trusts_recorded_status_without_disk(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    proposed = record_finding(db, _finding(ClaimType.MISSING_TEST, claim="not verified yet"))
    verified = record_finding(db, _finding(ClaimType.MISSING_TEST, claim="confirmed missing"))
    update_finding_status(
        db, verified, FindingStatus.VERIFIED, verification_method="grep", checked_at_sha="head123"
    )
    assert proposed != verified

    facts = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert facts.open_blocking_findings == 1


def test_summarise_judged_counts_stale_and_skips_settled(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fresh = record_finding(db, _finding(ClaimType.DESIGN, claim="fresh"))
    stale = record_finding(db, _finding(ClaimType.SECURITY, claim="stale"))
    refuted = record_finding(db, _finding(ClaimType.DESIGN, claim="hallucinated"))
    update_finding_status(
        db, fresh, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="head123"
    )
    update_finding_status(
        db, stale, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="old999"
    )
    update_finding_status(db, refuted, FindingStatus.REFUTED, verification_method="refuter")

    facts = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert facts.open_blocking_findings == 2


def test_open_judged_blocking_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fid = record_finding(db, _finding(ClaimType.SECURITY, claim="unresolved by failed fix"))
    update_finding_status(
        db, fid, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="head123"
    )
    update_finding_status(db, fid, FindingStatus.OPEN, verification_method="coder")

    gathered = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert gathered.open_blocking_findings == 1
    assert compute_merge_decision(gathered).merge is False
    assert verdict_from_facts(gathered) is ReviewVerdict.REQUEST_CHANGES

    summarised = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert summarised.open_blocking_findings == 1


def test_stale_sha_judged_blocking_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fid = record_finding(db, _finding(ClaimType.SECURITY, claim="verified before the push"))
    update_finding_status(
        db, fid, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="old999"
    )
    record_review_integrity(db, pr_number=1, head_sha="newhead", n_reviewers=4, n_unparsed=0)

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="newhead", ci_green=True
    )
    assert facts.open_blocking_findings == 1
    assert compute_merge_decision(facts).merge is False
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES


def test_spec_coverage_pinned_to_head(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_spec_coverage(
        db,
        pr_number=1,
        head_sha="old999",
        coverage=SpecCoverage(spec_id="SP-X", n_promised=2, n_present=2, missing=[], covered=True),
    )
    stale = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert stale.spec_covered is False
    assert stale.spec_coverage_known is False

    record_spec_coverage(
        db,
        pr_number=1,
        head_sha="head123",
        coverage=SpecCoverage(spec_id="SP-X", n_promised=2, n_present=2, missing=[], covered=True),
    )
    fresh = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert fresh.spec_covered is True
    assert fresh.spec_coverage_known is True


def test_summarise_ci_green_from_broken_behavior(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    facts = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert facts.ci_green is True

    ci_fail = record_finding(
        db, _finding(ClaimType.BROKEN_BEHAVIOR, claim="CI check failed: tests")
    )
    update_finding_status(
        db, ci_fail, FindingStatus.VERIFIED, verification_method="ci", checked_at_sha="head123"
    )
    red = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert red.ci_green is False
    assert red.open_blocking_findings == 0

    update_finding_status(
        db, ci_fail, FindingStatus.OPEN, verification_method="ci", checked_at_sha="head123"
    )
    update_finding_status(db, ci_fail, FindingStatus.RESOLVED, verification_method="pytest")
    green = summarise_ledger_facts(db, pr_number=1, head_sha="head123")
    assert green.ci_green is True


def test_gather_review_integrity_clean_rerun_redeems_flaked_head(tmp_path: Path) -> None:
    """A clean re-review at the same head outweighs an earlier flaked run.

    Observed live on PR #2 (2026-07-03): a transport-flaked round
    (n_unparsed=1) followed by a clean 4/0 re-review at the SAME head
    left the gate refusing forever, because the aggregation required
    ALL integrity rows at the head to be clean. One clean complete run
    is proof that a complete review happened at this head.
    """
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=1)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=0)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_complete is True
    assert compute_merge_decision(facts).merge is True


def test_missing_ledger_reason_names_artifact_transport(tmp_path: Path) -> None:
    """An empty ledger points at the artifact, not at a review that never ran."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    decision = compute_merge_decision(facts)
    assert decision.merge is False
    assert any("artifact missing or not transported" in r for r in decision.reasons)


def test_stale_head_reason_names_the_older_head(tmp_path: Path) -> None:
    """Integrity rows at another head yield the older-head refusal, not the artifact one."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="old999", n_reviewers=4, n_unparsed=0)
    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    decision = compute_merge_decision(facts)
    assert decision.merge is False
    assert any("review ran at an older head" in r for r in decision.reasons)


def test_blocking_unverified_refuses_merge() -> None:
    facts = _facts(blocking_unverified=["finding 42: spec_gap is PROPOSED at head"])
    decision = compute_merge_decision(facts)
    assert decision.merge is False
    assert any("finding 42" in r for r in decision.reasons)


def test_gate_judged_partition_matches_refuter() -> None:
    """SP-CLAIM-TYPE-PARTITION-ALIGN AC1: one shared judged-type partition.

    The gate's ``_JUDGED_TYPES`` must equal the refuter's
    ``JUDGED_CLAIM_TYPES`` exactly — including ``spec_gap`` — so a claim
    type the refuter judges can never silently fall outside what the
    gate counts as an open blocking finding.
    """
    assert _JUDGED_TYPES == JUDGED_CLAIM_TYPES
    assert frozenset({ClaimType.DESIGN, ClaimType.SECURITY, ClaimType.SPEC_GAP}) == _JUDGED_TYPES


def test_verified_spec_gap_blocks_merge(tmp_path: Path) -> None:
    """SP-CLAIM-TYPE-PARTITION-ALIGN AC2: a refuter-VERIFIED blocking

    ``spec_gap`` finding, fresh at head, counts as an open blocking
    finding and refuses the merge — it must never land in
    ``blocking_unverified`` as "no verifier" (the pre-fix behaviour,
    since ``spec_gap`` sat outside the gate's misaligned partition).
    """
    db = tmp_path / "f.db"
    init_findings_schema(db)
    fid = record_finding(
        db, _finding(ClaimType.SPEC_GAP, claim="acceptance selector missing from the diff")
    )
    update_finding_status(
        db, fid, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="head123"
    )

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 1
    assert facts.blocking_unverified == []
    assert compute_merge_decision(facts).merge is False
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES


def test_oversized_omitted_file_blocks_merge(tmp_path: Path) -> None:
    """SP-DIFF-SCOPER-OVERSIZE-BLOCK AC2: an oversized-omitted file fails closed.

    A `ScopedDiff` naming one file too large to show any reviewer
    records a BLOCKING finding via :func:`record_oversized_findings`;
    :func:`gather_merge_facts` + :func:`compute_merge_decision` at head
    must then refuse the merge — an unreviewed file cannot be padded
    past the cap and merged anyway.
    """
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=0)

    scoped = ScopedDiff(
        prompt_diff="",
        included=[],
        omitted=["src/huge.py"],
        oversized=["src/huge.py"],
    )
    record_oversized_findings(db, pr_number=1, head_sha="head123", round_n=1, scoped=scoped)

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 1
    assert compute_merge_decision(facts).merge is False


def test_cap_fit_only_files_not_blocked_by_oversized_rule(tmp_path: Path) -> None:
    """Companion to AC2: a diff with only cap-fit files records nothing and merges clean."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=1, head_sha="head123", n_reviewers=4, n_unparsed=0)

    scoped = ScopedDiff(
        prompt_diff="diff --git a/src/small.py b/src/small.py\n",
        included=["src/small.py"],
        omitted=[],
        oversized=[],
    )
    n_recorded = record_oversized_findings(
        db, pr_number=1, head_sha="head123", round_n=1, scoped=scoped
    )
    assert n_recorded == 0

    facts = gather_merge_facts(
        db, pr_number=1, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.open_blocking_findings == 0
    assert compute_merge_decision(facts).merge is True
