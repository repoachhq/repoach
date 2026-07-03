"""Tests for the auto-merge gate.

Covers: base-branch refusal, idempotency on already-merged PRs, the
pure-gate refusal (no fresh review / open blocking findings), refusal
on red required checks, happy-path merge with squash + delete-branch,
and an L4 row written to ``pr_merges`` for every outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import create_engine, text

from ferova.review.auto_merge import (
    DEFAULT_REQUIRED_CHECK_NAMES,
    OUTCOME_ALREADY_MERGED,
    OUTCOME_FAILED,
    OUTCOME_MERGED,
    OUTCOME_SKIP_BASE,
    OUTCOME_SKIP_CI_FAILED,
    OUTCOME_SKIP_GATE,
    classify_required_checks,
    fetch_check_runs,
    required_checks_green,
    run_auto_merge,
)
from ferova.review.findings import record_review_integrity
from ferova.review.gh_client import GhResult


def _all_green_rollup() -> list[dict]:
    return [
        {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in DEFAULT_REQUIRED_CHECK_NAMES
    ]


def _one_failed_rollup() -> list[dict]:
    rollup = _all_green_rollup()
    rollup[0] = {**rollup[0], "conclusion": "FAILURE"}
    return rollup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_HEAD = "head_abc123"
"""The head SHA the mocked PR resolves to; the pure gate decides at it."""


def _seed_review_complete(db: Path, pr_number: int) -> None:
    """Seed a fresh, complete review-integrity record so the pure gate permits.

    The flipped ``auto_merge`` decides on :func:`gather_merge_facts`, which
    requires a review-integrity record at the exact head with every reviewer
    parsed and zero unparsed — the evidence-first replacement for the archive
    verdict. With no findings recorded, this is the merge-permitting baseline.
    """
    record_review_integrity(db, pr_number=pr_number, head_sha=_HEAD, n_reviewers=4, n_unparsed=0)


def _gh(
    *,
    base: str = "develop",
    state: str = "OPEN",
    checks_ok: bool = True,
    merge_ok: bool = True,
    head: str = "feat/x",
) -> MagicMock:
    """Build a GhCli mock with sensible defaults — override per-test."""
    gh = MagicMock()
    gh.pr_view.return_value = {
        "baseRefName": base,
        "headRefName": head,
        "state": state,
    }
    gh.pr_head_sha.return_value = _HEAD

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            entries = _all_green_rollup() if checks_ok else _one_failed_rollup()
            nd = "\n".join(json.dumps(e) for e in entries)
            return GhResult(returncode=0, stdout=nd, stderr="", argv=args)
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in " ".join(args):
            return GhResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "statusCheckRollup": _all_green_rollup()
                        if checks_ok
                        else _one_failed_rollup()
                    }
                ),
                stderr="",
                argv=args,
            )
        if args[:2] == ["pr", "merge"]:
            return GhResult(
                returncode=0 if merge_ok else 1,
                stdout="abc1234567890abc1234567890abc1234567890a" if merge_ok else "",
                stderr="" if merge_ok else "merge failed",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run.side_effect = _run_side
    return gh


def _row_count(db_path: Path) -> int:
    """Count rows in ``pr_merges``."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM pr_merges")).scalar() or 0


def _last_outcome(db_path: Path) -> str:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        return (
            conn.execute(text("SELECT outcome FROM pr_merges ORDER BY id DESC LIMIT 1")).scalar()
            or ""
        )


# ---------------------------------------------------------------------------
# required_checks_green
# ---------------------------------------------------------------------------


def test_required_checks_green_pass() -> None:
    gh = _gh(checks_ok=True)
    ok, _ = required_checks_green(gh, 1, wait_seconds=0, poll_interval=0)
    assert ok is True


def test_required_checks_green_fail() -> None:
    gh = _gh(checks_ok=False)
    ok, msg = required_checks_green(gh, 1, wait_seconds=0, poll_interval=0)
    assert ok is False
    # Reports the structured failure reason from the status rollup.
    assert "required_check_failed" in msg
    assert "FAILURE" in msg


# ---------------------------------------------------------------------------
# run_auto_merge — gates
# ---------------------------------------------------------------------------


def test_auto_merge_refuses_main_base(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _gh(base="main")
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_SKIP_BASE
    # gh pr merge must NOT have been called.
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []
    assert _last_outcome(db) == OUTCOME_SKIP_BASE


def test_auto_merge_idempotent_on_already_merged(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _gh(base="develop", state="MERGED")
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_ALREADY_MERGED
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []


def test_auto_merge_skips_when_review_integrity_missing(tmp_path: Path) -> None:
    """No review-integrity record at head — the pure gate refuses to merge.

    The flip drops the archive verdict as a gate; absence of a fresh,
    complete review (the evidence-first replacement) blocks the merge.
    """
    db = tmp_path / "test.db"
    gh = _gh(base="develop")
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_SKIP_GATE
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []


def test_auto_merge_skips_when_review_incomplete(tmp_path: Path) -> None:
    """An unparsed reviewer at head fails the integrity fact — no merge.

    This closes audit CRITICAL #2: a parse_failed reviewer can no longer
    promote a PR to merge, because the integrity record carries the
    unparsed count and the gate requires zero.
    """
    db = tmp_path / "test.db"
    gh = _gh(base="develop")
    record_review_integrity(db, pr_number=1, head_sha=_HEAD, n_reviewers=4, n_unparsed=2)
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_SKIP_GATE


def test_auto_merge_skips_when_ci_red(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _gh(base="develop", checks_ok=False)
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_SKIP_CI_FAILED
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []


def test_auto_merge_happy_path(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _gh(base="develop", checks_ok=True, merge_ok=True)
    _seed_review_complete(db, 1)
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_MERGED
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert len(merge_calls) == 1
    args = merge_calls[0].args[0]
    assert "--squash" in args
    assert "--delete-branch" in args
    assert _last_outcome(db) == OUTCOME_MERGED
    assert res.merged_sha is not None


def test_auto_merge_records_failure_when_gh_merge_fails(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _gh(base="develop", checks_ok=True, merge_ok=False)
    _seed_review_complete(db, 1)
    res = run_auto_merge(1, gh=gh, db_path=db)
    assert res.outcome == OUTCOME_FAILED
    assert _last_outcome(db) == OUTCOME_FAILED


def test_auto_merge_persists_each_outcome(tmp_path: Path) -> None:
    """Every gate outcome must write a row to L4 ``pr_merges``."""
    db = tmp_path / "test.db"
    run_auto_merge(1, gh=_gh(base="main"), db_path=db)
    run_auto_merge(2, gh=_gh(), db_path=db)
    run_auto_merge(3, gh=_gh(checks_ok=False), db_path=db)
    run_auto_merge(4, gh=_gh(), db_path=db)
    assert _row_count(db) == 4


def test_classify_prefers_latest_started_at() -> None:
    """A newer green run outweighs a stale cancelled one — and vice versa.

    Observed live on PR #3 (2026-07-03): the PR rollup pinned a stale
    CANCELLED entry over a fresh green run at the same SHA; neither
    source is reliably ordered, so the latest ``startedAt`` per name
    must decide.
    """
    name = DEFAULT_REQUIRED_CHECK_NAMES[0]
    other = DEFAULT_REQUIRED_CHECK_NAMES[1]
    green_other = {"name": other, "status": "COMPLETED", "conclusion": "SUCCESS"}
    stale_cancel_then_green = [
        {
            "name": name,
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "startedAt": "2026-07-02T19:29:18Z",
        },
        {
            "name": name,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-02T23:58:48Z",
        },
        green_other,
    ]
    failed, pending, missing = classify_required_checks(stale_cancel_then_green)
    assert (failed, pending, missing) == ([], [], [])

    green_then_regression = [
        {
            "name": name,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-02T19:29:18Z",
        },
        {
            "name": name,
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-07-02T23:58:48Z",
        },
        green_other,
    ]
    failed, _, _ = classify_required_checks(green_then_regression)
    assert failed == [f"{name}=FAILURE"]


def test_ci_gate_falls_back_to_rollup_when_commit_api_fails() -> None:
    """A commit check-runs API failure degrades to the PR rollup."""
    gh = _gh(checks_ok=True)
    original_side = gh._run.side_effect

    def _api_broken(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            return GhResult(returncode=1, stdout="", stderr="boom", argv=args)
        return original_side(args)

    gh._run.side_effect = _api_broken
    ok, _ = required_checks_green(gh, 1, wait_seconds=0, poll_interval=0)
    assert ok is True


def test_fetch_check_runs_unparseable_line_returns_error() -> None:
    """A garbage NDJSON line degrades to an error, never a partial truth."""
    gh = MagicMock()
    gh._run.return_value = GhResult(
        returncode=0,
        stdout='{"name": "Test suite (Python 3.11)", "status": "COMPLETED", "conclusion": "SUCCESS"}\nnot json',
        stderr="",
        argv=["api"],
    )
    entries, err = fetch_check_runs(gh, "abc123")
    assert entries == []
    assert "unparseable" in err
