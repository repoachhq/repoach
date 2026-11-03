"""An open review thread blocks the merge (SP-OPEN-THREADS-BLOCK-THE-MERGE).

The owner's rule: while a review thread is open on a pull request,
nothing merges it. The merge gate counts the open threads the forge
reports at decision time (:meth:`~repoach.forge.Forge.unresolved_threads`)
and refuses while one is open, or while the forge cannot say: a read
that fails is a refusal naming why, never a zero.

This module reads the threads, names them and states the two refusal
reasons; :mod:`~repoach.review.merge_gate` decides on the count,
:mod:`~repoach.review.auto_merge` reads them for every merge path that
reaches ``decide_at_head``, ``review stranded --dispatch`` holds a green
pull request with open threads instead of dispatching it, and the
release gate reads the release pull request's own threads.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from ..core.logging import get_logger
from ..core.redaction import redact_error
from ..forge import Forge, ForgeError, ReviewThread

_log = get_logger(__name__)


class PullRequestRow(Protocol):
    """Any row naming a pull request and its head, such as a stranded PR."""

    @property
    def pr_number(self) -> int:
        """The pull request."""
        ...

    @property
    def head_sha(self) -> str:
        """Its head."""
        ...


RowT = TypeVar("RowT", bound=PullRequestRow)

NOT_READ = "not read"
"""Why the threads are unknown when no read was attempted."""


def unresolved_reason(count: int) -> str:
    """The refusal reason of ``count`` open threads: ``<n> unresolved review thread(s)``."""
    return f"{count} unresolved review thread(s)"


def unreadable_reason(why: str) -> str:
    """The refusal reason of a failed read: ``review threads unreadable: <why>``."""
    return f"review threads unreadable: {why or NOT_READ}"


class OpenThreads(BaseModel):
    """What one read of a pull request's open review threads gave.

    Attributes:
        read: True when the forge answered every page.
        threads: The open threads, in the forge's order (empty when not read).
        error: Why the read failed, redacted; empty when ``read``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: bool
    threads: list[ReviewThread] = []
    error: str = ""

    @property
    def count(self) -> int:
        """The number of open threads read."""
        return len(self.threads)

    def refusal(self) -> str | None:
        """The reason this read refuses a merge, ``None`` when it permits one."""
        if not self.read:
            return unreadable_reason(self.error)
        if self.threads:
            return unresolved_reason(self.count)
        return None

    def summary_line(self) -> str:
        """``review threads: <n> unresolved``, or ``review threads: unreadable: <why>``."""
        if not self.read:
            return f"review threads: unreadable: {self.error or NOT_READ}"
        return f"review threads: {self.count} unresolved"

    def thread_lines(self) -> list[str]:
        """One ``open thread <id> at <file>:<line>`` line per open thread."""
        return [f"open thread {thread_label(t)} at {thread_location(t)}" for t in self.threads]

    def as_json(self) -> dict[str, object]:
        """The JSON ``review pr``, ``review gate`` and ``release gate`` carry under ``review_threads``."""
        return {
            "read": self.read,
            "unresolved": self.count if self.read else None,
            "error": self.error,
            "threads": [
                {
                    "root_id": t.root_id,
                    "thread_id": t.thread_id,
                    "path": t.path,
                    "line": t.line,
                }
                for t in self.threads
            ],
        }


def printable(text: str) -> str:
    """``text`` with every non-printable character written as its Python escape.

    A newline becomes backslash and ``n``, the ANSI escape character
    backslash and ``x1b``: the text stays readable on one line.

    A file name and a forge error come from outside: a newline or an ANSI
    code in one must not split or forge a line ``review gate`` prints, nor
    start a ``::`` workflow command when the gate runs in CI.
    """
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def thread_label(thread: ReviewThread) -> str:
    """Name a thread by its root comment id, and its GitLab discussion id when it has one.

    Args:
        thread: An open thread as the forge listed it.

    Returns:
        ``<root id>`` (``?`` when the forge gave none), followed by
        ``(<discussion id>)`` on GitLab, escaped with :func:`printable`.
    """
    root = str(thread.root_id) if thread.root_id else "?"
    return f"{root} ({printable(thread.thread_id)})" if thread.thread_id else root


def thread_location(thread: ReviewThread) -> str:
    """Locate a thread by the file and line of its root comment.

    Args:
        thread: An open thread as the forge listed it.

    Returns:
        ``<file>:<line>``, ``<file>`` without a line, or ``(no file)`` for
        a thread outside the diff; the file name escaped with
        :func:`printable`.
    """
    if not thread.path:
        return "(no file)"
    path = printable(thread.path)
    return f"{path}:{thread.line}" if thread.line is not None else path


def read_open_threads(forge: Forge, pr_number: int) -> OpenThreads:
    """Read the pull request's open review threads; a failure is a named, unread result.

    A :class:`~repoach.forge.ForgeError` is not raised: it is logged
    (``open_threads.unreadable``, warning) and returned as ``read=False``
    with its redacted message, which every gate turns into the refusal
    ``review threads unreadable: <why>``. Any other exception propagates.

    Args:
        forge: The forge the pull request lives on.
        pr_number: The pull / merge request.

    Returns:
        The threads read, or ``read=False`` with the redacted reason.
    """
    try:
        threads = forge.unresolved_threads(pr_number)
    except ForgeError as exc:
        error = printable(redact_error(exc.message)[:200]) or type(exc).__name__
        _log.warning("open_threads.unreadable", pr_number=pr_number, error=error)
        return OpenThreads(read=False, error=error)
    return OpenThreads(read=True, threads=list(threads))


class HeldPR(BaseModel):
    """A green pull request ``review stranded --dispatch`` does not dispatch.

    Attributes:
        pr_number: The pull request.
        head_sha: Its head.
        reason: The refusal its threads give
            (:meth:`OpenThreads.refusal`): a dispatch would only be refused.
    """

    pr_number: int
    head_sha: str
    reason: str


def hold_open_threads(
    rows: Sequence[RowT], read: Callable[[int], OpenThreads]
) -> tuple[list[RowT], list[HeldPR]]:
    """Split stranded rows into those to dispatch and those their threads hold.

    A stranded pull request whose threads are open or unreadable is held:
    dispatching ``merge-on-ci.yml`` for it would end in the same refusal,
    and the resolution of its last thread re-arms that workflow itself.

    Args:
        rows: The stranded pull requests (any row with ``pr_number`` and
            ``head_sha``; this module knows nothing of the sweep).
        read: Reads one pull request's open threads.

    Returns:
        ``(dispatchable, held)``, each in the order of ``rows``.
    """
    dispatchable: list[RowT] = []
    held: list[HeldPR] = []
    for row in rows:
        refusal = read(row.pr_number).refusal()
        if refusal is None:
            dispatchable.append(row)
        else:
            held.append(HeldPR(pr_number=row.pr_number, head_sha=row.head_sha, reason=refusal))
    return dispatchable, held


__all__ = [
    "NOT_READ",
    "HeldPR",
    "OpenThreads",
    "PullRequestRow",
    "hold_open_threads",
    "printable",
    "read_open_threads",
    "thread_label",
    "thread_location",
    "unreadable_reason",
    "unresolved_reason",
]
