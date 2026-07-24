"""Runner-side hallucination guard for review-bot outcomes.

The plan-aware reviewers feed the spec into every reviewer's prompt
alongside the diff.  This unlocks spec-vs-impl checks but also
surfaces four recurring hallucination modes — observed live on PR
#19, PR #20, PR #157, and follow-ups:

1. **Missing-X claims** — reviewer flags ``Args:`` / ``Returns:`` /
   ``method X`` as missing when the symbol is in the file but outside
   the narrow diff window the model received.
2. **Self-referential prompt-template confusion** — reviewer claims a
   ``prompts/review/*.md`` diff "includes the full plan" because the
   substituted ``{SPEC_PLAN}`` block (in the reviewer's own prompt)
   shares tokens with the diff text.
3. **"This file embeds the full spec"** — broader variant where the
   reviewer cites any file (prompts or source) and falsely claims the
   diff embeds the spec body as literal text.
4. **"No test for X" / "add test_Y" when X / Y already exist** — Tester
   pattern from PR #157 / #163 / #164 : a new test file was added in
   the PR but the bot's diff slice didn't include it, so the bot
   recommends adding tests that ship in the same PR.  Caught by
   grep-on-disk over ``tests/**/*.py`` (SP-CODER-EVIDENCE-CHALLENGE).

This module post-processes a :class:`ReviewerOutcome` against the
working tree, downgrades blocker/major comments whose claim is
disproved by reading the actual file, and softens the verdict when
the supporting evidence collapses.

The guard is conservative: it only downgrades when the disproof is
unambiguous (the missing token is found in the file, or the cited
``prompts/review/*`` line is just the placeholder).  Every downgrade
is logged via ``hallucination_guard.downgraded`` for audit.

Module-level constants :
    - :data:`_EMBEDS_SPEC_BODY` matches the broader "embeds the full
      spec" pattern.  The narrower "the full plan" wording is covered
      by :data:`_SELF_REFERENTIAL_BODY`; this regex extends the verb
      and noun set so claims like "embeds the full SP-XXX spec doc"
      are also caught when paired with an evidence read.
    - :data:`_SPEC_DOC_MARKERS` matches the Markdown headers we use
      in ``docs/specs/`` (``# SP-…``, ``## Why``, ``## How``, ``## Out
      of scope``, ``## Done when``, ``## Definition of Done``,
      ``## Status``, ``## Owner``, ``## Metadata``).  When NONE of
      these appear in the cited file, an "embeds the full spec"
      claim is provably false.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.logging import get_logger
from .reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict


@dataclass(frozen=True)
class GuardEvent:
    """One downgrade event produced by :func:`apply_hallucination_guard`.

    Captured per-comment so the orchestrator can persist a per-PR
    audit trail (L4 ``pr_hallucinations``) and surface the activity
    in the sticky archive comment.

    Attributes:
        role: Reviewer whose comment was downgraded.
        file: File path the comment was attached to (or empty string).
        line: Line number the comment cites (``0`` when missing).
        original_severity: ``"blocker"`` or ``"major"`` before the guard.
        reason: ``"self_referential"`` |
            ``"missing_token_found_in_file"`` (with tokens captured
            in :attr:`tokens_found`).
        tokens_found: Section names found in the file when the disproof
            mode is :attr:`reason` ``"missing_token_found_in_file"``.
        original_body: Verbatim body of the original comment for audit.
    """

    role: BotRole
    file: str
    line: int
    original_severity: str
    reason: str
    tokens_found: tuple[str, ...] = field(default_factory=tuple)
    original_body: str = ""


_log = get_logger(__name__)

_DOWNGRADE_PREFIX = "[guard:downgraded] "
_FILE_READ_CAP_BYTES = 200_000

_TRIGGER = re.compile(
    r"\b(?:missing|absent|lacks?|lacking|is\s+not\s+(?:present|defined|documented))\b",
    re.IGNORECASE,
)

_GOOGLE_SECTIONS = (
    "Args",
    "Arguments",
    "Returns",
    "Return",
    "Yields",
    "Raises",
    "Attributes",
    "Note",
    "Notes",
    "Example",
    "Examples",
)


_SELF_REFERENTIAL_FILE = re.compile(r"^prompts/review/.+\.md$")
_SELF_REFERENTIAL_BODY = re.compile(r"(?i)(?:includes?|contains?|embeds?)\s+the\s+full\s+plan")
_SELF_REFERENTIAL_DOMAIN_VOCAB = re.compile(
    r"(?i)remove\s+(?:the\s+)?french\s+(?:word|wording|term|phrase)"
)

_EMBEDS_SPEC_BODY = re.compile(
    r"(?i)(?:includes?|contains?|embeds?|leaks?)\s+the\s+full\s+"
    r"(?:plan|spec(?:\s+doc)?|"
    r"(?:CH|SP)[-_][A-Z0-9-]+(?:\s+spec(?:\s+doc)?)?)"
)
_SPEC_DOC_MARKERS = re.compile(
    r"(?m)^(?:#\s+SP-[A-Z0-9-]+|"
    r"##\s+(?:Why|How|Out\s+of\s+scope|Done\s+when|Definition\s+of\s+Done|"
    r"Status|Owner|Metadata))"
)

_MISSING_TEST_TRIGGER = re.compile(
    r"(?i)(?:add\s+test_|no\s+test|missing\s+test|unexercised|"
    r"no\s+(?:dedicated\s+)?(?:unit\s+)?test|"
    r"add\s+(?:a\s+)?test\s+(?:for|asserting|covering)|"
    r"there\s+is\s+no\s+test)"
)
_TEST_NAME_PATTERN = re.compile(r"\btest_[A-Za-z0-9_]+\b")
_BACKTICK_SYMBOL_PATTERN = re.compile(r"`([A-Za-z_][A-Za-z0-9_]+)`")


FileReader = Callable[[str], str | None]

SymbolSearcher = Callable[[str], bool]
"""``(symbol) -> True`` when the worktree contains the symbol on disk."""


def _default_file_reader(repo_root: Path) -> FileReader:
    """Build a FileReader that reads files relative to ``repo_root``."""

    def _read(path: str) -> str | None:
        try:
            target = (repo_root / path).resolve()
            root = repo_root.resolve()
            if not str(target).startswith(str(root)):
                return None
            if not target.is_file():
                return None
            with open(target, "rb") as fh:
                blob = fh.read(_FILE_READ_CAP_BYTES)
            return blob.decode("utf-8", errors="replace")
        except OSError as exc:
            _log.warning(
                "hallucination_guard.file_read_failed",
                path=str(target),
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return None

    return _read


def _extract_missing_tokens(body: str) -> list[str]:
    """Return candidate tokens claimed-missing by the comment body.

    The guard targets the dominant observed hallucination mode: a
    reviewer claiming a Google docstring section (``Args``, ``Returns``,
    ``Raises``, …) is missing when in fact it is in the file but
    outside the diff window.

    Strategy: only run when the body matches the "missing / absent /
    lacks / not present" trigger AND mentions a Google section name.
    Other capitalised words and backtick-quoted identifiers are skipped
    on purpose — those are usually location anchors (the function /
    class where the docstring lives), not the target of the missing
    claim, and matching them yields false-positive downgrades.
    """
    if not _TRIGGER.search(body):
        return []

    seen: list[str] = []
    for section in _GOOGLE_SECTIONS:
        if re.search(rf"\b{section}\b", body) and section not in seen:
            seen.append(section)
    return seen


def _file_contains_token(file_text: str, token: str) -> bool:
    """Return True when ``token`` appears as a whole word in ``file_text``.

    Match is case-sensitive on the alphabetic part: ``Args`` is found in
    ``Args:`` but not in ``args``.  Trailing colons / parentheses on the
    file side are tolerated.
    """
    pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)")
    return bool(pattern.search(file_text))


def _comment_is_self_referential(comment: ReviewComment) -> bool:
    """Detect Architect's prompt-template self-referential hallucination.

    The narrow pattern: file under ``prompts/review/`` AND body
    claims "includes/contains/embeds the full plan" or asks to remove
    French project-domain vocabulary.  Caught by content +
    file-location alone, no file read needed.
    """
    if not _SELF_REFERENTIAL_FILE.match(comment.file or ""):
        return False
    body = comment.body or ""
    return bool(_SELF_REFERENTIAL_BODY.search(body) or _SELF_REFERENTIAL_DOMAIN_VOCAB.search(body))


def _comment_falsely_claims_spec_embedded(
    comment: ReviewComment,
    *,
    file_reader: FileReader,
) -> bool:
    """Disprove "this file embeds the full spec/spec" claims.

    Broader counterpart to :func:`_comment_is_self_referential`: it
    accepts any cited file (``prompts/review/*.md`` and
    ``src/**/*.py`` alike, since Architect on PR #20 spread the same
    false claim across both), but only fires when the file is read
    and proven NOT to contain spec-doc Markdown markers
    (``# SP-...``, ``## Pourquoi``, ``## Comment``, ``## Out of
    scope``, ``## Fini quand``, ``## Status``, ``## Owner``).

    A file that genuinely embeds a spec doc would have those
    headers — their absence proves the claim is hallucinated.
    Returns ``True`` only when:

    - body matches :data:`_EMBEDS_SPEC_BODY`
    - the file is readable
    - the file does NOT contain :data:`_SPEC_DOC_MARKERS`
    """
    body = comment.body or ""
    if not _EMBEDS_SPEC_BODY.search(body):
        return False
    if not comment.file:
        return False
    file_text = file_reader(comment.file)
    if file_text is None:
        return False
    return not _SPEC_DOC_MARKERS.search(file_text)


def _extract_missing_test_symbols(body: str) -> list[str]:
    """Return symbols a reviewer claims are missing from the test suite.

    Captures two shapes :

    - explicit ``test_<name>`` identifiers anywhere in the body
      (the dominant Tester pattern when suggesting a new test).
    - backtick-quoted identifiers immediately following one of the
      missing-test trigger phrases — caught by combining
      :data:`_MISSING_TEST_TRIGGER` with :data:`_BACKTICK_SYMBOL_PATTERN`.

    The combined set is de-duplicated while preserving discovery order.
    Returns ``[]`` when no trigger fires, so the guard skips the comment
    cheaply.

    Args:
        body: Reviewer comment body.

    Returns:
        Ordered, de-duplicated symbol names.
    """
    if not _MISSING_TEST_TRIGGER.search(body):
        return []
    seen: list[str] = []
    for match in _TEST_NAME_PATTERN.finditer(body):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    for match in _BACKTICK_SYMBOL_PATTERN.finditer(body):
        token = match.group(1)
        if token not in seen and (token.startswith("test_") or token.startswith("_")):
            seen.append(token)
    return seen


def _comment_falsely_claims_missing_test(
    comment: ReviewComment,
    *,
    symbol_searcher: SymbolSearcher,
) -> tuple[bool, list[str]]:
    """Return ``(True, resolved)`` when the comment cites tests already on disk.

    SP-CODER-EVIDENCE-CHALLENGE — Tester regularly recommends
    ``test_X`` to be added even when ``test_X`` ships in the same PR
    (the diff slice the bot sees missed the new test file).  This
    helper greps the worktree for the cited names and reports which
    ones resolve.

    Args:
        comment: Review comment under inspection.
        symbol_searcher: Worktree-aware searcher returning ``True`` when
            a symbol exists.

    Returns:
        Pair ``(disproved, resolved_symbols)``.  ``disproved`` is
        ``True`` iff at least one cited symbol resolves on disk.
    """
    symbols = _extract_missing_test_symbols(comment.body or "")
    if not symbols:
        return False, []
    resolved = [sym for sym in symbols if symbol_searcher(sym)]
    return bool(resolved), resolved


def _downgrade(comment: ReviewComment, reason: str) -> ReviewComment:
    """Return a copy of ``comment`` with severity ``nit`` and a guard tag."""
    new_body = (
        comment.body
        if comment.body.startswith(_DOWNGRADE_PREFIX)
        else (f"{_DOWNGRADE_PREFIX}({reason}) {comment.body}")
    )
    return replace(comment, severity="nit", body=new_body)


def apply_hallucination_guard(
    outcome: ReviewerOutcome,
    *,
    file_reader: FileReader,
    symbol_searcher: SymbolSearcher | None = None,
) -> tuple[ReviewerOutcome, list[GuardEvent]]:
    """Post-process one reviewer's outcome to downgrade hallucinated claims.

    Args:
        outcome: The reviewer's :class:`ReviewerOutcome`, as produced by
            :meth:`Reviewer.review_diff`.
        file_reader: Callable ``(relative_path) -> file_text | None``
            used to read files from the working tree to disprove
            "missing X" claims.  Returns ``None`` for unreadable paths
            (the comment is then left untouched — guard never fabricates
            a downgrade on missing evidence).
        symbol_searcher: Optional worktree-aware searcher used to
            disprove "no test for X" / "add test_Y" Tester
            hallucinations (SP-CODER-EVIDENCE-CHALLENGE).  When
            ``None`` (the legacy call shape) the missing-test branch
            is skipped and the comment passes through unchanged.

    Returns:
        A pair ``(outcome, events)``.  ``outcome`` carries the
        downgraded comments and, possibly, a softened verdict
        (``REQUEST_CHANGES`` → ``COMMENT`` when ≥50% of the
        blocker+major comments were downgraded).  ``events`` lists
        every downgrade with its reason for the orchestrator to
        persist and report.
    """
    if not outcome.comments:
        return outcome, []

    n_blocking_before = sum(1 for c in outcome.comments if c.severity in {"blocker", "major"})
    if n_blocking_before == 0:
        return outcome, []

    new_comments: list[ReviewComment] = []
    events: list[GuardEvent] = []

    for comment in outcome.comments:
        if comment.severity not in {"blocker", "major"}:
            new_comments.append(comment)
            continue

        if _comment_is_self_referential(comment):
            _log.info(
                "hallucination_guard.downgraded",
                role=outcome.role.value,
                file=comment.file,
                line=comment.line,
                reason="self_referential",
                original_severity=comment.severity,
            )
            new_comments.append(_downgrade(comment, "self_referential"))
            events.append(
                GuardEvent(
                    role=outcome.role,
                    file=comment.file,
                    line=comment.line,
                    original_severity=comment.severity,
                    reason="self_referential",
                    tokens_found=tuple(),
                    original_body=comment.body,
                )
            )
            continue

        if symbol_searcher is not None:
            disproved, resolved = _comment_falsely_claims_missing_test(
                comment,
                symbol_searcher=symbol_searcher,
            )
            if disproved:
                _log.info(
                    "hallucination_guard.downgraded",
                    role=outcome.role.value,
                    file=comment.file,
                    line=comment.line,
                    reason="missing_test_found_on_disk",
                    tokens_found=resolved,
                    original_severity=comment.severity,
                )
                new_comments.append(
                    _downgrade(comment, f"missing_test_found:{','.join(resolved)}"),
                )
                events.append(
                    GuardEvent(
                        role=outcome.role,
                        file=comment.file,
                        line=comment.line,
                        original_severity=comment.severity,
                        reason="missing_test_found_on_disk",
                        tokens_found=tuple(resolved),
                        original_body=comment.body,
                    ),
                )
                continue

        if _comment_falsely_claims_spec_embedded(comment, file_reader=file_reader):
            _log.info(
                "hallucination_guard.downgraded",
                role=outcome.role.value,
                file=comment.file,
                line=comment.line,
                reason="spec_embed_claim_unproven",
                original_severity=comment.severity,
            )
            new_comments.append(_downgrade(comment, "spec_embed_claim_unproven"))
            events.append(
                GuardEvent(
                    role=outcome.role,
                    file=comment.file,
                    line=comment.line,
                    original_severity=comment.severity,
                    reason="spec_embed_claim_unproven",
                    tokens_found=tuple(),
                    original_body=comment.body,
                )
            )
            continue

        tokens = _extract_missing_tokens(comment.body)
        if not tokens:
            new_comments.append(comment)
            continue

        file_text = file_reader(comment.file) if comment.file else None
        if file_text is None:
            new_comments.append(comment)
            continue

        disproved = [tok for tok in tokens if _file_contains_token(file_text, tok)]
        if not disproved:
            new_comments.append(comment)
            continue

        _log.info(
            "hallucination_guard.downgraded",
            role=outcome.role.value,
            file=comment.file,
            line=comment.line,
            reason="missing_token_found_in_file",
            tokens_found=disproved,
            original_severity=comment.severity,
        )
        new_comments.append(_downgrade(comment, f"missing_token_found:{','.join(disproved)}"))
        events.append(
            GuardEvent(
                role=outcome.role,
                file=comment.file,
                line=comment.line,
                original_severity=comment.severity,
                reason="missing_token_found_in_file",
                tokens_found=tuple(disproved),
                original_body=comment.body,
            )
        )

    n_downgraded = len(events)
    new_verdict = outcome.verdict
    if outcome.verdict == ReviewVerdict.REQUEST_CHANGES and n_downgraded * 2 >= n_blocking_before:
        n_remaining_blocking = sum(1 for c in new_comments if c.severity in {"blocker", "major"})
        if n_remaining_blocking == 0:
            _log.info(
                "hallucination_guard.verdict_softened",
                role=outcome.role.value,
                from_verdict=outcome.verdict.value,
                to_verdict=ReviewVerdict.COMMENT.value,
                n_downgraded=n_downgraded,
                n_blocking_before=n_blocking_before,
            )
            new_verdict = ReviewVerdict.COMMENT

    return replace(outcome, comments=new_comments, verdict=new_verdict), events


def make_repo_file_reader(repo_root: Path | str) -> FileReader:
    """Public helper to build a :class:`FileReader` for a repo root."""
    return _default_file_reader(Path(repo_root))


def _make_subtree_symbol_searcher(
    repo_root: Path | str, subtrees: tuple[str, ...]
) -> SymbolSearcher:
    """Build a SymbolSearcher scoped to the given top-level subtrees.

    Shared by :func:`make_repo_symbol_searcher` (``tests`` + ``src``) and
    :func:`make_tests_symbol_searcher` (``tests`` only,
    SP-MISSING-TEST-VERIFIER-SCOPE) so the scoping lives in one place and
    neither caller regresses when the other's subtree list changes.

    Args:
        repo_root: Path to the repo root the subtrees resolve against.
        subtrees: Top-level directory names to scan, relative to
            ``repo_root``.

    Returns:
        A :class:`SymbolSearcher` that returns ``True`` as soon as
        ``def <symbol>`` or ``class <symbol>`` appears in any ``.py``
        file under one of ``subtrees``.  No subprocess dependency —
        works in restricted environments without ``rg``.
    """
    root = Path(repo_root)

    def _search(symbol: str) -> bool:
        if not symbol or not symbol.isascii():
            return False
        def_pattern = re.compile(
            rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b",
            re.MULTILINE,
        )
        for sub in subtrees:
            sub_dir = root / sub
            if not sub_dir.is_dir():
                continue
            for path in sub_dir.rglob("*.py"):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    _log.debug(
                        "hallucination_guard.file_scan_skipped",
                        path=str(path),
                        error_type=type(exc).__name__,
                    )
                    continue
                if def_pattern.search(text):
                    return True
        return False

    return _search


def make_repo_symbol_searcher(repo_root: Path | str) -> SymbolSearcher:
    """Build a SymbolSearcher that greps ``tests/`` and ``src/`` for a symbol.

    The searcher iterates over the existing Python files under both
    trees and returns ``True`` as soon as ``def <symbol>`` or
    ``class <symbol>`` appears.  No subprocess dependency — works in
    restricted environments without ``rg``.

    Args:
        repo_root: Path to the repo root containing ``tests/`` and ``src/``.

    Returns:
        A :class:`SymbolSearcher` ready for
        :func:`apply_hallucination_guard`.
    """
    return _make_subtree_symbol_searcher(repo_root, ("tests", "src"))


def make_tests_symbol_searcher(repo_root: Path | str) -> SymbolSearcher:
    """Build a SymbolSearcher that greps ``tests/`` ONLY for a symbol.

    SP-MISSING-TEST-VERIFIER-SCOPE : the missing-test verifier must
    never let a symbol's existence in ``src/`` (the code under review)
    refute a "no test for X" claim.  This searcher scopes the grep to
    ``tests/`` so only a genuine covering test counts as evidence.

    Args:
        repo_root: Path to the repo root containing ``tests/``.

    Returns:
        A :class:`SymbolSearcher` scoped to ``tests/`` only.
    """
    return _make_subtree_symbol_searcher(repo_root, ("tests",))
