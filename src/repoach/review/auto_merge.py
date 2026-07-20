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

When all gates pass, we run ``gh pr merge --squash --delete-branch``
and persist the outcome to L4 ``pr_merges``.

Module-level constants :
    - The ``OUTCOME_*`` names are kept stable so historical L4
      queries keep working.  ``OUTCOME_SKIP_CI`` is the legacy
      umbrella tag preserved for back-compat; new refused-on-CI
      paths emit one of the more specific tags
      (``OUTCOME_SKIP_CI_FAILED``, ``OUTCOME_SKIP_CI_TIMEOUT``,
      ``OUTCOME_SKIP_CI_MISSING``).
    - :data:`DEFAULT_REQUIRED_CHECK_NAMES` mirrors the matrix in
      ``.github/workflows/ci.yml``.  Override per call when the
      matrix changes.
    - The ``_FAIL_CONCLUSIONS`` / ``_SUCCESS_CONCLUSIONS`` /
      ``_PENDING_STATUSES`` frozensets bucket the conclusion strings
      returned by ``gh pr view --json statusCheckRollup``.
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
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from .gh_client import GhCli
from .merge_gate import (
    MergeDecision,
    MergeFacts,
    compute_merge_decision,
    gather_merge_facts,
)
from .persistence import init_schema, record_merge

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
        OUTCOME_SKIP_STALE_HEAD,
    }
)


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


DEFAULT_REQUIRED_CHECK_NAMES: tuple[str, ...] = (
    "Test suite (Python 3.11)",
    "Test suite (Python 3.13)",
)

_FAIL_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_PENDING_STATUSES: frozenset[str] = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED", "REQUESTED"}
)


_MERGE_SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")


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


def fetch_status_rollup(gh: GhCli, pr_number: int) -> tuple[list[dict], str]:
    """Return ``(rollup_entries, error_message)`` from ``gh pr view``.

    On parse errors or non-zero exit, ``rollup_entries`` is empty and
    ``error_message`` carries a short diagnostic.
    """
    res = gh._run(
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "statusCheckRollup",
        ]
    )
    if not res.ok:
        return [], f"gh pr view failed: {res.stderr[:200]}"
    try:
        data = json.loads(res.stdout) or {}
    except json.JSONDecodeError:
        return [], f"unparseable gh output: {res.stdout[:200]}"
    rollup = data.get("statusCheckRollup")
    if not isinstance(rollup, list):
        return [], "statusCheckRollup missing from gh output"
    return [dict(entry) for entry in rollup], ""


def fetch_check_runs(gh: GhCli, head_sha: str) -> tuple[list[dict], str]:
    """Return ``(check_run_entries, error_message)`` for a commit.

    Queries the commit's complete check-run record instead of the PR
    ``statusCheckRollup``: the rollup is blind to ``workflow_dispatch``
    runs and was observed pinning a stale CANCELLED entry over a newer
    green run at the same SHA (PR #3, 2026-07-03).  Entries are
    normalised to the rollup shape (``name`` / ``status`` /
    ``conclusion`` / ``startedAt``); the ``--jq`` projection emits one
    JSON object per line.
    """
    res = gh._run(
        [
            "api",
            "repos/{owner}/{repo}/commits/" + head_sha + "/check-runs",
            "--paginate",
            "--jq",
            ".check_runs[] | {name, status, conclusion, startedAt: .started_at}",
        ]
    )
    if not res.ok:
        return [], f"gh api check-runs failed: {res.stderr[:200]}"
    entries: list[dict] = []
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entries.append(dict(json.loads(stripped)))
        except json.JSONDecodeError:
            return [], f"unparseable check-runs line: {stripped[:200]}"
    return entries, ""


def classify_required_checks(
    rollup: Iterable[dict],
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
) -> tuple[list[str], list[str], list[str]]:
    """Bucket required checks into ``(failed, pending, missing)``.

    Each ``rollup`` entry is a ``{"name", "status", "conclusion", ...}``
    dict, from either ``gh pr view --json statusCheckRollup`` or
    :func:`fetch_check_runs`.  Duplicate entries (workflow re-runs,
    concurrency cancellations) collapse to the latest ``startedAt``
    per name — neither source is reliably ordered, and the observed
    stale-cancelled-over-fresh-green pinning (PR #3, 2026-07-03) means
    iteration order must never decide.  ISO-8601 timestamps compare
    lexicographically; an entry without one is treated as oldest.  An
    unknown conclusion bucket is treated as a failure (fail-closed).
    """
    by_name: dict[str, dict] = {}
    for entry in rollup:
        name = str(entry.get("name") or "")
        if not name:
            continue
        started = str(entry.get("startedAt") or "")
        current = by_name.get(name)
        if current is None or started >= str(current.get("startedAt") or ""):
            by_name[name] = dict(entry)

    failed: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    for required in required_names:
        match = by_name.get(required)
        if match is None:
            missing.append(required)
            continue
        conclusion = str(match.get("conclusion") or "").upper()
        status = str(match.get("status") or "").upper()
        if conclusion in _FAIL_CONCLUSIONS:
            failed.append(f"{required}={conclusion}")
        elif conclusion in _SUCCESS_CONCLUSIONS:
            continue
        elif status in _PENDING_STATUSES or not conclusion:
            pending.append(required)
        else:
            failed.append(f"{required}={conclusion or 'EMPTY'}")
    return failed, pending, missing


def evaluate_ci_gate(
    gh: GhCli,
    pr_number: int,
    *,
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
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
    API fails.
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
        required_names=list(required_names),
    )

    while True:
        sha = head_sha or gh.pr_head_sha(pr_number)
        if sha:
            rollup, err = fetch_check_runs(gh, sha)
            if err and not rollup:
                _log.warning(
                    "auto_merge.check_runs_fallback_to_rollup",
                    pr_number=pr_number,
                    head_sha=sha[:12],
                    error=err[:200],
                )
                rollup, err = fetch_status_rollup(gh, pr_number)
        else:
            rollup, err = fetch_status_rollup(gh, pr_number)
        poll_count += 1
        if err and not rollup:
            _log.error(
                "auto_merge.ci_gate_fetch_failed",
                pr_number=pr_number,
                poll=poll_count,
                error=err[:200],
            )
            return CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_CI,
                reason=err,
                rollup_snapshot=[],
            )
        last_snapshot = rollup
        failed, pending, missing = classify_required_checks(rollup, required_names)
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
    gh: GhCli,
    pr_number: int,
    *,
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
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
        gh,
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


def squash_merge(
    gh: GhCli, pr_number: int, *, delete_branch: bool = True
) -> tuple[bool, str | None, str]:
    """Run ``gh pr merge --squash`` and return ``(ok, merge_sha, log)``.

    The success path of ``gh pr merge`` prints the merge commit SHA;
    we extract it from stdout when present so callers can persist it
    to L4 for audit.

    Args:
        gh: A :class:`GhCli` wrapper.
        pr_number: PR to merge.
        delete_branch: When True, also delete the head branch.

    Returns:
        ``(ok, merge_sha_or_None, log_tail)``.
    """
    args = ["pr", "merge", str(pr_number), "--squash"]
    if delete_branch:
        args.append("--delete-branch")
    res = gh._run(args)
    if not res.ok:
        return False, None, (res.stderr or res.stdout)[-500:]
    m = _MERGE_SHA_RE.search(res.stdout)
    sha = m.group(1) if m else None
    return True, sha, res.stdout[-500:]


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
    """

    head_sha: str
    ci_gate: CIGateOutcome
    facts: MergeFacts
    decision: MergeDecision


def _ls_remote_tip(gh: GhCli, head_ref: str) -> tuple[str | None, str]:
    """Resolve the branch tip via ``git ls-remote origin refs/heads/<head_ref>``.

    Shared by :func:`resolve_verified_head` and the pre-squash re-read in
    :func:`run_auto_merge` — both need the exact same ``ls-remote`` parsing
    and the exact same fail-closed contract (``None`` plus a reason string
    on any transport or parse failure).

    Args:
        gh: The GitHub client wrapper; only ``_run_git`` is used.
        head_ref: The branch name to resolve (``headRefName``).

    Returns:
        ``(sha, "")`` on success, ``(None, reason)`` on failure.
    """
    ls_remote = gh._run_git(["ls-remote", "origin", f"refs/heads/{head_ref}"])
    if not ls_remote.ok or not ls_remote.stdout.strip():
        error = (ls_remote.stderr or ls_remote.stdout).strip()
        return None, f"ls-remote failed: {error[:200]}"
    first_line = ls_remote.stdout.strip().splitlines()[0]
    tokens = first_line.split()
    remote_sha = tokens[0] if tokens else ""
    if not remote_sha:
        return None, f"ls-remote returned no SHA: {first_line[:200]}"
    return remote_sha, ""


def resolve_verified_head(
    gh: GhCli,
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
    :meth:`GhCli.pr_head_sha`, re-polling up to ``attempts`` times with
    ``sleep(delay_s)`` between polls until the two agree.

    The contract is fail-closed: a persistent mismatch, or an
    ``ls-remote`` transport failure, returns ``None`` rather than a
    best-guess SHA — callers must refuse to merge, not merge at a
    head that cannot be verified.

    Args:
        gh: The GitHub client wrapper; both ``pr_head_sha`` and the
            underlying ``_run_git`` are used.
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
        ls_remote = gh._run_git(["ls-remote", "origin", f"refs/heads/{head_ref}"])
        if not ls_remote.ok or not ls_remote.stdout.strip():
            error = (ls_remote.stderr or ls_remote.stdout).strip()
            return None, f"ls-remote failed: {error[:200]}"
        first_line = ls_remote.stdout.strip().splitlines()[0]
        tokens = first_line.split()
        remote_sha = tokens[0] if tokens else ""
        if not remote_sha:
            return None, f"ls-remote returned no SHA: {first_line[:200]}"
        api_sha = gh.pr_head_sha(pr_number) or ""
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
    gh: GhCli,
    db: Path,
    ci_green: bool,
    repo_root: Path | None = None,
    head_sha: str | None = None,
) -> tuple[str, MergeFacts, MergeDecision]:
    """Resolve the head, re-verify the ledger there, and decide.

    The single source of the pure merge decision. Both
    :func:`run_auto_merge` (after its CI short-circuit) and
    :func:`evaluate_merge_gate` (for the read-only CLI) call this and
    nothing else to obtain ``(head_sha, facts, decision)`` — extracting
    it is what keeps the CI auto-merge and the local gate from drifting.

    Args:
        pr_number: The PR under decision.
        gh: The GitHub client used to resolve the head SHA when
            ``head_sha`` is not supplied.
        db: The findings + coverage ledger.
        ci_green: Whether required checks are green at head (folded into
            the facts so the decision reasons include CI state).
        repo_root: The head checkout the mechanical verifiers resolve
            against; defaults to the current working directory.
        head_sha: An explicit head to decide at, overriding
            ``gh.pr_head_sha``.  SP-AUTOMERGE-FRESH-HEAD:
            :func:`run_auto_merge` passes the ``resolve_verified_head``
            SHA here so gate facts are computed at the exact commit
            about to be merged, not whatever the API happens to be
            serving.  ``None`` (the default) keeps today's behaviour for
            other callers such as :func:`evaluate_merge_gate`.

    Returns:
        ``(head_sha, facts, decision)``.
    """
    resolved_head = head_sha if head_sha is not None else (gh.pr_head_sha(pr_number) or "")
    facts = gather_merge_facts(
        db,
        pr_number=pr_number,
        repo_root=repo_root or Path.cwd(),
        head_sha=resolved_head,
        ci_green=ci_green,
    )
    return resolved_head, facts, compute_merge_decision(facts)


def evaluate_merge_gate(
    pr_number: int,
    *,
    gh: GhCli | None = None,
    db_path: Path | None = None,
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
) -> GateEvaluation:
    """Run the CI gate and the pure merge decision, without merging.

    The read-only evaluation behind ``repoach review gate <N>``: it
    waits on required CI the same way the auto-merge does, then reaches
    :func:`decide_at_head`. It performs no base/state check and never
    merges or posts — ``safe_merge.sh`` keeps ownership of the actual
    ``gh pr merge``.

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
    """
    gh = gh or GhCli()
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)

    pr_meta = gh.pr_view(pr_number) or {}
    head_ref = str(pr_meta.get("headRefName") or "")

    verified_head, stale_reason = resolve_verified_head(
        gh,
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
        stale_facts = MergeFacts(
            head_sha="",
            ci_green=False,
            open_blocking_findings=0,
            spec_covered=False,
            spec_coverage_known=False,
            review_complete=False,
            review_integrity_known=False,
        )
        return GateEvaluation(
            head_sha="",
            ci_gate=CIGateOutcome(
                green=False,
                outcome_tag=OUTCOME_SKIP_STALE_HEAD,
                reason=reason,
            ),
            facts=stale_facts,
            decision=MergeDecision(merge=False, reasons=[reason]),
        )

    ci_gate = evaluate_ci_gate(
        gh,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
        head_sha=verified_head,
    )
    head_sha, facts, decision = decide_at_head(
        pr_number,
        gh=gh,
        db=db,
        ci_green=ci_gate.green,
        repo_root=repo_root,
        head_sha=verified_head,
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
    )


def run_auto_merge(
    pr_number: int,
    *,
    gh: GhCli | None = None,
    db_path: Path | None = None,
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
    wait_seconds: int | None = None,
    poll_interval: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
) -> AutoMergeResult:
    """Evaluate the merge gates and (if all green) squash-merge.

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
    gh = gh or GhCli()
    wait_seconds, poll_interval = _resolve_wait_poll(wait_seconds, poll_interval)
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)

    pr_meta = gh.pr_view(pr_number) or {}
    base = str(pr_meta.get("baseRefName") or "")
    head = str(pr_meta.get("headRefName") or "")
    state = str(pr_meta.get("state") or "").upper()

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

    if base != "develop":
        return _persist(
            AutoMergeResult(
                pr_number=pr_number,
                outcome=OUTCOME_SKIP_BASE,
                notes=f"base={base!r}, refusing (only develop is allowed)",
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

    verified_head, stale_reason = resolve_verified_head(
        gh,
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
        gh,
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

    _, facts, decision = decide_at_head(
        pr_number,
        gh=gh,
        db=db,
        ci_green=ci_gate.green,
        repo_root=repo_root,
        head_sha=verified_head,
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

    tip_sha, tip_err = _ls_remote_tip(gh, head)
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

    ok, merge_sha, log = squash_merge(gh, pr_number, delete_branch=True)
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
