"""CLI subcommands for the operator-only ``develop -> main`` release gate.

Exposed as ``repoach release ...`` once the typer subapp is mounted on
the main app in :mod:`repoach.cli.main` (SP-RELEASE-GATE).

Subcommands:

* ``repoach release gate [--pr N]`` -- gather the release-range
  provenance, head-freshness and CI facts, print the pure decision,
  and write a local receipt. Never merges -- the operator alone holds
  the authority to run ``gh pr merge``.
* ``repoach release verify`` -- post-merge check that the live
  ``origin/main`` tip matches the ``develop`` head the gate approved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ..affected import gh as affected_gh
from ..affected import harvest as affected_harvest
from ..affected.misses import DEFAULT_MISSES_PATH
from ..affected.select import DEFAULT_WORKFLOW_PATH
from ..affected.table import DEFAULT_TABLE_PATH
from ..core.config import get_settings
from ..forge import select_forge
from ..project.config import ConfigError, load_project_config
from ..review.base_ref import BaseRefUnresolvableError
from ..review.release_gate import (
    ProvenanceSource,
    compute_release_decision,
    describe_branch_freshness,
    gather_release_facts,
    verify_release,
    verify_release_live,
    write_gate_receipt,
)
from .doctor_cmds import project_root_or_exit
from .refusals import base_ref_refusal

release_app = typer.Typer(
    help="Operator-only develop -> main release gate (read-only, never merges).",
    no_args_is_help=True,
)

_RECEIPT_PATH = Path("tmp/release_gate_receipt.json")

_PROVENANCE_OPTION = typer.Option(
    ProvenanceSource.LEDGER,
    "--provenance",
    help=(
        "Release-range provenance source: 'ledger' (default, the "
        "pr_merges SQLite ledger populated by the factory merge path) "
        "or 'github' (gh pr list --state merged, for the control-tower "
        "manual-merge regime where the ledger stays empty)."
    ),
)
"""Module-level singleton (SP-RELEASE-PROVENANCE-GH-FALLBACK): ruff's B008
flags a ``typer.Option(...)`` call in an enum-typed parameter default
(unlike the builtin-typed ``pr_number`` option above, ``ProvenanceSource``
is not a type ruff recognises as immutable) -- hoisting the call to a
module-level constant is ruff's own documented fix.
"""


def _repo_root() -> Path:
    """Resolve the git toplevel of the working directory, as ``arch_check`` does."""
    return project_root_or_exit()


def affected_misses_report(root: Path) -> dict[str, object]:
    """Harvest the last merged pull requests and replay them, for a report line (D5).

    SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES D5: the release gate reports
    the misses of the path-to-job table, never refuses on them. Nothing
    is written: the committed corpus is rewritten only by ``repoach ci
    affected harvest``.

    Args:
        root: The project root.

    Returns:
        ``line`` (``affected-jobs misses: <new>/<failed jobs> over the
        last <n> merged pull requests``, or ``affected-jobs misses:
        cannot say: <why>``) and the counts, or ``error``.
    """
    window = affected_harvest.HARVEST_WINDOW
    try:
        base = load_project_config(root).repoach.integration_branch
        result = affected_harvest.harvest_and_replay(
            root,
            repo=None,
            merged=window,
            base=base,
            table=DEFAULT_TABLE_PATH,
            workflow=DEFAULT_WORKFLOW_PATH,
            misses=DEFAULT_MISSES_PATH,
            gh=affected_gh.run_gh,
        )
    except (ConfigError, *affected_harvest.HARVEST_REFUSALS) as exc:
        return {"line": f"affected-jobs misses: cannot say: {exc}", "error": str(exc)}
    report = result.report
    return {
        "line": (
            f"affected-jobs misses: {len(report.new_misses)}/{report.failed_jobs} "
            f"over the last {window} merged pull requests"
        ),
        "new_misses": len(report.new_misses),
        "failed_jobs": report.failed_jobs,
        "merged": window,
    }


@release_app.command("gate")
def release_gate(
    pr_number: int | None = typer.Option(
        None, "--pr", help="Release PR number (adds PR head-freshness check)."
    ),
    provenance: ProvenanceSource = _PROVENANCE_OPTION,
) -> None:
    """Evaluate the pure release gate -- read-only, never merges.

    Gathers release-range provenance, head-freshness against
    ``origin/develop`` (and the release PR's ``headRefOid`` when
    ``--pr`` is given), and the local CI mirror outcome, then prints
    the pure decision and writes a local receipt consumed by
    ``repoach release verify``. The gate only ever prints facts and a
    decision -- it never shells out to ``gh pr merge``; the operator
    keeps sole authority over the actual merge. It also prints, on
    stderr and under ``affected_jobs_misses``, the misses of the
    path-to-job table over the last merged pull requests
    (:func:`affected_misses_report`); they never change the verdict.

    Exit codes:
      * ``0`` -- gate satisfied (``merge`` is True): the release may
        be merged, as a merge commit, by the operator.
      * ``5`` -- gate refused (``merge`` is False): see ``reasons``.
      * ``1`` -- could not evaluate (transport error or missing CI
        script gathering facts).
      * ``10`` -- ``origin/<release>`` does not resolve or has no merge
        base with ``origin/<integration>`` (a shallow clone); stdout
        carries the keys ``error`` (``base_ref_unresolvable``), ``ref``
        and ``hint``.
    """
    root = _repo_root()
    try:
        facts = gather_release_facts(
            repo_root=root,
            forge=select_forge(cwd=root),
            pr_number=pr_number,
            db_path=Path(get_settings().db_path),
            provenance=provenance,
        )
    except BaseRefUnresolvableError as exc:
        raise base_ref_refusal(exc, prints_json=True) from exc
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    decision = compute_release_decision(facts)
    for freshness in facts.branch_freshness:
        note = describe_branch_freshness(freshness)
        if note is not None:
            typer.echo(f"release gate: {note} (verdict unaffected)", err=True)
    misses = affected_misses_report(root)
    typer.echo(f"release gate: {misses['line']} (verdict unaffected)", err=True)
    write_gate_receipt(_RECEIPT_PATH, develop_sha=facts.develop_sha, decision=decision)
    payload = {
        "develop_sha": facts.develop_sha,
        "out_of_band_commits": facts.out_of_band_commits,
        "remote_sha": facts.remote_sha,
        "pr_head_sha": facts.pr_head_sha,
        "ci_green": facts.ci_green,
        "branch_freshness": [freshness.model_dump() for freshness in facts.branch_freshness],
        "review_threads": facts.open_threads.as_json() if facts.open_threads else None,
        "merge": decision.merge,
        "reasons": decision.reasons,
        "affected_jobs_misses": misses,
    }
    if decision.merge:
        payload["required_merge_method"] = "Create a merge commit — never squash a release."
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    if not decision.merge:
        raise typer.Exit(code=5)


@release_app.command("verify")
def release_verify(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help=(
                "Skip the local gate receipt; compare against origin/develop's "
                "live tip instead (the mode the push-triggered CI check uses)."
            ),
        ),
    ] = False,
) -> None:
    """Verify the live ``main`` tip matches the release the gate approved.

    Without ``--live``, reads back the receipt written by
    ``repoach release gate`` and compares its recorded ``develop_sha``
    against the live ``origin/main`` tip -- the operator's manual
    post-merge check, unchanged. With ``--live``, skips the receipt
    entirely and compares against ``origin/develop``'s live tip
    instead, so a fresh CI checkout with no receipt file on disk can
    still run the check (the mode the push-triggered workflow uses). A
    squash-merge or a stale merge diverges the two SHAs immediately,
    while the mistake is still one revert away.

    Exit codes:
      * ``0`` -- verified: ``main`` tip matches the approved release.
      * ``5`` -- divergence: see ``detail``.
      * ``1`` -- could not evaluate (missing/unparsable receipt or
        transport error).
    """
    root = _repo_root()
    try:
        forge = select_forge(cwd=root)
        result = (
            verify_release_live(forge=forge) if live else verify_release(_RECEIPT_PATH, forge=forge)
        )
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "verified": result.verified,
                "main_sha": result.main_sha,
                "expected_sha": result.expected_sha,
                "detail": result.detail,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if not result.verified:
        raise typer.Exit(code=5)
