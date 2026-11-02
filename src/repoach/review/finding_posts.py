"""The ``finding_posts`` table: the forge comment and thread of every posted finding.

SP-FINDING-THREAD-IDS. A finding posted on the forge stays joinable to
the thread it opened: when the review posts a finding's inline comment,
the forge's own identifiers are written here in the same step that
receives them (``provenance = "posted"``). GitHub fills ``comment_id``
(the review comment id), ``thread_id`` (the root comment id, which is
what a reply takes) and ``review_id`` (the batched review); GitLab fills
``comment_id`` (the note id) and ``thread_id`` (the 40-hex discussion
id) and leaves ``review_id`` empty.

Readers resolve a thread through this table first and fall back to the
legacy ``(file, line)`` match only for findings that have no row: the
refuted-finding sentinel (:mod:`repoach.review.thread_context`) and the
owner commands (:mod:`repoach.review.owner_commands`).
:func:`backfill_finding_posts` links findings posted before this table
existed, strictly: one bot root at the finding's exact file and line,
and no other finding claiming it (``backfill_line_tolerance = 0``).

:func:`detect_closed_threads` records when a recorded thread is found
resolved on the forge by the bot or a maintainer, and by whom. It
changes no finding status: a resolved thread is not a settled finding.
Only ``/resolve``, a fix at head or the refuter settle one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from ..forge.protocol import Comment, Forge, ForgeError, ReviewThread, is_bot_login
from .findings import Finding, fetch_findings, init_findings_schema, pr_findings

_log = get_logger(__name__)

_metadata = MetaData()

PROVENANCE_POSTED: Literal["posted"] = "posted"
"""A row written by the posting path from the forge's answer."""

PROVENANCE_BACKFILL: Literal["backfill"] = "backfill"
"""A row linking a finding posted before this table to its thread."""

finding_posts = Table(
    "finding_posts",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("finding_id", Integer, nullable=False, index=True),
    Column("pr_number", Integer, nullable=False, index=True),
    Column("forge", String, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("comment_id", String, nullable=False),
    Column("thread_id", String, nullable=True),
    Column("review_id", String, nullable=True),
    Column("in_reply_to", String, nullable=True),
    Column("provenance", String, nullable=False),
    Column("posted_at", String, nullable=False),
    Column("thread_closed_at_head", String, nullable=True),
    Column("thread_closed_by", Text, nullable=True),
    UniqueConstraint("finding_id", "comment_id", name="uq_finding_posts_comment"),
)
"""One row per forge comment that states a finding (``db:table:finding_posts``)."""


class FindingPostIntegrityError(ValueError):
    """A row would reference a finding id absent from ``pr_findings``."""


class FindingPost(BaseModel):
    """One posted (or backfilled) comment of a finding.

    Attributes:
        id: The row id, ``None`` before it is stored.
        finding_id: The ``pr_findings`` row the comment states.
        pr_number: The pull / merge request.
        forge: ``github`` or ``gitlab``.
        head_sha: The head the comment was anchored on.
        comment_id: GitHub review comment id, GitLab note id.
        thread_id: GitHub root comment id, GitLab discussion id; ``None``
            for a GitLab note the forge refused to position.
        review_id: The GitHub batched review id; ``None`` on GitLab.
        in_reply_to: The root comment id when the post is itself a reply.
        provenance: ``posted`` or ``backfill``.
        posted_at: UTC ISO-8601 time the row was written.
        thread_closed_at_head: The head at which the thread was first
            seen resolved by the bot or a maintainer, else ``None``.
        thread_closed_by: Who resolved it, as the forge reports the login.
    """

    id: int | None = None
    finding_id: int
    pr_number: int
    forge: Literal["github", "gitlab"]
    head_sha: str
    comment_id: str
    thread_id: str | None = None
    review_id: str | None = None
    in_reply_to: str | None = None
    provenance: Literal["posted", "backfill"] = PROVENANCE_POSTED
    posted_at: str = ""
    thread_closed_at_head: str | None = None
    thread_closed_by: str | None = None

    @property
    def root_id(self) -> int | None:
        """The thread root a reply takes: the comment itself, or the one it answers."""
        raw = self.in_reply_to or self.comment_id
        return int(raw) if raw.isdigit() else None


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine on ``db_path``, creating its directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_finding_posts_schema(db_path: Path) -> None:
    """Create ``finding_posts`` if absent and add any column an older copy lacks.

    Idempotent. A column added after the table was first created is
    nullable, so ``ALTER TABLE … ADD COLUMN`` keeps every stored row.

    Args:
        db_path: The ledger database.
    """
    engine = _engine_for(db_path)
    ensure_schema_created(engine, _metadata)
    existing = {column["name"] for column in inspect(engine).get_columns("finding_posts")}
    missing = [column for column in finding_posts.columns if column.name not in existing]
    if not missing:
        return
    with engine.begin() as conn:
        for column in missing:
            conn.execute(text(f"ALTER TABLE finding_posts ADD COLUMN {column.name} TEXT"))


def _row_to_post(row: RowMapping) -> FindingPost:
    """Build a :class:`FindingPost` from a mapping row."""
    return FindingPost.model_validate(dict(row))


def record_finding_post(db_path: Path, post: FindingPost) -> int:
    """Store one post; return its row id (the existing one for a comment already stored).

    Args:
        db_path: The ledger database.
        post: The post to store.

    Returns:
        The row id.

    Raises:
        FindingPostIntegrityError: ``post.finding_id`` is not a finding of
            ``post.pr_number``; nothing is inserted.
    """
    init_findings_schema(db_path)
    init_finding_posts_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        known = conn.execute(
            select(pr_findings.c.id).where(
                pr_findings.c.id == post.finding_id,
                pr_findings.c.pr_number == post.pr_number,
            )
        ).first()
    if known is None:
        _log.error(
            "finding_posts.unknown_finding",
            pr_number=post.pr_number,
            finding_id=post.finding_id,
            comment_id=post.comment_id,
        )
        raise FindingPostIntegrityError(
            f"finding #{post.finding_id} is not a finding of #{post.pr_number}"
        )
    values = post.model_dump(exclude={"id"})
    values["posted_at"] = post.posted_at or datetime.now(UTC).isoformat(timespec="seconds")
    try:
        with engine.begin() as conn:
            inserted = conn.execute(insert(finding_posts).values(**values)).inserted_primary_key
    except IntegrityError:
        with engine.connect() as conn:
            existing = conn.execute(
                select(finding_posts.c.id).where(
                    finding_posts.c.finding_id == post.finding_id,
                    finding_posts.c.comment_id == post.comment_id,
                )
            ).scalar_one()
        return int(existing)
    if inserted is None:
        raise FindingPostIntegrityError(f"no row id returned for comment {post.comment_id}")
    return int(inserted[0])


def fetch_finding_posts(
    db_path: Path, pr_number: int, *, finding_id: int | None = None
) -> list[FindingPost]:
    """Return the pull request's posts, oldest first.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.
        finding_id: Only the posts of this finding, when given.

    Returns:
        The stored rows.
    """
    init_finding_posts_schema(db_path)
    stmt = select(finding_posts).where(finding_posts.c.pr_number == pr_number)
    if finding_id is not None:
        stmt = stmt.where(finding_posts.c.finding_id == finding_id)
    with _engine_for(db_path).connect() as conn:
        rows = conn.execute(stmt.order_by(finding_posts.c.id)).mappings().fetchall()
    return [_row_to_post(row) for row in rows]


def resolve_thread(db_path: Path, finding: Finding) -> FindingPost | None:
    """Return the thread the finding's latest root post opened, if any.

    Args:
        db_path: The ledger database.
        finding: A stored finding.

    Returns:
        Its most recent post that is a thread root, or ``None`` when the
        finding has no recorded thread.
    """
    if finding.id is None:
        return None
    roots = [
        post
        for post in fetch_finding_posts(db_path, finding.pr_number, finding_id=finding.id)
        if post.in_reply_to is None and post.root_id is not None
    ]
    return roots[-1] if roots else None


def thread_id_of(forge_name: str, comment: Comment) -> str | None:
    """The thread handle of a posted comment as its forge names it.

    Args:
        forge_name: ``github`` or ``gitlab``.
        comment: The comment the forge returned.

    Returns:
        GitLab's discussion id; on GitHub the root comment id (a root is
        its own); ``None`` for a GitLab note posted outside a discussion.
    """
    if comment.thread_id:
        return comment.thread_id
    if forge_name == "github":
        return str(comment.in_reply_to or comment.id)
    return None


@dataclass
class ThreadIndex:
    """The pull request's recorded threads, for matching a reply to its finding.

    Attributes:
        posts: Every post of the pull request.
    """

    posts: list[FindingPost] = field(default_factory=list)

    @property
    def posted(self) -> set[int]:
        """The findings that have at least one recorded post."""
        return {post.finding_id for post in self.posts}

    def findings_for(self, root: Comment) -> list[int]:
        """The findings whose recorded thread is the one ``root`` opens.

        Args:
            root: An inline thread's first comment.

        Returns:
            The finding ids, in the order they were posted; empty when no
            post names that root or discussion.
        """
        found: list[int] = []
        for post in self.posts:
            same_root = post.root_id == root.id
            same_thread = bool(root.thread_id) and post.thread_id == root.thread_id
            if (same_root or same_thread) and post.finding_id not in found:
                found.append(post.finding_id)
        return found


def thread_index(db_path: Path, pr_number: int) -> ThreadIndex:
    """Read the pull request's posts once for thread matching.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.

    Returns:
        The index.
    """
    return ThreadIndex(posts=fetch_finding_posts(db_path, pr_number))


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


def mark_thread_closed(db_path: Path, post_id: int, *, head_sha: str, login: str) -> None:
    """Record that a post's thread was found resolved at ``head_sha`` by ``login``.

    Args:
        db_path: The ledger database.
        post_id: The ``finding_posts`` row.
        head_sha: The head the review saw it resolved at.
        login: Who resolved it.
    """
    stmt = (
        update(finding_posts)
        .where(finding_posts.c.id == post_id, finding_posts.c.thread_closed_at_head.is_(None))
        .values(thread_closed_at_head=head_sha, thread_closed_by=login)
    )
    with _engine_for(db_path).begin() as conn:
        conn.execute(stmt)


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
