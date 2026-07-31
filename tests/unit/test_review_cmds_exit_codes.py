"""SP-CONSISTENCY-SWEEP G4 — CLI exit codes derive from ``reason_code``.

Audit 2026-07-13 finding C4: ``repoach review develop`` / ``repoach
review plan`` mapped ``typer.Exit`` codes by substring-matching the
free-text ``no_op_reason`` / ``error`` (e.g. ``"ruff" in reason``,
``"pytest" in reason``). A coincidental substring inside an unrelated
message — a step-contract violation whose text happens to mention
``"ruff reformatting"`` while never touching the ruff gate itself — was
silently mismapped to the ruff/pytest exit code. Both CLI commands now
map ``reason_code -> exit code`` by identity instead.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from repoach.cli.review_cmds import review_app
from repoach.review.coder_findings import ReasonCode
from repoach.review.dev_runner import DevSessionResult
from repoach.review.planner import PlannerOutcome


def _dev_result(**overrides) -> DevSessionResult:
    base = {"spec_id": "SP-X"}
    base.update(overrides)
    return DevSessionResult(**base)


def test_reason_code_maps_to_exit_not_substring() -> None:
    """A decoy ``no_op_reason`` mentioning "ruff" maps by ``reason_code``, not text.

    Pre-fix, the CLI scanned ``no_op_reason`` text and would have raised
    exit ``3`` here purely because the string "ruff" appears in it — this
    test fails on that pre-change code. Post-fix, the untouched
    ``reason_code`` (``NONE``, a contract-violation case with no
    dedicated exit-code branch) drives the real ``not pushed`` fallback
    to exit ``1`` instead.
    """
    runner = CliRunner()
    decoy = _dev_result(
        no_op_reason=(
            "step 2 ('write config'): the gates introduced changes outside "
            "the contract: ['other.py'] (e.g. ruff reformatting an unrelated "
            "file) - refusing to commit them; not retried"
        ),
        reason_code=ReasonCode.NONE,
        pushed=False,
    )
    with patch("repoach.cli.review_cmds.run_developer_session", return_value=decoy):
        result = runner.invoke(review_app, ["develop", "SP-X"])

    assert result.exit_code == 1


def test_ruff_red_reason_code_maps_to_exit_3() -> None:
    """A real ``RUFF_RED`` reason code maps to exit 3 through the real CLI."""
    runner = CliRunner()
    ruffed = _dev_result(
        no_op_reason="some unrelated free text with no matching keyword",
        reason_code=ReasonCode.RUFF_RED,
        pushed=False,
    )
    with patch("repoach.cli.review_cmds.run_developer_session", return_value=ruffed):
        result = runner.invoke(review_app, ["develop", "SP-X"])

    assert result.exit_code == 3


def test_self_verify_reason_code_maps_to_exit_6() -> None:
    runner = CliRunner()
    failed = _dev_result(reason_code=ReasonCode.SELF_VERIFY, pushed=False)
    with patch("repoach.cli.review_cmds.run_developer_session", return_value=failed):
        result = runner.invoke(review_app, ["develop", "SP-X"])

    assert result.exit_code == 6


def test_decompose_reason_code_maps_to_exit_7() -> None:
    runner = CliRunner()
    failed = _dev_result(reason_code=ReasonCode.DECOMPOSE_OR_SUPERSEDE, pushed=False)
    with patch("repoach.cli.review_cmds.run_developer_session", return_value=failed):
        result = runner.invoke(review_app, ["develop", "SP-X"])

    assert result.exit_code == 7


def test_spec_not_found_reason_code_maps_to_exit_5_for_develop() -> None:
    runner = CliRunner()
    failed = _dev_result(reason_code=ReasonCode.SPEC_NOT_FOUND, pushed=False)
    with patch("repoach.cli.review_cmds.run_developer_session", return_value=failed):
        result = runner.invoke(review_app, ["develop", "SP-X"])

    assert result.exit_code == 5


def test_spec_not_found_reason_code_maps_to_exit_5_for_plan() -> None:
    runner = CliRunner()
    outcome = PlannerOutcome(
        spec_id="SP-X",
        error="spec not found: no doc",
        reason_code=ReasonCode.SPEC_NOT_FOUND,
        written=False,
    )
    with patch("repoach.review.planner.run_planner_session", return_value=outcome):
        result = runner.invoke(review_app, ["plan", "SP-X"])

    assert result.exit_code == 5
