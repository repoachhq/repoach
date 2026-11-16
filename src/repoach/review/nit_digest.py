"""The nit digest: every nit of a pull / merge request in one comment that blocks nothing.

SP-REVIEW-NITS-ONE-COMMENT. A lens's ``nit`` is recorded in the ledger as
a :attr:`~repoach.review.findings.Severity.NOTE` finding with status
``noted`` and is never posted as an inline comment, so it opens no
review thread and the merge gate never counts it. Instead the review
keeps one conversation comment per pull / merge request carrying
:data:`NIT_DIGEST_MARKER` (``format:nit-digest-comment``): a GitLab
general note or a GitHub issue comment, neither of them resolvable. It
is posted at the first head that has a nit and edited in place at every
later head; a replay whose rendering is unchanged writes nothing.

Each note finding the digest lists is joined to the comment through a
``finding_posts`` row of provenance ``digest``
(:data:`~repoach.review.finding_posts.PROVENANCE_DIGEST`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from ..core.logging import get_logger
from ..forge.protocol import Forge, ForgeError, ForgeStateRefusedError, marked_body
from .finding_posts import (
    PROVENANCE_DIGEST,
    FindingPost,
    fetch_finding_posts,
    forge_kind,
    record_finding_post,
)
from .findings import Finding, Severity, fetch_findings, init_findings_schema
from .findings_bridge import finding_fingerprint

_log = get_logger(__name__)

NIT_DIGEST_MARKER = "<!-- repoach:nit-digest -->"
"""The HTML marker that identifies the nit digest comment on the forge."""

NIT_DIGEST_MAX_ITEMS = 40
"""Items the digest lists at most (the spec's ``nit_digest_max_items``); the rest are counted."""

_COMMENT_ANCHOR: dict[str, str] = {
    "github": "#issuecomment-{comment_id}",
    "gitlab": "#note_{comment_id}",
}
"""The URL fragment of a conversation comment on the pull / merge request page, per forge kind."""


class NitItem(BaseModel):
    """One nit as the digest lists it.

    Attributes:
        finding_id: The ``pr_findings`` row of the note finding.
        head_sha: The head the nit was raised at.
        path: The cited file.
        line: The cited line.
        lens: The reviewer that raised it.
        text: The claim, on one line.
    """

    finding_id: int
    head_sha: str
    path: str
    line: int
    lens: str
    text: str


class NitDigest(BaseModel):
    """The digest of a pull / merge request at one head.

    Attributes:
        pr_number: The pull / merge request.
        head_sha: The head the review ran at; its items go under ``## At``.
        items: The listed nits, at most :data:`NIT_DIGEST_MAX_ITEMS`.
        truncated: How many more nits there are than ``items`` lists.
        comment_id: The forge id of the digest comment, as the ledger or
            the last post recorded it; ``None`` before any post.
        url: The web URL of the digest comment once posted, else ``None``.
        error: Why the last post or edit failed; ``None`` when it did not.
    """

    pr_number: int
    head_sha: str
    items: list[NitItem]
    truncated: int = 0
    comment_id: str | None = None
    url: str | None = None
    error: str | None = None

    @property
    def count(self) -> int:
        """Every nit of the pull / merge request, listed or counted."""
        return len(self.items) + self.truncated

    def as_json(self, *, with_body: bool) -> dict[str, object]:
        """Return the ``nits`` block of the review JSON and the archive.

        The archive comment is posted on the forge, so it never carries
        the rendered digest: the body holds :data:`NIT_DIGEST_MARKER`,
        and a second comment carrying it would be taken for the digest.

        Args:
            with_body: Add ``body``, the rendered digest (``None`` when
                there is no nit), as the ``review pr`` JSON shows it.

        Returns:
            ``count``, ``digest_url`` and ``error``, plus ``body`` when asked.
        """
        block: dict[str, object] = {
            "count": self.count,
            "digest_url": self.url,
            "error": self.error,
        }
        if with_body:
            block["body"] = render_digest(self) if self.items else None
        return block

    def report_line(self) -> str:
        """Return the stderr line naming the digest, empty when there is no nit."""
        if self.error is not None:
            return f"nit digest not updated: {self.error}"
        if not self.items:
            return ""
        where = self.url or "not posted"
        return f"nit digest: {self.count} nit(s), {where}"


def _one_line(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed to one space."""
    return " ".join(text.split())


def collect_nits(findings: Sequence[Finding], *, head_sha: str) -> list[NitItem]:
    """Return the digest items of the note findings, in digest order.

    A nit raised again at a later head (same
    :func:`~repoach.review.findings_bridge.finding_fingerprint`) is
    listed once, at the latest head that raised it, ``head_sha`` first.
    A nit the same head also recorded with a higher severity (same
    fingerprint) is not a nit there: it is left out.
    Order: the items of ``head_sha``, then those of each earlier head,
    the most recently recorded head first; by path, then line, within a
    head.

    Args:
        findings: Ledger findings of one pull / merge request; only the
            :attr:`~repoach.review.findings.Severity.NOTE` ones are read.
        head_sha: The head the review ran at.

    Returns:
        One item per distinct nit.

    Raises:
        ValueError: A note finding has no id (it was never stored), so
            no ledger row can join it to the digest.
    """
    outranked = {
        (finding.head_sha, finding_fingerprint(finding))
        for finding in findings
        if finding.severity is not Severity.NOTE
    }
    notes = [
        finding
        for finding in findings
        if finding.severity is Severity.NOTE
        and (finding.head_sha, finding_fingerprint(finding)) not in outranked
    ]
    for note in notes:
        if note.id is None:
            raise ValueError(f"note finding at {note.file}:{note.line_start} has no ledger id")
    latest_first = sorted(notes, key=lambda f: (f.head_sha != head_sha, -(f.id or 0)))
    kept: dict[str, Finding] = {}
    for note in latest_first:
        kept.setdefault(finding_fingerprint(note), note)
    head_rank: dict[str, int] = {}
    for note in latest_first:
        head_rank.setdefault(note.head_sha, len(head_rank))
    ordered = sorted(
        kept.values(), key=lambda f: (head_rank[f.head_sha], f.file, f.line_start, f.id or 0)
    )
    return [
        NitItem(
            finding_id=note.id or 0,
            head_sha=note.head_sha,
            path=note.file,
            line=note.line_start,
            lens=note.finder,
            text=_one_line(note.claim),
        )
        for note in ordered
    ]


def digest_for_pr(
    db_path: Path, pr_number: int, *, head_sha: str, max_items: int = NIT_DIGEST_MAX_ITEMS
) -> NitDigest:
    """Read the pull / merge request's nits and its digest comment id from the ledger.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.
        head_sha: The head the review ran at.
        max_items: Items listed at most; the rest are counted in ``truncated``.

    Returns:
        The digest, its ``comment_id`` the latest ``digest`` row's.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: The ledger could not be read.
        ValueError: As :func:`collect_nits`.
    """
    init_findings_schema(db_path)
    items = collect_nits(fetch_findings(db_path, pr_number), head_sha=head_sha)
    digest_rows = [
        post
        for post in fetch_finding_posts(db_path, pr_number)
        if post.provenance == PROVENANCE_DIGEST
    ]
    return NitDigest(
        pr_number=pr_number,
        head_sha=head_sha,
        items=items[:max_items],
        truncated=max(len(items) - max_items, 0),
        comment_id=digest_rows[-1].comment_id if digest_rows else None,
    )


def _item_line(item: NitItem, *, with_head: bool) -> str:
    """Render one item as ``path:line — lens — text``, its head first when asked."""
    head = f"`{item.head_sha[:7]}` " if with_head else ""
    return f"- {head}`{item.path}:{item.line}` — {item.lens} — {item.text}"


def render_digest(digest: NitDigest) -> str:
    """Render the digest comment (``format:nit-digest-comment``).

    The marker, a heading, the items of ``digest.head_sha`` under
    ``## At <sha7>``, the items of earlier heads under ``## Earlier``
    with their head, and ``… and <n> more`` when items were left out.
    A section with no item is left out.

    Args:
        digest: The digest to render.

    Returns:
        The comment body, the marker on its first line.
    """
    current = [item for item in digest.items if item.head_sha == digest.head_sha]
    earlier = [item for item in digest.items if item.head_sha != digest.head_sha]
    lines = [
        NIT_DIGEST_MARKER,
        "### Repoach nits",
        "",
        f"{digest.count} nit(s) raised on this change. A nit opens no review thread "
        "and blocks no merge: read them, there is nothing to resolve.",
    ]
    if current:
        lines += ["", f"## At {digest.head_sha[:7]}", ""]
        lines += [_item_line(item, with_head=False) for item in current]
    if earlier:
        lines += ["", "## Earlier", ""]
        lines += [_item_line(item, with_head=True) for item in earlier]
    if digest.truncated:
        lines += ["", f"… and {digest.truncated} more"]
    return "\n".join(lines) + "\n"


def post_or_edit_digest(forge: Forge, digest: NitDigest, *, db_path: Path) -> NitDigest:
    """Post the digest comment, or edit the one already there, and record what it lists.

    Nothing is written when the digest has no item, or when the comment
    on the forge already reads exactly the body the forge writes for it
    (:func:`~repoach.forge.protocol.marked_body`; a replay at
    the same head). Otherwise the trusted comment opened by
    :data:`NIT_DIGEST_MARKER` is edited in place, or posted when there
    is none, and each listed note finding gets a ``digest``
    ``finding_posts`` row naming the comment. A forge refusal leaves the
    comment as it was: ``nit_digest_edit_failed`` is logged and the
    returned digest carries the forge's reason in ``error``.

    Args:
        forge: The forge of the pull / merge request; its write barrier
            must be open.
        digest: The digest to publish, from :func:`digest_for_pr`.
        db_path: The ledger database.

    Returns:
        ``digest`` with ``comment_id`` and ``url`` of the comment, or with
        ``error`` set when the forge refused; ``digest`` itself when it
        has no item.

    Raises:
        ForgeStateRefusedError: The pull / merge request is no longer
            open; the review stops posting.
        ValueError: The forge keeps no comment ids (neither GitHub nor
            GitLab), so the digest cannot be joined to the ledger.
        sqlalchemy.exc.SQLAlchemyError: The ledger rows could not be written.
    """
    if not digest.items:
        return digest
    kind = forge_kind(forge.name)
    if kind is None:
        raise ValueError(f"forge {forge.name!r} keeps no comment ids for a nit digest")
    body = render_digest(digest)
    try:
        page = forge.pull_request(digest.pr_number).url
        existing = forge.find_marked_comment(digest.pr_number, NIT_DIGEST_MARKER)
        if (
            existing is not None
            and existing.body.strip() == marked_body(NIT_DIGEST_MARKER, body).strip()
        ):
            comment = existing
        else:
            comment = forge.upsert_marked_comment(digest.pr_number, NIT_DIGEST_MARKER, body)
            _log.info(
                "nit_digest_edited" if existing is not None else "nit_digest_posted",
                pr_number=digest.pr_number,
                head_sha=digest.head_sha[:12],
                comment_id=comment.id,
                count=digest.count,
            )
    except ForgeStateRefusedError:
        raise
    except ForgeError as exc:
        _log.warning(
            "nit_digest_edit_failed",
            pr_number=digest.pr_number,
            head_sha=digest.head_sha[:12],
            comment_id=digest.comment_id,
            error=exc.message[:200],
        )
        return digest.model_copy(update={"error": exc.message[:200]})
    comment_id = str(comment.id)
    for item in digest.items:
        record_finding_post(
            db_path,
            FindingPost(
                finding_id=item.finding_id,
                pr_number=digest.pr_number,
                forge=kind,
                head_sha=digest.head_sha,
                comment_id=comment_id,
                provenance=PROVENANCE_DIGEST,
            ),
        )
    url = page + _COMMENT_ANCHOR[kind].format(comment_id=comment_id) if page else None
    return digest.model_copy(update={"comment_id": comment_id, "url": url})
