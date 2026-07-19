"""Integration test for SP-PROPOSED-ESCALATION end-to-end.

Exercises the full pipeline: a BLOCKING missing_test finding whose
promised symbol resolves nowhere is recorded, verified twice across
two review runs, and on the second run the assessor names it for
escalation while the dossier builder renders the expected shape.

Hermetic: no network, no LLM, no .env file.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.finding_verifiers import verify_findings_for_pr
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.stuck import (
    PROPOSED_ESCALATION_ATTEMPTS,
    assess_proposed_escalation,
    build_proposed_escalation_dossier,
    select_newly_escalated,
)


def test_frozen_proposed_finding_escalates_end_to_end(tmp_path: Path) -> None:
    """A BLOCKING missing_test with no checkable symbol escalates after 2 attempts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "findings.db"
    init_findings_schema(db)

    finding = Finding(
        pr_number=1,
        head_sha="head123",
        round=1,
        finder="architect",
        claim_type=ClaimType.MISSING_TEST,
        severity=Severity.BLOCKING,
        file="src/m.py",
        line_start=3,
        line_end=3,
        claim="missing test for foo",
        evidence_pointer="src/m.py:3",
        status=FindingStatus.PROPOSED,
    )
    fid = record_finding(db, finding)

    counts = verify_findings_for_pr(db, pr_number=1, repo_root=repo, head_sha="head123")
    assert counts["deferred"] == 1

    after_first = fetch_findings(db, 1)
    assert len(after_first) == 1
    assert after_first[0].verify_attempts == 1
    assert after_first[0].verification_method == "symbol_search"
    assert after_first[0].verification_result == "no checkable symbol"
    assert after_first[0].status is FindingStatus.PROPOSED

    assert assess_proposed_escalation(after_first) == []

    counts2 = verify_findings_for_pr(db, pr_number=1, repo_root=repo, head_sha="head123")
    assert counts2["deferred"] == 1

    after_second = fetch_findings(db, 1)
    assert len(after_second) == 1
    assert after_second[0].verify_attempts == 2
    assert after_second[0].status is FindingStatus.PROPOSED

    eligible = assess_proposed_escalation(after_second)
    assert len(eligible) == 1
    assert eligible[0].id == fid

    newly = select_newly_escalated(eligible)
    assert len(newly) == 1
    assert newly[0].id == fid

    dossier = build_proposed_escalation_dossier(newly)
    assert dossier["kind"] == "proposed_escalation"
    assert len(dossier["findings"]) == 1
    entry = dossier["findings"][0]
    assert entry["id"] == fid
    assert entry["claim"] == "missing test for foo"
    assert entry["claim_type"] == "missing_test"
    assert entry["verify_attempts"] == PROPOSED_ESCALATION_ATTEMPTS
    assert entry["verification_method"] == "symbol_search"
    assert entry["verification_result"] == "no checkable symbol"
