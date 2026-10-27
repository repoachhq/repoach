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
* ``repoach review merge-attempts <N>`` — every recorded merge attempt
  of PR ``<N>`` (SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G5).
* ``repoach review stranded`` — the open PRs green at their head that no
  merge reached, ``--dispatch`` re-arms them (G3, D3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import typer
from sqlalchemy import create_engine, text

from ..arch import MalformedFrontmatterError
from ..core.config import GatewayConfigError, get_settings
from ..core.logging import get_logger
from ..core.redaction import redact_error
from ..forge import (
    ARCHIVE_MARKER,
    Forge,
    ForgeError,
    ForgeWriteRefusedError,
    WriteRefusal,
    select_forge,
)
from ..project.config import ConfigError, load_project_config
from ..project.spec_source import (
    SpecResolution,
    declared_spec_missing,
    resolve_spec,
    spec_coverage_label,
)
from ..providers.errors import ProviderConfigError
from ..providers.select import PROVIDER_FLAG_HELP, set_provider_override
from ..providers.usage import run_summary_line, run_usage_json, usage_run
from ..review.auto_merge import (
    OUTCOME_ALREADY_MERGED,
    GateEvaluation,
    evaluate_merge_gate,
    merge_exit_code,
    run_auto_merge,
)
from ..review.coder_findings import ReasonCode, run_coder_fix_from_findings
from ..review.dev_runner import open_pr, run_developer_session
from ..review.generated_artefacts import report_lines
from ..review.generated_verify import GeneratedRefusalError, verify_generated
from ..review.governed_spec import SpecRefUnavailableError, branch_spec_id_at_ref
from ..review.merge_wait import (
    attempts_line,
    attempts_report,
    check_producers,
    dispatch_stranded,
    judge_pull_request,
    load_attempts,
    required_checks,
    stranded,
)
from ..review.orchestrator import run_review
from ..review.refuter import Refutation
from ..review.report import findings_table_lines
from ..review.review_head import HeadUnavailableError, HeadUnresolvedError
from ..review.reviewer import ReviewVerdict
from ..review.spec import canonical_spec_id, spec_exists, specs_dir_for
from ..review.spec_grading import (
    RESOLUTION_OWNERSHIP,
    resolution_report,
)
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


def refuter_tokens_json(
    refutations: Sequence[Refutation], usage: Mapping[str, object]
) -> dict[str, int]:
    """Report what the refuter consumed in the run: evidence estimate and ledger total.

    ``evidence`` sums each finding's ``evidence_tokens`` (the
    ``ceil(utf8_bytes / 3)`` estimate); ``total`` is the ``Refuter`` row
    of the usage ledger (input, output and cache tokens, as the provider
    counts them). Reported only: the evidence window is governed by
    ``[gates].refuter_context_tokens`` alone, never by these figures.
    """
    rows = usage.get("per_reviewer")
    refuter = (
        next(
            (r for r in rows if isinstance(r, Mapping) and r.get("reviewer") == "Refuter"),
            None,
        )
        if isinstance(rows, list)
        else None
    )
    total = refuter.get("total_tokens", 0) if refuter else 0
    return {
        "evidence": sum(r.evidence_tokens for r in refutations),
        "total": int(total) if isinstance(total, int) else 0,
    }


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
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-run the reviewers on a head that already has a completed review run.",
    ),
    dry_run_reason: str = typer.Option(
        WriteRefusal.DRY_RUN.value,
        "--dry-run-reason",
        hidden=True,
        help="Why a --dry-run writes nothing; set by `repoach ci review`.",
    ),
    generated_evidence: str | None = typer.Option(
        None,
        "--generated-evidence",
        help=(
            "The review-generated.json the secret-free `review generated` job wrote "
            "for this head; without it a touched generated artefact is reported not run."
        ),
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

    A finding is recorded once per head (SP-FINDINGS-BRIDGE-DOCFIX 0.2):
    one ``findings: N new, M already recorded, K refuted earlier`` line
    on stderr says how many of the reviewers' findings were new at the
    head, already in the ledger there, or refuted there by an earlier
    run; a row already judged there is not judged again. ``findings_recorded`` in
    the JSON carries the same three counts.

    The unit of a review is the run (SP-FINDINGS-BRIDGE-DOCFIX 0.3): when
    a completed run already exists at the head (every reviewer parsed,
    its findings written), no reviewer and no refuter is called. The
    stored outcomes are replayed, the older-head re-check still runs, the
    JSON has the same shape with ``cached`` true, and one ``review:
    cached at <sha7> (run <id>, <when>); use --force to re-review`` line
    on stderr names the run. The spec coverage is not replayed: it is
    recomputed at the head with the current rule (no model call), its
    row rewritten, and one ``review: spec coverage recomputed at <sha7>:
    planned N, missing M`` line on stderr says it. A dry run and a posting run share that
    cache; a posting run posts the stored outcomes. ``--force`` runs the
    reviewers again.

    The review reads the PR head, never the checkout's branch
    (SP-REVIEW-PINS-HEAD G2): the head SHA comes from the forge, its tree
    is the checkout when that is at the SHA and clean, else a detached
    worktree under the state directory, removed after the run unless
    ``--keep-tree``. One ``reviewing <N> at <sha7> (<tree> <path>)`` line
    on stderr says which; the JSON carries ``head_sha``, ``tree``
    (``worktree`` / ``checkout``) and ``worktree`` (the kept path, else
    null). A head the forge does not serve, or one that cannot be
    fetched, exits 2 before any reviewer runs.

    ``findings`` lists every finding that can move ``final_verdict``,
    each once, with ``judge`` (``mechanical`` / ``refuter`` /
    ``reverify`` / ``ledger``), ``state``, ``verification_method``
    (``mechanical:<claim_type>`` for a mechanical verifier),
    ``evidence`` and ``blocking``: the findings a mechanical verifier
    settled, every finding the refuter saw (its ``refutation = {state,
    reason}`` kept — ``verified`` / ``refuted`` / ``deferred`` /
    ``unverified``), the findings of older heads re-checked at the head,
    and any finding still counted as open blocking.
    ``verdict_basis`` is the ids the verdict counts as open blocking,
    each one entry of ``findings``. One ``refuter: judged …`` line on
    stderr counts the refuter's states and reasons
    (SP-REFUTER-JUDGES-WHAT-IT-CAN), then a ``report: N finding(s) listed`` table
    names each finding's judge and a ``verdict basis:`` line closes it.

    A dry run closes the forge's write barrier (SP-DRY-RUN-NEVER-POSTS):
    ``refused_writes`` counts the writes it refused and ``write_refusal``
    names why it was closed (``dry run``; null when posting). One
    ``repoach: refused_writes: N (<reason>)`` line on stderr says the
    same. A refused write is a defect: one ``repoach: dry run attempted
    a forge write: <head>`` line per write, and the command exits 8.

    A declared generated artefact the diff touches
    (``review.generated`` at the base ref) reaches the reviewers as
    one summary paragraph; its check never runs here: the evidence of
    ``--generated-evidence`` is read, ``generated`` in the JSON lists each
    run (``path``, ``check``, ``state``, ``ms``, ``sources_changed``,
    ``producer_inputs_changed``, ``script_ref``) and one ``generated:
    <path> verified by <check> (<ms> ms)`` line per verified run is
    printed on stderr (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G5).
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
                force=force,
                generated_evidence=_evidence_path(generated_evidence),
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
    spec_resolution = team.spec_resolution
    usage = run_usage_json(run)
    payload = {
        "pr_number": team.pr_number,
        "head_sha": team.head_sha,
        "tree": team.tree,
        "worktree": team.worktree,
        "spec": spec_id,
        "spec_source": team.spec_source,
        "spec_coverage": spec_coverage_label(
            spec_id or _owned_specs_label(spec_resolution),
            team.spec_covered,
            missing=team.spec_missing,
        ),
        "spec_resolution": spec_resolution,
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
        "findings_recorded": team.findings_recorded,
        "cached": team.cached,
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
        "findings": [entry.as_json() for entry in team.findings],
        "verdict_basis": list(team.verdict_basis),
        "generated": team.generated,
        "usage": usage,
        "refuter_tokens": refuter_tokens_json(team.refutations, usage),
    }
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    if team.cache_line:
        typer.echo(team.cache_line, err=True)
    if team.spec_coverage_line:
        typer.echo(team.spec_coverage_line, err=True)
    for line in team.spec_report:
        typer.echo(line, err=True)
    if team.findings_line:
        typer.echo(team.findings_line, err=True)
    if team.refuter_summary is not None:
        typer.echo(team.refuter_summary, err=True)
    for line in findings_table_lines(team.findings, team.verdict_basis):
        typer.echo(line, err=True)
    if team.reverify_line:
        typer.echo(team.reverify_line, err=True)
    for line in team.generated_lines:
        typer.echo(line, err=True)
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


def _evidence_path(value: object) -> Path | None:
    """Return the ``--generated-evidence`` path; ``None`` when the option was not given.

    A direct call of :func:`review_pr` leaves the option at its Typer
    default object, which names no file.
    """
    return Path(value) if isinstance(value, str) and value else None


@review_app.command("generated")
def review_generated(
    pr_number: int = typer.Option(..., "--pr", help="PR number (GitHub) or MR iid (GitLab)."),
    head: str = typer.Option(..., "--head", help="The head SHA the review will run at."),
    out: str = typer.Option(..., "--out", help="Where to write review-generated.json."),
    base: str | None = typer.Option(
        None,
        "--base",
        help="The base ref the declarations are read at (default: origin/<integration_branch>).",
    ),
) -> None:
    """Run the checks of the declared generated artefacts a PR touches (secret-free job).

    The verification half of SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ: the
    ``review.generated`` declarations of the base ref are read, each
    check whose artefact the PR changes runs once against the head tree
    (twice, base script then head script, when the check script itself
    changed), never through a shell and with every secret-bearing
    variable removed from its environment. The evidence
    (``format: review-generated-json``, bound to the head SHA and each
    artefact's sha256) goes to ``--out`` for ``review pr
    --generated-evidence``. One ``generated: …`` line per run and one
    ``generated checks: <n> ms + <m> ms overhead`` line go to stderr.

    Run it only in a job holding no forge token and no model credential:
    the check is pull-request code. Exits 0 once the evidence is written,
    whatever the checks said (the review decides); 2 when the head or the
    base does not resolve or the base declarations are invalid.
    """
    root = project_root_or_exit()
    base_ref = base or f"origin/{load_project_config(root).repoach.integration_branch}"
    state_dir = Path(get_settings().db_path).parent
    try:
        payload, checks, timing = verify_generated(
            root, pr_number=pr_number, head=head, base_ref=base_ref, state_dir=state_dir
        )
    except (GeneratedRefusalError, ConfigError) as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for line in report_lines(checks, timing if checks else None):
        typer.echo(line, err=True)
    if not checks:
        typer.echo("generated: no declared generated artefact touched", err=True)


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


@review_app.command("merge-attempts")
def review_merge_attempts(
    pr_number: int = typer.Argument(..., help="PR number (GitHub) or MR iid (GitLab)."),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
) -> None:
    """Print every recorded merge attempt of a PR (SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G5).

    Reads the PR's ``<!-- repoach-merge-attempts:v1 -->`` comment, else the
    local ``pr_merges`` table. Stdout is one ``format:merge-attempts-json``
    object; stderr is the line
    ``merge attempts: <n>, last: <OUTCOME> on <check|reason> at <t> (<trigger>)``.
    Read-only; exits 0, or 2 when the forge cannot be selected.
    """
    project_root_or_exit()
    forge = _forge_kwargs(forge_name).get("forge")
    if forge is None:
        try:
            forge = select_forge()
        except ForgeError as exc:
            raise _forge_failure(exc) from exc
    attempts = load_attempts(forge, Path(get_settings().db_path), pr_number)
    report = attempts_report(pr_number, attempts)
    typer.echo(attempts_line(attempts), err=True)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))


@review_app.command("stranded")
def review_stranded(
    base: str | None = typer.Option(
        None, "--base", help="Only PRs targeting this branch (default: the integration branch)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object on stdout."),
    dispatch: bool = typer.Option(
        False, "--dispatch", help="Dispatch merge-on-ci.yml once for each stranded PR."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="With --dispatch, name what would be dispatched; write nothing."
    ),
    forge_name: str | None = typer.Option(None, "--forge", help=_FORGE_HELP),
) -> None:
    """List the open PRs that are green at their head with no merge since.

    SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G3: a PR is stranded when its
    required checks have been green for ``stranded_grace_s`` and its last
    attempt at that head is absent, older, or a skip. Read-only unless
    ``--dispatch``, which starts ``merge-on-ci.yml -f pr=<n>`` once per
    stranded PR through the write barrier and prints ``dispatched #<n>
    (<reason>)`` on stderr (D3, the release sweep). ``--dry-run`` writes
    nothing. PRs whose required checks are cancelled-only or missing are
    listed under ``unjudged``. Exits 0, or 2 when the forge cannot be
    selected or read.
    """
    root = project_root_or_exit()
    forge = _forge_kwargs(forge_name).get("forge")
    try:
        forge = forge or select_forge(cwd=root)
        target = base or get_settings().integration_branch
        prs = forge.open_pull_requests(target)
        names = required_checks(root)
        producers = check_producers(root)
        verdicts = {pr.number: judge_pull_request(forge, pr, names, producers) for pr in prs}
    except ForgeError as exc:
        raise _forge_failure(exc) from exc
    db = Path(get_settings().db_path)
    attempts = {pr.number: load_attempts(forge, db, pr.number) for pr in prs}
    rows = stranded(prs, verdicts, attempts, datetime.now(UTC))
    dispatched: list[int] = []
    if dispatch:
        dispatched = dispatch_stranded(
            forge, rows, dry_run=dry_run, echo=lambda line: typer.echo(line, err=True)
        )
    unjudged = [
        {"pr": number, "state": v.state, "checks": v.cancelled or v.missing}
        for number, v in verdicts.items()
        if v.state in {"cancelled", "missing"}
    ]
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "base": target,
                    "stranded": [
                        {
                            "pr": row.pr_number,
                            "head_sha": row.head_sha,
                            "gate": "stranded",
                            "reason": row.reason,
                        }
                        for row in rows
                    ],
                    "unjudged": unjudged,
                    "dispatched": dispatched,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    for row in rows:
        typer.echo(f"#{row.pr_number}\t{row.head_sha[:7]}\tstranded\t{row.reason}")
    for entry in unjudged:
        typer.echo(f"#{entry['pr']}\t\t{entry['state']}\t{', '.join(entry['checks'])}")


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


def _branch_spec_at_head(
    declared: SpecResolution, *, branch: str, head_sha: str, root: Path
) -> SpecResolution:
    """Re-resolve a branch-derived spec id against the specs at the PR head.

    The checkout may not carry the PR's spec yet, so the branch parser
    falls back to its short regex (``docs/sp-agent-context-loop`` →
    ``SP-AGENT``). The head's specs and the matched spec's frontmatter
    ``id`` give the id the review graded; the checkout's resolution is
    kept when the head names no spec or cannot be read.
    """
    try:
        at_head = branch_spec_id_at_ref(branch, head_sha, root=root)
    except (SpecRefUnavailableError, MalformedFrontmatterError) as exc:
        typer.echo(f"gate: spec id from the checkout ({exc})", err=True)
        return declared
    if at_head is None:
        return declared
    return SpecResolution(at_head, declared.source)


_log = get_logger(__name__)


def _owned_specs_label(resolution: dict[str, object]) -> str | None:
    """Return the specs a PR was graded against by ownership, joined; None otherwise."""
    specs = resolution.get("specs")
    if resolution.get("source") != RESOLUTION_OWNERSHIP or not isinstance(specs, list) or not specs:
        return None
    return ", ".join(str(spec) for spec in specs)


def _gate_spec_resolution(
    declared: SpecResolution,
    *,
    changed_files: list[str] | None,
    base_ref: str,
    root: Path,
) -> tuple[dict[str, object], list[str]]:
    """Return the gate's ``spec_resolution`` object and its stderr lines (G7).

    :func:`~repoach.review.spec_grading.resolution_report` decides it; the
    CLI only passes the declared spec and the checkout. A declared spec is
    graded alone; with none declared, a diff that cannot be read leaves the
    source ``none`` (skipped, named), a base that cannot be read names the
    fail-closed refusal, and otherwise the specs owning the diff resolve.
    """
    return resolution_report(
        declared.spec_id, changed_files=changed_files, root=root, base_ref=base_ref or None
    )


def _gate_changed_files(evaluation: GateEvaluation) -> list[str] | None:
    """Return the paths of the gated PR's diff, or None when they cannot be read."""
    if evaluation.changed_files is not None:
        return evaluation.changed_files
    if evaluation.read_changed_files is None:
        return None
    try:
        return evaluation.read_changed_files()
    except Exception as exc:
        _log.warning(
            "gate.diff_unread", error=f"{type(exc).__name__}: {redact_error(str(exc))[:200]}"
        )
        return None


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
    a spec that has no document. ``spec_acceptance`` lists the selectors
    ``missing`` at head, the ``planned`` ones of a draft spec (absent
    files that do not refuse) and the ``draft_refusals`` that withheld
    that exemption (SP-PLAN-SELECTOR-AUDIT-WIRE 0.2). A coverage row at
    head graded by a retired rule (``rule_version`` below the current
    one) is recomputed on the head tree and rewritten before the
    decision, so it can never refuse on its own (0.3).

    A spec without a plan is graded on its Acceptance Criteria, whatever
    its status, and ``plan-absent: graded against the acceptance criteria
    of <id> (<n> selectors)`` is reported on stderr and in
    ``spec_acceptance.report``; ``spec_acceptance.contract`` names what
    was graded. Every coverage refusal names its selectors and their
    origin. A PR declaring no spec is graded against every spec owning a
    path of its diff at the base; ``spec_resolution`` says how
    (``declared`` / ``ownership`` / ``none``) and lists the ``ungoverned``
    paths, which never refuse (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS).

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
    if declared.source == "branch" and pr is not None and evaluation.head_sha:
        declared = _branch_spec_at_head(
            declared, branch=pr.head_ref, head_sha=evaluation.head_sha, root=root
        )
    resolution, resolution_lines = _gate_spec_resolution(
        declared,
        changed_files=_gate_changed_files(evaluation) if declared.spec_id is None else [],
        base_ref=pr.base_ref if pr is not None else "",
        root=root,
    )
    coverage = spec_coverage_label(
        declared.spec_id or _owned_specs_label(resolution),
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
                "spec_resolution": resolution,
                "merge": evaluation.decision.merge,
                "reasons": evaluation.decision.reasons,
                "reverified": facts.reverified,
                "spec_acceptance": {
                    "missing": facts.spec_missing,
                    "planned": facts.spec_planned,
                    "draft_refusals": facts.spec_draft_refusals,
                    "contract": facts.spec_contract,
                    "report": facts.spec_report,
                },
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
    for line in (*resolution_lines, *facts.spec_report):
        typer.echo(line, err=True)
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
