"""Render the PR review report from the findings ledger.

SP-VERDICT-FLIP (redesign slice 10a): the sticky archive comment stops
being the verdict authority and becomes a *report* built from the
findings ledger and the re-verified merge facts. The pure gate
(:func:`auto_merge.evaluate_merge_gate`) owns the merge decision; this
module only presents it.

An optional ``archive_appendix`` carries the machine-readable
``TeamOutcome`` JSON (consumed by ``repoach review report``), the
hallucination-guard section, and the dialogue transcript below the
report. The legacy per-reviewer verdict framing was dropped in 10b-4
(SP-RETIRE-VERDICT-ARCHIVE) — the verdict is no longer the authority.
"""

from __future__ import annotations

from pathlib import Path

from .findings import Finding, fetch_findings
from .merge_gate import MergeDecision, MergeFacts

LEDGER_REPORT_HEADER: str = "### Repoach review report"


def _short_sha(head_sha: str) -> str:
    """Return the 12-char prefix of a SHA, or ``unknown`` when empty."""
    return head_sha[:12] if head_sha else "unknown"


def _decision_header(decision: MergeDecision, head_sha: str) -> str:
    """Render the headline reflecting the pure-gate decision."""
    where = _short_sha(head_sha)
    if decision.merge:
        return f"## Decision: MERGE-READY at `{where}`"
    reasons = "; ".join(decision.reasons) or "blocked"
    return f"## Decision: BLOCKED at `{where}` — {reasons}"


def _facts_table(facts: MergeFacts) -> str:
    """Render the re-verified merge facts as a markdown table."""
    spec = facts.spec_covered if facts.spec_coverage_known else "n/a"
    review = facts.review_complete if facts.review_integrity_known else "n/a"
    rows = [
        ("CI green", facts.ci_green),
        ("Open blocking findings", facts.open_blocking_findings),
        ("Spec covered", spec),
        ("Review complete", review),
    ]
    lines = ["| Fact | Value |", "| --- | --- |"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    return "\n".join(lines)


def _finding_line(finding: Finding) -> str:
    """Render one finding as a markdown bullet with location + evidence."""
    if finding.line_start == finding.line_end:
        loc = f"{finding.file}:{finding.line_start}"
    else:
        loc = f"{finding.file}:{finding.line_start}-{finding.line_end}"
    bullet = (
        f"- **[{finding.severity.value}/{finding.claim_type.value}]** `{loc}` — {finding.claim}"
    )
    if finding.evidence_pointer:
        bullet = f"{bullet}\n  - evidence: {finding.evidence_pointer}"
    return bullet


def _findings_section(findings: list[Finding]) -> str:
    """Group findings by status, then severity / claim_type / location."""
    if not findings:
        return "_No findings recorded._"
    by_status: dict[str, list[Finding]] = {}
    for finding in findings:
        by_status.setdefault(finding.status.value, []).append(finding)
    blocks: list[str] = []
    for status in sorted(by_status):
        items = by_status[status]
        blocks.append(f"### {status} ({len(items)})")
        ordered = sorted(
            items,
            key=lambda f: (
                f.severity.value,
                f.claim_type.value,
                f.file,
                f.line_start,
            ),
        )
        blocks.extend(_finding_line(finding) for finding in ordered)
    return "\n".join(blocks)


def render_ledger_report(
    db_path: Path,
    *,
    pr_number: int,
    decision: MergeDecision,
    facts: MergeFacts,
    archive_appendix: str = "",
) -> str:
    """Render the sticky archive report from the ledger and merge facts.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR being reported on.
        decision: The pure-gate decision (the headline).
        facts: The re-verified merge facts (the facts table).
        archive_appendix: Optional markdown appended below the report —
            the machine-readable ``TeamOutcome`` JSON, guard section, and
            dialogue transcript; empty to omit it.

    Returns:
        The full markdown body for the sticky archive comment.
    """
    findings = fetch_findings(db_path, pr_number)
    parts = [
        LEDGER_REPORT_HEADER,
        "",
        _decision_header(decision, facts.head_sha),
        "",
        _facts_table(facts),
        "",
        "## Findings",
        "",
        _findings_section(findings),
    ]
    if archive_appendix:
        parts.extend(["", "---", "", archive_appendix])
    return "\n".join(parts)
