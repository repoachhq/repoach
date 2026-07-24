"""Bridge: derive Finding records from ReviewerOutcome comments (dual-run, SP-FINDER-OUTPUT).

Provisional claim_type defaults map each lens to a ClaimType bucket.
Verifier slices 4-5 own the refinement; proposed status signals unverified.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.logging import get_logger
from .diff_scoper import ScopedDiff
from .findings import (
    JUDGED_CLAIM_TYPES,
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from .reviewer import BotRole, ReviewComment, ReviewerOutcome
from .thread_context import REPEAT_SIMILARITY_THRESHOLD, similarity

_log = get_logger(__name__)

_OVERSIZED_FINDER = "diff_scoper"
"""Finder identifier stamped on oversized-omission findings (SP-DIFF-SCOPER-OVERSIZE-BLOCK)."""

LENS_DEFAULT_CLAIM_TYPE: dict[BotRole, ClaimType] = {
    BotRole.ARCHITECT: ClaimType.DESIGN,
    BotRole.SENTINEL: ClaimType.SECURITY,
    BotRole.TESTER: ClaimType.MISSING_TEST,
    BotRole.SCRIBE: ClaimType.MISSING_DOCSTRING,
}

SEVERITY_MAP: dict[str, Severity] = {
    "blocker": Severity.BLOCKING,
    "major": Severity.BLOCKING,
    "minor": Severity.ADVISORY,
    "nit": Severity.ADVISORY,
}

_MISSING_TEST_CUE = re.compile(r"(?i)no test for|missing test")
_MISSING_DOCSTRING_CUE = re.compile(r"(?i)missing docstring|no docstring")
_LINT_CONVENTION_CUE = re.compile(r"(?i)\blint\b|convention|style nit")
_BROKEN_BEHAVIOR_CUE = re.compile(r"(?i)\brace\b|deadlock|crash|broken|\bbug\b")
_SPEC_GAP_CUE = re.compile(r"(?i)spec gap|not in spec|specification gap")
_SECURITY_CUE = re.compile(r"(?i)security|vulnerability|injection|auth bypass")
_DESIGN_CUE = re.compile(r"(?i)design|architecture|pattern")

_JUDGED_CUES: list[tuple[ClaimType, re.Pattern[str]]] = [
    (ClaimType.SPEC_GAP, _SPEC_GAP_CUE),
    (ClaimType.SECURITY, _SECURITY_CUE),
    (ClaimType.DESIGN, _DESIGN_CUE),
]
_MECHANICAL_CUES: list[tuple[ClaimType, re.Pattern[str]]] = [
    (ClaimType.MISSING_TEST, _MISSING_TEST_CUE),
    (ClaimType.MISSING_DOCSTRING, _MISSING_DOCSTRING_CUE),
    (ClaimType.LINT_CONVENTION, _LINT_CONVENTION_CUE),
]

_CLAIM_TYPE_CUES: list[tuple[ClaimType, re.Pattern[str]]] = [
    *_JUDGED_CUES,
    *_MECHANICAL_CUES,
    (ClaimType.BROKEN_BEHAVIOR, _BROKEN_BEHAVIOR_CUE),
]
"""Ordered (claim_type, cue) pairs; first match wins.

Judged claim types (:data:`JUDGED_CLAIM_TYPES` — spec_gap, security,
design) are checked BEFORE the mechanical ones (missing_test,
missing_docstring, lint_convention), the reverse of the pre-
SP-CLAIM-TYPE-PARTITION-ALIGN order. An explicit judged-type signal
must not be overridden by an incidental mechanical keyword sharing the
same comment — e.g. a security finding whose body happens to mention
"lint" routes to SECURITY, never LINT_CONVENTION. broken_behavior sits
outside both partitions (its own CI-derived verifier, see
:mod:`merge_gate`) and is checked last, unchanged from before.
"""

assert {claim_type for claim_type, _ in _JUDGED_CUES} == JUDGED_CLAIM_TYPES, (
    "cue precedence has drifted from the judged-type partition"
)


def classify_claim_type(body: str, role: BotRole) -> ClaimType:
    """Classify a comment body into a ClaimType by content, falling back to the lens.

    Scans ``body`` against :data:`_CLAIM_TYPE_CUES` in priority order
    (judged types before mechanical types) and returns the first cue
    that fires. When no cue fires, returns the lens default for
    ``role`` (today's behaviour), defaulting to ClaimType.DESIGN for an
    unmapped role.

    Pure and total: string matching only, no exceptions possible.

    Args:
        body: The reviewer comment body to scan.
        role: BotRole that produced the comment, used as the fallback lens.

    Returns:
        The classified ClaimType.
    """
    text = body or ""
    for claim_type, pattern in _CLAIM_TYPE_CUES:
        if pattern.search(text):
            return claim_type
    return LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN)


def _is_unparsed(outcome: ReviewerOutcome) -> bool:
    """Return True when outcome signals a parse failure or bot crash.

    Args:
        outcome: The ReviewerOutcome to inspect.

    Returns:
        True when the summary starts with a parse_failed or bot crashed marker.
    """
    summary = outcome.summary or ""
    return summary.startswith("[parse_failed:") or summary.startswith("_(bot crashed:")


def _files_in_diff(diff: str) -> set[str]:
    """Return the repo-relative paths a unified-diff blob touches.

    Walks ``diff --git a/<old> b/<new>`` headers plus ``+++ b/<path>`` and
    ``--- a/<path>`` lines, whitespace-tolerant, discarding ``/dev/null``.

    Failure-soft: malformed input yields whatever it could parse (an empty
    set is acceptable — the caller then keeps every comment, matching the
    historical no-filter behaviour).

    Args:
        diff: A unified-diff blob.

    Returns:
        The set of repo-relative file paths the diff touches.
    """
    files: set[str] = set()
    for raw in diff.splitlines():
        line = raw.strip()
        if line.startswith("+++ b/"):
            files.add(line[len("+++ b/") :].strip())
        elif line.startswith("--- a/"):
            files.add(line[len("--- a/") :].strip())
        elif line.startswith("diff --git "):
            for token in line.split():
                if token.startswith("a/") or token.startswith("b/"):
                    files.add(token[2:])
    files.discard("/dev/null")
    return files


def comment_to_finding(
    comment: ReviewComment,
    *,
    role: BotRole,
    pr_number: int,
    head_sha: str,
    round_n: int,
) -> Finding:
    """Derive a Finding from one ReviewComment.

    Args:
        comment: The reviewer comment to convert.
        role: BotRole that produced the comment.
        pr_number: GitHub PR number.
        head_sha: Commit SHA at review time.
        round_n: Review round index.

    Returns:
        A Finding with status=PROPOSED and claim_type from classify_claim_type.
    """
    return Finding(
        pr_number=pr_number,
        head_sha=head_sha,
        round=round_n,
        finder=role.value,
        claim_type=classify_claim_type(comment.body, role),
        severity=SEVERITY_MAP.get(comment.severity, Severity.ADVISORY),
        file=comment.file,
        line_start=comment.line,
        line_end=comment.line,
        claim=comment.body[:500],
        evidence_pointer=f"{comment.file}:{comment.line} — {comment.body[:200]}",
    )


def oversized_finding(
    path: str,
    *,
    pr_number: int,
    head_sha: str,
    round_n: int,
) -> Finding:
    """Build the fail-closed BLOCKING finding for one oversized-omitted file.

    `scope_diff` already confirmed, by measuring `unit.chars` against
    `cap_chars`, that ``path`` is unreviewable at this size and never
    shown to any reviewer lens (SP-DIFF-SCOPER-OVERSIZE-BLOCK). That
    measurement is the verification, so the finding is recorded
    ``verified`` directly rather than ``proposed`` — no further judging
    round is needed before it counts as an open blocking finding in
    :func:`merge_gate.gather_merge_facts`. ``claim_type`` is
    ``spec_gap`` (a member of
    :data:`~repoach.review.findings.JUDGED_CLAIM_TYPES`, per the gate's
    blocking partition), so the finding clears the same way any other
    judged finding does — through the findings-driven Coder/refuter
    lifecycle — once the file is split or shrunk below the cap.

    Args:
        path: Repo-relative path dropped from every reviewer prompt
            because its diff exceeds the scoping cap
            (``ScopedDiff.oversized``).
        pr_number: GitHub PR number.
        head_sha: Commit SHA the diff was scoped at.
        round_n: Review round index.

    Returns:
        A `spec_gap`/BLOCKING :class:`Finding` naming ``path``.
    """
    return Finding(
        pr_number=pr_number,
        head_sha=head_sha,
        round=round_n,
        finder=_OVERSIZED_FINDER,
        claim_type=ClaimType.SPEC_GAP,
        severity=Severity.BLOCKING,
        file=path,
        line_start=0,
        line_end=0,
        claim=(
            f"{path} exceeds the diff-scoping character cap and was omitted "
            "from every reviewer's prompt — unreviewed content cannot merge."
        ),
        evidence_pointer=f"{path}: oversized, omitted by diff_scoper.scope_diff",
        status=FindingStatus.VERIFIED,
        verification_method="diff_scoper",
        verification_result="file size exceeds cap_chars at this head",
        checked_at_sha=head_sha,
    )


def record_oversized_findings(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str | None,
    round_n: int,
    scoped: ScopedDiff,
) -> int:
    """Persist one fail-closed BLOCKING finding per oversized-omitted file.

    `scope_diff` stays a pure function (SP-DIFF-SCOPER-OVERSIZE-BLOCK
    NG3); this is the caller's recording step, given the `ScopedDiff`
    it already produced. `ScopedDiff.oversized` names files whose
    individual diff exceeds the cap — never shown to any reviewer lens
    — so each one gets its own finding via :func:`oversized_finding`.
    A merely budget-omitted file (fits the cap alone, dropped only
    because the running total was exceeded) is never in ``oversized``
    and never blocks here (NG2) — the nominal case with no oversized
    file records nothing and leaves the gate unaffected.

    Args:
        db_path: The findings ledger database.
        pr_number: GitHub PR number.
        head_sha: Commit SHA the diff was scoped at (``None`` becomes ``""``).
        round_n: Review round index.
        scoped: The `ScopedDiff` the reviewer pass just produced.

    Returns:
        Number of oversized-blocking findings recorded.
    """
    if not scoped.oversized:
        return 0
    init_findings_schema(db_path)
    effective_sha = head_sha or ""
    for path in scoped.oversized:
        record_finding(
            db_path,
            oversized_finding(
                path,
                pr_number=pr_number,
                head_sha=effective_sha,
                round_n=round_n,
            ),
        )
    _log.warning(
        "findings_bridge.oversized_blocking_recorded",
        pr_number=pr_number,
        n_oversized=len(scoped.oversized),
        oversized=scoped.oversized[:10],
    )
    return len(scoped.oversized)


def _reopen_refuted_matches(
    db_path: Path,
    refuted_rows: list[Finding],
    finding: Finding,
) -> int:
    """Re-open previously refuted findings the new *finding* re-raises.

    A refuted row matches when it cites the same file, carries the same
    claim type, and its claim text is similar past
    :data:`~repoach.review.thread_context.REPEAT_SIMILARITY_THRESHOLD`.
    Each match transitions REFUTED -> PROPOSED
    (SP-REFUTER-INJECTION-HARDEN G3) so the finding is judged afresh
    and the refuted-finding sentinel stops defending the dismissal — an
    injected refutation verdict can no longer bury a blocking finding
    permanently. Matched rows are removed from *refuted_rows* so one
    re-raise never re-opens the same row twice in a pass.
    """
    reopened = 0
    for prior in list(refuted_rows):
        if prior.id is None or prior.file != finding.file:
            continue
        if prior.claim_type is not finding.claim_type:
            continue
        if similarity(prior.claim, finding.claim) < REPEAT_SIMILARITY_THRESHOLD:
            continue
        if update_finding_status(
            db_path,
            prior.id,
            FindingStatus.PROPOSED,
            verification_method="reraise",
            verification_result=f"re-raised by {finding.finder} in round {finding.round}",
        ):
            reopened += 1
            refuted_rows.remove(prior)
            _log.info(
                "findings_bridge.refuted_reopened",
                pr_number=finding.pr_number,
                finding_id=prior.id,
                file=prior.file,
                claim_type=prior.claim_type.value,
                reraised_by=finding.finder,
            )
    return reopened


def record_findings_for_outcomes(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str | None,
    outcomes: list[ReviewerOutcome],
    round_n: int,
    diff: str,
) -> int:
    """Persist one Finding per in-diff comment across all valid outcomes.

    Calls init_findings_schema once; skips any outcome where _is_unparsed
    returns True; skips any comment whose file is not touched by ``diff``
    (an off-diff comment cites a path the PR does not change — the legacy
    arbiter's filter, ported here so the findings-driven Coder never
    fixes a file the PR never touched); returns the recorded count.
    head_sha=None is stored as empty string. When ``diff`` is empty or
    malformed (no files parse out) the off-diff filter is disabled and
    every comment is recorded, matching the arbiter's
    never-silently-drop-everything contract.

    A recorded comment that re-raises a previously REFUTED finding
    (same file, same claim type, similar claim) also re-opens that
    finding to PROPOSED via :func:`_reopen_refuted_matches`
    (SP-REFUTER-INJECTION-HARDEN G3).

    Args:
        db_path: Path to the SQLite database file.
        pr_number: GitHub PR number.
        head_sha: Commit SHA at review time (None becomes "").
        outcomes: All reviewer outcomes from the run.
        round_n: Review round index (1 or 2).
        diff: The PR's unified diff; comments off it are dropped.

    Returns:
        Number of findings recorded.
    """
    init_findings_schema(db_path)
    effective_sha = head_sha or ""
    files_in_diff = _files_in_diff(diff)
    diff_filter_enabled = bool(files_in_diff)
    refuted_rows = list(fetch_findings(db_path, pr_number, status=FindingStatus.REFUTED))
    count = 0
    skipped = 0
    reopened = 0
    for outcome in outcomes:
        if _is_unparsed(outcome):
            continue
        for comment in outcome.comments:
            if diff_filter_enabled and comment.file not in files_in_diff:
                skipped += 1
                continue
            finding = comment_to_finding(
                comment,
                role=outcome.role,
                pr_number=pr_number,
                head_sha=effective_sha,
                round_n=round_n,
            )
            record_finding(db_path, finding)
            count += 1
            reopened += _reopen_refuted_matches(db_path, refuted_rows, finding)
    if skipped:
        _log.info("findings_bridge.off_diff_skipped", pr_number=pr_number, n_skipped=skipped)
    if reopened:
        _log.info(
            "findings_bridge.refuted_reopened_total", pr_number=pr_number, n_reopened=reopened
        )
    return count
