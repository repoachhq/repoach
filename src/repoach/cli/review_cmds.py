"""CLI subcommands for the review-bot team.

Exposed as ``ferova review …`` once the typer subapp is mounted on
the main app in :mod:`repoach.cli.main`.

Subcommands:

* ``ferova review pr <N>`` — run the four reviewer bots on PR
  ``<N>`` and (by default) post their findings to GitHub.
* ``ferova review pr <N> --dry-run`` — same but never publish to
  GitHub; only persist to L4.  Useful for local previews and tests.
* ``ferova review report <N>`` — pull the sticky archive comment
  off PR ``<N>`` and emit the full TeamOutcome as JSON.  Works
  regardless of where the workflow ran (local machine vs. Actions
  runner) — the comment lives on the PR itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from sqlalchemy import create_engine, text

from ..core.config import get_settings
from ..review.auto_merge import (
    evaluate_merge_gate,
    merge_exit_code,
    run_auto_merge,
)
from ..review.coder_findings import run_coder_fix_from_findings
from ..review.dev_runner import open_pr, run_developer_session
from ..review.gh_client import GhCli
from ..review.orchestrator import run_review

review_app = typer.Typer(
    help="Review-bot team operations (NIM-only, no Anthropic quota).",
    no_args_is_help=True,
)


@review_app.command("pr")
def review_pr(
    pr_number: int = typer.Argument(..., help="GitHub PR number to review."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Run the four reviewers and persist outcomes to L4 but never "
            "publish anything to GitHub. Useful for local previews."
        ),
    ),
) -> None:
    """Run the review-bot team on a GitHub PR and emit a summary.

    The four reviewers (Architect / Sentinel / Tester / Scribe) run in
    parallel against the PR diff fetched via ``gh pr diff``.  Their
    individual verdicts are aggregated into a final team verdict
    (``REQUEST_CHANGES`` wins, otherwise ``APPROVE`` wins, otherwise
    ``COMMENT``) which is posted alongside per-bot inline comments.

    The emitted JSON payload's *schema* is identical between live and
    ``--dry-run`` invocations (``tests/unit/test_review_ci_mode.py``
    pins both).  Only the ``posted_comments`` / ``posted_reviews``
    counters differ : they are zero on a dry run because nothing was
    actually published to GitHub.  ``scripts/ci_local.sh --review`` and
    the ``.github/workflows/auto-review.yml`` workflow therefore consume
    the same shape regardless of which mode they ran in
    (SP-AUTO-REVIEW-V2-MIGRATION).
    """
    team = run_review(pr_number, post=not dry_run)
    payload = {
        "pr_number": team.pr_number,
        "final_verdict": team.final_verdict.value,
        "n_blockers": team.n_blockers,
        "n_majors": team.n_majors,
        "posted_comments": team.posted_comments,
        "posted_reviews": team.posted_reviews,
        "reviews": [
            {
                "role": o.role.value,
                "verdict": o.verdict.value,
                "n_comments": len(o.comments),
                "n_blockers": sum(1 for c in o.comments if c.severity == "blocker"),
                "n_majors": sum(1 for c in o.comments if c.severity == "major"),
                "model_used": o.model_used,
                "elapsed_s": round(o.elapsed_s, 2),
                "tokens_used": o.tokens_used,
                "summary": o.summary,
                "trace": list(getattr(o, "trace", []) or []),
            }
            for o in team.reviews
        ],
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    if team.final_verdict.value == "REQUEST_CHANGES":
        raise typer.Exit(code=2)


_ARCHIVE_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@review_app.command("report")
def review_report(
    pr_number: int = typer.Argument(..., help="GitHub PR number."),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Emit the full archive comment markdown instead of the JSON payload.",
    ),
) -> None:
    """Fetch the sticky review-archive comment off a PR and print it.

    Works whether the bots ran locally or on the GitHub Actions
    runner — the source of truth is the PR comment carrying the
    ``ferova-review-archive`` marker.

    Exit codes (SP-SAFE-MERGE-ARCHIVE-RETRY — gating callers such as
    ``safe_merge.sh`` retry code ``6`` but treat ``1`` as final):

    * ``0`` — archive comment found and printed.
    * ``1`` — the API answered but no comment carries the marker.
    * ``6`` — the GitHub API call itself failed (transient: rate
      limit, network); the archive may well exist.
    """
    gh = GhCli()
    fetch = gh.fetch_archive_comment_with_status(pr_number)
    if fetch.api_error is not None:
        typer.echo(
            f"Archive fetch failed on PR #{pr_number} — gh API error, "
            f"not a missing archive: {fetch.api_error}",
            err=True,
        )
        raise typer.Exit(code=6)
    body = fetch.body
    if body is None:
        typer.echo(
            f"No archive comment found on PR #{pr_number} (marker: {gh.ARCHIVE_MARKER})",
            err=True,
        )
        raise typer.Exit(code=1)
    if raw:
        typer.echo(body)
        return
    match = _ARCHIVE_FENCE.search(body)
    if match is None:
        typer.echo("Archive comment found but no JSON fence inside.", err=True)
        typer.echo(body)
        raise typer.Exit(code=1)
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        typer.echo(f"Archive JSON unparseable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@review_app.command("fix")
def review_fix(
    pr_number: int = typer.Argument(..., help="GitHub PR number to auto-fix."),
) -> None:
    """Run one findings-driven Coder auto-fix iteration on a PR.

    Resolves the PR's open blocking findings from the ledger
    (evidence-first): fetches them, invokes the Coder to fix each,
    validates proposed edits against the path whitelist + placeholder
    guard, runs ``ruff`` + ``pytest``, commits + pushes if green, then
    re-verifies each finding's resolution at head.

    Exit codes:
      * ``0`` — fixes applied & pushed (the in-run re-review takes over).
      * ``4`` — nothing to fix (no open blocking findings, or pytest red
        and reverted).
      * ``5`` — base branch is not ``develop`` (refused by safety gate).
      * ``9`` — SP-CODER-PLACEHOLDER-DETECT: every proposed fix was
        rejected as an LLM placeholder string ("# ... rest of file
        ...", single-comment-line file, massive shrinkage, test file
        with no ``def test_``).  The runner refused to corrupt the
        working tree; the full plan is persisted to
        ``logs/coder_placeholder_rejected_<pr>_<utc>.txt``.
      * ``3`` — SP-STUCK-ESCALATION: the cross-run iteration cap
        (``stuck.MAX_CODER_ROUNDS`` rounds) or a no-progress stall was
        hit; the surviving open blocking findings were driven to
        ``stuck``, a routine dossier was fired, and no fix was attempted.
        The push/CI loop stops here for human intervention.
    """
    fr = run_coder_fix_from_findings(pr_number)
    typer.echo(
        json.dumps(
            {
                "pr_number": fr.pr_number,
                "n_open_findings": fr.n_open_findings,
                "fixes_applied": fr.fixes_applied,
                "fixes_rejected": fr.fixes_rejected,
                "rejected_paths": fr.rejected_paths,
                "pytest_passed": fr.pytest_passed,
                "pushed": fr.pushed,
                "resolved": fr.resolved,
                "still_open": fr.still_open,
                "placeholder_rejected": fr.placeholder_rejected,
                "stuck": fr.stuck,
                "no_op_reason": fr.no_op_reason,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if fr.no_op_reason and "base=" in fr.no_op_reason:
        raise typer.Exit(code=5)
    if fr.placeholder_rejected:
        raise typer.Exit(code=9)
    if fr.stuck:
        raise typer.Exit(code=3)
    if not fr.pushed:
        raise typer.Exit(code=4)


@review_app.command("merge")
def review_merge(
    pr_number: int = typer.Argument(..., help="GitHub PR number to auto-merge."),
) -> None:
    """Squash-merge a PR into ``develop`` once all gates are green.

    Gates (all must hold): base = ``develop``, PR open, required CI
    checks all green, and the pure merge gate satisfied at head (zero
    open blocking findings, complete review, spec coverage).

    Exit codes:
      * ``0`` — merged successfully OR already merged (``APPROVE``,
        ``ALREADY_MERGED``).
      * ``5`` — a non-merge gate prevented action (``SKIP_BASE``,
        ``SKIP_GATE``, ``SKIP_CI_RED``, ``SKIP_CI_FAILED``,
        ``SKIP_CI_TIMEOUT``, ``SKIP_CI_MISSING``, ``SKIP_STALE_HEAD``).
        Non-fatal — the next review round can pick it up.
      * ``1`` — merge attempted but failed (``FAILED``) or any
        unrecognised outcome.
    """
    result = run_auto_merge(pr_number)
    typer.echo(
        json.dumps(
            {
                "pr_number": result.pr_number,
                "outcome": result.outcome,
                "merged_sha": result.merged_sha,
                "notes": result.notes,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    raise typer.Exit(code=merge_exit_code(result.outcome))


@review_app.command("insights")
def review_insights(
    pr_number: int | None = typer.Argument(
        None, help="Restrict to one PR; omit to aggregate across the whole ledger."
    ),
) -> None:
    """Report findings-ledger insights: status / claim-type mix + per-lens precision.

    The aggregate (long-loop) view of the review learning loop
    (SP-REVIEW-LESSONS): how findings distribute across the lifecycle and
    claim types, and each lens's precision — the fraction of its findings
    that reached a verdict and were confirmed real rather than refuted.
    Read-only; always exits 0.
    """
    from ..review.review_lessons import gather_insights

    insights = gather_insights(Path(get_settings().db_path), pr_number=pr_number)
    typer.echo(
        json.dumps(
            {
                "scope": "all" if pr_number is None else f"pr#{pr_number}",
                "total": insights.total,
                "by_status": insights.by_status,
                "by_claim_type": insights.by_claim_type,
                "planner_rule_violations": insights.planner_rule_violations,
                "lens_precision": [
                    {
                        "finder": lp.finder,
                        "confirmed": lp.confirmed,
                        "refuted": lp.refuted,
                        "precision": lp.precision,
                    }
                    for lp in insights.lens_precision
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@review_app.command("gate")
def review_gate(
    pr_number: int = typer.Argument(..., help="GitHub PR number to evaluate."),
) -> None:
    """Evaluate the pure merge gate for a PR — read-only, never merges.

    SP-VERDICT-FLIP (10a): the evidence-first decision exposed for
    ``scripts/safe_merge.sh`` to gate on, replacing the forgeable
    archive 4/4-APPROVE check. It re-verifies the findings ledger at
    head, waits on required CI, and emits the decision as JSON. It does
    not post and does not merge — ``safe_merge.sh`` keeps ownership of
    the actual ``gh pr merge``. The decision is byte-identical to the
    one ``ferova review merge`` would reach (both go through
    :func:`decide_at_head`).

    Exit codes:
      * ``0`` — gate satisfied (``merge`` is True): the PR may merge.
      * ``5`` — gate refused (``merge`` is False): see ``reasons``.
      * ``1`` — could not evaluate (transport error gathering facts).
    """
    try:
        evaluation = evaluate_merge_gate(pr_number)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {"pr_number": pr_number, "error": str(exc)},
                indent=2,
                ensure_ascii=False,
            )
        )
        raise typer.Exit(code=1) from exc
    facts = evaluation.facts
    typer.echo(
        json.dumps(
            {
                "pr_number": pr_number,
                "head_sha": evaluation.head_sha,
                "merge": evaluation.decision.merge,
                "reasons": evaluation.decision.reasons,
                "facts": {
                    "ci_green": facts.ci_green,
                    "open_blocking_findings": facts.open_blocking_findings,
                    "spec_covered": facts.spec_covered,
                    "spec_coverage_known": facts.spec_coverage_known,
                    "review_complete": facts.review_complete,
                    "review_integrity_known": facts.review_integrity_known,
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if not evaluation.decision.merge:
        raise typer.Exit(code=5)


@review_app.command("develop")
def review_develop(
    spec_id: str = typer.Argument(
        ...,
        help='Spec identifier — accepts "sec", "SP-SEC", "sp-sec", etc.',
    ),
    base: str = typer.Option(
        "develop",
        "--base",
        help="Source ref the new branch is created from. Defaults to develop.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Override the auto-generated branch name (default: feat/sp-<slug>-impl).",
    ),
    no_push: bool = typer.Option(
        False,
        "--no-push",
        help="Run the Developer + local gate, but do not push or open a PR (dry-run).",
    ),
    open_pull_request: bool = typer.Option(
        True,
        "--open-pr/--no-open-pr",
        help="Whether to call ``gh pr create`` after pushing (default: yes).",
    ),
    explore_via: str = typer.Option(
        "proxy",
        "--explore-via",
        help="Planning backend when no plan is committed: 'proxy' (NIM/coder "
        "chain) or 'claude_cli' (one read-only claude -p session on the Max quota).",
    ),
    cc_model: str = typer.Option(
        "sonnet",
        "--cc-model",
        help="CLI model alias for --explore-via claude_cli (sonnet/opus/haiku).",
    ),
) -> None:
    """Dispatch the autonomous NIM Developer on a spec.

    Reads the spec at
    ``docs/specs/<...>{SPEC_ID}<...>.md`` and:

    1. Builds prompt context from files the spec references.
    2. Invokes the Developer agent (full-file rewrites + tests).
    3. Validates paths against the same whitelist as the Coder loop.
    4. Runs the local CI gate (``ruff check`` + ``pytest`` matrix
       3.11 + 3.13 if available) before any push.
    5. On green: commit as ``dev-bot[bot]``, push, open PR → develop.
    6. The PR then enters the existing NIM review→fix→merge pipeline
       (every review bot in the loop receives the same spec).

    Exit codes:
      * ``0`` — implementation pushed (and PR opened, unless --no-push)
      * ``2`` — bad ``--explore-via`` value.
      * ``3`` — local ruff/pytest gate red after Developer output; the
        partial work is left on disk (no revert), so the caller can
        inspect it and rerun after editing the spec.
      * ``4`` — Developer produced no fixes (spec ambiguous?).
      * ``5`` — spec id not found (no doc matches).
      * ``6`` — self-verify gate failed (mechanical or judge).
      * ``7`` — decomposition / parent supersession failed.
    """
    if explore_via not in ("proxy", "claude_cli"):
        typer.echo(f"--explore-via must be 'proxy' or 'claude_cli', got {explore_via!r}", err=True)
        raise typer.Exit(code=2)

    result = run_developer_session(
        spec_id,
        base=base,
        branch=branch,
        push=not no_push,
        explore_via=explore_via,
        cc_model=cc_model,
    )

    from ..review.builder_memory import remember_build_outcome

    remember_build_outcome(
        result.spec_id,
        pushed=result.pushed,
        no_op_reason=result.no_op_reason,
        n_steps=result.steps_completed,
    )

    payload = {
        "spec_id": result.spec_id,
        "branch": result.branch,
        "fixes_applied": result.fixes_applied,
        "fixes_rejected": result.fixes_rejected,
        "rejected_paths": result.rejected_paths,
        "ruff_passed": result.ruff_passed,
        "pytest_passed": result.pytest_passed,
        "self_verified": result.self_verified,
        "steps_total": result.steps_total,
        "steps_completed": result.steps_completed,
        "decomposed": result.decomposed,
        "sub_spec_ids": result.sub_spec_ids,
        "pushed": result.pushed,
        "no_op_reason": result.no_op_reason,
    }

    if result.pushed and open_pull_request:
        if result.decomposed:
            spec_ref = (
                f"`{result.spec_id}` was decomposed into sub-specs "
                f"({', '.join(result.sub_spec_ids)}) — see the diff."
            )
        else:
            spec_ref = f"`{result.spec_id}` (see `docs/specs/`)."
        url = open_pr(
            spec_id=result.spec_id,
            title=result.pr_title,
            summary=result.pr_summary,
            spec_ref=spec_ref,
            branch=result.branch,
            base=base,
        )
        payload["pr_url"] = url

    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))

    reason = (result.no_op_reason or "").lower()
    if "spec not found" in reason:
        raise typer.Exit(code=5)
    if "no fixes" in reason or "rejected by whitelist" in reason:
        raise typer.Exit(code=4)
    if "self-verify" in reason:
        raise typer.Exit(code=6)
    if "decompose" in reason or "supersede" in reason:
        raise typer.Exit(code=7)
    if "ruff" in reason or "pytest" in reason:
        raise typer.Exit(code=3)
    if not result.pushed and not no_push:
        raise typer.Exit(code=1)


@review_app.command("plan")
def review_plan(
    spec_id: str = typer.Argument(
        ...,
        help='Spec identifier — accepts "sec", "SP-SEC", "sp-sec", etc.',
    ),
    explore_via: str = typer.Option(
        "proxy",
        "--explore-via",
        help="Exploration backend: 'proxy' (NIM/coder chain) or "
        "'claude_cli' (one read-only claude -p session on the Max quota).",
    ),
    cc_model: str = typer.Option(
        "sonnet",
        "--cc-model",
        help="CLI model alias for --explore-via claude_cli (sonnet/opus/haiku).",
    ),
) -> None:
    """Run the Planner agent on a spec and write ``docs/plans/<SP-ID>.md``.

    SP-PLANNER-AGENT: the Planner explores the repository with
    read-only tools, emits a validated ActionPlan, and the session
    renders it to the plan document. Committing that document as the
    branch's first commit is the Developer runner's job
    (SP-DEV-PLAN-EXEC), not this command's.

    ``--explore-via claude_cli`` (SP-PLANNER-CC-EXPLORE) delegates the
    exploration to one ``claude -p`` session with the CLI's native
    read-only tools instead of the proxy chain.

    Exit codes:
      * ``0`` — plan written.
      * ``1`` — planner failed (no payload, invalid plan, wrong spec id).
      * ``2`` — bad ``--explore-via`` value.
      * ``5`` — spec id not found (no doc matches).
    """
    from ..review.planner import run_planner_session

    if explore_via not in ("proxy", "claude_cli"):
        typer.echo(f"--explore-via must be 'proxy' or 'claude_cli', got {explore_via!r}", err=True)
        raise typer.Exit(code=2)

    outcome = run_planner_session(spec_id, explore_via=explore_via, cc_model=cc_model)
    payload = {
        "spec_id": outcome.spec_id,
        "plan_path": outcome.plan_path,
        "written": outcome.written,
        "error": outcome.error,
        "n_steps": outcome.n_steps,
        "tool_calls": outcome.tool_calls,
        "turns": outcome.turns,
        "tokens_used": outcome.tokens_used,
        "model_used": outcome.model_used,
        "elapsed_s": outcome.elapsed_s,
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))

    reason = (outcome.error or "").lower()
    if "spec not found" in reason:
        raise typer.Exit(code=5)
    if not outcome.written:
        raise typer.Exit(code=1)


@review_app.command("hallucinations")
def review_hallucinations(
    pr_number: int | None = typer.Argument(
        None,
        help="Restrict to one PR.  When omitted, aggregate across all PRs.",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        help="Cap the per-event listing to this many rows (most recent first).",
    ),
) -> None:
    """Summarise reviewer hallucinations downgraded by the V2-B3 guard.

    Reads the L4 ``pr_hallucinations`` table populated by
    :class:`ReviewTeamOrchestrator` and emits a JSON payload with:

    * ``totals.by_role`` — count of downgrades per reviewer
    * ``totals.by_reason`` — count per pattern
      (``self_referential`` / ``missing_token_found_in_file``)
    * ``events`` — most recent downgrades, capped by ``--limit``

    Exits ``0`` whether or not any rows are present.
    """
    db_path = Path(get_settings().db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    where = "WHERE pr_number = :pr_number" if pr_number is not None else ""
    bind = {"pr_number": pr_number} if pr_number is not None else {}

    with engine.begin() as conn:
        try:
            totals_role = conn.execute(
                text(
                    f"SELECT role, COUNT(*) AS n FROM pr_hallucinations "
                    f"{where} GROUP BY role ORDER BY n DESC"
                ),
                bind,
            ).all()
            totals_reason = conn.execute(
                text(
                    f"SELECT reason, COUNT(*) AS n FROM pr_hallucinations "
                    f"{where} GROUP BY reason ORDER BY n DESC"
                ),
                bind,
            ).all()
            rows = conn.execute(
                text(
                    f"SELECT pr_number, role, file, line, original_severity, "
                    f"reason, tokens_found, original_body, created_at "
                    f"FROM pr_hallucinations {where} ORDER BY created_at DESC LIMIT :limit"
                ),
                {**bind, "limit": limit},
            ).all()
        except Exception as exc:
            typer.echo(f"L4 query failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    payload = {
        "scope": {"pr_number": pr_number, "limit": limit},
        "totals": {
            "by_role": {row[0]: row[1] for row in totals_role},
            "by_reason": {row[0]: row[1] for row in totals_reason},
            "n_events": sum(row[1] for row in totals_role),
        },
        "events": [
            {
                "pr_number": row[0],
                "role": row[1],
                "file": row[2],
                "line": row[3],
                "original_severity": row[4],
                "reason": row[5],
                "tokens_found": json.loads(row[6] or "[]"),
                "original_body": row[7],
                "created_at": str(row[8]),
            }
            for row in rows
        ],
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
