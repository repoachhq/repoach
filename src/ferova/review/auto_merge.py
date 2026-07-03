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
    - ``_DEFAULT_WAIT_SECONDS`` matches the "Wait for CI" window of
      ``auto-review.yml`` (12 minutes).  ``_DEFAULT_POLL_INTERVAL``
      polls every 30 s within that window.
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
OUTCOME_FAILED: str = "FAILED"

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

_DEFAULT_WAIT_SECONDS: int = 12 * 60
_DEFAULT_POLL_INTERVAL: int = 30


_MERGE_SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")


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
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    head_sha: str | None = None,
) -> CIGateOutcome:
    """Block until every required check succeeds, fails, or times out.

    SP-AUTOMERGE-CI-GATE: refuses to merge when any required status
    check is in FAILURE / CANCELLED / TIMED_OUT / ACTION_REQUIRED.
    Pending checks are polled every ``poll_interval`` seconds for up
    to ``wait_seconds`` total before timing out.  Facts come from the
    head commit's check-runs (``head_sha`` when given, else resolved
    per poll so a mid-poll push is still judged at the fresh head);
    the PR ``statusCheckRollup`` is only a fallback when the commit
    API fails.
    """
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
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    """Backward-compatible wrapper around :func:`evaluate_ci_gate`.

    Returns ``(green, reason)``.  Callers that need the structured
    outcome tag or rollup snapshot should call ``evaluate_ci_gate``
    directly.
    """
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


def decide_at_head(
    pr_number: int,
    *,
    gh: GhCli,
    db: Path,
    ci_green: bool,
    repo_root: Path | None = None,
) -> tuple[str, MergeFacts, MergeDecision]:
    """Resolve the head, re-verify the ledger there, and decide.

    The single source of the pure merge decision. Both
    :func:`run_auto_merge` (after its CI short-circuit) and
    :func:`evaluate_merge_gate` (for the read-only CLI) call this and
    nothing else to obtain ``(head_sha, facts, decision)`` — extracting
    it is what keeps the CI auto-merge and the local gate from drifting.

    Args:
        pr_number: The PR under decision.
        gh: The GitHub client used to resolve the head SHA.
        db: The findings + coverage ledger.
        ci_green: Whether required checks are green at head (folded into
            the facts so the decision reasons include CI state).
        repo_root: The head checkout the mechanical verifiers resolve
            against; defaults to the current working directory.

    Returns:
        ``(head_sha, facts, decision)``.
    """
    head_sha = gh.pr_head_sha(pr_number) or ""
    facts = gather_merge_facts(
        db,
        pr_number=pr_number,
        repo_root=repo_root or Path.cwd(),
        head_sha=head_sha,
        ci_green=ci_green,
    )
    return head_sha, facts, compute_merge_decision(facts)


def evaluate_merge_gate(
    pr_number: int,
    *,
    gh: GhCli | None = None,
    db_path: Path | None = None,
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
) -> GateEvaluation:
    """Run the CI gate and the pure merge decision, without merging.

    The read-only evaluation behind ``ferova review gate <N>``: it
    waits on required CI the same way the auto-merge does, then reaches
    :func:`decide_at_head`. It performs no base/state check and never
    merges or posts — ``safe_merge.sh`` keeps ownership of the actual
    ``gh pr merge``.
    """
    gh = gh or GhCli()
    settings = get_settings()
    db = db_path or Path(settings.db_path)
    init_schema(db)
    ci_gate = evaluate_ci_gate(
        gh,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
    )
    head_sha, facts, decision = decide_at_head(
        pr_number,
        gh=gh,
        db=db,
        ci_green=ci_gate.green,
        repo_root=repo_root,
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
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    poll_interval: int = _DEFAULT_POLL_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    repo_root: Path | None = None,
) -> AutoMergeResult:
    """Evaluate the merge gates and (if all green) squash-merge.

    The function is the unit invoked by ``ferova review merge
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
        log_method = (
            _log.info
            if result.outcome in {OUTCOME_MERGED, OUTCOME_ALREADY_MERGED}
            else _log.warning
        )
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

    ci_gate = evaluate_ci_gate(
        gh,
        pr_number,
        required_names=required_names,
        wait_seconds=wait_seconds,
        poll_interval=poll_interval,
        monotonic=monotonic,
        sleep=sleep,
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
