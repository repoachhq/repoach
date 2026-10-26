"""A green PR no merge reached is listed, and the release sweep re-arms it (AC10, AC13).

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G3 and D3, on the heads measured
for #256, #247 and #259 (all five CI checks recorded green).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.review.merge_wait import (
    MergeAttempt,
    judge_required_checks,
    load_attempts,
    render_attempts_comment,
    required_checks,
    stranded,
)
from tests.merge_wait_fakes import (
    BOT,
    HEAD_247,
    HEAD_256,
    HEAD_259,
    FixturePR,
    MergeWaitFixtureForge,
    ci_runs,
    measured_check_runs,
    ts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.fromisoformat("2026-10-26T10:00:00+00:00")
SELECTORS = "spec acceptance selectors not all present"
GREEN_256 = "stranded: green since 2026-10-26T07:11:43Z, no merge attempt"
GREEN_247 = (
    "stranded: green since 2026-10-25T15:23:29Z, last attempt SKIP_GATE "
    f"({SELECTORS}) at 2026-10-25T15:02:08Z"
)


def _attempt(pr: int, head: str, outcome: str, reason: str, at: str) -> MergeAttempt:
    return MergeAttempt(
        pr_number=pr,
        head_sha=head,
        outcome=outcome,
        reason=reason,
        trigger="merge-on-ci:1",
        rule_version="merge-wait:1",
        recorded_at=ts(at),
    )


def _comment(pr: int, attempts: list[MergeAttempt]) -> dict[str, object]:
    return {"id": 500 + pr, "body": render_attempts_comment(pr, attempts), "user": {"login": BOT}}


def _fixture(*extra: FixturePR) -> MergeWaitFixtureForge:
    prs = [
        FixturePR(
            256,
            HEAD_256,
            "fix/reverify-finds-the-test-by-name",
            check_runs=measured_check_runs(HEAD_256),
        ),
        FixturePR(
            247,
            HEAD_247,
            "docs/sp-claude-code-first-class",
            check_runs=measured_check_runs(HEAD_247),
            comments=[
                _comment(
                    247, [_attempt(247, HEAD_247, "SKIP_GATE", SELECTORS, "2026-10-25T15:02:08Z")]
                )
            ],
        ),
        FixturePR(
            259,
            HEAD_259,
            "docs/sp-approved-spec-without-plan",
            check_runs=measured_check_runs(HEAD_259),
            comments=[
                _comment(
                    259, [_attempt(259, HEAD_259, "APPROVE", "gate green", "2026-10-26T07:58:57Z")]
                )
            ],
        ),
        *extra,
    ]
    return MergeWaitFixtureForge(prs, now=NOW)


def _invoke(monkeypatch: pytest.MonkeyPatch, fx: MergeWaitFixtureForge, tmp_path: Path, *args: str):
    monkeypatch.setattr(review_cmds, "select_forge", lambda **_k: fx.forge)
    monkeypatch.setattr(
        review_cmds,
        "get_settings",
        lambda: SimpleNamespace(db_path=str(tmp_path / "review.db"), integration_branch="develop"),
    )
    result = CliRunner().invoke(review_cmds.review_app, ["stranded", *args])
    assert result.exit_code == 0, result.output
    return result


def test_stranded_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = _fixture()
    names = required_checks(REPO_ROOT)
    prs = fx.forge.open_pull_requests("develop")
    verdicts = {
        pr.number: judge_required_checks(
            fx.forge.ci_status(pr.number, head_sha=pr.head_sha, required=names).checks,
            names,
            pr.head_sha,
        )
        for pr in prs
    }
    attempts = {pr.number: load_attempts(fx.forge, None, pr.number) for pr in prs}
    rows = stranded(prs, verdicts, attempts, NOW)
    assert [(r.pr_number, r.reason) for r in rows] == [(256, GREEN_256), (247, GREEN_247)]

    fx.argv.clear()
    result = _invoke(monkeypatch, fx, tmp_path, "--json")
    payload = json.loads(result.stdout)
    assert [(r["pr"], r["gate"], r["reason"]) for r in payload["stranded"]] == [
        (256, "stranded", GREEN_256),
        (247, "stranded", GREEN_247),
    ]
    assert payload["dispatched"] == []
    assert fx.writes == []


def test_dispatch_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = FixturePR(
        260,
        "1" * 40,
        "feat/still-running",
        check_runs=ci_runs("1" * 40, in_progress=["wheel-build"]),
    )
    fx = _fixture(pending)
    result = _invoke(monkeypatch, fx, tmp_path, "--dispatch", "--json")
    assert fx.dispatches == [
        "workflow_run merge-on-ci.yml pr=256",
        "workflow_run merge-on-ci.yml pr=247",
    ]
    assert len(fx.writes) == 2
    assert f"dispatched #256 ({GREEN_256})" in result.stderr
    assert json.loads(result.stdout)["dispatched"] == [256, 247]

    dry = _fixture(pending)
    dry_result = _invoke(monkeypatch, dry, tmp_path, "--dispatch", "--dry-run", "--json")
    assert dry.writes == []
    assert dry.dispatches == []
    assert dry.forge.refused_writes == []
    assert json.loads(dry_result.stdout)["dispatched"] == []
