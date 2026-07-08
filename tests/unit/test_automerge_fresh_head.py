"""Tests for :func:`resolve_verified_head` (SP-AUTOMERGE-FRESH-HEAD).

PR #50 orphaned-commit incident (2026-07-06): the PR API served a
``headRefOid`` 40+ minutes stale, and ``run_auto_merge`` squash-merged
at that stale head, orphaning a repair commit already sitting on the
real branch tip.  These tests cover the bounded, fail-closed
API-vs-``git ls-remote`` convergence check that closes that hole:
immediate agreement, convergence after a bounded re-poll, and the two
fail-closed paths (persistent mismatch, ``ls-remote`` transport
failure) that must never return a best-guess SHA.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text

from ferova.review.auto_merge import (
    DEFAULT_REQUIRED_CHECK_NAMES,
    OUTCOME_SKIP_GATE,
    OUTCOME_SKIP_STALE_HEAD,
    evaluate_merge_gate,
    resolve_verified_head,
    run_auto_merge,
)
from ferova.review.gh_client import GhResult
from ferova.review.merge_gate import MergeDecision, MergeFacts

_FRESH_SHA = "abc123def456abc123def456abc123def456abc1"
_STALE_SHA = "1111111111111111111111111111111111111111"
_OTHER_SHA = "2222222222222222222222222222222222222222"


def _ls_remote_result(sha: str, head_ref: str = "feat/x") -> GhResult:
    return GhResult(
        returncode=0,
        stdout=f"{sha}\trefs/heads/{head_ref}\n",
        stderr="",
        argv=["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
    )


def test_verified_head_match_first_try(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.return_value = _ls_remote_result(_FRESH_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        sleep=sleep,
    )

    assert sha == _FRESH_SHA
    assert reason == ""
    sleep.assert_not_called()
    gh._run_git.assert_called_once_with(["ls-remote", "origin", "refs/heads/feat/x"])


def test_verified_head_converges_after_repoll(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.side_effect = [_STALE_SHA, _FRESH_SHA]
    gh._run_git.return_value = _ls_remote_result(_FRESH_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        delay_s=5.0,
        sleep=sleep,
    )

    assert sha == _FRESH_SHA
    assert reason == ""
    sleep.assert_called_once_with(5.0)


def test_verified_head_persistent_mismatch_fails_closed(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _STALE_SHA
    gh._run_git.return_value = _ls_remote_result(_OTHER_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        attempts=4,
        delay_s=1.0,
        sleep=sleep,
    )

    assert sha is None
    assert _STALE_SHA[:12] in reason
    assert _OTHER_SHA[:12] in reason
    assert f"api={_STALE_SHA[:12]}" in reason
    assert f"ls_remote={_OTHER_SHA[:12]}" in reason
    assert sleep.call_count == 3


def test_verified_head_ls_remote_error_fails_closed(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.return_value = GhResult(
        returncode=1,
        stdout="",
        stderr="fatal: unable to access origin: network unreachable",
        argv=["git", "ls-remote", "origin", "refs/heads/feat/x"],
    )
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        sleep=sleep,
    )

    assert sha is None
    assert "network unreachable" in reason
    sleep.assert_not_called()


# ---------------------------------------------------------------------------
# run_auto_merge wiring (step 2/4 — SP-AUTOMERGE-FRESH-HEAD)
# ---------------------------------------------------------------------------


def _make_gh(
    *,
    base: str = "develop",
    state: str = "OPEN",
    head: str = "feat/x",
    checks_ok: bool = True,
) -> MagicMock:
    """Build a GhCli mock covering ``pr_view`` + the CI check-runs + merge calls.

    ``pr_head_sha`` and ``_run_git`` (the ``ls-remote`` path) are left for
    each test to configure explicitly — that is exactly the surface
    :func:`resolve_verified_head` and the pre-squash re-read exercise.
    """
    gh = MagicMock()
    gh.pr_view.return_value = {
        "baseRefName": base,
        "headRefName": head,
        "state": state,
    }

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            entries = [
                {
                    "name": name,
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS" if checks_ok else "FAILURE",
                }
                for name in DEFAULT_REQUIRED_CHECK_NAMES
            ]
            nd = "\n".join(json.dumps(e) for e in entries)
            return GhResult(returncode=0, stdout=nd, stderr="", argv=args)
        if args[:2] == ["pr", "merge"]:
            return GhResult(
                returncode=0,
                stdout="abc1234567890abc1234567890abc1234567890a",
                stderr="",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run.side_effect = _run_side
    return gh


def _last_row(db_path: Path) -> dict[str, object]:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        row = (
            conn.execute(text("SELECT outcome, notes FROM pr_merges ORDER BY id DESC LIMIT 1"))
            .mappings()
            .first()
        )
    return dict(row) if row is not None else {}


def _stub_facts(head_sha: str) -> MergeFacts:
    return MergeFacts(
        head_sha=head_sha,
        ci_green=True,
        open_blocking_findings=0,
        spec_covered=True,
        spec_coverage_known=True,
        review_complete=True,
        review_integrity_known=True,
        review_integrity_any=True,
    )


def test_auto_merge_refuses_on_stale_head_and_does_not_merge(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _make_gh(base="develop", head="feat/x")
    gh.pr_head_sha.return_value = _STALE_SHA
    gh._run_git.return_value = _ls_remote_result(_OTHER_SHA)

    with patch("ferova.review.auto_merge.squash_merge") as mocked_squash:
        res = run_auto_merge(1, gh=gh, db_path=db, sleep=MagicMock())

    assert res.outcome == OUTCOME_SKIP_STALE_HEAD
    assert _STALE_SHA[:12] in res.notes
    assert _OTHER_SHA[:12] in res.notes
    mocked_squash.assert_not_called()

    row = _last_row(db)
    assert row["outcome"] == OUTCOME_SKIP_STALE_HEAD
    assert _STALE_SHA[:12] in row["notes"]
    assert _OTHER_SHA[:12] in row["notes"]

    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []


# ---------------------------------------------------------------------------
# evaluate_merge_gate wiring (step 3/4 -- SP-AUTOMERGE-FRESH-HEAD)
# ---------------------------------------------------------------------------


def test_evaluate_merge_gate_stale_head_refuses(tmp_path: Path) -> None:
    """A persistent API-vs-ls-remote mismatch refuses through the gate CLI path.

    OPERATOR RULE -- no stubs: this drives the real resolve_verified_head
    end to end (no monkeypatching of it) so evaluate_merge_gate's stale-head
    refusal is exercised exactly as ferova review gate would hit it, and
    ferova review gate exits 5 through the existing exit-code mapping.
    """
    db = tmp_path / "test.db"
    gh = _make_gh(base="develop", head="feat/x")
    gh.pr_head_sha.return_value = _STALE_SHA
    gh._run_git.return_value = _ls_remote_result(_OTHER_SHA)

    evaluation = evaluate_merge_gate(1, gh=gh, db_path=db, repo_root=tmp_path, sleep=MagicMock())

    assert evaluation.decision.merge is False
    reason_blob = " ".join(evaluation.decision.reasons)
    assert "stale head" in reason_blob
    assert _STALE_SHA[:12] in reason_blob
    assert _OTHER_SHA[:12] in reason_blob


def test_gate_facts_computed_at_verified_head(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _make_gh(base="develop", head="feat/x", checks_ok=True)
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.return_value = _ls_remote_result(_FRESH_SHA, head_ref="feat/x")

    facts = _stub_facts(_FRESH_SHA)
    decision = MergeDecision(merge=False, reasons=["stub refusal so the test never reaches squash"])

    with patch(
        "ferova.review.auto_merge.decide_at_head",
        return_value=(_FRESH_SHA, facts, decision),
    ) as mocked_decide:
        res = run_auto_merge(1, gh=gh, db_path=db, sleep=MagicMock())

    assert res.outcome == OUTCOME_SKIP_GATE
    mocked_decide.assert_called_once()
    _, kwargs = mocked_decide.call_args
    assert kwargs["head_sha"] == _FRESH_SHA
    assert kwargs["head_sha"] != gh.pr_view.return_value["headRefName"]


def test_auto_merge_refuses_when_head_moves_mid_gate(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    gh = _make_gh(base="develop", head="feat/x", checks_ok=True)
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.side_effect = [
        _ls_remote_result(_FRESH_SHA, head_ref="feat/x"),
        _ls_remote_result(_OTHER_SHA, head_ref="feat/x"),
    ]

    facts = _stub_facts(_FRESH_SHA)
    decision = MergeDecision(merge=True, reasons=[])

    with (
        patch(
            "ferova.review.auto_merge.decide_at_head",
            return_value=(_FRESH_SHA, facts, decision),
        ),
        patch("ferova.review.auto_merge.squash_merge") as mocked_squash,
    ):
        res = run_auto_merge(1, gh=gh, db_path=db, sleep=MagicMock())

    assert res.outcome == OUTCOME_SKIP_STALE_HEAD
    assert _FRESH_SHA[:12] in res.notes
    assert _OTHER_SHA[:12] in res.notes
    mocked_squash.assert_not_called()

    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []
