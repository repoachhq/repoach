"""Review learning loop — distill verified findings + bench insights (SP-REVIEW-LESSONS).

The write side of the review memory loop (recall lives in
:mod:`review_memory`). At a review cycle's end the findings that survived
verification — the problems the builder actually shipped — are distilled
into builder-scoped agentmemory lessons, so the Planner recalls them
before the next build (closing the short learning loop). Refuted findings
are NEVER learned from (that would teach the bench its own
hallucinations); they feed only the per-lens *precision* view of the
insights report.

The insights side aggregates the ledger into a status / claim-type
distribution and a per-lens precision metric — what fraction of each
reviewer's findings that reached a verdict were confirmed real rather than
refuted — surfacing which lenses hallucinate and which judge well.

Slice 11 of the evidence-first redesign.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from ..memory import agentmemory_client
from .builder_memory import BUILDER_PROJECT
from .findings import Finding, FindingStatus, fetch_all_findings, fetch_findings

_log = get_logger(__name__)

_CONFIRMED_REAL: frozenset[FindingStatus] = frozenset(
    {
        FindingStatus.VERIFIED,
        FindingStatus.OPEN,
        FindingStatus.RESOLVED,
        FindingStatus.STUCK,
    }
)
"""Statuses downstream of VERIFIED — the finding was confirmed a real problem."""


@dataclass(frozen=True)
class LensPrecision:
    """One reviewer lens's verified-vs-refuted track record.

    Attributes:
        finder: The lens identifier (architect / sentinel / tester / scribe).
        confirmed: Findings the lens raised that were confirmed real.
        refuted: Findings the lens raised that the verifier refuted.
        precision: ``confirmed / (confirmed + refuted)``, or ``None`` when
            the lens has no finding that reached a verdict yet.
    """

    finder: str
    confirmed: int
    refuted: int
    precision: float | None


@dataclass(frozen=True)
class FindingsInsights:
    """Aggregate view of the findings ledger.

    Attributes:
        total: Total findings considered.
        by_status: Count of findings per lifecycle status.
        by_claim_type: Count of findings per claim type.
        lens_precision: Per-lens precision, ordered by finder name.
    """

    total: int
    by_status: dict[str, int]
    by_claim_type: dict[str, int]
    lens_precision: list[LensPrecision]


def distill_verified_lessons(db_path: Path, pr_number: int) -> list[str]:
    """Distill a PR's confirmed-real findings into builder lesson strings.

    Only findings downstream of VERIFIED are kept (refuted / still-proposed
    ones are excluded); duplicates across rounds are collapsed by
    claim-type + file + claim prefix.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose findings to distill.

    Returns:
        One concise lesson per distinct confirmed-real finding.
    """
    seen: set[tuple[str, str, str]] = set()
    lessons: list[str] = []
    for finding in fetch_findings(db_path, pr_number):
        if finding.status not in _CONFIRMED_REAL:
            continue
        key = (finding.claim_type.value, finding.file, finding.claim[:80])
        if key in seen:
            continue
        seen.add(key)
        lessons.append(f"[{finding.claim_type.value}] {finding.file} — {finding.claim[:200]}")
    return lessons


def remember_verified_findings(
    db_path: Path,
    pr_number: int,
    *,
    remember_fn=agentmemory_client.remember,
) -> int:
    """Distill a PR's confirmed findings and write them to builder memory.

    Gated on ``review_lessons_enabled``; degrades gracefully (the client
    never raises, so a down service simply writes nothing). Lessons land in
    ``project=builder`` so the Planner recalls them before the next build.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose findings to learn from.
        remember_fn: The agentmemory write callable (injected in tests).

    Returns:
        The number of lessons the service accepted.
    """
    settings = get_settings()
    if not settings.review_lessons_enabled:
        return 0
    lessons = distill_verified_lessons(db_path, pr_number)
    written = 0
    for lesson in lessons:
        if remember_fn(lesson, project=BUILDER_PROJECT, base_url=settings.agentmemory_url):
            written += 1
    if written:
        _log.info("review_lessons.remembered", pr_number=pr_number, n_lessons=written)
    return written


def compute_lens_precision(findings: list[Finding]) -> list[LensPrecision]:
    """Compute each lens's verified-vs-refuted precision over *findings*.

    A finding counts as confirmed when its status is downstream of
    VERIFIED, and refuted when its status is REFUTED; still-proposed
    findings (no verdict yet) are ignored. Lenses with no settled finding
    report ``precision=None``.

    Args:
        findings: The findings to aggregate.

    Returns:
        One :class:`LensPrecision` per lens, ordered by finder name.
    """
    confirmed: dict[str, int] = defaultdict(int)
    refuted: dict[str, int] = defaultdict(int)
    for finding in findings:
        if finding.status in _CONFIRMED_REAL:
            confirmed[finding.finder] += 1
        elif finding.status is FindingStatus.REFUTED:
            refuted[finding.finder] += 1
    lenses = sorted(set(confirmed) | set(refuted))
    out: list[LensPrecision] = []
    for finder in lenses:
        c, r = confirmed[finder], refuted[finder]
        total = c + r
        out.append(
            LensPrecision(
                finder=finder, confirmed=c, refuted=r, precision=(c / total if total else None)
            )
        )
    return out


def compute_insights(findings: list[Finding]) -> FindingsInsights:
    """Aggregate *findings* into a status/claim-type/precision view.

    Args:
        findings: The findings to aggregate (one PR's, or the whole ledger).

    Returns:
        The assembled :class:`FindingsInsights`.
    """
    by_status: dict[str, int] = defaultdict(int)
    by_claim_type: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_status[finding.status.value] += 1
        by_claim_type[finding.claim_type.value] += 1
    return FindingsInsights(
        total=len(findings),
        by_status=dict(sorted(by_status.items())),
        by_claim_type=dict(sorted(by_claim_type.items())),
        lens_precision=compute_lens_precision(findings),
    )


def render_lens_track_record(
    db_path: Path, role: str, limit: int = 3, *, max_chars: int = 1200
) -> str:
    """Render a lens's refuted track record for prompt injection.

    Computes the lens's verified-vs-refuted precision over the full ledger,
    then lists the latest ``limit`` refuted findings newest-first.  The
    section is truncated to ``max_chars`` and degrades to ``""`` on any
    database error.

    Args:
        db_path: The findings ledger database.
        role: The lens identifier (e.g. ``"scribe"``).
        limit: How many recent refuted findings to include.
        max_chars: Hard cap on the rendered section length.

    Returns:
        A bounded text section, or ``""`` when the lens has no refutations
        or when the database is unreachable.
    """
    try:
        findings = fetch_all_findings(db_path)
    except Exception as exc:
        _log.warning("review.track_record.db_error", role=role, error=str(exc))
        return ""

    lens_precision = compute_lens_precision(findings)
    precision_info = next((lp for lp in lens_precision if lp.finder == role), None)

    refuted = [f for f in findings if f.status is FindingStatus.REFUTED and f.finder == role]
    if not refuted:
        return ""

    refuted = sorted(refuted, key=lambda f: f.id or 0, reverse=True)

    heading = "Your recent refuted claims — do not re-raise without new evidence"

    if precision_info is not None and precision_info.precision is not None:
        total = precision_info.confirmed + precision_info.refuted
        precision_line = (
            f"precision {precision_info.confirmed}/{total} ({precision_info.precision:.2f})"
        )
    else:
        precision_line = "precision 0/0 (N/A)"

    lines: list[str] = [heading, precision_line]

    for finding in refuted[:limit]:
        if not finding.file or not finding.claim:
            continue
        claim_summary = finding.claim[:120]
        refuter_text = finding.verification_result[:200] if finding.verification_result else "N/A"
        lines.append(f"- {finding.file}: {claim_summary} — refuter: {refuter_text}")

    section = "\n".join(lines)

    if len(section) > max_chars:
        section = section[: max_chars - 3] + "..."

    return section


def gather_insights(db_path: Path, *, pr_number: int | None = None) -> FindingsInsights:
    """Load findings (one PR or the whole ledger) and compute insights.

    Args:
        db_path: The findings ledger database.
        pr_number: Restrict to one PR; ``None`` aggregates every PR.

    Returns:
        The assembled :class:`FindingsInsights`.
    """
    findings = (
        fetch_all_findings(db_path) if pr_number is None else fetch_findings(db_path, pr_number)
    )
    return compute_insights(findings)
