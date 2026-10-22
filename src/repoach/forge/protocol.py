"""The ``Forge`` protocol: every forge operation repoach performs (SP-UNIVERSAL-FORGE).

A forge is the hosting service a change request lives on — GitHub
pull requests or GitLab merge requests. Review, merge and release
callers never speak ``gh`` or ``glab`` directly: they receive a
:class:`Forge` from :func:`repoach.forge.select.select_forge` and read
and write through the normalised models declared here.

Contract shared by every implementation:

* every method returns a model or raises a :class:`ForgeError`
  subclass — there is no silent non-zero result to forget to check;
* identifiers are the forge's own integers (GitHub PR number / comment
  id, GitLab MR ``iid`` / note id);
* :meth:`Forge.diff` always carries git's ``diff --git a/… b/…``
  headers, whatever the transport, because the diff scoper and the
  findings bridge split on them;
* trust is keyed on :attr:`Forge.bot_username` and fails closed: an
  empty username never matches, a missing author never matches.

The protocol is wider than the minimal interface sketched in the spec:
the extra members (:meth:`Forge.head_sha`, :meth:`Forge.conversation_comments`,
:meth:`Forge.checks`, :meth:`Forge.check_log`, :meth:`Forge.fetch_head`,
:meth:`Forge.git`) are the calls today's callers already make, kept
cheap and explicit instead of being folded into a heavier read.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..core.logging import get_logger
from .runner import CommandResult

_log = get_logger(__name__)

ARCHIVE_MARKER = "<!-- repoach-review-archive -->"
"""HTML marker identifying the sticky review-archive comment / note."""

PullRequestState = Literal["open", "merged", "closed", "locked", "unknown"]
MergeRefusal = Literal[
    "pending",
    "not_approved",
    "discussions_not_resolved",
    "requested_changes",
    "conflict",
    "draft",
    "blocked",
    "behind",
    "head_moved",
    "not_open",
    "unrecognized",
    "refused",
]
WRITABLE_STATES: frozenset[str] = frozenset({"open"})
"""States a write (post, merge) may target; ``locked``/``unknown`` refuse (G7)."""

_WriteMethod = TypeVar("_WriteMethod", bound=Callable[..., Any])
CheckState = Literal["success", "failure", "pending", "missing"]


class MergeMethod(StrEnum):
    """How a change request is merged into its base branch."""

    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class ForgeError(Exception):
    """Base class of every error a :class:`Forge` raises.

    Attributes:
        message: The forge's own diagnostic, verbatim (truncated by
            callers when logged).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ForgeUnavailableError(ForgeError):
    """The forge binary or its token is missing.

    Attributes:
        binary: The executable that could not be found (``gh``/``glab``).
    """

    def __init__(self, message: str, *, binary: str) -> None:
        super().__init__(message)
        self.binary = binary


class ForgeNotFoundError(ForgeError):
    """The pull / merge request (or another addressed object) does not exist.

    Attributes:
        number: The PR / MR number that was addressed, when known.
    """

    def __init__(self, message: str, *, number: int | None = None) -> None:
        super().__init__(message)
        self.number = number


class ForgeForbiddenError(ForgeError):
    """The token's scope is insufficient for the call (HTTP 401/403)."""


class ForgeMergeRefusedError(ForgeError):
    """The forge refused the merge (pipeline, conflicts, protection).

    Attributes:
        reason: Why, read from forge facts rather than error text:
            ``pending`` (pipeline not succeeded yet), ``not_approved``,
            ``discussions_not_resolved``, ``conflict``, ``head_moved``
            (the head is not the SHA the gate decided at), ``not_open``
            (merged / closed / locked / unknown state) or ``refused``
            (the forge said no and no fact explains why).
    """

    def __init__(self, message: str, *, reason: MergeRefusal = "refused") -> None:
        super().__init__(message)
        self.reason: MergeRefusal = reason

    @property
    def pending(self) -> bool:
        """True when the refusal is a pipeline that has not succeeded yet."""
        return self.reason == "pending"


class ForgeStateRefusedError(ForgeError):
    """A write targeted a change that is not ``open`` (merged, closed, locked, unknown).

    Attributes:
        number: The PR / MR addressed.
        state: The state the forge reported.
    """

    def __init__(self, message: str, *, number: int, state: str) -> None:
        super().__init__(message)
        self.number = number
        self.state = state


class WriteRefusal(StrEnum):
    """Why a forge handle refuses every write (SP-DRY-RUN-NEVER-POSTS)."""

    DRY_RUN = "dry run"
    POSTING_DISABLED = "posting disabled"
    ADVISORY_POST_OFF = "REPOACH_ADVISORY_POST is not 1"


class ForgeWriteRefusedError(RuntimeError):
    """A write was attempted on a read-only forge handle.

    Attributes:
        head: What was refused: the write method and its target
            (``post_review #7``) or the argv head (``api --method POST
            repos/…``).
        reason: Why the handle is read-only.
    """

    def __init__(self, head: str, reason: WriteRefusal) -> None:
        super().__init__(f"{reason.value} attempted a forge write: {head}")
        self.head = head
        self.reason = reason


class ForgeWriteBypassError(RuntimeError):
    """A mutating argv reached a forge's read path without passing the barrier."""


_API_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_WRITE_VERBS = frozenset(
    {
        "approve",
        "cancel",
        "close",
        "comment",
        "create",
        "delete",
        "edit",
        "lock",
        "merge",
        "note",
        "ready",
        "rebase",
        "reopen",
        "rerun",
        "review",
        "revoke",
        "run",
        "subscribe",
        "unlock",
        "unsubscribe",
        "update",
        "upload",
    }
)


def _api_method(args: Sequence[str]) -> str | None:
    """The explicit HTTP method of a ``gh api`` / ``glab api`` argv, if any."""
    for i, arg in enumerate(args):
        if arg in {"--method", "-X"} and i + 1 < len(args):
            return args[i + 1].upper()
        if arg.startswith("--method="):
            return arg.split("=", 1)[1].upper()
    return None


def is_write_argv(args: Sequence[str]) -> bool:
    """True when a ``gh`` / ``glab`` argv (binary excluded) changes forge state.

    ``api`` writes with any method but ``GET``, and with no method when it
    carries a field or an input (both CLIs then default to ``POST``); any
    other command writes when its verb (``pr comment``, ``mr merge``…) is
    a mutating one.
    """
    if not args:
        return False
    if args[0] == "api":
        method = _api_method(args)
        if method is not None:
            return method != "GET"
        return any(arg in _API_FIELD_FLAGS for arg in args)
    return len(args) > 1 and args[1] in _WRITE_VERBS


def argv_head(args: Sequence[str]) -> str:
    """The loggable head of a write argv: the command and its endpoint, no body."""
    if args and args[0] == "api":
        method = _api_method(args) or "POST"
        endpoint = next((a for a in args[1:] if "/" in a and not a.startswith("-")), "")
        return f"api --method {method} {endpoint}".rstrip()
    return " ".join(args[:3])


class ForgeWriteBarrier:
    """The one gate every forge write passes through (SP-DRY-RUN-NEVER-POSTS).

    A barrier is open (writes go through) or closed with a
    :class:`WriteRefusal`; closing is one-way. A closed barrier refuses
    each write before anything is spawned, records what it refused and
    logs ``forge.write_refused`` once per refusal. Every forge owns one:
    its write methods call :meth:`refuse_if_closed` first, and its argv
    writes go through :meth:`admit`, the only context in which the
    forge's read path accepts a mutating argv (:meth:`require_admitted`).

    Args:
        refusal: Close the barrier from the start with this reason.
    """

    def __init__(self, refusal: WriteRefusal | None = None) -> None:
        self._refusal = refusal
        self._refused: list[str] = []
        self._lock = threading.Lock()
        self._local = threading.local()

    @property
    def read_only(self) -> bool:
        """True once the barrier is closed."""
        return self._refusal is not None

    @property
    def refusal(self) -> WriteRefusal | None:
        """Why the barrier is closed; ``None`` while writes go through."""
        return self._refusal

    @property
    def refused(self) -> list[str]:
        """Every refused write, in order (a copy)."""
        with self._lock:
            return list(self._refused)

    def close(self, refusal: WriteRefusal) -> None:
        """Refuse every write from now on; an already closed barrier keeps its reason."""
        with self._lock:
            if self._refusal is None:
                self._refusal = refusal

    def refuse_if_closed(self, head: str) -> None:
        """Raise :class:`ForgeWriteRefusedError` for ``head`` when the barrier is closed."""
        refusal = self._refusal
        if refusal is None:
            return
        with self._lock:
            self._refused.append(head)
        _log.warning("forge.write_refused", head=head, reason=refusal.value)
        raise ForgeWriteRefusedError(head, refusal)

    @contextmanager
    def admit(self, args: Sequence[str]) -> Iterator[None]:
        """Let one write argv through, or refuse it when the barrier is closed."""
        self.refuse_if_closed(argv_head(args))
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth = depth

    def require_admitted(self, args: Sequence[str]) -> None:
        """Refuse a mutating argv that did not come through :meth:`admit`.

        Raises:
            ForgeWriteBypassError: ``args`` writes and no admission is open
                on this thread — a write path that skipped the barrier.
        """
        if getattr(self._local, "depth", 0) == 0 and is_write_argv(args):
            raise ForgeWriteBypassError(
                f"write argv bypassed the forge write barrier: {argv_head(args)}"
            )


def write_barrier(method: _WriteMethod) -> _WriteMethod:
    """Guard a numbered forge write: the read-only barrier, then the state (G7).

    Every numbered write method of both forges (``post_comment``,
    ``post_review``, ``reply``, ``upsert_marked_comment``, ``merge``) is
    decorated with this one barrier. A closed :class:`ForgeWriteBarrier`
    refuses first, before any read; otherwise :meth:`Forge.pr_state` is
    read for the method's first argument (the PR / MR number) right
    before the write and :class:`ForgeStateRefusedError` is raised for
    any state outside :data:`WRITABLE_STATES`. A failed state read
    propagates — no write happens on an unknown state.
    """

    @functools.wraps(method)
    def guarded(self: Any, number: int, *args: Any, **kwargs: Any) -> Any:
        self.barrier.refuse_if_closed(f"{method.__name__} #{number}")
        state = self.pr_state(number)
        if state not in WRITABLE_STATES:
            raise ForgeStateRefusedError(
                f"refusing {method.__name__} on #{number}: state is {state!r}",
                number=number,
                state=state,
            )
        return method(self, number, *args, **kwargs)

    return cast(_WriteMethod, guarded)


def unnumbered_write(method: _WriteMethod) -> _WriteMethod:
    """Guard a forge write that targets no existing change (``create_pull_request``)."""

    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        self.barrier.refuse_if_closed(method.__name__)
        return method(self, *args, **kwargs)

    return cast(_WriteMethod, guarded)


class _Model(BaseModel):
    """Shared model config: immutable, unknown fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PullRequest(_Model):
    """A pull request (GitHub) or merge request (GitLab), normalised.

    Attributes:
        number: PR number / MR ``iid``.
        title: Title.
        body: Description markdown (``""`` when absent).
        author: Login / username of the author (``""`` when absent).
        state: ``open``, ``merged``, ``closed``, ``locked`` (GitLab, mid
            merge) or ``unknown`` (a value the forge added); only ``open``
            accepts writes (:data:`WRITABLE_STATES`).
        head_sha: Head commit SHA (``""`` when not reported).
        head_ref: Source branch name (kept even after deletion).
        base_ref: Target branch name.
        url: Web URL.
        draft: True for a draft / work-in-progress change.
    """

    number: int
    title: str = ""
    body: str = ""
    author: str = ""
    state: PullRequestState = "open"
    head_sha: str = ""
    head_ref: str = ""
    base_ref: str = ""
    url: str = ""
    draft: bool = False


class Diff(_Model):
    """A change request's unified diff in git's format.

    Attributes:
        text: The full diff, ``diff --git`` headers included.
        files: Post-image paths named by the ``diff --git`` headers, in order.
    """

    text: str
    files: list[str] = Field(default_factory=list)


class Comment(_Model):
    """A comment, review comment or note.

    Attributes:
        id: Forge id of the comment / note.
        author: Login / username (``""`` when the forge reports none).
        body: Markdown body.
        path: File path for an inline comment, else ``None``.
        line: Post-image line for an inline comment, else ``None``.
        created_at: ISO-8601 timestamp as reported (``""`` when absent).
        in_reply_to: Root comment id for a reply, ``None`` for a root.
        thread_id: Forge thread handle (GitLab discussion id), when any.
    """

    id: int
    author: str = ""
    body: str = ""
    path: str | None = None
    line: int | None = None
    created_at: str = ""
    in_reply_to: int | None = None
    thread_id: str | None = None


class InlineCommentDraft(_Model):
    """One inline comment to post as part of a :class:`ReviewDraft`.

    Attributes:
        path: Repository-relative file path.
        line: 1-based post-image line.
        body: Rendered markdown body.
    """

    path: str
    line: int
    body: str


class ReviewDraft(_Model):
    """A review to post: a verdict, its body and its inline comments.

    Attributes:
        verdict: ``APPROVE``, ``REQUEST_CHANGES`` or ``COMMENT``; an
            unknown value is posted as ``COMMENT``.
        body: Review body markdown.
        comments: Inline comments carried by the review.
        commit_sha: The head the inline comments anchor to. ``None``
            submits a bare verdict (no anchors possible).
    """

    verdict: str
    body: str
    comments: list[InlineCommentDraft] = Field(default_factory=list)
    commit_sha: str | None = None


class Review(_Model):
    """A posted review / approval.

    Attributes:
        id: Forge id (GitHub review id; GitLab verdict note id, or 0).
        author: Reviewer login / username.
        state: Forge state (``APPROVED``, ``CHANGES_REQUESTED``,
            ``COMMENTED``…).
        body: Review body.
        comments: Inline comments the review created, with their ids.
    """

    id: int = 0
    author: str = ""
    state: str = ""
    body: str = ""
    comments: list[Comment] = Field(default_factory=list)


class CheckRun(_Model):
    """One CI check (GitHub check run / GitLab pipeline job).

    Attributes:
        name: Check name.
        status: Forge status, upper-cased (``COMPLETED``, ``IN_PROGRESS``…).
        conclusion: Forge conclusion, upper-cased (``SUCCESS``, ``FAILURE``…).
        started_at: ISO-8601 start timestamp (``""`` when absent).
        bucket: Coarse class: ``pass``, ``fail``, ``pending``,
            ``skipping``, ``cancel`` (``""`` when unknown).
        link: Web link to the check's log page.
        workflow: Workflow (GitHub) or stage (GitLab) name.
    """

    name: str
    status: str = ""
    conclusion: str = ""
    started_at: str = ""
    bucket: str = ""
    link: str = ""
    workflow: str = ""


class CheckStatus(_Model):
    """The one CI fact at a head.

    Attributes:
        state: ``success``, ``failure``, ``pending`` or ``missing``.
        checks: Per-check detail the state was derived from.
        failed: Required checks that failed, as ``name=CONCLUSION``.
        pending: Required checks still running.
        missing: Required checks never reported.
        required: The required check names the state was judged against
            (empty on a forge with one pipeline status).
    """

    state: CheckState
    checks: list[CheckRun] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)


class MergeResult(_Model):
    """Outcome of a successful merge.

    Attributes:
        merged: True when the forge reports the change merged.
        sha: The resulting commit SHA, when the forge reports it.
        message: Tail of the forge's own output, for the audit trail.
    """

    merged: bool
    sha: str | None = None
    message: str = ""


class BranchProtection(_Model):
    """What the forge enforces on a branch.

    Attributes:
        required_checks: Check names the forge requires before merge
            (GitLab: ``["pipeline"]`` when a passing pipeline is required).
        allow_force_push: Whether force pushes are allowed.
        readable: False when the settings could not be read (no
            protection, insufficient scope); callers then use their
            configured fallback. Never an exception.
        merge_method: The project's merge method, when the forge has one.
    """

    required_checks: list[str] = Field(default_factory=list)
    allow_force_push: bool = False
    readable: bool = True
    merge_method: str | None = None


@runtime_checkable
class Forge(Protocol):
    """Every forge operation repoach performs.

    Implementations: :class:`repoach.forge.github.GitHubForge` and
    :class:`repoach.forge.gitlab.GitLabForge`. Construct through
    :func:`repoach.forge.select.select_forge`.
    """

    name: str

    @property
    def barrier(self) -> ForgeWriteBarrier:
        """The write barrier every write of this forge passes through."""
        ...

    @property
    def read_only(self) -> bool:
        """True when :attr:`barrier` refuses every write."""
        ...

    @property
    def refused_writes(self) -> list[str]:
        """Every write :attr:`barrier` refused, in order."""
        ...

    @property
    def bot_username(self) -> str:
        """The login whose trust-bearing markers are honoured (may be empty)."""
        ...

    def available(self) -> bool:
        """True when the forge binary is reachable."""
        ...

    def pull_request(self, number: int) -> PullRequest:
        """Read one pull / merge request."""
        ...

    def pr_state(self, number: int) -> PullRequestState:
        """Read only the state — cheap, consulted before any write."""
        ...

    def head_sha(self, number: int) -> str:
        """Read only the head SHA — cheap, polled by the fresh-head guards."""
        ...

    def diff(self, number: int) -> Diff:
        """Read the change's diff, ``diff --git`` headers included."""
        ...

    def comments(self, number: int) -> list[Comment]:
        """Read every inline review comment (thread roots and replies)."""
        ...

    def conversation_comments(self, number: int) -> list[Comment]:
        """Read the non-inline, non-system conversation comments / notes."""
        ...

    def reviews(self, number: int) -> list[Review]:
        """Read the reviews / approvals."""
        ...

    def post_comment(
        self,
        number: int,
        body: str,
        *,
        path: str | None = None,
        line: int | None = None,
        commit_sha: str | None = None,
    ) -> Comment:
        """Post a conversation comment, or an inline one when anchored."""
        ...

    def post_review(self, number: int, review: ReviewDraft) -> Review:
        """Post a verdict with its inline comments."""
        ...

    def reply(self, number: int, comment_id: int, body: str) -> Comment:
        """Reply in the thread rooted at ``comment_id``."""
        ...

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        """Return the trusted conversation comment carrying ``marker``."""
        ...

    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        """Create or update the trusted comment carrying ``marker``."""
        ...

    def ci_status(
        self,
        number: int,
        *,
        head_sha: str | None = None,
        required: list[str] | tuple[str, ...] | None = None,
    ) -> CheckStatus:
        """Read the one CI fact at the change's head."""
        ...

    def checks(self, number: int) -> list[CheckRun]:
        """Read every CI check / job on the change's head."""
        ...

    def check_log(self, link: str) -> str | None:
        """Read a failed check's log; ``None`` when ``link`` names no log."""
        ...

    def merge(
        self,
        number: int,
        *,
        method: MergeMethod,
        delete_branch: bool,
        head_sha: str | None = None,
    ) -> MergeResult:
        """Merge the change with ``method``, only at ``head_sha`` when given."""
        ...

    def branch_protection(self, branch: str) -> BranchProtection:
        """Read what the forge enforces on ``branch``; never raises."""
        ...

    def open_pull_requests(self, base: str | None = None) -> list[PullRequest]:
        """List open changes, optionally only those targeting ``base``."""
        ...

    def merged_shas(self, base: str) -> set[str]:
        """Return the commit SHAs merged changes landed on ``base``."""
        ...

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Open a new change request."""
        ...

    def is_trusted_author(self, comment: Comment) -> bool:
        """True when ``comment`` was authored by :attr:`bot_username`."""
        ...

    def fetch_head(self, number: int) -> str:
        """Fetch the change's head into the local clone; return the ref fetched."""
        ...

    def git(self, args: list[str]) -> CommandResult:
        """Run ``git`` in the forge's local clone."""
        ...


def diff_files(text: str) -> list[str]:
    """Return the post-image paths named by a diff's ``diff --git`` headers.

    Args:
        text: A unified diff in git's format.

    Returns:
        One path per ``diff --git a/<old> b/<new>`` header, in order.
    """
    files: list[str] = []
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        _, _, new = line.partition(" b/")
        if new:
            files.append(new)
    return files


def trusted(bot_username: str, author: str) -> bool:
    """The fail-closed trust rule shared by every forge.

    Args:
        bot_username: The configured bot identity.
        author: The comment's author.

    Returns:
        True only when both are non-empty and equal.
    """
    return bool(bot_username) and bool(author) and author == bot_username
