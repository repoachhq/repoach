"""Review-team orchestration: the evidence-first pipeline from diff to posted findings.

``ReviewTeamOrchestrator.review_pr`` is the entry point invoked by the
CLI command (``repoach review pr``) and the auto-review GitHub Actions
workflow.  Given a PR number, it drives the following pipeline, in
order:

1. Fetches the diff via ``gh pr diff``.
2. Submits the fresh-head guard (:func:`resolve_fresh_head`) to a
   background thread so its up-to-30-second poll against the GitHub
   API overlaps with the four-reviewer fan-out instead of blocking
   it (SP-FRESH-HEAD-CONCURRENT).  The resolved head SHA is joined
   immediately before the findings ledger write.
3. Auto-loads the active spec from the PR's head branch.
4. Fetches resolved disagreements per reviewer.
5. Runs the four reviewer bots (Architect, Sentinel, Tester, Scribe)
   concurrently.
6. Applies the hallucination guard to disprove unsupported "missing
   X" claims before they reach a human.
7. Optionally runs a round-2 confirm-or-retract dialogue for every
   reviewer that flagged a blocker or major, so a challenged claim
   can be retracted with evidence.
8. Records each finding plus a review-integrity row, so every claim
   is traceable back to its origin.
9. Runs mechanical verification (``finding_verifiers``) and the
   adversarial refuter over judged claims, advancing each finding
   through its lifecycle in the findings ledger.
10. Derives the team verdict from the findings ledger via
    :func:`merge_gate.verdict_from_facts` — the verdict is *derived*
    from the ledger's open findings, not aggregated from per-bot
    verdicts.
11. Posts inline comments, per-bot reviews, and a sticky archive
    comment on the PR.  The archive comment is report-only: a
    human-readable snapshot of the run, not the retrievable source of
    truth (that remains the findings ledger and ``pr_reviews`` in L4).
12. Persists every outcome in L4 ``pr_reviews``.

Auto-merge and the findings-driven Coder fix loop are separate,
workflow-driven follow-ups hosted in ``auto_merge.py`` and
``coder_findings.py`` respectively — the orchestrator reviews a PR;
it never merges one.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from ..core.paths import project_root
from ..core.redaction import redact_error
from ..forge import (
    ARCHIVE_MARKER,
    Forge,
    ForgeError,
    ForgeStateRefusedError,
    InlineCommentDraft,
    Review,
    ReviewDraft,
    WriteRefusal,
    select_forge,
)
from ..forge.protocol import diff_files
from ..gates.config import (
    load_gates_config,
    resolve_refuter_budget,
)
from ..project.config import load_project_config
from ..project.spec_source import (
    SpecResolution,
    declared_spec_missing,
    resolve_spec,
)
from ..providers.select import require_provider_ready
from ..providers.usage import bind_reviewer, current_run_id, reviewer_scope
from .diff_scoper import scope_diff
from .finding_verifiers import (
    MechanicalVerification,
    VerificationReport,
    recorded_verifications,
    verify_findings_for_pr,
)
from .findings import ReviewRun, fetch_completed_review_run, record_review_run
from .findings_bridge import (
    RecordedFindings,
    record_findings_for_outcomes,
    record_oversized_findings,
    recount_findings_at_head,
)
from .generated_artefacts import (
    ReviewScope,
    record_generated_findings,
    report_json,
    report_lines,
    review_scope,
)
from .generated_evidence import load_review_evidence
from .generated_store import fetch_generated_checks, record_generated_checks
from .generated_types import GeneratedCheck, GeneratedTiming
from .hallucination_guard import (
    GuardEvent,
    apply_hallucination_guard,
    make_repo_file_reader,
    make_repo_symbol_searcher,
)
from .head_reverify import (
    NO_HEAD_TREE,
    HeadReverification,
    Reverification,
    reverify_older_findings,
    skipped_reverification,
)
from .merge_gate import (
    NO_REVERIFICATION,
    MergeFacts,
    compute_merge_decision,
    summarise_ledger_facts,
    verdict_from_facts,
)
from .notifier import fire_review_routine
from .persistence import (
    DialogueEntry,
    fetch_dialogue,
    init_schema,
    record_dialogue,
    record_hallucination,
    record_review,
)
from .refuter import (
    NOT_YET_MECHANICALLY_VERIFIED,
    Refutation,
    configured_max_judged,
    judge_findings_for_pr,
)
from .report import ReportFinding, render_ledger_report, report_findings
from .review_cache import (
    cache_line,
    completed_run,
    replayed_outcomes,
    replayed_refutations,
    replayed_spec,
)
from .review_head import materialise_head, require_head_sha
from .review_lessons import remember_verified_findings
from .review_memory import recall_review_lessons, review_lessons_section
from .reviewer import (
    _DIFF_HARD_CAP_CHARS,
    Architect,
    BotRole,
    DialogueContext,
    GuardEventSummary,
    PeerOutcome,
    PriorReviewContext,
    ReviewComment,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
    make_file_excerpts,
)
from .spec import SpecPlan, load_spec
from .spec_gate import (
    RESOLUTION_DECLARED,
    RESOLUTION_NONE,
    RESOLUTION_OWNERSHIP,
    DraftScope,
    draft_scope_at_head,
    record_spec_coverages,
)
from .spec_grading import (
    CoverageGrading,
    coverage_rows_at_head,
    failed_grading_row,
    readable_base,
    resolution_payload,
)
from .thread_context import ResolvedDisagreement

_log = get_logger(__name__)


def _render_comment_body(outcome: ReviewerOutcome, comment: ReviewComment) -> str:
    """Render one inline comment's markdown body for posting.

    Shared by :meth:`ReviewTeamOrchestrator._publish_batched_review`
    and :meth:`ReviewTeamOrchestrator._publish_per_comment` so both the
    batched and the pre-batching fallback path render the exact same
    ``**[{role}/{severity}]** {body}`` template.
    """
    return (
        f"**[{outcome.role.value}/{comment.severity}]** "
        f"{comment.body}\n\n_— Repoach review-bot ({outcome.model_used})_"
    )


def _render_review_body(outcome: ReviewerOutcome) -> str:
    """Render one reviewer's verdict + summary footer as review body markdown.

    Shared by :meth:`ReviewTeamOrchestrator._publish_batched_review`
    and :meth:`ReviewTeamOrchestrator._publish_per_comment` so both
    paths submit the exact same review body.
    """
    return (
        f"### {outcome.role.value.title()} review\n\n"
        f"**Verdict:** {outcome.verdict.value}\n\n"
        f"{outcome.summary}\n\n"
        f"_— Repoach review bot, {outcome.model_used}, "
        f"{outcome.tokens_used} tokens, {outcome.elapsed_s}s_"
    )


def record_review_ledger(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str | None,
    outcomes: list[ReviewerOutcome],
    round_n: int,
    diff: str,
    generated: Sequence[GeneratedCheck] = (),
    generated_timing: GeneratedTiming | None = None,
) -> RecordedFindings | None:
    """Persist the review findings, then the integrity row — fail closed.

    The integrity row is written ONLY after the findings write succeeds
    (SP-FINDINGS-WRITE-FAIL-CLOSED, audit 2026-07-13 finding H3). The
    two writes used to sit in independent ``try`` blocks: a transient
    DB/transport failure of :func:`record_findings_for_outcomes` was
    swallowed to a warning while a clean integrity row was still
    written, yielding a head that read as fully reviewed with ZERO
    findings — which the merge gate turns into APPROVE. Skipping the
    integrity row on a failed findings write makes the gate see no
    complete review at that head (``review_complete`` False), so the
    head can never merge on the strength of a review whose findings
    were lost.

    The same re-scoping the reviewer bots ran the diff through
    (:func:`diff_scoper.scope_diff` at ``_DIFF_HARD_CAP_CHARS``) is
    re-run here so any file whose diff exceeded the cap — never shown
    to any reviewer lens — gets its own fail-closed BLOCKING finding
    via :func:`findings_bridge.record_oversized_findings`
    (SP-DIFF-SCOPER-OVERSIZE-BLOCK): padding a hostile change past the
    cap no longer removes it from every review lens while keeping the
    PR mergeable. This runs inside the same fail-closed write as the
    comment-derived findings, so a failure here also skips the
    integrity row.

    Args:
        db_path: The findings + integrity ledger database.
        pr_number: The PR reviewed.
        head_sha: The head the review ran against.
        outcomes: Every reviewer outcome of the pass, parsed or not.
        round_n: The dialogue round the findings belong to.
        diff: The reviewed diff (scopes findings to touched files), the
            declared generated artefacts already summarised.
        generated: The check runs of the declared generated artefacts the
            diff touches, recorded as ``generated_checks`` rows with one
            blocking ``generated_artefact`` finding per path they do not
            prove (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G2, G3), inside
            the same fail-closed write.
        generated_timing: The evidence's measured delay, stored on the rows.

    Returns:
        What the findings write did (new / already recorded / refuted
        earlier, SP-FINDINGS-BRIDGE-DOCFIX 0.2) when both writes landed;
        None when either failed (the failure is logged loudly and the
        head stays non-mergeable).
    """
    try:
        recorded = record_findings_for_outcomes(
            db_path,
            pr_number=pr_number,
            head_sha=head_sha,
            outcomes=outcomes,
            round_n=round_n,
            diff=diff,
            run_id=current_run_id(),
        )
        scoped = scope_diff(diff, _DIFF_HARD_CAP_CHARS)
        n_oversized = record_oversized_findings(
            db_path,
            pr_number=pr_number,
            head_sha=head_sha,
            round_n=round_n,
            scoped=scoped,
        )
        n_generated = 0
        if generated:
            record_generated_checks(
                db_path,
                pr_number=pr_number,
                head_sha=head_sha or "",
                checks=generated,
                timing=generated_timing,
            )
            n_generated = record_generated_findings(
                db_path, generated, pr_number=pr_number, round_n=round_n
            )
        _log.info(
            "review_team.findings_recorded",
            pr_number=pr_number,
            n_generated_blocking=n_generated,
            n_findings=recorded.new,
            n_already_recorded=recorded.already_recorded,
            n_refuted_earlier=recorded.refuted_earlier,
            n_oversized_blocking=n_oversized,
        )
    except Exception as exc:
        _log.error(
            "review_team.findings_record_failed",
            pr_number=pr_number,
            exc=type(exc).__name__,
            message=str(exc)[:200],
        )
        _log.error(
            "review_team.integrity_skipped_findings_failed",
            pr_number=pr_number,
            head_sha=head_sha,
        )
        return None

    try:
        from .findings import record_review_integrity
        from .findings_bridge import _is_unparsed

        n_unparsed = sum(1 for o in outcomes if _is_unparsed(o))
        record_review_integrity(
            db_path,
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
        return None
    return recorded


@dataclass
class TeamOutcome:
    """Aggregated outcome of the four reviewers on one PR.

    ``final_verdict`` is sourced from the findings ledger
    (:func:`merge_gate.verdict_from_facts`), not the retired consensus
    gate, except that one reviewer ending ``ERROR`` (transport exhausted,
    bot crashed) makes it ``ERROR``. ``posting_outcome`` is
    ``"SKIP_ERROR"`` when posting was requested but suppressed because a
    reviewer errored, and ``"SKIP_STATE"`` when the forge refused
    a write because the PR / MR is no longer open; posting then stopped
    at that first refused write. ``spec_id`` / ``spec_source`` name the
    spec the review ran against and where it was declared (``flag``,
    ``description``, ``branch``), both None when none was declared;
    ``spec_covered`` is None when no coverage was computed;
    ``spec_missing`` is True when a ``Spec:`` line named an id with no
    document (the review then runs without a spec). ``refutations``
    holds one entry per finding the refuter saw, each with its state and
    reason; ``refuter_summary`` is the one line naming them
    (SP-REFUTER-JUDGES-WHAT-IT-CAN), None when the refuter never ran.
    ``verifications`` holds one entry per finding a mechanical verifier
    settled at the head (read back from the ledger on a replay) and
    ``reverifications`` one per finding of an older head re-checked
    there. ``findings`` merges the three into one list, each entry
    naming its judge (``mechanical`` / ``refuter`` / ``reverify`` /
    ``ledger``), plus every finding the ledger counts as open blocking
    that no judge of this run named; ``verdict_basis`` is the ids the
    verdict counts as open blocking, each one entry of ``findings``.
    ``tree`` is ``worktree`` or ``checkout`` when the review read a
    pinned head tree (SP-REVIEW-PINS-HEAD), and ``worktree`` its path
    when it was kept. ``reverified`` counts the findings of older heads
    re-checked at the head before the verdict (SP-GATE-REVERIFIES-AT-HEAD
    G7), ``reverify_reasons`` names each one still blocking and
    ``reverify_line`` is the one stderr line saying it (empty when no
    head was known). ``findings_recorded`` counts what the review's
    findings write did at the head (new, already recorded, refuted
    earlier; SP-FINDINGS-BRIDGE-DOCFIX 0.2) and ``findings_line`` is the
    stderr line saying it, empty when the write failed. ``cached`` is
    True when the run replayed a completed run at the same head instead
    of calling the reviewers (SP-FINDINGS-BRIDGE-DOCFIX 0.3) and
    ``cache_line`` the stderr line naming that run; a replay recomputes
    the spec coverage at its head with the current rule and
    ``spec_coverage_line`` says so (empty on a fresh run).
    ``spec_report`` holds the coverage lines reported on stderr
    (``plan-absent: …``, ``spec resolved by ownership: …``,
    ``ungoverned paths: …``) and ``spec_resolution`` how the graded specs
    were found (``declared`` / ``ownership`` / ``none``,
    SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS G7); a PR declaring no
    spec is graded against the specs owning its code, ``spec_source``
    ``ownership``.
    ``generated`` lists each check run of a declared generated artefact
    the diff touches (``path``, ``check``, ``state``, ``ms``,
    ``sources_changed``, ``producer_inputs_changed``, ``script_ref``) and
    ``generated_lines`` the report lines naming what proved each one
    (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G5).
    ``refused_writes`` lists every write the forge's
    barrier refused during the run (SP-DRY-RUN-NEVER-POSTS: empty on a
    healthy dry run, any entry is a defect) and ``write_refusal`` names
    why the barrier was closed, None when writes went through.
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
    posting_outcome: str | None = None
    spec_id: str | None = None
    spec_source: str | None = None
    spec_covered: bool | None = None
    spec_missing: bool = False
    refutations: list[Refutation] = field(default_factory=list)
    refuter_summary: str | None = None
    verifications: list[MechanicalVerification] = field(default_factory=list)
    reverifications: list[Reverification] = field(default_factory=list)
    findings: list[ReportFinding] = field(default_factory=list)
    verdict_basis: list[int] = field(default_factory=list)
    refused_writes: list[str] = field(default_factory=list)
    write_refusal: str | None = None
    tree: str | None = None
    worktree: str | None = None
    reverified: dict[str, int] = field(default_factory=lambda: dict(NO_REVERIFICATION))
    reverify_reasons: list[str] = field(default_factory=list)
    reverify_line: str = ""
    findings_recorded: dict[str, int] = field(default_factory=dict)
    findings_line: str = ""
    cached: bool = False
    cache_line: str = ""
    spec_coverage_line: str = ""
    spec_report: list[str] = field(default_factory=list)
    spec_resolution: dict[str, object] = field(default_factory=dict)
    generated: list[dict[str, object]] = field(default_factory=list)
    generated_lines: list[str] = field(default_factory=list)


class ReviewTeamOrchestrator:
    """Run the four reviewer bots in parallel and post their findings.

    Args:
        forge: Optional pre-built :class:`~repoach.forge.Forge`; selected
            for ``repo_root`` (:func:`~repoach.forge.select_forge`) when omitted.
        db_path: Path to the L4 SQLite db.  Defaults to settings.db_path.
        post_to_github: When False, do not publish anything to GitHub
            (dry-run mode for tests / local previews); the forge's write
            barrier is then closed, so a write that reaches it is refused
            and counted instead of sent (SP-DRY-RUN-NEVER-POSTS).
        write_refusal: The reason the barrier names when
            ``post_to_github`` is False; ``posting disabled`` by default.
        max_workers: Thread-pool worker count for the four reviewers.
        repo_root: The tree every reader resolves against (the PR head
            tree under :func:`run_review`); the working directory when
            omitted.
        head_sha: The head the caller already resolved and materialised;
            when given, the fresh-head guard is skipped and every ledger
            row carries it, dry run included (SP-REVIEW-PINS-HEAD).
        config_root: Where ``repoach.toml`` is read from (``[gates]``,
            ``[review]``): the trusted checkout. The PR head tree under
            ``repo_root`` is evidence only and never configures the
            review; defaults to ``repo_root``.
    """

    def __init__(
        self,
        *,
        forge: Forge | None = None,
        db_path: Path | None = None,
        post_to_github: bool = True,
        max_workers: int = 4,
        repo_root: Path | None = None,
        head_sha: str | None = None,
        config_root: Path | None = None,
        write_refusal: WriteRefusal | None = None,
        force: bool = False,
        generated_evidence: Path | None = None,
    ) -> None:
        """Initialise the orchestrator with optional dependencies.

        ``force`` re-reviews a pinned head that already has a completed
        run instead of replaying it (SP-FINDINGS-BRIDGE-DOCFIX 0.3).
        ``generated_evidence`` is the ``review-generated.json`` the
        secret-free verification job wrote; the review never runs a check
        itself (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G2).
        """
        settings = get_settings()
        self._pinned_head = head_sha
        self._repo_root = repo_root or Path.cwd()
        self._config_root = config_root or self._repo_root
        self._forge = forge or select_forge(cwd=self._repo_root)
        if not post_to_github:
            self._forge.barrier.close(write_refusal or WriteRefusal.POSTING_DISABLED)
        self._db_path = db_path or Path(settings.db_path)
        self._post = post_to_github
        self._max_workers = max_workers
        self._force = force
        self._generated_evidence = generated_evidence
        init_schema(self._db_path)

    def _load_declared_spec(self, declared: SpecResolution) -> SpecPlan | None:
        """Load the spec a pull request declares, or None.

        A declared id with no document under the spec directory is
        reviewed without a spec. A ``Spec:`` line naming a missing
        document is a warning (the author asked for a spec); a branch
        naming one stays an info, as it always was. The ``--spec`` flag
        is checked by the CLI before the review starts.

        Args:
            declared: The resolved id and its source.

        Returns:
            The loaded spec, or None.
        """
        if declared.spec_id is None:
            return None
        try:
            return load_spec(declared.spec_id, root=self._repo_root)
        except FileNotFoundError as exc:
            log = _log.warning if declared.source == "description" else _log.info
            log(
                "review_team.declared_spec_missing",
                spec_id=declared.spec_id,
                source=declared.source,
                error=str(exc)[:200],
            )
            return None

    def review_pr(self, pr_number: int, *, spec_flag: str | None = None) -> TeamOutcome:
        """Review ``pr_number`` (:meth:`_review_pr`) and report the barrier's refusals.

        ``refused_writes`` on the outcome lists the writes the forge's
        barrier refused during this run and ``write_refusal`` why it was
        closed (SP-DRY-RUN-NEVER-POSTS).
        """
        refused_before = len(self._forge.barrier.refused)
        team = self._review_pr(pr_number, spec_flag=spec_flag)
        team.refused_writes = self._forge.barrier.refused[refused_before:]
        refusal = self._forge.barrier.refusal
        team.write_refusal = refusal.value if refusal is not None else None
        return team

    def _review_pr(self, pr_number: int, *, spec_flag: str | None = None) -> TeamOutcome:
        """Run the four reviewers in parallel and return aggregated outcome.

        Args:
            pr_number: GitHub PR number.
            spec_flag: The ``--spec`` value; outranks the description's
                ``Spec:`` line and the branch name (SP-UNIVERSAL-PROJECT-CONFIG G4).

        Returns:
            A :class:`TeamOutcome` with each reviewer's verdict, the
            aggregated team verdict, and posting statistics.

        Pipeline:
            1. **Fetch diff.**  Failure to read the PR diff is fatal —
               we return an ``ERROR`` outcome rather than
               crash the workflow.
            2. **Submit fresh-head guard to background thread**
               (SP-FRESH-HEAD-CONCURRENT) — when posting to GitHub,
               :func:`resolve_fresh_head` is submitted to a dedicated
               ``ThreadPoolExecutor(max_workers=1)`` so its
               up-to-30-second poll against the GitHub API overlaps
               with the four-reviewer fan-out (step 5) instead of
               blocking it.  The resolved head SHA is joined
               immediately before the findings ledger write and passed
               to :func:`record_review_ledger`.  When
               ``post_to_github=False`` no background thread is
               created and ``head_sha`` stays ``None``.
            3. **Auto-load active spec** (SP-DEV-V2-B2) from the PR's
               head branch so every reviewer reads the spec alongside
               the diff.  Spec lookup failures are logged and treated
               as "no spec" — never fatal.
            4. **Fetch resolved disagreements per reviewer** (SP-CODER-
               EVIDENCE-CHALLENGE phase 3B) — root threads the Coder
               has already answered with "Verified — challenge with
               evidence".  Logging + integration in production traffic
               ; the prompt-template bump that makes the model act on
               them ships in a follow-up.
            5. **Run the four reviewers in parallel** through a
               :class:`ThreadPoolExecutor`.  A single bot blowing up
               (NIM chain exhaustion, parser crash) records a degraded
               ``ERROR`` outcome — the others run, nothing is published.
            6. **Hallucination guard** (SP-DEV-V2-B3) — post-process
               each outcome to disprove "missing X" claims by grepping
               the working tree, and to catch the self-referential
               prompt-template hallucination mode.  Defence in depth
               alongside the anti-hallucination paragraphs in the
               persona files.
            7. **Persist round-1 verdicts** to L4 ``review_dialogue``
               so the dialogue transcript can be replayed later.
            8. **Round 2** (SP-REVIEW-DIALOGUE-A) — when at least one
               reviewer flagged a blocker / major, every flagging
               reviewer gets a second pass with peer outcomes + its
               own guard verdicts + live file excerpts.  Comments
               retracted in round 2 drop from the final aggregation.
               At most one round 2 per PR.  Only the reviewers whose
               outcome was actually replaced get a round-2 dialogue
               row (crashes keep the round-1 object in place).
            9. **Ledger-sourced team verdict** (SP-VERDICT-RESOURCE-LEDGER)
               — ``final_verdict`` is derived from the findings ledger via
               :func:`merge_gate.verdict_from_facts` (REQUEST_CHANGES on an
               open blocking finding or an incomplete review), not the
               retired consensus gate.  Iteration happens at the workflow
               layer : each new commit on the impl branch re-fires
               ``auto-review.yml`` which calls back into us.

        Before any of it, a pinned head with a completed run in the
        ledger is replayed (:meth:`_replay`) unless ``force`` was given:
        no reviewer and no refuter is called (SP-FINDINGS-BRIDGE-DOCFIX
        0.3).
        """
        cached = self._completed_run(pr_number)
        if cached is not None:
            return self._replay(pr_number, cached)
        try:
            diff = self._forge.diff(pr_number).text
        except ForgeError as exc:
            _log.error(
                "review_team.diff_fetch_failed",
                pr_number=pr_number,
                stderr=exc.message[:200],
            )
            return TeamOutcome(
                pr_number=pr_number,
                final_verdict=ReviewVerdict.ERROR,
                n_blockers=0,
                n_majors=0,
            )
        diff_hash = _compute_diff_hash(diff)

        head_guard_pool: ThreadPoolExecutor | None = None
        head_guard_future: Future[str | None] | None = None
        if self._post and self._pinned_head is None:
            head_guard_pool = ThreadPoolExecutor(max_workers=1)
            head_guard_future = head_guard_pool.submit(
                resolve_fresh_head, self._forge, pr_number, repo_root=self._repo_root
            )
        head_sha: str | None = None

        spec_plan_md: str | None = None
        spec_id: str | None = None
        spec_source: str | None = None
        spec_covered: bool | None = None
        spec_missing = False
        pr_title = ""
        arch_edges_block = ""
        anchored = False
        base_ref: str | None = None
        try:
            pr = self._forge.pull_request(pr_number)
            pr_title = pr.title
            head_ref = pr.head_ref
            base_ref = pr.base_ref or None
            declared = resolve_spec(
                flag=spec_flag,
                description=pr.body,
                branch=head_ref,
                sources=load_project_config(project_root(self._config_root)).review.spec_sources,
                root=self._repo_root,
            )
            spec = self._load_declared_spec(declared)
            if spec is not None:
                spec_plan_md = spec.raw_markdown
                spec_id = spec.id
                spec_source = declared.source
            elif declared_spec_missing(declared, self._repo_root):
                spec_id = declared.spec_id
                spec_source = declared.source
                spec_missing = True
            elif head_ref:
                from .subspec_anchor import maybe_anchor_decomposed_parent

                anchor = maybe_anchor_decomposed_parent(head_ref, root=self._repo_root)
                if anchor is not None:
                    spec_plan_md = anchor.spec_plan_md
                    spec_id = anchor.parent_id
                    spec_source = "branch"
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

        if spec_id and not anchored and not spec_missing:
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

        generated_scope = self._generated_scope(diff, base_ref)
        reviewed_diff = generated_scope.review_diff

        reviewers: list[Reviewer] = [
            Architect(db_path=self._db_path),
            Sentinel(db_path=self._db_path),
            Tester(db_path=self._db_path),
            Scribe(db_path=self._db_path),
        ]

        from .thread_context import fetch_resolved_disagreements

        resolved_per_role: dict[BotRole, tuple[ResolvedDisagreement, ...]] = {}
        try:
            for r in reviewers:
                items = fetch_resolved_disagreements(self._forge, pr_number, role=r.role)
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

        lessons_block = _build_review_lessons_block(pr_title, reviewed_diff)

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = [
                (
                    r,
                    pool.submit(
                        bind_reviewer(r.role.value.title(), r.review_diff),
                        reviewed_diff,
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
                            verdict=ReviewVerdict.ERROR,
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
            diff=reviewed_diff,
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
                and o.verdict not in {ReviewVerdict.APPROVE, ReviewVerdict.ERROR}
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

        errored_roles = [o.role.value for o in outcomes if o.verdict == ReviewVerdict.ERROR]
        publish = self._post and not errored_roles
        if errored_roles:
            _log.error(
                "review_team.reviewer_errored",
                pr_number=pr_number,
                roles=errored_roles,
                posting="suppressed",
            )

        n_blockers = sum(1 for o in outcomes for c in o.comments if c.severity == "blocker")
        n_majors = sum(1 for o in outcomes for c in o.comments if c.severity == "major")

        head_sha = self._pinned_head or self._join_head_guard(
            head_guard_pool, head_guard_future, pr_number=pr_number
        )

        generated_checks, generated_timing = load_review_evidence(
            self._generated_evidence,
            generated_scope.touched,
            head_sha=head_sha or "",
            head_dir=self._repo_root,
            changed_files=generated_scope.changed,
            base_sha=generated_scope.base_sha,
        )
        recorded = record_review_ledger(
            self._db_path,
            pr_number=pr_number,
            head_sha=head_sha,
            outcomes=outcomes,
            round_n=2 if round2_ran else 1,
            diff=reviewed_diff,
            generated=generated_checks,
            generated_timing=generated_timing,
        )

        verification = VerificationReport()
        try:
            verification = verify_findings_for_pr(
                self._db_path,
                pr_number=pr_number,
                repo_root=self._repo_root,
                head_sha=head_sha,
                config=load_gates_config(self._config_root),
            )
            _log.info("review_team.findings_verified", pr_number=pr_number, **verification.counts)
        except Exception as exc:
            _log.warning(
                "review_team.findings_verify_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence=f"unjudged claim types reported unverified: {NOT_YET_MECHANICALLY_VERIFIED}",
            )

        refutations: list[Refutation] = []
        refuter_summary: str | None = None
        refuter_calls = 0
        budget = resolve_refuter_budget(self._config_root)
        try:
            with reviewer_scope("Refuter"):
                judging = judge_findings_for_pr(
                    self._db_path,
                    pr_number=pr_number,
                    repo_root=self._repo_root,
                    head_sha=head_sha,
                    max_judged=configured_max_judged(self._config_root),
                    diff_files=diff_files(diff),
                    tree_sha=_local_git(self._repo_root, "rev-parse", "HEAD"),
                    review_head=head_sha or _served_head(self._forge, pr_number),
                    scope_head=head_sha,
                    diff=reviewed_diff,
                    pr_spec=(spec_id, spec_plan_md) if spec_id and spec_plan_md else None,
                    budget=budget,
                )
            refutations = list(judging.outcomes)
            refuter_summary = judging.summary_line()
            refuter_calls = judging.model_calls
            _log.info(
                "review_team.findings_judged",
                pr_number=pr_number,
                model_calls=judging.model_calls,
                stale=judging.stale,
                **judging.counts,
            )
            for deferral in judging.deferrals:
                _log.info(
                    "review_team.finding_deferred",
                    pr_number=pr_number,
                    finding_id=deferral.finding_id,
                    file=deferral.file,
                    line=deferral.line_start,
                    claim_type=deferral.claim_type.value,
                    state=deferral.state,
                    reason=deferral.reason,
                )
        except Exception as exc:
            refuter_summary = f"refuter: failed: {type(exc).__name__}, no finding judged"
            _log.warning(
                "review_team.findings_judge_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        reverified = self._reverify_older_heads(
            pr_number, head_sha=head_sha, refuter_calls=refuter_calls
        )

        state_refused: ForgeStateRefusedError | None = None
        try:
            from .thread_context import post_refuted_finding_sentinels

            if publish:
                n_sentinels = post_refuted_finding_sentinels(self._forge, self._db_path, pr_number)
                _log.info(
                    "review_team.refuted_sentinels_posted",
                    pr_number=pr_number,
                    n_sentinels=n_sentinels,
                )
            else:
                _log.info("review_team.refuted_sentinels_skipped_dry_run", pr_number=pr_number)
        except ForgeStateRefusedError as exc:
            state_refused = exc
        except Exception as exc:
            _log.warning(
                "review_team.refuted_sentinels_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        try:
            if publish:
                self._fire_proposed_escalation_dossier(pr_number)
            else:
                _log.info("review_team.proposed_escalation_skipped_dry_run", pr_number=pr_number)
        except Exception as exc:
            _log.warning(
                "review_team.proposed_escalation_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )

        grading = None
        if not spec_missing:
            grading = self._record_spec_coverage(
                pr_number,
                spec_id=spec_id,
                head_sha=head_sha,
                base_ref=base_ref,
                changed_files=diff_files(diff),
            )
            if grading is not None and grading.rows:
                spec_covered = all(row.covered for row in grading.rows)
                if spec_id is None:
                    spec_source = grading.source

        ledger_facts = summarise_ledger_facts(
            self._db_path, pr_number=pr_number, head_sha=head_sha or ""
        )
        final = ReviewVerdict.ERROR if errored_roles else verdict_from_facts(ledger_facts)
        verdict_findings = self._verdict_findings(
            pr_number,
            facts=ledger_facts,
            verifications=verification.settled,
            refutations=refutations,
            reverifications=reverified.outcomes,
        )

        team = TeamOutcome(
            pr_number=pr_number,
            final_verdict=final,
            n_blockers=n_blockers,
            n_majors=n_majors,
            head_sha=head_sha,
            reviews=outcomes,
            hallucinations=all_events,
            spec_id=spec_id,
            spec_source=spec_source,
            spec_covered=spec_covered,
            spec_missing=spec_missing,
            spec_report=_spec_report(grading),
            spec_resolution=_spec_resolution(grading, spec_id),
            refutations=refutations,
            refuter_summary=refuter_summary,
            verifications=list(verification.settled),
            reverifications=list(reverified.outcomes),
            findings=verdict_findings,
            verdict_basis=list(ledger_facts.open_blocking_ids),
            reverified=reverified.counts(),
            reverify_reasons=reverified.reasons(),
            reverify_line=reverified.summary_line("review") if head_sha else "",
            findings_recorded=recorded.counts() if recorded is not None else {},
            findings_line=recorded.summary_line() if recorded is not None else "",
            generated=report_json(generated_checks),
            generated_lines=report_lines(generated_checks, generated_timing),
        )

        if not errored_roles and recorded is not None and self._pinned_head:
            self._store_completed_run(
                run_id=recorded.run_id, outcomes=outcomes, team=team, head_sha=self._pinned_head
            )

        for outcome in outcomes:
            record_review(
                self._db_path,
                pr_number=pr_number,
                outcome=outcome,
            )
        for event in all_events:
            record_hallucination(self._db_path, pr_number=pr_number, event=event)

        if self._post and errored_roles:
            team.posting_outcome = "SKIP_ERROR"
        if publish and state_refused is None:
            try:
                self._publish_all(
                    pr_number=pr_number,
                    outcomes=outcomes,
                    head_sha=head_sha,
                    team=team,
                    resolved_per_role=resolved_per_role,
                )
            except ForgeStateRefusedError as exc:
                state_refused = exc
        if state_refused is not None:
            team.posting_outcome = "SKIP_STATE"
            _log.warning(
                "review_team.posting_skipped_state",
                pr_number=pr_number,
                state=state_refused.state,
                posted_reviews=team.posted_reviews,
                posted_comments=team.posted_comments,
            )

        _log.info(
            "review_team.done",
            pr_number=pr_number,
            verdict=final.value,
            n_blockers=n_blockers,
            n_majors=n_majors,
            posted=team.posted_reviews,
        )
        return team

    def _completed_run(self, pr_number: int) -> ReviewRun | None:
        """Return the completed run at the pinned head to replay, or None.

        None when no head is pinned (the head is only known after the
        reviewers ran), when ``force`` was given, or when no run at that
        head completed. A ledger read failure is logged and reviews
        afresh.
        """
        if self._force or not self._pinned_head:
            return None
        try:
            return fetch_completed_review_run(self._db_path, pr_number, self._pinned_head)
        except Exception as exc:
            _log.warning(
                "review_team.cache_read_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence="reviewing afresh",
            )
            return None

    def _store_completed_run(
        self,
        *,
        run_id: str,
        outcomes: list[ReviewerOutcome],
        team: TeamOutcome,
        head_sha: str,
    ) -> None:
        """Store the run just completed so a later run at ``head_sha`` replays it.

        A write failure is logged; the next run at that head then
        reviews afresh.
        """
        try:
            record_review_run(
                self._db_path,
                completed_run(
                    run_id=run_id,
                    pr_number=team.pr_number,
                    head_sha=head_sha,
                    final_verdict=team.final_verdict,
                    outcomes=outcomes,
                    refutations=team.refutations,
                    spec={
                        "spec_id": team.spec_id,
                        "spec_source": team.spec_source,
                        "spec_covered": team.spec_covered,
                        "spec_missing": team.spec_missing,
                    },
                ),
            )
        except Exception as exc:
            _log.warning(
                "review_team.run_store_failed",
                pr_number=team.pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence="the next review at this head runs the reviewers again",
            )

    def _replay(self, pr_number: int, run: ReviewRun) -> TeamOutcome:
        """Return the outcome of ``run`` at its head without calling any reviewer.

        The stored reviewer outcomes, refutations and spec fields are
        read back, except the spec coverage: it is derived data, so it is
        recomputed at the head with the current rule and its row rewritten
        (:meth:`_replayed_spec_coverage`, no model call); the findings of the head are already in the ledger, and
        the ``findings:`` counts are recounted against it without a write
        (:func:`~repoach.review.findings_bridge.recount_findings_at_head`).
        The re-check of older heads still runs
        (SP-GATE-REVERIFIES-AT-HEAD G7; no model call when nothing older
        is unsettled) and the verdict is derived from the ledger as a
        fresh run derives it. A posting run posts the stored outcomes
        through the forge's barrier; a dry run's barrier refuses them.
        """
        outcomes = replayed_outcomes(run)
        spec = replayed_spec(run)
        head_sha = run.head_sha
        recounted = recount_findings_at_head(
            self._db_path,
            pr_number=pr_number,
            head_sha=head_sha,
            outcomes=outcomes,
            run_id=run.run_id,
        )
        reverified = self._reverify_older_heads(pr_number, head_sha=head_sha, refuter_calls=0)
        spec_id = _optional_str(spec.get("spec_id"))
        spec_missing = bool(spec.get("spec_missing", False))
        spec_covered = spec.get("spec_covered")
        spec_coverage_line = ""
        spec_source = _optional_str(spec.get("spec_source"))
        grading = None
        if not spec_missing:
            grading = self._replayed_spec_coverage(
                pr_number,
                spec_id=None if spec_source == RESOLUTION_OWNERSHIP else spec_id,
                head_sha=head_sha,
            )
            if grading is not None and grading.rows:
                spec_covered = all(row.covered for row in grading.rows)
                spec_coverage_line = (
                    f"review: spec coverage recomputed at {head_sha[:7]}: "
                    f"planned {sum(len(row.planned) for row in grading.rows)}, "
                    f"missing {sum(len(row.missing) for row in grading.rows)}"
                )
                if spec_id is None:
                    spec_source = grading.source
        facts = summarise_ledger_facts(self._db_path, pr_number=pr_number, head_sha=head_sha)
        verifications = recorded_verifications(
            self._db_path, pr_number=pr_number, head_sha=head_sha
        ).settled
        refutations = replayed_refutations(run)
        team = TeamOutcome(
            pr_number=pr_number,
            final_verdict=verdict_from_facts(facts),
            n_blockers=sum(1 for o in outcomes for c in o.comments if c.severity == "blocker"),
            n_majors=sum(1 for o in outcomes for c in o.comments if c.severity == "major"),
            head_sha=head_sha,
            reviews=outcomes,
            spec_id=spec_id,
            spec_source=spec_source,
            spec_covered=None if spec_covered is None else bool(spec_covered),
            spec_missing=spec_missing,
            spec_report=_spec_report(grading),
            spec_resolution=_spec_resolution(grading, spec_id),
            refutations=refutations,
            verifications=list(verifications),
            reverifications=list(reverified.outcomes),
            findings=self._verdict_findings(
                pr_number,
                facts=facts,
                verifications=verifications,
                refutations=refutations,
                reverifications=reverified.outcomes,
            ),
            verdict_basis=list(facts.open_blocking_ids),
            reverified=reverified.counts(),
            reverify_reasons=reverified.reasons(),
            reverify_line=reverified.summary_line("review"),
            findings_recorded=recounted.counts(),
            findings_line=recounted.summary_line(),
            cached=True,
            cache_line=cache_line(run),
            spec_coverage_line=spec_coverage_line,
        )
        generated_checks, generated_timing = fetch_generated_checks(
            self._db_path, pr_number, head_sha
        )
        team.generated = report_json(generated_checks)
        team.generated_lines = report_lines(generated_checks, generated_timing)
        if self._post:
            self._publish_replayed(pr_number, team)
        _log.info(
            "review_team.replayed",
            pr_number=pr_number,
            head_sha=head_sha[:12],
            run_id=run.run_id,
            verdict=team.final_verdict.value,
        )
        return team

    def _verdict_findings(
        self,
        pr_number: int,
        *,
        facts: MergeFacts,
        verifications: Iterable[MechanicalVerification],
        refutations: Iterable[Refutation],
        reverifications: Iterable[Reverification],
    ) -> list[ReportFinding]:
        """List every finding that can move the verdict, each with its judge.

        A ledger read failure is logged and yields an empty list; the
        verdict itself is unaffected (it was already derived).
        """
        try:
            return report_findings(
                self._db_path,
                pr_number=pr_number,
                facts=facts,
                verifications=verifications,
                refutations=refutations,
                reverifications=reverifications,
            )
        except Exception as exc:
            _log.warning(
                "review_team.report_findings_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence="the report lists no finding",
            )
            return []

    def _publish_replayed(self, pr_number: int, team: TeamOutcome) -> None:
        """Post a replayed run's stored outcomes, as a fresh posting run posts its own."""
        from .thread_context import fetch_resolved_disagreements

        try:
            resolved_per_role = {
                o.role: tuple(fetch_resolved_disagreements(self._forge, pr_number, role=o.role))
                for o in team.reviews
            }
        except Exception as exc:
            _log.warning(
                "review_team.resolved_disagreements_fetch_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )
            resolved_per_role = {o.role: () for o in team.reviews}
        try:
            self._publish_all(
                pr_number=pr_number,
                outcomes=team.reviews,
                head_sha=team.head_sha,
                team=team,
                resolved_per_role=resolved_per_role,
            )
        except ForgeStateRefusedError as exc:
            team.posting_outcome = "SKIP_STATE"
            _log.warning(
                "review_team.posting_skipped_state",
                pr_number=pr_number,
                state=exc.state,
                posted_reviews=team.posted_reviews,
                posted_comments=team.posted_comments,
            )

    def _draft_scope(
        self, spec_id: str, *, base_ref: str | None, changed_files: list[str]
    ) -> tuple[DraftScope | None, str]:
        """Return the draft scope of *spec_id* at head (:func:`~repoach.review.spec_gate.draft_scope_at_head`)."""
        return draft_scope_at_head(
            self._repo_root, spec_id, base_ref=base_ref, changed_files=changed_files
        )

    def _generated_scope(self, diff: str, base_ref: str | None) -> ReviewScope:
        """Summarise the declared generated artefacts the diff touches (G1, G2).

        The declarations are read at the base ref, never the head. A base
        that cannot be read or declares nothing leaves the diff as it is,
        so an oversized artefact keeps its blocking finding (fail closed).
        """
        try:
            base = readable_base(self._repo_root, base_ref, self._fetch_base)
            scope = review_scope(self._repo_root, diff, base)
        except Exception as exc:
            _log.warning(
                "review_team.generated_scope_failed",
                exc=type(exc).__name__,
                message=redact_error(str(exc))[:200],
                consequence="the diff is reviewed as it is; no artefact is summarised",
            )
            return ReviewScope(review_diff=diff)
        if scope.touched:
            _log.info(
                "review_team.generated_summarised",
                base=base,
                touched=list(scope.touched),
            )
        return scope

    def _fetch_base(self, base_ref: str) -> None:
        """Fetch ``origin/<base_ref>`` so the base contract can be read from it."""
        self._forge.git(
            [
                "fetch",
                "--quiet",
                "origin",
                f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
            ]
        )

    def _record_spec_coverage(
        self,
        pr_number: int,
        *,
        spec_id: str | None,
        head_sha: str | None,
        base_ref: str | None,
        changed_files: list[str],
    ) -> CoverageGrading | None:
        """Grade the PR's specs at head and record their rows (a refusing row on failure).

        The declared spec is graded alone; with none declared, every spec
        owning a changed path at the base is
        (:func:`~repoach.review.spec_grading.coverage_rows_at_head`). No model
        call: the report is a pure function of the specs, the head tree
        and the diff's paths, so a replayed run recomputes it with the
        current rule exactly as a fresh run computes it. A failure is
        logged (redacted) and records the not-covered row of
        :func:`~repoach.review.spec_grading.failed_grading_row`, so the gate
        refuses (fail closed); ``None`` only when that row cannot be recorded.
        """
        try:
            grading = coverage_rows_at_head(
                self._repo_root,
                spec_id=spec_id,
                base_ref=base_ref,
                changed_files=changed_files,
                fetch_base=self._fetch_base,
            )
            record_spec_coverages(
                self._db_path, pr_number=pr_number, head_sha=head_sha, coverages=grading.rows
            )
        except Exception as exc:
            _log.warning(
                "review_team.spec_coverage_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=redact_error(str(exc))[:200],
                consequence="a not-covered row is recorded (fail closed)",
            )
            return self._record_grading_failure(
                pr_number, spec_id=spec_id, head_sha=head_sha, error=exc
            )
        for coverage in grading.rows:
            _log.info(
                "review_team.spec_coverage",
                pr_number=pr_number,
                spec_id=coverage.spec_id,
                resolution=coverage.resolution,
                contract=coverage.contract,
                covered=coverage.covered,
                n_promised=coverage.n_promised,
                n_present=coverage.n_present,
                n_planned=len(coverage.planned),
                draft_refusals=coverage.draft_refusals,
            )
        return grading

    def _record_grading_failure(
        self, pr_number: int, *, spec_id: str | None, head_sha: str | None, error: Exception
    ) -> CoverageGrading | None:
        """Record the row of a failed grading (:func:`~repoach.review.spec_grading.failed_grading_row`).

        ``None`` only when even that row cannot be recorded.
        """
        row = failed_grading_row(spec_id, error)
        try:
            record_spec_coverages(
                self._db_path, pr_number=pr_number, head_sha=head_sha, coverages=[row]
            )
        except Exception as exc:
            _log.error(
                "review_team.spec_coverage_failure_unrecorded",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=redact_error(str(exc))[:200],
            )
            return None
        return CoverageGrading(rows=[row])

    def _replayed_spec_coverage(
        self, pr_number: int, *, spec_id: str | None, head_sha: str
    ) -> CoverageGrading | None:
        """Recompute the coverage of a replayed run at its head with the current rule.

        The PR's base ref and diff paths are read from the forge (no
        model call). ``None`` when they cannot be read or the grading
        fails; the stored row then stands, and the gate recomputes it
        when its rule is retired (SP-PLAN-SELECTOR-AUDIT-WIRE 0.3).
        """
        try:
            base_ref = self._forge.pull_request(pr_number).base_ref or None
            changed = diff_files(self._forge.diff(pr_number).text)
        except ForgeError as exc:
            _log.warning(
                "review_team.replay_spec_coverage_unread",
                pr_number=pr_number,
                stderr=exc.message[:200],
                consequence="the stored coverage row stands",
            )
            return None
        return self._record_spec_coverage(
            pr_number,
            spec_id=spec_id,
            head_sha=head_sha,
            base_ref=base_ref,
            changed_files=changed,
        )

    def _reverify_older_heads(
        self, pr_number: int, *, head_sha: str | None, refuter_calls: int
    ) -> HeadReverification:
        """Re-check the findings of older heads at ``head_sha`` before the verdict.

        SP-GATE-REVERIFIES-AT-HEAD G7-G10: the same pass as the gate
        (:func:`~repoach.review.head_reverify.reverify_older_findings`),
        so a person's fix pushed at ``head_sha`` settles the findings of
        the head before it and the team verdict counts only what is still
        open there. It runs only on the pinned head tree; CI is not read
        (``broken_behavior`` stays with the gate) and the judge calls come
        out of the refuter cap the review's own refuter pass spent
        ``refuter_calls`` of. A failure is logged and reported as a
        skipped re-check, and the findings of older heads keep blocking.
        """
        if not head_sha:
            return HeadReverification(head_sha="")
        if not self._pinned_head:
            return skipped_reverification(
                self._db_path,
                pr_number=pr_number,
                head_sha=head_sha,
                reason=NO_HEAD_TREE,
                reads_ci=False,
            )
        cap = configured_max_judged(self._config_root)
        budget = resolve_refuter_budget(self._config_root)
        try:
            with reviewer_scope("Refuter"):
                return reverify_older_findings(
                    self._db_path,
                    pr_number=pr_number,
                    repo_root=self._repo_root,
                    head_sha=head_sha,
                    ci_green=None,
                    config=load_gates_config(self._config_root),
                    max_judged=max(cap - refuter_calls, 0),
                    cap=cap,
                    budget=budget,
                )
        except Exception as exc:
            _log.warning(
                "review_team.head_reverify_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
                consequence="findings of older heads keep blocking",
            )
            return HeadReverification(
                head_sha=head_sha, skipped=f"re-check failed: {type(exc).__name__}"
            )

    def _publish_all(
        self,
        *,
        pr_number: int,
        outcomes: list[ReviewerOutcome],
        head_sha: str | None,
        team: TeamOutcome,
        resolved_per_role: dict[BotRole, tuple[ResolvedDisagreement, ...]],
    ) -> None:
        """Publish every reviewer, the auto-challenges and the archive, then notify.

        A :class:`~repoach.forge.ForgeStateRefusedError` from any write
        propagates out of every nested fallback, so a PR / MR that is no
        longer open stops the whole sequence at its first write — one
        state read, no COMMENT retry, no per-comment or verdict fallback.
        """
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

    def _join_head_guard(
        self,
        pool: ThreadPoolExecutor | None,
        future: Future[str | None] | None,
        *,
        pr_number: int,
    ) -> str | None:
        """Join the background head-freshness poll started in review_pr.

        Returns the resolved head SHA, or ``None`` when no guard was
        started (``post_to_github=False``) or the background poll
        raised unexpectedly. The review always proceeds either way,
        matching resolve_fresh_head's own evidence-first contract of
        never letting this check block or crash the pipeline.
        """
        if pool is None or future is None:
            return None
        try:
            result = future.result()
        except Exception as exc:
            _log.warning(
                "review_team.head_guard_failed",
                pr_number=pr_number,
                exc=type(exc).__name__,
                message=str(exc)[:200],
            )
            result = None
        finally:
            pool.shutdown(wait=False)
        return result

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
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures: dict[int, Future[ReviewerOutcome]] = {}
            for i in triggers:
                reviewer = reviewers[i]
                ctx = self._build_dialogue_context(
                    reviewer_role=reviewer.role,
                    reviewer_outcome=round1_outcomes[i],
                    round1_outcomes=round1_outcomes,
                    guard_events=guard_events,
                )
                futures[i] = pool.submit(
                    bind_reviewer(reviewer.role.value.title(), reviewer.review_diff),
                    diff,
                    pr_number=pr_number,
                    spec_plan=spec_plan,
                    dialogue_context=ctx,
                    extra_prompt_section=extra_prompt_section,
                    arch_edges=arch_edges,
                )
            for i, fut in futures.items():
                reviewer = reviewers[i]
                outcome = round1_outcomes[i]
                try:
                    new_outcome = fut.result()
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

    def _build_dialogue_context(
        self,
        *,
        reviewer_role: BotRole,
        reviewer_outcome: ReviewerOutcome,
        round1_outcomes: list[ReviewerOutcome],
        guard_events: list[GuardEvent],
    ) -> DialogueContext:
        """Assemble the round-2 context for one reviewer.

        Excludes the reviewer's own round-1 outcome from peer_outcomes;
        truncates each peer's prose to 200 chars; filters guard events
        to those targeting this reviewer; reads file excerpts from the
        reviewed tree for every cited ``file:line``.
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
            repo_root=self._repo_root,
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

        SP-REVIEW-POST-BATCH: when ``head_sha`` is known, every inline
        comment and the verdict are submitted together through
        :meth:`_publish_batched_review` — one ``gh`` subprocess for the
        whole reviewer's output instead of one per comment. When
        ``head_sha`` is ``None`` (fresh-head resolution failed) there is
        no commit to anchor inline comments to, so batching is skipped
        entirely and :meth:`_publish_per_comment` runs directly — the
        pre-batching degrade behavior for an unresolved head, preserved
        verbatim.

        Failures here are logged but never raised — we want partial
        success rather than aborting the whole team.

        When ``posted_ids`` is not None, the GitHub comment id of every
        successfully posted inline comment is recorded so the
        orchestrator's auto-challenge pass can target replies on the
        right thread.
        """
        if head_sha is None:
            self._publish_per_comment(
                pr_number=pr_number,
                outcome=outcome,
                head_sha=head_sha,
                team=team,
                posted_ids=posted_ids,
            )
            return
        self._publish_batched_review(
            pr_number=pr_number,
            outcome=outcome,
            head_sha=head_sha,
            team=team,
            posted_ids=posted_ids,
        )

    def _publish_batched_review(
        self,
        *,
        pr_number: int,
        outcome: ReviewerOutcome,
        head_sha: str,
        team: TeamOutcome,
        posted_ids: dict[tuple[str, int], int] | None,
    ) -> None:
        """Submit one reviewer's verdict + inline comments as a single review.

        ``GITHUB_TOKEN``-authenticated bots cannot submit APPROVE /
        REQUEST_CHANGES reviews on a PR (only humans / PATs can —
        observed live on PR #1 from the auto-review workflow). When the
        batched submission is rejected while ``outcome.verdict`` is
        APPROVE or REQUEST_CHANGES, it is retried exactly once with the
        event downgraded to COMMENT (SP-REVIEW-POST-BATCH NG4 — at most
        one retry). When that retry (or the original call, when the
        verdict was already COMMENT) also fails — e.g. a comment's line
        no longer resolves against ``head_sha`` after a force-push,
        HTTP 422 — this falls back to
        :meth:`_publish_per_comment`'s pre-batching sequence, so a
        single bad anchor degrades in isolation rather than losing the
        whole reviewer's output.
        """
        drafts = [
            InlineCommentDraft(
                path=comment.file,
                line=comment.line,
                body=_render_comment_body(outcome, comment),
            )
            for comment in outcome.comments
        ]
        review_body = _render_review_body(outcome)
        verdict = outcome.verdict.value
        posted: Review | None = None
        last_error = ""
        try:
            posted = self._forge.post_review(
                pr_number,
                ReviewDraft(
                    verdict=verdict, body=review_body, comments=drafts, commit_sha=head_sha
                ),
            )
        except ForgeStateRefusedError:
            raise
        except ForgeError as exc:
            if verdict == ReviewVerdict.COMMENT.value:
                last_error = exc.message
            else:
                _log.warning(
                    "review_team.batch_review_rejected",
                    role=outcome.role.value,
                    verdict=verdict,
                    stderr=exc.message[:200],
                )
                try:
                    posted = self._forge.post_review(
                        pr_number,
                        ReviewDraft(
                            verdict=ReviewVerdict.COMMENT.value,
                            body=review_body,
                            comments=drafts,
                            commit_sha=head_sha,
                        ),
                    )
                except ForgeStateRefusedError:
                    raise
                except ForgeError as retry_exc:
                    last_error = retry_exc.message
        if posted is None:
            _log.warning(
                "review_team.batch_review_fully_rejected",
                role=outcome.role.value,
                stderr=last_error[:200],
            )
            self._publish_per_comment(
                pr_number=pr_number,
                outcome=outcome,
                head_sha=head_sha,
                team=team,
                posted_ids=posted_ids,
            )
            return

        team.posted_reviews += 1
        team.posted_comments += len(outcome.comments)
        if posted_ids is not None and drafts:
            for comment in posted.comments:
                if comment.path is not None and comment.line is not None and comment.id:
                    posted_ids[(comment.path, comment.line)] = comment.id

    def _publish_per_comment(
        self,
        *,
        pr_number: int,
        outcome: ReviewerOutcome,
        head_sha: str | None,
        team: TeamOutcome,
        posted_ids: dict[tuple[str, int], int] | None,
    ) -> None:
        """Post one gh subprocess call per inline comment, then the verdict.

        The pre-SP-REVIEW-POST-BATCH posting sequence, kept as the
        degrade path for two cases: ``head_sha`` unresolved (no commit
        to anchor a batched review to) and a batched review rejected
        for every event tried by :meth:`_publish_batched_review`. A bad
        comment anchor fails in isolation here — siblings still post —
        which is exactly why this sequence is preserved rather than
        replaced outright.

        Note:
            ``GITHUB_TOKEN``-authenticated bots cannot submit APPROVE
            / REQUEST_CHANGES reviews on a PR (only humans / PATs
            can — observed live on PR #1 from the auto-review
            workflow).  When the verdict-submit call is rejected we
            fall back to a regular issue comment so each bot's
            verdict still surfaces on the PR conversation.
        """
        for comment in outcome.comments:
            body = _render_comment_body(outcome, comment)
            try:
                created = self._forge.post_comment(
                    pr_number,
                    body,
                    path=comment.file,
                    line=comment.line,
                    commit_sha=head_sha,
                )
            except ForgeStateRefusedError:
                raise
            except ForgeError as exc:
                _log.warning(
                    "review_team.comment_post_failed",
                    role=outcome.role.value,
                    file=comment.file,
                    line=comment.line,
                    stderr=exc.message[:200],
                )
                continue
            team.posted_comments += 1
            if posted_ids is not None and created.id:
                posted_ids[(comment.file, comment.line)] = created.id

        review_body = _render_review_body(outcome)
        try:
            self._forge.post_review(
                pr_number, ReviewDraft(verdict=outcome.verdict.value, body=review_body)
            )
        except ForgeStateRefusedError:
            raise
        except ForgeError as exc:
            _log.warning(
                "review_team.review_submit_failed",
                role=outcome.role.value,
                stderr=exc.message[:200],
            )
        else:
            team.posted_reviews += 1
            return
        fallback_body = (
            f"_(verdict submission rejected — falling back to a comment)_\n\n{review_body}"
        )
        try:
            self._forge.post_comment(pr_number, fallback_body)
        except ForgeStateRefusedError:
            raise
        except ForgeError as exc:
            _log.warning(
                "review_team.review_fallback_failed",
                role=outcome.role.value,
                stderr=exc.message[:200],
            )
        else:
            team.posted_comments += 1

    def _fire_proposed_escalation_dossier(self, pr_number: int) -> None:
        """Fire a proposed-escalation dossier for findings frozen in proposed.

        After the verify+judge passes, fetches the PR's findings and
        computes :func:`stuck.assess_proposed_escalation` and
        :func:`stuck.select_newly_escalated`. When non-empty, builds the
        dossier and fires it through the same settings-guarded routine
        seam as :meth:`_fire_routine` — absent routine secrets means
        silent no-op, never an error.

        Only called by :meth:`review_pr` when ``publish`` is true, so a
        dry run (``post_to_github=False``) never reaches this method.

        Args:
            pr_number: The PR whose proposed findings to assess.
        """
        from .findings import fetch_findings
        from .stuck import (
            assess_proposed_escalation,
            build_proposed_escalation_dossier,
            select_newly_escalated,
        )

        findings = fetch_findings(self._db_path, pr_number)
        eligible = assess_proposed_escalation(findings)
        if not eligible:
            return
        newly = select_newly_escalated(eligible)
        if not newly:
            return

        settings = get_settings()
        routine_id = settings.claude_code_routine_id
        token_secret = settings.claude_code_routine_token
        if not routine_id or token_secret is None:
            return
        token = token_secret.get_secret_value()
        if not token:
            return

        dossier = build_proposed_escalation_dossier(newly)
        self._forge.barrier.refuse_if_closed("routine fire proposed_escalation")
        result = fire_review_routine(
            routine_id=routine_id,
            token=token,
            payload=dossier,
        )
        if result.ok:
            _log.info(
                "review_team.proposed_escalation_fired",
                pr_number=pr_number,
                routine_id=routine_id,
                n_findings=len(newly),
            )
        else:
            _log.warning(
                "review_team.proposed_escalation_fire_failed",
                pr_number=pr_number,
                routine_id=routine_id,
                status_code=result.status_code,
                error=(result.error or "")[:200],
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
        self._forge.barrier.refuse_if_closed("routine fire review_outcome")
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
                try:
                    self._forge.reply(pr_number, match.new_comment_id, body)
                except ForgeStateRefusedError:
                    raise
                except ForgeError as exc:
                    _log.warning(
                        "review.bot.auto_challenge_reply_failed",
                        pr_number=pr_number,
                        role=outcome.role.value,
                        new_comment_id=match.new_comment_id,
                        stderr=exc.message[:200],
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

        The comment is identified by :data:`~repoach.forge.ARCHIVE_MARKER`
        so repeat runs replace it instead of stacking.  This gives us a
        machine-readable archive that survives the workflow runner —
        retrievable via ``repoach review report`` from any machine with
        PR-read access.
        """
        payload = team_outcome_to_dict(team)
        body_json = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        archive_appendix = (
            f"### Repoach review archive\n\n"
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
        try:
            self._forge.upsert_marked_comment(team.pr_number, ARCHIVE_MARKER, body)
        except ForgeStateRefusedError:
            raise
        except ForgeError as exc:
            _log.warning(
                "review_team.archive_upsert_failed",
                pr_number=team.pr_number,
                stderr=exc.message[:200],
            )

    def _render_report_body(self, team: TeamOutcome, *, archive_appendix: str) -> str:
        """Render the sticky archive as a ledger report (SP-VERDICT-FLIP 10a).

        The headline + facts table are a snapshot of the recorded ledger
        state at this head; the machine-readable ``TeamOutcome`` JSON, guard
        section, and transcript ride below in ``archive_appendix``. The
        authoritative merge decision is the pure gate re-verifying at a
        checked-out head (``repoach review gate`` / auto-merge), never
        this report — the review job runs from the base ref, so on-disk
        re-verification here would be wrong (hence
        :func:`summarise_ledger_facts`, not :func:`gather_merge_facts`).
        The findings of older heads the review re-checked at its head and
        found still blocking are named in the headline's reasons, as the
        gate names them (SP-GATE-REVERIFIES-AT-HEAD G8).
        """
        facts = summarise_ledger_facts(
            self._db_path,
            pr_number=team.pr_number,
            head_sha=team.head_sha or "",
        ).model_copy(
            update={
                "reverified": dict(team.reverified),
                "reverify_reasons": list(team.reverify_reasons),
            }
        )
        decision = compute_merge_decision(facts)
        return render_ledger_report(
            self._db_path,
            pr_number=team.pr_number,
            decision=decision,
            facts=facts,
            archive_appendix=archive_appendix,
            judged=team.findings,
            generated_lines=team.generated_lines,
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


def _spec_report(grading: CoverageGrading | None) -> list[str]:
    """Return the coverage lines a review reports on stderr (resolution first)."""
    if grading is None:
        return []
    return [*grading.lines, *(line for row in grading.rows for line in row.report)]


def _spec_resolution(grading: CoverageGrading | None, spec_id: str | None) -> dict[str, object]:
    """Return the ``spec_resolution`` object of the review JSON (G7)."""
    if grading is None:
        return resolution_payload(RESOLUTION_DECLARED if spec_id else RESOLUTION_NONE)
    return grading.resolution_payload()


def _optional_str(value: object) -> str | None:
    """Return ``value`` as a string, None when it is None."""
    return None if value is None else str(value)


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
        "reverified": dict(team.reverified),
        "reverify_reasons": list(team.reverify_reasons),
        "findings_recorded": dict(team.findings_recorded),
        "cached": team.cached,
        "findings": [entry.as_json() for entry in team.findings],
        "verdict_basis": list(team.verdict_basis),
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


def _local_git(repo_root: Path, *args: str) -> str | None:
    """Return stdout of a git command in *repo_root*, or ``None`` on error."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("orchestrator.local_git_failed", args=args, error=str(exc)[:120])
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def resolve_fresh_head(
    forge: Forge,
    pr_number: int,
    *,
    repo_root: Path,
    attempts: int = 6,
    delay_s: float = 5.0,
    sleep: object = time.sleep,
) -> str | None:
    """Resolve the PR head, guarding against post-push propagation lag.

    A review launched right after ``git push`` can be served the
    pre-push head — recorded live on PR #3, where a full round of
    integrity and findings landed at the stale SHA and the run was
    wasted. When the local checkout sits on the PR's head branch, the
    locally committed HEAD is ground truth: re-poll ``gh`` (bounded)
    until the API catches up. If it never does, log loudly and return
    what the forge serves — the review must still record what it actually
    reviewed (evidence-first), but the operator sees the divergence.
    """
    served = _served_head(forge, pr_number)
    local_branch = _local_git(repo_root, "symbolic-ref", "--short", "HEAD")
    if not local_branch:
        return served
    try:
        head_branch: str | None = forge.pull_request(pr_number).head_ref
    except Exception as exc:
        _log.debug(
            "orchestrator.head_guard_view_failed",
            pr_number=pr_number,
            error=str(exc)[:120],
        )
        return served
    if head_branch != local_branch:
        return served
    local_head = _local_git(repo_root, "rev-parse", "HEAD")
    if not local_head:
        return served
    for attempt in range(attempts):
        if served == local_head:
            return served
        _log.warning(
            "orchestrator.head_propagation_lag",
            pr_number=pr_number,
            attempt=attempt + 1,
            served=(served or "")[:12],
            local=local_head[:12],
        )
        sleep(delay_s)
        served = _served_head(forge, pr_number)
    if served != local_head:
        _log.error(
            "orchestrator.reviewing_stale_head",
            pr_number=pr_number,
            served=(served or "")[:12],
            local=local_head[:12],
        )
    return served


def _served_head(forge: Forge, pr_number: int) -> str | None:
    """The head SHA the forge serves, ``None`` when it cannot be read."""
    try:
        return forge.head_sha(pr_number)
    except ForgeError as exc:
        _log.debug(
            "orchestrator.head_sha_unavailable", pr_number=pr_number, error=exc.message[:120]
        )
        return None


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


def run_review(
    pr_number: int,
    *,
    post: bool = True,
    forge: Forge | None = None,
    spec: str | None = None,
    keep_tree: bool = False,
    announce: Callable[[str], None] | None = None,
    write_refusal: WriteRefusal | None = None,
    force: bool = False,
    generated_evidence: Path | None = None,
) -> TeamOutcome:
    """Review the PR head tree and return the aggregated outcome (CLI entry).

    SP-REVIEW-PINS-HEAD G2: the head SHA is resolved from the forge first
    (with :func:`resolve_fresh_head`'s propagation-lag poll), the tree at
    that SHA is materialised (:func:`~repoach.review.review_head.materialise_head`:
    the checkout in place when it is at the SHA and clean, else a
    detached worktree under the state directory) and every reader of the
    review resolves against it. The worktree is removed afterwards
    unless ``keep_tree``.

    Args:
        pr_number: PR number (GitHub) or MR iid (GitLab) to review.
        post: When False, do not publish anything to the forge (dry-run).
        forge: The forge to read and post through; selected from the
            repository when omitted.
        spec: The ``--spec`` value, highest-precedence spec source.
        keep_tree: Leave the worktree in place and report its path.
        announce: Receives the ``reviewing <N> at <sha7> (<tree> <path>)``
            line before the reviewers start.
        write_refusal: Why nothing may be written when ``post`` is False
            (``dry run`` from ``--dry-run``, the advisory variable in CI);
            the forge is read-only from its selection on.
        force: Re-review a head that already has a completed run instead
            of replaying it (SP-FINDINGS-BRIDGE-DOCFIX 0.3).
        generated_evidence: The ``review-generated.json`` of the
            secret-free verification job, bound to this head
            (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

    Returns:
        The :class:`TeamOutcome` aggregated across the four reviewers,
        with ``head_sha`` and ``tree`` filled.

    Raises:
        GatewayConfigError: The selected provider cannot be used (the
            gateway URL or secret, the ``claude`` CLI or its login, the
            Anthropic key); raised before any forge call.
        HeadUnresolvedError: The forge served no usable head SHA.
        HeadUnavailableError: The head tree could not be materialised.
    """
    require_provider_ready()
    checkout = Path.cwd()
    refusal = None if post else (write_refusal or WriteRefusal.POSTING_DISABLED)
    forge = forge or select_forge(cwd=checkout)
    if refusal is not None:
        forge.barrier.close(refusal)
    sha = require_head_sha(resolve_fresh_head(forge, pr_number, repo_root=checkout), pr_number)
    db_path = Path(get_settings().db_path)
    with materialise_head(
        forge, pr_number, sha, repo_root=checkout, state_dir=db_path.parent, keep=keep_tree
    ) as tree:
        if announce is not None:
            announce(tree.announcement(pr_number))
        team = ReviewTeamOrchestrator(
            forge=forge,
            db_path=db_path,
            post_to_github=post,
            repo_root=tree.root,
            head_sha=sha,
            config_root=checkout,
            write_refusal=refusal,
            force=force,
            generated_evidence=generated_evidence,
        ).review_pr(pr_number, spec_flag=spec)
    team.tree = tree.tree
    team.worktree = str(tree.root) if tree.tree == "worktree" and tree.kept else None
    return team
