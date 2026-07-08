"""Unit tests for SP-RELEASE-GATE -- the pure release-range classifier and decision core.

The classifier is pinned on the squash-subject suffix pattern; the
decision function is pinned on every red-fact condition.
"""

from __future__ import annotations

from ferova.review.release_gate import (
    ReleaseFacts,
    classify_release_range,
    compute_release_decision,
)


def _facts(**over: object) -> ReleaseFacts:
    base: dict[str, object] = {
        "develop_sha": "abc123",
        "out_of_band_commits": [],
        "remote_sha": "abc123",
        "pr_head_sha": None,
        "ci_green": True,
        "ci_checked": True,
    }
    base.update(over)
    return ReleaseFacts(**base)


def test_release_range_all_squashes() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Fix sentinel max tokens (#37)",
        "Wire release gate CLI (#59)",
    ]
    assert classify_release_range(subjects) == []


def test_release_range_flags_non_pr_commit() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Hotfix: patch prod config directly",
        "Wire release gate CLI (#59)",
    ]
    flagged = classify_release_range(subjects)
    assert flagged == ["Hotfix: patch prod config directly"]


def test_release_gate_fail_closed_on_red_ci() -> None:
    facts = _facts(ci_green=False)
    decision = compute_release_decision(facts)
    assert decision.merge is False
    assert any("CI" in reason for reason in decision.reasons)
