"""Findings-driven Coder path: resolve open findings, re-verify at head.

Redesign slice 8a. The flip (slice 7b) made the merge gate decide on
**open blocking findings re-verified at head**, but the Coder loop is
still verdict/comment-driven and never advances a finding's lifecycle.
This module adds the resolution side of the loop, additive to the
legacy path:

* the to-fix queue is the set of **blocking** findings that are
  ``verified`` (or already ``open``) at head;
* picking one up transitions it ``verified -> open``;
* after the Coder's fix lands, the **same check that confirmed each
  finding** re-runs at the new head — when it no longer confirms the
  problem the finding moves ``open -> resolved``; when it still
  confirms, the finding stays ``open``. Across rounds, the cap +
  no-progress stall detector (SP-STUCK-ESCALATION, slice 9) drives a
  give-up PR's open findings to ``stuck`` and escalates.

The re-verification semantics are deliberately inverted from the
proposal side: a verifier confirms a *problem* (``verified``);
resolution means the verifier can no longer confirm it (``refuted``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from .finding_verifiers import verify_finding
from .findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from .gh_client import GhCli
from .notifier import RoutineFireResult, fire_review_routine
from .refuter import JUDGED_CLAIM_TYPES, Judge, make_refuter_judge, refute_finding
from .reviewer import Coder
from .stuck import (
    assess_stuck,
    build_stuck_dossier,
    fetch_coder_rounds,
    init_stuck_schema,
    mark_findings_stuck,
    record_coder_round,
)

_log = get_logger(__name__)

_MECHANICAL_TYPES = frozenset(
    {ClaimType.MISSING_TEST, ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION}
)
"""Claim types whose resolution is re-checked mechanically on disk."""

_OPEN_QUEUE_STATUSES = frozenset({FindingStatus.VERIFIED, FindingStatus.OPEN})
"""A blocking finding counts as work-to-do while verified or open."""

_MAX_REVERIFY_JUDGED = 10
"""Cap on judged findings re-verified per run (mirrors the refuter cap)
so a stuck PR cannot fan out unbounded OPUS judge calls at head."""


def fetch_open_blocking_findings(db_path: Path, pr_number: int) -> list[Finding]:
    """Return the blocking findings that still need fixing for a PR.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose findings to scan.

    Returns:
        Findings with severity ``blocking`` and status ``verified`` or
        ``open`` (the to-fix queue), oldest first. Advisory findings
        and settled (resolved / refuted) or unverified (proposed) ones
        are excluded.
    """
    return [
        finding
        for finding in fetch_findings(db_path, pr_number)
        if finding.severity is Severity.BLOCKING and finding.status in _OPEN_QUEUE_STATUSES
    ]


def open_verified_blocking(db_path: Path, pr_number: int, *, head_sha: str | None) -> int:
    """Transition each verified blocking finding to ``open`` (the queue).

    A finding the verifiers confirmed (``verified``) becomes actionable
    work the moment the Coder picks it up; ``open`` is that
    work-in-progress state. The original verification method / result
    are preserved so the resolution audit trail keeps the evidence.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose verified blocking findings to open.
        head_sha: The head the transition is recorded against.

    Returns:
        How many findings moved from ``verified`` to ``open``.
    """
    moved = 0
    for finding in fetch_findings(db_path, pr_number, status=FindingStatus.VERIFIED):
        if finding.severity is not Severity.BLOCKING or finding.id is None:
            continue
        if update_finding_status(
            db_path,
            finding.id,
            FindingStatus.OPEN,
            verification_method=finding.verification_method,
            verification_result=finding.verification_result,
            checked_at_sha=head_sha or "",
        ):
            moved += 1
    if moved:
        _log.info("coder_findings.opened_queue", pr_number=pr_number, n_opened=moved)
    return moved


def reverify_resolution_for_pr(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str | None,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
) -> dict[str, int]:
    """Re-check every open finding at head; resolve the ones now fixed.

    Each ``open`` finding is re-run through the SAME check that
    confirmed it — :func:`verify_finding` for mechanical claim types,
    :func:`refute_finding` for judged (design / security) ones. When
    the check returns ``refuted`` (it can no longer confirm the
    problem) the finding transitions ``open -> resolved``; otherwise it
    stays ``open``. ``broken_behavior`` findings are skipped here —
    their re-check is the pytest gate, handled by
    :func:`resolve_broken_behavior_findings`. Other claim types with no
    re-check stay open (settled elsewhere, never silently resolved).

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose open findings to re-verify.
        repo_root: The head checkout the checks resolve against.
        head_sha: The head the resolution is recorded against.
        judge_factory: Builds the refuter judge lazily on the first
            judged finding (no LLM loop for a run without any).

    Returns:
        Counts ``{"resolved": n, "still_open": n}``.
    """
    counts = {"resolved": 0, "still_open": 0}
    judge: Judge | None = None
    judged_seen = 0
    for finding in fetch_findings(db_path, pr_number, status=FindingStatus.OPEN):
        if finding.id is None or finding.claim_type is ClaimType.BROKEN_BEHAVIOR:
            continue
        method = ""
        result = ""
        if finding.claim_type in _MECHANICAL_TYPES:
            status, method, result = verify_finding(finding, repo_root=repo_root)
        elif finding.claim_type in JUDGED_CLAIM_TYPES:
            if judged_seen >= _MAX_REVERIFY_JUDGED:
                counts["still_open"] += 1
                continue
            judged_seen += 1
            if judge is None:
                judge = judge_factory()
            status, result = refute_finding(finding, repo_root=repo_root, judge=judge)
            method = "refuter"
        else:
            counts["still_open"] += 1
            continue
        if status is FindingStatus.REFUTED:
            update_finding_status(
                db_path,
                finding.id,
                FindingStatus.RESOLVED,
                verification_method=method,
                verification_result=result,
                checked_at_sha=head_sha or "",
            )
            counts["resolved"] += 1
        else:
            counts["still_open"] += 1
    _log.info(
        "coder_findings.reverified",
        pr_number=pr_number,
        resolved=counts["resolved"],
        still_open=counts["still_open"],
    )
    return counts


_BROKEN_BEHAVIOR_OPEN = frozenset(
    {FindingStatus.PROPOSED, FindingStatus.VERIFIED, FindingStatus.OPEN}
)
"""A CI-failure finding is live (no duplicate recorded) in these states."""


def record_ci_failures_as_findings(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str | None,
    failed_rows: list[dict[str, str]],
    failure_logs: list[str] | None = None,
) -> int:
    """Materialise red CI checks as ``broken_behavior`` findings.

    Slice 8b, single-source: a CI failure flows through the ledger like
    any other defect rather than a side channel. Each failed required
    check becomes a blocking ``broken_behavior`` finding recorded
    ``verified`` (CI red IS the confirmation), so the findings-driven
    Coder picks it up as work. The merge gate already refuses a red CI
    independently, so these findings drive the fix, they do not
    double-count the block.

    Idempotent per check name: a live finding (proposed / verified /
    open) for the same check is not duplicated across re-runs.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose CI failed.
        head_sha: The head the failure was observed at.
        failed_rows: ``{"name", "link"}`` dicts from
            :func:`coder_loop.fetch_ci_status`.
        failure_logs: Optional per-check log blocks (each headed by a
            ``### <check name>`` line) from
            :func:`coder_loop.fetch_failed_check_logs`, stored as the
            finding's verification result.

    Returns:
        How many new findings were recorded.
    """
    init_findings_schema(db_path)
    existing = fetch_findings(db_path, pr_number)
    live_ci_claims = {
        f.claim
        for f in existing
        if f.claim_type is ClaimType.BROKEN_BEHAVIOR and f.status in _BROKEN_BEHAVIOR_OPEN
    }
    logs_by_name: dict[str, str] = {}
    for block in failure_logs or []:
        first = block.splitlines()[0] if block else ""
        name = first.lstrip("# ").strip()
        if name:
            logs_by_name[name] = block
    created = 0
    for row in failed_rows:
        name = row.get("name") or "?"
        claim = f"CI check failed: {name}"
        if claim in live_ci_claims:
            continue
        record_finding(
            db_path,
            Finding(
                pr_number=pr_number,
                head_sha=head_sha or "",
                round=1,
                finder="ci",
                claim_type=ClaimType.BROKEN_BEHAVIOR,
                severity=Severity.BLOCKING,
                file=f"(ci):{name}",
                line_start=0,
                line_end=0,
                claim=claim,
                evidence_pointer=row.get("link") or "",
                status=FindingStatus.VERIFIED,
                verification_method="ci",
                verification_result=logs_by_name.get(name, "")[:4000],
                checked_at_sha=head_sha or "",
            ),
        )
        created += 1
    if created:
        _log.info("coder_findings.ci_failures_recorded", pr_number=pr_number, n_created=created)
    return created


def resolve_broken_behavior_findings(db_path: Path, *, pr_number: int, head_sha: str | None) -> int:
    """Resolve open ``broken_behavior`` findings — the pytest gate is green.

    The check that confirmed a CI-failure finding is "the suite is red";
    the re-check is "the suite is green". The findings-driven runner
    only reaches this after the local pytest gate passed, so a green
    gate IS the re-verification: every open ``broken_behavior`` finding
    transitions ``open -> resolved``.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR being fixed.
        head_sha: The post-fix head the resolution is recorded against.

    Returns:
        How many findings moved to ``resolved``.
    """
    resolved = 0
    for finding in fetch_findings(db_path, pr_number, status=FindingStatus.OPEN):
        if finding.claim_type is not ClaimType.BROKEN_BEHAVIOR or finding.id is None:
            continue
        if update_finding_status(
            db_path,
            finding.id,
            FindingStatus.RESOLVED,
            verification_method="pytest",
            verification_result="local pytest gate green at head",
            checked_at_sha=head_sha or "",
        ):
            resolved += 1
    if resolved:
        _log.info(
            "coder_findings.broken_behavior_resolved", pr_number=pr_number, n_resolved=resolved
        )
    return resolved


@dataclass
class CoderFindingsResult:
    """Outcome of one :func:`run_coder_fix_from_findings` invocation."""

    pr_number: int
    n_open_findings: int = 0
    fixes_applied: int = 0
    fixes_rejected: int = 0
    rejected_paths: list[str] = field(default_factory=list)
    pytest_passed: bool = False
    pushed: bool = False
    resolved: int = 0
    still_open: int = 0
    placeholder_rejected: bool = False
    stuck: bool = False
    no_op_reason: str | None = None


def _local_head_sha(repo_root: Path) -> str:
    """Return the working tree's current HEAD commit SHA, or ``""``."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("coder_findings.head_sha_failed", error=str(exc)[:160])
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _fire_stuck_dossier(
    payload: dict[str, object],
    *,
    routine_fire: Callable[..., RoutineFireResult],
) -> None:
    """Fire the stuck-escalation dossier to the operator's routine.

    Settings-guarded like :meth:`orchestrator.ReviewTeam._fire_routine`:
    when ``CLAUDE_CODE_ROUTINE_ID`` + ``CLAUDE_CODE_ROUTINE_TOKEN`` are
    absent this is a no-op (the common unit-test and quota-spared case).
    Unlike the per-round verdict ping, the dossier fires only when the
    loop has genuinely given up, so it does not burn routine quota on
    every sync.

    Args:
        payload: The dossier from :func:`stuck.build_stuck_dossier`.
        routine_fire: The routine-fire callable (injected in tests).
    """
    settings = get_settings()
    routine_id = settings.claude_code_routine_id
    token_secret = settings.claude_code_routine_token
    if not routine_id or token_secret is None:
        return
    token = token_secret.get_secret_value()
    if not token:
        return
    result = routine_fire(routine_id=routine_id, token=token, payload=payload)
    if result.ok:
        _log.info("coder_findings.stuck_routine_fired", routine_id=routine_id)
    else:
        _log.warning(
            "coder_findings.stuck_routine_fire_failed",
            routine_id=routine_id,
            status_code=result.status_code,
            error=(result.error or "")[:200],
        )


def run_coder_fix_from_findings(
    pr_number: int,
    *,
    gh: GhCli | None = None,
    repo_root: Path | None = None,
    coder: Coder | None = None,
    db_path: Path | None = None,
    routine_fire: Callable[..., RoutineFireResult] = fire_review_routine,
) -> CoderFindingsResult:
    """Resolve a PR's open blocking findings, then re-verify at head.

    The findings-driven counterpart to
    :func:`coder_loop.run_coder_fix` (redesign slice 8a, additive). The
    work queue is the merge gate's own blocker set — open blocking
    findings — not the archive verdict, so a PR that is archive-APPROVE
    yet carries a verified blocking finding is still acted on (the gap
    the flip opened). After the fix passes the local ruff + pytest gate
    and is pushed, each worked finding is re-checked by the same check
    that confirmed it; resolved ones transition ``open -> resolved``.

    The local gate is single-pass here: a fix that fails ruff or pytest
    is reverted and the run is a no-op (the workflow re-invokes on the
    next sync). The cross-run iteration cap and ``stuck`` escalation
    (SP-STUCK-ESCALATION, slice 9) bound that re-invocation: before
    attempting a fix, the recorded round history is assessed
    (:func:`stuck.assess_stuck`); when the cap or a no-progress stall is
    hit the open blocking findings are driven to ``stuck``, a routine
    dossier fires, and no fix is attempted (the push/CI loop stops). Each
    successful push records one round.

    Args:
        pr_number: GitHub PR number (must target ``develop``).
        gh: Optional pre-built :class:`GhCli` (tests inject a mock).
        repo_root: Repo root; defaults to ``cwd``.
        coder: Optional pre-built :class:`Coder` (tests inject a fake).
        db_path: Override the ledger db path; defaults to settings.
        routine_fire: The routine-fire callable for the stuck-escalation
            dossier; defaults to the live notifier (tests inject a fake).

    Returns:
        A :class:`CoderFindingsResult` describing what happened.
    """
    from .coder_loop import (
        CI_GREEN,
        CI_RED,
        apply_fixes,
        fetch_ci_status,
        fetch_failed_check_logs,
        git_commit_and_push,
        persist_placeholder_rejected,
        run_pytest_matrix,
        run_ruff_gate,
    )
    from .persistence import init_schema, record_coder_response

    repo = (repo_root or Path.cwd()).resolve()
    gh = gh or GhCli(cwd=repo)
    db = db_path or Path(get_settings().db_path)
    init_schema(db)
    init_findings_schema(db)

    pr_meta = gh.pr_view(pr_number) or {}
    base = str(pr_meta.get("baseRefName") or "")
    head = str(pr_meta.get("headRefName") or "")
    if base != "develop":
        _log.warning("coder_findings.refuse_non_develop_base", pr_number=pr_number, base=base)
        return CoderFindingsResult(
            pr_number=pr_number,
            no_op_reason=f"base={base!r}, refusing (only develop is allowed)",
        )

    head_sha = gh.pr_head_sha(pr_number) or ""
    ci_state, failed_rows = fetch_ci_status(gh, pr_number)
    if ci_state == CI_RED and failed_rows:
        record_ci_failures_as_findings(
            db,
            pr_number=pr_number,
            head_sha=head_sha,
            failed_rows=failed_rows,
            failure_logs=fetch_failed_check_logs(gh, failed_rows),
        )
    elif ci_state == CI_GREEN:
        resolve_broken_behavior_findings(db, pr_number=pr_number, head_sha=head_sha)
    open_verified_blocking(db, pr_number, head_sha=head_sha)
    findings = fetch_open_blocking_findings(db, pr_number)
    if not findings:
        return CoderFindingsResult(
            pr_number=pr_number,
            no_op_reason="no open blocking findings to resolve",
        )

    init_stuck_schema(db)
    prior_rounds = fetch_coder_rounds(db, pr_number)
    assessment = assess_stuck(prior_rounds)
    if assessment.stuck:
        stuck_findings = mark_findings_stuck(db, pr_number)
        _fire_stuck_dossier(
            build_stuck_dossier(
                pr_number=pr_number,
                reason=assessment.reason,
                rounds=prior_rounds,
                stuck_findings=stuck_findings,
            ),
            routine_fire=routine_fire,
        )
        _log.warning(
            "coder_findings.stuck_escalation",
            pr_number=pr_number,
            reason=assessment.reason,
            n_rounds=assessment.n_rounds,
            n_stuck=len(stuck_findings),
        )
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            stuck=True,
            no_op_reason=f"stuck — {assessment.reason}; escalated (no fix attempted)",
        )

    diff_res = gh.pr_diff(pr_number)
    if not diff_res.ok:
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            no_op_reason=f"diff fetch failed: {diff_res.stderr[:200]}",
        )

    spec_plan_md: str | None = None
    try:
        from .spec import maybe_load_active_spec

        spec = maybe_load_active_spec(branch=head)
        if spec is not None:
            spec_plan_md = spec.raw_markdown
    except Exception as exc:
        _log.info("coder_findings.spec_lookup_failed", branch=head, error=str(exc)[:160])

    coder = coder or Coder()
    plan = coder.respond_to_findings(
        findings=findings,
        diff=diff_res.stdout,
        pr_number=pr_number,
        spec_plan=spec_plan_md,
    )
    record_coder_response(
        db,
        pr_number=pr_number,
        plan=plan,
        model_used=str(plan.get("model_used") or ""),
        elapsed_s=float(plan.get("elapsed_s") or 0.0),
        tokens_used=int(plan.get("tokens_used") or 0),
    )

    fixes = plan.get("fixes") or []
    if not fixes:
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            no_op_reason="coder produced no fixes",
        )

    placeholder_rejections: list[dict[str, str]] = []
    applied, rejected = apply_fixes(
        fixes, repo_root=repo, placeholder_rejections_out=placeholder_rejections
    )
    if applied == 0:
        if placeholder_rejections:
            persisted = persist_placeholder_rejected(
                pr_number=pr_number, plan=plan, rejected=placeholder_rejections
            )
            _log.warning(
                "coder_findings.placeholder_rejected_no_fixes",
                pr_number=pr_number,
                n_rejections=len(placeholder_rejections),
                rejected_paths=[r["path"] for r in placeholder_rejections],
                persisted_path=str(persisted) if persisted is not None else None,
            )
            return CoderFindingsResult(
                pr_number=pr_number,
                n_open_findings=len(findings),
                fixes_applied=0,
                fixes_rejected=len(rejected),
                rejected_paths=rejected,
                placeholder_rejected=True,
                no_op_reason=(
                    f"all {len(placeholder_rejections)} fix(es) rejected as "
                    f"placeholder content — see "
                    f"{persisted.name if persisted else 'logs/'} for the full plan"
                ),
            )
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            fixes_applied=0,
            fixes_rejected=len(rejected),
            rejected_paths=rejected,
            no_op_reason="all proposed fixes rejected (whitelist)",
        )

    ruff_ok, ruff_tail = run_ruff_gate(repo)
    if not ruff_ok:
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            fixes_applied=applied,
            fixes_rejected=len(rejected),
            rejected_paths=rejected,
            no_op_reason=f"ruff gate red; fixes left uncommitted ({ruff_tail[:160]})",
        )

    pytest_ok, log_tail = run_pytest_matrix(repo)
    if not pytest_ok:
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            fixes_applied=applied,
            fixes_rejected=len(rejected),
            rejected_paths=rejected,
            pytest_passed=False,
            no_op_reason=f"pytest red; fixes left uncommitted ({log_tail[:160]})",
        )

    commit_msg = str(plan.get("commit_message") or "").strip() or (
        f"fix(review): resolve open findings on PR #{pr_number}"
    )
    pushed, push_log = git_commit_and_push(
        repo_root=repo, commit_message=commit_msg, branch=head or "develop"
    )
    if not pushed:
        return CoderFindingsResult(
            pr_number=pr_number,
            n_open_findings=len(findings),
            fixes_applied=applied,
            fixes_rejected=len(rejected),
            rejected_paths=rejected,
            pytest_passed=True,
            no_op_reason=f"commit/push failed: {push_log}",
        )

    new_head = _local_head_sha(repo)
    counts = reverify_resolution_for_pr(db, pr_number=pr_number, repo_root=repo, head_sha=new_head)
    bb_resolved = resolve_broken_behavior_findings(db, pr_number=pr_number, head_sha=new_head)
    record_coder_round(
        db,
        pr_number=pr_number,
        open_blocking_before=len(findings),
        open_blocking_after=counts["still_open"],
    )
    return CoderFindingsResult(
        pr_number=pr_number,
        n_open_findings=len(findings),
        fixes_applied=applied,
        fixes_rejected=len(rejected),
        rejected_paths=rejected,
        pytest_passed=True,
        pushed=True,
        resolved=counts["resolved"] + bb_resolved,
        still_open=counts["still_open"],
    )
