"""SP-SAFE-MERGE-ARCHIVE-RETRY — archive fetch distinguishes failure modes.

The legacy :meth:`GhCli.fetch_archive_comment` collapsed three distinct
situations into a single ``None``: archive present, archive genuinely
absent, and the ``gh api`` call itself failing (secondary rate limit
right after the review team's posting burst).  ``safe_merge.sh`` step
[5/6] then reported ``verdict=UNREADABLE`` on unanimous-APPROVE PRs.
These tests pin the sibling :meth:`fetch_archive_comment_with_status`
(three-way outcome), the legacy delegation, the warning logs that make
API failures loud, and the CLI exit codes gating callers rely on
(``0`` found / ``1`` absent / ``6`` API failure).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from repoach.cli.review_cmds import review_app
from repoach.review.gh_client import ArchiveFetch, GhCli, GhResult

_MARKER = GhCli.ARCHIVE_MARKER
_ARCHIVE_BODY = f'{_MARKER}\n### archive\n```json\n{{"final_verdict": "APPROVE"}}\n```'


class _CannedGhCli(GhCli):
    """GhCli whose ``_run`` returns a single canned :class:`GhResult`."""

    def __init__(self, canned: GhResult) -> None:
        super().__init__()
        self._canned = canned

    def _run(self, args: list[str]) -> GhResult:
        return self._canned


def _result(returncode: int, stdout: str = "", stderr: str = "") -> GhResult:
    return GhResult(returncode=returncode, stdout=stdout, stderr=stderr, argv=["gh"])


def test_api_failure_sets_api_error_and_no_body() -> None:
    cli = _CannedGhCli(_result(1, stderr="HTTP 403: secondary rate limit"))
    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.body is None
    assert fetch.api_error is not None
    assert "rate limit" in fetch.api_error


def test_api_failure_without_stderr_reports_exit_code() -> None:
    cli = _CannedGhCli(_result(1))
    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.api_error == "gh exited 1"


def test_absent_marker_yields_both_none() -> None:
    cli = _CannedGhCli(_result(0, stdout='[[{"body": "unrelated comment"}]]'))
    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch == ArchiveFetch(body=None, api_error=None)


def test_found_marker_yields_body_without_error() -> None:
    import json

    bot_login = GhCli().bot_login
    stdout = json.dumps(
        [[{"body": "noise"}, {"body": _ARCHIVE_BODY, "user": {"login": bot_login}}]]
    )
    cli = _CannedGhCli(_result(0, stdout=stdout))
    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.api_error is None
    assert fetch.body is not None
    assert _MARKER in fetch.body


def test_decode_failure_sets_api_error() -> None:
    cli = _CannedGhCli(_result(0, stdout="[][]"))
    fetch = cli.fetch_archive_comment_with_status(42)
    assert fetch.body is None
    assert fetch.api_error is not None
    assert "decode" in fetch.api_error.lower()


def test_legacy_fetch_delegates_to_sibling() -> None:
    import json

    failing = _CannedGhCli(_result(1, stderr="HTTP 502"))
    assert failing.fetch_archive_comment(42) is None
    bot_login = GhCli().bot_login
    found = _CannedGhCli(
        _result(0, stdout=json.dumps([[{"body": _ARCHIVE_BODY, "user": {"login": bot_login}}]]))
    )
    assert found.fetch_archive_comment(42) == _ARCHIVE_BODY


def test_api_failure_logs_warning() -> None:
    fake_log = MagicMock()
    with patch("repoach.review.gh_client._log", fake_log):
        _CannedGhCli(_result(1, stderr="HTTP 403")).fetch_archive_comment_with_status(7)
    events = [call.args[0] for call in fake_log.warning.call_args_list]
    assert "gh_client.archive_fetch_api_failed" in events


def test_find_archive_api_failure_logs_warning() -> None:
    fake_log = MagicMock()
    with patch("repoach.review.gh_client._log", fake_log):
        _CannedGhCli(_result(1, stderr="HTTP 403")).find_archive_comment(7)
    events = [call.args[0] for call in fake_log.warning.call_args_list]
    assert "gh_client.find_archive_api_failed" in events


def _invoke_report_with(fetch: ArchiveFetch):
    runner = CliRunner()
    fake_cli = MagicMock()
    fake_cli.fetch_archive_comment_with_status.return_value = fetch
    fake_cli.ARCHIVE_MARKER = _MARKER
    with patch("repoach.cli.review_cmds.GhCli", return_value=fake_cli):
        return runner.invoke(review_app, ["report", "42"])


def test_cli_report_exit_6_on_api_failure() -> None:
    result = _invoke_report_with(ArchiveFetch(body=None, api_error="HTTP 403"))
    assert result.exit_code == 6
    assert "gh API error" in result.stderr


def test_cli_report_exit_1_on_genuinely_absent_archive() -> None:
    result = _invoke_report_with(ArchiveFetch(body=None, api_error=None))
    assert result.exit_code == 1
    assert "No archive comment" in result.stderr


def test_cli_report_exit_0_and_json_payload_on_found_archive() -> None:
    import json

    result = _invoke_report_with(ArchiveFetch(body=_ARCHIVE_BODY, api_error=None))
    assert result.exit_code == 0
    assert json.loads(result.stdout)["final_verdict"] == "APPROVE"
