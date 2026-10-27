"""Every merge attempt is recorded with its reason and survives the job (AC8, AC9).

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G5: one ``pr_merges`` row per
attempt with the head, the pending checks, the trigger and the rule
version, and one PR comment marked ``<!-- repoach-merge-attempts:v1 -->``
upserted through the write barrier.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.review.auto_merge import run_auto_merge
from repoach.review.merge_wait import (
    MERGE_ATTEMPTS_MARKER,
    MergeAttemptsComment,
)
from tests.merge_wait_fakes import HEAD_259, seed_review
from tests.unit.test_merge_wait_rearm import (
    REPO_ROOT,
    pending_fixture,
    pending_then_green,
)


def _rows(db: Path) -> list[dict]:
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT head_sha, pending_checks, trigger, rule_version, outcome "
                "FROM pr_merges WHERE pr_number = 259 ORDER BY id"
            )
        )
        return [dict(row._mapping) for row in result]


def test_row_shape(tmp_path: Path) -> None:
    fx = pending_fixture()
    db = tmp_path / "review.db"
    pending_then_green(fx, db)

    rows = _rows(db)
    assert len(rows) == 2
    assert all(re.fullmatch(r"[0-9a-f]{40}", row["head_sha"]) for row in rows)
    assert [json.loads(row["pending_checks"]) for row in rows] == [
        ["Test suite (Python 3.11)"],
        [],
    ]
    assert all(re.fullmatch(r"merge-on-ci:\d+", row["trigger"]) for row in rows)
    assert all(row["rule_version"] for row in rows)

    marked = [c for c in fx.pr.comments if MERGE_ATTEMPTS_MARKER in str(c["body"])]
    assert len(marked) == 1
    assert [n for n, _ in fx.comment_writes] == [259, 259]
    block = re.search(r"```json\n(.*?)\n```", str(marked[0]["body"]), re.S)
    assert block is not None
    payload = MergeAttemptsComment.model_validate_json(block.group(1))
    assert [a.outcome for a in payload.attempts] == ["SKIP_CI_PENDING", "APPROVE"]
    assert not [a for a in fx.argv if any("findings-ledger" in part for part in a)]

    dry = pending_fixture(read_only=True)
    dry_db = tmp_path / "dry.db"
    seed_review(dry_db, 259, HEAD_259)
    run_auto_merge(
        259,
        forge=dry.forge,
        db_path=dry_db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="merge-on-ci:1",
        clock=dry.clock,
    )
    assert dry.writes == []
    assert dry.forge.refused_writes == []


def _cli(monkeypatch: pytest.MonkeyPatch, fx, db: Path) -> tuple[dict, str]:
    monkeypatch.setattr(review_cmds, "select_forge", lambda **_k: fx.forge)
    monkeypatch.setattr(
        review_cmds,
        "get_settings",
        lambda: SimpleNamespace(db_path=str(db), integration_branch="develop"),
    )
    result = CliRunner().invoke(review_cmds.review_app, ["merge-attempts", "259"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout), result.stderr


def test_cli_attempts_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = pending_fixture()
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    run_auto_merge(
        259,
        forge=fx.forge,
        db_path=db,
        repo_root=REPO_ROOT,
        wait_seconds=0,
        trigger="merge-on-ci:40288565429",
        clock=fx.clock,
    )
    report, stderr = _cli(monkeypatch, fx, db)
    assert report["count"] == 1
    assert "merge attempts: 1, last: SKIP_CI_PENDING on Test suite (Python 3.11)" in stderr

    full = pending_fixture()
    full_db = tmp_path / "full.db"
    pending_then_green(full, full_db)
    report, stderr = _cli(monkeypatch, full, full_db)
    assert report["count"] == 2
    assert report["last"]["outcome"] == "APPROVE"
    assert set(report) == {"pr", "attempts", "count", "last"}
    assert "merge attempts: 2, last: APPROVE" in stderr


def test_db_is_the_fallback_when_no_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = pending_fixture()
    db = tmp_path / "review.db"
    pending_then_green(fx, db)
    fx.pr.comments.clear()
    report, stderr = _cli(monkeypatch, fx, db)
    assert report["count"] == 2
    assert "merge attempts: 2, last: APPROVE" in stderr
