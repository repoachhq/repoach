"""Sentinel inherits the base reviewer token budget — no shrinking override.

The base ``Reviewer.max_tokens`` default is 4096, sized for reasoning
models that emit a chain-of-thought BEFORE the structured JSON (live
mid-JSON truncation at the historic 1500 cap, PR #93).  Sentinel — the
only reviewer on ``PROXY_OPUS_CHAIN``, where reasoning models are most
likely — carried a stale ``max_tokens = 2000`` override from the
1500-cap era, leaving the security bench with the lowest budget of the
four reviewers and re-exposing exactly the truncation class the 4096
base fixed.  These tests pin the invariant so a shrinking override
cannot silently return on any role.
"""

from __future__ import annotations

from ferova.review.reviewer import Architect, Reviewer, Scribe, Sentinel, Tester


def test_sentinel_inherits_base_max_tokens() -> None:
    """Sentinel must not override the base reviewer token budget."""
    assert Sentinel.max_tokens == Reviewer.max_tokens


def test_no_reviewer_shrinks_the_base_budget() -> None:
    """Every reviewer role carries at least the base 4096 budget."""
    for role in (Architect, Sentinel, Tester, Scribe):
        assert role.max_tokens >= Reviewer.max_tokens
