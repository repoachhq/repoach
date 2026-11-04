"""The git reads of the generated-artefact flow (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

Shared by the review job (:mod:`repoach.review.generated_artefacts`:
base declarations and blobs) and the verification job
(:mod:`repoach.review.generated_verify`: refs, changed paths, the clone).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .secret_env import scrubbed_env

GIT_ISOLATION: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
)
"""Settings no repository or user config may override: no hook, no fsmonitor, no ext:: remote."""


def _git_env() -> dict[str, str]:
    """The environment for git: no user or system config, never a prompt.

    In the verification job a check is pull-request code run by the job's
    user, so it can write that user's ``~/.gitconfig``; git must not read it.
    Local reads need no secret, so git and any helper it spawns get none
    (:func:`~repoach.review.secret_env.scrubbed_env`).
    """
    env = scrubbed_env()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``root`` and capture its bytes (never raises on its exit).

    The command reads no user or system config and runs no hook or
    fsmonitor (:data:`GIT_ISOLATION`), whatever a check wrote there.

    Args:
        root: The working directory.
        *args: The git arguments.

    Returns:
        The completed process, stdout and stderr as bytes.
    """
    return subprocess.run(
        ["git", *GIT_ISOLATION, *args],
        cwd=root,
        capture_output=True,
        check=False,
        env=_git_env(),
    )


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


GitReader = Callable[[Sequence[str]], tuple[int, str, str]]
"""Runs one git command and returns ``(exit code, stdout, stderr)``, never raising on its exit."""

BASE_REF_UNRESOLVABLE = "base_ref_unresolvable"
"""The ``error`` value a command prints in its JSON when its base does not resolve."""

EXIT_BASE_UNRESOLVABLE = 10
"""The exit code of every command refused by :class:`BaseRefUnresolvableError`, before any work."""

FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A full object name, SHA-1 or SHA-256: the only form a resolved base takes."""

SHALLOW_HINT = (
    "the clone is shallow and its depth excludes the merge base: "
    "clone with GIT_DEPTH 0 (or git fetch --unshallow)"
)
"""The hint for a shallow clone whose depth stops before the merge base."""

CAUSE_ABSENT = "absent from the clone"
"""The cause of a ref git does not know: the one a fetch of `origin/<branch>` can cure."""

_REMOTE_PREFIX = "origin/"


class BaseRefUnresolvableError(Exception):
    """The base ref of a review, a gate or a release names no commit with a merge base.

    Raised before any review work (Yoob B-221): a base that does not
    resolve is never replaced by its own name or by the empty string.

    Attributes:
        ref: The ref as the command received it (``""`` when none was given).
        hint: The one action that makes the ref resolve.
        cause: Why it does not resolve.
    """

    def __init__(self, ref: str, hint: str, *, cause: str) -> None:
        self.ref = ref
        self.hint = hint
        self.cause = cause
        super().__init__(f"base ref {ref!r} does not resolve: {cause}; {hint}")

    def as_json(self) -> dict[str, str]:
        """Return the JSON a command prints for this refusal."""
        return {"error": BASE_REF_UNRESOLVABLE, "ref": self.ref, "hint": self.hint}


def local_git(root: Path) -> GitReader:
    """Return the :data:`GitReader` that runs git in ``root`` through :func:`run_git`."""

    def read(args: Sequence[str]) -> tuple[int, str, str]:
        """Run one git command in ``root``."""
        done = run_git(root, *args)
        return done.returncode, done.stdout.decode(), done.stderr.decode()

    return read


def _branch_of(ref: str) -> str:
    """Return the branch a remote-tracking ref names (``origin/develop`` gives ``develop``)."""
    return ref[len(_REMOTE_PREFIX) :] if ref.startswith(_REMOTE_PREFIX) else ref


def fetch_hint(ref: str) -> str:
    """Return the hint for a ref absent from the clone: the fetch that brings it."""
    return f"fetch it first: git fetch origin {_branch_of(ref) or '<branch>'}"


def resolve_base_ref(root: Path, ref: str, *, git: GitReader | None = None) -> str:
    """Return the full SHA of the commit ``ref`` names, or raise :class:`BaseRefUnresolvableError`.

    The one base resolution of ``review pr``, ``review gate``, ``review
    generated``, ``release gate`` and the Coder loop: an empty ref, a ref
    git would read as an option, a ref absent from the clone and an
    ambiguous ref (a branch and a tag of one name) are each refused by
    name, so neither the empty string nor the ref's own name ever stands
    in for a commit.

    Args:
        root: Any checkout of the repository.
        ref: The base revision (``develop``, ``origin/develop``, a SHA).
        git: The git seam; :func:`local_git` of ``root`` by default.

    Returns:
        The full commit SHA.

    Raises:
        BaseRefUnresolvableError: The ref is empty, option-like, absent or ambiguous.
    """
    if not ref.strip():
        raise BaseRefUnresolvableError(
            ref, "pass the base branch (--base or integration_branch)", cause="no base ref given"
        )
    if _option_like(ref):
        raise BaseRefUnresolvableError(
            ref, "pass a branch name or a SHA", cause="it starts with '-', read by git as an option"
        )
    read = git or local_git(root)
    code, out, err = read(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if code != 0:
        raise BaseRefUnresolvableError(ref, fetch_hint(ref), cause=CAUSE_ABSENT)
    if "ambiguous" in err:
        branch = _branch_of(ref)
        raise BaseRefUnresolvableError(
            ref,
            f"name it in full: refs/remotes/origin/{branch} or refs/heads/{branch}",
            cause="ambiguous, more than one ref has that name",
        )
    sha = out.strip()
    if not FULL_SHA.fullmatch(sha):
        raise BaseRefUnresolvableError(ref, fetch_hint(ref), cause=f"git named no commit ({sha!r})")
    return sha


def require_merge_base(root: Path, ref: str, head: str, *, git: GitReader | None = None) -> str:
    """Return the merge base of ``ref`` and ``head``, or raise :class:`BaseRefUnresolvableError`.

    ``ref`` is resolved first (:func:`resolve_base_ref`); a merge base git
    cannot find is then named by its cause: a shallow clone whose depth
    excludes it, or two histories without a common ancestor.

    Args:
        root: Any checkout of the repository.
        ref: The base revision.
        head: The head revision.
        git: The git seam; :func:`local_git` of ``root`` by default.

    Returns:
        The full merge-base SHA.

    Raises:
        BaseRefUnresolvableError: The base does not resolve, or has no merge base with ``head``.
    """
    read = git or local_git(root)
    base_sha = resolve_base_ref(root, ref, git=read)
    if _option_like(head):
        raise BaseRefUnresolvableError(
            ref, "pass the head as a SHA", cause=f"head {head!r} starts with '-'"
        )
    code, out, _err = read(["merge-base", base_sha, head])
    found = out.strip()
    if code == 0 and FULL_SHA.fullmatch(found):
        return found
    _shallow_code, shallow, _shallow_err = read(["rev-parse", "--is-shallow-repository"])
    if shallow.strip() == "true":
        raise BaseRefUnresolvableError(ref, SHALLOW_HINT, cause=f"no merge base with {head[:12]}")
    raise BaseRefUnresolvableError(
        ref,
        f"{fetch_hint(ref)}, and check that the head descends from it",
        cause=f"no common ancestor with {head[:12]}",
    )


class _GitRun(Protocol):
    """What a forge's ``git`` returns: the fields :func:`reader_of` reads."""

    returncode: int
    stdout: str
    stderr: str


def reader_of(git: Callable[[list[str]], _GitRun]) -> GitReader:
    """Return the :data:`GitReader` over a forge's ``git`` (``Forge.git``).

    Args:
        git: Runs one git command in the forge's clone.

    Returns:
        The reader, so the release gate resolves its base through its forge.
    """

    def read(args: Sequence[str]) -> tuple[int, str, str]:
        """Run one git command through ``git``."""
        done = git(list(args))
        return done.returncode, done.stdout, done.stderr

    return read


class _HasGit(Protocol):
    """What :func:`origin_fetcher` needs of a forge: its ``git``."""

    def git(self, args: list[str]) -> object:
        """Run one git command in the forge's clone."""
        ...


def origin_fetcher(forge: _HasGit) -> Callable[[str], None]:
    """Return the fetch of ``origin/<branch>`` through ``forge.git``.

    ``forge.git`` is looked up when the fetch runs, not before: a base
    that resolves never touches the forge.

    Args:
        forge: The forge whose clone receives the fetch; its result is not read.

    Returns:
        A callable fetching ``refs/heads/<branch>`` into ``refs/remotes/origin/<branch>``.
    """

    def fetch_base(branch: str) -> None:
        """Fetch ``branch`` from origin into its remote-tracking ref."""
        forge.git(
            ["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]
        )

    return fetch_base


def require_pr_base(
    root: Path,
    base_ref: str,
    head: str,
    *,
    fetch_base: Callable[[str], None] | None = None,
    git: GitReader | None = None,
) -> str:
    """Return the merge base of a pull request's base branch and ``head``, fetching it once.

    ``base_ref`` is tried as it is; when it is absent from the clone and
    ``fetch_base`` is given, ``origin/<base_ref>`` is fetched and tried. A
    base that is ambiguous, or present with no merge base, is not fetched
    again: the fetch cannot cure it.

    Args:
        root: Any checkout of the repository.
        base_ref: The pull request's base branch (``develop``) or a ref.
        head: The head revision.
        fetch_base: Fetches ``origin/<base_ref>`` into ``root``; ``None`` never fetches.
        git: The git seam; :func:`local_git` of ``root`` by default.

    Returns:
        The full merge-base SHA.

    Raises:
        BaseRefUnresolvableError: Neither ref resolves to a commit with a merge base.
    """
    read = git or local_git(root)
    try:
        return require_merge_base(root, base_ref, head, git=read)
    except BaseRefUnresolvableError as exc:
        remote = f"{_REMOTE_PREFIX}{base_ref}"
        if exc.cause != CAUSE_ABSENT or fetch_base is None or base_ref.startswith(_REMOTE_PREFIX):
            raise
        fetch_base(base_ref)
        return require_merge_base(root, remote, head, git=read)


__all__ = [
    "BASE_REF_UNRESOLVABLE",
    "CAUSE_ABSENT",
    "EXIT_BASE_UNRESOLVABLE",
    "FULL_SHA",
    "SHALLOW_HINT",
    "BaseRefUnresolvableError",
    "GitReader",
    "blob_at",
    "changed_paths",
    "fetch_hint",
    "local_git",
    "merge_base",
    "origin_fetcher",
    "reader_of",
    "require_merge_base",
    "require_pr_base",
    "resolve_base_ref",
    "resolve_commit",
    "run_git",
]
