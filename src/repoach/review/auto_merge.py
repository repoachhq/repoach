"""Auto-merge gate: squash-merge a merge-ready PR into ``develop``.

The gate verifies, in order:

1. **Base branch** is exactly ``develop``.  Bots NEVER auto-merge to
   ``main`` — that promotion is reserved for the human owner.
2. **PR state** is ``OPEN`` and ``mergeable``.  Already-merged or
   conflicted PRs short-circuit with an idempotent no-op.
3. **CI gate** (SP-AUTOMERGE-CI-GATE) — ``statusCheckRollup`` must
   not show any required check in FAILURE / CANCELLED / TIMED_OUT /
   ACTION_REQUIRED.  Required checks default to the matrix
   ``Test suite (Python 3.11)`` + ``Test suite (Python 3.13)``;
   missing entries and pending checks past the 12-min deadline are
   refused with structured reasons (``required_check_failed``,
   ``required_check_timed_out``, ``required_check_missing``).
4. **Pure merge gate** (SP-PURE-MERGE-GATE / SP-VERDICT-FLIP 10a) —
   :func:`decide_at_head` re-verifies the findings ledger at the exact
   head: CI green ∧ zero open blocking findings ∧ a complete review ∧
   spec coverage present-when-known.  The self-reported archive verdict
   is NOT consulted (it was a forgeable, stale signal — audit CRITICAL
   #1); the sticky comment is report-only.

When all gates pass, the forge merges with the configured method
(``squash`` by default — ``gh pr merge --squash --delete-branch`` on
GitHub) and the outcome is persisted to L4 ``pr_merges``.

Module-level constants :
    - The ``OUTCOME_*`` names are kept stable so historical L4
      queries keep working.  ``OUTCOME_SKIP_CI`` is the legacy
      umbrella tag preserved for back-compat; new refused-on-CI
      paths emit one of the more specific tags
      (``OUTCOME_SKIP_CI_FAILED``, ``OUTCOME_SKIP_CI_TIMEOUT``,
      ``OUTCOME_SKIP_CI_MISSING``, ``OUTCOME_SKIP_CI_PENDING``).
    - CI facts come from :meth:`repoach.forge.protocol.Forge.ci_status`
      (SP-UNIVERSAL-FORGE G5): on GitHub the required checks of the
      branch protection when readable, else
      :data:`DEFAULT_REQUIRED_CHECK_NAMES` (re-exported here, it mirrors
      the matrix in ``.github/workflows/ci.yml``); on GitLab the MR's
      ``head_pipeline.status``.
    - The CI-gate wait budget and poll cadence are sourced from
      :class:`repoach.core.config.Settings` at call time
      (``automerge_ci_wait_seconds`` / ``automerge_ci_poll_interval``,
      env-tunable as ``REPOACH_AUTOMERGE_CI_WAIT_SECONDS`` /
      ``REPOACH_AUTOMERGE_CI_POLL_INTERVAL``).  ``wait_seconds=0`` is
      a defined fail-fast contract: exactly one rollup evaluation,
      zero ``sleep`` calls (SP-AUTOMERGE-EVENT-DRIVEN).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from ..forge import (
    Forge,
    ForgeError,
    ForgeMergeRefusedError,
    ForgeStateRefusedError,
    MergeMethod,
    PullRequest,
    merge_preferences,
    select_forge,
)
from ..forge.github import DEFAULT_REQUIRED_CHECK_NAMES, classify_required_checks
from ..forge.protocol import WRITABLE_STATES, diff_files
from ..gates.config import GatesConfig, load_gates_config, resolve_refuter_max_judged
from .head_reverify import NO_HEAD_TREE, reverify_older_findings, skipped_reverification
from .merge_gate import (
    MergeDecision,
    MergeFacts,
    compute_merge_decision,
    gather_merge_facts,
)
from .persistence import init_schema, record_merge
from .refuter import Judge, make_refuter_judge
from .review_head import (
    HeadUnavailableError,
    HeadUnresolvedError,
    materialise_head,
    require_head_sha,
)
from .spec_gate import StaleCoverageRecomputeError, refresh_stale_coverage

_log = get_logger(__name__)


@dataclass
class AutoMergeResult:
    """Outcome of one :func:`run_auto_merge` invocation."""

    pr_number: int
    outcome: str
    merged_sha: str | None = None
    notes: str = ""


OUTCOME_MERGED: str = "APPROVE"
OUTCOME_ALREADY_MERGED: str = "ALREADY_MERGED"
OUTCOME_SKIP_BASE: str = "SKIP_BASE"
OUTCOME_SKIP_GATE: str = "SKIP_GATE"
OUTCOME_SKIP_CI: str = "SKIP_CI_RED"
OUTCOME_SKIP_CI_FAILED: str = "SKIP_CI_FAILED"
OUTCOME_SKIP_CI_TIMEOUT: str = "SKIP_CI_TIMEOUT"
OUTCOME_SKIP_CI_MISSING: str = "SKIP_CI_MISSING"
OUTCOME_SKIP_CI_PENDING: str = "SKIP_CI_PENDING"
OUTCOME_SKIP_NOT_APPROVED: str = "SKIP_NOT_APPROVED"
OUTCOME_SKIP_DISCUSSIONS: str = "SKIP_DISCUSSIONS_UNRESOLVED"
OUTCOME_SKIP_CONFLICT: str = "SKIP_CONFLICT"
OUTCOME_SKIP_STATE: str = "SKIP_STATE"
OUTCOME_SKIP_CHANGES_REQUESTED: str = "SKIP_CHANGES_REQUESTED"
OUTCOME_SKIP_DRAFT: str = "SKIP_DRAFT"
OUTCOME_SKIP_BLOCKED: str = "SKIP_BLOCKED"
OUTCOME_SKIP_BEHIND: str = "SKIP_BEHIND"
OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED: str = "SKIP_MERGE_STATUS_UNRECOGNIZED"
OUTCOME_SKIP_STALE_HEAD: str = "SKIP_STALE_HEAD"
OUTCOME_FAILED: str = "FAILED"

SUCCESS_OUTCOMES: frozenset[str] = frozenset({OUTCOME_MERGED, OUTCOME_ALREADY_MERGED})
NON_FATAL_SKIP_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_SKIP_BASE,
        OUTCOME_SKIP_GATE,
        OUTCOME_SKIP_CI,
        OUTCOME_SKIP_CI_FAILED,
        OUTCOME_SKIP_CI_TIMEOUT,
        OUTCOME_SKIP_CI_MISSING,
        OUTCOME_SKIP_CI_PENDING,
        OUTCOME_SKIP_NOT_APPROVED,
        OUTCOME_SKIP_DISCUSSIONS,
        OUTCOME_SKIP_CONFLICT,
        OUTCOME_SKIP_STATE,
        OUTCOME_SKIP_CHANGES_REQUESTED,
        OUTCOME_SKIP_DRAFT,
        OUTCOME_SKIP_BLOCKED,
        OUTCOME_SKIP_BEHIND,
        OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED,
        OUTCOME_SKIP_STALE_HEAD,
    }
)

_REFUSAL_OUTCOMES: dict[str, str] = {
    "pending": OUTCOME_SKIP_CI_PENDING,
    "not_approved": OUTCOME_SKIP_NOT_APPROVED,
    "discussions_not_resolved": OUTCOME_SKIP_DISCUSSIONS,
    "conflict": OUTCOME_SKIP_CONFLICT,
    "head_moved": OUTCOME_SKIP_STALE_HEAD,
    "not_open": OUTCOME_SKIP_STATE,
    "requested_changes": OUTCOME_SKIP_CHANGES_REQUESTED,
    "draft": OUTCOME_SKIP_DRAFT,
    "blocked": OUTCOME_SKIP_BLOCKED,
    "behind": OUTCOME_SKIP_BEHIND,
    "unrecognized": OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED,
}
"""Forge merge refusals read from facts → non-fatal outcomes; ``refused`` stays FAILED."""

__all__ = [
    "DEFAULT_REQUIRED_CHECK_NAMES",
    "classify_required_checks",
]


def merge_exit_code(outcome: str) -> int:
    """Return the process exit code for an auto-merge outcome.

    Returns 0 for success outcomes (merged or already-merged), 5 for
    non-fatal skip outcomes (every gate refusal), and 1 for FAILED or
    any unrecognised string.
    """
    if outcome in SUCCESS_OUTCOMES:
        return 0
    if outcome in NON_FATAL_SKIP_OUTCOMES:
        return 5
    return 1


def _resolve_wait_poll(wait_seconds: int | None, poll_interval: int | None) -> tuple[int, int]:
    """Resolve the CI-gate wait/poll knobs at call time.

    Returns the explicit argument when not ``None``; otherwise reads
    the value from :func:`repoach.core.config.get_settings`
    (``automerge_ci_wait_seconds`` / ``automerge_ci_poll_interval``).
    An explicit argument always wins over the settings default so
    callers (tests, the ``merge-on-ci.yml`` workflow) can override
    the env-sourced budget without mutating the process environment.
    """
    settings = get_settings()
    resolved_wait = wait_seconds if wait_seconds is not None else settings.automerge_ci_wait_seconds
    resolved_poll = (
        poll_interval if poll_interval is not None else settings.automerge_ci_poll_interval
    )
    return resolved_wait, resolved_poll


@dataclass
class CIGateOutcome:
    """Result of :func:`evaluate_ci_gate` — green or refused with reason."""

    green: bool
    outcome_tag: str
    reason: str
    rollup_snapshot: list[dict] = field(default_factory=list)


def evaluate_ci_gate(
    forge: Forge,
    pr_number: int,
    *,
    required_names: Sequence[str] | None = None,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    head_sha: str | None = None,
) -> CIGateOutcome:
    """Block until every required check succeeds, fails, or times out.

    SP-AUTOMERGE-CI-GATE: refuses to merge when any required status
    check is in FAILURE / CANCELLED / TIMED_OUT / ACTION_REQUIRED.
    Pending checks are polled every ``poll_interval`` seconds for up
    to ``wait_seconds`` total before timing out.  Both default from
    :class:`repoach.core.config.Settings` (``REPOACH_AUTOMERGE_CI_WAIT_SECONDS``
    / ``REPOACH_AUTOMERGE_CI_POLL_INTERVAL``) when not given explicitly;
    ``wait_seconds=0`` means exactly one rollup evaluation with zero
    ``sleep`` calls (SP-AUTOMERGE-EVENT-DRIVEN).  Facts come from the
    head commit's check-runs (``head_sha`` when given, else resolved
    per poll so a mid-poll push is still judged at the fresh head);
    the PR ``statusCheckRollup`` is only a fallback when the commit
    API fails. Both live behind :meth:`Forge.ci_status`; ``required_names``
    ``None`` lets the forge resolve them (SP-UNIVERSAL-FORGE G5).
    """
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    deadline = monotonic() + wait_seconds
    last_snapshot: list[dict] = []
    poll_count = 0

    _log.info(
        "auto_merge.ci_gate_started",
        pr_number=pr_number,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        required_names=list(required_names) if required_names is not None else None,
    )

    while True:
        try:
            status = forge.ci_status(pr_number, head_sha=head_sha, required=required_names)
        except ForgeError as exc:
            poll_count += 1
            _log.error(
                "auto_merge.ci_gate_fetch_failed",
                pr_number=pr_number,
                poll=poll_count,
                error=exc.message[:200],
            )
            return CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_CI,
                reason=exc.message,
                rollup_snapshot=[],
            )
        poll_count += 1
        last_snapshot = [
            {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "startedAt": check.started_at,
            }
            for check in status.checks
        ]
        failed, pending, missing = status.failed, status.pending, status.missing
        _log.info(
            "auto_merge.ci_gate_poll",
            pr_number=pr_number,
            poll=poll_count,
            n_failed=len(failed),
            n_pending=len(pending),
            n_missing=len(missing),
            failed=failed[:5],
            pending=pending[:5],
        )
        if failed:
            return CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_CI_FAILED,
                reason=f"required_check_failed: {', '.join(failed)}",
                rollup_snapshot=last_snapshot,
            )
        if missing and not pending:
            return CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_CI_MISSING,
                reason=f"required_check_missing: {', '.join(missing)}",
                rollup_snapshot=last_snapshot,
            )
        if not pending and not missing:
            return CIGateOutcome(
                green=True,
                outcome_tag=OUTCOME_MERGED,
                reason="all required checks succeeded",
                rollup_snapshot=last_snapshot,
            )
        if monotonic() >= deadline:
            return CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_CI_TIMEOUT,
                reason=(
                    "required_check_timed_out: "
                    + ", ".join(pending + missing)
                    + f" (deadline {wait_seconds}s)"
                ),
                rollup_snapshot=last_snapshot,
            )
        sleep(poll_interval)


def required_checks_green(
    forge: Forge,
    pr_number: int,
    *,
    required_names: Sequence[str] | None = None,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Backward-compatible wrapper around :func:`evaluate_ci_gate`.

    Returns ``(green, reason)``.  Callers that need the structured
    outcome tag or rollup snapshot should call ``evaluate_ci_gate``
    directly.
    """
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    outcome = evaluate_ci_gate(
        forge,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )
    return outcome.green, outcome.reason


def _summarise_rollup(rollup: Sequence[dict]) -> str:
    """Compact JSON summary of a status-check rollup for L4 ``notes``.

    Keeps only ``name`` + ``status`` + ``conclusion`` to avoid blowing
    up the 1000-char ``notes`` cap and to stay grep-friendly.
    """
    summary = [
        {
            "name": str(entry.get("name") or ""),
            "status": str(entry.get("status") or ""),
            "conclusion": str(entry.get("conclusion") or ""),
        }
        for entry in rollup
    ]
    return json.dumps(summary, separators=(",", ":"))


def merge_change(
    forge: Forge,
    pr_number: int,
    *,
    method: MergeMethod = MergeMethod.SQUASH,
    delete_branch: bool = True,
    head_sha: str | None = None,
) -> tuple[bool, str | None, str, str]:
    """Merge through the forge and return ``(ok, merge_sha, log, refusal)``.

    On GitHub this runs ``gh pr merge --<method> [--match-head-commit
    <sha>] [--delete-branch]``; the merge commit SHA is extracted from
    its output when present so callers can persist it to L4 for audit.

    Args:
        forge: The forge the PR lives on.
        pr_number: PR / MR to merge.
        method: The configured merge method.
        delete_branch: When True, also delete the head branch.
        head_sha: The head the gate decided at; the forge refuses to
            merge any other.

    Returns:
        ``(ok, merge_sha_or_None, log_tail, refusal)`` — ``refusal`` is
        ``""`` on success, else the forge's
        :attr:`~repoach.forge.ForgeMergeRefusedError.reason`
        (``refused`` for any other forge error).
    """
    try:
        result = forge.merge(
            pr_number, method=method, delete_branch=delete_branch, head_sha=head_sha
        )
    except ForgeMergeRefusedError as exc:
        return False, None, exc.message[-500:], exc.reason
    except ForgeStateRefusedError as exc:
        return False, None, exc.message[-500:], "not_open"
    except ForgeError as exc:
        return False, None, exc.message[-500:], "refused"
    return True, result.sha, result.message[-500:], ""


def _head_sha_or_empty(forge: Forge, pr_number: int) -> str:
    """The forge's head SHA for ``pr_number``, ``""`` when it cannot be read."""
    try:
        return forge.head_sha(pr_number)
    except ForgeError as exc:
        _log.warning(
            "auto_merge.head_sha_unavailable", pr_number=pr_number, error=exc.message[:200]
        )
        return ""


def _pull_request_or_none(forge: Forge, pr_number: int) -> PullRequest | None:
    """The PR metadata, ``None`` when it cannot be read (callers fail closed)."""
    try:
        return forge.pull_request(pr_number)
    except ForgeError as exc:
        _log.warning("auto_merge.pr_view_failed", pr_number=pr_number, error=exc.message[:200])
        return None


@dataclass
class GateEvaluation:
    """The pure-gate evaluation shared by auto-merge and the gate CLI.

    Both paths reach the same :func:`decide_at_head` unit, so the merge
    decision is byte-identical whether it is computed by the CI
    ``auto_merge`` job or by the operator's ``safe_merge.sh`` tool — the
    two can never drift into different answers.

    Attributes:
        head_sha: The head the gate decided against.
        ci_gate: The CI-gate outcome (green flag, outcome tag, reason).
        facts: The merge facts re-verified at ``head_sha``.
        decision: The pure-gate decision over those facts.
        pull_request: The PR / MR as read for this evaluation, when the
            forge answered (its description and branch declare the spec).
        tree: ``worktree`` / ``checkout`` when the ledger was re-verified
            on a pinned head tree (SP-REVIEW-PINS-HEAD), else None.
        changed_files: The paths the PR diff touches, when already known.
        read_changed_files: Reads them from the forge on demand; the gate
            CLI calls it only for a PR declaring no spec, whose specs it
            resolves by ownership (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS G7).
    """

    head_sha: str
    ci_gate: CIGateOutcome
    facts: MergeFacts
    decision: MergeDecision
    pull_request: PullRequest | None = None
    tree: str | None = None
    changed_files: list[str] | None = None
    read_changed_files: Callable[[], list[str]] | None = None


def _unknown_facts() -> MergeFacts:
    """Return merge facts for a gate that stopped before reading the ledger."""
    return MergeFacts(
        head_sha="",
        ci_green=False,
        open_blocking_findings=0,
        spec_covered=False,
        spec_coverage_known=False,
        review_complete=False,
        review_integrity_known=False,
    )


def _ls_remote_tip(forge: Forge, head_ref: str) -> tuple[str | None, str]:
    """Resolve the branch tip via ``git ls-remote origin refs/heads/<head_ref>``.

    Shared by :func:`resolve_verified_head` and the pre-squash re-read in
    :func:`run_auto_merge` — both need the exact same ``ls-remote`` parsing
    and the exact same fail-closed contract (``None`` plus a reason string
    on any transport or parse failure).

    Args:
        forge: The forge; only its local :meth:`~Forge.git` is used.
        head_ref: The branch name to resolve (``headRefName``).

    Returns:
        ``(sha, "")`` on success, ``(None, reason)`` on failure.  The
        reason always names its cause (SP-GATE-MERGED-MR-NAMED-REASON):
        no head branch read, the branch absent from origin (deleted
        after merge, or never pushed), or the ``ls-remote`` exit code
        when git printed nothing — never an empty string after a colon.
    """
    if not head_ref:
        return None, "no head branch: the pull request's source branch could not be read"
    ls_remote = forge.git(["ls-remote", "origin", f"refs/heads/{head_ref}"])
    if ls_remote.ok and not ls_remote.stdout.strip():
        return None, f"branch {head_ref!r} not found on origin (deleted, or never pushed)"
    if not ls_remote.ok:
        error = (ls_remote.stderr or ls_remote.stdout).strip()
        detail = error[:200] if error else f"exit {ls_remote.returncode}, no output"
        return None, f"ls-remote failed: {detail}"
    first_line = ls_remote.stdout.strip().splitlines()[0]
    tokens = first_line.split()
    remote_sha = tokens[0] if tokens else ""
    if not remote_sha:
        return None, f"ls-remote returned no SHA: {first_line[:200]}"
    return remote_sha, ""


def resolve_verified_head(
    forge: Forge,
    pr_number: int,
    head_ref: str,
    *,
    repo_root: Path,
    attempts: int = 4,
    delay_s: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str | None, str]:
    """Cross-check the PR API head against ``git ls-remote`` ground truth.

    SP-AUTOMERGE-FRESH-HEAD (PR #50 orphaned-commit incident,
    2026-07-06): ``gh pr view --json headRefOid`` was observed 40+
    minutes stale, so a merge computed at that head silently orphaned
    a newer commit already sitting on the real branch tip.  This
    helper resolves the branch tip with
    ``git ls-remote origin refs/heads/<head_ref>`` — the git backend's
    own ground truth — and compares it against
    :meth:`Forge.head_sha`, re-polling up to ``attempts`` times with
    ``sleep(delay_s)`` between polls until the two agree.

    The contract is fail-closed: a persistent mismatch, or an
    ``ls-remote`` transport failure, returns ``None`` rather than a
    best-guess SHA — callers must refuse to merge, not merge at a
    head that cannot be verified.

    Args:
        forge: The forge; both :meth:`~Forge.head_sha` and the local
            :meth:`~Forge.git` are used.
        pr_number: PR whose head is under verification.
        head_ref: The PR's head branch name (``headRefName``).
        repo_root: The checkout the caller is verifying against; kept
            explicit in the signature even though ``gh``'s own working
            directory drives the actual ``git`` invocation, so callers
            state unambiguously which clone they mean.
        attempts: Maximum number of API-vs-``ls-remote`` comparisons
            before failing closed.
        delay_s: Seconds passed to ``sleep`` between re-polls.
        sleep: Injectable sleep so tests run instantly.

    Returns:
        ``(converged_sha, "")`` once the API head and the ``ls-remote``
        tip agree.  ``(None, reason)`` fail-closed otherwise: ``reason``
        carries the ``ls-remote`` error text on a transport failure, or
        both 12-char SHA prefixes (``api=...``, ``ls_remote=...``) on a
        persistent mismatch.
    """
    _ = repo_root
    api_sha = ""
    remote_sha = ""
    for attempt in range(attempts):
        tip, reason = _ls_remote_tip(forge, head_ref)
        if tip is None:
            return None, reason
        remote_sha = tip
        api_sha = _head_sha_or_empty(forge, pr_number)
        if api_sha == remote_sha:
            return remote_sha, ""
        if attempt < attempts - 1:
            sleep(delay_s)
    return (
        None,
        "head verification failed after "
        f"{attempts} attempts: api={api_sha[:12]}, ls_remote={remote_sha[:12]}",
    )


def decide_at_head(
    pr_number: int,
    *,
    forge: Forge,
    db: Path,
    ci_green: bool,
    repo_root: Path | None = None,
    head_sha: str | None = None,
    gates_config: GatesConfig | None = None,
    max_judged: int | None = None,
    judge_factory: Callable[[], Judge] | None = None,
    head_tree: bool = False,
    base_ref: str | None = None,
) -> tuple[str, MergeFacts, MergeDecision]:
    """Resolve the head, re-verify the ledger there, and decide.

    Before the facts are gathered, every unsettled blocking finding
    raised at an older head is re-checked at this head
    (:func:`~repoach.review.head_reverify.reverify_older_findings`,
    SP-GATE-REVERIFIES-AT-HEAD): the ones the head no longer supports
    become ``resolved_at_head``, the others are named in the reasons. A
    judged finding is re-judged by the refuter, so the gate can make up
    to ``max_judged`` model calls. The re-check runs only when
    ``head_tree`` says ``repo_root`` is the materialised head tree;
    otherwise it is skipped with a named reason and the findings of
    older heads keep blocking — a checkout on another branch must never
    settle one.

    On the head tree too, a spec-coverage row at head graded by a retired
    rule is recomputed and rewritten first
    (:func:`~repoach.review.spec_gate.refresh_stale_coverage`, no model
    call), so the decision reads what the current rule says. A failed
    recomputation records nothing and refuses whatever the stored verdict
    (``spec coverage recompute failed at <sha7>: <error>; stored verdict
    from rule <v> not trusted``) until a recompute succeeds; a row at the
    current rule is unaffected.

    The single source of the pure merge decision. Both
    :func:`run_auto_merge` (after its CI short-circuit) and
    :func:`evaluate_merge_gate` (for the read-only CLI) call this and
    nothing else to obtain ``(head_sha, facts, decision)`` — extracting
    it is what keeps the CI auto-merge and the local gate from drifting.

    Args:
        pr_number: The PR under decision.
        forge: The forge used to resolve the head SHA when
            ``head_sha`` is not supplied.
        db: The findings + coverage ledger.
        ci_green: Whether required checks are green at head (folded into
            the facts so the decision reasons include CI state).
        repo_root: The head checkout the mechanical verifiers resolve
            against; defaults to the current working directory.
        head_sha: An explicit head to decide at, overriding
            :meth:`Forge.head_sha`.  SP-AUTOMERGE-FRESH-HEAD:
            :func:`run_auto_merge` passes the ``resolve_verified_head``
            SHA here so gate facts are computed at the exact commit
            about to be merged, not whatever the API happens to be
            serving.  ``None`` (the default) keeps today's behaviour for
            other callers such as :func:`evaluate_merge_gate`.
        gates_config: The trusted gate configuration (the checkout's,
            never the PR head tree's); read from ``repo_root`` when omitted.
        max_judged: Refuter calls the re-check may make; ``None`` reads
            :func:`~repoach.gates.config.resolve_refuter_max_judged` at
            ``repo_root``.
        judge_factory: Builds the refuter judge on its first call;
            ``None`` means :func:`~repoach.review.refuter.make_refuter_judge`.
        head_tree: Whether ``repo_root`` is the tree of ``head_sha``
            (:func:`~repoach.review.review_head.materialise_head`).
        base_ref: The PR's base branch, read by the recomputation of a
            stale coverage row (SP-PLAN-SELECTOR-AUDIT-WIRE 0.3).

    Returns:
        ``(head_sha, facts, decision)``.
    """
    resolved_head = head_sha if head_sha is not None else _head_sha_or_empty(forge, pr_number)
    root = repo_root or Path.cwd()
    config = gates_config if gates_config is not None else load_gates_config(root)
    cap = max_judged if max_judged is not None else resolve_refuter_max_judged(root)[0]
    if head_tree:
        reverified = reverify_older_findings(
            db,
            pr_number=pr_number,
            repo_root=root,
            head_sha=resolved_head,
            ci_green=ci_green,
            config=config,
            max_judged=cap,
            judge_factory=judge_factory or make_refuter_judge,
        )
    else:
        reverified = skipped_reverification(
            db, pr_number=pr_number, head_sha=resolved_head, reason=NO_HEAD_TREE
        )
    recompute_failure = ""
    if head_tree and resolved_head:
        recompute_failure = _refresh_coverage(
            db,
            forge=forge,
            pr_number=pr_number,
            head_sha=resolved_head,
            root=root,
            base_ref=base_ref,
        )
    facts = gather_merge_facts(
        db,
        pr_number=pr_number,
        repo_root=root,
        head_sha=resolved_head,
        ci_green=ci_green,
        gates_config=config,
    ).model_copy(
        update={
            "reverified": reverified.counts(),
            "reverify_reasons": reverified.reasons(),
            "reverify_line": reverified.summary_line(),
            "spec_recompute_failure": recompute_failure,
        }
    )
    return resolved_head, facts, compute_merge_decision(facts)


def _refresh_coverage(
    db: Path, *, forge: Forge, pr_number: int, head_sha: str, root: Path, base_ref: str | None
) -> str:
    """Recompute a stale coverage row at *head_sha* on the head tree *root*.

    Returns:
        ``""`` when nothing was stale or the row was recomputed; else the
        refusal reason naming the failure and the untrusted stored rule.
    """

    def fetch_base(ref: str) -> None:
        forge.git(["fetch", "--quiet", "origin", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"])

    try:
        refresh_stale_coverage(
            db,
            pr_number=pr_number,
            head_sha=head_sha,
            repo_root=root,
            base_ref=base_ref,
            changed_files=lambda: diff_files(forge.diff(pr_number).text),
            fetch_base=fetch_base,
        )
    except StaleCoverageRecomputeError as exc:
        _log.warning(
            "auto_merge.stale_coverage_refresh_failed",
            pr_number=pr_number,
            reason=exc.reason,
            consequence="the gate refuses until a recompute succeeds",
        )
        return exc.reason
    return ""


def evaluate_merge_gate(
    pr_number: int,
    *,
    forge: Forge | None = None,
    db_path: Path | None = None,
    required_names: Sequence[str] | None = None,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
    pin_tree: bool = False,
    keep_tree: bool = False,
    announce: Callable[[str], None] | None = None,
    judge_factory: Callable[[], Judge] | None = None,
) -> GateEvaluation:
    """Run the CI gate and the pure merge decision, without merging.

    The read-only evaluation behind ``repoach review gate <N>``: it
    waits on required CI the same way the auto-merge does, then reaches
    :func:`decide_at_head`. It performs no base/state check and never
    merges or posts — ``safe_merge.sh`` keeps ownership of the actual
    merge.

    SP-AUTOMERGE-FRESH-HEAD: before any of that, the PR API head is
    cross-checked against ``git ls-remote`` ground truth through
    :func:`resolve_verified_head` — the exact same bounded, fail-closed
    convergence check :func:`run_auto_merge` performs before squashing.
    A stale or unverifiable head short-circuits with a refused
    decision (``merge`` False, a reason containing "stale head" and
    both 12-char SHA prefixes) instead of reaching
    :func:`decide_at_head` at all, so ``repoach review gate`` exits 5
    through the existing exit-code mapping and ``safe_merge.sh``
    aborts on the same signal the CI auto-merge would refuse on.

    A pull request the forge reports ``merged`` short-circuits before
    any of that with ``outcome_tag = ALREADY_MERGED``, ``merge`` False
    and that reason (SP-GATE-MERGED-MR-NAMED-REASON): its source branch
    may be gone, and there is nothing left to gate.

    With ``pin_tree`` (``repoach review gate``), the verifiers re-check
    the ledger on the tree of the verified head
    (:func:`~repoach.review.review_head.materialise_head`), not on the
    checkout's branch; ``keep_tree`` keeps that worktree and ``announce``
    receives the ``reviewing <N> at <sha7> (<tree> <path>)`` line.

    The refuter cap of the re-check of older findings
    (SP-GATE-REVERIFIES-AT-HEAD) is read from the checkout, never from
    the head tree; ``judge_factory`` replaces the refuter judge (tests).
    """
    forge = forge or select_forge(cwd=repo_root)
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)

    pr = _pull_request_or_none(forge, pr_number)
    head_ref = pr.head_ref if pr is not None else ""

    if pr is not None and pr.state == "merged":
        reason = f"{OUTCOME_ALREADY_MERGED}: #{pr_number} is already merged into {pr.base_ref!r}"
        _log.info("auto_merge.gate_already_merged", pr_number=pr_number, base=pr.base_ref)
        return GateEvaluation(
            head_sha=pr.head_sha,
            ci_gate=CIGateOutcome(green=False, outcome_tag=OUTCOME_ALREADY_MERGED, reason=reason),
            facts=_unknown_facts(),
            decision=MergeDecision(merge=False, reasons=[reason]),
            pull_request=pr,
        )

    verified_head, stale_reason = resolve_verified_head(
        forge,
        pr_number,
        head_ref,
        repo_root=repo_root or Path.cwd(),
        sleep=sleep,
    )
    if verified_head is None:
        reason = f"stale head: {stale_reason}"
        _log.warning(
            "auto_merge.gate_stale_head",
            pr_number=pr_number,
            head_ref=head_ref,
            reason=stale_reason,
        )
        return GateEvaluation(
            head_sha="",
            ci_gate=CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_STALE_HEAD,
                reason=reason,
            ),
            facts=_unknown_facts(),
            decision=MergeDecision(merge=False, reasons=[reason]),
            pull_request=pr,
        )

    ci_gate = evaluate_ci_gate(
        forge,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
        head_sha=verified_head,
    )
    tree_kind: str | None = None
    if pin_tree:
        checkout = repo_root or Path.cwd()
        with materialise_head(
            forge,
            pr_number,
            require_head_sha(verified_head, pr_number),
            repo_root=checkout,
            state_dir=db.parent,
            keep=keep_tree,
        ) as tree:
            if announce is not None:
                announce(tree.announcement(pr_number))
            head_sha, facts, decision = decide_at_head(
                pr_number,
                forge=forge,
                db=db,
                ci_green=ci_gate.green,
                repo_root=tree.root,
                head_sha=verified_head,
                gates_config=load_gates_config(checkout),
                max_judged=resolve_refuter_max_judged(checkout)[0],
                judge_factory=judge_factory,
                head_tree=True,
                base_ref=pr.base_ref if pr is not None else None,
            )
        tree_kind = tree.tree
    else:
        head_sha, facts, decision = decide_at_head(
            pr_number,
            forge=forge,
            db=db,
            ci_green=ci_gate.green,
            repo_root=repo_root,
            head_sha=verified_head,
            judge_factory=judge_factory,
        )
    _log.info(
        "auto_merge.gate_evaluated",
        pr_number=pr_number,
        merge=decision.merge,
        ci_green=ci_gate.green,
        open_blocking=facts.open_blocking_findings,
        reasons=decision.reasons[:5],
    )
    return GateEvaluation(
        head_sha=head_sha,
        ci_gate=ci_gate,
        facts=facts,
        decision=decision,
        pull_request=pr,
        tree=tree_kind,
        read_changed_files=lambda: diff_files(forge.diff(pr_number).text),
    )


def run_auto_merge(
    pr_number: int,
    *,
    forge: Forge | None = None,
    db_path: Path | None = None,
    required_names: Sequence[str] | None = None,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
    merge_method: MergeMethod | None = None,
    delete_branch: bool | None = None,
    pin_tree: bool = False,
    judge_factory: Callable[[], Judge] | None = None,
) -> AutoMergeResult:
    """Evaluate the merge gates and (if all green) merge with the configured method.

    With ``pin_tree`` (``repoach review merge``), the ledger is
    re-verified on the tree of the verified head exactly as
    ``review gate`` does (:func:`evaluate_merge_gate`), so the findings
    of older heads are re-checked there (SP-GATE-REVERIFIES-AT-HEAD);
    without it the re-check is skipped and they keep blocking.

    The function is the unit invoked by ``repoach review merge
    <N>`` and the ``auto_merge`` job in the GitHub Actions workflow.

    SP-PURE-MERGE-GATE (redesign slice 7b, the flip): the archive's
    self-reported 4/4 verdict is no longer a merge gate — it is read
    for report context only. After the base-branch and idempotency
    checks, the decision is the pure function
    :func:`compute_merge_decision` of facts re-verified at the exact
    head: CI green, a fresh complete review (every reviewer parsed,
    zero unparsed), zero blocking findings surviving re-verification,
    and (when recorded) spec coverage present. This closes the
    forgeable-archive hole (audit CRITICAL #1, the verdict no longer
    gates) and the parse_failed-promote hole (audit CRITICAL #2, an
    unparsed reviewer fails the review-integrity fact). A refused pure
    gate persists ``OUTCOME_SKIP_GATE`` with the failing reasons.

    The function never raises on a "we should not merge" outcome —
    the caller (CLI) maps the outcome to an exit code.  An
    already-merged or closed PR is treated as idempotent success
    (``OUTCOME_ALREADY_MERGED``).
    """
    forge = forge or select_forge(cwd=repo_root)
    if merge_method is None or delete_branch is None:
        configured_method, configured_delete = merge_preferences(repo_root)
        merge_method = merge_method if merge_method is not None else configured_method
        delete_branch = delete_branch if delete_branch is not None else configured_delete
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)

    pr = _pull_request_or_none(forge, pr_number)
    base = pr.base_ref if pr is not None else ""
    head = pr.head_ref if pr is not None else ""
    state = pr.state.upper() if pr is not None else ""

    _log.info(
        "auto_merge.run_started",
        pr_number=pr_number,
        base=base,
        head=head,
        state=state,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
    )

    def _persist(result: AutoMergeResult) -> AutoMergeResult:
        """Persist + return."""
        try:
            record_merge(
                db,
                pr_number=pr_number,
                outcome=result.outcome,
                base_ref=base,
                head_ref=head,
                merged_sha=result.merged_sha,
                notes=result.notes,
            )
        except Exception as exc:
            _log.warning("auto_merge.persist_failed", pr_number=pr_number, exc=str(exc))
        log_method = _log.info if result.outcome in SUCCESS_OUTCOMES else _log.warning
        log_method(
            "auto_merge.gate_decision",
            pr_number=pr_number,
            outcome=result.outcome,
            merged_sha=result.merged_sha,
            base=base,
            head=head,
            notes_preview=result.notes[:200],
        )
        return result

    integration_branch = settings.integration_branch
    if base != integration_branch:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_BASE,
                notes=f"base={base!r}, refusing (only {integration_branch} is allowed)",
            )
        )

    if state in {"MERGED", "CLOSED"}:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_ALREADY_MERGED,
                notes=f"state={state}",
            )
        )
    if pr is not None and pr.state not in WRITABLE_STATES:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_STATE,
                notes=f"state={state}, refusing to write",
            )
        )

    verified_head, stale_reason = resolve_verified_head(
        forge,
        pr_number,
        head,
        repo_root=repo_root or Path.cwd(),
        sleep=sleep,
    )
    if verified_head is None:
        _log.warning(
            "auto_merge.stale_head",
            pr_number=pr_number,
            base=base,
            head=head,
            reason=stale_reason,
        )
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_STALE_HEAD,
                notes=f"stale head: {stale_reason}",
            )
        )

    ci_gate = evaluate_ci_gate(
        forge,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
        head_sha=verified_head,
    )
    if not ci_gate.green:
        snapshot = _summarise_rollup(ci_gate.rollup_snapshot)
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=ci_gate.outcome_tag,
                notes=f"{ci_gate.reason} | rollup={snapshot}",
            )
        )

    if pin_tree:
        checkout = repo_root or Path.cwd()
        try:
            with materialise_head(
                forge,
                pr_number,
                require_head_sha(verified_head, pr_number),
                repo_root=checkout,
                state_dir=db.parent,
            ) as tree:
                _, facts, decision = decide_at_head(
                    pr_number,
                    forge=forge,
                    db=db,
                    ci_green=ci_gate.green,
                    repo_root=tree.root,
                    head_sha=verified_head,
                    gates_config=load_gates_config(checkout),
                    max_judged=resolve_refuter_max_judged(checkout)[0],
                    judge_factory=judge_factory,
                    head_tree=True,
                    base_ref=base or None,
                )
        except (HeadUnavailableError, HeadUnresolvedError) as exc:
            _log.warning("auto_merge.head_tree_unavailable", pr_number=pr_number, error=str(exc))
            return _persist(
                AutoMergeResult(
                    pr_number=pr_number,
                    outcome=OUTCOME_SKIP_GATE,
                    notes=f"head tree unavailable: {exc}",
                )
            )
    else:
        _, facts, decision = decide_at_head(
            pr_number,
            forge=forge,
            db=db,
            ci_green=ci_gate.green,
            repo_root=repo_root,
            head_sha=verified_head,
            judge_factory=judge_factory,
        )
    _log.info(
        "auto_merge.pure_gate",
        pr_number=pr_number,
        merge=decision.merge,
        open_blocking=facts.open_blocking_findings,
        review_complete=facts.review_complete,
        spec_covered=facts.spec_covered,
        reasons=decision.reasons[:5],
    )
    if not decision.merge:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_GATE,
                notes="pure gate refused: " + "; ".join(decision.reasons),
            )
        )

    tip_sha, tip_err = _ls_remote_tip(forge, head)
    if tip_sha is None or tip_sha != verified_head:
        reason = (
            tip_err
            if tip_sha is None
            else f"head moved after gate decision: api={verified_head[:12]}, "
            f"ls_remote={tip_sha[:12]}"
        )
        _log.warning(
            "auto_merge.stale_head",
            pr_number=pr_number,
            base=base,
            head=head,
            reason=reason,
        )
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_STALE_HEAD,
                notes=f"stale head: {reason}",
            )
        )

    ok, merge_sha, log, refusal = merge_change(
        forge,
        pr_number,
        method=merge_method,
        delete_branch=delete_branch,
        head_sha=verified_head,
    )
    if not ok and refusal in _REFUSAL_OUTCOMES:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=_REFUSAL_OUTCOMES[refusal],
                notes=f"merge refused ({refusal}): {log}",
            )
        )
    if not ok:
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_FAILED,
                notes=f"merge failed: {log}",
            )
        )

    return _persist(
        AutoMergeResult(
            pr_number=pr_number,
            outcome=OUTCOME_MERGED,
            merged_sha=merge_sha,
            notes=log,
        )
    )
