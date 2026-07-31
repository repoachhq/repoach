"""Pins the three deduplicated taxonomy constants (SP-DEDUP-CLASSIFICATION-CONSTANTS).

Mirrors the already-fixed ``_JUDGED_TYPES`` / ``JUDGED_CLAIM_TYPES`` pattern for
the three remaining independently-redeclared constants: the mechanical-claim
partition, the confirmed-real status partition, and the chain-tier ordering.
Each pair must now be identity-bound to the same shared object, not merely
value-equal.
"""

from __future__ import annotations

from repoach.review import (
    chain_health,
    chain_rewrite,
    coder_findings,
    findings,
    merge_gate,
    review_lessons,
    reviewer_outcomes,
)
from repoach.review.findings import ClaimType, FindingStatus


def test_mechanical_claim_types_value_unchanged() -> None:
    assert (
        frozenset({ClaimType.MISSING_TEST, ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION})
        == findings.MECHANICAL_CLAIM_TYPES
    )


def test_confirmed_real_statuses_value_unchanged() -> None:
    assert (
        frozenset(
            {
                FindingStatus.VERIFIED,
                FindingStatus.OPEN,
                FindingStatus.RESOLVED,
                FindingStatus.STUCK,
            }
        )
        == findings.CONFIRMED_REAL_STATUSES
    )


def test_chain_tiers_value_unchanged() -> None:
    assert chain_health.CHAIN_TIERS == ("opus", "sonnet", "haiku")


def test_mechanical_claim_types_deduplicated() -> None:
    assert merge_gate._MECHANICAL_TYPES is findings.MECHANICAL_CLAIM_TYPES
    assert coder_findings._MECHANICAL_TYPES is findings.MECHANICAL_CLAIM_TYPES


def test_confirmed_real_statuses_deduplicated() -> None:
    assert reviewer_outcomes._CONFIRMED_REAL is findings.CONFIRMED_REAL_STATUSES
    assert review_lessons._CONFIRMED_REAL is findings.CONFIRMED_REAL_STATUSES


def test_chain_tiers_deduplicated() -> None:
    assert chain_rewrite._TIERS is chain_health.CHAIN_TIERS
