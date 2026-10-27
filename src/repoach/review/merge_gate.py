"""Pure evidence-first merge gate (SP-MERGE-GATE-SHADOW, redesign slice 7a).

The legacy gate trusts the archive's self-reported 4/4 verdict — a
forgeable, stale signal (audit CRITICAL #1) fed by the parse_failed
promotion path (CRITICAL #2). This module computes the merge decision
as a PURE FUNCTION of facts re-verified at the exact head: CI green,
zero open blocking findings (mechanical claims re-verified on disk at
head; judged claims block whenever unsettled — open or SHA-stale
included, fail closed per SP-GATE-JUDGED-FAIL-CLOSED), and spec
coverage computed at THIS head. Stored finding state is a hint — the
truth is the re-verification at head.

Slice 7a runs this in SHADOW: ``run_auto_merge`` computes and logs the
decision next to the live 4/4 gate WITHOUT changing what merges, so
the pure gate can be compared against the 4/4 on real PRs before the
flip (slice 7b drops the archive gate and decides on this).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..gates.config import GatesConfig, load_gates_config
from .finding_verifiers import verify_finding
from .findings import (
    JUDGED_CLAIM_TYPES,
    MECHANICAL_CLAIM_TYPES,
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    fetch_review_integrity,
    init_findings_schema,
)
from .reviewer import ReviewVerdict
from .spec_gate import (
    RESOLUTION_OWNERSHIP,
    SpecCoverage,
    coverage_refusals,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    latest_batch,
)

_MIN_REVIEWERS = 4
"""A complete review has at least four reviewers parsed (the bench)."""

_MECHANICAL_TYPES = MECHANICAL_CLAIM_TYPES
_JUDGED_TYPES = JUDGED_CLAIM_TYPES
"""Alias of :data:`findings.JUDGED_CLAIM_TYPES` (SP-CLAIM-TYPE-PARTITION-ALIGN).

The gate and the refuter used to keep drifting, independently-defined
copies of this set — the refuter judged ``spec_gap`` while the gate's
copy omitted it, so a refuter-VERIFIED blocking ``spec_gap`` fell
through both ``_MECHANICAL_TYPES`` and ``_JUDGED_TYPES`` and landed in
``blocking_unverified`` as a misleading "no verifier" reason instead of
counting as an open blocking finding. Importing the shared constant
instead of redeclaring it makes that drift structurally impossible:
every claim type the refuter judges is, by construction, also judged
here.
"""
_SETTLED = frozenset(
    {
        FindingStatus.RESOLVED,
        FindingStatus.REFUTED,
        FindingStatus.NOT_APPLICABLE,
        FindingStatus.RESOLVED_AT_HEAD,
    }
)
"""Statuses that never block: ``not_applicable`` is terminal and non-blocking
(SP-UNIVERSAL-GATES G4) — the verifier does not apply to the cited file;
``resolved_at_head`` is the gate's own re-check finding that the head no
longer supports a finding of an older head (SP-GATE-REVERIFIES-AT-HEAD)."""

NO_REVERIFICATION: dict[str, int] = {"resolved": 0, "still_open": 0, "needs_judgement": 0}
"""The ``reverified`` counts of a gate that re-checked nothing."""
_BLOCKING_STATUSES = frozenset({FindingStatus.VERIFIED, FindingStatus.STUCK})
"""Statuses that confirm a live blocking problem (SP-STUCK-ESCALATION).

``stuck`` is a terminal escalation state — auto-resolution gave up — so a
stuck blocking finding must keep gating the merge exactly like a verified
one; otherwise marking a real judged finding ``stuck`` would silently
unblock it. ``stuck`` is deliberately NOT in ``_SETTLED`` and the gate's
re-verification stays the escape hatch (a stuck finding the operator
actually fixes re-verifies green / falls stale and merges normally).
"""


class MergeFacts(BaseModel):
    """The re-verified facts the pure gate decides on.

    Attributes:
        head_sha: The exact head the decision is computed against.
        ci_green: Whether every required check is green at that head.
        open_blocking_findings: Count of blocking findings confirmed
            real at head (mechanical re-verified; judged counted
            whenever unsettled and past ``proposed`` — ``open`` and
            SHA-stale included, fail closed). ``stuck`` blocking
            findings count exactly like ``verified`` ones (terminal
            escalation must keep the merge blocked).
        open_blocking_ids: The ledger ids of exactly the findings
            ``open_blocking_findings`` counts, in ledger order — what a
            ``REQUEST_CHANGES`` is traceable to (the report's
            ``verdict_basis``); ``len(open_blocking_ids) ==
            open_blocking_findings`` always.
        spec_covered: Whether a coverage record computed at THIS head
            reports every promised acceptance selector present.
        spec_coverage_known: Whether a coverage record exists at this
            exact head (a hand-shipped PR has none; older-head records
            do not count — their head lacks a fresh review-integrity
            row and is refused on that axis).
        review_complete: Whether the bench ran fresh at this head with
            every reviewer parsed and zero unparsed outcomes — the
            evidence-first replacement for the archive verdict that
            closes the parse_failed-promote hole (audit CRITICAL #2).
        review_integrity_known: Whether a review-integrity record
            exists at this exact head.
        review_integrity_any: Whether ANY review-integrity record
            exists for this PR — distinguishes a review that ran at an
            older head from a findings-ledger artifact that failed to
            transport into an empty database (the generic "review did
            not run" reason misdirected the operator; tech-debt survey).
        blocking_unverified: One line per blocking finding no check
            could confirm at head.
        reverified: Counts of the findings of older heads re-checked at
            this head — ``resolved``, ``still_open``,
            ``needs_judgement`` (SP-GATE-REVERIFIES-AT-HEAD).
        reverify_reasons: One line per re-checked finding still
            blocking, naming the head it was raised at.
        reverify_line: The one-line summary of the re-check (empty when
            the gate did not reach the ledger).
        spec_missing: The acceptance selectors absent at head, from the
            coverage record at head.
        spec_planned: The selectors of a pre-implementation spec whose
            file does not exist yet — reported, never refusing
            (SP-PLAN-SELECTOR-AUDIT-WIRE 0.2).
        spec_draft_refusals: One line per rule that withheld that
            exemption; each one is a refusal reason.
        spec_recompute_failure: Why a coverage row graded by a retired
            rule could not be recomputed (SP-PLAN-SELECTOR-AUDIT-WIRE
            0.3); always a refusal reason, whatever the stored verdict,
            since that verdict comes from a retired rule. Empty otherwise.
        spec_contract: What the coverage was graded against — ``plan``,
            ``acceptance-criteria``, ``none`` or ``unavailable``
            (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS).
        spec_origins: Selector to origin (``AC3``, ``plan step 1``) for
            naming each missing selector.
        spec_no_selectors_line: The ``no selectors to grade: …`` line of a
            contract with nothing to grade, empty otherwise (G3).
        spec_report: Lines reported and never refusing (``plan-absent: …``).
        spec_rows: The coverage rows of the latest grading at head, one
            per graded spec; the decision names each not-covered row's
            causes, prefixed with the spec id for a spec resolved by
            ownership (G7).

    ``spec_rows`` is the one source: the flat ``spec_missing``,
    ``spec_planned``, ``spec_draft_refusals``, ``spec_contract``,
    ``spec_origins``, ``spec_no_selectors_line`` and ``spec_report`` are
    properties derived from it, which the gate JSON prints (its
    documented ``spec_acceptance`` shape).
    """

    model_config = ConfigDict(extra="forbid")

    head_sha: str
    ci_green: bool
    open_blocking_findings: int
    open_blocking_ids: list[int] = []
    spec_covered: bool
    spec_coverage_known: bool
    review_complete: bool
    review_integrity_known: bool
    review_integrity_any: bool = False
    blocking_unverified: list[str] = []
    reverified: dict[str, int] = dict(NO_REVERIFICATION)
    reverify_reasons: list[str] = []
    reverify_line: str = ""
    spec_recompute_failure: str = ""
    spec_rows: list[SpecCoverage] = []

    @property
    def spec_missing(self) -> list[str]:
        """The acceptance selectors absent at head, every row in order."""
        return [selector for row in self.spec_rows for selector in row.missing]

    @property
    def spec_planned(self) -> list[str]:
        """The planned selectors of a pre-implementation spec, every row in order."""
        return [selector for row in self.spec_rows for selector in row.planned]

    @property
    def spec_draft_refusals(self) -> list[str]:
        """The refusal lines the rows carry, every row in order."""
        return [line for row in self.spec_rows for line in row.draft_refusals]

    @property
    def spec_contract(self) -> str:
        """What the latest row was graded against; empty without a row."""
        return self.spec_rows[-1].contract if self.spec_rows else ""

    @property
    def spec_origins(self) -> dict[str, str]:
        """Selector to origin over every row."""
        return {k: v for row in self.spec_rows for k, v in row.origins.items()}

    @property
    def spec_no_selectors_line(self) -> str:
        """The ``no selectors to grade: …`` line of a contract with nothing to grade."""
        lines = [
            line
            for row in self.spec_rows
            if row.contract == "none"
            for line in (*row.draft_refusals, *row.report)
        ]
        return lines[0] if lines else ""

    @property
    def spec_report(self) -> list[str]:
        """Lines reported and never refusing, every row in order."""
        return [line for row in self.spec_rows for line in row.report]


class MergeDecision(BaseModel):
    """The pure gate's verdict.

    Attributes:
        merge: True when every condition holds.
        reasons: One human-readable line per failing condition; empty
            when ``merge`` is True.
    """

    merge: bool
    reasons: list[str]


def compute_merge_decision(facts: MergeFacts) -> MergeDecision:
    """Decide whether to merge, purely from re-verified *facts*.

    Args:
        facts: The facts gathered at the exact head.

    Returns:
        A :class:`MergeDecision`. ``merge`` is True only when the head
        is known, CI is green, no blocking finding survives
        re-verification, and (when coverage was recorded) the spec's
        acceptance selectors are present.
    """
    reasons: list[str] = []
    if not facts.head_sha:
        reasons.append("head_sha unknown")
    if not facts.review_integrity_known:
        if facts.review_integrity_any:
            reasons.append("no review-integrity record at head (review ran at an older head)")
        else:
            reasons.append(
                "no review records for this PR in the ledger — "
                "findings-ledger artifact missing or not transported?"
            )
    elif not facts.review_complete:
        reasons.append("review incomplete or unparsed reviewers at head")
    if not facts.ci_green:
        reasons.append("CI not green at head")
    if facts.open_blocking_findings > 0:
        reasons.append(f"{facts.open_blocking_findings} open blocking finding(s) at head")
    if facts.spec_coverage_known and not facts.spec_covered:
        reasons.extend(_coverage_reasons(facts))
    if facts.spec_recompute_failure:
        reasons.append(facts.spec_recompute_failure)
    if facts.blocking_unverified:
        reasons.append(
            f"{len(facts.blocking_unverified)} unverified blocking finding(s): "
            f"{'; '.join(facts.blocking_unverified)}"
        )
    if reasons:
        reasons.extend(facts.reverify_reasons)
    return MergeDecision(merge=not reasons, reasons=reasons)


def _coverage_reasons(facts: MergeFacts) -> list[str]:
    """Return the coverage refusals, each naming its cause (G2, G3, G7).

    Every not-covered row contributes its lines
    (:func:`~repoach.review.spec_gate.coverage_refusals`); a row of a
    spec resolved by ownership prefixes each with ``<id>: ``. Facts that
    say "not covered" without a row (built by hand) get one line saying so.
    """
    if not facts.spec_rows:
        return ["spec coverage not covered: no coverage row at head names a cause"]
    reasons: list[str] = []
    for row in facts.spec_rows:
        prefix = f"{row.spec_id}: " if row.resolution == RESOLUTION_OWNERSHIP else ""
        reasons.extend(f"{prefix}{line}" for line in coverage_refusals(row))
    return reasons


def verdict_from_facts(facts: MergeFacts) -> ReviewVerdict:
    """Derive the team review verdict from the findings ledger.

    The evidence-first replacement for the strict consensus gate
    (SP-CODER-TRIGGER-FLIP / redesign slice 10b): the team verdict is
    ``REQUEST_CHANGES`` exactly when the review surfaced a blocking
    problem the author must address — an open blocking finding at head,
    or an incomplete review (an unparsed reviewer or fewer than the full
    bench) — otherwise ``APPROVE``.

    CI freshness and spec coverage are deliberately NOT folded in: CI is
    still pending while the review runs, and both are re-checked by the
    authoritative gate (:func:`compute_merge_decision`). Keeping this in
    lockstep with the *findings* dimension of that gate means the review
    verdict never contradicts the merge it gates — unlike the legacy
    consensus, which also blocked on ``COMMENT`` verdicts and ``major``
    comments that the pure gate would happily merge.

    Args:
        facts: The ledger facts summarised at the review head.

    Returns:
        ``REQUEST_CHANGES`` or ``APPROVE``.
    """
    if facts.open_blocking_findings > 0 or not facts.review_complete:
        return ReviewVerdict.REQUEST_CHANGES
    return ReviewVerdict.APPROVE


def gather_merge_facts(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str,
    ci_green: bool,
    gates_config: GatesConfig | None = None,
) -> MergeFacts:
    """Re-verify the ledger at *head_sha* and assemble :class:`MergeFacts`.

    Mechanical blocking findings are re-checked on disk at the current
    head (their stored status is a hint, not trusted). Judged blocking
    findings count whenever they are unsettled and past ``proposed`` —
    ``verified``/``stuck`` at ANY sha (a stale ``checked_at_sha`` means
    unverified at head) and ``open`` (a fix that did not resolve them)
    all block; only settled findings (resolved / refuted) never count
    (SP-GATE-JUDGED-FAIL-CLOSED, audit finding C2 — the earlier
    fresh-at-head requirement silently DROPPED stale and open judged
    findings, merging over unresolved security work).

    Args:
        db_path: The findings + coverage ledger.
        pr_number: The PR under decision.
        repo_root: The head checkout the verifiers resolve against.
        head_sha: The exact head being decided on.
        ci_green: Whether required checks are green at head.
        gates_config: The trusted gate configuration (the checkout's,
            never the PR head tree's); read from ``repo_root`` when omitted.

    Returns:
        The assembled :class:`MergeFacts`.
    """
    init_findings_schema(db_path)
    open_blocking: list[int] = []
    blocking_unverified: list[str] = []
    if gates_config is None:
        gates_config = load_gates_config(repo_root)
    for finding in fetch_findings(db_path, pr_number):
        if finding.severity is not Severity.BLOCKING or finding.status in _SETTLED:
            continue
        if finding.claim_type not in _MECHANICAL_TYPES and finding.claim_type not in _JUDGED_TYPES:
            blocking_unverified.append(
                f"finding {finding.id}: {finding.claim_type.value} has no verifier"
            )
            continue
        if finding.status is FindingStatus.PROPOSED:
            blocking_unverified.append(
                f"finding {finding.id}: {finding.claim_type.value} is PROPOSED at head"
            )
            continue
        if finding.claim_type in _MECHANICAL_TYPES:
            status, _, _ = verify_finding(finding, repo_root=repo_root, config=gates_config)
            if status is FindingStatus.VERIFIED:
                open_blocking.append(_ledger_id(finding))
        elif finding.claim_type in _JUDGED_TYPES:
            open_blocking.append(_ledger_id(finding))
    return _assemble_facts(
        db_path,
        pr_number=pr_number,
        head_sha=head_sha,
        ci_green=ci_green,
        open_blocking=open_blocking,
        blocking_unverified=blocking_unverified,
    )


def summarise_ledger_facts(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
) -> MergeFacts:
    """Assemble :class:`MergeFacts` from RECORDED ledger state only.

    The display sibling of :func:`gather_merge_facts`: it trusts each
    finding's stored status instead of re-verifying on disk, so it is
    safe to call where the PR head is NOT checked out (the review job
    runs from the base ref) and without any GitHub call. It feeds the
    review-time report (:func:`report.render_ledger_report`) — a snapshot
    of what the ledger currently records, never the merge authority. The
    authoritative decision always comes from :func:`gather_merge_facts`
    re-verifying at a checked-out head (auto-merge / ``review gate``).

    A blocking finding counts as open when it is not settled and its
    stored status still confirms the problem: mechanical findings when
    ``status == verified``; judged findings whenever they are past
    ``proposed`` — ``open``, or ``verified``/``stuck`` at ANY sha, the
    same fail-closed rule as the authoritative gate
    (SP-GATE-JUDGED-FAIL-CLOSED). ``ci_green`` is
    derived from the ledger too — a red CI is materialised as a verified
    ``broken_behavior`` finding (slice 8b), so CI is treated as red while
    any such finding is unsettled.

    Args:
        db_path: The findings + coverage ledger.
        pr_number: The PR being reported on.
        head_sha: The head the report is rendered against.

    Returns:
        The assembled :class:`MergeFacts` from recorded state.
    """
    init_findings_schema(db_path)
    open_blocking: list[int] = []
    ci_red = False
    for finding in fetch_findings(db_path, pr_number):
        if finding.status in _SETTLED:
            continue
        if finding.claim_type is ClaimType.BROKEN_BEHAVIOR:
            ci_red = True
            continue
        if finding.severity is not Severity.BLOCKING:
            continue
        if finding.claim_type in _JUDGED_TYPES:
            if finding.status is not FindingStatus.PROPOSED:
                open_blocking.append(_ledger_id(finding))
            continue
        if finding.status not in _BLOCKING_STATUSES:
            continue
        if finding.claim_type in _MECHANICAL_TYPES:
            open_blocking.append(_ledger_id(finding))
    return _assemble_facts(
        db_path,
        pr_number=pr_number,
        head_sha=head_sha,
        ci_green=not ci_red,
        open_blocking=open_blocking,
    )


def _ledger_id(finding: Finding) -> int:
    """Return the ledger id of a fetched finding (``-1`` for an unpersisted one)."""
    return finding.id if finding.id is not None else -1


def _assemble_facts(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    ci_green: bool,
    open_blocking: list[int],
    blocking_unverified: list[str] | None = None,
) -> MergeFacts:
    """Fold the spec-coverage and review-integrity records into facts.

    Shared by :func:`gather_merge_facts` (re-verified ``open_blocking``)
    and :func:`summarise_ledger_facts` (recorded ``open_blocking``) so
    the non-finding facts can never drift between the two. Spec
    coverage is pinned to ``head_sha`` exactly like review integrity:
    only a record computed at the decided head can satisfy
    ``spec_covered``, so a stale ``covered=True`` from an earlier push
    no longer carries the gate after a regressing push
    (SP-GATE-JUDGED-FAIL-CLOSED, audit finding M8). A head with older
    coverage records but none at head reads as coverage-unknown — safe,
    because such a head has no fresh review-integrity row either and
    is already refused via ``review_complete``.
    """
    init_spec_coverage_schema(db_path)
    coverage = fetch_spec_coverage(db_path, pr_number, head_sha=head_sha)
    integrity = fetch_review_integrity(db_path, pr_number)
    fresh = [r for r in integrity if r["head_sha"] == head_sha]
    review_complete = any(
        r["n_unparsed"] == 0 and r["n_reviewers"] >= _MIN_REVIEWERS for r in fresh
    )
    rows = latest_batch(coverage)
    latest = rows[-1] if rows else None
    return MergeFacts(
        head_sha=head_sha,
        ci_green=ci_green,
        open_blocking_findings=len(open_blocking),
        open_blocking_ids=list(open_blocking),
        spec_covered=bool(rows) and all(row.covered for row in rows),
        spec_coverage_known=latest is not None,
        review_complete=review_complete,
        review_integrity_known=bool(fresh),
        review_integrity_any=bool(integrity),
        blocking_unverified=blocking_unverified or [],
        spec_rows=rows,
    )
