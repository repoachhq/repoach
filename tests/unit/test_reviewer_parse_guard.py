"""Reviewer._parse_response — loud failure on truncated/malformed JSON.

Re-homed from the retired ``test_review_consensus_strict_gate.py`` when
slice 10b deleted the consensus module. These pin the
``[parse_failed:TRUNCATED]`` / ``[parse_failed:MALFORMED]`` /
``[parse_failed:unknown_verdict:…]`` marker contract: a review the
runner could not parse must surface as ``REQUEST_CHANGES`` with a
``parse_failed`` summary, which ``findings_bridge._is_unparsed`` keys
on to exclude the outcome from the findings ledger and mark the review
incomplete (the integrity fact the pure merge gate refuses on).

Origin: the 2026-05-05 PR #93 bug — deepseek-reasoner emitted a long
chain-of-thought before the JSON; with a tight ``max_tokens`` the JSON
got cut mid-string and the parser silently stored the truncated blob as
a ``COMMENT`` summary. The unknown-verdict variant
(SP-CLAIM-TYPE-PARTITION-ALIGN, audit 2026-07-13) is the same silent-
``COMMENT`` bug for a mistyped ``verdict`` value instead of a missing
one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from repoach.review.reviewer import Architect, ReviewVerdict


def _new_architect() -> Architect:
    """Build an Architect with a stub AgentLoop (we won't actually run it)."""
    return Architect(loop=MagicMock())


def test_parse_response_clean_json_still_works() -> None:
    """Regression: well-formed JSON still produces the right verdict."""
    raw = """
    {
      "verdict": "APPROVE",
      "summary": "Looks fine",
      "comments": []
    }
    """
    verdict, summary, comments = _new_architect()._parse_response(raw)
    assert verdict is ReviewVerdict.APPROVE
    assert summary == "Looks fine"
    assert comments == []


def test_parse_response_truncated_json_returns_request_changes() -> None:
    """Truncation mid-JSON (max_tokens cap) -> REQUEST_CHANGES, not COMMENT."""
    raw = (
        '{\n  "verdict": "REQUEST_CHANGES",\n  "summary": '
        '"The deploy script comments describe a checkout/restore '
        "mechanism that is not implemented, leading"
    )
    verdict, summary, _ = _new_architect()._parse_response(raw)
    assert verdict is ReviewVerdict.REQUEST_CHANGES, (
        "Truncated JSON must produce REQUEST_CHANGES so the gate "
        "refuses to merge a review the runner couldn't parse"
    )
    assert "parse_failed" in summary
    assert "TRUNCATED" in summary


def test_parse_response_malformed_no_braces_returns_request_changes() -> None:
    """No JSON at all in the response -> REQUEST_CHANGES (loud refusal)."""
    raw = "I refuse to produce structured output today, sorry."
    verdict, summary, _ = _new_architect()._parse_response(raw)
    assert verdict is ReviewVerdict.REQUEST_CHANGES
    assert "parse_failed" in summary
    assert "MALFORMED" in summary


def test_unknown_verdict_fails_loud_not_comment() -> None:
    """SP-CLAIM-TYPE-PARTITION-ALIGN: an unparseable ``verdict`` value

    (e.g. a typo like ``"BLOCK"``) must fail LOUD — a
    ``[parse_failed:unknown_verdict:…]`` marker plus ``REQUEST_CHANGES``
    — never the historic silent coercion to non-blocking ``COMMENT``
    that let a mistyped verdict merge with no trace.
    """
    raw = '{"verdict": "BLOCK", "summary": "iffy", "comments": []}'
    verdict, summary, _ = _new_architect()._parse_response(raw)
    assert verdict is ReviewVerdict.REQUEST_CHANGES
    assert verdict is not ReviewVerdict.COMMENT
    assert "parse_failed" in summary
    assert "unknown_verdict" in summary
    assert "BLOCK" in summary
