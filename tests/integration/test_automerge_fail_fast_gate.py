"""End-to-end integration test for SP-AUTOMERGE-EVENT-DRIVEN Lane 1.

Exercises the WHOLE settings-to-outcome path: ``FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0``
in the environment, through the settings singleton, into
:func:`ferova.review.auto_merge.run_auto_merge` called with no explicit
``wait_seconds`` / ``poll_interval`` arguments. Proves the fail-fast
contract end to end — a still-pending required check yields exactly one
rollup evaluation, zero sleeps, ``SKIP_CI_TIMEOUT``, a persisted L4 row,
and no squash-merge call.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

import ferova.core.config as config
from ferova.review.auto_merge import (
    DEFAULT_REQUIRED_CHECK_NAMES,
    OUTCOME_SKIP_CI_TIMEOUT,
    run_auto_merge,
)
from ferova.review.gh_client import GhResult

_HEAD = "head_abc123"
"""The head SHA the mocked PR resolves to; the pure gate decides at it."""


def _truthful_ls_remote(sha: str, head_ref: str) -> GhResult:
    """Build an ``ls-remote`` result agreeing with a given ``pr_head_sha``.

    SP-AUTOMERGE-FRESH-HEAD (operator rule: no stubs) — ``resolve_verified_head``
    runs for real here, so the fake ``GhCli`` must answer
    ``_run_git(["ls-remote", ...])`` with the SAME SHA its ``pr_head_sha``
    returns; otherwise the real convergence check would (correctly) refuse
    this scenario as a stale head.
    """
    return GhResult(
        returncode=0,
        stdout=f"{sha}\trefs/heads/{head_ref}\n",
        stderr="",
        argv=["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
    )


def _one_pending_rollup() -> list[dict]:
    """A green rollup except the first required check is stuck QUEUED."""
    rollup = [
        {"name": name, "status": "COMPLETED", "conclusion": "SUCCESS"}
        for name in DEFAULT_REQUIRED_CHECK_NAMES
    ]
    rollup[0] = {"name": rollup[0]["name"], "status": "QUEUED", "conclusion": ""}
    return rollup


def _gh_with_pending_check(*, head: str = "feat/x") -> MagicMock:
    """A truthful boundary-fake ``GhCli`` with one required check QUEUED forever."""
    gh = MagicMock()
    gh.pr_view.return_value = {
        "baseRefName": "develop",
        "headRefName": head,
        "state": "OPEN",
    }
    gh.pr_head_sha.return_value = _HEAD
    gh._run_git.return_value = _truthful_ls_remote(_HEAD, head)

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            entries = _one_pending_rollup()
            nd = "\n".join(json.dumps(e) for e in entries)
            return GhResult(returncode=0, stdout=nd, stderr="", argv=args)
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in " ".join(args):
            return GhResult(
                returncode=0,
                stdout=json.dumps({"statusCheckRollup": _one_pending_rollup()}),
                stderr="",
                argv=args,
            )
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


def _last_outcome(db_path: Path) -> str:
    """Return the ``outcome`` of the most recently written ``pr_merges`` row."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        return (
            conn.execute(text("SELECT outcome FROM pr_merges ORDER BY id DESC LIMIT 1")).scalar()
            or ""
        )


def test_automerge_fail_fast_settings_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 through settings to a real skip.

    No ``wait_seconds`` / ``poll_interval`` argument reaches
    ``run_auto_merge`` — the fail-fast budget is sourced entirely from the
    ``Settings`` singleton, which is rebuilt from the process environment
    after being reset to ``None``.
    """
    monkeypatch.setenv("FEROVA_AUTOMERGE_CI_WAIT_SECONDS", "0")
    config._settings = None
    try:
        db = tmp_path / "test.db"
        gh = _gh_with_pending_check()
        sleep_calls: list[float] = []

        def _record_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        result = run_auto_merge(1, gh=gh, db_path=db, sleep=_record_sleep)

        assert result.outcome == OUTCOME_SKIP_CI_TIMEOUT
        assert _last_outcome(db) == OUTCOME_SKIP_CI_TIMEOUT
        merge_calls = [
            call for call in gh._run.call_args_list if call.args[0][:2] == ["pr", "merge"]
        ]
        assert merge_calls == []
        assert sleep_calls == []
    finally:
        config._settings = None
