"""CLI subcommands for the review-bot team.

Exposed as ``repoach review …`` once the typer subapp is mounted on
the main app in :mod:`repoach.cli.main`.

Subcommands:

* ``repoach review pr <N>`` — run the four reviewer bots on PR / MR
  ``<N>`` and (by default) post their findings to the forge.
* ``repoach review pr <N> --dry-run`` — same but never publish to
  the forge; only persist to L4.  Useful for local previews and tests.

Every command takes ``--forge github|gitlab`` (SP-UNIVERSAL-FORGE G4);
without it the forge comes from ``REPOACH_FORGE``, ``repoach.toml`` or
the origin remote. A forge that cannot be selected exits ``2``.
* ``repoach review report <N>`` — pull the sticky archive comment
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

from ..core.config import GatewayConfigError, get_settings
from ..forge import (
    ARCHIVE_MARKER,
    Forge,
    ForgeError,
    ForgeWriteRefusedError,
    WriteRefusal,
    select_forge,
)
from ..project.config import load_project_config
from ..project.spec_source import declared_spec_missing, resolve_spec, spec_coverage_label
from ..providers.errors import ProviderConfigError
from ..providers.select import PROVIDER_FLAG_HELP, set_provider_override
from ..providers.usage import run_summary_line, run_usage_json, usage_run
from ..review.auto_merge import (
    OUTCOME_ALREADY_MERGED,
    evaluate_merge_gate,
    merge_exit_code,
    run_auto_merge,
)
from ..review.coder_findings import ReasonCode, run_coder_fix_from_findings
from ..review.dev_runner import open_pr, run_developer_session
from ..review.orchestrator import run_review
from ..review.review_head import HeadUnavailableError, HeadUnresolvedError
from ..review.reviewer import ReviewVerdict
from ..review.spec import canonical_spec_id, spec_exists, specs_dir_for
from .doctor_cmds import project_root_or_exit

_FORGE_HELP = (
    "Forge the PR / MR lives on: github or gitlab (default: REPOACH_FORGE, repoach.toml, origin)."
)

_SPEC_HELP = (
    "Spec the PR / MR implements; outranks a 'Spec: SP-…' description line and the branch "
    "name. Without any, the review runs with no spec (spec coverage: not_applicable)."
)


EXIT_REVIEW_ERROR = 3
"""``review pr`` exit code when a reviewer could not reach a model.

Distinct from 2 (request changes, or a ``--spec`` naming no document)
and from 5 (a refused merge gate): no review happened, nothing was
published (SP-REVIEW-TRANSPORT-ERROR-VERDICT)."""

EXIT_WRITE_REFUSED = 8
"""``review pr`` exit code when a dry run reached the forge write barrier.

Nothing was sent; a step attempted a forge write the run was not allowed
to make, which is a defect of that step (SP-DRY-RUN-NEVER-POSTS)."""


def _dry_run_refusal(value: str) -> WriteRefusal:
    """Return the :class:`WriteRefusal` a ``--dry-run-reason`` names.

    Raises:
        typer.Exit: Code 2 after one stderr line when ``value`` names none.
    """
    try:
        return WriteRefusal(value)
    except ValueError as exc:
        names = ", ".join(repr(r.value) for r in WriteRefusal)
        typer.echo(f"repoach: --dry-run-reason must be one of {names}", err=True)
        raise typer.Exit(code=2) from exc


def _spec_flag(value: object) -> str | None:
    """Return the ``--spec`` value, or None when absent.

    Anything but a non-empty string — typer's ``OptionInfo`` default when
    a command function is called directly from Python included — means
    no flag was given.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _require_spec(spec: str | None, root: Path) -> None:
    """End the command with rc 2 when ``--spec`` names no spec document.

    Args:
        spec: The ``--spec`` value, None when absent.
        root: The project root whose spec directory is searched.

    Raises:
        typer.Exit: Code 2, ``spec SP-X not found under <specs_dir>``.
    """
    if spec is None or spec_exists(spec, root=root):
        return
    where = specs_dir_for(root).relative_to(root)
    typer.echo(f"spec {canonical_spec_id(spec)} not found under {where}", err=True)
    raise typer.Exit(code=2)


def _forge_kwargs(forge_name: object) -> dict[str, Forge]:
    """Select the forge named by ``--forge``; ``{}`` lets the command select its own.

    Anything but a non-empty string — ``None``, or typer's ``OptionInfo``
    default when a command function is called directly from Python —
    means no flag was given.

    Raises:
        typer.Exit: Code 2 when the named forge cannot be selected.
    """
    if not isinstance(forge_name, str) or not forge_name:
        return {}
    try:
        return {"forge": select_forge(override=forge_name)}
    except ForgeError as exc:
        typer.echo(f"forge unavailable: {exc.message}", err=True)
        raise typer.Exit(code=2) from exc


def apply_provider_flag(value: str | None) -> None:
    """Make ``--provider`` outrank ``REPOACH_PROVIDER`` and ``[provider].kind`` for this process.

    Raises:
        typer.Exit: Code 2 after one stderr line when ``value`` names no
            provider.
    """
    if not isinstance(value, str):
        return
    try:
        set_provider_override(value)
    except ProviderConfigError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _forge_failure(exc: ForgeError) -> typer.Exit:
    """Report a forge that could not be selected or reached; exit code 2."""
    typer.echo(f"forge unavailable: {exc.message}", err=True)
    return typer.Exit(code=2)


review_app = typer.Typer(
    help="Review-bot team operations (NIM-only, no Anthropic quota).",
    no_args_is_help=True,
)

_DEV_SESSION_REASON_EXIT_CODES: dict[ReasonCode, int] = {
    ReasonCode.SPEC_NOT_FOUND: 5,
    ReasonCode.NO_FIXES: 4,
    ReasonCode.WHITELIST_REJECTED: 4,
    ReasonCode.SELF_VERIFY: 6,
    ReasonCode.DECOMPOSE_OR_SUPERSEDE: 7,
    ReasonCode.RUFF_RED: 3,
    ReasonCode.PYTEST_RED: 3,
}
"""``repoach review develop`` exit codes, keyed by identity on ``reason_code``.

Audit 2026-07-13 finding C4: this used to be a chain of
``substring in result.no_op_reason.lower()`` checks — brittle, since a
coincidental substring inside an unrelated message (e.g. a
contract-violation reason that happens to quote a ``ruff``-reformatted
path) could mismap to the wrong exit code. ``ReasonCode.PUSH_FAILED``
and ``ReasonCode.NONE`` are intentionally absent: both fall through to
the caller's ``not result.pushed`` catch-all (exit ``1``), matching the
pre-fix behaviour for every reason with no dedicated branch.
"""


@review_app.command("pr")
def review_pr(
    pr_number: int = typer.Argument(..., help="PR number (GitHub) or MR iid (GitLab) to review."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Run the four reviewers and persist outcomes to L4 but never "
            "publish anything to the forge. Useful for local previews."
        ),
    ),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
    spec: str | None = typer.Option(None, "--spec", help=_SPEC_HELP),
    provider: str | None = typer.Option(None, "--provider", help=PROVIDER_FLAG_HELP),
    keep_tree: bool = typer.Option(
        False,
        "--keep-tree",
        help="Keep the worktree of the PR head after the review and print its path.",
    ),
    dry_run_reason: str = typer.Option(
        WriteRefusal.DRY_RUN.value,
        "--dry-run-reason",
        hidden=True,
        help="Why a --dry-run writes nothing; set by `repoach ci review`.",
    ),
) -> None:
    """Run the review-bot team on a PR / MR and emit a summary.

    The four reviewers (Architect / Sentinel / Tester / Scribe) run in
    parallel against the diff read through the forge.  Their
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

    ``spec`` / ``spec_source`` name the spec the review ran against and
    where it was declared (``--spec``, a ``Spec:`` description line, the
    branch name); with none, ``spec`` is null and ``spec_coverage`` is
    ``not_applicable`` — never a refusal.  A ``Spec:`` line naming no
    spec document prints a warning and reports ``spec_coverage`` as
    ``missing``.  A ``--spec`` naming no spec document exits 2 before
    any forge call.

    A reviewer that could not reach a model (transport exhausted, bot
    crashed) ends ``ERROR``: the team verdict is ``ERROR``, nothing is
    published even without ``--dry-run`` (``posting_outcome`` is
    ``SKIP_ERROR``), and the command exits 3.  ``posting_outcome`` is null
    when posting ran or was not requested.

    Every model call is recorded in the usage ledger (SP-USAGE-LEDGER):
    ``usage`` in the JSON sums them per reviewer and in total (tokens,
    duration, cost estimate or ``subscription (tokens only)``), and one
    ``repoach: usage: …`` line on stderr says the total.

    Before the team verdict, every unsettled blocking finding raised at
    an older head is re-checked at the reviewed head, exactly as
    ``review gate`` does (SP-GATE-REVERIFIES-AT-HEAD G7): the ones the
    head no longer supports become ``resolved_at_head`` and no longer
    count, the others stay counted and are named in
    ``reverify_reasons`` (``raised at <sha7>, still present at
    <sha7>``). ``reverified`` in the JSON counts them and one
    ``review: re-verified …`` line on stderr says it. CI is not read,
    so ``broken_behavior`` findings are left to the gate.

    The review reads the PR head, never the checkout's branch
    (SP-REVIEW-PINS-HEAD G2): the head SHA comes from the forge, its tree
    is the checkout when that is at the SHA and clean, else a detached
    worktree under the state directory, removed after the run unless
    ``--keep-tree``. One ``reviewing <N> at <sha7> (<tree> <path>)`` line
    on stderr says which; the JSON carries ``head_sha``, ``tree``
    (``worktree`` / ``checkout``) and ``worktree`` (the kept path, else
    null). A head the forge does not serve, or one that cannot be
    fetched, exits 2 before any reviewer runs.

    ``findings`` lists every finding the refuter saw, each with
    ``refutation = {state, reason}`` (``verified`` / ``refuted`` /
    ``deferred`` / ``unverified``), and one ``refuter: judged …`` line on
    stderr counts them by state and reason
    (SP-REFUTER-JUDGES-WHAT-IT-CAN).

    A dry run closes the forge's write barrier (SP-DRY-RUN-NEVER-POSTS):
    ``refused_writes`` counts the writes it refused and ``write_refusal``
    names why it was closed (``dry run``; null when posting). One
    ``repoach: refused_writes: N (<reason>)`` line on stderr says the
    same. A refused write is a defect: one ``repoach: dry run attempted
    a forge write: <head>`` line per write, and the command exits 8.
    """
    root = project_root_or_exit()
    apply_provider_flag(provider)
    spec_flag = _spec_flag(spec)
    _require_spec(spec_flag, root)
    refusal = _dry_run_refusal(dry_run_reason)
    try:
        with usage_run("review pr", pr_number) as run:
            team = run_review(
                pr_number,
                post=not dry_run,
                spec=spec_flag,
                keep_tree=keep_tree,
                announce=lambda line: typer.echo(line, err=True),
                write_refusal=refusal,
                **_forge_kwargs(forge_name),
            )
    except (GatewayConfigError, HeadUnresolvedError, HeadUnavailableError) as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except ForgeWriteRefusedError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_WRITE_REFUSED) from exc
    except ForgeError as exc:
        raise _forge_failure(exc) from exc
    spec_id = team.spec_id
    if team.spec_missing:
        where = specs_dir_for(root).relative_to(root)
        typer.echo(
            f"repoach: warning: spec {spec_id} (from {team.spec_source}) not found under "
            f"{where}; reviewed without a spec",
            err=True,
        )
    payload = {
        "pr_number": team.pr_number,
        "head_sha": team.head_sha,
        "tree": team.tree,
        "worktree": team.worktree,
        "spec": spec_id,
        "spec_source": team.spec_source,
        "spec_coverage": spec_coverage_label(spec_id, team.spec_covered, missing=team.spec_missing),
        "final_verdict": team.final_verdict.value,
        "n_blockers": team.n_blockers,
        "n_majors": team.n_majors,
        "posted_comments": team.posted_comments,
        "posted_reviews": team.posted_reviews,
        "posting_outcome": team.posting_outcome,
        "refused_writes": len(team.refused_writes),
        "write_refusal": team.write_refusal,
        "reverified": team.reverified,
        "reverify_reasons": team.reverify_reasons,
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
        "findings": [refutation.as_json() for refutation in team.refutations],
        "usage": run_usage_json(run),
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    if team.refuter_summary is not None:
        typer.echo(team.refuter_summary, err=True)
    if team.reverify_line:
        typer.echo(team.reverify_line, err=True)
    typer.echo(f"repoach: {run_summary_line(run)}", err=True)
    if team.write_refusal is not None or team.refused_writes:
        typer.echo(
            f"repoach: refused_writes: {len(team.refused_writes)} "
            f"({team.write_refusal or 'writes allowed'})",
            err=True,
        )
    for head in team.refused_writes:
        typer.echo(f"repoach: {team.write_refusal} attempted a forge write: {head}", err=True)
    if team.refused_writes:
        raise typer.Exit(code=EXIT_WRITE_REFUSED)
    if team.final_verdict == ReviewVerdict.ERROR:
        raise typer.Exit(code=EXIT_REVIEW_ERROR)
    if team.final_verdict == ReviewVerdict.REQUEST_CHANGES:
        raise typer.Exit(code=2)


_ARCHIVE_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


@review_app.command("report")
def review_report(
    pr_number: int = typer.Argument(..., help="PR number (GitHub) or MR iid (GitLab)."),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Emit the full archive comment markdown instead of the JSON payload.",
    ),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
) -> None:
    """Fetch the sticky review-archive comment off a PR and print it.

    Works whether the bots ran locally or on the GitHub Actions
    runner — the source of truth is the PR comment carrying the
    ``repoach-review-archive`` marker.

    Exit codes (SP-SAFE-MERGE-ARCHIVE-RETRY — gating callers such as
    ``safe_merge.sh`` retry code ``6`` but treat ``1`` as final):

    * ``0`` — archive comment found and printed.
    * ``1`` — the API answered but no comment carries the marker.
    * ``6`` — the forge API call itself failed (transient: rate
      limit, network); the archive may well exist.
    * ``2`` — the forge could not be selected.
    """
    project_root_or_exit()
    forge = _forge_kwargs(forge_name).get("forge")
    try:
        forge = forge or select_forge()
    except ForgeError as exc:
        raise _forge_failure(exc) from exc
    try:
        found = forge.find_marked_comment(pr_number, ARCHIVE_MARKER)
    except ForgeError as exc:
        typer.echo(
            f"Archive fetch failed on PR #{pr_number} — {forge.name} API error, "
            f"not a missing archive: {exc.message[:200]}",
            err=True,
        )
        raise typer.Exit(code=6) from exc
    if found is None:
        typer.echo(
            f"No archive comment found on PR #{pr_number} (marker: {ARCHIVE_MARKER})",
            err=True,
        )
        raise typer.Exit(code=1)
    body = found.body
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
    pr_number: int = typer.Argument(..., help="PR number (GitHub) or MR iid (GitLab) to auto-fix."),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
    spec: str | None = typer.Option(None, "--spec", help=_SPEC_HELP),
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
      * ``2`` — ``--spec`` names no spec document, or the forge is unusable.

    The Coder reads the spec resolved like ``review pr`` (``--spec`` >
    ``Spec:`` description line > branch name); without one it fixes the
    findings with no spec context.
    """
    root = project_root_or_exit()
    spec_flag = _spec_flag(spec)
    _require_spec(spec_flag, root)
    try:
        fr = run_coder_fix_from_findings(pr_number, spec=spec_flag, **_forge_kwargs(forge_name))
    except ForgeError as exc:
        raise _forge_failure(exc) from exc
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
    if fr.wrong_base:
        raise typer.Exit(code=5)
    if fr.placeholder_rejected:
        raise typer.Exit(code=9)
    if fr.stuck:
        raise typer.Exit(code=3)
    if not fr.pushed:
        raise typer.Exit(code=4)


@review_app.command("merge")
def review_merge(
    pr_number: int = typer.Argument(
        ..., help="PR number (GitHub) or MR iid (GitLab) to auto-merge."
    ),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
) -> None:
    """Merge a PR into ``develop`` once all gates are green (configured method, squash by default).

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

    Like ``review gate``, the ledger is re-verified on the tree of the
    verified head, never on the checkout's branch; the findings of older
    heads are re-checked there (SP-GATE-REVERIFIES-AT-HEAD).
    """
    project_root_or_exit()
    try:
        result = run_auto_merge(pr_number, pin_tree=True, **_forge_kwargs(forge_name))
    except ForgeError as exc:
        raise _forge_failure(exc) from exc
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
    project_root_or_exit()
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
    pr_number: int = typer.Argument(..., help="PR number (GitHub) or MR iid (GitLab) to evaluate."),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
    spec: str | None = typer.Option(None, "--spec", help=_SPEC_HELP),
    provider: str | None = typer.Option(None, "--provider", help=PROVIDER_FLAG_HELP),
    keep_tree: bool = typer.Option(
        False,
        "--keep-tree",
        help="Keep the worktree of the PR head after the gate and print its path.",
    ),
) -> None:
    """Evaluate the pure merge gate for a PR — read-only, never merges.

    SP-VERDICT-FLIP (10a): the evidence-first decision exposed for
    ``scripts/safe_merge.sh`` to gate on, replacing the forgeable
    archive 4/4-APPROVE check. It re-verifies the findings ledger at
    head, waits on required CI, and emits the decision as JSON. It does
    not post and does not merge — ``safe_merge.sh`` keeps ownership of
    the actual ``gh pr merge``. The decision is byte-identical to the
    one ``repoach review merge`` would reach (both go through
    :func:`decide_at_head`).

    Exit codes:
      * ``0`` — gate satisfied (``merge`` is True): the PR may merge; or
        the PR is already merged (``outcome`` ``ALREADY_MERGED``,
        ``merge`` False, nothing left to gate).
      * ``5`` — gate refused (``merge`` is False): see ``reasons``.
      * ``1`` — could not evaluate (transport error gathering facts).

    ``spec`` / ``spec_source`` follow the same resolution as ``review pr``
    (``--spec`` > ``Spec:`` description line > branch name).
    ``facts.spec_coverage`` is ``not_applicable`` when no spec is
    declared: a PR without a spec is never refused for lacking one; it
    is ``missing`` (with the id in ``spec``) when a ``Spec:`` line names
    a spec that has no document.

    The verifiers re-check the ledger on the tree of the verified head,
    never on the checkout's branch (SP-REVIEW-PINS-HEAD G2): one
    ``reviewing <N> at <sha7> (<tree> <path>)`` line on stderr names it,
    ``tree`` in the JSON says ``worktree`` or ``checkout``, and
    ``--keep-tree`` keeps the worktree. When the gate stops before any
    tree is built (an MR already merged, a stale head), ``--keep-tree``
    prints ``nothing materialised (<why>)`` instead of a path.

    Every unsettled blocking finding raised at an older head is
    re-checked at the head first (SP-GATE-REVERIFIES-AT-HEAD): the ones
    the head no longer supports become ``resolved_at_head``, the others
    are named in ``reasons`` (``still present at <sha7>`` or
    ``unresolved: needs judgement``). One ``gate: re-verified …`` line
    on stderr says it, and ``reverified`` in the JSON counts it. A
    judged finding is re-judged by the refuter, so the gate may make
    model calls through ``--provider`` (up to the refuter cap).
    """
    root = project_root_or_exit()
    apply_provider_flag(provider)
    spec_flag = _spec_flag(spec)
    _require_spec(spec_flag, root)
    try:
        evaluation = evaluate_merge_gate(
            pr_number,
            pin_tree=True,
            keep_tree=keep_tree,
            announce=lambda line: typer.echo(line, err=True),
            **_forge_kwargs(forge_name),
        )
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
    pr = evaluation.pull_request
    declared = resolve_spec(
        flag=spec_flag,
        description=pr.body if pr is not None else "",
        branch=pr.head_ref if pr is not None else "",
        sources=load_project_config(root).review.spec_sources,
        root=root,
    )
    coverage = spec_coverage_label(
        declared.spec_id,
        facts.spec_covered if facts.spec_coverage_known else None,
        missing=declared_spec_missing(declared, root),
    )
    typer.echo(
        json.dumps(
            {
                "pr_number": pr_number,
                "outcome": evaluation.ci_gate.outcome_tag,
                "head_sha": evaluation.head_sha,
                "tree": evaluation.tree,
                "spec": declared.spec_id,
                "spec_source": declared.source,
                "merge": evaluation.decision.merge,
                "reasons": evaluation.decision.reasons,
                "reverified": facts.reverified,
                "facts": {
                    "ci_green": facts.ci_green,
                    "open_blocking_findings": facts.open_blocking_findings,
                    "spec_coverage": coverage,
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
    if facts.reverify_line:
        typer.echo(facts.reverify_line, err=True)
    if keep_tree and evaluation.tree is None:
        merged = evaluation.ci_gate.outcome_tag == OUTCOME_ALREADY_MERGED
        why = "already merged" if merged else evaluation.ci_gate.outcome_tag.lower()
        typer.echo(f"nothing materialised ({why})", err=True)
    if evaluation.ci_gate.outcome_tag == OUTCOME_ALREADY_MERGED:
        return
    if not evaluation.decision.merge:
        raise typer.Exit(code=5)


@review_app.command("develop")
def review_develop(
    spec_id: str = typer.Argument(
        ...,
        help='Spec identifier — accepts "sec", "SP-SEC", "sp-sec", etc.',
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Source ref the new branch is created from. Defaults to integration_branch.",
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
        help="Whether to open a PR / MR through the forge after pushing (default: yes).",
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
    project_root_or_exit()
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

    exit_code = _DEV_SESSION_REASON_EXIT_CODES.get(result.reason_code)
    if exit_code is not None:
        raise typer.Exit(code=exit_code)
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
    project_root_or_exit()
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

    if outcome.reason_code is ReasonCode.SPEC_NOT_FOUND:
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
    project_root_or_exit()
    db_path = Path(get_settings().db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
