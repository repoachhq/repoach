"""Pure evidence-first release gate (SP-RELEASE-GATE).

Mirrors the per-PR pure merge gate (:mod:`repoach.review.merge_gate`) for
the operator-only ``develop -> main`` release: a pure commit-subject
classifier decides release-range provenance, a pure decision function
refuses on any red fact, and the gate itself only ever prints facts and
a decision. It never shells out to the GitHub CLI merge subcommand --
the operator alone holds the authority to actually merge ``main``.

This module is the leaf slice of the design (step 1 of 5): the pure
classifier and decision core. Fact-gathering (shelling out to
``git``/``gh``/``scripts/ci_local.sh``) and the receipt round-trip for
``repoach release verify`` land in later steps.

Code-shape guarantee (not a runtime assertion -- verified by
``test_release_gate_never_calls_merge`` reading this file's source):
this module never invokes a merge or push operation anywhere. It
only ever gathers facts, decides, prints, and round-trips a receipt
between ``repoach release gate`` and ``repoach release verify``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from ..core.config import get_settings
from .gh_client import GhCli
from .persistence import fetch_merged_pr_shas

_SQUASH_SUBJECT_RE = re.compile(r"\(#\d+\)$")
"""Matches GitHub's default squash-merge subject suffix, e.g. ``(#42)``."""

_DEFAULT_CI_SCRIPT = "scripts/ci_local.sh"
"""Repo-relative path to the local CI parity mirror the gate shells out to."""

_PROVENANCE_LEDGER_UNREADABLE = "pr_merges ledger unreadable: {error}"
_PROVENANCE_LEDGER_EMPTY = (
    "pr_merges ledger has no recorded merges but the release range has "
    "{count} commit(s) -- provenance unverifiable"
)
_PROVENANCE_GH_EMPTY = (
    "no PRs merged into {branch} reported by GitHub but the release range has "
    "{count} commit(s) -- provenance unverifiable"
)


class ProvenanceSource(StrEnum):
    """Selects the source of merged-PR SHAs for release-range provenance.

    SP-RELEASE-PROVENANCE-GH-FALLBACK: both members feed the same
    :func:`classify_release_range_against_ledger` and the same
    empty-set fail-closed rule -- only the SHA source differs, so the
    security properties are identical.

    Attributes:
        LEDGER: The ``pr_merges`` SQLite ledger
            (SP-RELEASE-PROVENANCE-LEDGER), populated only by the
            factory merge path (``repoach review merge``). The default.
        GITHUB: Every ``mergeCommitOid`` GitHub reports for PRs merged
            into the integration branch
            (:meth:`~repoach.review.gh_client.GhCli.merged_pr_merge_shas`)
            -- authoritative even when the ledger is empty, e.g. under
            the control-tower manual-merge regime.
    """

    LEDGER = "ledger"
    GITHUB = "github"


def classify_release_range(subjects: list[str]) -> list[str]:
    """Return the commit subjects that are NOT gated-PR squashes.

    Weaker, subject-only heuristic kept as a fallback for callers with
    no access to the ``pr_merges`` ledger (see
    :func:`classify_release_range_against_ledger` for the authoritative
    check SP-RELEASE-PROVENANCE-LEDGER introduced): a subject ending in
    GitHub's default squash title suffix ``(#N)`` is *assumed* to be a
    gated-PR squash. That suffix is plain commit-message text, so a
    hand-crafted commit can forge it -- prefer the ledger-backed
    classifier wherever the ledger is reachable.

    Args:
        subjects: Commit subjects in ``main..develop``, one per commit.

    Returns:
        The subset of ``subjects`` whose stripped text does not end in
        ``(#N)``. Empty when ``subjects`` is empty or every subject is a
        gated-PR squash.
    """
    return [subject for subject in subjects if not _SQUASH_SUBJECT_RE.search(subject.strip())]


def classify_release_range_against_ledger(
    commits: list[tuple[str, str]], merged_shas: set[str]
) -> list[str]:
    """Return commit subjects whose SHA has no ``pr_merges`` merge record.

    The authoritative provenance check (SP-RELEASE-PROVENANCE-LEDGER): a
    commit in ``main..develop`` is a legitimate gated-PR squash only
    when its own SHA is a member of *merged_shas* -- the recorded
    ``pr_merges.merged_sha`` values (see
    :func:`repoach.review.persistence.fetch_merged_pr_shas`). The
    commit subject's ``(#N)`` suffix is corroborating text only, never
    authoritative by itself: a forged ``hotfix ... (#99)`` subject
    whose SHA has no ledger record is out-of-band even though it
    matches :data:`_SQUASH_SUBJECT_RE`, while a legitimate squash whose
    subject was hand-edited to drop the suffix is still accepted
    because its SHA is recorded -- the SHA check is strictly stronger
    than the regex it replaces.

    Args:
        commits: Release-range commits as ``(sha, subject)`` pairs, one
            per commit in ``main..develop``.
        merged_shas: Recorded ``pr_merges.merged_sha`` values for real
            gated-PR merges.

    Returns:
        The subset of commit subjects whose SHA is absent from
        *merged_shas*. Empty when every commit in the range has a
        matching ledger record.
    """
    return [subject for sha, subject in commits if sha not in merged_shas]


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
        provenance_error: Set when the ``pr_merges`` ledger could not be
            read, or was empty, while classifying a non-empty release
            range (SP-RELEASE-PROVENANCE-LEDGER) -- an explicit refusal
            reason rather than a silently-empty ``out_of_band_commits``.
            ``None`` when provenance was verified (or the caller did not
            request ledger verification at all).
    """

    develop_sha: str
    out_of_band_commits: list[str] = []
    remote_sha: str
    pr_head_sha: str | None = None
    ci_green: bool
    ci_checked: bool = True
    provenance_error: str | None = None


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
    if facts.provenance_error is not None:
        reasons.append(f"provenance unverifiable: {facts.provenance_error}")
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
    db_path: Path | None = None,
    provenance: ProvenanceSource = ProvenanceSource.LEDGER,
) -> ReleaseFacts:
    """Gather the facts the pure release gate decides on.

    Args:
        repo_root: Repository root, used to locate the local CI
            parity mirror.
        gh: A :class:`~repoach.review.gh_client.GhCli`-like wrapper
            used for the ``git``/``gh`` invocations.
        pr_number: The release PR number, when known; enables the
            release PR's ``headRefOid`` freshness cross-check.
        ci_runner: Injectable CI runner for tests; defaults to
            :func:`_default_ci_runner`, which shells out to
            :data:`_DEFAULT_CI_SCRIPT`.
        db_path: Review ledger SQLite path. Only consulted when
            *provenance* is :attr:`ProvenanceSource.LEDGER`. When given,
            release-range provenance is verified against the
            ``pr_merges`` ledger
            (:func:`classify_release_range_against_ledger`) -- the
            authoritative SP-RELEASE-PROVENANCE-LEDGER check, fail-closed
            on an unreadable or empty ledger via ``provenance_error``.
            When ``None``, the caller opted out of ledger verification
            and provenance falls back to the weaker subject-only
            :func:`classify_release_range` heuristic.
        provenance: Which source *merged_shas* comes from
            (SP-RELEASE-PROVENANCE-GH-FALLBACK). :attr:`ProvenanceSource.LEDGER`
            (the default) preserves the *db_path*-driven behaviour above
            unchanged. :attr:`ProvenanceSource.GITHUB` ignores *db_path*
            and instead verifies against
            :meth:`~repoach.review.gh_client.GhCli.merged_pr_merge_shas`
            -- every ``mergeCommitOid`` GitHub reports for PRs merged
            into the integration branch -- fail-closed the same way on
            an empty set via ``provenance_error``.

    Returns:
        The assembled :class:`ReleaseFacts`.

    Raises:
        Exception: Any exception raised while running CI (notably a
            :class:`FileNotFoundError` from the default runner when
            the script is missing or non-executable) propagates
            unchanged -- fail-closed: the caller must treat this as
            an evaluation error, never a fact.
    """
    settings = get_settings()
    integration_branch = settings.integration_branch
    release_branch = settings.release_branch
    develop_sha = gh._run_git(["rev-parse", integration_branch]).stdout.strip()
    log_result = gh._run_git(
        ["log", f"{release_branch}..{integration_branch}", "--format=%H%x09%s"]
    )
    commits: list[tuple[str, str]] = []
    for line in log_result.stdout.splitlines():
        if not line.strip():
            continue
        sha, _tab, subject = line.partition("\t")
        commits.append((sha, subject))
    out_of_band_commits: list[str]
    provenance_error: str | None = None
    if provenance is ProvenanceSource.GITHUB:
        merged_shas = gh.merged_pr_merge_shas(base=integration_branch)
        if not merged_shas and commits:
            provenance_error = _PROVENANCE_GH_EMPTY.format(
                branch=integration_branch, count=len(commits)
            )
        out_of_band_commits = (
            []
            if provenance_error is not None
            else classify_release_range_against_ledger(commits, merged_shas)
        )
    elif db_path is None:
        out_of_band_commits = classify_release_range([subject for _sha, subject in commits])
    else:
        try:
            merged_shas = fetch_merged_pr_shas(db_path)
        except Exception as exc:
            merged_shas = set()
            provenance_error = _PROVENANCE_LEDGER_UNREADABLE.format(error=exc)
        if provenance_error is None and not merged_shas and commits:
            provenance_error = _PROVENANCE_LEDGER_EMPTY.format(count=len(commits))
        out_of_band_commits = (
            []
            if provenance_error is not None
            else classify_release_range_against_ledger(commits, merged_shas)
        )
    ls_remote_result = gh._run_git(["ls-remote", "origin", integration_branch])
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
        provenance_error=provenance_error,
    )


class ReleaseVerifyResult(BaseModel):
    """Outcome of a post-merge ``repoach release verify`` run.

    Attributes:
        verified: True when the live ``main`` tip matches the
            ``develop`` head the gate approved in the receipt.
        main_sha: The live ``origin/main`` tip at verify time.
        expected_sha: The ``develop_sha`` recorded in the gate receipt.
        detail: Human-readable explanation of the outcome.
    """

    verified: bool
    main_sha: str
    expected_sha: str
    detail: str


def write_gate_receipt(path: Path, *, develop_sha: str, decision: ReleaseDecision) -> None:
    """Write the gate receipt that ``repoach release verify`` reads back.

    Args:
        path: Destination file for the receipt (parent directories are
            created as needed).
        develop_sha: The ``develop`` head the gate approved.
        decision: The gate's decision, recorded alongside the head so
            a refused gate's receipt is self-explanatory too.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "develop_sha": develop_sha,
                "merge": decision.merge,
                "reasons": decision.reasons,
            },
            indent=2,
        )
    )


def _ls_remote_sha(gh: GhCli, ref: str) -> str:
    """Return the live SHA ``git ls-remote origin <ref>`` reports.

    Args:
        gh: A :class:`~repoach.review.gh_client.GhCli`-like wrapper used
            for the ``git ls-remote`` invocation.
        ref: The remote ref name (e.g. ``"main"`` or ``"develop"``).

    Returns:
        The first token of ``ls-remote``'s first output line, or
        ``""`` when the ref could not be resolved.
    """
    ls_remote_result = gh._run_git(["ls-remote", "origin", ref])
    first_line = ls_remote_result.stdout.splitlines()[0] if ls_remote_result.stdout.strip() else ""
    return first_line.split()[0] if first_line.split() else ""


def _sanctioned_shape_result(
    *, expected_sha: str, main_sha: str, second_parent: str, distance: str
) -> ReleaseVerifyResult:
    """Decide whether ``main``'s tip matches the sanctioned release shape.

    The one place the sanctioned-shape definition lives, shared by
    :func:`verify_release` (receipt-based ``expected_sha``) and
    :func:`verify_release_live` (live ``origin/develop``-tip
    ``expected_sha``): a fast-forward (the expected SHA is the
    ``main`` tip itself) or a merge commit (the expected SHA is the
    ``main`` tip's second parent, with ``main..develop`` distance
    zero -- the shape "Create a merge commit" produces).

    Args:
        expected_sha: The ``develop`` head the shape is checked
            against, whether sourced from a receipt or a live
            ``ls-remote``.
        main_sha: The live ``origin/main`` tip.
        second_parent: ``origin/main``'s second parent (``origin/main^2``).
        distance: ``git rev-list --count origin/main..origin/develop``,
            as a string.

    Returns:
        The assembled :class:`ReleaseVerifyResult`.
    """
    verified = bool(expected_sha) and (
        main_sha == expected_sha or (second_parent == expected_sha and distance == "0")
    )
    detail = (
        "main tip matches the approved develop head"
        if verified
        else "main tip does not match the approved develop head -- squash or stale merge? "
        "revert and re-merge as a merge commit"
    )
    return ReleaseVerifyResult(
        verified=verified, main_sha=main_sha, expected_sha=expected_sha, detail=detail
    )


def verify_release(path: Path, *, gh: GhCli) -> ReleaseVerifyResult:
    """Verify that the live ``main`` tip matches the sanctioned release shape.

    Reads back the receipt written by :func:`write_gate_receipt` and
    checks the live ``origin/main`` tip against the two shapes
    ``release gate`` sanctions for landing the approved ``develop``
    head (see :func:`_sanctioned_shape_result`). A squash-merge or a
    stale merge satisfies neither shape, while the mistake is still
    one revert away.

    Args:
        path: The gate receipt written by :func:`write_gate_receipt`.
        gh: A :class:`~repoach.review.gh_client.GhCli`-like wrapper used
            for the ``git ls-remote``/``rev-parse``/``rev-list``
            invocations.

    Returns:
        The assembled :class:`ReleaseVerifyResult`.

    Raises:
        FileNotFoundError: When the receipt is missing -- an
            evaluation error, mirroring the CI-script fail-closed
            contract of :func:`_default_ci_runner`.
        json.JSONDecodeError: When the receipt is not valid JSON --
            likewise an evaluation error, never a silent pass.
        RuntimeError: When ``git fetch origin main develop`` fails --
            fail-closed, since the subsequent ``origin/main^2``/
            ``origin/main..origin/develop`` reads are only trustworthy
            against freshly-fetched remote-tracking refs.
    """
    settings = get_settings()
    integration_branch = settings.integration_branch
    release_branch = settings.release_branch
    data = json.loads(path.read_text())
    expected_sha = data["develop_sha"]
    fetch_result = gh._run_git(["fetch", "--quiet", "origin", release_branch, integration_branch])
    if not fetch_result.ok:
        raise RuntimeError(
            f"git fetch origin {release_branch} {integration_branch} failed: "
            f"{fetch_result.stderr.strip()}"
        )
    main_sha = _ls_remote_sha(gh, release_branch)
    second_parent = gh._run_git(["rev-parse", f"origin/{release_branch}^2"]).stdout.strip()
    distance = gh._run_git(
        ["rev-list", "--count", f"origin/{release_branch}..origin/{integration_branch}"]
    ).stdout.strip()
    return _sanctioned_shape_result(
        expected_sha=expected_sha,
        main_sha=main_sha,
        second_parent=second_parent,
        distance=distance,
    )


def verify_release_live(*, gh: GhCli) -> ReleaseVerifyResult:
    """Verify main's tip against the sanctioned shape using develop's live tip.

    The receipt-free sibling of :func:`verify_release`: instead of
    reading ``expected_sha`` back from a gate receipt that only ever
    exists on the operator's own machine, it fetches ``origin/develop``
    fresh and uses its live tip as the expected SHA -- so the check can
    run unattended on a fresh CI checkout with no receipt file anywhere
    on disk. Shares the shape comparison itself with
    :func:`verify_release` through :func:`_sanctioned_shape_result`.

    Args:
        gh: A :class:`~repoach.review.gh_client.GhCli`-like wrapper used
            for the ``git ls-remote``/``rev-parse``/``rev-list``
            invocations.

    Returns:
        The assembled :class:`ReleaseVerifyResult`, with
        ``expected_sha`` set to ``origin/develop``'s live tip rather
        than a value read from any receipt file.

    Raises:
        RuntimeError: When ``git fetch origin main develop`` fails --
            fail-closed, mirroring :func:`verify_release`.
    """
    settings = get_settings()
    integration_branch = settings.integration_branch
    release_branch = settings.release_branch
    fetch_result = gh._run_git(["fetch", "--quiet", "origin", release_branch, integration_branch])
    if not fetch_result.ok:
        raise RuntimeError(
            f"git fetch origin {release_branch} {integration_branch} failed: "
            f"{fetch_result.stderr.strip()}"
        )
    expected_sha = _ls_remote_sha(gh, integration_branch)
    main_sha = _ls_remote_sha(gh, release_branch)
    second_parent = gh._run_git(["rev-parse", f"origin/{release_branch}^2"]).stdout.strip()
    distance = gh._run_git(
        ["rev-list", "--count", f"origin/{release_branch}..origin/{integration_branch}"]
    ).stdout.strip()
    return _sanctioned_shape_result(
        expected_sha=expected_sha,
        main_sha=main_sha,
        second_parent=second_parent,
        distance=distance,
    )
