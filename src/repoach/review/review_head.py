"""Review the PR head tree, not whatever branch the checkout is on (SP-REVIEW-PINS-HEAD G2).

A review is a statement about one commit. :func:`materialise_head`
gives the reviewers, the verifiers and the refuter that commit's tree:
the current checkout when it already sits at the head SHA with a clean
working tree, otherwise a detached worktree under the state directory
(``<state_dir>/worktrees/pr-<N>-<sha12>``) built from the forge's head
ref (``refs/pull/<N>/head`` on GitHub, ``refs/merge-requests/<N>/head``
on GitLab, :meth:`~repoach.forge.Forge.fetch_head`). The worktree is
removed when the review ends unless the caller keeps it.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.logging import get_logger
from ..forge import Forge, ForgeError
from ..forge.runner import CommandResult, spawn

_log = get_logger(__name__)

_SHA = re.compile(r"[0-9a-f]{40}")
"""A full commit SHA, the only head a review may pin."""

_GIT_TIMEOUT_S = 120
"""Seconds a local ``git`` command may run (a worktree checkout of a large tree)."""

TreeKind = Literal["worktree", "checkout"]
"""Where the reviewed tree lives: a fresh worktree, or the checkout in place."""


class HeadUnresolvedError(RuntimeError):
    """The forge served no head SHA, or one that is not 40 hex characters."""


class HeadUnavailableError(RuntimeError):
    """The head SHA could not be materialised as a tree to review."""


@dataclass(frozen=True)
class ReviewTree:
    """The tree a review reads.

    Attributes:
        sha: The PR head commit, 40 hex characters.
        tree: ``checkout`` when the checkout was already at ``sha`` and
            clean, else ``worktree``.
        root: Directory every evidence and file reader resolves against.
        ref: The forge ref the head was fetched from (``""`` for a checkout).
        kept: Whether the worktree outlives the review.
    """

    sha: str
    tree: TreeKind
    root: Path
    ref: str = ""
    kept: bool = False

    def announcement(self, pr_number: int) -> str:
        """Return the stderr line naming the commit and the tree reviewed."""
        return f"reviewing {pr_number} at {self.sha[:7]} ({self.tree} {self.root})"


def require_head_sha(sha: str | None, pr_number: int) -> str:
    """Return ``sha`` when it is a full commit SHA.

    Raises:
        HeadUnresolvedError: ``sha`` is empty or not 40 hex characters.
    """
    if sha is None or not _SHA.fullmatch(sha):
        raise HeadUnresolvedError(f"cannot resolve head of PR {pr_number}")
    return sha


def worktree_path(state_dir: Path, pr_number: int, sha: str) -> Path:
    """Return where the worktree of ``pr_number`` at ``sha`` lives."""
    return state_dir / "worktrees" / f"pr-{pr_number}-{sha[:12]}"


def _git(repo_root: Path, *args: str) -> CommandResult:
    """Run ``git`` in ``repo_root``; a missing ``git`` reads as a failed call."""
    git = shutil.which("git")
    if git is None:
        return CommandResult(returncode=127, stdout="", stderr="git not installed", argv=list(args))
    return spawn([git, *args], cwd=repo_root, timeout_s=_GIT_TIMEOUT_S)


def checkout_is_at(repo_root: Path, sha: str) -> bool:
    """Return True when ``repo_root`` is at ``sha`` with no change, staged or not, and no untracked file."""
    head = _git(repo_root, "rev-parse", "HEAD")
    if not head.ok or head.stdout.strip() != sha:
        return False
    status = _git(repo_root, "status", "--porcelain")
    return status.ok and not status.stdout.strip()


def _remove_worktree(repo_root: Path, path: Path) -> None:
    """Remove the worktree at ``path``; a failure is a warning, never an error."""
    res = _git(repo_root, "worktree", "remove", "--force", str(path))
    if not res.ok:
        _log.warning(
            "review.worktree_remove_failed",
            path=str(path),
            error=(res.stderr or res.stdout).strip()[:200],
        )


@contextmanager
def materialise_head(
    forge: Forge,
    pr_number: int,
    sha: str,
    *,
    repo_root: Path,
    state_dir: Path,
    keep: bool = False,
) -> Iterator[ReviewTree]:
    """Yield the tree of ``sha`` for the review of ``pr_number``.

    Args:
        forge: Fetches the PR head ref into the checkout's object store.
        pr_number: The PR / MR under review.
        sha: Its head commit (see :func:`require_head_sha`).
        repo_root: The checkout the command runs in.
        state_dir: Parent of the ``worktrees`` directory.
        keep: Leave the worktree in place after the review.

    Yields:
        The :class:`ReviewTree`; its ``root`` is the checkout itself when
        it is at ``sha`` and clean, else a detached worktree at ``sha``.

    Raises:
        HeadUnavailableError: The head ref cannot be fetched, the fetch
            does not bring ``sha``, or the worktree cannot be created.
    """
    if checkout_is_at(repo_root, sha):
        yield ReviewTree(sha=sha, tree="checkout", root=repo_root, kept=True)
        return
    try:
        ref = forge.fetch_head(pr_number)
    except ForgeError as exc:
        raise HeadUnavailableError(
            f"cannot fetch the head of PR {pr_number} from origin: {exc.message}"
        ) from exc
    if not _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").ok:
        raise HeadUnavailableError(f"{ref} from origin does not contain {sha[:12]}")
    path = worktree_path(state_dir, pr_number, sha)
    if path.exists():
        _remove_worktree(repo_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    added = _git(repo_root, "worktree", "add", "--detach", str(path), sha)
    if not added.ok:
        raise HeadUnavailableError(
            f"cannot create a worktree at {sha[:12]}: {(added.stderr or added.stdout).strip()[:200]}"
        )
    _log.info("review.worktree_created", pr_number=pr_number, sha=sha[:12], path=str(path))
    try:
        yield ReviewTree(sha=sha, tree="worktree", root=path, ref=ref, kept=keep)
    finally:
        if not keep:
            _remove_worktree(repo_root, path)
