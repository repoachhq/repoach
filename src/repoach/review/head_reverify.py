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
  gate names it. The stamp names the refuter's rule version and context
  (:func:`judgement_key`), and a later gate at the same head re-judges it
  when either changed (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G3).

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
from ..gates.config import (
    DEFAULT_REFUTER_BUDGET,
    GatesConfig,
    RefuterBudget,
)
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
from .generated_artefacts import settle_at_head, settlements_at_head
from .refuter import JUDGING_CAP_REACHED, Judge, LazyJudge, judge_finding, make_refuter_judge
from .refuter_evidence import REFUTER_RULE_VERSION
from .spec_gate import vitest_test_bodies

_log = get_logger(__name__)

ReverifyState = Literal["resolved", "still_open", "needs_judgement"]
"""The one state every re-checked finding ends in."""

_STATES: tuple[ReverifyState, ...] = ("resolved", "still_open", "needs_judgement")

REVERIFY_METHOD = "head_reverify"
"""``verification_method`` stamped on a finding the gate found still present."""

NEEDS_JUDGEMENT_METHOD = "head_reverify_needs_judgement"
"""``verification_method`` stamped on a finding no check could settle at a head."""

_CACHED_METHODS = frozenset({REVERIFY_METHOD, NEEDS_JUDGEMENT_METHOD})
"""Stamps a later gate at the same head reuses instead of re-checking."""


def judgement_key(budget: RefuterBudget) -> str:
    """Return what a ``needs judgement`` stamp was judged under: rule version, floor and ceiling.

    A stamp carrying another key (an earlier
    :data:`~repoach.review.refuter_evidence.REFUTER_RULE_VERSION`, another
    ``[gates].refuter_context_tokens`` or ``refuter_context_ceiling``, or
    none) is not reused.

    Args:
        budget: The refuter's floor and ceiling the re-check used.

    Returns:
        ``refuter rule <V>, context <N>, ceiling <C>``, with ``V`` the
        :data:`~repoach.review.refuter_evidence.REFUTER_RULE_VERSION`.
    """
    return f"refuter rule {REFUTER_RULE_VERSION}, context {budget.floor}, ceiling {budget.ceiling}"


_NEEDS_JUDGEMENT_STAMP = re.compile(r"^needs judgement at (?P<sha7>\S*) \((?P<key>[^)]*)\): ")
"""The head of a keyed ``needs judgement`` stamp, as :func:`needs_judgement_stamp` writes it."""


def needs_judgement_stamp(sha7: str, key: str, reason: str) -> str:
    """Return the ``verification_result`` a ``needs judgement`` outcome is stamped with.

    Args:
        sha7: The head the finding was re-checked at, abbreviated.
        key: The :func:`judgement_key` it was judged under.
        reason: The deferral reason.

    Returns:
        ``needs judgement at <sha7> (<key>): <reason>``, which
        :func:`stamp_key` reads back.
    """
    return f"needs judgement at {sha7} ({key}): {reason}"


def stamp_key(result: str) -> str | None:
    """Return the :func:`judgement_key` a ``needs judgement`` stamp carries, ``None`` if it has none.

    Args:
        result: A row's ``verification_result``.

    Returns:
        The key :func:`needs_judgement_stamp` wrote, or ``None`` for a
        stamp written before the key existed or any other text.
    """
    match = _NEEDS_JUDGEMENT_STAMP.match(result)
    return match.group("key") if match else None


NO_HEAD_TREE = "no head tree pinned"
"""Why the re-check is skipped when the gate does not read the head tree."""

_UNSETTLED = frozenset(
    {FindingStatus.PROPOSED, FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.STUCK}
)
"""Statuses a blocking finding of an older head is re-checked from."""

_SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build", "vendor"})
"""Directories never walked when looking for test files."""

_JS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"})
"""Suffixes of a cited file whose tests are looked for in Vitest test files."""

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
        budget_used: Refuter context tokens the re-check used; 0 when
            the refuter assembled no evidence.
    """

    finding_id: int | None
    claim_type: ClaimType
    file: str
    line_start: int
    recorded_at: str
    state: ReverifyState
    reason: str
    budget_used: int = 0

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
        skipped: Why nothing was re-checked (empty when the re-check
            ran); the findings of older heads then keep blocking.
    """

    head_sha: str
    outcomes: tuple[Reverification, ...] = ()
    model_calls: int = 0
    skipped: str = ""

    def counts(self) -> dict[str, int]:
        """Return ``{"resolved", "still_open", "needs_judgement"}`` counts."""
        tally = Counter(outcome.state for outcome in self.outcomes)
        return {state: tally.get(state, 0) for state in _STATES}

    def summary_line(self, prefix: str = "gate") -> str:
        """Return the one stderr line of the command named by ``prefix``.

        Args:
            prefix: ``gate`` for ``review gate`` / ``review merge``,
                ``review`` for ``review pr``.
        """
        if self.skipped:
            return f"{prefix}: findings of older heads not re-verified ({self.skipped})"
        counts = self.counts()
        return (
            f"{prefix}: re-verified {len(self.outcomes)} finding(s) of older heads at "
            f"{self.head_sha[:7] or '(no head)'}: resolved {counts['resolved']}, "
            f"still open {counts['still_open']}, "
            f"needs judgement {counts['needs_judgement']}"
        )

    def reasons(self) -> list[str]:
        """Return one gate reason per finding still blocking, oldest first."""
        if self.skipped:
            return [f"findings of older heads not re-verified ({self.skipped})"]
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


def _test_files(repo_root: Path) -> Iterator[str]:
    """Yield every JavaScript/TypeScript test file under ``repo_root``."""
    for directory, subdirs, files in os.walk(repo_root):
        subdirs[:] = [d for d in subdirs if d not in _SKIPPED_DIRS]
        for name in files:
            relative = PurePosixPath(Path(directory, name).relative_to(repo_root)).as_posix()
            if is_js_test_file(relative):
                yield relative


def _tests_exercise(repo_root: Path, path: str, symbol: str) -> bool:
    """Whether a running ``it`` / ``test`` block of ``path`` uses ``symbol`` in its code.

    Only the code of a non-skipped block's callback counts (comments and
    string literals dropped): a ``// TODO``, an ``it.skip`` / ``it.todo``,
    a test title or an exported string naming the symbol do not.
    """
    try:
        text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("head_reverify.test_file_unreadable", path=path, error=type(exc).__name__)
        return False
    word = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")
    return any(word.search(body) for body in vitest_test_bodies(text))


def _test_presence(finding: Finding, repo_root: Path) -> tuple[ReverifyState, str] | None:
    """Re-check a ``missing_test`` claim on a JavaScript/TypeScript file by its tests.

    The symbol is the name declared on the cited lines as they were at
    the finding's head. A Vitest test file that is new or changed since
    that head, with a running ``it`` / ``test`` block whose code uses the
    symbol, resolves the claim; none keeps it open. ``None`` when the
    cited file is not JavaScript/TypeScript, no symbol is declared there,
    or the finding's head is not in the object store (the refuter decides).
    """
    if PurePosixPath(finding.file).suffix not in _JS_SUFFIXES:
        return None
    if not _commit_known(repo_root, finding.head_sha):
        return None
    symbol = _declared_symbol(_show_at(repo_root, finding.head_sha, finding.file), finding)
    if symbol is None:
        return None
    naming = [path for path in _test_files(repo_root) if _tests_exercise(repo_root, path, symbol)]
    if not naming:
        return "still_open", f"no test exercises {symbol}"
    for path in naming:
        before = _show_at(repo_root, finding.head_sha, path)
        after = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        if before != after:
            return "resolved", f"{path} exercises {symbol}"
    return "still_open", f"{naming[0]} exercised {symbol} already at {finding.head_sha[:7]}"


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
    finding: Finding,
    repo_root: Path,
    judge: LazyJudge,
    max_judged: int,
    cap: int,
    budget: RefuterBudget,
) -> tuple[ReverifyState, str, str, int]:
    """Hand a finding to the refuter at the head, within ``max_judged`` calls.

    A finding past the budget names the configured ``cap``, which is
    larger than ``max_judged`` when the caller already spent part of it.
    The last item is the context the refuter used (``budget_used``).
    """
    if judge.calls >= max_judged:
        return "needs_judgement", "refuter", JUDGING_CAP_REACHED.format(cap=cap), 0
    judgement = judge_finding(
        finding,
        repo_root=repo_root,
        judge=judge,
        budget=budget,
    )
    status, reason, used = judgement.status, judgement.reason, judgement.budget_used
    if status is FindingStatus.REFUTED:
        return "resolved", "refuter", reason, used
    if status is FindingStatus.VERIFIED:
        return "still_open", "refuter", reason, used
    return "needs_judgement", "refuter", reason, used


def _recheck(
    finding: Finding,
    *,
    repo_root: Path,
    head_sha: str,
    ci_green: bool,
    config: GatesConfig,
    judge: LazyJudge,
    max_judged: int,
    cap: int,
    budget: RefuterBudget,
) -> tuple[ReverifyState, str, str, int]:
    """Return ``(state, method, reason, budget_used)`` for one finding at the head."""
    sha7 = head_sha[:7]
    if finding.claim_type is ClaimType.BROKEN_BEHAVIOR:
        if ci_green:
            return "resolved", "ci", f"CI green at {sha7}", 0
        return "still_open", "ci", f"CI not green at {sha7}", 0
    settled = _settled_on_disk(finding, repo_root=repo_root, head_sha=head_sha, config=config)
    if settled is not None:
        return (*settled, 0)
    if finding.claim_type not in MECHANICAL_CLAIM_TYPES | JUDGED_CLAIM_TYPES:
        _log.info("head_reverify.unknown_claim_type", claim_type=finding.claim_type.value)
    return _judged(finding, repo_root, judge, max_judged, cap, budget)


def _settled_on_disk(
    finding: Finding, *, repo_root: Path, head_sha: str, config: GatesConfig
) -> tuple[ReverifyState, str, str] | None:
    """Settle a finding from the head tree alone, without a model call.

    ``None`` when neither the cited file's absence nor a mechanical check
    settles it (the refuter decides).
    """
    if _cited_file_absent(finding, repo_root):
        return "resolved", "file_presence", f"{finding.file} absent at {head_sha[:7]}"
    if finding.claim_type in MECHANICAL_CLAIM_TYPES:
        return _mechanical(finding, repo_root, config)
    return None


def _persist(
    db_path: Path,
    finding: Finding,
    outcome: Reverification,
    *,
    method: str,
    head: str,
    key: str,
) -> None:
    """Record an outcome on the finding's row at ``head``.

    A ``needs_judgement`` outcome is stamped (status unchanged), with the
    :func:`judgement_key` it was judged under, so a later gate at the same
    head under the same key makes no second model call, except when the
    judging cap deferred it: that finding was never judged, and the next
    gate spends its cap on it.
    """
    if finding.id is None:
        return
    sha7 = head[:7]
    if outcome.state == "needs_judgement":
        if outcome.reason.startswith(JUDGING_CAP_REACHED.split("{", 1)[0]):
            return
        record_verification_attempt(
            db_path,
            finding.id,
            method=NEEDS_JUDGEMENT_METHOD,
            result=needs_judgement_stamp(sha7, key, outcome.reason),
            checked_at_sha=head,
            budget_used=outcome.budget_used or None,
        )
        return
    if outcome.state == "resolved":
        update_finding_status(
            db_path,
            finding.id,
            FindingStatus.RESOLVED_AT_HEAD,
            verification_method=method,
            verification_result=f"resolved at {sha7}: {outcome.reason}",
            checked_at_sha=head,
            budget_used=outcome.budget_used or None,
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
            budget_used=outcome.budget_used or None,
        )
        return
    record_verification_attempt(
        db_path,
        finding.id,
        method=REVERIFY_METHOD,
        result=result,
        checked_at_sha=head,
        budget_used=outcome.budget_used or None,
    )


def _cached(finding: Finding, head_sha: str, key: str) -> Reverification | None:
    """Return the outcome a previous gate at ``head_sha`` stamped on ``finding``.

    ``broken_behavior`` is never reused: its re-check is CI at the head,
    which a later gate may read green where an earlier one read pending.
    A mechanical claim's stamp is reused only when no on-disk check
    settles it now: that check costs no model call, and a stamp left by
    an earlier, narrower check must not outlive it (G11). A ``needs
    judgement`` stamp is reused only under the same :func:`judgement_key`.
    """
    if (
        finding.claim_type is ClaimType.BROKEN_BEHAVIOR
        or finding.checked_at_sha != head_sha
        or finding.verification_method not in _CACHED_METHODS
    ):
        return None
    if (
        finding.verification_method == NEEDS_JUDGEMENT_METHOD
        and stamp_key(finding.verification_result) != key
    ):
        return None
    state: ReverifyState = (
        "needs_judgement" if finding.verification_method == NEEDS_JUDGEMENT_METHOD else "still_open"
    )
    return _outcome(finding, state, _stored_reason(finding))


def _claim_key(finding: Finding) -> tuple[str, str, int, int, str]:
    """Return what makes two findings the same claim (raised by several reviewers)."""
    return (
        finding.claim_type.value,
        finding.file,
        finding.line_start,
        finding.line_end,
        " ".join(finding.claim.split()).lower(),
    )


def _stored_reason(finding: Finding) -> str:
    """Return the reason a previous re-check at this head stored on the row."""
    _, _, reason = finding.verification_result.partition(": ")
    return reason or finding.verification_result


def _older_unsettled(finding: Finding, head_sha: str, *, reads_ci: bool = True) -> bool:
    """Whether ``finding`` is an unsettled blocking finding raised at another head.

    A ``broken_behavior`` finding is settled by CI at the head; a caller
    that does not read CI (``reads_ci`` False, ``review pr``) leaves it
    to the gate.
    """
    return (
        finding.severity is Severity.BLOCKING
        and finding.status in _UNSETTLED
        and finding.head_sha != head_sha
        and (reads_ci or finding.claim_type is not ClaimType.BROKEN_BEHAVIOR)
    )


def skipped_reverification(
    db_path: Path, *, pr_number: int, head_sha: str, reason: str, reads_ci: bool = True
) -> HeadReverification:
    """Return the report of a re-check that did not run, for ``reason``.

    The reason is named only when there was something to re-check (an
    unsettled blocking finding raised at another head; ``broken_behavior``
    ones only when ``reads_ci``); otherwise the report is empty, exactly
    like a re-check that found nothing.
    """
    init_findings_schema(db_path)
    pending = any(
        _older_unsettled(f, head_sha, reads_ci=reads_ci) for f in fetch_findings(db_path, pr_number)
    )
    return HeadReverification(head_sha=head_sha, skipped=reason if pending else "")


def reverify_older_findings(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str,
    ci_green: bool | None,
    config: GatesConfig,
    max_judged: int,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
    cap: int | None = None,
    budget: RefuterBudget = DEFAULT_REFUTER_BUDGET,
) -> HeadReverification:
    """Re-check every unsettled blocking finding of an older head at ``head_sha``.

    A ``generated_artefact`` finding, and the oversized finding of a
    path the base now declares, is settled from the ``generated_checks``
    rows of ``head_sha`` before any other check and never reaches the
    refuter (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G3, G6).

    The gate and ``review merge`` call it before gathering their facts;
    ``review pr`` calls it after its refuter pass and before computing
    the team verdict (SP-GATE-REVERIFIES-AT-HEAD G7), with ``ci_green``
    ``None`` because it does not read CI.

    Args:
        db_path: The findings ledger.
        pr_number: The pull request under decision.
        repo_root: The head tree (never the checkout).
        head_sha: The head the gate decides on; empty re-checks nothing.
        ci_green: Whether required checks are green at the head (settles
            ``broken_behavior``); ``None`` when the caller does not read
            CI, and ``broken_behavior`` findings are then not re-checked.
        config: The trusted gate configuration (the checkout's).
        max_judged: Judge calls allowed in this re-check.
        judge_factory: Builds the refuter judge on its first call.
        cap: The configured refuter cap, named in the reason of a
            finding the budget defers; ``max_judged`` when omitted
            (``review pr`` passes the cap it spent part of).
        budget: The refuter's configured floor and ceiling
            (:func:`~repoach.gates.config.resolve_refuter_budget`), which
            the evidence of a re-judged finding fills, as in the review's
            own refuter pass; the defaults when omitted. Part of the
            :func:`judgement_key` a ``needs judgement`` stamp carries, so
            a change of either re-opens the finding at the same head.

    Returns:
        The :class:`HeadReverification`; resolved and still-open
        outcomes are already persisted on their rows.
    """
    if not head_sha:
        return HeadReverification(head_sha="")
    init_findings_schema(db_path)
    judge = LazyJudge(judge_factory)
    budget = RefuterBudget.clamped(*budget)
    current_key = judgement_key(budget)
    outcomes: list[Reverification] = []
    seen: dict[tuple[str, str, int, int, str], tuple[ReverifyState, str, str, int]] = {}
    generated = settlements_at_head(db_path, pr_number, head_sha)
    for finding in fetch_findings(db_path, pr_number):
        if not _older_unsettled(finding, head_sha, reads_ci=ci_green is not None):
            continue
        key = _claim_key(finding)
        settled = settle_at_head(finding, generated)
        if settled is not None:
            seen[key] = (*settled, 0)
        cached = None if settled is not None else _cached(finding, head_sha, current_key)
        if cached is not None:
            if finding.claim_type not in MECHANICAL_CLAIM_TYPES:
                outcomes.append(cached)
                continue
            if key not in seen:
                fresh = _settled_on_disk(
                    finding, repo_root=repo_root, head_sha=head_sha, config=config
                )
                if fresh is None or fresh[0] == cached.state:
                    outcomes.append(cached)
                    continue
                seen[key] = (*fresh, 0)
        if key not in seen:
            seen[key] = _recheck(
                finding,
                repo_root=repo_root,
                head_sha=head_sha,
                ci_green=bool(ci_green),
                config=config,
                judge=judge,
                max_judged=max_judged,
                cap=max_judged if cap is None else cap,
                budget=budget,
            )
        state, method, reason, used = seen[key]
        outcome = _outcome(finding, state, reason, used)
        _persist(db_path, finding, outcome, method=method, head=head_sha, key=current_key)
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


def _outcome(
    finding: Finding, state: ReverifyState, reason: str, budget_used: int = 0
) -> Reverification:
    """Build the :class:`Reverification` of ``finding``."""
    return Reverification(
        finding_id=finding.id,
        claim_type=finding.claim_type,
        file=finding.file,
        line_start=finding.line_start,
        recorded_at=finding.head_sha,
        state=state,
        reason=reason,
        budget_used=budget_used,
    )
