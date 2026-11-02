"""SP-FINDING-THREAD-IDS AC5, AC8: ``review backfill-threads`` links legacy findings strictly.

A finding posted before ``finding_posts`` existed is linked only to a
bot root at its exact file and line that no other finding claims
(``backfill_line_tolerance = 0``). Everything else stays unmatched and
unknown: no row, and :func:`resolve_thread` returns ``None``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.forge.protocol import Comment, ForgeError
from repoach.review.finding_posts import (
    backfill_finding_posts,
    fetch_finding_posts,
    resolve_thread,
)
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)

PR = 7
BOT = "repoach-bot"


class Forge:
    """The bot's inline comments on PR 7, read through the protocol's ``comments``."""

    name = "github"
    bot_username = BOT

    def __init__(self, comments: list[Comment], *, fail: bool = False) -> None:
        self._comments = comments
        self._fail = fail

    def comments(self, number: int) -> list[Comment]:
        if self._fail:
            raise ForgeError("HTTP 503")
        return self._comments


def _finding(db: Path, *, file: str = "src/x.py", line: int = 10) -> Finding:
    init_findings_schema(db)
    finding = Finding(
        pr_number=PR,
        head_sha="abc",
        round=1,
        finder="sentinel",
        claim_type=ClaimType.SPEC_GAP,
        severity=Severity.BLOCKING,
        file=file,
        line_start=line,
        line_end=line,
        claim="a claim",
        evidence_pointer=f"{file}:{line}",
        status=FindingStatus.VERIFIED,
    )
    return finding.model_copy(update={"id": record_finding(db, finding)})


def _root(comment_id: int, line: int, *, author: str = BOT, path: str = "src/x.py") -> Comment:
    return Comment(id=comment_id, author=author, path=path, line=line)


def test_backfill_is_strict_and_leaves_unmatched_unknown(tmp_path: Path) -> None:
    """AC8: a root two lines away, or two roots at the line, link nothing."""
    db = tmp_path / "shifted.db"
    finding = _finding(db)
    report = backfill_finding_posts(db, Forge([_root(500, 12)]), PR, bot_login=BOT)
    assert (report.unmatched, report.linked) == (1, 0)
    assert fetch_finding_posts(db, PR) == []
    assert resolve_thread(db, finding) is None

    db = tmp_path / "twice.db"
    finding = _finding(db)
    report = backfill_finding_posts(db, Forge([_root(500, 10), _root(501, 10)]), PR, bot_login=BOT)
    assert (report.unmatched, report.linked) == (1, 0)
    assert fetch_finding_posts(db, PR) == []
    assert resolve_thread(db, finding) is None


def test_backfill_links_one_exact_bot_root(tmp_path: Path) -> None:
    """One bot root at the exact line links; a person's root and a reply are ignored."""
    db = tmp_path / "review.db"
    finding = _finding(db)
    comments = [
        _root(500, 10),
        _root(501, 10, author="someone"),
        Comment(id=502, author=BOT, path="src/x.py", line=10, in_reply_to=500),
    ]

    report = backfill_finding_posts(db, Forge(comments), PR, bot_login=BOT)

    assert report.line() == "pr 7: 1 findings, 1 linked, 0 unmatched, 0 already linked"
    post = resolve_thread(db, finding)
    assert post is not None
    assert (post.comment_id, post.thread_id, post.provenance) == ("500", "500", "backfill")
    again = backfill_finding_posts(db, Forge(comments), PR, bot_login=BOT)
    assert (again.linked, again.already_linked) == (0, 1)


def test_two_findings_on_one_root_stay_unmatched(tmp_path: Path) -> None:
    """A root claimed by two findings links neither."""
    db = tmp_path / "review.db"
    _finding(db)
    _finding(db)
    report = backfill_finding_posts(db, Forge([_root(500, 10)]), PR, bot_login=BOT)
    assert (report.findings, report.linked, report.unmatched) == (2, 0, 2)


def test_backfill_threads_cli_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: the dry run prints the counts and leaves the table unchanged; the real run grows it."""
    db = tmp_path / "review.db"
    _finding(db)
    _finding(db, line=20)
    forge = Forge([_root(500, 10)])
    monkeypatch.setenv("REPOACH_DB_PATH", str(db))
    monkeypatch.setattr("repoach.core.config._settings", None)
    monkeypatch.setattr(review_cmds, "select_forge", lambda: forge)
    monkeypatch.chdir(Path(__file__).resolve().parents[2])

    dry = CliRunner().invoke(review_cmds.review_app, ["backfill-threads", "7", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert dry.stdout.strip() == "pr 7: 2 findings, 1 linked, 1 unmatched, 0 already linked"
    assert fetch_finding_posts(db, PR) == []

    real = CliRunner().invoke(review_cmds.review_app, ["backfill-threads", "7"])
    assert real.exit_code == 0, real.output
    assert len(fetch_finding_posts(db, PR)) == 1
    assert len(fetch_findings(db, PR)) == 2


def test_backfill_threads_cli_unreachable_forge_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forge that cannot list the comments ends with rc 2 and writes nothing."""
    db = tmp_path / "review.db"
    _finding(db)
    monkeypatch.setenv("REPOACH_DB_PATH", str(db))
    monkeypatch.setattr("repoach.core.config._settings", None)
    monkeypatch.setattr(review_cmds, "select_forge", lambda: Forge([], fail=True))
    monkeypatch.chdir(Path(__file__).resolve().parents[2])

    result = CliRunner().invoke(review_cmds.review_app, ["backfill-threads", "7"])

    assert result.exit_code == 2
    assert fetch_finding_posts(db, PR) == []
