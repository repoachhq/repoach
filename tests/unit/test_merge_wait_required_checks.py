"""The merge waits on the named required checks, never on the rollup (AC1, AC3, AC4).

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G1 and G3, on PR #259 at
``ba9585eb…`` as measured on 2026-10-26 (F3): the only running check at
the in-run merge's decision was ``Auto-merge to develop``, the job that
was deciding.
"""

from __future__ import annotations

from pathlib import Path

from repoach.forge import CheckRun
from repoach.forge.github import MERGEABILITY_FIELDS
from repoach.review.auto_merge import (
    OUTCOME_MERGED,
    OUTCOME_SKIP_CI_CANCELLED,
    OUTCOME_SKIP_CI_FAILED,
    merge_exit_code,
    run_auto_merge,
)
from repoach.review.merge_wait import judge_required_checks, required_checks
from tests.merge_wait_fakes import (
    HEAD_259,
    FixturePR,
    MergeWaitFixtureForge,
    ci_runs,
    run,
    seed_review,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fixture(check_runs: list[dict[str, object]]) -> MergeWaitFixtureForge:
    return MergeWaitFixtureForge(
        [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan", check_runs=check_runs)]
    )


def _merge(fx: MergeWaitFixtureForge, db: Path):
    seed_review(db, 259, HEAD_259)
    return run_auto_merge(
        259,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="auto-review:40285144378",
        clock=fx.clock,
    )


def _verdict(fx: MergeWaitFixtureForge):
    names = required_checks(REPO_ROOT)
    checks = fx.forge.ci_status(259, head_sha=HEAD_259, required=names).checks
    return judge_required_checks(checks, names, HEAD_259)


def test_own_run_pending_merges(tmp_path: Path) -> None:
    fx = _fixture(
        [
            *ci_runs(),
            run(
                "Auto-merge to develop",
                head=HEAD_259,
                status="in_progress",
                conclusion=None,
                started="2026-10-26T07:58:02Z",
            ),
            run(
                "Coder auto-fix loop",
                head=HEAD_259,
                status="queued",
                conclusion=None,
                started="2026-10-26T07:58:40Z",
            ),
        ]
    )

    assert _verdict(fx).state == "green"
    result = _merge(fx, tmp_path / "review.db")

    assert result.outcome == OUTCOME_MERGED
    assert fx.merges == [259]
    views = [a for a in fx.argv if a[:2] == ["pr", "view"]]
    assert ["pr", "view", "259", "--json", "statusCheckRollup"] not in views
    assert [a for a in views if "mergeStateStatus" in a[-1]] == [
        ["pr", "view", "259", "--json", MERGEABILITY_FIELDS]
    ]
    first_merge_view = fx.argv.index(["pr", "view", "259", "--json", MERGEABILITY_FIELDS])
    check_runs_reads = [
        i for i, a in enumerate(fx.argv) if a[:1] == ["api"] and "/check-runs" in a[1]
    ]
    assert check_runs_reads and max(check_runs_reads) < first_merge_view


def test_cancelled_placeholder_ignored(tmp_path: Path) -> None:
    py313 = "Test suite (Python 3.13)"
    placeholders = [
        run(py313, head=HEAD_259, conclusion="cancelled", started=at, completed=at)
        for at in ("2026-10-25T15:54:00Z", "2026-10-25T16:04:00Z", "2026-10-25T19:21:00Z")
    ]
    others = [r for r in ci_runs() if r["name"] != py313]
    real = run(
        py313, head=HEAD_259, started="2026-10-25T14:06:00Z", completed="2026-10-25T14:20:00Z"
    )

    fx = _fixture([*others, real, *placeholders])
    assert _verdict(fx).state == "green"
    merged = _merge(fx, tmp_path / "green.db")
    assert merged.outcome == OUTCOME_MERGED
    assert fx.merges == [259]

    only_cancelled = _fixture([*others, *placeholders])
    verdict = _verdict(only_cancelled)
    assert verdict.state == "cancelled"
    assert verdict.failed == []
    skipped = _merge(only_cancelled, tmp_path / "cancelled.db")
    assert skipped.outcome == OUTCOME_SKIP_CI_CANCELLED
    assert skipped.notes == "cancelled with no other run: Test suite (Python 3.13)"
    assert merge_exit_code(skipped.outcome) == 5
    assert only_cancelled.merges == []


def test_real_red_not_merged(tmp_path: Path) -> None:
    py313 = "Test suite (Python 3.13)"
    others = [r for r in ci_runs() if r["name"] != py313]
    fx = _fixture(
        [
            *others,
            run(
                py313,
                head=HEAD_259,
                started="2026-10-26T07:00:00Z",
                completed="2026-10-26T07:10:00Z",
            ),
            run(
                py313,
                head=HEAD_259,
                conclusion="failure",
                started="2026-10-26T07:40:17Z",
                completed="2026-10-26T07:52:30Z",
            ),
            run(
                py313,
                head=HEAD_259,
                conclusion="cancelled",
                started="2026-10-26T07:55:00Z",
                completed="2026-10-26T07:55:01Z",
            ),
        ]
    )

    result = _merge(fx, tmp_path / "review.db")

    assert result.outcome == OUTCOME_SKIP_CI_FAILED
    assert "Test suite (Python 3.13)=FAILURE" in result.notes
    assert fx.merges == []


def test_a_run_at_another_head_never_decides() -> None:
    names = ["Test suite (Python 3.11)"]
    stale = CheckRun(name=names[0], status="COMPLETED", conclusion="SUCCESS", head_sha="0" * 40)
    verdict = judge_required_checks([stale], names, HEAD_259)
    assert verdict.state == "missing"
    assert verdict.missing == names
