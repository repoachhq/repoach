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

from ..core.config import get_settings
from ..review.gh_client import GhCli
from ..review.release_gate import (
    ProvenanceSource,
    compute_release_decision,
    gather_release_facts,
    verify_release,
    verify_release_live,
    write_gate_receipt,
)

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
    """Resolve the repository root the same way ``arch_check`` does."""
    return Path(__file__).resolve().parents[3]


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
    keeps sole authority over the actual merge.

    Exit codes:
      * ``0`` -- gate satisfied (``merge`` is True): the release may
        be merged, as a merge commit, by the operator.
      * ``5`` -- gate refused (``merge`` is False): see ``reasons``.
      * ``1`` -- could not evaluate (transport error or missing CI
        script gathering facts).
    """
    gh = GhCli(cwd=_repo_root())
    try:
        facts = gather_release_facts(
            repo_root=_repo_root(),
            gh=gh,
            pr_number=pr_number,
            db_path=Path(get_settings().db_path),
            provenance=provenance,
        )
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False))
        raise typer.Exit(code=1) from exc
    decision = compute_release_decision(facts)
    write_gate_receipt(_RECEIPT_PATH, develop_sha=facts.develop_sha, decision=decision)
    payload = {
        "develop_sha": facts.develop_sha,
        "out_of_band_commits": facts.out_of_band_commits,
        "remote_sha": facts.remote_sha,
        "pr_head_sha": facts.pr_head_sha,
        "ci_green": facts.ci_green,
        "merge": decision.merge,
        "reasons": decision.reasons,
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
    gh = GhCli(cwd=_repo_root())
    try:
        result = verify_release_live(gh=gh) if live else verify_release(_RECEIPT_PATH, gh=gh)
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
