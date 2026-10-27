"""Secret-shaped values never published: forge tokens, emails, URL credentials.

One home for the patterns the forge fixture recorder refuses
(:mod:`repoach.forge.recording`) and for :func:`redact_error`, which the
spec gate applies to an error it publishes as a merge reason
(SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS 0.5), so neither module
depends on the other for it.
"""

from __future__ import annotations

import re

SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"glrt-[A-Za-z0-9_-]{10,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{10,}"),
    re.compile(r"GR1348941[A-Za-z0-9_-]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(
        r"(?<![\\\w])[A-Za-z0-9][A-Za-z0-9._%+-]*[A-Za-z0-9]@(?!incoming\.)[A-Za-z0-9-]+\.[a-z]{2,}"
    ),
)
"""Values that must never appear in a fixture: forge tokens and email addresses."""

_URL_USERINFO = re.compile(r"(?<=://)[^/\s]+@")
"""The ``user[:secret]@`` part of a URL, up to the last ``@`` before the host (a
secret may hold an unencoded ``@``), which a failed fetch may echo."""


def redact_error(text: str) -> str:
    """Return *text* with URL credentials and forge tokens replaced by ``***``.

    A grading error becomes a merge reason that the gate publishes (its
    JSON, notes and PR comment) and a log line; a failed ``git fetch``
    can carry a credentialed remote URL.

    Args:
        text: The error text.

    Returns:
        The text with every secret-shaped value replaced.
    """
    cleaned = _URL_USERINFO.sub("***@", text)
    for pattern in SECRET_VALUE_PATTERNS:
        cleaned = pattern.sub("***", cleaned)
    return cleaned
