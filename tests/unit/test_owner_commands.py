"""SP-OWNER-SETTLES-A-FINDING: a maintainer settles a finding with ``/resolve`` and ``/reopen``.

A fake forge serves the inline and conversation comments and answers
``is_maintainer`` from a table; the ledger is a real SQLite file. The
tests pin the parsing, the owner rule, the ledger effect the gate and
the re-check at head read, replay idempotence and the report lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from repoach.forge.protocol import Comment, ForgeError
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.owner_commands import (
    REASON_MIN_CHARS,
    OwnerVerb,
    apply_owner_commands,
    parse_command,
)
from repoach.review.owner_store import fetch_owner_commands

PR = 339
OLD = "a" * 40
HEAD = "b" * 40
REASON = "environment review-gate is protected: deploy access Maintainers (API read back)"


@dataclass
class FakeForge:
    """The forge seams owner commands use, with every write recorded."""

    inline: list[Comment] = field(default_factory=list)
    conversation: list[Comment] = field(default_factory=list)
    maintainers: dict[str, bool] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)
    bot_username: str = "repoach-bot"
    replies: list[tuple[int, str]] = field(default_factory=list)
    posted: list[str] = field(default_factory=list)
    permission_reads: list[str] = field(default_factory=list)

    def comments(self, number: int) -> list[Comment]:
        """Inline comments, roots and replies."""
        return list(self.inline)

    def conversation_comments(self, number: int) -> list[Comment]:
        """Pull-request comments."""
        return list(self.conversation)

    def is_maintainer(self, login: str) -> bool:
        """Answer from the table; raise for an unreadable login."""
        self.permission_reads.append(login)
        if login in self.unreadable:
            raise ForgeError(f"HTTP 502 reading {login}")
        return self.maintainers.get(login, False)

    def reply(self, number: int, comment_id: int, body: str) -> Comment:
        """Record a thread reply."""
        self.replies.append((comment_id, body))
        return Comment(id=900 + len(self.replies), author=self.bot_username, body=body)

    def post_comment(self, number: int, body: str, **_kwargs: object) -> Comment:
        """Record a pull-request comment."""
        self.posted.append(body)
        return Comment(id=800 + len(self.posted), author=self.bot_username, body=body)


def _finding(
    db: Path,
    *,
    file: str = "ci/review-gate.sh",
    line: int = 12,
    head: str = OLD,
    severity: Severity = Severity.BLOCKING,
    status: FindingStatus = FindingStatus.VERIFIED,
    claim_type: ClaimType = ClaimType.SECURITY,
) -> int:
    return record_finding(
        db,
        Finding(
            pr_number=PR,
            head_sha=head,
            round=1,
            finder="sentinel",
            claim_type=claim_type,
            severity=severity,
            file=file,
            line_start=line,
            line_end=line,
            claim="the review-gate environment is not protected",
            evidence_pointer=f"{file}:{line}",
            status=status,
            verification_method="refuter",
            verification_result="needs judgement (evidence outside window)",
            checked_at_sha=head,
        ),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "review.db"
    init_findings_schema(path)
    return path


def _row(db: Path, finding_id: int) -> Finding:
    return next(f for f in fetch_findings(db, PR) if f.id == finding_id)


def _root(comment_id: int = 10, *, path: str = "ci/review-gate.sh", line: int = 12) -> Comment:
    return Comment(id=comment_id, author="repoach-bot", body="blocking: …", path=path, line=line)


def _reply(comment_id: int, body: str, *, root: int = 10, author: str = "jwfaye") -> Comment:
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        path="ci/review-gate.sh",
        line=12,
        in_reply_to=root,
        created_at=f"2026-11-01T10:00:{comment_id % 60:02d}Z",
    )


def _pr_comment(comment_id: int, body: str, *, author: str = "jwfaye") -> Comment:
    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created_at=f"2026-11-01T11:00:{comment_id % 60:02d}Z",
    )


def _apply(forge: FakeForge, db: Path, *, post: bool = True, head: str = HEAD):
    return apply_owner_commands(forge, db, pr_number=PR, head_sha=head, post=post)


def test_parse_command_reads_verb_id_and_reason() -> None:
    """The verb, an optional ``#id`` and the collapsed reason; anything else is no command."""
    parsed = parse_command(f"/resolve #14   {REASON}\n second line")
    assert parsed is not None
    assert (parsed.verb, parsed.finding_id) == (OwnerVerb.RESOLVE, 14)
    assert parsed.reason == f"{REASON} second line"
    reopen = parse_command("  /reopen  ")
    assert reopen is not None and reopen.verb is OwnerVerb.REOPEN and reopen.finding_id is None
    assert parse_command("/resolved this") is None
    assert parse_command("please /resolve this") is None
    assert parse_command("LGTM") is None


def test_github_and_gitlab_maintainers_are_owners_and_the_bot_never_is(db: Path) -> None:
    """Ownership comes from the forge; the bot's own command is recorded and never applied."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(20, f"/resolve #{target} {REASON}", author="repoach-bot")],
        maintainers={"repoach-bot": True},
    )
    run = _apply(forge, db)
    assert forge.permission_reads == []
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert [row.outcome for row in run.handled] == ["bot"]
    assert forge.posted == [] and forge.replies == []


def test_thread_reply_resolves_the_finding_it_anchors(db: Path) -> None:
    """``/resolve <reason>`` in the finding's thread settles it and answers in that thread."""
    target = _finding(db)
    _finding(db, file="ci/other.sh", line=3)
    forge = FakeForge(
        inline=[_root(), _reply(11, f"/resolve {REASON}")], maintainers={"jwfaye": True}
    )
    run = _apply(forge, db)
    row = _row(db, target)
    assert row.status is FindingStatus.OWNER_RESOLVED
    assert row.verification_method == "owner_resolve"
    assert row.verification_result == f"owner-resolved by jwfaye: {REASON}"
    assert row.checked_at_sha == HEAD
    stored = fetch_owner_commands(db, PR)
    assert [(s.comment_id, s.finding_id, s.resolution, s.author) for s in stored] == [
        (11, target, "owner_resolve", "jwfaye")
    ]
    assert stored[0].reason == REASON and stored[0].head_sha == HEAD
    assert [comment_id for comment_id, _ in forge.replies] == [10]
    assert f"Finding #{target} owner-resolved by @jwfaye" in forge.replies[0][1]
    assert run.unposted == []


def test_pr_level_command_names_the_finding(db: Path) -> None:
    """A finding never posted is settled by ``/resolve #<id> <reason>`` on the pull request."""
    target = _finding(db, file="ci/review-gate.sh", line=0)
    forge = FakeForge(
        conversation=[_pr_comment(30, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    _apply(forge, db)
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED
    assert len(forge.posted) == 1 and forge.replies == []


def test_resolve_without_reason_is_answered_and_ignored(db: Path) -> None:
    """A reason under the minimum is refused with one reply naming the rule."""
    target = _finding(db)
    short = "x" * (REASON_MIN_CHARS - 1)
    forge = FakeForge(
        conversation=[_pr_comment(31, f"/resolve #{target} {short}")],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert [row.outcome for row in run.handled] == ["no_reason"]
    assert len(forge.posted) == 1
    assert f"at least {REASON_MIN_CHARS} characters" in forge.posted[0]


def test_non_owner_is_answered_once_and_ignored(db: Path) -> None:
    """A command from someone the forge does not grant maintainer is ignored, answered once."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(34, f"/resolve #{target} {REASON}", author="dev")],
        maintainers={"dev": False},
    )
    _apply(forge, db)
    _apply(forge, db)
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert forge.posted == [
        "Ignored: @dev is not a maintainer of this repository; only a maintainer's "
        "`/resolve` or `/reopen` settles a finding."
    ]


def test_unreadable_permission_applies_nothing_and_is_retried(db: Path) -> None:
    """A permission the forge cannot read applies nothing, records nothing, answers nothing."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(35, f"/resolve #{target} {REASON}")], unreadable={"jwfaye"}
    )
    run = _apply(forge, db)
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert run.unreadable == ["jwfaye"] and fetch_owner_commands(db, PR) == []
    forge.unreadable.clear()
    forge.maintainers["jwfaye"] = True
    _apply(forge, db)
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED


def test_a_command_is_applied_once_across_runs(db: Path) -> None:
    """A re-run at the same head, then at a later head, neither re-applies nor re-answers."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(40, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    _apply(forge, db)
    second = _apply(forge, db)
    third = _apply(forge, db, head="c" * 40)
    assert second.handled == [] and third.handled == []
    assert len(forge.posted) == 1
    assert len(fetch_owner_commands(db, PR)) == 1
    assert fetch_owner_commands(db, PR)[0].head_sha == HEAD
    assert [s.finding_id for s in third.settled] == [target]


def test_dry_run_prints_replies_and_a_posting_run_posts_them_once(db: Path) -> None:
    """A dry run applies the command, posts nothing, and leaves the answer to the next posting run."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(41, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    dry = _apply(forge, db, post=False)
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED
    assert forge.posted == [] and len(dry.unposted) == 1
    assert any(line.startswith("owner reply not posted: ") for line in dry.lines())
    _apply(forge, db, post=True)
    _apply(forge, db, post=True)
    assert len(forge.posted) == 1


def test_unreadable_comments_apply_nothing(db: Path) -> None:
    """A forge that cannot list the comments applies nothing and says why."""
    target = _finding(db)

    class Broken(FakeForge):
        def comments(self, number: int) -> list[Comment]:
            raise ForgeError("HTTP 503")

    run = _apply(Broken(), db)
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert run.lines() == ["owner commands: not read: HTTP 503"]
