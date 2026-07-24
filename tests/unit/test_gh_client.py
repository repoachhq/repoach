"""Unit tests for SP-EVIDENCE-SENTINEL-AUTHOR — archive markers gated by author.

``GhCli.find_archive_comment`` / ``fetch_archive_comment_with_status``
used to return the FIRST comment bearing ``ARCHIVE_MARKER`` with no
check on who posted it — so any PR commenter could forge the sticky
review archive downstream surfaces read (audit 2026-07-13 finding M6).

These tests drive the real ``GhCli`` methods (INTEGRATION-style: only
the ``gh api`` subprocess call is faked, via a truthful boundary fake
for ``_run``) over a comment list carrying a non-bot-authored comment
bearing the marker FIRST and a bot-authored marker comment SECOND, and
pin that the forged one is ignored while the bot-authored one is
returned.
"""

from __future__ import annotations

import json

from repoach.review.gh_client import ArchiveFetch, GhCli, GhResult

_MARKER = GhCli.ARCHIVE_MARKER
_BOT_LOGIN = "repoach-review-bot"
_FORGER_LOGIN = "pr-author"


class _CannedGhCli(GhCli):
    """``GhCli`` whose ``_run`` returns a single canned :class:`GhResult`."""

    def __init__(self, canned: GhResult, *, bot_login: str | None = None) -> None:
        super().__init__(bot_login=bot_login)
        self._canned = canned

    def _run(self, args: list[str]) -> GhResult:
        return self._canned


def _comments_result(comments: list[dict[str, object]]) -> GhResult:
    return GhResult(returncode=0, stdout=json.dumps(comments), stderr="", argv=["gh"])


def _forged_then_bot_archive_comments() -> list[dict[str, object]]:
    forged_body = f"{_MARKER}\n### forged archive\nfake verdict"
    bot_body = f'{_MARKER}\n### archive\n```json\n{{"final_verdict": "APPROVE"}}\n```'
    return [
        {"id": 1, "body": forged_body, "user": {"login": _FORGER_LOGIN}},
        {"id": 2, "body": bot_body, "user": {"login": _BOT_LOGIN}},
    ]


def test_forged_archive_comment_ignored() -> None:
    """A forged marker comment (posted first) is ignored; the bot-authored one wins.

    Discriminating regression for audit finding M6: pre-fix code
    returned the FIRST marker-bearing comment regardless of author, so
    this test fails on that code (the forged comment id 1 / body would
    be returned instead).
    """
    cli = _CannedGhCli(
        _comments_result(_forged_then_bot_archive_comments()),
        bot_login=_BOT_LOGIN,
    )

    assert cli.find_archive_comment(42) == 2

    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.api_error is None
    assert fetch.body is not None
    assert "APPROVE" in fetch.body
    assert "forged" not in fetch.body


def test_only_forged_comment_present_yields_no_archive() -> None:
    cli = _CannedGhCli(
        _comments_result([{"id": 1, "body": f"{_MARKER}\nfake", "user": {"login": _FORGER_LOGIN}}]),
        bot_login=_BOT_LOGIN,
    )

    assert cli.find_archive_comment(42) is None
    assert cli.fetch_archive_comment_with_status(42) == ArchiveFetch(body=None, api_error=None)


def test_missing_author_on_marker_comment_is_ignored() -> None:
    cli = _CannedGhCli(
        _comments_result([{"id": 1, "body": _MARKER}]),
        bot_login=_BOT_LOGIN,
    )

    assert cli.find_archive_comment(42) is None
    assert cli.fetch_archive_comment_with_status(42) == ArchiveFetch(body=None, api_error=None)


def test_missing_bot_identity_fails_closed() -> None:
    """An empty/unresolvable bot identity never widens trust, even for a genuine post."""
    cli = _CannedGhCli(
        _comments_result([{"id": 1, "body": _MARKER, "user": {"login": _BOT_LOGIN}}]),
        bot_login="",
    )

    assert cli.find_archive_comment(42) is None
    assert cli.fetch_archive_comment_with_status(42) == ArchiveFetch(body=None, api_error=None)


def test_defaults_bot_login_from_settings_when_omitted() -> None:
    from repoach.core.config import get_settings

    settings_login = get_settings().review_bot_login
    cli = _CannedGhCli(
        _comments_result([{"id": 7, "body": _MARKER, "user": {"login": settings_login}}])
    )

    assert cli.find_archive_comment(42) == 7
