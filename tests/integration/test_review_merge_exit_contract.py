"""Integration tests for the merge exit-code contract (SP-MERGE-EXIT-CONTRACT).

Exercises ``ferova review merge`` through ``CliRunner`` with
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
        lambda pr_number: AutoMergeResult(
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
        lambda pr_number: AutoMergeResult(
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
