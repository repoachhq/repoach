"""Integration tests for the merge exit-code contract (SP-MERGE-EXIT-CONTRACT).

Exercises ``repoach review merge`` through ``CliRunner`` with
``run_auto_merge`` replaced at the CLI seam to verify that every
non-fatal skip outcome exits 5 and FAILED exits 1.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.review.auto_merge import (
    OUTCOME_FAILED,
    OUTCOME_MERGED,
    OUTCOME_SKIP_CI_TIMEOUT,
    AutoMergeResult,
)


def test_cli_review_merge_skip_ci_timeout_exits_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKIP_CI_TIMEOUT exits 5 and the result JSON is still printed."""
    monkeypatch.setattr(
        review_cmds,
        "run_auto_merge",
        lambda pr_number, **_kwargs: AutoMergeResult(
            pr_number=pr_number,
            outcome=OUTCOME_SKIP_CI_TIMEOUT,
            notes="CI timed out",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(review_cmds.review_app, ["merge", "79"])

    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload["pr_number"] == 79
    assert payload["outcome"] == OUTCOME_SKIP_CI_TIMEOUT


def test_cli_review_merge_failed_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAILED exits 1 and the result JSON is still printed."""
    monkeypatch.setattr(
        review_cmds,
        "run_auto_merge",
        lambda pr_number, **_kwargs: AutoMergeResult(
            pr_number=pr_number,
            outcome=OUTCOME_FAILED,
            notes="gh pr merge failed",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(review_cmds.review_app, ["merge", "79"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["pr_number"] == 79
    assert payload["outcome"] == OUTCOME_FAILED


def test_cli_review_merge_pins_head_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``review merge`` re-verifies the ledger on the pinned head tree (SP-GATE-REVERIFIES-AT-HEAD)."""
    captured_kwargs: dict[str, object] = {}

    def _fake_run_auto_merge(pr_number: int, **kwargs: object) -> AutoMergeResult:
        captured_kwargs.update(kwargs)
        return AutoMergeResult(
            pr_number=pr_number,
            outcome=OUTCOME_MERGED,
            merged_sha="deadbeef",
            notes="merged",
        )

    monkeypatch.setattr(review_cmds, "run_auto_merge", _fake_run_auto_merge)

    runner = CliRunner()
    result = runner.invoke(review_cmds.review_app, ["merge", "79"])

    assert result.exit_code == 0
    assert captured_kwargs.get("pin_tree") is True
