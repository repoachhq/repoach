"""How the ``generated_checks`` rows of a head settle findings of older heads (G3, G6).

A ``generated_artefact`` finding, and the oversized finding of a path the
base now declares, is settled from the rows at the head the re-check
decides on, never through the refuter
(SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from .findings import ClaimType, Finding
from .findings_bridge import OVERSIZED_FINDER
from .generated_store import fetch_generated_checks
from .generated_types import GENERATED_FINDER, GeneratedState, by_path, check_cause, path_verified

ReverifyOutcome = tuple[Literal["resolved", "still_open"], str, str]
"""``(state, method, reason)`` of one settled path, the shape ``head_reverify`` records."""


_SETTLEABLE_FINDERS = frozenset({GENERATED_FINDER, OVERSIZED_FINDER})


def settlements_at_head(db_path: Path, pr_number: int, head_sha: str) -> dict[str, ReverifyOutcome]:
    """Return how the ``generated_checks`` rows of ``head_sha`` settle each recorded path (G3, G6).

    ``resolved`` when every run of the path verified the derivation (a
    source changed, exit 0), ``still_open`` with the cause otherwise.

    Args:
        db_path: The ledger database.
        pr_number: The pull request.
        head_sha: The head the re-check decides on.

    Returns:
        ``{path: (state, method, reason)}`` for every path with a row at the head.
    """
    if not head_sha:
        return {}
    checks, _ = fetch_generated_checks(db_path, pr_number, head_sha)
    settled: dict[str, ReverifyOutcome] = {}
    for path, runs in by_path(checks).items():
        if path_verified(runs):
            reason = f"declared generated at {head_sha[:7]}, verified by {' '.join(runs[0].argv)}"
            settled[path] = ("resolved", GENERATED_FINDER, reason)
        else:
            causes = "; ".join(
                check_cause(r) for r in runs if r.state is not GeneratedState.VERIFIED
            )
            settled[path] = ("still_open", GENERATED_FINDER, causes)
    return settled


def settle_at_head(
    finding: Finding, settlements: Mapping[str, ReverifyOutcome]
) -> ReverifyOutcome | None:
    """Settle an older-head finding on a declared path from the head's rows, without a refuter.

    Applies to a ``generated_artefact`` finding and to the oversized
    finding of a path the base now declares; ``None`` for every other
    finding or a path with no row at the head.

    Args:
        finding: An unsettled finding of an older head.
        settlements: The outcomes :func:`settlements_at_head` computed at the head.

    Returns:
        ``(state, method, reason)``, or ``None`` when the finding is not settled here.
    """
    if finding.finder not in _SETTLEABLE_FINDERS:
        return None
    if finding.claim_type not in {ClaimType.GENERATED_ARTEFACT, ClaimType.SPEC_GAP}:
        return None
    return settlements.get(finding.file)


__all__ = ["ReverifyOutcome", "settle_at_head", "settlements_at_head"]
