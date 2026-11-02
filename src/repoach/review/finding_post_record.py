"""Join the comments a review just posted to their findings and record them (SP-FINDING-THREAD-IDS).

The posting path of :mod:`repoach.review.orchestrator` hands over what
the forge answered; this module finds the finding each comment states
(:func:`posted_finding_id`, by fingerprint at the posted head), pairs a
batched review's created comments with their drafts (:func:`pair_posted`)
and writes the ``finding_posts`` rows (:func:`record_posted_comments`).
It is apart from :mod:`repoach.review.finding_posts` because it reads
the findings bridge, which imports :mod:`repoach.review.thread_context`,
itself a reader of ``finding_posts``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ..core.logging import get_logger
from ..forge.protocol import Comment, InlineCommentDraft
from .finding_posts import (
    FindingPost,
    FindingPostIntegrityError,
    forge_kind,
    record_finding_post,
    thread_id_of,
)
from .findings import Finding, fetch_findings
from .findings_bridge import comment_to_finding, finding_fingerprint
from .reviewer import BotRole, ReviewComment

_log = get_logger(__name__)


def posted_finding_id(
    rows: Sequence[Finding],
    comment: ReviewComment,
    *,
    role: BotRole,
    pr_number: int,
    head_sha: str,
) -> int | None:
    """The ledger row a posted comment states: same head and fingerprint, its reviewer's first.

    Args:
        rows: The pull request's findings at ``head_sha``.
        comment: The posted reviewer comment.
        role: The reviewer that raised it.
        pr_number: The pull request.
        head_sha: The head it was posted at.

    Returns:
        The newest matching finding id, or ``None`` when the comment was
        never recorded (off the diff).
    """
    probe = comment_to_finding(
        comment, role=role, pr_number=pr_number, head_sha=head_sha, round_n=1
    )
    fingerprint = finding_fingerprint(probe)
    matches = [row for row in rows if finding_fingerprint(row) == fingerprint]
    own = [row for row in matches if row.finder == probe.finder]
    ids = [row.id for row in own or matches if row.id is not None]
    return max(ids) if ids else None


def pair_posted(
    comments: Sequence[ReviewComment],
    drafts: Sequence[InlineCommentDraft],
    created: Sequence[Comment],
) -> list[tuple[ReviewComment, Comment]]:
    """Pair each reviewer comment with the forge comment a batched review created for it.

    ``comments`` and ``drafts`` run in step. A created comment is matched
    on its path, line and body; when the forge rewrote the body, on its
    path and line alone if exactly one unclaimed comment sits there. A
    draft matched by neither is left out, never guessed.

    Args:
        comments: The reviewer comments, in posting order.
        drafts: The drafts sent for them, in the same order.
        created: The comments the forge reports for the review.

    Returns:
        ``(reviewer comment, created comment)`` pairs.
    """
    unclaimed = list(created)
    pairs: list[tuple[ReviewComment, Comment]] = []
    for comment, draft in zip(comments, drafts, strict=False):
        exact = [
            c
            for c in unclaimed
            if c.path == draft.path and c.line == draft.line and c.body == draft.body
        ]
        placed = [c for c in unclaimed if c.path == draft.path and c.line == draft.line]
        chosen = exact[0] if exact else placed[0] if len(placed) == 1 else None
        if chosen is None:
            continue
        unclaimed.remove(chosen)
        pairs.append((comment, chosen))
    return pairs


def record_posted_comments(
    db_path: Path,
    *,
    forge_name: str,
    pr_number: int,
    role: BotRole,
    head_sha: str,
    posted: Sequence[tuple[ReviewComment, Comment]],
    review_id: int | None,
) -> int:
    """Write one ``posted`` row per posted comment that states a recorded finding.

    A comment with no finding at ``head_sha`` (off the diff) is skipped;
    one whose forge answer carried no id is skipped and logged
    (``finding_posts.id_unparseable``); a refused or failed write is
    logged (``finding_posts.write_failed``) and the next one is tried.

    Args:
        db_path: The ledger database.
        forge_name: ``github`` or ``gitlab``.
        pr_number: The pull / merge request.
        role: The reviewer that posted the comments.
        head_sha: The head they were posted at.
        posted: ``(reviewer comment, created comment)`` pairs.
        review_id: The GitHub batched review id, when any.

    Returns:
        The number of rows written.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: The findings could not be read.
    """
    forge = forge_kind(forge_name)
    if forge is None:
        _log.warning("finding_posts.forge_unrecorded", pr_number=pr_number, forge=forge_name)
        return 0
    rows = [
        finding
        for finding in fetch_findings(db_path, pr_number)
        if finding.head_sha == head_sha and finding.id is not None
    ]
    written = 0
    for comment, created in posted:
        finding_id = posted_finding_id(
            rows, comment, role=role, pr_number=pr_number, head_sha=head_sha
        )
        if finding_id is None:
            continue
        if created.id <= 0:
            _log.warning(
                "finding_posts.id_unparseable",
                pr_number=pr_number,
                finding_id=finding_id,
                file=comment.file,
                line=comment.line,
            )
            continue
        post = FindingPost(
            finding_id=finding_id,
            pr_number=pr_number,
            forge=forge,
            head_sha=head_sha,
            comment_id=str(created.id),
            thread_id=thread_id_of(forge, created),
            review_id=str(review_id) if review_id and forge == "github" else None,
            in_reply_to=str(created.in_reply_to) if created.in_reply_to else None,
        )
        try:
            record_finding_post(db_path, post)
        except (FindingPostIntegrityError, SQLAlchemyError) as exc:
            _log.warning(
                "finding_posts.write_failed",
                pr_number=pr_number,
                finding_id=finding_id,
                comment_id=post.comment_id,
                error=str(exc)[:200],
            )
            continue
        written += 1
    return written
