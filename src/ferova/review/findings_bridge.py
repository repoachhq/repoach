"""Bridge: derive Finding records from ReviewerOutcome comments (dual-run, SP-FINDER-OUTPUT).

Provisional claim_type defaults map each lens to a ClaimType bucket.
Verifier slices 4-5 own the refinement; proposed status signals unverified.
"""

from __future__ import annotations

from pathlib import Path

from ..core.logging import get_logger
from .findings import ClaimType, Finding, Severity, init_findings_schema, record_finding
from .reviewer import BotRole, ReviewComment, ReviewerOutcome

_log = get_logger(__name__)

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
    historical no-filter behaviour). This mirrors
    ``coder_loop._files_in_diff`` deliberately; the duplicate is temporary
    until the legacy arbiter is retired with that module.

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
        A Finding with status=PROPOSED and claim_type from LENS_DEFAULT_CLAIM_TYPE.
    """
    return Finding(
        pr_number=pr_number,
        head_sha=head_sha,
        round=round_n,
        finder=role.value,
        claim_type=LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN),
        severity=SEVERITY_MAP.get(comment.severity, Severity.ADVISORY),
        file=comment.file,
        line_start=comment.line,
        line_end=comment.line,
        claim=comment.body[:500],
        evidence_pointer=f"{comment.file}:{comment.line} — {comment.body[:200]}",
    )


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
    count = 0
    skipped = 0
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
    if skipped:
        _log.info("findings_bridge.off_diff_skipped", pr_number=pr_number, n_skipped=skipped)
    return count
