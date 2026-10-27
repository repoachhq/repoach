"""SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS 0.5 — a published error carries no secret."""

from __future__ import annotations

import pytest

from repoach.core.redaction import SECRET_VALUE_PATTERNS, redact_error

_TOKENS = {
    "gitlab runner": "glrt-" + "a1" * 6,
    "gitlab personal": "glpat-" + "b2" * 6,
    "gitlab registration": "GR1348941" + "c3" * 6,
    "github classic": "ghp_" + "d4" * 11,
    "github server": "ghs_" + "e5" * 11,
    "github fine-grained": "github_pat_" + "f6" * 11,
    "email": "bot.user@example.org",
}


def test_every_secret_pattern_has_a_case() -> None:
    covered = {
        pattern.pattern
        for pattern in SECRET_VALUE_PATTERNS
        for value in _TOKENS.values()
        if pattern.search(value)
    }
    assert covered == {pattern.pattern for pattern in SECRET_VALUE_PATTERNS}


@pytest.mark.parametrize("value", list(_TOKENS.values()), ids=list(_TOKENS))
def test_redact_error_masks_each_secret_pattern(value: str) -> None:
    redacted = redact_error(f"push refused for {value} (403)")
    assert value not in redacted
    assert redacted == "push refused for *** (403)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fatal: https://bot:token@github.com/o/r", "fatal: https://***@github.com/o/r"),
        (
            "fatal: https://bot:p@ss@github.com/o/r failed",
            "fatal: https://***@github.com/o/r failed",
        ),
        (
            "fatal: https://bot:pa/ss@github.com/o/r failed",
            "fatal: https://***@github.com/o/r failed",
        ),
        ("fatal: https://bot@github.com/o/r", "fatal: https://***@github.com/o/r"),
        (
            "from https://a:x@h1/p to https://b:y@h2/q",
            "from https://***@h1/p to https://***@h2/q",
        ),
        (
            "remotes https://a:x@h1/p,https://b:y@h2/q",
            "remotes https://***@h1/p,https://***@h2/q",
        ),
    ],
    ids=[
        "user-and-secret",
        "unencoded-at",
        "slash-in-secret",
        "user-only",
        "two-urls-spaced",
        "two-urls-joined",
    ],
)
def test_redact_error_masks_url_userinfo(text: str, expected: str) -> None:
    assert redact_error(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "git show failed: no such path docs/plans/SP-X.md",
        "see https://github.com/o/r and retry",
    ],
    ids=["empty", "plain", "url-without-userinfo"],
)
def test_redact_error_leaves_clean_text_alone(text: str) -> None:
    assert redact_error(text) == text
