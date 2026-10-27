"""Grade a pull request head against its specs (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS).

:mod:`spec_gate` grades one spec's acceptance contract and stores the
row; this module decides WHICH specs a head is graded against and keeps
their rows fresh: the declared spec, or every governed spec owning a path
of an undeclared PR's diff, read at the base (G7); the refusing row of a
grading that failed; the recomputation of rows graded by a retired rule
(SP-PLAN-SELECTOR-AUDIT-WIRE 0.3); and the redaction of an error that a
refusal publishes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..arch import path_contains
from ..core.logging import get_logger
from .governed_spec import governed_specs_at_ref, ref_resolves
from .spec_gate import (
    DOCS_ROOT,
    SPEC_COVERAGE_RULE_VERSION,
    SpecCoverage,
    coverage_at_head,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    latest_batch,
    record_spec_coverages,
)

_log = get_logger(__name__)

OWNERSHIP_UNRESOLVED = "ownership-unresolved"
"""Spec id and resolution of the row of an undeclared PR whose base could not be read."""


def ownership_unavailable_reason(ref: str) -> str:
    """Return the refusal of an undeclared PR whose specs cannot be resolved (G7, fail closed)."""
    return f"spec resolution by ownership not graded: base ref {ref} unavailable (fail closed)"


@dataclass(frozen=True)
class SpecsByOwnership:
    """The specs owning the paths of an undeclared PR (G7).

    Attributes:
        specs: Spec id to the changed paths it owns, in id order.
        ungoverned: The changed paths outside :data:`DOCS_ROOT` no spec
            owns, in diff order — reported, never refusing.
    """

    specs: dict[str, list[str]] = field(default_factory=dict)
    ungoverned: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Return the report lines of this resolution, one per spec then the ungoverned paths."""
        out = [
            f"spec resolved by ownership: {spec_id} ({len(paths)} paths)"
            for spec_id, paths in self.specs.items()
        ]
        if self.ungoverned:
            out.append(f"ungoverned paths: {', '.join(self.ungoverned)}")
        return out


def specs_owning(paths: list[str], *, root: Path, ref: str) -> SpecsByOwnership:
    """Return the governed specs owning *paths*, ownership read at *ref*.

    Ownership is the ``owns.code`` registry of SP-ARCH-GRAPH as committed
    at *ref* (the PR's base), so a PR cannot disown its code by editing a
    spec's ``owns``. A path goes to the spec owning its longest prefix.

    Args:
        paths: The paths the PR diff touches.
        root: Repository root the git commands run against.
        ref: The base ref the registry is read at.

    Returns:
        The owning specs and the ungoverned paths.

    Raises:
        SpecRefUnavailableError: *ref* does not resolve to a commit.
    """
    governed = governed_specs_at_ref(ref, root=root)
    owners: dict[str, list[str]] = {}
    ungoverned: list[str] = []
    for path in paths:
        best: str | None = None
        best_len = -1
        for spec in governed:
            for owned in spec.owns_code:
                if path_contains(owned, path) and len(owned.rstrip("/")) > best_len:
                    best, best_len = spec.id, len(owned.rstrip("/"))
        if best is not None:
            owners.setdefault(best, []).append(path)
        elif not path_contains(DOCS_ROOT, path):
            ungoverned.append(path)
    return SpecsByOwnership(
        specs={spec_id: owners[spec_id] for spec_id in sorted(owners)}, ungoverned=ungoverned
    )


def readable_base(
    repo_root: Path, base_ref: str | None, fetch_base: Callable[[str], None] | None
) -> str | None:
    """Return *base_ref*, or ``origin/<base_ref>`` after one fetch, when it resolves.

    The one base resolution of an undeclared PR, shared by the review
    (:func:`coverage_rows_at_head`) and ``review gate``.

    Args:
        repo_root: Repository root the git commands run against.
        base_ref: The PR's base branch, or ``None`` when unknown.
        fetch_base: Fetches ``origin/<base_ref>``; ``None`` never fetches.

    Returns:
        The ref that resolves, or ``None``.
    """
    if not base_ref:
        return None

    if ref_resolves(repo_root, base_ref):
        return base_ref
    if fetch_base is not None:
        fetch_base(base_ref)
    remote = f"origin/{base_ref}"
    return remote if ref_resolves(repo_root, remote) else None


@dataclass(frozen=True)
class CoverageGrading:
    """The coverage rows of one PR head and how its specs were resolved.

    Attributes:
        rows: One report per graded spec; empty when nothing resolved.
        ownership: The ownership resolution of an undeclared PR, else ``None``.
        lines: Report lines of the resolution (never refusing).
    """

    rows: list[SpecCoverage]
    ownership: SpecsByOwnership | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        """``declared``, ``ownership`` or ``none`` (G7)."""
        if self.ownership is None:
            if any(row.resolution == OWNERSHIP_UNRESOLVED for row in self.rows):
                return "ownership"
            return "declared" if self.rows else "none"
        return "ownership" if self.ownership.specs else "none"


def coverage_rows_at_head(
    repo_root: Path,
    *,
    spec_id: str | None,
    base_ref: str | None,
    changed_files: list[str],
    fetch_base: Callable[[str], None] | None = None,
) -> CoverageGrading:
    """Grade a PR head against its declared spec, or against the specs owning its code.

    A declared spec is graded alone (:func:`coverage_at_head`). With none
    declared, every governed spec owning a changed path at the base ref
    is graded exactly as if declared (G7), each row marked
    ``resolution="ownership"``. When the base ref cannot be read, the
    ownership cannot be resolved: nothing is graded and the line says so.

    Args:
        repo_root: The PR head tree.
        spec_id: The declared spec, or ``None``.
        base_ref: The PR's base branch, or ``None`` when unknown.
        changed_files: The paths the PR diff touches.
        fetch_base: Fetches ``origin/<base_ref>`` into *repo_root*.

    Returns:
        The rows to record and the resolution lines to report.
    """
    if spec_id is not None:
        coverage, _ = coverage_at_head(
            repo_root,
            spec_id=spec_id,
            base_ref=base_ref,
            changed_files=changed_files,
            fetch_base=fetch_base,
        )
        return CoverageGrading(rows=[coverage.model_copy(update={"resolution": "declared"})])
    ref = readable_base(repo_root, base_ref, fetch_base)
    if ref is None:
        _log.warning("spec_gate.ownership_unresolved", base_ref=base_ref)
        unresolved = SpecCoverage(
            spec_id=OWNERSHIP_UNRESOLVED,
            n_promised=0,
            n_present=0,
            missing=[],
            covered=False,
            draft_refusals=[ownership_unavailable_reason(base_ref or "(unknown)")],
            contract="unavailable",
            resolution=OWNERSHIP_UNRESOLVED,
        )
        return CoverageGrading(rows=[unresolved])
    ownership = specs_owning(changed_files, root=repo_root, ref=ref)
    rows = [
        coverage_at_head(repo_root, spec_id=owner, base_ref=ref, changed_files=changed_files)[
            0
        ].model_copy(update={"resolution": "ownership"})
        for owner in ownership.specs
    ]
    return CoverageGrading(rows=rows, ownership=ownership, lines=ownership.lines())


_URL_USERINFO = re.compile(r"(?<=://)[^/\s@]+@")
"""The ``user[:secret]@`` part of a URL, which a failed fetch may echo."""


def redact_error(text: str) -> str:
    """Return *text* with URL credentials and forge tokens replaced by ``***``.

    A grading error becomes a merge reason that the gate publishes (its
    JSON, notes and PR comment) and a log line; a failed ``git fetch``
    can carry a credentialed remote URL.
    """
    from ..forge.recording import SECRET_VALUE_PATTERNS

    cleaned = _URL_USERINFO.sub("***@", text)
    for pattern in SECRET_VALUE_PATTERNS:
        cleaned = pattern.sub("***", cleaned)
    return cleaned


def failed_grading_row(spec_id: str | None, error: Exception) -> SpecCoverage:
    """Return the not-covered row recorded when the grading of a head raised.

    A grading that raised (a malformed spec, a git failure while
    resolving ownership) must not leave the head without a row: the gate
    reads no row as "coverage unknown" and does not refuse on it
    (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS 0.5). The error is
    published as a merge reason, so it is redacted (:func:`redact_error`).

    Args:
        spec_id: The declared spec, or ``None`` for an undeclared PR.
        error: The exception the grading raised.

    Returns:
        A row with ``contract="unavailable"`` and one refusal line
        ``spec coverage not graded: <Type>: <message> (fail closed)``.
    """
    return SpecCoverage(
        spec_id=str(spec_id) if spec_id else OWNERSHIP_UNRESOLVED,
        n_promised=0,
        n_present=0,
        missing=[],
        covered=False,
        draft_refusals=[
            f"spec coverage not graded: {type(error).__name__}: "
            f"{redact_error(str(error))[:200]} (fail closed)"
        ],
        contract="unavailable",
        resolution="declared" if spec_id else OWNERSHIP_UNRESOLVED,
    )


class StaleCoverageRecomputeError(RuntimeError):
    """A stale coverage row could not be recomputed; its stored verdict is not trusted.

    Attributes:
        head_sha: The head whose row is stale.
        rule_version: The ``rule_version`` of the stored row.
        reason: The gate reason naming the failure and the untrusted rule.
    """

    def __init__(self, *, head_sha: str, rule_version: int, error: Exception) -> None:
        self.head_sha = head_sha
        self.rule_version = rule_version
        self.reason = (
            f"spec coverage recompute failed at {head_sha[:7]}: {type(error).__name__}: "
            f"{redact_error(str(error))[:200]}; stored verdict from rule {rule_version} "
            "not trusted"
        )
        super().__init__(self.reason)


def refresh_stale_coverage(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    repo_root: Path,
    base_ref: str | None,
    changed_files: Callable[[], list[str]],
    fetch_base: Callable[[str], None] | None = None,
) -> list[SpecCoverage] | None:
    """Recompute and record the coverage at *head_sha* when its latest batch is stale.

    A row is stale when its ``rule_version`` is below
    :data:`SPEC_COVERAGE_RULE_VERSION`: it was graded by code whose rule
    has since changed, so it no longer says what the current rule says
    about the same head (SP-PLAN-SELECTOR-AUDIT-WIRE 0.3). The spec is the
    row's own, or the specs owning the diff for a batch resolved by
    ownership; *repo_root* must be the tree of *head_sha*.

    Args:
        db_path: The coverage ledger.
        pr_number: The PR under decision.
        head_sha: The decided head.
        repo_root: The materialised tree of *head_sha*.
        base_ref: The PR's base branch, or ``None`` when unknown.
        changed_files: Returns the paths the PR diff touches; called only
            when a row is stale.
        fetch_base: Fetches ``origin/<base_ref>`` when the base does not
            resolve locally.

    Returns:
        The recorded reports, or ``None`` when no row at head is stale.

    Raises:
        StaleCoverageRecomputeError: The diff, the base fetch or the
            grading failed; nothing was recorded.
    """
    init_spec_coverage_schema(db_path)
    batch = latest_batch(fetch_spec_coverage(db_path, pr_number, head_sha=head_sha))
    stale = [row for row in batch if row.rule_version < SPEC_COVERAGE_RULE_VERSION]
    if not stale:
        return None
    oldest = min(row.rule_version for row in stale)
    by_ownership = batch[0].resolution in {"ownership", OWNERSHIP_UNRESOLVED}
    try:
        grading = coverage_rows_at_head(
            repo_root,
            spec_id=None if by_ownership else batch[-1].spec_id,
            base_ref=base_ref,
            changed_files=changed_files(),
            fetch_base=fetch_base,
        )
    except Exception as exc:
        raise StaleCoverageRecomputeError(
            head_sha=head_sha, rule_version=oldest, error=exc
        ) from exc
    record_spec_coverages(db_path, pr_number=pr_number, head_sha=head_sha, coverages=grading.rows)
    for coverage in grading.rows:
        _log.info(
            "spec_gate.stale_coverage_recomputed",
            pr_number=pr_number,
            head_sha=head_sha[:12],
            spec_id=coverage.spec_id,
            stale_rule_version=oldest,
            covered=coverage.covered,
            n_planned=len(coverage.planned),
            n_missing=len(coverage.missing),
        )
    return grading.rows
