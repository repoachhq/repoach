"""What the bot merge waits on, and the record of every attempt.

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF. On 2026-10-26 four green,
approved PRs were not merged: the merge step read GitHub's rollup
``mergeStateStatus``, which counts every check on the head, the merge
job's own run and the review bot's queued jobs included, saw
``UNSTABLE`` and skipped. This module holds the one judgment that
replaces it and the bookkeeping around it:

* :func:`judge_required_checks` judges the committed list of required
  checks (:func:`required_checks`), name by name, at the head. A check
  not in the list can never make the merge wait. A ``CANCELLED`` entry
  is ignored when another entry of the same name exists at the head; a
  name with only cancelled entries is ``cancelled``, never red (G1, G3).
* :func:`required_checks`, :func:`rearm_workflows` and
  :func:`unrearmed_checks` read ``[auto_merge]`` and the workflow files:
  every required check must be produced by a workflow whose completion
  re-runs the merge, or a pending check would strand the PR (G2).
* :class:`MergeAttempt` is one attempt; :func:`upsert_attempt` keeps the
  last :data:`MERGE_ATTEMPTS_KEPT` of them in one PR comment marked
  :data:`MERGE_ATTEMPTS_MARKER` (``format:merge-attempts-comment``), so
  they outlive the job's database (G5).
* :func:`stranded` lists the open PRs that are green with no merge
  attempt since, and :func:`dispatch_stranded` re-arms them (G3, D3).

``repoach queue run`` (SP-MERGE-QUEUE-BY-SPEC-DEPENDENCIES) must judge CI
through :func:`judge_required_checks` too, never through
``mergeStateStatus``.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from ..core.logging import get_logger
from ..forge import CheckRun, Forge, ForgeError, ForgeWriteRefusedError, PullRequest
from ..forge.github import DEFAULT_REQUIRED_CHECK_NAMES
from ..project.config import load_project_config
from .persistence import fetch_merge_rows
from .spec_gate import SPEC_COVERAGE_RULE_VERSION

_log = get_logger(__name__)

CheckJudgement = Literal["green", "pending", "failed", "missing", "cancelled"]

MERGE_ATTEMPTS_MARKER = "<!-- repoach-merge-attempts:v1 -->"
"""Marker of the one PR comment holding the recent merge attempts (G5)."""

MERGE_ATTEMPTS_KEPT = 20
"""Attempts kept in the comment (``merge_attempts_kept_in_comment``)."""

STRANDED_GRACE_S = 300
"""Seconds a green PR may wait for its merge before it is stranded."""

COALESCE_WINDOW_S = 60
"""Seconds an owner-event burst is folded into (``coalesce_window_s``)."""

DEFAULT_REARM_WORKFLOWS: tuple[str, ...] = ("CI", "Auto Review (review bots)")
"""Workflows whose completion re-runs the merge (``rearm_workflows``)."""

CI_WORKFLOW_NAME = "CI"
"""The workflow whose job names are the default required checks (D4)."""

MERGE_WORKFLOW_FILE = "merge-on-ci.yml"
"""The workflow :func:`dispatch_stranded` starts for each stranded PR."""

MERGE_RULE_VERSION = f"merge-wait:1,spec-coverage:{SPEC_COVERAGE_RULE_VERSION}"
"""The gate rule in force, written on every attempt (``rule_version``)."""

MERGED_OUTCOMES: frozenset[str] = frozenset({"APPROVE", "ALREADY_MERGED"})
"""Attempt outcomes after which there is nothing left to merge."""

_FAIL_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_CANCELLED = "CANCELLED"
_MATRIX_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.S)


class MergeWaitConfigError(ValueError):
    """``[auto_merge]`` names a check that no re-arming workflow produces."""


class RequiredChecksVerdict(BaseModel):
    """The judgment of the required checks at one head.

    Attributes:
        state: ``green``, ``pending``, ``failed``, ``missing`` or
            ``cancelled``; precedence in that order from ``failed``:
            failed, pending, cancelled, missing, else green.
        pending: Required checks still queued or running.
        failed: Required checks whose decisive run failed, ``name=CONCLUSION``.
        missing: Required checks with no run at the head.
        cancelled: Required checks whose only runs at the head were cancelled.
        green_since: The latest completion among the required checks,
            when green and every one carries a completion time.
        rearmed_by: Pending check name -> the workflow whose completion
            re-runs the merge.
    """

    state: CheckJudgement
    pending: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    cancelled: list[str] = Field(default_factory=list)
    green_since: datetime | None = None
    rearmed_by: dict[str, str] = Field(default_factory=dict)

    def note(self) -> str:
        """The attempt note naming what decided this verdict."""
        if self.state == "failed":
            return f"required_check_failed: {', '.join(self.failed)}"
        if self.state == "pending":
            workflows = list(dict.fromkeys(self.rearmed_by.get(n, "unknown") for n in self.pending))
            return (
                f"pending: {', '.join(self.pending)}; re-armed by workflow {', '.join(workflows)}"
            )
        if self.state == "cancelled":
            return f"cancelled with no other run: {', '.join(self.cancelled)}"
        if self.state == "missing":
            return f"required_check_missing: {', '.join(self.missing)}"
        return "all required checks succeeded"


def parse_time(value: str) -> datetime | None:
    """Parse an ISO-8601 forge timestamp (``Z`` accepted); ``None`` when empty or bad."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _log.warning("merge_wait.timestamp_unreadable", value=value[:40], error=str(exc))
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def format_time(value: datetime) -> str:
    """Render a timestamp as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decisive(runs: Sequence[CheckRun]) -> CheckRun:
    """The run with the latest ``started_at``; the later listed one wins a tie."""
    chosen = runs[0]
    for run in runs[1:]:
        if run.started_at >= chosen.started_at:
            chosen = run
    return chosen


def judge_required_checks(
    check_runs: Sequence[CheckRun],
    required: Sequence[str],
    head_sha: str,
    *,
    producers: Mapping[str, str] | None = None,
) -> RequiredChecksVerdict:
    """Judge each required check at ``head_sha``; nothing else is consulted.

    Per name, the latest run at the head that is not ``CANCELLED``
    decides: a success conclusion is green, a failure conclusion is
    ``failed``, no conclusion yet is ``pending``. A run whose
    ``head_sha`` names another commit is ignored; one with no
    ``head_sha`` is taken as at the head (the commit's own check-runs).
    Checks not in ``required`` are never read, so the merge job's own run
    or a review-bot job cannot make the merge wait.

    Args:
        check_runs: Every check run reported for the head.
        required: The committed list of required check names.
        head_sha: The head the merge decides at.
        producers: Check name -> producing workflow, for the re-arm note
            when the run does not carry its workflow.

    Returns:
        The verdict, in SP-CI-RED-IS-EVIDENCE's vocabulary.
    """
    at_head = [
        run for run in check_runs if not head_sha or not run.head_sha or run.head_sha == head_sha
    ]
    known = producers or {}
    pending: list[str] = []
    failed: list[str] = []
    missing: list[str] = []
    cancelled: list[str] = []
    rearmed: dict[str, str] = {}
    completions: list[datetime | None] = []
    for name in required:
        runs = [run for run in at_head if run.name == name]
        if not runs:
            missing.append(name)
            continue
        live = [run for run in runs if run.conclusion.upper() != _CANCELLED]
        if not live:
            cancelled.append(name)
            continue
        run = _decisive(live)
        conclusion = run.conclusion.upper()
        if conclusion in _SUCCESS_CONCLUSIONS:
            completions.append(parse_time(run.completed_at))
        elif not conclusion:
            pending.append(name)
            rearmed[name] = known.get(name) or run.workflow or "unknown"
        else:
            failed.append(f"{name}={conclusion}")
    state: CheckJudgement
    if failed:
        state = "failed"
    elif pending:
        state = "pending"
    elif cancelled:
        state = "cancelled"
    elif missing:
        state = "missing"
    else:
        state = "green"
    green_since = None
    if state == "green" and completions and all(t is not None for t in completions):
        green_since = max(t for t in completions if t is not None)
    return RequiredChecksVerdict(
        state=state,
        pending=pending,
        failed=failed,
        missing=missing,
        cancelled=cancelled,
        green_since=green_since,
        rearmed_by=rearmed,
    )


def _expand_job_names(job_id: str, job: Mapping[str, Any]) -> list[str]:
    """The check names one workflow job produces, its matrix expanded."""
    template = str(job.get("name") or job_id)
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, Mapping) else None
    axes: list[tuple[str, list[str]]] = []
    if isinstance(matrix, Mapping):
        for key, values in matrix.items():
            if key in {"include", "exclude"} or not isinstance(values, list):
                continue
            axes.append((str(key), [str(v) for v in values]))
    if not axes:
        return [template]
    names: list[str] = []
    for combination in itertools.product(*(values for _, values in axes)):
        bound = dict(zip((key for key, _ in axes), combination, strict=True))
        if _MATRIX_RE.search(template):
            names.append(_substitute_matrix(template, bound))
        else:
            names.append(f"{template} ({', '.join(combination)})")
    return names


def _substitute_matrix(template: str, bound: Mapping[str, str]) -> str:
    """Replace each ``${{ matrix.<key> }}`` of ``template`` by its bound value."""
    return _MATRIX_RE.sub(lambda m: bound.get(m.group(1), m.group(0)), template)


def workflow_checks(root: Path) -> dict[str, list[str]]:
    """Map each workflow name under ``.github/workflows`` to the check names it produces.

    A workflow without ``name`` is keyed by its file stem; an unreadable
    file is skipped with a warning.
    """
    found: dict[str, list[str]] = {}
    directory = root / ".github" / "workflows"
    if not directory.is_dir():
        return found
    for path in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            _log.warning("merge_wait.workflow_unreadable", path=str(path), error=str(exc)[:200])
            continue
        if not isinstance(data, Mapping):
            continue
        jobs = data.get("jobs")
        if not isinstance(jobs, Mapping):
            continue
        names: list[str] = []
        for job_id, job in jobs.items():
            if isinstance(job, Mapping):
                names.extend(_expand_job_names(str(job_id), job))
        found[str(data.get("name") or path.stem)] = names
    return found


def required_checks(root: Path) -> list[str]:
    """``[auto_merge].required_checks``, else the CI workflow's job names (matrix expanded).

    Falls back to :data:`~repoach.forge.github.DEFAULT_REQUIRED_CHECK_NAMES`
    when the repository has no ``CI`` workflow file.

    Raises:
        ConfigError: ``repoach.toml`` exists and is invalid.
    """
    configured = load_project_config(root).auto_merge.required_checks
    if configured is not None:
        return list(configured)
    derived = workflow_checks(root).get(CI_WORKFLOW_NAME)
    return list(derived) if derived else list(DEFAULT_REQUIRED_CHECK_NAMES)


def rearm_workflows(root: Path) -> list[str]:
    """``[auto_merge].rearm_workflows``: workflows whose completion re-runs the merge."""
    return list(load_project_config(root).auto_merge.rearm_workflows)


def check_producers(root: Path) -> dict[str, str]:
    """Map each check name to the first re-arming workflow that produces it."""
    produced = workflow_checks(root)
    producers: dict[str, str] = {}
    for workflow in rearm_workflows(root):
        for name in produced.get(workflow, []):
            producers.setdefault(name, workflow)
    return producers


def unrearmed_checks(root: Path, required: Sequence[str] | None = None) -> list[str]:
    """Name every required check no re-arming workflow produces.

    Returns:
        One ``<name>: not re-armed by any of [<workflows>]`` line per
        such check; empty when every check is re-armed.
    """
    names = list(required) if required is not None else required_checks(root)
    workflows = rearm_workflows(root)
    producers = check_producers(root)
    return [
        f"{name}: not re-armed by any of {workflows!r}" for name in names if name not in producers
    ]


def assert_rearmed(root: Path) -> list[str]:
    """Return :func:`required_checks` when every one is re-armed.

    Raises:
        MergeWaitConfigError: Naming each check no re-arming workflow produces.
    """
    names = required_checks(root)
    problems = unrearmed_checks(root, names)
    if problems:
        raise MergeWaitConfigError("; ".join(problems))
    return names


class MergeAttempt(BaseModel):
    """One merge attempt, as recorded in ``pr_merges`` and in the PR comment.

    Attributes:
        pr_number: The PR.
        head_sha: The head the attempt decided at.
        outcome: The ``OUTCOME_*`` tag.
        reason: The attempt's notes.
        pending_checks: Required checks still running at the decision.
        trigger: ``auto-review:<run_id>``, ``merge-on-ci:<run_id>``,
            ``dispatch:<run_id>`` or ``cli``.
        rule_version: The gate rule in force.
        recorded_at: When the attempt was decided.
    """

    pr_number: int
    head_sha: str
    outcome: str
    reason: str
    pending_checks: list[str] = Field(default_factory=list)
    trigger: str
    rule_version: str
    recorded_at: datetime


class MergeAttemptsComment(BaseModel):
    """``format:merge-attempts-comment``: the JSON block of the attempts comment."""

    pr: int
    attempts: list[MergeAttempt] = Field(max_length=MERGE_ATTEMPTS_KEPT)


class MergeAttemptsReport(BaseModel):
    """``format:merge-attempts-json``: what ``review merge-attempts`` prints."""

    pr: int
    attempts: list[MergeAttempt]
    count: int
    last: MergeAttempt | None


class StrandedPR(BaseModel):
    """A green PR the merge never reached.

    Attributes:
        pr_number: The PR.
        head_sha: Its head.
        green_since: The latest required completion at that head.
        last_attempt: The last attempt at that head, if any.
        reason: ``stranded: green since <t>, …`` naming the last attempt.
    """

    pr_number: int
    head_sha: str
    green_since: datetime
    last_attempt: MergeAttempt | None = None
    reason: str


def trigger_from_env(env: Mapping[str, str]) -> str:
    """Name what started this attempt from the GitHub Actions environment.

    Returns:
        ``dispatch:<run_id>`` for a ``workflow_dispatch`` run,
        ``merge-on-ci:<run_id>`` / ``auto-review:<run_id>`` by workflow
        file, ``<event>:<run_id>`` for any other workflow, ``cli`` outside
        Actions.
    """
    run_id = env.get("GITHUB_RUN_ID", "")
    if env.get("GITHUB_ACTIONS") != "true" or not run_id:
        return "cli"
    event = env.get("GITHUB_EVENT_NAME", "")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")
    if event == "workflow_dispatch":
        return f"dispatch:{run_id}"
    if "/merge-on-ci.yml" in workflow_ref:
        return f"merge-on-ci:{run_id}"
    if "/auto-review.yml" in workflow_ref:
        return f"auto-review:{run_id}"
    return f"{event or 'actions'}:{run_id}"


def render_attempts_comment(pr_number: int, attempts: Sequence[MergeAttempt]) -> str:
    """The attempts comment body: marker, one line, the last attempts as JSON."""
    kept = list(attempts)[-MERGE_ATTEMPTS_KEPT:]
    payload = MergeAttemptsComment(pr=pr_number, attempts=kept).model_dump(mode="json")
    last = kept[-1] if kept else None
    headline = (
        f"Merge attempts: {len(kept)} kept, last {last.outcome} at {format_time(last.recorded_at)}"
        if last is not None
        else "Merge attempts: none"
    )
    return (
        f"{MERGE_ATTEMPTS_MARKER}\n{headline}\n\n```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n"
    )


def parse_attempts_comment(body: str) -> list[MergeAttempt]:
    """Read the attempts back from a comment body.

    Raises:
        ValueError: The body carries no JSON block or it does not
            validate against ``format:merge-attempts-comment``.
    """
    match = _JSON_BLOCK_RE.search(body)
    if match is None:
        raise ValueError("merge-attempts comment carries no ```json block")
    return MergeAttemptsComment.model_validate_json(match.group(1)).attempts


def upsert_attempt(forge: Forge, attempt: MergeAttempt, *, replace_last: bool = False) -> bool:
    """Add ``attempt`` to the PR's attempts comment, through the write barrier.

    With ``replace_last``, an attempt with the same ``recorded_at`` and
    head at the end of the comment is replaced instead of appended (the
    pre-merge ``APPROVE`` superseded by the forge's refusal). A read-only
    forge writes nothing. Any failure is logged as
    ``merge_attempts.comment_failed`` and never changes the outcome.

    Returns:
        True when the comment was written.
    """
    if forge.read_only:
        _log.info("merge_attempts.comment_skipped", pr_number=attempt.pr_number, why="read_only")
        return False
    try:
        existing = forge.find_marked_comment(attempt.pr_number, MERGE_ATTEMPTS_MARKER)
        attempts = parse_attempts_comment(existing.body) if existing is not None else []
        if (
            replace_last
            and attempts
            and attempts[-1].recorded_at == attempt.recorded_at
            and attempts[-1].head_sha == attempt.head_sha
        ):
            attempts[-1] = attempt
        else:
            attempts.append(attempt)
        forge.upsert_marked_comment(
            attempt.pr_number,
            MERGE_ATTEMPTS_MARKER,
            render_attempts_comment(attempt.pr_number, attempts),
        )
    except (ForgeError, ForgeWriteRefusedError, ValueError) as exc:
        _log.warning(
            "merge_attempts.comment_failed",
            pr_number=attempt.pr_number,
            outcome=attempt.outcome,
            error=str(exc)[:200],
        )
        return False
    return True


def attempts_from_db(db: Path, pr_number: int) -> list[MergeAttempt]:
    """The PR's attempts as the local ``pr_merges`` table holds them, oldest first."""
    return [
        MergeAttempt(
            pr_number=row.pr_number,
            head_sha=row.head_sha,
            outcome=row.outcome,
            reason=row.notes,
            pending_checks=row.pending_checks,
            trigger=row.trigger or "cli",
            rule_version=row.rule_version,
            recorded_at=row.created_at,
        )
        for row in fetch_merge_rows(db, pr_number)
    ]


def load_attempts(forge: Forge | None, db: Path | None, pr_number: int) -> list[MergeAttempt]:
    """Read the attempts from the PR comment, else from the local database."""
    if forge is not None:
        try:
            comment = forge.find_marked_comment(pr_number, MERGE_ATTEMPTS_MARKER)
            if comment is not None:
                return parse_attempts_comment(comment.body)
        except (ForgeError, ValueError) as exc:
            _log.warning("merge_attempts.comment_unreadable", pr_number=pr_number, error=str(exc))
    return attempts_from_db(db, pr_number) if db is not None else []


def attempts_report(pr_number: int, attempts: Sequence[MergeAttempt]) -> MergeAttemptsReport:
    """Build the ``format:merge-attempts-json`` object."""
    listed = list(attempts)
    return MergeAttemptsReport(
        pr=pr_number, attempts=listed, count=len(listed), last=listed[-1] if listed else None
    )


def attempts_line(attempts: Sequence[MergeAttempt]) -> str:
    """``merge attempts: <n>, last: <OUTCOME> on <check|reason> at <t> (<trigger>)``."""
    if not attempts:
        return "merge attempts: 0"
    last = attempts[-1]
    subject = ""
    if last.pending_checks:
        subject = f" on {', '.join(last.pending_checks)}"
    elif last.outcome not in MERGED_OUTCOMES and last.reason:
        subject = f" on {last.reason[:120]}"
    return (
        f"merge attempts: {len(attempts)}, last: {last.outcome}{subject} "
        f"at {format_time(last.recorded_at)} ({last.trigger})"
    )


def stranded(
    prs: Sequence[PullRequest],
    verdicts: Mapping[int, RequiredChecksVerdict],
    attempts: Mapping[int, Sequence[MergeAttempt]],
    now: datetime,
    grace_s: int = STRANDED_GRACE_S,
) -> list[StrandedPR]:
    """List the open, non-draft PRs green at their head that no merge reached.

    A PR is stranded when its required checks are ``green`` since ``t``,
    ``now`` is past ``t + grace_s``, and its last attempt at that head is
    absent, predates ``t``, or was a skip. A PR whose last attempt merged
    after ``t``, or failed loudly after ``t``, is not listed; neither is
    one that is not green (pending, cancelled, missing or failed).
    """
    rows: list[StrandedPR] = []
    for pr in prs:
        if pr.draft or pr.state != "open":
            continue
        verdict = verdicts.get(pr.number)
        if verdict is None or verdict.state != "green" or verdict.green_since is None:
            continue
        since = verdict.green_since
        if now < since + timedelta(seconds=grace_s):
            continue
        at_head = [
            a for a in attempts.get(pr.number, ()) if not a.head_sha or a.head_sha == pr.head_sha
        ]
        last = max(at_head, key=lambda a: a.recorded_at) if at_head else None
        green = f"stranded: green since {format_time(since)}"
        if last is None:
            reason = f"{green}, no merge attempt"
        elif last.recorded_at < since or last.outcome.startswith("SKIP_"):
            reason = (
                f"{green}, last attempt {last.outcome} ({last.reason[:160]}) "
                f"at {format_time(last.recorded_at)}"
            )
        else:
            continue
        rows.append(
            StrandedPR(
                pr_number=pr.number,
                head_sha=pr.head_sha,
                green_since=since,
                last_attempt=last,
                reason=reason,
            )
        )
    return rows


def judge_pull_request(
    forge: Forge, pr: PullRequest, required: Sequence[str], producers: Mapping[str, str]
) -> RequiredChecksVerdict:
    """Read one PR's check runs at its head and judge the required ones.

    Raises:
        ForgeError: The check runs could not be read.
    """
    status = forge.ci_status(pr.number, head_sha=pr.head_sha or None, required=list(required))
    return judge_required_checks(status.checks, required, pr.head_sha, producers=producers)


def dispatch_stranded(
    forge: Forge,
    rows: Sequence[StrandedPR],
    *,
    dry_run: bool = False,
    echo: Callable[[str], None],
    workflow: str = MERGE_WORKFLOW_FILE,
) -> list[int]:
    """Start ``workflow`` once per stranded PR, through the write barrier (D3).

    Under ``dry_run`` nothing reaches the forge: each PR is announced as
    ``would dispatch``. A refused or failed dispatch is announced and
    skipped.

    Returns:
        The PR numbers dispatched.
    """
    dispatched: list[int] = []
    for row in rows:
        if dry_run:
            echo(f"would dispatch #{row.pr_number} ({row.reason})")
            continue
        try:
            forge.dispatch_workflow(workflow, {"pr": str(row.pr_number)})
        except (ForgeError, ForgeWriteRefusedError) as exc:
            _log.warning("merge_wait.dispatch_failed", pr_number=row.pr_number, error=str(exc))
            echo(f"dispatch failed #{row.pr_number}: {exc}")
            continue
        echo(f"dispatched #{row.pr_number} ({row.reason})")
        dispatched.append(row.pr_number)
    return dispatched


__all__ = [
    "COALESCE_WINDOW_S",
    "DEFAULT_REARM_WORKFLOWS",
    "MERGE_ATTEMPTS_KEPT",
    "MERGE_ATTEMPTS_MARKER",
    "MERGE_RULE_VERSION",
    "STRANDED_GRACE_S",
    "CheckJudgement",
    "MergeAttempt",
    "MergeAttemptsComment",
    "MergeAttemptsReport",
    "MergeWaitConfigError",
    "RequiredChecksVerdict",
    "StrandedPR",
    "assert_rearmed",
    "attempts_line",
    "attempts_report",
    "check_producers",
    "dispatch_stranded",
    "judge_pull_request",
    "judge_required_checks",
    "load_attempts",
    "rearm_workflows",
    "required_checks",
    "stranded",
    "trigger_from_env",
    "unrearmed_checks",
    "upsert_attempt",
    "workflow_checks",
]
