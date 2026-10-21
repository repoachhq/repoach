"""Re-check, at the head the gate decides on, the findings of older heads.

SP-GATE-REVERIFIES-AT-HEAD. A finding raised at head A stays in the
ledger when a person pushes head B with the fix; before this module the
only path from ``verified`` to a settled state was the Coder loop's own
re-check (:func:`~repoach.review.coder_findings.reverify_resolution_for_pr`),
so the gate kept refusing a fixed pull request forever.

:func:`reverify_older_findings` runs before the gate gathers its facts.
Every blocking finding still unsettled (``proposed``, ``verified``,
``open`` or ``stuck``) and recorded at another head is re-checked
against the head tree, and ends in exactly one of three states:

* ``resolved`` — the head no longer supports it; the row moves to the
  terminal ``resolved_at_head`` with ``resolved at <sha7>: <why>``;
* ``still_open`` — the head still supports it; the row stays blocking,
  stamped ``still present at <sha7>: <why>`` at the head;
* ``needs_judgement`` — neither a mechanical check nor the refuter
  could settle it; the row is left as it is (and keeps blocking) and the
  gate names it.

The re-check never reads the checkout: ``repo_root`` is the head tree
the gate pinned (SP-REVIEW-PINS-HEAD G2).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ..core.logging import get_logger
from ..gates.config import GatesConfig
from ..gates.selectors import is_js_test_file
from .finding_verifiers import verify_finding
from .findings import (
    JUDGED_CLAIM_TYPES,
    MECHANICAL_CLAIM_TYPES,
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_verification_attempt,
    update_finding_status,
)
from .refuter import JUDGING_CAP_REACHED, Judge, LazyJudge, make_refuter_judge, refute_finding

_log = get_logger(__name__)

ReverifyState = Literal["resolved", "still_open", "needs_judgement"]
"""The one state every re-checked finding ends in."""

_STATES: tuple[ReverifyState, ...] = ("resolved", "still_open", "needs_judgement")

REVERIFY_METHOD = "head_reverify"
"""``verification_method`` stamped on a finding the gate found still present."""

_UNSETTLED = frozenset(
    {FindingStatus.PROPOSED, FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.STUCK}
)
"""Statuses a blocking finding of an older head is re-checked from."""

_SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build", "vendor"})
"""Directories never walked when looking for test files."""

_DECLARATION = re.compile(
    r"\b(?:function\*?|class|interface|type|enum|const|let|var|def|func|fn)\s+"
    r"([A-Za-z_$][\w$]*)"
)
"""The name a source line declares (TypeScript, JavaScript, Go, Rust, Python)."""


@dataclass(frozen=True)
class Reverification:
    """What the gate's re-check did with one finding of an older head.

    Attributes:
        finding_id: Ledger row id.
        claim_type: The finding's claim type.
        file: The cited file.
        line_start: The first cited line.
        recorded_at: The head the finding was raised at.
        state: ``resolved``, ``still_open`` or ``needs_judgement``.
        reason: Why, in a few words (the check's own result).
    """

    finding_id: int | None
    claim_type: ClaimType
    file: str
    line_start: int
    recorded_at: str
    state: ReverifyState
    reason: str

    def describe(self, head_sha: str) -> str:
        """Return the gate reason naming this finding at ``head_sha``."""
        where = f"{self.claim_type.value} {self.file}:{self.line_start}"
        raised = self.recorded_at[:7] or "an unrecorded head"
        if self.state == "still_open":
            tail = f"still present at {head_sha[:7]} ({self.reason})"
        elif self.state == "needs_judgement":
            tail = f"unresolved: needs judgement ({self.reason})"
        else:
            tail = f"resolved_at_head {head_sha[:7]} ({self.reason})"
        return f"finding {self.finding_id}: {where} raised at {raised}, {tail}"


@dataclass(frozen=True)
class HeadReverification:
    """Every finding of an older head the gate re-checked at ``head_sha``.

    Attributes:
        head_sha: The head the re-check read.
        outcomes: One :class:`Reverification` per re-checked finding.
        model_calls: Judge invocations made.
    """

    head_sha: str
    outcomes: tuple[Reverification, ...] = ()
    model_calls: int = 0

    def counts(self) -> dict[str, int]:
        """Return ``{"resolved", "still_open", "needs_judgement"}`` counts."""
        tally = Counter(outcome.state for outcome in self.outcomes)
        return {state: tally.get(state, 0) for state in _STATES}

    def summary_line(self) -> str:
        """Return the one stderr line of the gate."""
        counts = self.counts()
        return (
            f"gate: re-verified {len(self.outcomes)} finding(s) of older heads at "
            f"{self.head_sha[:7] or '(no head)'}: resolved {counts['resolved']}, "
            f"still open {counts['still_open']}, "
            f"needs judgement {counts['needs_judgement']}"
        )

    def reasons(self) -> list[str]:
        """Return one gate reason per finding still blocking, oldest first."""
        return [
            outcome.describe(self.head_sha)
            for outcome in self.outcomes
            if outcome.state != "resolved"
        ]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only ``git`` command in ``repo_root``."""
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
    )


def _show_at(repo_root: Path, sha: str, path: str) -> str | None:
    """Return ``path`` as committed at ``sha``, or ``None`` when absent there."""
    proc = _git(repo_root, "show", f"{sha}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _commit_known(repo_root: Path, sha: str) -> bool:
    """Whether ``sha`` names a commit reachable from ``repo_root``'s object store."""
    if not sha:
        return False
    return _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _cited_file_absent(finding: Finding, repo_root: Path) -> bool:
    """Whether the finding cites a file that does not exist in the head tree."""
    if not finding.file:
        return False
    root = repo_root.resolve()
    target = (root / finding.file).resolve()
    return target.is_relative_to(root) and not target.exists()


def _declared_symbol(text: str | None, finding: Finding) -> str | None:
    """Return the first name declared on the cited lines of ``text``."""
    if text is None:
        return None
    lines = text.splitlines()
    start = max(finding.line_start, 1)
    end = min(max(finding.line_end, start), start + 5, len(lines))
    for line in lines[start - 1 : end]:
        match = _DECLARATION.search(line)
        if match:
            return match.group(1)
    return None


def _test_file_kind(path: str) -> bool:
    """Whether ``path`` is a JavaScript/TypeScript or Go test file."""
    return is_js_test_file(path) or path.endswith("_test.go")


def _test_files(repo_root: Path) -> Iterator[str]:
    """Yield every JavaScript/TypeScript and Go test file under ``repo_root``."""
    for directory, subdirs, files in os.walk(repo_root):
        subdirs[:] = [d for d in subdirs if d not in _SKIPPED_DIRS]
        for name in files:
            relative = PurePosixPath(Path(directory, name).relative_to(repo_root)).as_posix()
            if _test_file_kind(relative):
                yield relative


def _names(repo_root: Path, path: str, symbol: str) -> bool:
    """Whether the head copy of ``path`` names ``symbol`` as a whole word."""
    try:
        text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("head_reverify.test_file_unreadable", path=path, error=type(exc).__name__)
        return False
    return re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", text) is not None


def _test_presence(finding: Finding, repo_root: Path) -> tuple[ReverifyState, str] | None:
    """Re-check a ``missing_test`` claim on a non-Python file by its test files.

    The symbol is the name declared on the cited lines as they were at
    the finding's head. A test file of the language that names it and
    is new or changed since that head resolves the claim; none naming
    it keeps it open. ``None`` when no symbol is declared there or the
    finding's head is not in the object store (the refuter decides).
    """
    if not _commit_known(repo_root, finding.head_sha):
        return None
    symbol = _declared_symbol(_show_at(repo_root, finding.head_sha, finding.file), finding)
    if symbol is None:
        return None
    naming = [path for path in _test_files(repo_root) if _names(repo_root, path, symbol)]
    if not naming:
        return "still_open", f"no test file names {symbol}"
    for path in naming:
        before = _show_at(repo_root, finding.head_sha, path)
        after = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        if before != after:
            return "resolved", f"{path} names {symbol}"
    return "still_open", f"{naming[0]} named {symbol} already at {finding.head_sha[:7]}"


def _mechanical(
    finding: Finding, repo_root: Path, config: GatesConfig
) -> tuple[ReverifyState, str, str] | None:
    """Re-check a mechanical claim; ``None`` when no mechanical check settles it."""
    if finding.claim_type is ClaimType.MISSING_TEST and not finding.file.endswith(".py"):
        settled = _test_presence(finding, repo_root)
        if settled is None:
            return None
        return settled[0], "test_presence", settled[1]
    status, method, result = verify_finding(finding, repo_root=repo_root, config=config)
    if status is FindingStatus.REFUTED:
        return "resolved", method, result
    if status is FindingStatus.VERIFIED:
        return "still_open", method, result
    return None


def _judged(
    finding: Finding, repo_root: Path, judge: LazyJudge, max_judged: int
) -> tuple[ReverifyState, str, str]:
    """Hand a finding to the refuter at the head, within the cap."""
    if judge.calls >= max_judged:
        return "needs_judgement", "refuter", JUDGING_CAP_REACHED.format(cap=max_judged)
    status, reason = refute_finding(finding, repo_root=repo_root, judge=judge)
    if status is FindingStatus.REFUTED:
        return "resolved", "refuter", reason
    if status is FindingStatus.VERIFIED:
        return "still_open", "refuter", reason
    return "needs_judgement", "refuter", reason


def _recheck(
    finding: Finding,
    *,
    repo_root: Path,
    head_sha: str,
    ci_green: bool,
    config: GatesConfig,
    judge: LazyJudge,
    max_judged: int,
) -> tuple[ReverifyState, str, str]:
    """Return ``(state, method, reason)`` for one finding at the head."""
    sha7 = head_sha[:7]
    if finding.claim_type is ClaimType.BROKEN_BEHAVIOR:
        if ci_green:
            return "resolved", "ci", f"CI green at {sha7}"
        return "still_open", "ci", f"CI not green at {sha7}"
    if _cited_file_absent(finding, repo_root):
        return "resolved", "file_presence", f"{finding.file} absent at {sha7}"
    if finding.claim_type in MECHANICAL_CLAIM_TYPES:
        settled = _mechanical(finding, repo_root, config)
        if settled is not None:
            return settled
    elif finding.claim_type not in JUDGED_CLAIM_TYPES:
        _log.info("head_reverify.unknown_claim_type", claim_type=finding.claim_type.value)
    return _judged(finding, repo_root, judge, max_judged)


def _persist(
    db_path: Path, finding: Finding, outcome: Reverification, *, method: str, head: str
) -> None:
    """Record a resolved or still-open outcome on the finding's row at ``head``."""
    if finding.id is None or outcome.state == "needs_judgement":
        return
    sha7 = head[:7]
    if outcome.state == "resolved":
        update_finding_status(
            db_path,
            finding.id,
            FindingStatus.RESOLVED_AT_HEAD,
            verification_method=method,
            verification_result=f"resolved at {sha7}: {outcome.reason}",
            checked_at_sha=head,
        )
        return
    result = f"still present at {sha7}: {outcome.reason}"
    if finding.status is FindingStatus.PROPOSED:
        update_finding_status(
            db_path,
            finding.id,
            FindingStatus.VERIFIED,
            verification_method=REVERIFY_METHOD,
            verification_result=result,
            checked_at_sha=head,
        )
        return
    record_verification_attempt(
        db_path, finding.id, method=REVERIFY_METHOD, result=result, checked_at_sha=head
    )


def _stored_reason(finding: Finding) -> str:
    """Return the reason a previous re-check at this head stored on the row."""
    _, _, reason = finding.verification_result.partition(": ")
    return reason or finding.verification_result


def reverify_older_findings(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str,
    ci_green: bool,
    config: GatesConfig,
    max_judged: int,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
) -> HeadReverification:
    """Re-check every unsettled blocking finding of an older head at ``head_sha``.

    Args:
        db_path: The findings ledger.
        pr_number: The pull request under decision.
        repo_root: The head tree (never the checkout).
        head_sha: The head the gate decides on; empty re-checks nothing.
        ci_green: Whether required checks are green at the head (settles
            ``broken_behavior``).
        config: The trusted gate configuration (the checkout's).
        max_judged: Judge calls allowed in this re-check.
        judge_factory: Builds the refuter judge on its first call.

    Returns:
        The :class:`HeadReverification`; resolved and still-open
        outcomes are already persisted on their rows.
    """
    if not head_sha:
        return HeadReverification(head_sha="")
    init_findings_schema(db_path)
    judge = LazyJudge(judge_factory)
    outcomes: list[Reverification] = []
    for finding in fetch_findings(db_path, pr_number):
        if (
            finding.severity is not Severity.BLOCKING
            or finding.status not in _UNSETTLED
            or finding.head_sha == head_sha
        ):
            continue
        if finding.checked_at_sha == head_sha and finding.verification_method == REVERIFY_METHOD:
            outcomes.append(_outcome(finding, "still_open", _stored_reason(finding)))
            continue
        state, method, reason = _recheck(
            finding,
            repo_root=repo_root,
            head_sha=head_sha,
            ci_green=ci_green,
            config=config,
            judge=judge,
            max_judged=max_judged,
        )
        outcome = _outcome(finding, state, reason)
        _persist(db_path, finding, outcome, method=method, head=head_sha)
        outcomes.append(outcome)
    report = HeadReverification(
        head_sha=head_sha, outcomes=tuple(outcomes), model_calls=judge.calls
    )
    _log.info(
        "head_reverify.done",
        pr_number=pr_number,
        head_sha=head_sha[:12],
        model_calls=judge.calls,
        **report.counts(),
    )
    return report


def _outcome(finding: Finding, state: ReverifyState, reason: str) -> Reverification:
    """Build the :class:`Reverification` of ``finding``."""
    return Reverification(
        finding_id=finding.id,
        claim_type=finding.claim_type,
        file=finding.file,
        line_start=finding.line_start,
        recorded_at=finding.head_sha,
        state=state,
        reason=reason,
    )
