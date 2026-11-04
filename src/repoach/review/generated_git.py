"""The git reads of the generated-artefact flow (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

Shared by the review job (:mod:`repoach.review.generated_artefacts`:
base declarations and blobs) and the verification job
(:mod:`repoach.review.generated_verify`: refs, changed paths, the clone).
Every command goes through the isolated runner of
:mod:`repoach.review.git_run`, re-exported here as :func:`run_git`.
"""

from __future__ import annotations

from pathlib import Path

from .git_run import run_git


def _option_like(*refs: str) -> bool:
    """Whether a revision would reach git as an option (it starts with ``-``)."""
    return any(ref.startswith("-") for ref in refs)


def blob_at(root: Path, ref: str, path: str) -> bytes | None:
    """Return ``path``'s content at ``ref``, ``None`` when it is absent there.

    Args:
        root: Any checkout of the repository.
        ref: The revision.
        path: The repository-relative path.

    Returns:
        The bytes, or ``None`` (also for a ``ref`` starting with ``-``).
    """
    if _option_like(ref):
        return None
    shown = run_git(root, "show", f"{ref}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def merge_base(root: Path, base_ref: str, head: str) -> str | None:
    """Return the merge base of ``base_ref`` and ``head``, ``None`` when git cannot say.

    Args:
        root: Any checkout of the repository.
        base_ref: The base revision.
        head: The head revision.

    Returns:
        The merge-base SHA, or ``None`` (also for a ref starting with ``-``).
    """
    if _option_like(base_ref, head):
        return None
    found = run_git(root, "merge-base", base_ref, head)
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def resolve_commit(root: Path, ref: str) -> str | None:
    """Return the commit ``ref`` names in ``root``, ``None`` when it does not resolve.

    Args:
        root: Any checkout of the repository.
        ref: A revision.

    Returns:
        The commit SHA, or ``None`` (also for a ``ref`` starting with ``-``).
    """
    if _option_like(ref):
        return None
    found = run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return the paths changed between ``base_sha`` and ``head_sha``.

    Args:
        root: Any checkout of the repository.
        base_sha: The merge base.
        head_sha: The head.

    Returns:
        The changed paths, renames listed as deletion plus addition; none
        for a revision starting with ``-``.
    """
    if _option_like(base_sha, head_sha):
        return []
    listed = run_git(root, "diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in listed.stdout.decode().splitlines() if line]


__all__ = ["blob_at", "changed_paths", "merge_base", "resolve_commit", "run_git"]
