"""Render the PR review report from the findings ledger.

SP-VERDICT-FLIP (redesign slice 10a): the sticky archive comment stops
being the verdict authority and becomes a *report* built from the
findings ledger and the re-verified merge facts. The pure gate
(:func:`auto_merge.evaluate_merge_gate`) owns the merge decision; this
module only presents it.

Every finding that can move ``final_verdict`` is listed with its judge
(:func:`report_findings`): the ones a mechanical verifier settled
(``mechanical:<claim_type>``), the ones the refuter judged or deferred,
the findings of older heads re-checked at the head, and any finding the
ledger still counts as open blocking. ``verdict_basis`` names the ids a
``REQUEST_CHANGES`` rests on; each of them is one listed entry.

An optional ``archive_appendix`` carries the machine-readable
``TeamOutcome`` JSON (consumed by ``repoach review report``), the
hallucination-guard section, and the dialogue transcript below the
report. The legacy per-reviewer verdict framing was dropped in 10b-4
(SP-RETIRE-VERDICT-ARCHIVE) — the verdict is no longer the authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .finding_verifiers import MECHANICAL_METHODS, MechanicalVerification
from .findings import ClaimType, Finding, fetch_findings
from .merge_gate import MergeDecision, MergeFacts

if TYPE_CHECKING:
    from .head_reverify import Reverification
    from .refuter import Refutation

Judge = Literal["mechanical", "refuter", "reverify", "ledger"]
"""Who settled a listed finding: a mechanical verifier, the refuter, the
re-check of an older head at this head, or only a ledger stamp (a CI or
coder check, or no check at all)."""

REFUTER_METHOD = "refuter"
"""``verification_method`` the refuter stamps on the rows it judges."""

REVERIFY_METHOD_PREFIX = "head_reverify"
"""Prefix of every ``verification_method`` the head re-check stamps."""

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
    """Render the re-verified merge facts as a markdown table.

    The re-check of findings raised at older heads gets its own row
    when it re-checked any (SP-GATE-REVERIFIES-AT-HEAD G8).
    """
    spec = facts.spec_covered if facts.spec_coverage_known else "n/a"
    review = facts.review_complete if facts.review_integrity_known else "n/a"
    rows = [
        ("CI green", facts.ci_green),
        ("Open blocking findings", facts.open_blocking_findings),
        ("Spec covered", spec),
        ("Review complete", review),
    ]
    if any(facts.reverified.values()):
        counts = facts.reverified
        rows.append(
            (
                "Findings of older heads re-verified",
                f"resolved {counts.get('resolved', 0)}, "
                f"still open {counts.get('still_open', 0)}, "
                f"needs judgement {counts.get('needs_judgement', 0)}",
            )
        )
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
    judged: list[ReportFinding] | None = None,
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
        judged: The findings that can move the verdict, each with its
            judge (:func:`report_findings`); rendered as a table above
            the ledger listing, with the verdict basis, when given.

    Returns:
        The full markdown body for the sticky archive comment.
    """
    findings = fetch_findings(db_path, pr_number)
    parts: list[str] = [
        LEDGER_REPORT_HEADER,
        "",
        _decision_header(decision, facts.head_sha),
        "",
        _facts_table(facts),
        "",
    ]
    if judged is not None:
        basis = ", ".join(f"#{finding_id}" for finding_id in facts.open_blocking_ids) or "none"
        parts.extend(
            [
                "## Verdict-driving findings",
                "",
                f"Verdict basis: {basis}",
                "",
                judged_findings_section(judged),
                "",
            ]
        )
    parts.extend(["## Findings", "", _findings_section(findings)])
    if archive_appendix:
        parts.extend(["", "---", "", archive_appendix])
    return "\n".join(parts)


@dataclass(frozen=True)
class ReportFinding:
    """One finding the review report lists, with who judged it and how.

    Attributes:
        finding_id: Ledger row id (``None`` for an unrecorded finding).
        file: The cited file.
        line_start: First cited line.
        claim_type: The finding's claim type.
        judge: Who settled it (:data:`Judge`).
        state: The judge's own state — ``verified`` / ``refuted`` /
            ``not_applicable`` for a mechanical verifier, ``verified`` /
            ``refuted`` / ``deferred`` / ``unverified`` for the refuter,
            ``resolved`` / ``still_open`` / ``needs_judgement`` for the
            re-check, the ledger status otherwise.
        verification_method: The verifier's name —
            ``mechanical:<claim_type>``, ``refuter``, ``head_reverify``
            or the ledger's stamp.
        evidence: The one line the judge gave.
        severity: ``blocking`` / ``advisory`` from the ledger, empty when
            the row is not in it.
        status: The ledger status when the report was built.
        blocking: Whether the verdict counts it as an open blocking
            finding (its id is in ``verdict_basis``).
    """

    finding_id: int | None
    file: str
    line_start: int
    claim_type: ClaimType
    judge: Judge
    state: str
    verification_method: str
    evidence: str
    severity: str = ""
    status: str = ""
    blocking: bool = False

    def as_json(self) -> dict[str, object]:
        """Return the finding entry of the ``review pr`` JSON payload.

        A refuter entry keeps its ``refutation = {state, reason}``
        object (SP-REFUTER-JUDGES-WHAT-IT-CAN); every other entry has
        ``refutation`` null.
        """
        refutation = (
            {"state": self.state, "reason": self.evidence} if self.judge == "refuter" else None
        )
        return {
            "id": self.finding_id,
            "file": self.file,
            "line": self.line_start,
            "claim_type": self.claim_type.value,
            "severity": self.severity,
            "status": self.status,
            "judge": self.judge,
            "state": self.state,
            "verification_method": self.verification_method,
            "evidence": self.evidence,
            "blocking": self.blocking,
            "refutation": refutation,
        }


def _from_verification(outcome: MechanicalVerification) -> ReportFinding:
    """Return the report entry of one mechanical outcome."""
    return ReportFinding(
        finding_id=outcome.finding_id,
        file=outcome.file,
        line_start=outcome.line_start,
        claim_type=outcome.claim_type,
        judge="mechanical",
        state=outcome.state,
        verification_method=outcome.verifier,
        evidence=outcome.evidence,
    )


def _from_refutation(outcome: Refutation) -> ReportFinding:
    """Return the report entry of one refuter outcome."""
    return ReportFinding(
        finding_id=outcome.finding_id,
        file=outcome.file,
        line_start=outcome.line_start,
        claim_type=outcome.claim_type,
        judge="refuter",
        state=outcome.state,
        verification_method=REFUTER_METHOD,
        evidence=outcome.reason,
    )


def _from_reverification(outcome: Reverification) -> ReportFinding:
    """Return the report entry of one re-check of an older head."""
    return ReportFinding(
        finding_id=outcome.finding_id,
        file=outcome.file,
        line_start=outcome.line_start,
        claim_type=outcome.claim_type,
        judge="reverify",
        state=outcome.state,
        verification_method=REVERIFY_METHOD_PREFIX,
        evidence=f"raised at {outcome.recorded_at[:7] or 'an unrecorded head'}: {outcome.reason}",
    )


def _ledger_judge(finding: Finding) -> tuple[Judge, str]:
    """Name the judge and method a ledger row's stamp records."""
    method = finding.verification_method
    if method in MECHANICAL_METHODS:
        return "mechanical", f"mechanical:{finding.claim_type.value}"
    if method == REFUTER_METHOD:
        return "refuter", REFUTER_METHOD
    if method.startswith(REVERIFY_METHOD_PREFIX):
        return "reverify", method
    return "ledger", method


def _from_ledger(finding: Finding) -> ReportFinding:
    """Return the report entry of a ledger row no outcome of this run named."""
    judge, method = _ledger_judge(finding)
    evidence = finding.verification_result
    if judge == "mechanical" and finding.verification_method:
        evidence = f"{finding.verification_method}: {evidence}"
    return ReportFinding(
        finding_id=finding.id,
        file=finding.file,
        line_start=finding.line_start,
        claim_type=finding.claim_type,
        judge=judge,
        state=finding.status.value,
        verification_method=method,
        evidence=evidence,
    )


def report_findings(
    db_path: Path,
    *,
    pr_number: int,
    facts: MergeFacts,
    verifications: Iterable[MechanicalVerification] = (),
    refutations: Iterable[Refutation] = (),
    reverifications: Iterable[Reverification] = (),
) -> list[ReportFinding]:
    """List every finding that can move the review verdict, each once.

    The run's own outcomes come first — mechanical verdicts, then the
    refuter's, then the re-check of older heads; a later judge of the
    same ledger id replaces the earlier entry. Every id in
    ``facts.open_blocking_ids`` the run did not judge is then added from
    its ledger row, so each finding the verdict counts as open blocking
    is one entry with ``blocking`` true, whatever path settled it.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR being reported on.
        facts: The ledger facts the verdict was derived from.
        verifications: The settled mechanical outcomes of this run (or
            those the ledger records at the head, for a replay).
        refutations: What the refuter did with each finding it saw.
        reverifications: The findings of older heads re-checked at head.

    Returns:
        The entries in report order, each with its ledger severity and
        status filled in.
    """
    ledger = {finding.id: finding for finding in fetch_findings(db_path, pr_number)}
    entries: dict[object, ReportFinding] = {}
    judged = [
        *(_from_verification(outcome) for outcome in verifications),
        *(_from_refutation(outcome) for outcome in refutations),
        *(_from_reverification(outcome) for outcome in reverifications),
    ]
    for index, entry in enumerate(judged):
        key: object = entry.finding_id if entry.finding_id is not None else ("unrecorded", index)
        entries.pop(key, None)
        entries[key] = entry
    for finding_id in facts.open_blocking_ids:
        if finding_id not in entries and finding_id in ledger:
            entries[finding_id] = _from_ledger(ledger[finding_id])
    basis = set(facts.open_blocking_ids)
    listed: list[ReportFinding] = []
    for entry in entries.values():
        row = ledger.get(entry.finding_id)
        listed.append(
            replace(
                entry,
                severity=row.severity.value if row is not None else "",
                status=row.status.value if row is not None else "",
                blocking=entry.finding_id in basis,
            )
        )
    return listed


def verdict_basis(facts: MergeFacts) -> list[int]:
    """Return the ledger ids the verdict counts as open blocking findings."""
    return list(facts.open_blocking_ids)


def findings_table_lines(entries: list[ReportFinding], basis: list[int]) -> list[str]:
    """Render the stderr table of every listed finding and the verdict basis.

    Args:
        entries: The report's findings (:func:`report_findings`).
        basis: The ids the verdict rests on.

    Returns:
        A ``findings: N listed`` header, one row per finding (id, judge,
        state, method, claim type, location, ``BLOCKING`` when in the
        basis) and a closing ``verdict basis:`` line.
    """
    lines = [f"findings: {len(entries)} listed"]
    for entry in entries:
        mark = " BLOCKING" if entry.blocking else ""
        lines.append(
            f"  #{entry.finding_id if entry.finding_id is not None else '-'} "
            f"{entry.judge:<10} {entry.state:<15} {entry.verification_method} "
            f"{entry.claim_type.value} {entry.file}:{entry.line_start}{mark}"
        )
    named = ", ".join(f"#{finding_id}" for finding_id in basis) if basis else "none"
    lines.append(f"verdict basis: {named}")
    return lines


def judged_findings_section(entries: list[ReportFinding]) -> str:
    """Render the report's findings as a markdown table with a judge column."""
    if not entries:
        return "_No finding judged this run._"
    lines = [
        "| Id | Judge | State | Method | Finding | Blocking |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        where = f"`{entry.file}:{entry.line_start}` {entry.severity}/{entry.claim_type.value}"
        evidence = entry.evidence.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {entry.finding_id if entry.finding_id is not None else '-'} "
            f"| {entry.judge} | {entry.state} | {entry.verification_method} "
            f"| {where} — {evidence} | {'yes' if entry.blocking else 'no'} |"
        )
    return "\n".join(lines)
