"""Review-team orchestration: parallel reviewers + aggregated verdict + posting.

The orchestrator is the entry point invoked by the CLI command and
the GitHub Actions workflow.  Given a PR number, it:

1. Fetches the diff via ``gh pr diff``.
2. Runs the four reviewer bots concurrently.
3. Aggregates their verdicts into a final team verdict.
4. Posts each comment + each per-bot review on the GitHub PR.
5. Persists every outcome in L4 ``pr_reviews``.
6. Upserts a sticky archive comment on the PR carrying the full
   :class:`TeamOutcome` JSON, so the result can be retrieved later
   from any machine via :meth:`GhCli.fetch_archive_comment`.

The orchestrator does **not** auto-merge anything and does **not**
auto-invoke the Coder bot — those are explicit follow-up actions
(future :func:`run_coder_response`).  This keeps the human in the
loop for the merge button by default.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from .finding_verifiers import verify_findings_for_pr
from .findings_bridge import record_findings_for_outcomes
from .gh_client import GhCli
from .hallucination_guard import (
    GuardEvent,
    apply_hallucination_guard,
    make_repo_file_reader,
    make_repo_symbol_searcher,
)
from .merge_gate import compute_merge_decision, summarise_ledger_facts, verdict_from_facts
from .notifier import fire_review_routine
from .persistence import (
    DialogueEntry,
    fetch_dialogue,
    init_schema,
    record_dialogue,
    record_hallucination,
    record_review,
)
from .refuter import judge_findings_for_pr
from .report import render_ledger_report
from .review_lessons import remember_verified_findings
from .review_memory import recall_review_lessons, review_lessons_section
from .reviewer import (
    Architect,
    BotRole,
    DialogueContext,
    GuardEventSummary,
    PeerOutcome,
    PriorReviewContext,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
    make_file_excerpts,
)
from .thread_context import ResolvedDisagreement

_log = get_logger(__name__)


def _parse_comment_id(stdout: str) -> int | None:
    """Extract the GitHub comment id from a ``pr_review_comment`` response.

    The ``gh api`` invocation echoes the JSON of the created comment
    on stdout.  Tolerant of empty / non-JSON output (returns ``None``)
    so a missing id never crashes the review pass — the auto-challenge
    pass simply can't target that comment.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        _log.debug("orchestrator.comment_id_json_decode_failed", error=str(exc)[:120])
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError) as exc:
        _log.debug(
            "orchestrator.comment_id_int_cast_failed", raw=str(raw)[:50], error=str(exc)[:120]
        )
        return None


@dataclass
class TeamOutcome:
    """Aggregated outcome of the four reviewers on one PR.

    ``final_verdict`` is sourced from the findings ledger
    (:func:`merge_gate.verdict_from_facts`), not the retired consensus
    gate.
    """

    pr_number: int
    final_verdict: ReviewVerdict
    n_blockers: int
    n_majors: int
    reviews: list[ReviewerOutcome] = field(default_factory=list)
    posted_comments: int = 0
    posted_reviews: int = 0
    hallucinations: list[GuardEvent] = field(default_factory=list)
    head_sha: str | None = None
    auto_challenges: list[dict] = field(default_factory=list)


class ReviewTeamOrchestrator:
    """Run the four reviewer bots in parallel and post their findings.

    Args:
        gh: Optional pre-built :class:`GhCli` instance.
        db_path: Path to the L4 SQLite db.  Defaults to settings.db_path.
        post_to_github: When False, do not publish anything to GitHub
            (dry-run mode for tests / local previews).
        max_workers: Thread-pool worker count for the four reviewers.
    """

    def __init__(
        self,
        *,
        gh: GhCli | None = None,
        db_path: Path | None = None,
        post_to_github: bool = True,
        max_workers: int = 4,
        repo_root: Path | None = None,
    ) -> None:
        """Initialise the orchestrator with optional dependencies."""
        settings = get_settings()
        self._gh = gh or GhCli()
        self._db_path = db_path or Path(settings.db_path)
        self._post = post_to_github
        self._max_workers = max_workers
        self._repo_root = repo_root or Path.cwd()
        init_schema(self._db_path)

    def review_pr(self, pr_number: int) -> TeamOutcome:
        """Run the four reviewers in parallel and return aggregated outcome.

        Args:
            pr_number: GitHub PR number.

        Returns:
            A :class:`TeamOutcome` with each reviewer's verdict, the
            aggregated team verdict, and posting statistics.

        Pipeline:
            1. **Fetch diff.**  Failure to read the PR diff is fatal —
               we return a degraded ``COMMENT`` outcome rather than
               crash the workflow.
            2. **Auto-load active spec** (SP-DEV-V2-B2) from the PR's
               head branch so every reviewer reads the spec alongside
               the diff.  Spec lookup failures are logged and treated
               as "no spec" — never fatal.
            3. **Fetch resolved disagreements per reviewer** (SP-CODER-
               EVIDENCE-CHALLENGE phase 3B) — root threads the Coder
               has already answered with "Verified — challenge with
               evidence".  Logging + integration in production traffic
               ; the prompt-template bump that makes the model act on
               them ships in a follow-up.
            4. **Run the four reviewers in parallel** through a
               :class:`ThreadPoolExecutor`.  A single bot blowing up
               (NIM chain exhaustion, parser crash) records a degraded
               ``COMMENT`` outcome — the team continues without it.
            5. **Hallucination guard** (SP-DEV-V2-B3) — post-process
               each outcome to disprove "missing X" claims by grepping
               the working tree, and to catch the self-referential
               prompt-template hallucination mode.  Defence in depth
               alongside the anti-hallucination paragraphs in the
               persona files.
            6. **Persist round-1 verdicts** to L4 ``review_dialogue``
               so the dialogue transcript can be replayed later.
            7. **Round 2** (SP-REVIEW-DIALOGUE-A) — when at least one
               reviewer flagged a blocker / major, every flagging
               reviewer gets a second pass with peer outcomes + its
               own guard verdicts + live file excerpts.  Comments
               retracted in round 2 drop from the final aggregation.
               At most one round 2 per PR.  Only the reviewers whose
               outcome was actually replaced get a round-2 dialogue
               row (crashes keep the round-1 object in place).
            8. **Ledger-sourced team verdict** (SP-VERDICT-RESOURCE-LEDGER)
               — ``final_verdict`` is derived from the findings ledger via
               :func:`merge_gate.verdict_from_facts` (REQUEST_CHANGES on an
               open blocking finding or an incomplete review), not the
               retired consensus gate.  Iteration happens at the workflow
               layer : each new commit on the impl branch re-fires
               ``auto-review.yml`` which calls back into us.
        """
        diff_res = self._gh.pr_diff(pr_number)
        if not diff_res.ok:
            _log.error(
                "review_team.diff_fetch_failed",
                pr_number=pr_number,
                stderr=diff_res.stderr[:200],
            )
            return TeamOutcome(
                pr_number=pr_number,
                final_verdict=ReviewVerdict.COMMENT,
                n_blockers=0,
                n_majors=0,
            )
        diff = diff_res.stdout
        diff_hash = _compute_diff_hash(diff)

        head_sha = self._gh.pr_head_sha(pr_number) if self._post else None

        spec_plan_md: str | None = None
        spec_id: str | None = None
        pr_title = ""
        arch_edges_block = ""
        anchored = False
        try:
            view = self._gh.pr_view(pr_number)
            if isinstance(view, dict):
                pr_title = str(view.get("title") or "")
            head_ref = (view or {}).get("headRefName") if isinstance(view, dict) else None
            if isinstance(head_ref, str) and head_ref:
                from .spec import maybe_load_active_spec

                spec = maybe_load_active_spec(branch=head_ref)
                if spec is not None:
                    spec_plan_md = spec.raw_markdown
                    spec_id = spec.id
                else:
                    from .subspec_anchor import maybe_anchor_decomposed_parent

                    anchor = maybe_anchor_decomposed_parent(head_ref, root=self._repo_root)
                    if anchor is not None:
                        spec_plan_md = anchor.spec_plan_md
                        spec_id = anchor.parent_id
                        arch_edges_block = anchor.arch_edges
                        anchored = True
                        _log.info(
                            "review_team.anchored_to_sub_specs",
                            pr_number=pr_number,
                            parent_id=anchor.parent_id,
                            sub_spec_ids=list(anchor.sub_spec_ids),
                        )
        except Exception as exc:
            _log.info(
                "review_team.spec_lookup_failed",
                pr_number=pr_number,
                error=str(exc),
            )

        if spec_id and not anchored:
            try:
                from .governed_spec import load_governed_spec, render_arch_edges

                arch_edges_block = render_arch_edges(
                    load_governed_spec(spec_id, root=self._repo_root)
                )
            except Exception as exc:
                _log.info(
                    "review_team.arch_edges_failed",
                    pr_number=pr_number,
                    error=str(exc),
                )

        reviewers: list[Reviewer] = [
            Architect(),
            Sentinel(),
            Tester(),
            Scribe(),
        ]

        from .thread_context import fetch_resolved_disagreements

        resolved_per_role: dict[BotRole, tuple[ResolvedDisagreement, ...]] = {}
        try:
            for r in reviewers:
                items = fetch_resolved_disagreements(self._gh, pr_number, role=r.role)
                resolved_per_role[r.role] = tuple(items)
        except Exception as exc:
            _log.warning(
                "review_team.resolved_disagreements_fetch_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )
            resolved_per_role = {r.role: () for r in reviewers}

        prior_per_role = _build_prior_review_context(
            pr_number, diff_hash=diff_hash, db_path=self._db_path
        )

        lessons_block = _build_review_lessons_block(pr_title, diff)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [
                (
                    r,
                    pool.submit(
                        r.review_diff,
                        diff,
                        pr_number=pr_number,
                        spec_plan=spec_plan_md,
                        resolved_disagreements=resolved_per_role.get(r.role, ()),
                        prior_review=prior_per_role.get(r.role),
                        extra_prompt_section=lessons_block,
                        arch_edges=arch_edges_block,
                    ),
                )
                for r in reviewers
            ]
            outcomes: list[ReviewerOutcome] = []
            for reviewer, fut in futures:
                try:
                    outcomes.append(fut.result())
                except Exception as exc:
                    _log.warning(
                        "review_team.reviewer_failed",
                        role=reviewer.role.value,
                        exc=type(exc).__name__,
                        message=str(exc)[:200],
                    )
                    outcomes.append(
                        ReviewerOutcome(
                            role=reviewer.role,
                            verdict=ReviewVerdict.COMMENT,
                            summary=(f"_(bot crashed: {type(exc).__name__} — {str(exc)[:140]})_"),
                            comments=[],
                            model_used="<n/a>",
                            elapsed_s=0.0,
                            tokens_used=0,
                            raw_response="",
                        )
                    )

        file_reader = make_repo_file_reader(self._repo_root)
        symbol_searcher = make_repo_symbol_searcher(self._repo_root)
        guarded: list[ReviewerOutcome] = []
        all_events: list[GuardEvent] = []
        for o in outcomes:
            new_outcome, events = apply_hallucination_guard(
                o,
                file_reader=file_reader,
                symbol_searcher=symbol_searcher,
            )
            guarded.append(new_outcome)
            all_events.extend(events)
        outcomes = guarded

        for o in outcomes:
            record_dialogue(
                self._db_path,
                pr_number=pr_number,
                round="1",
                speaker=o.role.value,
                payload=_dialogue_payload(o, diff_hash=diff_hash),
            )

        round1_snapshot = list(outcomes)
        outcomes = self._round_two(
            reviewers=reviewers,
            round1_outcomes=outcomes,
            spec_plan=spec_plan_md,
            guard_events=all_events,
            diff=diff,
            pr_number=pr_number,
            extra_prompt_section=lessons_block,
            arch_edges=arch_edges_block,
        )
        round2_ran = False
        for before, after in zip(round1_snapshot, outcomes, strict=True):
            if after is not before:
                round2_ran = True
                record_dialogue(
                    self._db_path,
                    pr_number=pr_number,
                    round="2",
                    speaker=after.role.value,
                    payload=_dialogue_payload(after, diff_hash=diff_hash),
                )
        outcomes = [_drop_retracted_comments(o) for o in outcomes]

        for i, o in enumerate(outcomes):
            if (
                isinstance(o, ReviewerOutcome)
                and o.verdict != ReviewVerdict.APPROVE
                and not any(c.severity in {"blocker", "major"} for c in o.comments)
            ):
                outcomes[i] = replace(
                    o,
                    verdict=ReviewVerdict.APPROVE,
                    summary=o.summary + " [auto-promoted: nit-only]",
                )
                _log.info(
                    "review_team.nit_only_auto_promote",
                    role=o.role.value,
                    pr_number=pr_number,
                    original_verdict=o.verdict.value,
                )

        n_blockers = sum(1 for o in outcomes for c in o.comments if c.severity == "blocker")
        n_majors = sum(1 for o in outcomes for c in o.comments if c.severity == "major")

        try:
            n_findings = record_findings_for_outcomes(
                self._db_path,
                pr_number=pr_number,
                head_sha=head_sha,
                outcomes=outcomes,
                round_n=2 if round2_ran else 1,
                diff=diff,
            )
            _log.info(
                "review_team.findings_recorded",
                pr_number=pr_number,
                n_findings=n_findings,
            )
        except Exception as exc:
            _log.warning(
                "review_team.findings_record_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        try:
            from .findings import record_review_integrity
            from .findings_bridge import _is_unparsed

            n_unparsed = sum(1 for o in outcomes if _is_unparsed(o))
            record_review_integrity(
                self._db_path,
                pr_number=pr_number,
                head_sha=head_sha,
                n_reviewers=len(outcomes),
                n_unparsed=n_unparsed,
            )
            _log.info(
                "review_team.integrity_recorded",
                pr_number=pr_number,
                n_reviewers=len(outcomes),
                n_unparsed=n_unparsed,
            )
        except Exception as exc:
            _log.warning(
                "review_team.integrity_record_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        try:
            verify_counts = verify_findings_for_pr(
                self._db_path,
                pr_number=pr_number,
                repo_root=self._repo_root,
                head_sha=head_sha,
            )
            _log.info("review_team.findings_verified", pr_number=pr_number, **verify_counts)
        except Exception as exc:
            _log.warning(
                "review_team.findings_verify_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        try:
            judge_counts = judge_findings_for_pr(
                self._db_path,
                pr_number=pr_number,
                repo_root=self._repo_root,
                head_sha=head_sha,
            )
            _log.info("review_team.findings_judged", pr_number=pr_number, **judge_counts)
        except Exception as exc:
            _log.warning(
                "review_team.findings_judge_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        try:
            from .thread_context import post_refuted_finding_sentinels

            n_sentinels = post_refuted_finding_sentinels(self._gh, self._db_path, pr_number)
            _log.info(
                "review_team.refuted_sentinels_posted",
                pr_number=pr_number,
                n_sentinels=n_sentinels,
            )
        except Exception as exc:
            _log.warning(
                "review_team.refuted_sentinels_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        if spec_id is not None:
            try:
                from .plan import load_plan
                from .spec_gate import compute_spec_coverage, record_spec_coverage

                plan = load_plan(spec_id, root=self._repo_root)
                coverage = compute_spec_coverage(self._repo_root, spec_id=spec_id, plan=plan)
                record_spec_coverage(
                    self._db_path, pr_number=pr_number, head_sha=head_sha, coverage=coverage
                )
                _log.info(
                    "review_team.spec_coverage",
                    pr_number=pr_number,
                    covered=coverage.covered,
                    n_promised=coverage.n_promised,
                    n_present=coverage.n_present,
                )
            except Exception as exc:
                _log.info(
                    "review_team.spec_coverage_skipped",
                    pr_number=pr_number,
                    exc=type(exc).__name__,
                    message=str(exc)[:200],
                )

        ledger_facts = summarise_ledger_facts(
            self._db_path, pr_number=pr_number, head_sha=head_sha or ""
        )
        final = verdict_from_facts(ledger_facts)

        team = TeamOutcome(
            pr_number=pr_number,
            final_verdict=final,
            n_blockers=n_blockers,
            n_majors=n_majors,
            head_sha=head_sha,
            reviews=outcomes,
            hallucinations=all_events,
        )

        for outcome in outcomes:
            record_review(
                self._db_path,
                pr_number=pr_number,
                outcome=outcome,
            )
        for event in all_events:
            record_hallucination(self._db_path, pr_number=pr_number, event=event)

        if self._post:
            posted_ids: dict[tuple[str, int], int] = {}
            for outcome in outcomes:
                self._publish_outcome(
                    pr_number=pr_number,
                    outcome=outcome,
                    head_sha=head_sha,
                    team=team,
                    posted_ids=posted_ids,
                )
            self._run_auto_challenge_pass(
                pr_number=pr_number,
                outcomes=outcomes,
                resolved_per_role=resolved_per_role,
                posted_ids=posted_ids,
                team=team,
            )
            self._upsert_archive_comment(team)
            self._fire_routine(team)
            remember_verified_findings(self._db_path, pr_number=pr_number)

        _log.info(
            "review_team.done",
            pr_number=pr_number,
            verdict=final.value,
            n_blockers=n_blockers,
            n_majors=n_majors,
            posted=team.posted_reviews,
        )
        return team

    def _round_two(
        self,
        *,
        reviewers: list[Reviewer],
        round1_outcomes: list[ReviewerOutcome],
        spec_plan: str | None,
        guard_events: list[GuardEvent],
        diff: str,
        pr_number: int,
        extra_prompt_section: str = "",
        arch_edges: str = "",
    ) -> list[ReviewerOutcome]:
        """Re-invoke every reviewer that flagged blocker / major.

        Each flagging reviewer is sent back into ``review_diff`` with a
        :class:`DialogueContext` carrying:

        - peer outcomes (verdict + 200-char summary, NOT full prose),
        - the runner's hallucination-guard verdicts on its own round-1
          comments,
        - 30-line file excerpts around every ``file:line`` it cited.

        Reviewers whose round-1 verdict was already APPROVE are passed
        through unchanged.  At most one round-2 pass per PR.

        Args:
            reviewers: The four reviewer instances used in round 1.
            round1_outcomes: Round-1 outcomes (after hallucination
                guard) in the same index order as ``reviewers``.
            spec_plan: The spec doc text (may be ``None``).
            guard_events: All hallucination-guard events from round 1.
            diff: The PR diff (already truncated by the caller).
            pr_number: GitHub PR identifier (logging only).
            extra_prompt_section: Pre-rendered review-lessons block
                appended to each round-2 prompt (SP-REVIEW-MEMORY) —
                same block as round 1.
            arch_edges: The declared-dependency block for the Architect's
                ``{ARCH_EDGES}`` placeholder — same block as round 1, so
                edge-awareness is consistent across both rounds.

        Returns:
            New outcomes — one per reviewer.  For reviewers that did
            not re-run, the round-1 outcome is returned unchanged.
        """
        triggers = {
            i
            for i, o in enumerate(round1_outcomes)
            if any(c.severity in {"blocker", "major"} for c in o.comments)
        }
        if not triggers:
            return round1_outcomes

        revised = list(round1_outcomes)
        for i in triggers:
            reviewer = reviewers[i]
            outcome = round1_outcomes[i]
            ctx = self._build_dialogue_context(
                reviewer_role=reviewer.role,
                reviewer_outcome=outcome,
                round1_outcomes=round1_outcomes,
                guard_events=guard_events,
            )
            try:
                new_outcome = reviewer.review_diff(
                    diff,
                    pr_number=pr_number,
                    spec_plan=spec_plan,
                    dialogue_context=ctx,
                    extra_prompt_section=extra_prompt_section,
                    arch_edges=arch_edges,
                )
            except Exception as exc:
                _log.warning(
                    "review_team.round_two_failed",
                    role=reviewer.role.value,
                    pr_number=pr_number,
                    exc=type(exc).__name__,
                    message=str(exc)[:200],
                )
                continue
            _log.info(
                "review_team.round_two_done",
                role=reviewer.role.value,
                pr_number=pr_number,
                round1_verdict=outcome.verdict.value,
                round2_verdict=new_outcome.verdict.value,
                n_round1_comments=len(outcome.comments),
                n_round2_comments=len(new_outcome.comments),
            )
            revised[i] = new_outcome
        return revised

    @staticmethod
    def _build_dialogue_context(
        *,
        reviewer_role: BotRole,
        reviewer_outcome: ReviewerOutcome,
        round1_outcomes: list[ReviewerOutcome],
        guard_events: list[GuardEvent],
    ) -> DialogueContext:
        """Assemble the round-2 context for one reviewer.

        Excludes the reviewer's own round-1 outcome from peer_outcomes;
        truncates each peer's prose to 200 chars; filters guard events
        to those targeting this reviewer; reads file excerpts from disk
        for every cited ``file:line``.
        """
        peer_outcomes = tuple(
            PeerOutcome(
                role=o.role,
                verdict=o.verdict,
                summary=o.summary[:200],
            )
            for o in round1_outcomes
            if o.role is not reviewer_role
        )
        own_guard_raw = [ev for ev in guard_events if ev.role is reviewer_role]
        own_guard = tuple(
            GuardEventSummary(
                comment_index=_locate_comment_index(reviewer_outcome.comments, ev),
                reason=ev.reason,
            )
            for ev in own_guard_raw
        )
        excerpts = make_file_excerpts(
            reviewer_outcome.comments,
            repo_root=Path.cwd(),
        )
        return DialogueContext(
            peer_outcomes=peer_outcomes,
            own_guard_events=own_guard,
            file_excerpts=excerpts,
        )

    def _publish_outcome(
        self,
        *,
        pr_number: int,
        outcome: ReviewerOutcome,
        head_sha: str | None,
        team: TeamOutcome,
        posted_ids: dict[tuple[str, int], int] | None = None,
    ) -> None:
        """Post inline comments + final review verdict for one reviewer.

        Failures here are logged but never raised — we want partial
        success rather than aborting the whole team.

        When ``posted_ids`` is not None, the GitHub comment id of every
        successfully posted inline comment is recorded so the
        orchestrator's auto-challenge pass can target replies on the
        right thread.

        Note:
            ``GITHUB_TOKEN``-authenticated bots cannot submit APPROVE
            / REQUEST_CHANGES reviews on a PR (only humans / PATs
            can — observed live on PR #1 from the auto-review
            workflow).  When the verdict-submit call is rejected we
            fall back to a regular issue comment so each bot's
            verdict still surfaces on the PR conversation.
        """
        for comment in outcome.comments:
            body = (
                f"**[{outcome.role.value}/{comment.severity}]** "
                f"{comment.body}\n\n_— Ferova review-bot ({outcome.model_used})_"
            )
            res = self._gh.pr_review_comment(
                pr_number,
                body=body,
                commit_sha=head_sha,
                file=comment.file,
                line=comment.line,
            )
            if res.ok:
                team.posted_comments += 1
                if posted_ids is not None:
                    new_id = _parse_comment_id(res.stdout)
                    if new_id is not None:
                        posted_ids[(comment.file, comment.line)] = new_id
            else:
                _log.warning(
                    "review_team.comment_post_failed",
                    role=outcome.role.value,
                    file=comment.file,
                    line=comment.line,
                    stderr=res.stderr[:200],
                )

        review_body = (
            f"### {outcome.role.value.title()} review\n\n"
            f"**Verdict:** {outcome.verdict.value}\n\n"
            f"{outcome.summary}\n\n"
            f"_— Ferova review bot, {outcome.model_used}, "
            f"{outcome.tokens_used} tokens, {outcome.elapsed_s}s_"
        )
        res = self._gh.pr_review_submit(
            pr_number,
            verdict=outcome.verdict.value,
            summary=review_body,
        )
        if res.ok:
            team.posted_reviews += 1
            return
        _log.warning(
            "review_team.review_submit_failed",
            role=outcome.role.value,
            stderr=res.stderr[:200],
        )
        fallback_body = (
            f"_(verdict submission rejected — falling back to a comment)_\n\n{review_body}"
        )
        fb = self._gh.pr_review_comment(pr_number, body=fallback_body)
        if fb.ok:
            team.posted_comments += 1
        else:
            _log.warning(
                "review_team.review_fallback_failed",
                role=outcome.role.value,
                stderr=fb.stderr[:200],
            )

    def _fire_routine(self, team: TeamOutcome) -> None:
        """Notify a Claude Code routine of the team's verdict.

        When ``CLAUDE_CODE_ROUTINE_ID`` + ``CLAUDE_CODE_ROUTINE_TOKEN``
        are set (via ``.env`` locally or repo secrets in GitHub
        Actions), this fires a routine on claude.ai/code/routines that
        spawns a fresh Claude Code session pre-loaded with the review
        outcome — so the user gets the report delivered to the
        assistant rather than having to fetch it.

        Routine config is opt-in: if either is missing, this is a no-op.

        **Quota discipline**: Claude Code routines are capped (typically
        ~15 / day).  Each Coder iteration triggers a sync push, which
        retriggers the workflow, which re-fires the routine — that
        burns the quota fast on a PR that takes 2-3 fix cycles.  We
        therefore only fire on **REQUEST_CHANGES** verdicts (the user
        needs to know action is required) and skip APPROVE / COMMENT
        runs (the bot pipeline can finish those autonomously).
        """
        settings = get_settings()
        routine_id = settings.claude_code_routine_id
        token_secret = settings.claude_code_routine_token
        if not routine_id or token_secret is None:
            return
        token = token_secret.get_secret_value()
        if not token:
            return

        if team.final_verdict != ReviewVerdict.REQUEST_CHANGES:
            _log.info(
                "review_team.routine_skipped",
                reason="non-blocking verdict, sparing routine quota",
                verdict=team.final_verdict.value,
            )
            return

        payload = team_outcome_to_dict(team)
        result = fire_review_routine(
            routine_id=routine_id,
            token=token,
            payload=payload,
        )
        if result.ok:
            _log.info(
                "review_team.routine_fired",
                routine_id=routine_id,
                session_url=result.session_url,
            )
        else:
            _log.warning(
                "review_team.routine_fire_failed",
                routine_id=routine_id,
                status_code=result.status_code,
                error=(result.error or "")[:200],
            )

    def _run_auto_challenge_pass(
        self,
        *,
        pr_number: int,
        outcomes: list[ReviewerOutcome],
        resolved_per_role: dict[BotRole, tuple[ResolvedDisagreement, ...]],
        posted_ids: dict[tuple[str, int], int],
        team: TeamOutcome,
    ) -> None:
        """Auto-challenge new comments that repeat resolved disagreements.

        For each posted inline comment whose body is ≥
        :data:`thread_context.REPEAT_SIMILARITY_THRESHOLD` similar to a
        prior :class:`ResolvedDisagreement` for the same reviewer role
        AND that does NOT refute the cited evidence, post a reply on
        the new inline thread quoting the Coder's prior evidence
        verbatim.  The disagreement is then marked
        ``resolved-by-prior-challenge`` in the team's audit list so the
        archive renderer can surface it on the PR.

        Failures (gh API down, missing id) log + skip; never raise.
        """
        from .thread_context import (
            find_repeat_hallucinations,
            format_auto_challenge_reply,
        )

        for outcome in outcomes:
            resolved = resolved_per_role.get(outcome.role) or ()
            if not resolved:
                continue
            matches = find_repeat_hallucinations(
                list(outcome.comments),
                posted_ids=posted_ids,
                resolved=list(resolved),
            )
            for match in matches:
                body = format_auto_challenge_reply(match)
                res = self._gh.reply_to_review_comment(
                    pr_number,
                    comment_id=match.new_comment_id,
                    body=body,
                )
                if not res.ok:
                    _log.warning(
                        "review.bot.auto_challenge_reply_failed",
                        pr_number=pr_number,
                        role=outcome.role.value,
                        new_comment_id=match.new_comment_id,
                        stderr=(res.stderr or "")[:200],
                    )
                    continue
                team.auto_challenges.append(
                    {
                        "role": outcome.role.value,
                        "file": match.new_comment.file,
                        "line": match.new_comment.line,
                        "new_comment_id": match.new_comment_id,
                        "prior_root_id": match.resolved.root_comment_id,
                        "similarity": round(match.similarity, 3),
                    }
                )
                _log.info(
                    "review.bot.repeat_hallucination_auto_challenged",
                    pr_number=pr_number,
                    role=outcome.role.value,
                    file=match.new_comment.file,
                    line=match.new_comment.line,
                    new_comment_id=match.new_comment_id,
                    prior_root_id=match.resolved.root_comment_id,
                    similarity=round(match.similarity, 3),
                )

    def _upsert_archive_comment(self, team: TeamOutcome) -> None:
        """Push a sticky comment carrying the full TeamOutcome JSON.

        The comment is identified by :attr:`GhCli.ARCHIVE_MARKER` so
        repeat runs replace it instead of stacking.  This gives us a
        machine-readable archive that survives the workflow runner —
        retrievable via :meth:`GhCli.fetch_archive_comment` from any
        machine with PR-read access.
        """
        payload = team_outcome_to_dict(team)
        body_json = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        archive_appendix = (
            f"### Ferova review archive\n\n"
            f"{_render_guard_section(team)}"
            f"<details>\n<summary>Full TeamOutcome (JSON)</summary>\n\n"
            f"```json\n{body_json}\n```\n\n"
            f"</details>\n\n"
            f"_— posted at {datetime.now(UTC).isoformat()}_"
        )
        transcript = _render_dialogue_transcript(
            fetch_dialogue(team.pr_number, db_path=self._db_path)
        )
        if transcript:
            archive_appendix = f"{archive_appendix}\n{transcript}"
        body = self._render_report_body(team, archive_appendix=archive_appendix)
        res = self._gh.upsert_archive_comment(team.pr_number, body=body)
        if not res.ok:
            _log.warning(
                "review_team.archive_upsert_failed",
                pr_number=team.pr_number,
                stderr=res.stderr[:200],
            )

    def _render_report_body(self, team: TeamOutcome, *, archive_appendix: str) -> str:
        """Render the sticky archive as a ledger report (SP-VERDICT-FLIP 10a).

        The headline + facts table are a snapshot of the recorded ledger
        state at this head; the machine-readable ``TeamOutcome`` JSON, guard
        section, and transcript ride below in ``archive_appendix``. The
        authoritative merge decision is the pure gate re-verifying at a
        checked-out head (``ferova review gate`` / auto-merge), never
        this report — the review job runs from the base ref, so on-disk
        re-verification here would be wrong (hence
        :func:`summarise_ledger_facts`, not :func:`gather_merge_facts`).
        """
        facts = summarise_ledger_facts(
            self._db_path,
            pr_number=team.pr_number,
            head_sha=team.head_sha or "",
        )
        decision = compute_merge_decision(facts)
        return render_ledger_report(
            self._db_path,
            pr_number=team.pr_number,
            decision=decision,
            facts=facts,
            archive_appendix=archive_appendix,
        )


def _locate_comment_index(comments, event) -> int:
    """Best-effort match of a :class:`GuardEvent` to a comment index.

    Returns the position of the first comment whose ``(file, line)``
    matches the event, or ``-1`` if no match (the event was on a
    comment that has since been retracted or rewritten by the guard
    itself).  The orchestrator surfaces ``-1`` to the reviewer as
    "no specific comment index" rather than dropping the event —
    the reason text alone is still useful context.
    """
    for i, c in enumerate(comments):
        if c.file == event.file and c.line == event.line:
            return i
    return -1


def _drop_retracted_comments(outcome: ReviewerOutcome) -> ReviewerOutcome:
    """Remove ``severity="retracted"`` comments from one outcome.

    SP-REVIEW-DIALOGUE-A — round-2 self-revision marks comments as
    retracted; the orchestrator drops them from the final aggregation
    before computing blocker / major counts and the team verdict.

    The retracted comments are NOT written back into the outcome — the
    audit trail is preserved separately in ``raw_response``.  When all
    of a reviewer's blockers / majors retract, the verdict softens
    from ``REQUEST_CHANGES`` to ``COMMENT``.
    """
    kept = [c for c in outcome.comments if c.severity != "retracted"]
    if len(kept) == len(outcome.comments):
        return outcome
    new_verdict = outcome.verdict
    if outcome.verdict is ReviewVerdict.REQUEST_CHANGES and not any(
        c.severity in {"blocker", "major"} for c in kept
    ):
        new_verdict = ReviewVerdict.COMMENT
    return ReviewerOutcome(
        role=outcome.role,
        verdict=new_verdict,
        summary=outcome.summary,
        comments=kept,
        model_used=outcome.model_used,
        elapsed_s=outcome.elapsed_s,
        tokens_used=outcome.tokens_used,
        raw_response=outcome.raw_response,
    )


def team_outcome_to_dict(team: TeamOutcome) -> dict:
    """Serialise a TeamOutcome to a JSON-friendly dict (idempotent shape).

    Kept as a free function so the CLI report path can reuse it
    without instantiating an orchestrator.
    """
    return {
        "pr_number": team.pr_number,
        "final_verdict": team.final_verdict.value,
        "n_blockers": team.n_blockers,
        "n_majors": team.n_majors,
        "head_sha": team.head_sha,
        "posted_comments": team.posted_comments,
        "posted_reviews": team.posted_reviews,
        "n_hallucinations_downgraded": len(team.hallucinations),
        "n_auto_challenges": len(team.auto_challenges),
        "auto_challenges": list(team.auto_challenges),
        "hallucinations": [
            {
                "role": ev.role.value,
                "file": ev.file,
                "line": ev.line,
                "original_severity": ev.original_severity,
                "reason": ev.reason,
                "tokens_found": list(ev.tokens_found),
                "original_body": ev.original_body,
            }
            for ev in team.hallucinations
        ],
        "reviews": [
            {
                "role": o.role.value,
                "verdict": o.verdict.value,
                "summary": o.summary,
                "n_comments": len(o.comments),
                "model_used": o.model_used,
                "elapsed_s": round(o.elapsed_s, 3),
                "tokens_used": o.tokens_used,
                "comments": [
                    {
                        "file": c.file,
                        "line": c.line,
                        "severity": c.severity,
                        "body": c.body,
                    }
                    for c in o.comments
                ],
            }
            for o in team.reviews
        ],
    }


def _build_prior_review_context(
    pr_number: int,
    *,
    diff_hash: str,
    db_path: Path,
) -> dict[BotRole, PriorReviewContext]:
    """Load cross-invocation dialogue history and build prior context per reviewer."""
    prior_entries = fetch_dialogue(pr_number, db_path=db_path)
    if not prior_entries:
        return {}
    result: dict[BotRole, PriorReviewContext] = {}
    for role in (BotRole.ARCHITECT, BotRole.SENTINEL, BotRole.TESTER, BotRole.SCRIBE):
        latest = None
        for entry in reversed(prior_entries):
            if entry.speaker == role.value and entry.round in ("1", "2"):
                latest = entry
                break
        if latest is None:
            continue
        prior_hash = latest.payload.get("diff_hash")
        diff_changed = (prior_hash != diff_hash) if prior_hash is not None else None
        try:
            prior_verdict = ReviewVerdict(latest.payload.get("verdict", "COMMENT"))
        except ValueError:
            prior_verdict = ReviewVerdict.COMMENT
        result[role] = PriorReviewContext(
            role=role,
            verdict=prior_verdict,
            summary=(latest.payload.get("summary") or "")[:200],
            n_comments=latest.payload.get("n_comments", 0),
            diff_changed=diff_changed,
        )
    return result


def _compute_diff_hash(diff: str) -> str:
    """Return a truncated SHA-256 hex digest of the diff text."""
    return hashlib.sha256(diff.encode()).hexdigest()[:16]


def _build_review_lessons_block(pr_title: str, diff: str) -> str:
    """Recall review-scoped lessons ONCE per run and render the block.

    The query mixes the PR title with the changed paths so recall
    matches both intent and touched area (SP-REVIEW-MEMORY). Degrades
    to ``""`` via the kill-switch and the graceful agentmemory client,
    so the bench behaves exactly as before when the scope is off or
    the service is down.
    """
    paths = re.findall(r"^diff --git a/(\S+)", diff, flags=re.MULTILINE)[:10]
    query = f"review {pr_title} {' '.join(paths)}".strip()
    lessons = recall_review_lessons(query)
    _log.info("review_team.lessons_recalled", n_lessons=len(lessons))
    return review_lessons_section(lessons)


def _dialogue_payload(
    outcome: ReviewerOutcome,
    *,
    diff_hash: str | None = None,
) -> dict:
    """Serialise a :class:`ReviewerOutcome` for ``pr_review_dialogue``."""
    payload: dict = {
        "role": outcome.role.value,
        "verdict": outcome.verdict.value,
        "summary": outcome.summary[:400],
        "n_comments": len(outcome.comments),
        "comments": [
            {
                "file": c.file,
                "line": c.line,
                "severity": c.severity,
                "body": c.body[:400],
            }
            for c in outcome.comments
        ],
    }
    if diff_hash is not None:
        payload["diff_hash"] = diff_hash
    return payload


def _render_dialogue_transcript(entries: list[DialogueEntry]) -> str:
    """Render ``## Dialogue transcript`` from persisted dialogue rows.

    Empty when ``entries`` is empty.  Groups entries by round
    (``"1"`` → Round 1, ``"2"`` → Round 2, ``"challenge"`` → Coder
    challenges).  Each row is one bullet:

    * ``- **Architect** APPROVE — short summary``
    * ``- comment #1: **CHALLENGE** — `foo.py:343-358` ✓ verified``
    """
    if not entries:
        return ""
    rounds: dict[str, list[DialogueEntry]] = {}
    for e in entries:
        rounds.setdefault(e.round, []).append(e)
    parts: list[str] = ["## Dialogue transcript", ""]
    if "1" in rounds:
        parts.append("### Round 1 (initial verdicts)")
        for e in rounds["1"]:
            parts.append(_render_reviewer_bullet(e))
        parts.append("")
    if "2" in rounds:
        parts.append("### Round 2 (after peer review + guard verdicts)")
        for e in rounds["2"]:
            parts.append(_render_reviewer_bullet(e))
        parts.append("")
    if "challenge" in rounds:
        parts.append("### Coder challenges")
        for e in rounds["challenge"]:
            parts.append(_render_challenge_bullet(e))
        parts.append("")
    return "\n".join(parts)


def _render_reviewer_bullet(entry: DialogueEntry) -> str:
    """One bullet for round-1/round-2 reviewer entries."""
    role = entry.payload.get("role") or entry.speaker
    verdict = entry.payload.get("verdict", "?")
    summary = (entry.payload.get("summary") or "").replace("\n", " ").strip()[:160]
    suffix = f" — {summary}" if summary else ""
    return f"- **{role.title()}** {verdict}{suffix}"


def _render_challenge_bullet(entry: DialogueEntry) -> str:
    """One bullet for a Coder challenge entry."""
    p = entry.payload
    idx = p.get("comment_index", "?")
    decision = str(p.get("decision", "?"))
    rationale = (p.get("rationale") or "").replace("\n", " ").strip()[:160]
    evidence = ""
    ep = p.get("evidence_path")
    if ep:
        el = p.get("evidence_lines") or "?"
        verified_mark = "✓" if decision == "CHALLENGE" else "—"
        evidence = f" — `{ep}:{el}` {verified_mark}"
    suffix = f" — {rationale}" if rationale else ""
    return f"- comment #{idx}: **{decision}**{evidence}{suffix}"


def _render_guard_section(team: TeamOutcome) -> str:
    """Render the human-readable Guard activity section for the archive comment.

    Empty when no downgrade fired — keeps the archive comment uncluttered
    on PRs where the bots stayed honest.
    """
    if not team.hallucinations:
        return ""
    by_role: dict[str, int] = {}
    rows: list[str] = []
    for ev in team.hallucinations:
        by_role[ev.role.value] = by_role.get(ev.role.value, 0) + 1
        tokens = ", ".join(ev.tokens_found) if ev.tokens_found else "—"
        rows.append(
            f"| {ev.role.value} | `{ev.file}:{ev.line}` | "
            f"{ev.original_severity} → nit | {ev.reason} | {tokens} |"
        )
    breakdown = ", ".join(f"{role}: {n}" for role, n in sorted(by_role.items()))
    table = "\n".join(rows)
    return (
        f"**Guard activity:** {len(team.hallucinations)} hallucination(s) "
        f"downgraded ({breakdown}).\n\n"
        f"<details>\n<summary>Per-comment downgrades</summary>\n\n"
        f"| role | location | severity | reason | tokens |\n"
        f"|---|---|---|---|---|\n"
        f"{table}\n\n"
        f"</details>\n\n"
    )


def run_review(pr_number: int, *, post: bool = True) -> TeamOutcome:
    """Module-level convenience wrapper used by the CLI command.

    Args:
        pr_number: GitHub PR number to review.
        post: When False, do not publish anything to GitHub (dry-run).

    Returns:
        The :class:`TeamOutcome` aggregated across the four reviewers.
    """
    return ReviewTeamOrchestrator(post_to_github=post).review_pr(pr_number)
