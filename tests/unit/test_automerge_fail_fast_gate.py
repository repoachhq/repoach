"""Settings-sourced automerge CI-gate wait/poll knobs (SP-AUTOMERGE-EVENT-DRIVEN G1).

``REPOACH_AUTOMERGE_CI_WAIT_SECONDS`` / ``REPOACH_AUTOMERGE_CI_POLL_INTERVAL``
tune the total CI-gate wait budget and the poll cadence within it.  Always
build ``Settings(_env_file=None)`` so this test is immune to env-file
anchoring changes landing in the same region (SP-CONFIG-ENV-ANCHOR).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

import repoach.core.config as config
from repoach.core.config import Settings
from repoach.forge.github import GhResult, GitHubForge
from repoach.review.auto_merge import (
    DEFAULT_REQUIRED_CHECK_NAMES,
    OUTCOME_SKIP_CI_PENDING,
    OUTCOME_SKIP_CI_TIMEOUT,
    evaluate_ci_gate,
    evaluate_merge_gate,
    required_checks_green,
    run_auto_merge,
)
from tests.forge_fakes import gh_fail, gh_ok, pr_view_stdout, scripted_github


def test_settings_env_overrides_wait_and_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPOACH_AUTOMERGE_CI_* env vars override the defaults, else 720/30."""
    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", "0")
    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_POLL_INTERVAL", "5")
    overridden = Settings(_env_file=None)
    assert overridden.automerge_ci_wait_seconds == 0
    assert overridden.automerge_ci_poll_interval == 5

    monkeypatch.delenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("REPOACH_AUTOMERGE_CI_POLL_INTERVAL", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.automerge_ci_wait_seconds == 720
    assert defaults.automerge_ci_poll_interval == 30

    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", raising=False)

    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_POLL_INTERVAL", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def _pending_rollup() -> list[dict]:
    """One required check QUEUED forever, the other green."""
    names = list(DEFAULT_REQUIRED_CHECK_NAMES)
    rollup = [{"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"} for name in names]
    rollup[0] = {"name": names[0], "status": "QUEUED", "conclusion": ""}
    return rollup


def _truthful_ls_remote(sha: str, head_ref: str) -> GhResult:
    """Build an ``ls-remote`` result agreeing with a given ``pr_head_sha``.

    SP-AUTOMERGE-FRESH-HEAD (operator rule: no stubs) — the real
    ``resolve_verified_head`` runs in every test that reaches it, so
    the fake forge must answer ``git ls-remote`` with the same SHA its
    ``gh pr view --json headRefOid`` returns.
    """
    return GhResult(
        returncode=0,
        stdout=f"{sha}\trefs/heads/{head_ref}\n",
        stderr="",
        argv=["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
    )


def _pending_gh(*, head: str = "feat/x", base: str = "develop", state: str = "OPEN") -> GitHubForge:
    """A truthful scripted GitHub forge whose rollup never goes green."""
    head_sha = "head_pending123"

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            entries = _pending_rollup()
            nd = "\n".join(json.dumps(entry) for entry in entries)
            return GhResult(returncode=0, stdout=nd, stderr="", argv=args)
        if args[:1] == ["api"] and args[1].endswith("/protection"):
            return gh_fail("gh: Branch not protected (HTTP 404)")
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in " ".join(args):
            return GhResult(
                returncode=0,
                stdout=json.dumps({"statusCheckRollup": _pending_rollup()}),
                stderr="",
                argv=args,
            )
        if args[:2] == ["pr", "view"] and args[-1] == "headRefOid":
            return gh_ok(json.dumps({"headRefOid": head_sha}), argv=args)
        if args[:2] == ["pr", "view"]:
            return gh_ok(pr_view_stdout(base_ref=base, head_ref=head, state=state), argv=args)
        if args[:2] == ["pr", "merge"]:
            raise AssertionError("gh pr merge must never be called on a CI-gate refusal")
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    return scripted_github(run=_run_side, git=lambda _a: _truthful_ls_remote(head_sha, head))


def test_wait_zero_single_evaluation_no_sleep() -> None:
    """``wait_seconds=0`` reads the rollup once, never sleeps, and skips as pending.

    SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G2: a pending required check
    at the fail-fast evaluation is ``SKIP_CI_PENDING`` (the producing
    workflow's completion re-runs the merge), no longer a timeout.
    """
    gh = _pending_gh()
    sleeps: list[float] = []

    outcome = evaluate_ci_gate(
        gh,
        1,
        wait_seconds=0,
        poll_interval=30,
        monotonic=lambda: 100.0,
        sleep=lambda seconds: sleeps.append(seconds),
    )

    assert outcome.outcome_tag == OUTCOME_SKIP_CI_PENDING
    assert DEFAULT_REQUIRED_CHECK_NAMES[0] in outcome.reason
    assert sleeps == []
    check_run_calls = [
        call
        for call in gh._run.call_args_list
        if call.args[0][:1] == ["api"] and "/check-runs" in call.args[0][1]
    ]
    assert len(check_run_calls) == 1


def test_run_auto_merge_wait_zero_persists_fail_fast_skip(tmp_path: Path) -> None:
    """``run_auto_merge`` with ``wait_seconds=0`` persists a fail-fast skip."""
    db = tmp_path / "test.db"
    gh = _pending_gh()

    result = run_auto_merge(1, forge=gh, db_path=db, wait_seconds=0)

    assert result.outcome == OUTCOME_SKIP_CI_PENDING
    assert result.notes == (f"pending: {DEFAULT_REQUIRED_CHECK_NAMES[0]}; re-armed by workflow CI")
    merge_calls = [call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]]
    assert merge_calls == []

    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM pr_merges ORDER BY id DESC LIMIT 1")
        ).scalar()
    assert outcome == OUTCOME_SKIP_CI_PENDING


def test_explicit_wait_argument_beats_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``wait_seconds`` always wins over the settings default."""
    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", "0")
    config._settings = None
    try:
        gh = _pending_gh()
        sleeps: list[float] = []
        clock = {"now": 0.0}

        def _monotonic() -> float:
            return clock["now"]

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        outcome = evaluate_ci_gate(
            gh,
            1,
            wait_seconds=60,
            poll_interval=10,
            monotonic=_monotonic,
            sleep=_sleep,
        )

        assert outcome.outcome_tag == OUTCOME_SKIP_CI_TIMEOUT
        assert sleeps, "explicit wait_seconds=60 must poll, not fail fast like settings' 0"
    finally:
        config._settings = None


def test_gate_functions_source_defaults_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no explicit wait/poll, the gate functions read Settings (wait=0)."""
    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_WAIT_SECONDS", "0")
    monkeypatch.setenv("REPOACH_AUTOMERGE_CI_POLL_INTERVAL", "30")
    config._settings = None
    try:
        sleeps: list[float] = []

        ok, _reason = required_checks_green(
            _pending_gh(),
            1,
            monotonic=lambda: 100.0,
            sleep=lambda seconds: sleeps.append(seconds),
        )
        assert ok is False
        assert sleeps == []

        db = tmp_path / "gate.db"
        evaluation = evaluate_merge_gate(
            1,
            forge=_pending_gh(),
            db_path=db,
            monotonic=lambda: 100.0,
            sleep=lambda seconds: sleeps.append(seconds),
        )
        assert evaluation.decision.merge is False
        assert sleeps == []
    finally:
        config._settings = None
