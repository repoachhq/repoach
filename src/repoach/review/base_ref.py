"""The base resolution every review command runs before any work (Yoob B-221).

On Yoob !324 the review job's clone had depth 20 and the target branch
was not fetched: git found no merge base, the base became its own name
and the review ran against it. Here a base resolves to a full SHA with a
merge base with the head, or is refused by name
(:class:`BaseRefUnresolvableError`, typed by :class:`BaseRefKind`) with
the one action that cures it. ``review pr`` (the orchestrator),
``review gate`` (:mod:`repoach.review.auto_merge`), ``review generated``
(:mod:`repoach.review.generated_verify`), ``review fix``
(:mod:`repoach.review.coder_findings`) and ``release gate``
(:mod:`repoach.review.release_gate`) all go through it; the evidence
check refuses any base that is not a full SHA (:data:`FULL_SHA`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from ..forge import Forge
from .generated_git import run_git

GitReader = Callable[[Sequence[str]], tuple[int, str, str]]
"""Runs one git command and returns ``(exit code, stdout, stderr)``, never raising on its exit."""

BASE_REF_UNRESOLVABLE = "base_ref_unresolvable"
"""The ``error`` value a command prints in its JSON when its base does not resolve."""

FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A full object name, SHA-1 or SHA-256: the only form a resolved base takes."""

SHALLOW_HINT = (
    "the clone is shallow and its depth excludes the merge base: "
    "clone with GIT_DEPTH 0 (GitLab) or fetch-depth: 0 (GitHub Actions), "
    "or run git fetch --unshallow"
)
"""The hint for a shallow clone whose depth stops before the merge base."""


class BaseRefKind(StrEnum):
    """Why a base does not resolve; control flow reads this, never the message."""

    EMPTY = "empty"
    OPTION_LIKE = "option_like"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"
    NOT_A_COMMIT = "not_a_commit"
    SHALLOW = "shallow"
    UNRELATED = "unrelated"
    HEAD_OPTION_LIKE = "head_option_like"


_REMOTE_PREFIX = "origin/"


class BaseRefUnresolvableError(Exception):
    """The base ref of a review, a gate or a release names no commit with a merge base.

    Raised before any review work (Yoob B-221): a base that does not
    resolve is never replaced by its own name or by the empty string.

    Attributes:
        ref: The ref as the command received it (``""`` when none was given).
        hint: The one action that makes the ref resolve.
        kind: Why it does not resolve, typed (:class:`BaseRefKind`).
        cause: Why it does not resolve, in words.
    """

    def __init__(self, ref: str, hint: str, *, kind: BaseRefKind, cause: str) -> None:
        self.ref = ref
        self.hint = hint
        self.kind = kind
        self.cause = cause
        super().__init__(f"base ref {ref!r} does not resolve: {cause}; {hint}")

    def as_json(self) -> dict[str, str]:
        """Return the JSON a command prints for this refusal.

        Returns:
            ``{"error": "base_ref_unresolvable", "ref": ..., "hint": ...}``.
        """
        return {"error": BASE_REF_UNRESOLVABLE, "ref": self.ref, "hint": self.hint}


def local_git(root: Path) -> GitReader:
    """Return the :data:`GitReader` that runs git in ``root`` through :func:`run_git`.

    Args:
        root: The working directory.

    Returns:
        The reader, decoding stdout and stderr.
    """

    def read(args: Sequence[str]) -> tuple[int, str, str]:
        """Run one git command in ``root``."""
        done = run_git(root, *args)
        return done.returncode, done.stdout.decode(), done.stderr.decode()

    return read


_BRANCH_PREFIXES = ("refs/remotes/origin/", "refs/heads/", _REMOTE_PREFIX)


def _branch_of(ref: str) -> str:
    """Return the branch a ref names (``origin/develop`` or ``refs/remotes/origin/develop`` give ``develop``)."""
    for prefix in _BRANCH_PREFIXES:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def fetch_hint(ref: str) -> str:
    """Return the hint for a ref absent from the clone: the fetch that brings it.

    Args:
        ref: The ref, a branch or ``origin/<branch>``.

    Returns:
        ``fetch it first: git fetch origin <branch>``.
    """
    return f"fetch it first: git fetch origin {_branch_of(ref) or '<branch>'}"


def _names_more_than_one_ref(read: GitReader, ref: str) -> bool:
    """Whether ``ref`` is the short name of more than one ref, as git disambiguates it.

    The candidates are git's own (gitrevisions: ``refs/<ref>``,
    ``refs/tags/<ref>``, ``refs/heads/<ref>``, ``refs/remotes/<ref>``,
    ``refs/remotes/<ref>/HEAD``), listed with ``for-each-ref``: the answer
    never depends on the text of a warning, which a localised git
    translates.
    """
    if ref.startswith("refs/") or FULL_SHA.fullmatch(ref):
        return False
    candidates = (
        f"refs/{ref}",
        f"refs/tags/{ref}",
        f"refs/heads/{ref}",
        f"refs/remotes/{ref}",
        f"refs/remotes/{ref}/HEAD",
    )
    _code, out, _err = read(["for-each-ref", "--format=%(refname)", *candidates])
    return len({line.strip() for line in out.splitlines()} & set(candidates)) > 1


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
            ref,
            "pass the base branch (--base or integration_branch)",
            kind=BaseRefKind.EMPTY,
            cause="no base ref given",
        )
    if ref.startswith("-"):
        raise BaseRefUnresolvableError(
            ref,
            "pass a branch name or a SHA",
            kind=BaseRefKind.OPTION_LIKE,
            cause="it starts with '-', read by git as an option",
        )
    read = git or local_git(root)
    code, out, _err = read(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if code != 0:
        raise BaseRefUnresolvableError(
            ref, fetch_hint(ref), kind=BaseRefKind.ABSENT, cause="absent from the clone"
        )
    if _names_more_than_one_ref(read, ref):
        branch = _branch_of(ref)
        raise BaseRefUnresolvableError(
            ref,
            f"name it in full: refs/remotes/origin/{branch} or refs/heads/{branch}",
            kind=BaseRefKind.AMBIGUOUS,
            cause="ambiguous, more than one ref has that name",
        )
    sha = out.strip()
    if not FULL_SHA.fullmatch(sha):
        raise BaseRefUnresolvableError(
            ref,
            fetch_hint(ref),
            kind=BaseRefKind.NOT_A_COMMIT,
            cause=f"git named no commit ({sha!r})",
        )
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
        BaseRefUnresolvableError: The base does not resolve, or has no merge
            base with ``head``. A ``head`` starting with ``-`` is refused too,
            under the base's ``ref`` (kind ``head_option_like``): the
            command cannot read a merge base, whichever ref is at fault.
    """
    read = git or local_git(root)
    base_sha = resolve_base_ref(root, ref, git=read)
    if head.startswith("-"):
        raise BaseRefUnresolvableError(
            ref,
            "pass the head as a SHA",
            kind=BaseRefKind.HEAD_OPTION_LIKE,
            cause=f"head {head!r} starts with '-'",
        )
    code, out, _err = read(["merge-base", base_sha, head])
    found = out.strip()
    if code == 0 and FULL_SHA.fullmatch(found):
        return found
    _shallow_code, shallow, _shallow_err = read(["rev-parse", "--is-shallow-repository"])
    if shallow.strip() == "true":
        raise BaseRefUnresolvableError(
            ref, SHALLOW_HINT, kind=BaseRefKind.SHALLOW, cause=f"no merge base with {head[:12]}"
        )
    raise BaseRefUnresolvableError(
        ref,
        f"{fetch_hint(ref)}, and check that the head descends from it",
        kind=BaseRefKind.UNRELATED,
        cause=f"no common ancestor with {head[:12]}",
    )


def reader_of(forge: Forge) -> GitReader:
    """Return the :data:`GitReader` over a forge's ``git`` (``Forge.git``), the one forge adapter.

    ``forge.git`` is looked up when a command runs, not before: a base
    that resolves locally never touches the forge.

    Args:
        forge: The forge whose clone runs the commands.

    Returns:
        The reader, for :func:`require_merge_base` (the release gate) and
        :func:`origin_fetcher` (the review, the gate, the Coder loop).
    """

    def read(args: Sequence[str]) -> tuple[int, str, str]:
        """Run one git command through ``forge.git``."""
        done = forge.git(list(args))
        return done.returncode, done.stdout, done.stderr

    return read


def origin_fetcher(git: GitReader) -> Callable[[str], None]:
    """Return the fetch of ``origin/<branch>`` through ``git``.

    Args:
        git: The reader of the clone that receives the fetch (:func:`reader_of`
            of a forge); its result is not read.

    Returns:
        A callable fetching ``refs/heads/<branch>`` into ``refs/remotes/origin/<branch>``.
    """

    def fetch_base(branch: str) -> None:
        """Fetch ``branch`` from origin into its remote-tracking ref."""
        git(
            [
                "fetch",
                "--quiet",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/{_REMOTE_PREFIX}{branch}",
            ]
        )

    return fetch_base


class ResolvedBase(NamedTuple):
    """A pull request's base that resolves, as :func:`require_pr_base` found it.

    Attributes:
        ref: The ref that resolved (``develop``, or ``origin/develop`` after the fetch).
        tip: The full SHA of the base tip, where the base declarations are read.
        merge_base: The full SHA of the merge base, where the diff starts.
    """

    ref: str
    tip: str
    merge_base: str


def _is_branch_name(ref: str) -> bool:
    """Whether ``ref`` is a bare branch name (``develop``), as a forge reports a PR's base."""
    return bool(ref.strip()) and not (
        ref.startswith(("-", "refs/", _REMOTE_PREFIX)) or FULL_SHA.fullmatch(ref)
    )


def _names_a_commit(read: GitReader, ref: str) -> bool:
    """Whether the full refname ``ref`` exists and names a commit."""
    code, _out, _err = read(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return code == 0


def require_pr_base(
    root: Path,
    base_ref: str,
    head: str,
    *,
    fetch_base: Callable[[str], None] | None = None,
    git: GitReader | None = None,
) -> ResolvedBase:
    """Resolve a pull request's base branch as a branch, fetching it once, with its merge base.

    A forge names a PR's base by its bare branch name (``develop``). That
    name is read as a branch only, never as git's short name: a tag a PR
    author pushed under the same name would otherwise become the base
    tip, where the ``[[review.generated]]`` declarations are read.
    ``refs/remotes/origin/<base_ref>`` is tried first; when it is absent and
    ``fetch_base`` is given, it is fetched once; ``refs/heads/<base_ref>``
    is the last resort. Any other ref (``origin/develop``, a full refname,
    a SHA) is resolved as it is (:func:`require_merge_base`).

    Args:
        root: Any checkout of the repository.
        base_ref: The pull request's base branch (``develop``) or a ref.
        head: The head revision.
        fetch_base: Fetches ``origin/<base_ref>`` into ``root``; ``None`` never fetches.
        git: The git seam; :func:`local_git` of ``root`` by default.

    Returns:
        The ref that resolved, its tip and the merge base (:class:`ResolvedBase`).

    Raises:
        BaseRefUnresolvableError: No branch of that name names a commit with
            a merge base with ``head`` (kind ``absent`` when none exists).
    """
    read = git or local_git(root)
    if not _is_branch_name(base_ref):
        found = require_merge_base(root, base_ref, head, git=read)
        return ResolvedBase(base_ref, resolve_base_ref(root, base_ref, git=read), found)
    remote = f"refs/remotes/{_REMOTE_PREFIX}{base_ref}"
    if not _names_a_commit(read, remote) and fetch_base is not None:
        fetch_base(base_ref)
    for ref in (remote, f"refs/heads/{base_ref}"):
        if _names_a_commit(read, ref):
            found = require_merge_base(root, ref, head, git=read)
            return ResolvedBase(ref, resolve_base_ref(root, ref, git=read), found)
    raise BaseRefUnresolvableError(
        f"{_REMOTE_PREFIX}{base_ref}",
        fetch_hint(base_ref),
        kind=BaseRefKind.ABSENT,
        cause="absent from the clone",
    )


__all__ = [
    "BASE_REF_UNRESOLVABLE",
    "FULL_SHA",
    "SHALLOW_HINT",
    "BaseRefKind",
    "BaseRefUnresolvableError",
    "GitReader",
    "ResolvedBase",
    "fetch_hint",
    "local_git",
    "origin_fetcher",
    "reader_of",
    "require_merge_base",
    "require_pr_base",
    "resolve_base_ref",
]
