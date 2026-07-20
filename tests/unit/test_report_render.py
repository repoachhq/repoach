"""Tests for SP-VERDICT-FLIP (10a) — the ledger-rendered review report.

The report headline reflects the pure-gate decision, the facts table
mirrors the merge facts, findings are grouped by status, and the legacy
per-reviewer verdict is preserved verbatim under the informational
section.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from repoach.review.merge_gate import MergeDecision, MergeFacts
from repoach.review.report import render_ledger_report


def _facts(**over: object) -> MergeFacts:
    base = {
        "head_sha": "abcdef0123456789",
        "ci_green": True,
        "open_blocking_findings": 0,
        "spec_covered": True,
        "spec_coverage_known": True,
        "review_complete": True,
        "review_integrity_known": True,
    }
    base.update(over)
    return MergeFacts(**base)


def _finding(claim_type: ClaimType, *, severity: str, claim: str) -> Finding:
    return Finding(
        pr_number=1,
        head_sha="abcdef0123456789",
        round=1,
        finder="architect",
        claim_type=claim_type,
        severity=severity,
        file="src/m.py",
        line_start=10,
        line_end=10,
        claim=claim,
        evidence_pointer="src/m.py:10",
    )


def test_headline_merge_ready(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    body = render_ledger_report(
        db,
        pr_number=1,
        decision=MergeDecision(merge=True, reasons=[]),
        facts=_facts(),
    )
    assert "## Decision: MERGE-READY at `abcdef012345`" in body
    assert "_No findings recorded._" in body


def test_headline_blocked_lists_reasons(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    decision = MergeDecision(merge=False, reasons=["CI not green at head", "1 open blocking"])
    body = render_ledger_report(
        db,
        pr_number=1,
        decision=decision,
        facts=_facts(ci_green=False, open_blocking_findings=1),
    )
    assert "## Decision: BLOCKED at `abcdef012345`" in body
    assert "CI not green at head; 1 open blocking" in body
    assert "| CI green | False |" in body
    assert "| Open blocking findings | 1 |" in body


def test_facts_table_renders_na_when_unknown(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    body = render_ledger_report(
        db,
        pr_number=1,
        decision=MergeDecision(merge=True, reasons=[]),
        facts=_facts(spec_coverage_known=False, review_integrity_known=False),
    )
    assert "| Spec covered | n/a |" in body
    assert "| Review complete | n/a |" in body


def test_findings_grouped_by_status(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(db, _finding(ClaimType.MISSING_TEST, severity="blocking", claim="needs a test"))
    refuted = record_finding(
        db, _finding(ClaimType.DESIGN, severity="blocking", claim="hallucinated smell")
    )
    update_finding_status(db, refuted, FindingStatus.REFUTED, verification_method="refuter")

    body = render_ledger_report(
        db,
        pr_number=1,
        decision=MergeDecision(merge=False, reasons=["1 open blocking"]),
        facts=_facts(open_blocking_findings=1),
    )
    assert f"### {FindingStatus.PROPOSED.value}" in body
    assert f"### {FindingStatus.REFUTED.value}" in body
    assert "needs a test" in body
    assert "`src/m.py:10`" in body
    assert "evidence: src/m.py:10" in body


def test_archive_appendix_preserved(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    appendix = "### Repoach review archive\n\n```json\n{}\n```"
    body = render_ledger_report(
        db,
        pr_number=1,
        decision=MergeDecision(merge=True, reasons=[]),
        facts=_facts(),
        archive_appendix=appendix,
    )
    assert appendix in body
    assert body.index("## Decision:") < body.index(appendix)


def test_archive_appendix_omitted_when_empty(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    body = render_ledger_report(
        db,
        pr_number=1,
        decision=MergeDecision(merge=True, reasons=[]),
        facts=_facts(),
    )
    assert "review archive" not in body
