"""Reviewer thread-aware context for SP-CODER-EVIDENCE-CHALLENGE phase 3B.

Phase 3A made the Coder post ``"Verified — challenge with evidence"``
replies on hallucinated / already-satisfied reviewer threads.  Phase
3B feeds those replies back into the **next** reviewer run so the
bot does not re-emit the same critique unrefuted.

The flow:

1. Orchestrator opens round 2 (or any later round) on a PR.
2. Before invoking each reviewer, it calls
   :func:`fetch_resolved_disagreements` with the reviewer's role.
3. The function walks every inline thread on the PR via
   ``gh.list_review_comments``, finds threads whose **root** comment
   carries this role's tag (``[<role>/<severity>]``) AND whose
   replies include the ``"Verified — challenge with evidence"``
   sentinel posted by the Coder bot, and returns one
   :class:`ResolvedDisagreement` per match.
4. The orchestrator hands the list to ``review_diff`` which pipes
   it through to ``_render_prompt`` — the prompt template's
   ``{RESOLVED_DISAGREEMENTS}`` placeholder is filled with the
   markdown block from :func:`render_resolved_disagreements_block`.

Refute-or-retract enforcement lives in the prompt itself
(``prompts/review/<role>_0.3.0.md``):

> If you intend to re-raise any of these critiques, your new comment
> MUST quote the cited path / line / snippet from the evidence reply
> and explain why the evidence is wrong.

Phase 3C will add orchestrator-side auto-challenge when the reviewer
ignores this rule, but 3B alone delivers the prompt-level guard.

Module-level constants :

* :data:`EVIDENCE_REPLY_SENTINEL` is the substring
  :func:`coder_loop._format_evidence_reply` writes into every Coder
  evidence reply.  Any reply containing this marker is treated as a
  Coder evidence challenge for the purpose of resolving the thread.
* :data:`_ROLE_TAG_RE` matches the reviewer-side tag prefix the
  orchestrator's inline-comment poster writes :
  ``**[<role>/<severity>]**`` (case-insensitive).
* :data:`_EXCERPT_MAX_CHARS` caps reviewer-body and coder-reply
  excerpts in the prompt block.  Tight because we may render up to
  N disagreements per reviewer and the prompt token budget is
  finite.
* :data:`_MAX_DISAGREEMENTS_PER_ROLE` caps the resolved
  disagreements injected per reviewer.  Older threads (smaller
  comment ids) are dropped first when a PR accumulates many
  resolutions.
* :data:`REPEAT_SIMILARITY_THRESHOLD` is the SequenceMatcher ratio
  above which a new comment is treated as a repeat of a resolved
  thread (parent spec : "≥ 85 % similarity between a new reviewer
  comment and a thread the same reviewer already had challenged
  with evidence on this PR").
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from .findings import Finding, FindingStatus, fetch_findings
from .gh_client import GhCli
from .reviewer import BotRole, ReviewComment

_log = get_logger(__name__)


EVIDENCE_REPLY_SENTINEL: str = "Verified — challenge with evidence"

_ROLE_TAG_RE = re.compile(
    r"\*\*\[(?P<role>architect|sentinel|tester|scribe)/[a-z]+\]\*\*",
    re.IGNORECASE,
)

_EXCERPT_MAX_CHARS: int = 400

_MAX_DISAGREEMENTS_PER_ROLE: int = 8


@dataclass(frozen=True)
class ResolvedDisagreement:
    """One reviewer thread the Coder bot has already challenged.

    Attributes:
        role: Which reviewer role posted the root comment.
        file: ``path`` field of the root inline comment.
        line: ``line`` field of the root inline comment (1-based).
        reviewer_body: Truncated original critique (≤
            :data:`_EXCERPT_MAX_CHARS`).
        coder_evidence: Truncated Coder reply that contained the
            evidence sentinel (≤ :data:`_EXCERPT_MAX_CHARS`).
        root_comment_id: Inline-thread root id — useful for follow-on
            phases that want to reply on the same thread.
    """

    role: BotRole
    file: str
    line: int
    reviewer_body: str
    coder_evidence: str
    root_comment_id: int


def _detect_role_in_body(body: str) -> BotRole | None:
    """Return the reviewer role tagged in ``body``, or ``None``.

    Matches the orchestrator's ``**[<role>/<severity>]**`` prefix that
    every inline reviewer comment carries.  Case-insensitive on the
    role name; first match wins.
    """
    if not body:
        return None
    m = _ROLE_TAG_RE.search(body)
    if not m:
        return None
    role_raw = m.group("role").lower()
    try:
        return BotRole(role_raw)
    except ValueError:
        _log.debug("thread_context.unknown_role_tag", role_raw=role_raw)
        return None


def _truncate(s: str, *, max_chars: int = _EXCERPT_MAX_CHARS) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _partition_review_comments(
    threads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """Split raw inline comments into thread roots and replies-by-root.

    A comment with ``in_reply_to_id is None`` is a thread root; every
    other comment is a reply keyed under its root's id.  Non-dict
    entries and unparseable reply ids are dropped.
    """
    replies_by_root: dict[int, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for c in threads:
        if not isinstance(c, dict):
            continue
        in_reply = c.get("in_reply_to_id")
        if in_reply is None:
            roots.append(c)
            continue
        try:
            in_reply_int = int(in_reply)
        except (TypeError, ValueError):
            _log.debug("thread_context.bad_in_reply_id", raw=str(in_reply)[:50])
            continue
        replies_by_root.setdefault(in_reply_int, []).append(c)
    return roots, replies_by_root


def fetch_resolved_disagreements(
    gh: GhCli,
    pr_number: int,
    *,
    role: BotRole,
) -> list[ResolvedDisagreement]:
    """Return resolved-disagreement threads for ``role``.

    A "resolved" thread is one whose root comment was tagged by this
    reviewer role AND received a Coder reply containing the
    :data:`EVIDENCE_REPLY_SENTINEL` substring.

    Args:
        gh: GitHub CLI wrapper used to fetch the PR's review comments.
        pr_number: PR number.
        role: Reviewer role to filter on; only threads whose root
            comment is tagged with this role's ``[<role>/...]`` prefix
            are returned.

    Returns:
        Up to :data:`_MAX_DISAGREEMENTS_PER_ROLE` resolved threads,
        most recent first (thread root ``id`` descending).  Empty
        list when no thread matches or the gh call fails.
    """
    threads = gh.list_review_comments(pr_number)
    if not threads:
        return []

    roots, replies_by_root = _partition_review_comments(threads)

    out: list[ResolvedDisagreement] = []
    for root in roots:
        body = str(root.get("body") or "")
        detected = _detect_role_in_body(body)
        if detected is not role:
            continue
        try:
            root_id = int(root.get("id") or 0)
        except (TypeError, ValueError):
            _log.debug("thread_context.bad_root_id", raw=str(root.get("id"))[:50])
            continue
        if root_id <= 0:
            continue
        evidence_reply: str | None = None
        for reply in replies_by_root.get(root_id, []):
            reply_body = str(reply.get("body") or "")
            if EVIDENCE_REPLY_SENTINEL in reply_body:
                evidence_reply = reply_body
                break
        if evidence_reply is None:
            continue
        path = str(root.get("path") or "")
        try:
            line_int = int(root.get("line") or 0)
        except (TypeError, ValueError):
            _log.debug("thread_context.root_line_unparseable", raw=root.get("line"))
            line_int = 0
        out.append(
            ResolvedDisagreement(
                role=role,
                file=path,
                line=line_int,
                reviewer_body=_truncate(body),
                coder_evidence=_truncate(evidence_reply),
                root_comment_id=root_id,
            )
        )

    out.sort(key=lambda r: r.root_comment_id, reverse=True)
    if len(out) > _MAX_DISAGREEMENTS_PER_ROLE:
        _log.info(
            "review.resolved_disagreements.capped",
            pr_number=pr_number,
            role=role.value,
            kept=_MAX_DISAGREEMENTS_PER_ROLE,
            dropped=len(out) - _MAX_DISAGREEMENTS_PER_ROLE,
        )
        out = out[:_MAX_DISAGREEMENTS_PER_ROLE]
    _log.info(
        "review.resolved_disagreements.fetched",
        pr_number=pr_number,
        role=role.value,
        n=len(out),
    )
    return out


def render_resolved_disagreements_block(
    items: list[ResolvedDisagreement] | tuple[ResolvedDisagreement, ...],
) -> str:
    """Render the markdown block injected into the reviewer prompt.

    When ``items`` is empty, returns a single-line stub so the
    template never carries an un-substituted placeholder.  The
    non-empty form is a numbered list — each entry pairs the
    reviewer's original critique with the Coder's evidence reply so
    the model can refute or retract.
    """
    if not items:
        return "_(no prior disagreements on this PR — review the diff on its own merits.)_"
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        lines.append(f"### {idx}. `{item.file}:{item.line}`")
        lines.append("")
        lines.append("**Your original critique:**")
        lines.append("")
        lines.append("> " + item.reviewer_body.replace("\n", "\n> "))
        lines.append("")
        lines.append("**Coder's evidence reply:**")
        lines.append("")
        lines.append("> " + item.coder_evidence.replace("\n", "\n> "))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


REPEAT_SIMILARITY_THRESHOLD: float = 0.85


@dataclass(frozen=True)
class AutoChallengeMatch:
    """A new reviewer comment that repeats a previously resolved thread.

    Attributes:
        new_comment: The freshly posted :class:`ReviewComment`.
        new_comment_id: GitHub inline-thread id assigned to it after
            posting.  Required for the reply API.
        resolved: The prior :class:`ResolvedDisagreement` it matches.
        similarity: Computed similarity ratio (0–1).
    """

    new_comment: ReviewComment
    new_comment_id: int
    resolved: ResolvedDisagreement
    similarity: float


_ROLE_TAG_STRIP_RE = re.compile(r"\*\*\[[a-z]+/[a-z]+\]\*\*\s*", re.IGNORECASE)
_TRAILING_FOOTER_RE = re.compile(r"\n_— Ferova.*$", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_for_similarity(body: str) -> str:
    """Strip the role tag + trailing model-used footer, lowercase, collapse ws.

    The published reviewer comment carries a ``**[role/severity]**``
    prefix and a ``_— Ferova review-bot (model)_`` suffix that
    weren't part of the original ``ReviewComment.body``; the resolved
    body fetched from GitHub does carry both.  Stripping them lets
    the similarity score reflect the actual critique content rather
    than the boilerplate.
    """
    if not body:
        return ""
    s = _ROLE_TAG_STRIP_RE.sub("", body)
    s = _TRAILING_FOOTER_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip().lower()
    return s


def similarity(a: str, b: str) -> float:
    """Return :class:`difflib.SequenceMatcher` ratio of the normalised forms."""
    na, nb = _normalise_for_similarity(a), _normalise_for_similarity(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def has_refutation(new_body: str, resolved: ResolvedDisagreement) -> bool:
    """Return True when ``new_body`` quotes the prior evidence.

    Heuristic: the reviewer's new comment refutes the prior challenge
    only when it explicitly cites the path / line that lived in the
    Coder's evidence reply.  This mirrors the prompt rule: "your new
    comment MUST quote the cited path/line/snippet from the evidence
    reply".

    The check is lenient about format — the reviewer may write
    ``src/x.py:5`` or ``src/x.py``; we accept either.  Empty resolved
    file/line means we cannot prove refutation either way (returns
    False, treated as non-refutation).

    Path mention is treated as a necessary AND sufficient signal —
    the gate is conservative on purpose ; the orchestrator can still
    mark the disagreement resolved when the reviewer's new comment
    cites the disputed path rather than re-litigating the original
    body.
    """
    if not new_body or not resolved.file:
        return False
    body_lc = new_body.lower()
    file_lc = resolved.file.lower()
    return file_lc in body_lc


def find_repeat_hallucinations(
    new_comments: list[ReviewComment],
    *,
    posted_ids: dict[tuple[str, int], int],
    resolved: list[ResolvedDisagreement] | tuple[ResolvedDisagreement, ...],
    similarity_threshold: float = REPEAT_SIMILARITY_THRESHOLD,
) -> list[AutoChallengeMatch]:
    """Pair each new comment with its matching resolved thread, if any.

    Args:
        new_comments: Comments the reviewer just posted on this run.
        posted_ids: ``(file, line) → new comment id`` mapping built by
            the orchestrator after each successful inline post.
        resolved: Prior resolved disagreements for THIS reviewer's role
            (typically what ``fetch_resolved_disagreements`` returned
            at run start).
        similarity_threshold: Match cut-off; defaults to
            :data:`REPEAT_SIMILARITY_THRESHOLD`.

    Returns:
        One :class:`AutoChallengeMatch` per new comment that:

        * has a known ``new_comment_id`` in ``posted_ids``,
        * is ≥ ``similarity_threshold`` similar to a resolved entry,
        * does NOT refute that entry's evidence.

        At most one match per new comment (best-similarity wins).
    """
    if not new_comments or not resolved:
        return []
    out: list[AutoChallengeMatch] = []
    for comment in new_comments:
        new_id = posted_ids.get((comment.file, comment.line))
        if new_id is None:
            continue
        best: tuple[float, ResolvedDisagreement] | None = None
        for prior in resolved:
            ratio = similarity(comment.body, prior.reviewer_body)
            if ratio < similarity_threshold:
                continue
            if best is None or ratio > best[0]:
                best = (ratio, prior)
        if best is None:
            continue
        if has_refutation(comment.body, best[1]):
            continue
        out.append(
            AutoChallengeMatch(
                new_comment=comment,
                new_comment_id=new_id,
                resolved=best[1],
                similarity=best[0],
            )
        )
    return out


def format_auto_challenge_reply(match: AutoChallengeMatch) -> str:
    """Render the auto-challenge reply body.

    Quotes the Coder's prior evidence verbatim so the rebuttal stays
    symmetric — the reviewer can no longer slip past on a technicality.
    """
    similarity_pct = round(match.similarity * 100)
    return (
        "**Auto-challenge — repeat-hallucination guard:**\n\n"
        f"This new comment is ~{similarity_pct}% similar to a prior "
        f"thread on this PR (root comment id `{match.resolved.root_comment_id}`) "
        "where the Coder bot already posted a "
        '`"Verified — challenge with evidence"` reply.  Per the '
        "refute-or-retract rule, a re-raise must quote the cited "
        "path/line/snippet from the evidence reply and explain why "
        "it is wrong; this comment does not.\n\n"
        "**Prior evidence (verbatim):**\n\n"
        "> " + match.resolved.coder_evidence.replace("\n", "\n> ") + "\n\n"
        "🤖 Ferova orchestrator (ferova review-team) — "
        "this disagreement is marked "
        "``resolved-by-prior-challenge`` in the dialogue archive."
    )


def _format_finding_evidence_reply(finding: Finding) -> str:
    """Render the evidence sentinel reply body for a refuted finding.

    The first line embeds :data:`EVIDENCE_REPLY_SENTINEL` verbatim so
    :func:`fetch_resolved_disagreements` keys on it on the next review
    run.  This is the evidence-first replacement for the legacy
    ``coder_loop._format_evidence_reply`` (SP-FINDINGS-SENTINEL-REHOME);
    it cites the verifier or refuter that disproved the claim so a
    re-raise must bring fresh evidence rather than re-assert the body.
    """
    reason = finding.verification_result or "the finding was refuted at head."
    method = finding.verification_method or "verifier"
    return "\n".join(
        [
            f"**{EVIDENCE_REPLY_SENTINEL}:**",
            "",
            reason,
            "",
            (
                f"🤖 Ferova review-team — this `{finding.claim_type.value}` "
                f"finding was refuted by the `{method}` check and marked "
                "``refuted`` in the findings ledger; retained for operator "
                "review.  To re-raise it, quote the cited path/line/snippet "
                "and explain why the evidence is wrong."
            ),
        ]
    )


def _match_finding_thread(
    roots: list[dict[str, Any]],
    finding: Finding,
) -> dict[str, Any] | None:
    """Return the root thread a ``finding`` originated from, or ``None``.

    A :class:`~ferova.review.findings.Finding` carries no GitHub
    comment id (the source :class:`~ferova.review.reviewer.ReviewComment`
    never had one), so the thread is re-located by ``path`` + ``line``
    equality, with the highest :func:`similarity` of root body vs the
    finding's claim breaking ties when several critiques share a line.
    """
    candidates: list[dict[str, Any]] = []
    for root in roots:
        if str(root.get("path") or "") != finding.file:
            continue
        raw_line = root.get("line")
        try:
            line_int = int(raw_line) if raw_line is not None else None
        except (TypeError, ValueError):
            _log.debug("thread_context.match_line_unparseable", raw=str(raw_line)[:50])
            line_int = None
        if line_int != finding.line_start:
            continue
        candidates.append(root)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    candidates.sort(
        key=lambda r: similarity(str(r.get("body") or ""), finding.claim),
        reverse=True,
    )
    return candidates[0]


def _thread_has_sentinel(replies: list[dict[str, Any]]) -> bool:
    """Return True when any reply on the thread already carries the sentinel."""
    return any(EVIDENCE_REPLY_SENTINEL in str(r.get("body") or "") for r in replies)


def post_refuted_finding_sentinels(
    gh: GhCli,
    db_path: Path,
    pr_number: int,
) -> int:
    """Post the evidence sentinel on every refuted reviewer finding's thread.

    The evidence-first re-homing of the legacy pre-verify writer
    (SP-FINDINGS-SENTINEL-REHOME).  When the initial verify/refute pass
    disproves a reviewer-origin finding its stored status becomes
    ``refuted`` — a terminal state, so it can never be a fix-resolved
    finding (those settle at ``resolved``).  For each such finding a
    ``"Verified — challenge with evidence"`` reply is posted on the
    originating inline thread so the **next** review run's
    :func:`fetch_resolved_disagreements` feeds it into the reviewer
    prompt — restoring the anti-convergent-hallucination guard the
    findings path otherwise lost when the legacy Coder retired.

    CI-materialised findings (``finder == "ci"``) have no reviewer
    thread and are skipped.  Posting is idempotent: a thread that
    already carries the sentinel is left untouched, so re-runs and the
    accumulating refuted ledger never duplicate replies.  A failed
    ``gh`` reply is logged and skipped, never raised.

    Args:
        gh: GitHub CLI wrapper.
        db_path: The findings ledger database.
        pr_number: PR whose refuted reviewer findings are announced.

    Returns:
        The number of sentinel replies newly posted.
    """
    refuted = [
        f
        for f in fetch_findings(db_path, pr_number, status=FindingStatus.REFUTED)
        if f.finder != "ci" and f.file and f.line_start > 0
    ]
    if not refuted:
        return 0
    threads = gh.list_review_comments(pr_number)
    if not threads:
        return 0
    roots, replies_by_root = _partition_review_comments(threads)
    posted = 0
    for finding in refuted:
        root = _match_finding_thread(roots, finding)
        if root is None:
            continue
        try:
            root_id = int(root.get("id") or 0)
        except (TypeError, ValueError):
            _log.debug("thread_context.match_root_id_unparseable", raw=str(root.get("id"))[:50])
            continue
        if root_id <= 0:
            continue
        if _thread_has_sentinel(replies_by_root.get(root_id, [])):
            continue
        body = _format_finding_evidence_reply(finding)
        try:
            gh.reply_to_review_comment(pr_number, comment_id=root_id, body=body)
        except Exception as exc:
            _log.warning(
                "review.refuted_sentinel.post_failed",
                pr_number=pr_number,
                root_comment_id=root_id,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )
            continue
        posted += 1
        _log.info(
            "review.refuted_sentinel.posted",
            pr_number=pr_number,
            root_comment_id=root_id,
            file=finding.file,
            line=finding.line_start,
            claim_type=finding.claim_type.value,
        )
    _log.info(
        "review.refuted_sentinels.done",
        pr_number=pr_number,
        posted=posted,
        candidates=len(refuted),
    )
    return posted


__all__ = [
    "EVIDENCE_REPLY_SENTINEL",
    "REPEAT_SIMILARITY_THRESHOLD",
    "AutoChallengeMatch",
    "ResolvedDisagreement",
    "fetch_resolved_disagreements",
    "find_repeat_hallucinations",
    "format_auto_challenge_reply",
    "has_refutation",
    "post_refuted_finding_sentinels",
    "render_resolved_disagreements_block",
    "similarity",
]
