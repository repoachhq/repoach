"""Tests for SP-VERDICT-FLIP (10a) — the read-only pure merge gate.

Covers the shared :func:`decide_at_head` unit (so the CI auto-merge and
the local ``ferova review gate`` can never drift), the non-blocking
:func:`ci_snapshot_green`, and the CLI exit-code routing (0 merge-ready /
5 refused / 1 could-not-evaluate).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

from ferova.cli import review_cmds
from ferova.review.auto_merge import (
    DEFAULT_REQUIRED_CHECK_NAMES,
    GateEvaluation,
    decide_at_head,
    evaluate_merge_gate,
)
from ferova.review.findings import record_review_integrity
from ferova.review.gh_client import GhResult
from ferova.review.merge_gate import MergeDecision, MergeFacts

_HEAD = "head_abc123"


def _rollup(*, green: bool) -> list[dict]:
    rows = [
        {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in DEFAULT_REQUIRED_CHECK_NAMES
    ]
    if not green:
        rows[0] = {**rows[0], "conclusion": "FAILURE"}
    return rows


def _gh(*, green: bool = True, head: str = "feat/x") -> MagicMock:
    """Build a truthful fake GhCli — ``ls-remote`` agrees with ``pr_head_sha``.

    SP-AUTOMERGE-FRESH-HEAD (operator rule: no stubs) —
    ``evaluate_merge_gate`` now runs the real ``resolve_verified_head``
    convergence check before ``decide_at_head``, so this fake must
    answer ``pr_view`` with a ``headRefName`` and answer
    ``_run_git(["ls-remote", ...])`` with the SAME SHA ``pr_head_sha``
    returns — otherwise the real check would (correctly) refuse every
    scenario here as a stale head.
    """
    gh = MagicMock()
    gh.pr_head_sha.return_value = _HEAD
    gh.pr_view.return_value = {"baseRefName": "develop", "headRefName": head, "state": "OPEN"}
    gh._run_git.return_value = GhResult(
        returncode=0,
        stdout=f"{_HEAD}\trefs/heads/{head}\n",
        stderr="",
        argv=["git", "ls-remote", "origin", f"refs/heads/{head}"],
    )

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            nd = "\n".join(json.dumps(e) for e in _rollup(green=green))
            return GhResult(returncode=0, stdout=nd, stderr="", argv=args)
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in " ".join(args):
            return GhResult(
                returncode=0,
                stdout=json.dumps({"statusCheckRollup": _rollup(green=green)}),
                stderr="",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run.side_effect = _run_side
    return gh


def _seed_complete(db: Path, pr_number: int = 1) -> None:
    record_review_integrity(db, pr_number=pr_number, head_sha=_HEAD, n_reviewers=4, n_unparsed=0)


def test_evaluate_merge_gate_merges_when_green(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    _seed_complete(db)
    evaluation = evaluate_merge_gate(1, gh=_gh(green=True), db_path=db, repo_root=tmp_path)
    assert evaluation.ci_gate.green is True
    assert evaluation.decision.merge is True
    assert evaluation.head_sha == _HEAD


def test_evaluate_merge_gate_blocks_on_ci_red(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    _seed_complete(db)
    evaluation = evaluate_merge_gate(
        1, gh=_gh(green=False), db_path=db, repo_root=tmp_path, wait_seconds=0
    )
    assert evaluation.ci_gate.green is False
    assert evaluation.decision.merge is False
    assert any("CI not green" in r for r in evaluation.decision.reasons)


def test_evaluate_merge_gate_reaches_shared_decide_at_head(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    _seed_complete(db)
    gh = _gh(green=True)
    evaluation = evaluate_merge_gate(1, gh=gh, db_path=db, repo_root=tmp_path)
    _, _, direct = decide_at_head(1, gh=gh, db=db, ci_green=True, repo_root=tmp_path)
    assert evaluation.decision == direct


def _patch_eval(monkeypatch: pytest.MonkeyPatch, *, merge: bool, reasons: list[str]) -> None:
    facts = MergeFacts(
        head_sha=_HEAD,
        ci_green=merge,
        open_blocking_findings=0 if merge else 1,
        spec_covered=True,
        spec_coverage_known=True,
        review_complete=True,
        review_integrity_known=True,
    )
    evaluation = GateEvaluation(
        head_sha=_HEAD,
        ci_gate=MagicMock(green=merge),
        facts=facts,
        decision=MergeDecision(merge=merge, reasons=reasons),
    )
    monkeypatch.setattr(review_cmds, "evaluate_merge_gate", lambda pr_number: evaluation)


def test_cli_gate_exit_zero_when_merge_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, merge=True, reasons=[])
    review_cmds.review_gate(1)


def test_cli_gate_exit_five_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_eval(monkeypatch, merge=False, reasons=["1 open blocking finding(s) at head"])
    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_gate(1)
    assert exc.value.exit_code == 5


def test_cli_gate_exit_one_on_evaluation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(pr_number: int) -> GateEvaluation:
        raise RuntimeError("gh down")

    monkeypatch.setattr(review_cmds, "evaluate_merge_gate", _boom)
    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_gate(1)
    assert exc.value.exit_code == 1
