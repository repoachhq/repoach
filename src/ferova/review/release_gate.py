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

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from .gh_client import GhCli

_SQUASH_SUBJECT_RE = re.compile(r"\(#\d+\)$")
"""Matches GitHub's default squash-merge subject suffix, e.g. ``(#42)``."""

_DEFAULT_CI_SCRIPT = "scripts/ci_local.sh"
"""Repo-relative path to the local CI parity mirror the gate shells out to."""


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


def _default_ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
    """Run the local CI parity mirror at *repo_root*.

    Args:
        repo_root: Repository root expected to contain
            :data:`_DEFAULT_CI_SCRIPT`.

    Returns:
        The completed process from running the script.

    Raises:
        FileNotFoundError: When the script is missing or not
            executable -- an evaluation error, never a silent pass.
    """
    script = repo_root / _DEFAULT_CI_SCRIPT
    if not script.is_file() or not os.access(script, os.X_OK):
        raise FileNotFoundError(f"{script} missing or not executable")
    return subprocess.run([str(script)], cwd=repo_root, capture_output=True, text=True, check=False)


def gather_release_facts(
    *,
    repo_root: Path,
    gh: GhCli,
    pr_number: int | None = None,
    ci_runner: Callable[[Path], subprocess.CompletedProcess] | None = None,
) -> ReleaseFacts:
    """Gather the facts the pure release gate decides on.

    Args:
        repo_root: Repository root, used to locate the local CI
            parity mirror.
        gh: A :class:`~ferova.review.gh_client.GhCli`-like wrapper
            used for the ``git``/``gh`` invocations.
        pr_number: The release PR number, when known; enables the
            release PR's ``headRefOid`` freshness cross-check.
        ci_runner: Injectable CI runner for tests; defaults to
            :func:`_default_ci_runner`, which shells out to
            :data:`_DEFAULT_CI_SCRIPT`.

    Returns:
        The assembled :class:`ReleaseFacts`.

    Raises:
        Exception: Any exception raised while running CI (notably a
            :class:`FileNotFoundError` from the default runner when
            the script is missing or non-executable) propagates
            unchanged -- fail-closed: the caller must treat this as
            an evaluation error, never a fact.
    """
    develop_sha = gh._run_git(["rev-parse", "develop"]).stdout.strip()
    log_result = gh._run_git(["log", "main..develop", "--format=%s"])
    subjects = [line for line in log_result.stdout.splitlines() if line.strip()]
    out_of_band_commits = classify_release_range(subjects)
    ls_remote_result = gh._run_git(["ls-remote", "origin", "develop"])
    remote_sha = ""
    first_line = ls_remote_result.stdout.splitlines()[0] if ls_remote_result.stdout.strip() else ""
    if ls_remote_result.returncode == 0 and first_line:
        tokens = first_line.split()
        remote_sha = tokens[0] if tokens else ""
    pr_head_sha = gh.pr_head_sha(pr_number) if pr_number is not None else None
    result = (ci_runner or _default_ci_runner)(repo_root)
    ci_green = result.returncode == 0
    return ReleaseFacts(
        develop_sha=develop_sha,
        out_of_band_commits=out_of_band_commits,
        remote_sha=remote_sha,
        pr_head_sha=pr_head_sha,
        ci_green=ci_green,
    )
