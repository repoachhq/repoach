"""Link legacy findings to their threads and see recorded threads closed (SP-FINDING-THREAD-IDS).

Both read the forge and write ``finding_posts`` through
:mod:`repoach.review.finding_posts`. :func:`backfill_finding_posts`
links findings posted before that table existed, strictly: one bot root
at the finding's exact file and line, and no other finding claiming it
(``backfill_line_tolerance = 0``). :func:`detect_closed_threads` records
when a recorded thread is found resolved on the forge by the bot or a
maintainer, and by whom. It changes no finding status: a resolved thread
is not a settled finding. Only ``/resolve``, a fix at head or the refuter
settle one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from ..core.logging import get_logger
from ..forge.protocol import Comment, Forge, ForgeError, ReviewThread, is_bot_login
from .finding_posts import (
    PROVENANCE_BACKFILL,
    FindingPost,
    fetch_finding_posts,
    mark_thread_closed,
    record_finding_post,
    thread_id_of,
)
from .findings import fetch_findings, init_findings_schema

_log = get_logger(__name__)


class BackfillReport(BaseModel):
    """What ``review backfill-threads`` found and linked.

    Attributes:
        pr_number: The pull / merge request.
        findings: Findings of the pull request considered.
        linked: Findings linked to a thread by this run.
        unmatched: Findings with no exact, unambiguous bot root.
        already_linked: Findings that already had a post row.
        dry_run: True when nothing was written.
    """

    pr_number: int
    findings: int = 0
    linked: int = 0
    unmatched: int = 0
    already_linked: int = 0
    dry_run: bool = False

    def line(self) -> str:
        """The one line the command prints."""
        return (
            f"pr {self.pr_number}: {self.findings} findings, {self.linked} linked, "
            f"{self.unmatched} unmatched, {self.already_linked} already linked"
        )


def backfill_finding_posts(
    db_path: Path,
    forge: Forge,
    pr_number: int,
    *,
    bot_login: str,
    dry_run: bool = False,
) -> BackfillReport:
    """Link the pull request's unposted findings to the bot's threads, strictly.

    A finding is linked when exactly one bot-authored inline root sits at
    its exact ``(file, line_start)`` and no other unlinked finding claims
    that root. Anything else (a shifted line, two roots, two findings on
    one root, a conversation comment, no file) is ``unmatched`` and gets
    no row: it stays unknown, never an approximated link.

    Args:
        db_path: The ledger database.
        forge: The forge the pull request lives on.
        pr_number: The pull / merge request.
        bot_login: The login the review posts as.
        dry_run: Count only; write nothing.

    Returns:
        The counts.

    Raises:
        ForgeError: The comments could not be read; nothing is written.
    """
    comments = forge.comments(pr_number)
    init_findings_schema(db_path)
    findings = fetch_findings(db_path, pr_number)
    linked_already = {post.finding_id for post in fetch_finding_posts(db_path, pr_number)}
    roots = [
        comment
        for comment in comments
        if comment.in_reply_to is None
        and comment.path
        and comment.line is not None
        and is_bot_login(bot_login, comment.author)
    ]
    report = BackfillReport(pr_number=pr_number, findings=len(findings), dry_run=dry_run)
    candidates: dict[int, list[Comment]] = {}
    for finding in findings:
        if finding.id is None or finding.id in linked_already:
            continue
        candidates[finding.id] = [
            root
            for root in roots
            if finding.file
            and finding.line_start > 0
            and root.path == finding.file
            and root.line == finding.line_start
        ]
    claims: dict[int, int] = {}
    for found in candidates.values():
        if len(found) == 1:
            claims[found[0].id] = claims.get(found[0].id, 0) + 1
    for finding in findings:
        if finding.id in linked_already:
            report.already_linked += 1
            continue
        found = candidates.get(finding.id or 0, [])
        if len(found) != 1 or claims.get(found[0].id) != 1:
            report.unmatched += 1
            continue
        report.linked += 1
        if dry_run:
            continue
        root = found[0]
        record_finding_post(
            db_path,
            FindingPost(
                finding_id=finding.id or 0,
                pr_number=pr_number,
                forge="gitlab" if forge.name == "gitlab" else "github",
                head_sha=finding.head_sha,
                comment_id=str(root.id),
                thread_id=thread_id_of(forge.name, root),
                provenance=PROVENANCE_BACKFILL,
            ),
        )
    return report


class ThreadClosure(BaseModel):
    """A recorded thread found resolved on the forge.

    Attributes:
        finding_id: The finding the thread states.
        login: Who resolved it.
        head_sha: The head the review saw it resolved at.
        comment_id: The thread root.
        counted: True when the resolver is the bot or a maintainer (the
            closure is recorded); False when it is someone else (reported
            only).
    """

    finding_id: int
    login: str
    head_sha: str
    comment_id: str
    counted: bool

    def line(self) -> str:
        """The report line naming the closure."""
        if self.counted:
            return f"thread closed by {self.login} for #{self.finding_id}"
        return (
            f"thread resolved by {self.login} for #{self.finding_id}: not counted, "
            "neither the bot nor a maintainer"
        )


@dataclass
class ClosureRun:
    """What :func:`detect_closed_threads` saw.

    Attributes:
        closures: The resolved threads, counted or not, one per finding and login.
        error: Why the threads could not be read, else ``None``.
    """

    closures: list[ThreadClosure] = field(default_factory=list)
    error: str | None = None

    def lines(self) -> list[str]:
        """One report line per closure, then the read failure when any."""
        lines = [closure.line() for closure in self.closures]
        if self.error is not None:
            lines.append(f"thread closures unread: {self.error}")
        return lines

    def as_json(self) -> list[dict[str, object]]:
        """The closures as the ``review pr`` JSON carries them."""
        return [closure.model_dump() for closure in self.closures]


def _match(post: FindingPost, threads: Sequence[ReviewThread]) -> ReviewThread | None:
    """The forge thread a post opened: by root comment id, or by discussion id."""
    for thread in threads:
        if thread.root_id == post.root_id:
            return thread
        if post.thread_id and thread.thread_id == post.thread_id:
            return thread
    return None


def _resolver_counts(forge: Forge, login: str, cache: dict[str, bool]) -> bool:
    """Whether ``login`` may close a finding's thread: the bot, or a maintainer.

    Raises:
        ForgeError: The maintainer permission could not be read.
    """
    if is_bot_login(forge.bot_username, login):
        return True
    if login not in cache:
        cache[login] = forge.is_maintainer(login)
    return cache[login]


def detect_closed_threads(
    forge: Forge, db_path: Path, *, pr_number: int, head_sha: str
) -> ClosureRun:
    """Record the recorded threads the forge reports resolved by the bot or a maintainer.

    Reads the forge only when a post has an open record. A thread
    resolved by anyone else is reported and not recorded. No finding
    status moves here.

    Args:
        forge: The forge the pull request lives on.
        db_path: The ledger database.
        pr_number: The pull / merge request.
        head_sha: The reviewed head.

    Returns:
        The closures seen, and the read failure when the forge could not say.
    """
    run = ClosureRun()
    pending = [
        post
        for post in fetch_finding_posts(db_path, pr_number)
        if post.in_reply_to is None and post.thread_closed_at_head is None and post.id is not None
    ]
    if not pending:
        return run
    try:
        threads = forge.review_threads(pr_number)
    except ForgeError as exc:
        _log.warning("finding_posts.threads_unreadable", pr_number=pr_number, error=exc.message)
        run.error = exc.message[:200]
        return run
    cache: dict[str, bool] = {}
    seen: set[tuple[int, str]] = set()
    for post in pending:
        thread = _match(post, threads)
        if thread is None or not thread.resolved or not thread.resolved_by:
            continue
        login = thread.resolved_by
        try:
            counted = _resolver_counts(forge, login, cache)
        except ForgeError as exc:
            _log.warning(
                "finding_posts.resolver_unreadable",
                pr_number=pr_number,
                login=login,
                error=exc.message[:200],
            )
            continue
        if counted:
            mark_thread_closed(db_path, post.id or 0, head_sha=head_sha, login=login)
        if (post.finding_id, login) in seen:
            continue
        seen.add((post.finding_id, login))
        run.closures.append(
            ThreadClosure(
                finding_id=post.finding_id,
                login=login,
                head_sha=head_sha,
                comment_id=post.comment_id,
                counted=counted,
            )
        )
    return run
