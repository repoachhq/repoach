"""Tests for the merge exit-code contract (SP-MERGE-EXIT-CONTRACT).

Every ``OUTCOME_*`` constant must be classified into a known bucket
so a new outcome can never silently regress to exit 1.
"""

from __future__ import annotations

import inspect

import pytest

import ferova.review.auto_merge as am


def test_every_outcome_constant_is_classified() -> None:
    """Reflect over every ``OUTCOME_*`` attribute and assert membership."""

    outcome_values: set[str] = set()
    for name, value in inspect.getmembers(am):
        if not name.startswith("OUTCOME_"):
            continue
        if not isinstance(value, str):
            continue
        outcome_values.add(value)

    assert outcome_values, "No OUTCOME_* constants found"

    for value in outcome_values:
        classified = (
            value in am.SUCCESS_OUTCOMES
            or value in am.NON_FATAL_SKIP_OUTCOMES
            or value == am.OUTCOME_FAILED
        )
        assert classified, (
            f"OUTCOME_* value {value!r} is not in SUCCESS_OUTCOMES, "
            f"NON_FATAL_SKIP_OUTCOMES, or OUTCOME_FAILED"
        )


@pytest.mark.parametrize(
    "outcome",
    [
        am.OUTCOME_MERGED,
        am.OUTCOME_ALREADY_MERGED,
    ],
)
def test_merge_exit_code_success_outcomes_zero(outcome: str) -> None:
    assert am.merge_exit_code(outcome) == 0


@pytest.mark.parametrize(
    "outcome",
    [
        am.OUTCOME_SKIP_BASE,
        am.OUTCOME_SKIP_GATE,
        am.OUTCOME_SKIP_CI,
        am.OUTCOME_SKIP_CI_FAILED,
        am.OUTCOME_SKIP_CI_TIMEOUT,
        am.OUTCOME_SKIP_CI_MISSING,
        am.OUTCOME_SKIP_STALE_HEAD,
    ],
)
def test_merge_exit_code_non_fatal_skips_five(outcome: str) -> None:
    assert am.merge_exit_code(outcome) == 5


def test_merge_exit_code_failed_and_unknown_one() -> None:
    assert am.merge_exit_code(am.OUTCOME_FAILED) == 1
    assert am.merge_exit_code("BOGUS_UNKNOWN_STRING") == 1
