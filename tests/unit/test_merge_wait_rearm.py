"""A pending required check re-arms the merge through an event (AC2, AC12).

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G2: the skip names the pending
check and the workflow whose completion re-runs the merge; that
completion is a ``workflow_run`` event merge-on-ci.yml admits, and the
second invocation merges. ``test_measured_run_replay`` replays the six
measured decisions of the spec's measurement plan through the new code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from repoach.review import auto_merge
from repoach.review.auto_merge import (
    OUTCOME_MERGED,
    OUTCOME_SKIP_CI_PENDING,
    OUTCOME_SKIP_GATE,
    AutoMergeResult,
    merge_exit_code,
    run_auto_merge,
)
from repoach.review.merge_gate import MergeDecision
from repoach.review.merge_wait import (
    MergeAttempt,
    judge_required_checks,
    required_checks,
    stranded,
)
from tests.github_concurrency_sim import Context, job_condition
from tests.merge_wait_fakes import (
    HEAD_247,
    HEAD_249,
    HEAD_256,
    HEAD_259,
    REPOSITORY,
    FixturePR,
    MergeWaitFixtureForge,
    ci_runs,
    measured_check_runs,
    seed_review,
    ts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_ON_CI = REPO_ROOT / ".github" / "workflows" / "merge-on-ci.yml"


def merge_on_ci_admits(event: dict) -> bool:
    """Whether merge-on-ci.yml's trigger and job ``if`` admit a ``workflow_run`` event."""
    workflow = yaml.safe_load(MERGE_ON_CI.read_text(encoding="utf-8"))
    triggers = workflow[True]["workflow_run"]["workflows"]
    if event["event"]["workflow_run"]["name"] not in triggers:
        return False
    ctx = Context(
        github={
            "event_name": event["event_name"],
            "actor": event["actor"],
            "event": event["event"],
            "repository": event["repository"],
        }
    )
    return job_condition(workflow["jobs"]["merge"]["if"], ctx)


def pending_then_green(fx: MergeWaitFixtureForge, db: Path) -> list[AutoMergeResult]:
    """AC2's two invocations: pending at t0, then the CI-completed event at t0 + 600 s."""
    seed_review(db, 259, HEAD_259)
    first = run_auto_merge(
        259,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="merge-on-ci:40288565429",
        clock=fx.clock,
    )
    results = [first]
    fx.advance(600)
    event = fx.complete("Test suite (Python 3.11)")
    if merge_on_ci_admits(event):
        results.append(
            run_auto_merge(
                259,
                forge=fx.forge,
                db_path=db,
                repo_root=REPO_ROOT,
                wait_seconds=0,
                trigger="merge-on-ci:40288600000",
                clock=fx.clock,
            )
        )
    return results


def pending_fixture(*, read_only: bool = False) -> MergeWaitFixtureForge:
    """PR #259 with ``Test suite (Python 3.11)`` in progress, the four others green."""
    return MergeWaitFixtureForge(
        [
            FixturePR(
                259,
                HEAD_259,
                "docs/sp-approved-spec-without-plan",
                check_runs=ci_runs(in_progress=["Test suite (Python 3.11)"]),
            )
        ],
        read_only=read_only,
    )


def test_pending_required_check_rearms_then_merges(tmp_path: Path) -> None:
    fx = pending_fixture()

    results = pending_then_green(fx, tmp_path / "review.db")

    first = results[0]
    assert first.outcome == OUTCOME_SKIP_CI_PENDING
    assert first.notes == "pending: Test suite (Python 3.11); re-armed by workflow CI"
    assert merge_exit_code(first.outcome) == 5
    assert len(results) == 2, "merge-on-ci.yml did not admit the CI-completed event"
    assert results[1].outcome == OUTCOME_MERGED
    assert fx.merges == [259]


@dataclass(frozen=True)
class Replay:
    """One measured decision, cut at its log's ``ci_gate_poll`` time."""

    run_id: str
    pr: int
    head: str
    head_ref: str
    decided_at: str
    old_outcome: str
    gate: MergeDecision | None = None


_SELECTORS = "spec acceptance selectors not all present"
_RULE_1025 = MergeDecision(merge=False, reasons=[_SELECTORS])

REPLAYS: list[Replay] = [
    Replay(
        "40288565429",
        259,
        HEAD_259,
        "docs/sp-approved-spec-without-plan",
        "2026-10-26T07:55:27Z",
        "SKIP_CI_PENDING (UNSTABLE)",
    ),
    Replay(
        "40285144378",
        259,
        HEAD_259,
        "docs/sp-approved-spec-without-plan",
        "2026-10-26T07:58:57Z",
        "SKIP_CI_PENDING (UNSTABLE)",
    ),
    Replay(
        "40282435740",
        256,
        HEAD_256,
        "fix/reverify-finds-the-test-by-name",
        "2026-10-26T07:38:22Z",
        "SKIP_CI_PENDING (UNSTABLE)",
    ),
    Replay(
        "40284297500",
        256,
        HEAD_256,
        "fix/reverify-finds-the-test-by-name",
        "2026-10-26T07:14:20Z",
        "SKIP_CI_PENDING (UNSTABLE)",
    ),
    Replay(
        "40201873213",
        247,
        HEAD_247,
        "docs/sp-claude-code-first-class",
        "2026-10-25T15:02:08Z",
        "SKIP_CI_TIMEOUT",
    ),
    Replay(
        "40201873213+CI-15:23:29Z (10-25 rule)",
        247,
        HEAD_247,
        "docs/sp-claude-code-first-class",
        "2026-10-25T15:23:29Z",
        "-",
        _RULE_1025,
    ),
    Replay(
        "40201873213+CI-15:23:29Z (#259 rule)",
        247,
        HEAD_247,
        "docs/sp-claude-code-first-class",
        "2026-10-25T15:23:29Z",
        "-",
        MergeDecision(merge=True, reasons=[]),
    ),
    Replay(
        "40215051222",
        249,
        HEAD_249,
        "docs/sp-ci-red-is-evidence",
        "2026-10-25T17:46:14Z",
        "SKIP_GATE",
        _RULE_1025,
    ),
    Replay(
        "40217966040",
        249,
        HEAD_249,
        "docs/sp-ci-red-is-evidence",
        "2026-10-25T17:45:51Z",
        "SKIP_GATE",
        _RULE_1025,
    ),
]

EXPECTED: dict[str, tuple[str, str]] = {
    "40288565429": (OUTCOME_MERGED, ""),
    "40285144378": (OUTCOME_MERGED, ""),
    "40282435740": (OUTCOME_MERGED, ""),
    "40284297500": (OUTCOME_MERGED, ""),
    "40201873213": (
        OUTCOME_SKIP_CI_PENDING,
        "pending: Test suite (Python 3.11); re-armed by workflow CI",
    ),
    "40201873213+CI-15:23:29Z (10-25 rule)": (OUTCOME_SKIP_GATE, _SELECTORS),
    "40201873213+CI-15:23:29Z (#259 rule)": (OUTCOME_MERGED, ""),
    "40215051222": (OUTCOME_SKIP_GATE, _SELECTORS),
    "40217966040": (OUTCOME_SKIP_GATE, _SELECTORS),
}


def replay(case: Replay, db: Path, monkeypatch: pytest.MonkeyPatch) -> AutoMergeResult:
    """Run one measured decision through ``run_auto_merge`` on the recorded check runs.

    The check runs are the ones recorded at the head, cut at the decision
    time. The pure gate is the real one over a complete review, except
    where the run log recorded a refusal (or the spec names the rule in
    force): that decision is replayed as logged.
    """
    cut = ts(case.decided_at)
    fx = MergeWaitFixtureForge(
        [
            FixturePR(
                case.pr, case.head, case.head_ref, check_runs=measured_check_runs(case.head, cut)
            )
        ],
        now=cut,
    )
    seed_review(db, case.pr, case.head)
    if case.gate is not None:
        decision = case.gate
        real = auto_merge.decide_at_head

        def logged(pr_number: int, **kwargs: object):
            head, facts, _ = real(pr_number, **kwargs)
            return head, facts, decision

        monkeypatch.setattr(auto_merge, "decide_at_head", logged)
    return run_auto_merge(
        case.pr,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger=f"replay:{case.run_id}",
        clock=fx.clock,
    )


@pytest.mark.parametrize("case", REPLAYS, ids=[c.run_id for c in REPLAYS])
def test_measured_run_replay(case: Replay, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = replay(case, tmp_path / "review.db", monkeypatch)
    outcome, note = EXPECTED[case.run_id]
    assert result.outcome == outcome
    assert note in result.notes


def test_249_is_listed_stranded_with_its_gate_reason() -> None:
    names = required_checks(REPO_ROOT)
    fx = MergeWaitFixtureForge(
        [
            FixturePR(
                249,
                HEAD_249,
                "docs/sp-ci-red-is-evidence",
                check_runs=measured_check_runs(HEAD_249),
            )
        ]
    )
    checks = fx.forge.ci_status(249, head_sha=HEAD_249, required=names).checks
    verdict = judge_required_checks(checks, names, HEAD_249)
    last = MergeAttempt(
        pr_number=249,
        head_sha=HEAD_249,
        outcome=OUTCOME_SKIP_GATE,
        reason=f"pure gate refused: {_SELECTORS}",
        trigger="auto-review:40215051222",
        rule_version="10-25",
        recorded_at=ts("2026-10-25T17:46:14Z"),
    )
    rows = stranded(
        fx.forge.open_pull_requests("develop"),
        {249: verdict},
        {249: [last]},
        datetime.fromisoformat("2026-10-26T08:00:00+00:00"),
    )
    assert [row.pr_number for row in rows] == [249]
    assert rows[0].reason == (
        "stranded: green since 2026-10-25T17:45:05Z, last attempt SKIP_GATE "
        f"(pure gate refused: {_SELECTORS}) at 2026-10-25T17:46:14Z"
    )


def test_merge_on_ci_admits_both_rearming_workflows_and_review_events() -> None:
    fx = MergeWaitFixtureForge([FixturePR(259, HEAD_259, "docs/x", check_runs=ci_runs())])
    event = fx.complete("Test suite (Python 3.11)")
    assert merge_on_ci_admits(event)
    event["event"]["workflow_run"]["name"] = "Auto Review (review bots)"
    event["event"]["workflow_run"]["event"] = "pull_request_review"
    assert merge_on_ci_admits(event)
    event["event"]["workflow_run"]["event"] = "issue_comment"
    assert not merge_on_ci_admits(event)
    event["event"]["workflow_run"]["event"] = "pull_request"
    event["event"]["workflow_run"]["head_repository"] = {"full_name": "fork/repoach"}
    assert not merge_on_ci_admits(event)
    assert REPOSITORY == "repoachhq/repoach"
