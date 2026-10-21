"""A read-only forge serving one local diff as a pull request (the bench's fixture forge).

A planted-defect case has no PR on any forge: its base tree and patch
live under ``bench/fixtures/``. :class:`LocalDiffForge` serves the
committed diff, the head SHA and an open pull request with no comment,
so :class:`~repoach.review.orchestrator.ReviewTeamOrchestrator` reviews
it exactly as it reviews a forge PR in dry run. Every write raises
:class:`~repoach.forge.protocol.ForgeError`: a bench never posts.
"""

from __future__ import annotations

from pathlib import Path

from ..forge.protocol import (
    BranchProtection,
    CheckRun,
    CheckStatus,
    Comment,
    Diff,
    ForgeError,
    MergeMethod,
    MergeResult,
    PullRequest,
    PullRequestState,
    Review,
    ReviewDraft,
    diff_files,
)
from ..forge.runner import CommandResult, spawn

_GIT_TIMEOUT_S = 60


class LocalDiffForge:
    """Serve ``base..head`` of a local repository as pull request ``number``.

    Args:
        root: The repository holding both commits.
        number: The pull request number the case is reviewed as.
        base_sha: The base commit.
        head_sha: The head commit.
        title: The pull request title.
    """

    name = "local"

    def __init__(
        self, root: Path, *, number: int, base_sha: str, head_sha: str, title: str
    ) -> None:
        self._root = root
        self._number = number
        self._base = base_sha
        self._head = head_sha
        self._title = title

    @property
    def bot_username(self) -> str:
        """Return the empty login: no comment is ever trusted, none exists."""
        return ""

    def available(self) -> bool:
        """Return True: the repository is on disk."""
        return True

    def _check(self, number: int) -> None:
        if number != self._number:
            raise ForgeError(f"local bench forge serves #{self._number}, not #{number}")

    def _refuse(self, what: str) -> ForgeError:
        return ForgeError(f"local bench forge is read-only: {what} refused")

    def pull_request(self, number: int) -> PullRequest:
        """Return the open pull request of the case."""
        self._check(number)
        return PullRequest(
            number=number,
            title=self._title,
            state="open",
            head_sha=self._head,
            head_ref=f"bench/{number}",
            base_ref="base",
        )

    def pr_state(self, number: int) -> PullRequestState:
        """Return ``open``."""
        self._check(number)
        return "open"

    def head_sha(self, number: int) -> str:
        """Return the head commit."""
        self._check(number)
        return self._head

    def diff(self, number: int) -> Diff:
        """Return ``git diff base head`` of the case repository.

        Raises:
            ForgeError: git failed.
        """
        self._check(number)
        result = self.git(["diff", "--no-color", self._base, self._head])
        if not result.ok:
            raise ForgeError(f"local bench forge: git diff failed: {result.stderr.strip()}")
        return Diff(text=result.stdout, files=diff_files(result.stdout))

    def comments(self, number: int) -> list[Comment]:
        """Return no comment."""
        self._check(number)
        return []

    def conversation_comments(self, number: int) -> list[Comment]:
        """Return no comment."""
        self._check(number)
        return []

    def reviews(self, number: int) -> list[Review]:
        """Return no review."""
        self._check(number)
        return []

    def post_comment(
        self,
        number: int,
        body: str,
        *,
        path: str | None = None,
        line: int | None = None,
        commit_sha: str | None = None,
    ) -> Comment:
        """Refuse: a bench never posts."""
        raise self._refuse("post_comment")

    def post_review(self, number: int, review: ReviewDraft) -> Review:
        """Refuse: a bench never posts."""
        raise self._refuse("post_review")

    def reply(self, number: int, comment_id: int, body: str) -> Comment:
        """Refuse: a bench never posts."""
        raise self._refuse("reply")

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        """Return None: there is no comment."""
        self._check(number)
        return None

    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        """Refuse: a bench never posts."""
        raise self._refuse("upsert_marked_comment")

    def ci_status(
        self,
        number: int,
        *,
        head_sha: str | None = None,
        required: list[str] | tuple[str, ...] | None = None,
    ) -> CheckStatus:
        """Return ``missing``: a local diff has no CI."""
        self._check(number)
        return CheckStatus(state="missing")

    def checks(self, number: int) -> list[CheckRun]:
        """Return no check run."""
        self._check(number)
        return []

    def check_log(self, link: str) -> str | None:
        """Return None: there is no log."""
        return None

    def merge(
        self,
        number: int,
        *,
        method: MergeMethod,
        delete_branch: bool,
        head_sha: str | None = None,
    ) -> MergeResult:
        """Refuse: a bench never merges."""
        raise self._refuse("merge")

    def branch_protection(self, branch: str) -> BranchProtection:
        """Return an unreadable protection: there is none."""
        return BranchProtection(readable=False)

    def open_pull_requests(self, base: str | None = None) -> list[PullRequest]:
        """Return the case's pull request."""
        return [self.pull_request(self._number)]

    def merged_shas(self, base: str) -> set[str]:
        """Return no SHA: nothing is merged."""
        return set()

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Refuse: a bench never opens a pull request."""
        raise self._refuse("create_pull_request")

    def is_trusted_author(self, comment: Comment) -> bool:
        """Return False: no comment is trusted."""
        return False

    def fetch_head(self, number: int) -> str:
        """Return the head commit, already present."""
        self._check(number)
        return self._head

    def git(self, args: list[str]) -> CommandResult:
        """Run git in the case repository."""
        return spawn(["git", *args], cwd=self._root, timeout_s=_GIT_TIMEOUT_S)


__all__ = ["LocalDiffForge"]
