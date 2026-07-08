"""Pure evidence-first release gate (SP-RELEASE-GATE).

Mirrors the per-PR pure merge gate (:mod:`ferova.review.merge_gate`) for
the operator-only ``develop -> main`` release: a pure commit-subject
classifier decides release-range provenance, a pure decision function
refuses on any red fact, and the gate itself only ever prints facts and
a decision. It never shells out to ``gh pr merge`` -- the operator alone
holds the authority to actually merge ``main``.

This module is the leaf slice of the design (step 1 of 5): the pure
classifier and decision core. Fact-gathering (shelling out to
``git``/``gh``/``scripts/ci_local.sh``) and the receipt round-trip for
``ferova release verify`` land in later steps.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_SQUASH_SUBJECT_RE = re.compile(r"\(#\d+\)$")
"""Matches GitHub's default squash-merge subject suffix, e.g. ``(#42)``."""


def classify_release_range(subjects: list[str]) -> list[str]:
    """Return the commit subjects that are NOT gated-PR squashes.

    A clean release range has every commit in ``main..develop`` as the
    squash of a gated PR, recognisable by GitHub's default squash title
    suffix ``(#N)``. Any subject without that suffix is out-of-band --
    a hotfix or manual commit pushed straight to ``develop`` bypassing a
    PR -- and is returned so the caller can name it in a refusal.

    Args:
        subjects: Commit subjects in ``main..develop``, one per commit.

    Returns:
        The subset of ``subjects`` whose stripped text does not end in
        ``(#N)``. Empty when ``subjects`` is empty or every subject is a
        gated-PR squash.
    """
    return [subject for subject in subjects if not _SQUASH_SUBJECT_RE.search(subject.strip())]


class ReleaseFacts(BaseModel):
    """The facts the pure release gate decides on.

    Attributes:
        develop_sha: The local ``develop`` head the decision is
            computed against.
        out_of_band_commits: Commit subjects in the release range that
            are not gated-PR squashes (see :func:`classify_release_range`).
            Empty means clean provenance.
        remote_sha: The ``origin/develop`` tip (``git ls-remote``).
        pr_head_sha: The release PR's ``headRefOid``, when ``--pr N``
            was given; ``None`` when the gate ran without a PR number.
        ci_green: Whether the local CI mirror (``scripts/ci_local.sh``)
            exited zero at ``develop_sha``.
        ci_checked: Whether CI could be evaluated at all. ``False`` only
            when evaluation itself failed (missing/non-executable
            script, transport error) rather than ran and reported red;
            reserved for the fact-gatherer landing in step 2.
    """

    develop_sha: str
    out_of_band_commits: list[str] = []
    remote_sha: str
    pr_head_sha: str | None = None
    ci_green: bool
    ci_checked: bool = True


class ReleaseDecision(BaseModel):
    """The pure gate's verdict.

    Attributes:
        merge: True when every condition holds and the operator may
            merge ``develop`` into ``main``.
        reasons: One human-readable line per failing condition; empty
            when ``merge`` is True.
    """

    merge: bool
    reasons: list[str]


def compute_release_decision(facts: ReleaseFacts) -> ReleaseDecision:
    """Decide whether the release may merge, purely from *facts*.

    Args:
        facts: The facts gathered for the candidate release.

    Returns:
        A :class:`ReleaseDecision`. ``merge`` is True only when the
        release range is free of out-of-band commits, the local
        ``develop`` head matches the remote tip, the release PR's head
        (when known) matches ``develop_sha``, and the local CI mirror
        is green.
    """
    reasons: list[str] = []
    for subject in facts.out_of_band_commits:
        reasons.append(f"out-of-band commit not a gated-PR squash: {subject}")
    if facts.develop_sha != facts.remote_sha:
        reasons.append(
            f"head-freshness mismatch: local develop {facts.develop_sha} != "
            f"origin/develop {facts.remote_sha}"
        )
    if facts.pr_head_sha is not None and facts.pr_head_sha != facts.develop_sha:
        reasons.append(
            f"release PR stale: headRefOid {facts.pr_head_sha} != develop {facts.develop_sha}"
        )
    if not facts.ci_green:
        reasons.append("CI not green at develop head")
    return ReleaseDecision(merge=not reasons, reasons=reasons)
