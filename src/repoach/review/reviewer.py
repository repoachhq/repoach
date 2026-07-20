"""Review-bot base classes + 4 reviewer roles + Coder owner.

Each bot wraps :class:`~repoach.agent_engine.agent_loop.AgentLoop`
with a role-specific persona prompt loaded from ``prompts/review/``.
The reviewers run in **one-shot JSON mode** (no tool calls) — they
read a diff, return a verdict + comments — fast, predictable, cheap.

The :class:`Coder` owner is also one-shot today (it produces a
``fixes[]`` JSON plan that the orchestrator applies); a future
iteration can give it tool access to invoke Read/Edit/Bash for
deeper investigation, but the simpler one-shot path keeps the loop
NIM-only and cost-bounded.

Why no MCP tool exposure here:
  Per the security stance, the review bots do **not** need access to
  production data or mutating tools.  Their input is the PR diff —
  that is enough.  Exposing MCP tools to a network-boundary external
  reviewer is a separate hardening spec (SP-MCP-EXT).
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent_engine.agent_loop import (
    DEFAULT_REVIEWER_CHAIN,
    LONG_OUTPUT_TIMEOUT_S,
    PROXY_OPUS_CHAIN,
    PROXY_SONNET_CHAIN,
    AgentLoop,
)
from ..core.logging import get_logger
from .diff_scoper import scope_diff
from .findings import Finding

if TYPE_CHECKING:
    from .devagent_loop import DevLoopResult
    from .thread_context import ResolvedDisagreement

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"

_DIFF_HARD_CAP_CHARS = 140_000
"""Hard cap on the diff text substituted into reviewer prompts.

Raised from 60_000 after PR #323 (SP-PLANNER-AGENT, 67K-char diff):
the silent truncation cut the persona file mid-diff and THREE
reviewers convergently hallucinated the same blocker about content
that sat just past the cut ("persona embeds the spec instead of the
{SPEC_PLAN} placeholder" — the placeholder was there, beyond the
truncation point). 140K chars ≈ 35K tokens, comfortable for every
chain candidate, and large enough that a builder-sized slice fits
whole. The truncation note stays appended when the cap does hit, so
reviewers at least KNOW they saw a partial diff."""

_TRANSPORT_ERROR_RE = re.compile(
    r"(?:Error code:\s*\d{3}"
    r"|HTTP/[\d.]+\s+(?:4|5)\d{2}"
    r"|end of life"
    r"|Connection error"
    r"|RemoteProtocolError"
    r"|TimeoutException"
    r"|Request timed out"
    r"|request_id=req_[A-Za-z0-9]+)",
    re.IGNORECASE,
)


class BotRole(enum.StrEnum):
    """Identifier for each persona in the review team."""

    ARCHITECT = "architect"
    SENTINEL = "sentinel"
    TESTER = "tester"
    SCRIBE = "scribe"
    CODER = "coder"
    DEVELOPER = "developer"
    PLANNER = "planner"


class ReviewVerdict(enum.StrEnum):
    """Aggregable verdict each reviewer returns."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class ReviewComment:
    """One actionable comment a reviewer may post on a PR diff line.

    Attributes:
        file: Repo-relative path the comment targets.
        line: 1-based line number in the diff.
        severity: One of ``"blocker"``, ``"major"``, ``"minor"``, ``"nit"``.
        body: Free-form prose surfaced to the PR author.
    """

    file: str
    line: int
    severity: str
    body: str


@dataclass(frozen=True)
class _FailedRunResult:
    """Stub return value for :meth:`Reviewer._call_with_retry` after exhaustion.

    Mimics the surface of :class:`NimAgentOutput` so the outer
    :class:`ReviewerOutcome` can still report ``model_used`` /
    ``elapsed_s`` / ``tokens_used`` / ``trace`` defaults when every
    retry attempt raised before producing a real response.  Used only
    on the transport-exhausted fall-through path of
    SP-REVIEWER-RETRY-WITH-BACKOFF.

    The ``trace`` slot is an empty tuple rather than a list so the
    surface stays hashable (matches ``@dataclass(frozen=True)``) ;
    callers iterate it the same way they iterate the real list.
    """

    error: str
    text: str = ""
    model_used: str = "exhausted"
    elapsed_s: float = 0.0
    tokens_used: int = 0
    trace: tuple[dict[str, Any], ...] = ()


@dataclass
class ReviewerOutcome:
    """Aggregated result of one reviewer's pass over a PR.

    Attributes:
        role: Which bot produced this outcome.
        verdict: APPROVE / REQUEST_CHANGES / COMMENT.
        summary: Short one-paragraph judgement.
        comments: Inline diff comments with severity.
        model_used: NIM-hosted model that actually answered (chain failover
            may have promoted a fallback).
        elapsed_s: Wall-clock time of the call.
        tokens_used: Approximate total tokens.
        raw_response: Raw model output string (for audit / debugging).
        trace: SP-REVIEWER-TRACE-FIELD — per-candidate chain-failover
            trace forwarded from
            :attr:`~repoach.agent_engine.agent_loop.NimAgentOutput.trace`.
            Each entry is a flat dict with ``attempt`` ordinal,
            ``model``, ``outcome``, ``elapsed_ms``, and a truncated
            ``error_message`` for non-success outcomes.  Empty when
            the run fell through to a transport-level stub.  Surfaced
            in the CLI JSON payload so operators can audit which
            chain link each reviewer landed on without grepping
            structlog.
    """

    role: BotRole
    verdict: ReviewVerdict
    summary: str
    comments: list[ReviewComment] = field(default_factory=list)
    model_used: str = ""
    elapsed_s: float = 0.0
    tokens_used: int = 0
    raw_response: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PeerOutcome:
    """One peer reviewer's round-1 verdict + 200-char summary, fed back during round 2.

    Attributes:
        role: Which peer this summary represents.
        verdict: That peer's round-1 verdict.
        summary: Truncated 200-char digest of the peer's prose.
    """

    role: BotRole
    verdict: ReviewVerdict
    summary: str


@dataclass(frozen=True)
class FileExcerpt:
    r"""A 30-line window read fresh from disk around a cited file:line.

    Attributes:
        file: Repo-relative path the excerpt was read from.
        center_line: The line the reviewer cited (1-based).
        start_line: First line included in the excerpt (1-based).
        text: The excerpt body, ``\n``-joined.
    """

    file: str
    center_line: int
    start_line: int
    text: str


@dataclass(frozen=True)
class GuardEventSummary:
    """One hallucination-guard verdict on a reviewer's own comment.

    Fed back to the reviewer in round 2 so it can confirm or retract.

    Attributes:
        comment_index: Position of the affected comment in round-1 output.
        reason: Guard's downgrade reason (e.g. ``"missing-X verified present"``).
    """

    comment_index: int
    reason: str


@dataclass(frozen=True)
class DialogueContext:
    """Round-2 context bundle handed back to a reviewer for self-revision.

    Reviewers receive this when the orchestrator re-invokes them after
    the parallel round-1 pass.

    Attributes:
        peer_outcomes: 200-char summary of every OTHER reviewer's round-1
            view (verdict + digest).  Excludes the reviewer's own outcome.
        own_guard_events: The runner's downgrade verdicts on this reviewer's
            own round-1 comments — what the hallucination guard caught.
        file_excerpts: 30-line windows around each ``file:line`` this
            reviewer cited in round 1, read fresh from the repo on disk.
    """

    peer_outcomes: tuple[PeerOutcome, ...] = ()
    own_guard_events: tuple[GuardEventSummary, ...] = ()
    file_excerpts: tuple[FileExcerpt, ...] = ()


@dataclass(frozen=True)
class PriorReviewContext:
    """Cross-invocation context from a reviewer's most recent prior verdict.

    Attributes:
        role: The reviewer role this context applies to.
        verdict: The verdict cast in the prior invocation.
        summary: Truncated summary from the prior invocation.
        n_comments: Number of comments in the prior review.
        diff_changed: True if the diff hash differs from the prior, False
            if identical, None if the prior entry had no diff_hash.
    """

    role: BotRole
    verdict: ReviewVerdict
    summary: str
    n_comments: int
    diff_changed: bool | None


def _render_prior_review(ctx: PriorReviewContext) -> str:
    """Render :class:`PriorReviewContext` into a Markdown block for the prompt."""
    verdict_str = ctx.verdict.value
    if ctx.diff_changed is False:
        delta = "The diff has **NOT changed** since your prior review."
        instruction = (
            "Confirm your prior verdict. Do NOT introduce new concerns on an unchanged diff."
        )
    elif ctx.diff_changed is True:
        delta = "The diff **HAS changed** since your prior review."
        instruction = (
            "Re-evaluate the new diff on its merits — your prior concerns may have been addressed."
        )
    else:
        delta = "Diff change status unknown (no prior hash recorded)."
        instruction = "Review the diff on its merits."
    return (
        f"In your last review you gave **{verdict_str}** with "
        f"{ctx.n_comments} comment(s).\n"
        f"{delta}\n\n{instruction}"
    )


def _render_dialogue_context(ctx: DialogueContext) -> str:
    """Render :class:`DialogueContext` into a Markdown block for the prompt."""
    parts: list[str] = []
    if ctx.peer_outcomes:
        parts.append("### Peer reviewer outcomes (round 1)")
        for peer in ctx.peer_outcomes:
            parts.append(f"- **{peer.role.value}** {peer.verdict.value}: {peer.summary}")
        parts.append("")
    if ctx.own_guard_events:
        parts.append("### Hallucination guard verdicts on YOUR round-1 comments")
        for ev in ctx.own_guard_events:
            parts.append(f"- comment #{ev.comment_index}: {ev.reason}")
        parts.append("")
    if ctx.file_excerpts:
        parts.append("### Live file excerpts around your cited lines")
        for ex in ctx.file_excerpts:
            parts.append(f"- `{ex.file}:{ex.center_line}` (lines {ex.start_line}+):")
            parts.append("```")
            parts.append(ex.text)
            parts.append("```")
        parts.append("")
    if not parts:
        return "_(round 2 invoked but no peer / guard / file context attached)_"
    parts.append(
        "Confirm or RETRACT each of your round-1 comments based on the "
        "above evidence.  A comment with severity `retracted` is dropped "
        "from the final aggregation.  You may add ONE new comment that "
        "addresses something a peer surfaced."
    )
    return "\n".join(parts)


def make_file_excerpts(
    comments: Iterable[ReviewComment],
    *,
    repo_root: Path,
    window: int = 15,
) -> tuple[FileExcerpt, ...]:
    """Read ``window`` lines on each side of every cited ``file:line``.

    Skips files that are missing or fail to read; no exception leaves
    this helper.  ``window=15`` yields a 30-line excerpt centered on
    ``comment.line`` (15 above + the cited line + 14 below).

    Args:
        comments: Round-1 comments whose cited lines should be excerpted.
        repo_root: Absolute path of the repo working tree.
        window: Lines on each side of ``comment.line``.

    Returns:
        A tuple of :class:`FileExcerpt` in the order the comments
        appear, deduplicated on ``(file, center_line)``.
    """
    seen: set[tuple[str, int]] = set()
    out: list[FileExcerpt] = []
    for comment in comments:
        key = (comment.file, comment.line)
        if not comment.file or comment.line <= 0 or key in seen:
            continue
        seen.add(key)
        target = (repo_root / comment.file).resolve()
        try:
            target.relative_to(repo_root.resolve())
        except (ValueError, OSError) as exc:
            _log.debug(
                "reviewer.excerpt_path_outside_root",
                file=comment.file,
                error_type=type(exc).__name__,
            )
            continue
        if not target.is_file():
            continue
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            _log.debug(
                "reviewer.excerpt_read_failed",
                path=str(target),
                error_type=type(exc).__name__,
            )
            continue
        start = max(1, comment.line - window)
        end = min(len(lines), comment.line + window - 1)
        excerpt_text = "\n".join(
            f"{ln_no:5d}  {lines[ln_no - 1]}" for ln_no in range(start, end + 1)
        )
        out.append(
            FileExcerpt(
                file=comment.file,
                center_line=comment.line,
                start_line=start,
                text=excerpt_text,
            )
        )
    return tuple(out)


class Reviewer:
    """Base class for the four reviewer roles.

    Subclasses pick their persona file + their NIM-hosted model chain.
    The base class drives the actual call : read prompt, substitute
    diff, one-shot JSON-mode call, parse, return
    :class:`ReviewerOutcome`.

    The ``max_tokens=4096`` default is sized for reasoning models
    (deepseek-reasoner, kimi-thinking, ...) that emit a long chain-of-
    thought BEFORE the structured JSON.  At the historic 1500 cap we
    observed live truncation mid-JSON (PR #93 archive : Architect
    ``tokens_used=4395``, JSON cut mid-summary), the parser falling
    back to a fake ``COMMENT`` verdict, and the consensus gate
    slipping a malformed review through.  4096 keeps the reviewer
    under the ~8 k per-call cap of the cheapest providers (Cerebras,
    Groq).
    """

    role: BotRole = BotRole.ARCHITECT
    persona_filename: str = "architect_0.5.1.md"
    model_chain: tuple[str, ...] = DEFAULT_REVIEWER_CHAIN
    max_tokens: int = 4096
    temperature: float = 0.1
    _RETRY_BACKOFFS_S: tuple[float, ...] = (0.0, 30.0, 90.0)
    """Backoff schedule used by :meth:`_call_with_retry`.

    SP-REVIEWER-RETRY-WITH-BACKOFF — ports the
    :class:`Developer._RETRY_BACKOFFS_S` pattern to the four reviewer
    roles so a single parse-failed pass no longer poisons the
    consensus.  The first attempt fires immediately ; subsequent
    attempts back off 30 s then 90 s to give NIM endpoints time to
    recover from transient flakes (rate limit, semantic-garbage
    response, momentary chain-wide outage).
    """

    def __init__(
        self,
        *,
        loop: AgentLoop | None = None,
        db_path: Path | None = None,
    ) -> None:
        """Create the reviewer with an optional pre-built AgentLoop.

        Args:
            loop: A pre-configured :class:`AgentLoop`.  When
                ``None``, a new one is built using ``self.model_chain``.
            db_path: Optional path to the findings ledger database.
                When provided, the reviewer appends its own refuted
                track record to each prompt so the lens sees its past
                refutations.  ``None`` (default) skips the section.
        """
        self._db_path = db_path
        if loop is not None:
            self._loop = loop
        else:
            self._loop = AgentLoop(
                model_chain=self.model_chain,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        from .mcp_whitelist import allowed_tools_for

        self._allowed_tools: tuple[str, ...] = allowed_tools_for(self.role)

    def _call_with_retry(
        self,
        prompt: str,
        *,
        pr_number: int | None,
    ) -> tuple[ReviewVerdict, str, list[ReviewComment], Any]:
        """Drive ``run_oneshot`` + ``_parse_response`` with retry-with-backoff.

        SP-REVIEWER-RETRY-WITH-BACKOFF — ports the
        :class:`Developer._call_with_retry` pattern so a single
        parse-failed pass (transient transport flake, whitespace-only
        upstream, semantic-garbage response) doesn't poison the
        consensus.  Two retry triggers are honoured :

        1. :meth:`AgentLoop.run_oneshot` raised any exception (the
           chain failover inside ``run_oneshot`` already handled
           per-model errors ; reaching this layer means the whole
           chain exhausted at the network layer).
        2. :meth:`_parse_response` returned a ``[parse_failed:…]``
           summary (MALFORMED / TRUNCATED / TRANSPORT) — the proxy
           succeeded transport-wise but the response was unusable.

        After ``len(_RETRY_BACKOFFS_S)`` attempts exhaust, the last
        observed outcome is returned : a clean parse if any attempt
        produced one, otherwise the final parse-failed marker so the
        consensus gate still sees the loud refusal (per
        [[silent-merge-layered-root-cause]] — never silently merge).

        Args:
            prompt: The fully-rendered prompt to feed to
                ``run_oneshot``.
            pr_number: Optional PR identifier, used only for
                structured logging.

        Returns:
            A 4-tuple ``(verdict, summary, comments, result)``.
            ``result`` is the :class:`NimAgentOutput` (or equivalent)
            of the last attempt — never ``None``, because on full
            transport exhaustion we synthesise a stub
            :class:`_FailedRunResult` carrying the last exception
            string so the outer ``ReviewerOutcome`` can still report
            ``model_used`` / ``elapsed_s`` / ``tokens_used`` defaults.
        """
        import time as _time

        last_error: Exception | None = None
        last_outcome: tuple[ReviewVerdict, str, list[ReviewComment], Any] | None = None
        for attempt, wait_s in enumerate(self._RETRY_BACKOFFS_S, start=1):
            if wait_s > 0:
                _log.info(
                    "review.bot.retry_wait",
                    role=self.role.value,
                    pr_number=pr_number,
                    attempt=attempt,
                    wait_s=wait_s,
                    last_error=type(last_error).__name__ if last_error else None,
                )
                _time.sleep(wait_s)
            try:
                result = self._loop.run_oneshot(
                    prompt,
                    json_response=True,
                    accept_response=self._response_is_parsable,
                )
            except Exception as exc:
                last_error = exc
                _log.warning(
                    "review.bot.attempt_failed",
                    role=self.role.value,
                    pr_number=pr_number,
                    attempt=attempt,
                    of=len(self._RETRY_BACKOFFS_S),
                    error=type(exc).__name__,
                    message=str(exc)[:200],
                )
                continue
            verdict, summary, comments = self._parse_response(result.text)
            last_outcome = (verdict, summary, comments, result)
            if not summary.startswith("[parse_failed:"):
                return last_outcome
            _log.warning(
                "review.bot.parse_failed_retry",
                role=self.role.value,
                pr_number=pr_number,
                attempt=attempt,
                of=len(self._RETRY_BACKOFFS_S),
                marker=summary[:80],
            )

        if last_outcome is not None:
            _log.error(
                "review.bot.exhausted_returning_last_parse_failed",
                role=self.role.value,
                pr_number=pr_number,
                attempts=len(self._RETRY_BACKOFFS_S),
                marker=last_outcome[1][:80],
            )
            return last_outcome

        _log.error(
            "review.bot.exhausted_transport_exception",
            role=self.role.value,
            pr_number=pr_number,
            attempts=len(self._RETRY_BACKOFFS_S),
            final_error=type(last_error).__name__ if last_error else "Unknown",
        )
        stub = _FailedRunResult(error=str(last_error) if last_error else "Unknown")
        return (
            ReviewVerdict.COMMENT,
            f"[parse_failed:TRANSPORT] all {len(self._RETRY_BACKOFFS_S)} attempts raised — last={type(last_error).__name__ if last_error else 'Unknown'}",
            [],
            stub,
        )

    def review_diff(
        self,
        diff: str,
        *,
        pr_number: int | None = None,
        spec_plan: str | None = None,
        dialogue_context: DialogueContext | None = None,
        resolved_disagreements: tuple[ResolvedDisagreement, ...] | None = None,
        prior_review: PriorReviewContext | None = None,
        extra_prompt_section: str = "",
        arch_edges: str = "",
    ) -> ReviewerOutcome:
        """Send the diff to the model and parse the JSON-shaped response.

        Args:
            diff: Unified-diff text for the PR.
            pr_number: Optional PR identifier, used only for logging.
            spec_plan: Optional spec doc (spec)
                that drove the PR.  When provided, substituted into the
                prompt's ``{SPEC_PLAN}`` placeholder so the reviewer
                verifies the diff against the spec instead of reviewing
                in a vacuum.  SP-DEV-V2-B2.
            dialogue_context: Optional round-2 context (peer outcomes +
                this reviewer's hallucination-guard verdicts so far +
                live file excerpts around cited lines).  When provided,
                the prompt's ``{DIALOGUE_CONTEXT}`` placeholder is
                substituted with the rendered dialogue block; the
                reviewer is then expected to confirm-or-retract its
                round-1 comments.  SP-REVIEW-DIALOGUE-A.
            resolved_disagreements: Optional tuple of prior threads on
                this PR where the Coder bot has already posted a
                ``"Verified — challenge with evidence"`` reply against
                this reviewer's root comments.  Substituted into the
                prompt's ``{RESOLVED_DISAGREEMENTS}`` placeholder so the
                reviewer must refute the cited evidence before re-
                raising the same critique.  SP-CODER-EVIDENCE-CHALLENGE
                phase 3B.
            prior_review: Cross-invocation context from this reviewer's
                most recent prior verdict.  Substituted into the
                prompt's ``{PRIOR_REVIEW}`` placeholder.
                SP-REVIEW-CONVERGENCE-RATCHET.
            extra_prompt_section: Pre-rendered markdown appended verbatim
                AFTER all placeholder substitution (so it can never be
                injected through diff content) — used by the orchestrator
                for the review-scoped agentmemory lessons.
                SP-REVIEW-MEMORY.
            arch_edges: The declared-dependency block substituted into the
                Architect persona's ``{ARCH_EDGES}`` placeholder so it can
                review tier-2 couplings; empty for a frontier/no-spec PR and
                a no-op for non-Architect personas. SP-ARCH-REVIEW-WIRE.

        Returns:
            A :class:`ReviewerOutcome` with the parsed fields and the
            raw model response (truncated where needed).
        """
        scoped = scope_diff(diff, _DIFF_HARD_CAP_CHARS)
        if scoped.omitted:
            _log.warning(
                "review.diff_scoped",
                role=self.role.value,
                n_included=len(scoped.included),
                n_omitted=len(scoped.omitted),
                omitted=scoped.omitted[:10],
            )

        prompt = (
            self._render_prompt(
                scoped.prompt_diff,
                spec_plan=spec_plan,
                dialogue_context=dialogue_context,
                resolved_disagreements=resolved_disagreements,
                prior_review=prior_review,
                arch_edges=arch_edges,
            )
            + extra_prompt_section
        )

        if self._db_path is not None:
            from .review_lessons import render_lens_track_record

            track = render_lens_track_record(self._db_path, self.role.value)
            if track:
                prompt += "\n\n" + track

        _log.info(
            "review.bot.spawn",
            role=self.role.value,
            pr_number=pr_number,
            diff_chars=len(diff),
            round=2 if dialogue_context is not None else 1,
        )
        verdict, summary, comments, result = self._call_with_retry(
            prompt,
            pr_number=pr_number,
        )
        outcome = ReviewerOutcome(
            role=self.role,
            verdict=verdict,
            summary=summary,
            comments=comments,
            model_used=result.model_used,
            elapsed_s=result.elapsed_s,
            tokens_used=result.tokens_used,
            raw_response=result.text[:8000],
            trace=list(getattr(result, "trace", ()) or ()),
        )
        _log.info(
            "review.bot.done",
            role=self.role.value,
            verdict=outcome.verdict.value,
            n_comments=len(outcome.comments),
            elapsed_s=outcome.elapsed_s,
            model_used=outcome.model_used,
        )
        return outcome

    def _response_is_parsable(self, response: Any) -> bool:
        """Predicate for :meth:`AgentLoop.run_oneshot.accept_response`.

        SP-PROXY-SEMANTIC-FAILOVER : the AgentLoop calls this with
        the raw :class:`AgentResponse` after each candidate.  When
        :meth:`_parse_response` would emit a ``[parse_failed:…]``
        summary (whitespace-only, malformed JSON, truncated JSON), we
        return ``False`` so the loop adds the current candidate to its
        skip-list and walks to the next chain link.  Returning
        ``True`` lets the loop accept the response and forward it to
        the reviewer's outer parsing path unchanged.

        Args:
            response: The :class:`AgentResponse` the proxy returned
                for the current candidate.

        Returns:
            ``True`` if the response parses cleanly (no parse-failed
            marker) ; ``False`` otherwise, which signals the
            AgentLoop to walk to the next chain candidate.
        """
        text_parts: list[str] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        raw = "".join(text_parts)
        _, summary, _ = self._parse_response(raw)
        return not summary.startswith("[parse_failed:")

    def _render_prompt(
        self,
        diff: str,
        *,
        spec_plan: str | None = None,
        dialogue_context: DialogueContext | None = None,
        resolved_disagreements: tuple[ResolvedDisagreement, ...] | None = None,
        prior_review: PriorReviewContext | None = None,
        arch_edges: str = "",
    ) -> str:
        """Read the persona file and substitute the placeholders.

        Substitutes ``{DIFF}``, ``{SPEC_PLAN}``, ``{DIALOGUE_CONTEXT}``,
        ``{RESOLVED_DISAGREEMENTS}``, ``{PRIOR_REVIEW}``, and ``{ARCH_EDGES}``
        (the declared-dependency block; only the Architect persona carries
        that placeholder, so the substitution is a no-op for the others).
        Every placeholder gets a fallback stub when its source is empty.
        """
        if spec_plan:
            spec_block = spec_plan[:_DIFF_HARD_CAP_CHARS]
        else:
            spec_block = "_(no spec context — review the diff on its own merits)_"

        if dialogue_context is not None:
            dialogue_block = _render_dialogue_context(dialogue_context)
        else:
            dialogue_block = "_(round 1 — no peer outcomes, no guard verdicts yet)_"

        from .thread_context import render_resolved_disagreements_block

        disagreements_block = render_resolved_disagreements_block(
            list(resolved_disagreements or ())
        )

        if prior_review is not None:
            prior_block = _render_prior_review(prior_review)
        else:
            prior_block = "_(first review of this PR — no prior context)_"

        path = _PROMPTS_DIR / self.persona_filename
        template = path.read_text(encoding="utf-8")
        return (
            template.replace("{DIFF}", diff)
            .replace("{SPEC_PLAN}", spec_block)
            .replace("{DIALOGUE_CONTEXT}", dialogue_block)
            .replace("{RESOLVED_DISAGREEMENTS}", disagreements_block)
            .replace("{PRIOR_REVIEW}", prior_block)
            .replace("{ARCH_EDGES}", arch_edges)
        )

    def _parse_response(self, raw: str) -> tuple[ReviewVerdict, str, list[ReviewComment]]:
        """Tolerant JSON parse: extract the first ``{...}`` block.

        Models occasionally wrap the JSON in fences or add prose around
        it.  We scan for the largest ``{...}`` blob that parses as a
        dict with the expected keys.

        Comment-severity strings on each parsed comment are normalised
        to one of ``{"blocker", "major", "minor", "nit", "retracted"}``
        ; ``retracted`` is the round-2 self-revision marker — the
        orchestrator drops those from the final aggregation but keeps
        them in the audit trail.

        Failure handling :

        * **Parsed but no ``verdict`` key** — emit ``REQUEST_CHANGES``
          with a structured ``[parse_failed:…]`` marker.  The historic
          silent fallback to ``COMMENT`` let truncated reasoner output
          (output capped at 1500 tokens with a chain-of-thought
          reasoner emitting > 2k tokens) silently merge ; failing loud
          keeps the consensus gate strict.
        * **Truncation** — when the response started like JSON (first
          non-whitespace char is ``{``, optionally preceded by markdown
          fences) but never reached a closing ``}``, we mark the
          outcome as ``TRUNCATED`` (verdict = REQUEST_CHANGES).  That
          is the signature of a max_tokens cut mid-emission.
        * **Transport** — when the raw matches
          :data:`_TRANSPORT_ERROR_RE` or is empty / whitespace-only,
          we mark the outcome as ``TRANSPORT`` (verdict = COMMENT,
          non-blocking).  Transport failures (HTTP 410, "end of life",
          ``Request timed out``, ``Connection error``,
          ``RemoteProtocolError``, ``TimeoutException``,
          ``request_id=req_*`` envelopes) are infra issues, not code
          critiques ; the arbiter (see ``coder_loop``) drops these
          outcomes before Coder dispatch.  An empty / whitespace
          ``raw`` (Kimi K2.6 returned a single space on PR #136 / #137
          Sentinel passes) classifies as TRANSPORT for the same
          reason — no review content was produced.
        * **Malformed** — every other parse failure (verdict =
          REQUEST_CHANGES).
        """
        candidates = re.findall(r"\{[\s\S]*\}", raw)
        candidates.sort(key=len, reverse=True)
        for blob in candidates:
            try:
                data = json.loads(blob)
            except json.JSONDecodeError as exc:
                _log.debug("reviewer.verdict_blob_unparseable", error=str(exc)[:120])
                continue
            if not isinstance(data, dict):
                continue
            if "verdict" not in data:
                continue
            verdict_raw = str(data.get("verdict", "")).upper()
            try:
                verdict = ReviewVerdict(verdict_raw)
            except ValueError:
                verdict = ReviewVerdict.COMMENT
            summary = str(data.get("summary", ""))[:240]
            comments_raw = data.get("comments", []) or []
            comments: list[ReviewComment] = []
            for c in comments_raw:
                if not isinstance(c, dict):
                    continue
                file = str(c.get("file", ""))
                if not file:
                    continue
                try:
                    line = int(c.get("line", 0))
                except (TypeError, ValueError) as exc:
                    _log.debug(
                        "reviewer.bad_comment_line",
                        raw=str(c.get("line"))[:50],
                        error=str(exc)[:120],
                    )
                    continue
                severity = str(c.get("severity", "minor")).lower()
                if severity not in {"blocker", "major", "minor", "nit", "retracted"}:
                    severity = "minor"
                body = str(c.get("body", ""))[:400]
                comments.append(ReviewComment(file=file, line=line, severity=severity, body=body))
            return verdict, summary, comments

        stripped = raw.lstrip()
        for fence in ("```json", "```"):
            if stripped.startswith(fence):
                stripped = stripped[len(fence) :].lstrip()
        looks_truncated = stripped.startswith("{") and not raw.rstrip().endswith("}")

        looks_transport_error = bool(_TRANSPORT_ERROR_RE.search(raw)) or not raw.strip()

        _log.warning(
            "review.bot.parse_failed",
            role=self.role.value,
            raw_preview=raw[:200],
            raw_chars=len(raw),
            looks_truncated=looks_truncated,
            looks_transport_error=looks_transport_error,
        )

        if looks_transport_error:
            marker = "TRANSPORT"
            verdict = ReviewVerdict.COMMENT
        elif looks_truncated:
            marker = "TRUNCATED"
            verdict = ReviewVerdict.REQUEST_CHANGES
        else:
            marker = "MALFORMED"
            verdict = ReviewVerdict.REQUEST_CHANGES

        return (
            verdict,
            f"[parse_failed:{marker}] raw_preview={raw[:160]!r}",
            [],
        )


class Architect(Reviewer):
    """Architecture / coupling / naming / SoC reviewer.

    Routes through ``PROXY_SONNET_CHAIN`` so the proxy handles
    upstream failover while still keeping NIM as a candidate.
    """

    role = BotRole.ARCHITECT
    persona_filename = "architect_0.5.1.md"
    model_chain = PROXY_SONNET_CHAIN


class Sentinel(Reviewer):
    """Security reviewer — secrets, gates, subprocess, prompt injection.

    Routes through ``PROXY_OPUS_CHAIN`` because security review needs
    the highest reasoning tier.  ``max_tokens`` deliberately inherits
    the 4096 base default : the opus chain is where reasoning models
    (chain-of-thought before the JSON) are most likely, and a stale
    ``max_tokens = 2000`` override — a leftover from the era of the
    1500 base cap, when 2000 was a bump — had left the security bench
    with the LOWEST budget of the four reviewers, re-exposing the
    mid-JSON truncation class the 4096 base fixed (see the base-class
    docstring, PR #93).
    """

    role = BotRole.SENTINEL
    persona_filename = "sentinel_0.4.3.md"
    model_chain = PROXY_OPUS_CHAIN


class Tester(Reviewer):
    """Test coverage / edge case / fixture reviewer."""

    role = BotRole.TESTER
    persona_filename = "tester_0.4.1.md"
    model_chain = PROXY_SONNET_CHAIN


class Scribe(Reviewer):
    """Docstring / commit message / README consistency reviewer."""

    role = BotRole.SCRIBE
    persona_filename = "scribe_0.5.1.md"
    model_chain = PROXY_SONNET_CHAIN


class Coder:
    """Owner of the branch — resolves open findings into a JSON fix-plan.

    The Coder consumes the structured open blocking findings the merge
    gate is holding a PR on (each already verified mechanically or by the
    adversarial refuter) and emits a list of targeted fixes the runner
    will apply.  The Coder itself does NOT execute git operations — it
    only proposes them.  The single live entry point is
    :meth:`respond_to_findings` (``coder_findings_0.1.1.md``);
    ``fixes[].new_content`` is the complete UTF-8 contents of the file at
    ``fixes[].path`` after the fix.  Diff-hunk patches were tried in an
    early revision and proved too fragile to apply reliably (context
    drift, whitespace, line numbering across revisions).

    The persona enforces **patch discipline**: the Coder must emit minimal
    targeted edits (preserve all lines unrelated to the finding).  The
    runner backs this with a 40% size-delta guard — patches that change a
    file's line count by more than 40% are rejected as full-file rewrites
    (SP-CODER-TARGETED-PATCH).

    The ``max_tokens=16000`` budget is sized for a fix touching several
    files at once: earlier revisions at 8 000 tokens truncated the
    trailing ``}`` and tripped ``review.coder.parse_failed``.  16 000
    covers ~3-4 mid-size files per response while staying under the
    per-call NIM cap.
    """

    role = BotRole.CODER
    model_chain = PROXY_SONNET_CHAIN
    max_tokens = 16000
    temperature = 0.05

    def __init__(
        self,
        *,
        loop: AgentLoop | None = None,
        logs_dir: Path | None = None,
    ) -> None:
        """Create the Coder bot with an optional pre-built AgentLoop.

        Args:
            loop: Optional pre-built AgentLoop (tests inject a mock).
            logs_dir: Optional override for the on-disk dump directory
                used by :func:`_persist_parse_failed_response` when the
                model returns an unparseable response.  Default
                ``None`` keeps the production behaviour (repo's
                ``logs/``).  Tests pass ``tmp_path`` to keep parse
                failures off the working tree —
                SP-DEV-TEST-POLLUTION-LOGS-DIR.
        """
        if loop is not None:
            self._loop = loop
        else:
            self._loop = AgentLoop(
                model_chain=self.model_chain,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        self._logs_dir = logs_dir
        from .mcp_whitelist import allowed_tools_for

        self._allowed_tools: tuple[str, ...] = allowed_tools_for(self.role)

    def respond_to_findings(
        self,
        *,
        findings: list[Finding],
        diff: str,
        pr_number: int | None = None,
        spec_plan: str | None = None,
    ) -> dict[str, Any]:
        """Produce a JSON fix-plan that resolves a set of open findings.

        The findings-driven counterpart to :meth:`respond` (redesign
        slice 8a). Instead of reviewer comments + a challenge pass, the
        Coder is handed the structured open findings the merge gate is
        blocking on — each already verified by a mechanical check or
        the adversarial refuter, so there is nothing left to challenge.
        Its job is a targeted fix per finding; resolution is re-verified
        at head by :func:`coder_findings.reverify_resolution_for_pr`.

        Args:
            findings: The open blocking findings to resolve. Rendered
                into the prompt's ``{FINDINGS_JSON}`` placeholder.
            diff: The PR diff (context for the fix).
            pr_number: Optional PR identifier for logging.
            spec_plan: Optional spec doc, substituted into
                ``{SPEC_PLAN}`` so the fix stays spec-aware.

        Returns:
            A dict with keys ``fixes``, ``commit_message``, ``summary``
            (plus model telemetry). On parse failure, ``fixes`` is empty
            and ``summary`` carries the raw head — same contract as
            :meth:`respond`.
        """
        truncated_diff = diff[:_DIFF_HARD_CAP_CHARS]
        if len(diff) > _DIFF_HARD_CAP_CHARS:
            truncated_diff += "\n[... diff truncated ...]\n"

        findings_payload = [
            {
                "id": f.id,
                "file": f.file,
                "line_start": f.line_start,
                "line_end": f.line_end,
                "claim_type": f.claim_type.value,
                "severity": f.severity.value,
                "claim": f.claim,
                "evidence_pointer": f.evidence_pointer,
            }
            for f in findings
        ]
        findings_json = json.dumps(findings_payload, indent=2)

        if spec_plan:
            spec_block = spec_plan[:_DIFF_HARD_CAP_CHARS]
        else:
            spec_block = "_(no spec context — improvise from diff + findings)_"

        template = (_PROMPTS_DIR / "coder_findings_0.1.1.md").read_text(encoding="utf-8")
        prompt = (
            template.replace("{DIFF}", truncated_diff)
            .replace("{FINDINGS_JSON}", findings_json)
            .replace("{SPEC_PLAN}", spec_block)
        )

        _log.info(
            "review.coder.findings_spawn",
            pr_number=pr_number,
            diff_chars=len(diff),
            n_findings=len(findings),
        )
        result = self._loop.run_oneshot(prompt, json_response=True)

        data = _parse_fix_plan(result.text)
        if data is None:
            diag = _diagnose_parse_failed(result.text)
            persisted = _persist_parse_failed_response(
                role="coder",
                identifier=pr_number if pr_number is not None else "unknown",
                raw=result.text,
                logs_dir=self._logs_dir,
            )
            _log.warning(
                "review.coder.findings_parse_failed",
                pr_number=pr_number,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                elapsed_s=result.elapsed_s,
                persisted_path=str(persisted) if persisted is not None else None,
                **diag,
            )
            return {
                "fixes": [],
                "commit_message": "",
                "summary": result.text[:240],
                "model_used": result.model_used,
                "elapsed_s": result.elapsed_s,
                "tokens_used": result.tokens_used,
            }

        raw_fixes = data.get("fixes", []) or []
        if not isinstance(raw_fixes, list):
            raw_fixes = []
        fixes: list[dict[str, Any]] = []
        for item in raw_fixes:
            if not isinstance(item, dict):
                continue
            fix_path = item.get("path")
            content = item.get("new_content")
            if not isinstance(fix_path, str) or not fix_path:
                continue
            if not isinstance(content, str):
                continue
            fixes.append(
                {
                    "path": fix_path,
                    "new_content": content,
                    "rationale": str(item.get("rationale", ""))[:400],
                }
            )
        return {
            "fixes": fixes,
            "commit_message": str(data.get("commit_message", ""))[:1000],
            "summary": str(data.get("summary", ""))[:240],
            "model_used": result.model_used,
            "elapsed_s": result.elapsed_s,
            "tokens_used": result.tokens_used,
        }


def _diagnose_parse_failed(raw: str) -> dict[str, Any]:
    """Return a dict of diagnostic fields the parse_failed log should carry.

    PR #96 final dogfood (2026-05-06) hit ``review.coder.parse_failed``
    with only 200 chars of the raw response in the log — not enough
    to tell whether the failure was truncation, structural mismatch,
    malformed fences, or a parser bug.  This helper extracts every
    cheap signal that distinguishes those classes:

    * ``raw_chars`` — total length of the response.
    * ``raw_head`` — first 500 chars (tells us how the response opens).
    * ``raw_tail`` — last 500 chars.  A clean response ends with ``}``
      or a closing markdown fence; truncation ends mid-token.
    * ``has_fence_open`` / ``has_fence_close`` — whether the model
      wrapped the JSON in markdown fences and whether the closing
      fence ever arrived.
    * ``balanced_braces`` — count of ``{`` minus count of ``}``.
      Negative = extra closers (suspicious / malformed).  Positive
      = unclosed (truncation likely).  Zero = balanced (the parser
      itself rejected the structure — most likely
      ``_is_valid_coder_plan`` says no).
    * ``trimmed_balanced_braces`` — same delta computed AFTER the
      string-aware walker steps over quoted content.  Disagreement
      between the two means a brace count was misled by a brace
      character inside a string literal.

    These fields go straight into the structured log line; the
    workflow's "Dump LLM proxy log" step prints them at parse_failed
    time without any re-CI cycle.

    Args:
        raw: The full response text from the model.

    Returns:
        A flat dict suitable for ``**kwargs`` to the structured logger.
    """
    if not raw:
        return {
            "raw_chars": 0,
            "raw_head": "",
            "raw_tail": "",
            "has_fence_open": False,
            "has_fence_close": False,
            "balanced_braces": 0,
            "trimmed_balanced_braces": 0,
        }

    head = raw[:500]
    tail = raw[-500:] if len(raw) > 500 else raw
    head_probe = raw[:200]
    tail_probe = raw[-200:] if len(raw) > 200 else raw

    has_fence_open = "```" in head_probe
    has_fence_close = "```" in tail_probe and tail_probe.rstrip().endswith("```")

    naive_open = raw.count("{")
    naive_close = raw.count("}")
    balanced_braces = naive_open - naive_close

    in_string = False
    escape = False
    aware_open = 0
    aware_close = 0
    for ch in raw:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            aware_open += 1
        elif ch == "}":
            aware_close += 1
    trimmed_balanced_braces = aware_open - aware_close

    return {
        "raw_chars": len(raw),
        "raw_head": head,
        "raw_tail": tail,
        "has_fence_open": has_fence_open,
        "has_fence_close": has_fence_close,
        "balanced_braces": balanced_braces,
        "trimmed_balanced_braces": trimmed_balanced_braces,
    }


def _persist_parse_failed_response(
    *,
    role: str,
    identifier: str | int,
    raw: str,
    logs_dir: Path | None = None,
) -> Path | None:
    """Write the full raw response to ``logs/coder_parse_failed_<id>_<utc>.txt``.

    The workflow's "Dump LLM proxy log" step picks up everything
    under ``logs/`` so this file lands in the workflow output next
    to the proxy log on failure.  Local runs stash it in the same
    location so post-mortem doesn't depend on stderr scrollback.

    Args:
        role: ``"coder"`` or ``"developer"`` — namespaces the file.
        identifier: PR number for the Coder, spec_id for the Developer.
        raw: Full response text to persist.
        logs_dir: Override the destination directory (tests).

    Returns:
        The path written to, or ``None`` when the I/O failed (we
        don't want a logging failure to crash the dispatch).
    """
    target_dir = logs_dir or (Path(__file__).resolve().parents[3] / "logs")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.warning(
            "reviewer.parse_log_mkdir_failed",
            role=str(role),
            target_dir=str(target_dir),
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(identifier))[:64] or "unknown"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = target_dir / f"{role}_parse_failed_{safe_id}_{timestamp}.txt"
    try:
        path.write_text(raw, encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "reviewer.parse_log_write_failed",
            role=str(role),
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None
    return path


def _is_valid_coder_plan(parsed: Any) -> bool:
    """Return True when ``parsed`` is a dict that carries a Coder payload.

    A valid Coder / Developer payload is a dict with a ``"fixes"`` key —
    the findings-driven Coder and the Developer both emit fixes only.
    """
    if not isinstance(parsed, dict):
        return False
    return "fixes" in parsed


def _parse_fix_plan(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON parser for the Coder / Developer response shape.

    Recovery strategy, in order:

    1. Strict ``json.loads`` on the raw response (the happy path).
    2. Strip an optional ```` ```json ```` markdown fence.
    3. The largest ``{...}`` greedy match (legacy behaviour).
    4. **Salvage truncated responses** — close the dangling JSON by
       walking the bracket / quote depth at the cut-off point and
       appending the missing closers.  Triggered when the model is
       cut by ``max_tokens`` and emits well-formed JSON up to the
       cut, which we can recover into a partial-but-parseable plan.

    A "valid" Coder payload carries ``fixes`` (see
    :func:`_is_valid_coder_plan`).  Returns ``None`` only when no recovery
    yields a valid Coder payload.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if _is_valid_coder_plan(data):
            return data
    except json.JSONDecodeError as exc:
        _log.debug("reviewer.coder_plan_raw_decode_failed", error=str(exc)[:120])
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if fence is not None:
        try:
            data = json.loads(fence.group(1))
            if _is_valid_coder_plan(data):
                return data
        except json.JSONDecodeError as exc:
            _log.debug("reviewer.coder_plan_fence_decode_failed", error=str(exc)[:120])
    candidates = sorted(re.findall(r"\{[\s\S]*\}", raw), key=len, reverse=True)
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError as exc:
            _log.debug("reviewer.coder_plan_blob_decode_failed", error=str(exc)[:120])
            continue
        if _is_valid_coder_plan(parsed):
            return parsed
    return _salvage_truncated_plan(raw)


def _salvage_truncated_plan(raw: str) -> dict[str, Any] | None:
    """Best-effort close-the-brackets recovery for a truncated JSON.

    The model emitted well-formed JSON up to ``max_tokens`` and got
    cut.  We walk forward tracking ``{}``, ``[]`` and string quotes,
    then synthesize the missing close characters.  If the resulting
    string parses AND has ``"fixes"`` we return it.

    This is best-effort: a truncation that happens inside a long
    string literal can't always be recovered (we can't guess what
    content was elided).  In that case we trim the in-progress
    object back to the last complete entry.
    """
    start = raw.find("{")
    if start < 0:
        return None
    body = raw[start:]
    stack: list[str] = []
    in_string = False
    escape = False
    last_complete = -1
    for i, ch in enumerate(body):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            opener = stack.pop()
            matched = (opener == "{" and ch == "}") or (opener == "[" and ch == "]")
            if matched and not stack:
                last_complete = i
    if stack:
        if in_string:
            comma = body.rfind(",")
            if comma > 0:
                body = body[:comma]
                stack = []
                in_string = False
                escape = False
                for ch in body:
                    if escape:
                        escape = False
                        continue
                    if ch == "\\" and in_string:
                        escape = True
                        continue
                    if ch == '"':
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch in "{[":
                        stack.append(ch)
                    elif ch in "}]" and stack:
                        stack.pop()
        body += "".join("}" if c == "{" else "]" for c in reversed(stack))
    elif last_complete >= 0:
        body = body[: last_complete + 1]
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.debug("reviewer.coder_plan_salvage_failed", error=str(exc)[:120])
        return None
    if _is_valid_coder_plan(parsed):
        return parsed
    return None


def _normalise_edits(raw_edits: object) -> list[dict[str, str]] | None:
    """Validate a ``fixes[].edits`` list (SP-DEV-TARGETED-PATCH).

    Returns the cleaned list, or ``None`` when the shape is unusable
    (so the caller drops the fix instead of half-applying it).
    """
    if not isinstance(raw_edits, list) or not raw_edits:
        return None
    out: list[dict[str, str]] = []
    for item in raw_edits:
        if not isinstance(item, dict):
            return None
        search = item.get("search")
        replace = item.get("replace")
        if not isinstance(search, str) or not search or not isinstance(replace, str):
            return None
        out.append({"search": search, "replace": replace})
    return out


def _normalise_fixes(raw_fixes: object) -> list[dict[str, Any]]:
    """Defensive normalisation of a ``fixes[]`` list to the runner schema.

    Accepts two shapes per item: ``new_content`` (complete file —
    creations and small files) or ``edits`` (anchored search/replace
    pairs for existing files, SP-DEV-TARGETED-PATCH). Items carrying
    neither, or with non-string types, are dropped. The path-whitelist
    check is the runner's responsibility.
    """
    if not isinstance(raw_fixes, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_fixes:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        rationale = str(item.get("rationale", ""))[:400]
        content = item.get("new_content")
        if isinstance(content, str):
            out.append({"path": path, "new_content": content, "rationale": rationale})
            continue
        edits = _normalise_edits(item.get("edits"))
        if edits is not None:
            out.append({"path": path, "edits": edits, "rationale": rationale})
    return out


def _developer_response_has_fixes(response: Any) -> bool:
    """Tier-escalation predicate: reject Developer responses with no fixes.

    SP-NIM-RES-V2 — when the proxy returns ``200 OK`` with a parseable
    JSON body that has an empty ``fixes`` array, the agent loop must
    treat it as a soft failure (escalate / retry) instead of accepting
    the empty output.  We can't prove "the tier is broken" from HTTP
    state alone (response is fine); we infer it from "Developer was
    asked to write code, the model returned no fixes".

    SP-LLM-NATIVE-FORMAT (2026-05-06): the agent loop now passes the
    native :class:`AgentResponse` (Pydantic) instead of an
    OpenAI-shaped ``SimpleNamespace``.  The text we parse comes from
    :class:`TextBlock` blocks in ``response.content``.  The function
    is duck-typed on ``response.content`` so legacy tests that pass
    ``SimpleNamespace`` shapes don't have to migrate.

    Returns:
        ``True`` when the response is acceptable (non-empty fixes, or
        unparseable — let downstream handle it).  ``False`` when the
        response parses as ``{"fixes": []}`` and we want to escalate.
    """
    text = ""
    content = getattr(response, "content", None)
    if isinstance(content, list):
        for block in content:
            block_type = getattr(block, "type", None)
            block_text = getattr(block, "text", None)
            if block_type == "text" and isinstance(block_text, str):
                text += block_text
    if not text:
        try:
            choice = response.choices[0]
            text = choice.message.content or ""
        except (AttributeError, IndexError, TypeError):
            return True
    if not text.strip():
        return False
    candidates = re.findall(r"\{[\s\S]*\}", text)
    candidates.sort(key=len, reverse=True)
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError as exc:
            _log.debug("reviewer.fix_detector_blob_unparseable", error=str(exc)[:120])
            continue
        if not isinstance(parsed, dict):
            continue
        fixes = parsed.get("fixes")
        if isinstance(fixes, list) and fixes:
            return True
        if isinstance(fixes, list):
            return False
    return False


_DEVELOPER_MAX_TURNS = 30
"""Tool-call budget for the agentic Developer loop.

The AgentLoop default of 15 was sized for the Planner's read-only
exploration. Real code steps on large modules exhaust it on reads
alone: on SP-DEV-STEP-PREFLIGHT (2026-07-04) both dispatches ended
"I exhausted the tool-call budget on read operations" against the
~1,000-line dev_runner.py, and the first SP-FINDINGS-BRIDGE-DOCFIX
run died the same way — ~518k and ~280k tokens for zero writes.
Thirty turns gives orientation AND implementation room; the
per-step attempt cap still bounds the total cost, and
SP-DEV-BRIEF-FILE-CONTENT (embedding contract-file content in the
brief) remains the structural fix.
"""


class Developer:
    """Autonomous agent that implements a spec from its plan.

    Unlike :class:`Coder` which reacts to review feedback, the
    Developer takes the spec plan + the contents of any existing
    files referenced in the plan and emits the **initial**
    implementation — full-file rewrites, including unit tests.

    The output JSON shape is identical to :class:`Coder`'s so the
    runner can reuse the same path-whitelist + apply + commit flow.

    Inputs to :meth:`respond`:
        spec_plan: Markdown content of ``docs/specs/<id>.md``.
        existing_files: Mapping of repo-relative path → file contents
            for every file the spec references that already exists.
            The Developer aligns its output with the surrounding
            patterns in those files.
        repo_tree: Optional pre-rendered ``find src tests`` listing,
            capped, that the Developer uses for orientation.

    Capacity:
        The model chain (``PROXY_SONNET_CHAIN``) — initial impl + unit
        tests is harder than the surgical fixes :class:`Coder` does, but
        sonnet-tier quality is the right level for the loop.
        ``max_tokens=32000`` covers ~3-4 mid-size files JSON-escaped
        (a lower historic cap truncated mid-content on long
        dispatches).  The chain candidates sustain that ceiling, so
        the Developer cap grows without forcing chain failover.
    """

    role = BotRole.DEVELOPER
    persona_filename = "developer_0.2.1.md"
    model_chain = PROXY_SONNET_CHAIN
    max_tokens = 32000
    temperature = 0.05

    def __init__(
        self,
        *,
        loop: AgentLoop | None = None,
        logs_dir: Path | None = None,
    ) -> None:
        """Create the Developer with an optional pre-built AgentLoop.

        Long-output personas pin the per-call HTTP timeout to
        :data:`LONG_OUTPUT_TIMEOUT_S` (~7 min) so 32K-token JSON
        responses don't get cut mid-stream by the lower default
        used for reviewer-class calls.  SP-NIM-RES-V3.

        Args:
            loop: Optional pre-built AgentLoop (tests inject a mock).
            logs_dir: Optional override for the on-disk dump directory
                used by :func:`_persist_parse_failed_response` when the
                model returns an unparseable response.  Default
                ``None`` keeps the production behaviour (repo's
                ``logs/``).  Tests pass ``tmp_path`` to keep parse
                failures off the working tree —
                SP-DEV-TEST-POLLUTION-LOGS-DIR.
        """
        if loop is not None:
            self._loop = loop
        else:
            self._loop = AgentLoop(
                model_chain=self.model_chain,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                per_call_timeout_s=LONG_OUTPUT_TIMEOUT_S,
                max_turns=_DEVELOPER_MAX_TURNS,
            )
        self._logs_dir = logs_dir
        from .mcp_whitelist import allowed_tools_for

        self._allowed_tools: tuple[str, ...] = allowed_tools_for(self.role)

    _RETRY_BACKOFFS_S: tuple[float, ...] = (0.0, 30.0, 90.0)

    def _call_with_retry(self, prompt: str, *, spec_id: str | None) -> Any:
        """Call the AgentLoop up to len(_RETRY_BACKOFFS_S) times, return result or None.

        ``_RETRY_BACKOFFS_S`` schedules the wait (in seconds) before
        each attempt : the first call fires immediately, subsequent
        calls back off 30 s then 90 s to give NIM endpoints time to
        recover from transient errors.  Empirically a single
        ``APIConnectionError`` mid-stream is common and a 30 s wait
        is usually sufficient for NIM to stabilise.

        The chain failover inside :meth:`AgentLoop.run_oneshot`
        handles per-model errors, but a NIM endpoint outage hits every
        model in the chain at the network layer — exhaust → exception.
        This wrapper adds **runner-level retry-with-backoff** so the
        whole chain gets a second / third chance after a brief wait
        rather than killing the dispatch.

        Args:
            prompt: The fully-rendered prompt to feed to ``run_oneshot``.
            spec_id: For structured logging only.

        Returns:
            The :class:`NimAgentOutput` on success, or ``None`` after
            all retries failed.
        """
        import time as _time

        last_exc: Exception | None = None
        for attempt, wait_s in enumerate(self._RETRY_BACKOFFS_S, start=1):
            if wait_s > 0:
                _log.info(
                    "review.developer.retry_wait",
                    spec_id=spec_id,
                    attempt=attempt,
                    wait_s=wait_s,
                    last_error=type(last_exc).__name__ if last_exc else None,
                )
                _time.sleep(wait_s)
            try:
                return self._loop.run_oneshot(
                    prompt,
                    json_response=True,
                    accept_response=_developer_response_has_fixes,
                )
            except Exception as exc:
                last_exc = exc
                _log.warning(
                    "review.developer.attempt_failed",
                    spec_id=spec_id,
                    attempt=attempt,
                    of=len(self._RETRY_BACKOFFS_S),
                    error=type(exc).__name__,
                    message=str(exc)[:200],
                )
        _log.error(
            "review.developer.exhausted",
            spec_id=spec_id,
            attempts=len(self._RETRY_BACKOFFS_S),
            final_error=type(last_exc).__name__ if last_exc else "Unknown",
        )
        return None

    def respond(
        self,
        *,
        spec_plan: str,
        existing_files: dict[str, str] | None = None,
        repo_tree: str = "",
        spec_id: str | None = None,
    ) -> dict[str, Any]:
        """Produce a JSON fix-plan implementing the spec.

        Args:
            spec_plan: Markdown content of the spec doc.
            existing_files: Optional mapping ``{path: contents}`` for
                files referenced by the spec.
            repo_tree: Optional pre-rendered tree listing for context.
            spec_id: Optional canonical id (``"SP-DEV"``) used for
                logging only.

        Returns:
            A dict with keys ``fixes``, ``commit_message``, ``summary``,
            plus ``model_used`` / ``elapsed_s`` / ``tokens_used`` for
            audit.  On parse failure: ``fixes=[]`` + raw text in
            ``summary``.  On NIM API failure after all retries: empty
            fix-plan with the error in ``summary``.
        """
        existing_block = _format_existing_files(existing_files or {})

        path = _PROMPTS_DIR / self.persona_filename
        template = path.read_text(encoding="utf-8")
        prompt = (
            template.replace("{SPEC_PLAN}", spec_plan[:_DIFF_HARD_CAP_CHARS])
            .replace("{EXISTING_FILES}", existing_block)
            .replace("{REPO_TREE}", repo_tree[:4000])
        )

        _log.info(
            "review.developer.spawn",
            spec_id=spec_id,
            plan_chars=len(spec_plan),
            n_existing=len(existing_files or {}),
        )
        result = self._call_with_retry(prompt, spec_id=spec_id)
        if result is None:
            return {
                "fixes": [],
                "commit_message": "",
                "summary": "NIM API exhausted across all retries — try again later.",
                "model_used": "",
                "elapsed_s": 0.0,
                "tokens_used": 0,
            }

        data = _parse_fix_plan(result.text)
        if data is None:
            diag = _diagnose_parse_failed(result.text)
            persisted = _persist_parse_failed_response(
                role="developer",
                identifier=spec_id or "unknown",
                raw=result.text,
                logs_dir=self._logs_dir,
            )
            _log.warning(
                "review.developer.parse_failed",
                spec_id=spec_id,
                model_used=result.model_used,
                tokens_used=result.tokens_used,
                elapsed_s=result.elapsed_s,
                persisted_path=str(persisted) if persisted is not None else None,
                **diag,
            )
            return {
                "fixes": [],
                "commit_message": "",
                "summary": result.text[:240],
                "model_used": result.model_used,
                "elapsed_s": result.elapsed_s,
                "tokens_used": result.tokens_used,
            }

        return {
            "fixes": _normalise_fixes(data.get("fixes")),
            "commit_message": str(data.get("commit_message", ""))[:1000],
            "summary": str(data.get("summary", ""))[:240],
            "model_used": result.model_used,
            "elapsed_s": result.elapsed_s,
            "tokens_used": result.tokens_used,
        }

    agentic_persona_filename = "developer_agentic_0.1.1.md"

    def develop_step(
        self,
        *,
        brief: str,
        repo_root: Path,
        allowed_paths: Iterable[str],
        repo_tree: str = "",
        spec_id: str | None = None,
    ) -> DevLoopResult:
        """Implement one plan step agentically (SP-DEVAGENT-LOOP).

        The agentic counterpart of :meth:`respond`: instead of emitting a JSON
        fix-plan for the runner to apply, the Developer drives a multi-turn tool
        loop — reading the tree, writing/editing files, running the tests and ruff,
        and fixing forward until green — over its own :class:`AgentLoop`. The new
        agentic persona is the system prompt; the per-step ``brief`` is the first
        user message; writes are jailed to ``allowed_paths`` (the step's file
        contract) in-loop.

        Args:
            brief: The per-step brief (plan context + spec + gate feedback).
            repo_root: The working tree root the tools are jailed to.
            allowed_paths: The step's writable file contract.
            repo_tree: Optional orientation tree appended to the brief.
            spec_id: Canonical id used for logging only.

        Returns:
            A :class:`DevLoopResult`; ``error`` is set when the brain/proxy failed
            before the loop produced a final answer. The on-disk tree reflects
            whatever the model wrote.
        """
        from .devagent_loop import run_agentic_step

        allowed = tuple(allowed_paths)
        system = (_PROMPTS_DIR / self.agentic_persona_filename).read_text(encoding="utf-8")
        _log.info(
            "review.developer.develop_step_spawn",
            spec_id=spec_id,
            allowed_paths=sorted(set(allowed)),
            brief_chars=len(brief),
        )
        return run_agentic_step(
            self._loop,
            system=system,
            brief=brief,
            repo_root=repo_root,
            allowed_paths=allowed,
            repo_tree=repo_tree,
        )


def _format_existing_files(files: dict[str, str]) -> str:
    """Render ``{path: contents}`` as a labelled block for the prompt.

    Each file is wrapped in ``=== <path> ===`` markers and capped at
    32 KB.  Bumped from 8 KB after the first SP-MCP-EXT dispatch:
    truncating mid-class made the Developer drop docstrings on classes
    whose definitions started past the cut, which broke the ruff gate
    (``D101 Missing docstring in public class``) and forced two
    revert+retry cycles.  32 KB covers every existing file in the
    review module (~16 KB peak) and stays well under the NIM prompt
    budget when combined with the spec + repo tree.
    """
    if not files:
        return "_(no existing files referenced — fresh feature)_"
    chunks: list[str] = []
    for path, contents in files.items():
        capped = contents[:32000]
        if len(contents) > 32000:
            capped += "\n# [... file truncated; see repo for the full version ...]\n"
        chunks.append(f"=== {path} ===\n{capped}")
    return "\n\n".join(chunks)
