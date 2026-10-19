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

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .runner import CommandResult

ARCHIVE_MARKER = "<!-- repoach-review-archive -->"
"""HTML marker identifying the sticky review-archive comment / note."""

PullRequestState = Literal["open", "merged", "closed"]
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
        pending: True when the refusal is a pipeline still running — the
            auto-merge maps that to a non-fatal skip, never ``FAILED``.
    """

    def __init__(self, message: str, *, pending: bool = False) -> None:
        super().__init__(message)
        self.pending = pending


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
        state: ``open``, ``merged`` or ``closed``.
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

    def merge(self, number: int, *, method: MergeMethod, delete_branch: bool) -> MergeResult:
        """Merge the change with ``method``."""
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
