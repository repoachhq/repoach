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
from unittest.mock import MagicMock, patch

import pytest

from repoach.review.gh_client import ArchiveFetch, GhCli, GhResult, flatten_paginated_pages

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
    """Build a canned ``GhResult`` shaped like a ``--paginate --slurp`` reply.

    A single-page response ``--slurp``s to a one-element outer array of
    page arrays, so *comments* is wrapped once here rather than emitted
    as a bare list.
    """
    return GhResult(returncode=0, stdout=json.dumps([comments]), stderr="", argv=["gh"])


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


class _PagedUpsertGhCli(GhCli):
    """``GhCli`` returning *get_result* for reads, recording every write argv.

    Lets a test drive :meth:`GhCli.upsert_archive_comment` (which reads
    via :meth:`GhCli.find_archive_comment` before deciding POST vs
    PATCH) while observing which write verb it picked, without a real
    ``gh`` subprocess.
    """

    def __init__(self, get_result: GhResult, *, bot_login: str) -> None:
        super().__init__(bot_login=bot_login)
        self._get_result = get_result
        self.write_calls: list[list[str]] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if "--method" in args:
            self.write_calls.append(args)
            return GhResult(returncode=0, stdout=json.dumps({"id": 999}), stderr="", argv=args)
        return self._get_result


def _two_page_archive_payload() -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """A realistic two-page ``--paginate --slurp`` archive-comments payload.

    The archive marker sits on page 2 (AC2's discriminating shape) —
    pre-fix code that ``json.loads``\\ s the slurped array-of-arrays
    directly walks a list of *pages*, not comments, so it never sees a
    ``dict`` and never finds the marker.
    """
    marker_body = f'{_MARKER}\n### archive\n```json\n{{"final_verdict": "APPROVE"}}\n```'
    page1 = [{"id": 101, "body": "first page discussion", "user": {"login": "someone"}}]
    page2 = [{"id": 202, "body": marker_body, "user": {"login": _BOT_LOGIN}}]
    return page1, page2, marker_body


def test_paginated_array_endpoints_parse_all_pages() -> None:
    """AC1 + AC2: the flatten helper, and every caller built on it, merge every page.

    Reverting the ``gh_client.py`` change (back to a bare
    ``json.loads(res.stdout)`` with no flatten) makes this fail: with a
    genuine two-page ``--slurp`` payload the pre-fix code parses the
    JSON successfully (it is well-formed) but treats each *page* as a
    top-level entry instead of flattening — ``list_review_comments``
    returns the two page-lists unmerged, and
    ``find_archive_comment`` / ``fetch_archive_comment_with_status``
    never see a comment ``dict`` to match the marker against, so the
    page-2 archive is never found.
    """
    page1_root = [{"id": 1, "body": "a"}]
    page2_root = [{"id": 2, "body": "b"}]
    assert flatten_paginated_pages([page1_root, page2_root]) == page1_root + page2_root
    assert flatten_paginated_pages([[], []]) == []
    assert flatten_paginated_pages([]) == []

    page1, page2, marker_body = _two_page_archive_payload()
    slurped_stdout = json.dumps([page1, page2])
    canned = GhResult(returncode=0, stdout=slurped_stdout, stderr="", argv=["gh"])

    cli = _CannedGhCli(canned, bot_login=_BOT_LOGIN)
    assert cli.list_review_comments(42) == page1 + page2
    assert cli.find_archive_comment(42) == 202

    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.api_error is None
    assert fetch.body == marker_body

    upserter = _PagedUpsertGhCli(canned, bot_login=_BOT_LOGIN)
    upserter.upsert_archive_comment(42, body=marker_body)
    assert len(upserter.write_calls) == 1, "expected exactly one write call, not a duplicate POST"
    method_index = upserter.write_calls[0].index("--method")
    assert upserter.write_calls[0][method_index + 1] == "PATCH", (
        "the existing archive comment (found on page 2) must be PATCHed, "
        "never re-POSTed as a duplicate"
    )


def test_malformed_paginated_output_surfaces_loudly() -> None:
    """AC1 + AC2: a genuine parse failure is loud, not a silent empty/None.

    Covers both the helper directly (non-JSON-array-of-arrays shapes)
    and every caller, asserting each logs its own structured warning
    and fails closed rather than returning as if the PR simply had no
    comments.
    """
    with pytest.raises(ValueError):
        flatten_paginated_pages({"not": "a list of pages"})
    with pytest.raises(ValueError):
        flatten_paginated_pages([{"a": "page must be a list, not a dict"}])

    malformed = GhResult(returncode=0, stdout="[][]", stderr="", argv=["gh"])

    fake_log = MagicMock()
    with patch("repoach.review.gh_client._log", fake_log):
        cli = _CannedGhCli(malformed, bot_login=_BOT_LOGIN)
        assert cli.list_review_comments(42) == []
        assert cli.find_archive_comment(42) is None
        fetch = cli.fetch_archive_comment_with_status(42)
        assert fetch.body is None
        assert fetch.api_error is not None
        assert "decode" in fetch.api_error.lower()

    events = [call.args[0] for call in fake_log.warning.call_args_list]
    assert "gh_client.review_comments_decode_failed" in events
    assert "gh_client.find_archive_decode_failed" in events
    assert "gh_client.archive_body_decode_failed" in events
