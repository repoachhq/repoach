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
from repoach.gates.config import GatesConfig
from repoach.review import owner_commands
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
    record_review_integrity,
)
from repoach.review.head_reverify import reverify_older_findings
from repoach.review.merge_gate import (
    compute_merge_decision,
    summarise_ledger_facts,
    verdict_from_facts,
)
from repoach.review.owner_commands import (
    REASON_MAX_CHARS,
    REASON_MIN_CHARS,
    OwnerVerb,
    apply_owner_commands,
    parse_command,
)
from repoach.review.owner_store import fetch_owner_commands
from repoach.review.report import render_ledger_report
from repoach.review.reviewer import ReviewVerdict

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


def test_unknown_finding_is_answered_and_ignored(db: Path) -> None:
    """An id of no finding of the pull request, or a PR comment naming none, is refused."""
    _finding(db)
    forge = FakeForge(
        conversation=[
            _pr_comment(32, f"/resolve #999 {REASON}"),
            _pr_comment(33, f"/resolve {REASON}"),
        ],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert [row.outcome for row in run.handled] == ["unknown_finding", "unknown_finding"]
    assert "no finding #999" in forge.posted[0]
    assert "must name the finding" in forge.posted[1]


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


def test_ambiguous_thread_asks_for_the_id(db: Path) -> None:
    """A thread anchoring two open findings settles neither and asks for ``#<id>``."""
    first = _finding(db)
    second = _finding(db, claim_type=ClaimType.DESIGN)
    forge = FakeForge(
        inline=[_root(), _reply(12, f"/resolve {REASON}")], maintainers={"jwfaye": True}
    )
    _apply(forge, db)
    assert _row(db, first).status is FindingStatus.VERIFIED
    assert _row(db, second).status is FindingStatus.VERIFIED
    assert f"#{first}, #{second}" in forge.replies[0][1]


def test_resolve_on_a_settled_finding_is_a_noop_with_one_reply(db: Path) -> None:
    """An advisory or already-settled finding is left as it is, with one reply saying so."""
    advisory = _finding(db, severity=Severity.ADVISORY)
    refuted = _finding(db, file="ci/x.sh", status=FindingStatus.REFUTED)
    forge = FakeForge(
        conversation=[
            _pr_comment(36, f"/resolve #{advisory} {REASON}"),
            _pr_comment(37, f"/resolve #{refuted} {REASON}"),
        ],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert [row.outcome for row in run.handled] == ["noop", "noop"]
    assert _row(db, advisory).status is FindingStatus.VERIFIED
    assert _row(db, refuted).status is FindingStatus.REFUTED
    assert "not an open blocking finding" in forge.posted[0]
    assert len(forge.posted) == 2


def test_owner_resolved_is_settled_and_not_counted(db: Path) -> None:
    """The ledger facts no longer count an owner-resolved finding: the verdict flips."""
    target = _finding(db)
    record_review_integrity(db, pr_number=PR, head_sha=HEAD, n_reviewers=4, n_unparsed=0)
    before = summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD)
    assert before.open_blocking_ids == [target]
    assert verdict_from_facts(before) is ReviewVerdict.REQUEST_CHANGES
    forge = FakeForge(
        conversation=[_pr_comment(38, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    _apply(forge, db)
    after = summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD)
    assert after.open_blocking_ids == []
    assert verdict_from_facts(after) is ReviewVerdict.APPROVE


def test_reverify_skips_an_owner_resolved_finding(db: Path, tmp_path: Path) -> None:
    """The re-check at head never re-judges an owner-resolved finding: no judge is built."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(39, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    _apply(forge, db)

    def no_judge() -> object:
        raise AssertionError("an owner-resolved finding must not be judged")

    result = reverify_older_findings(
        db,
        pr_number=PR,
        repo_root=tmp_path,
        head_sha=HEAD,
        ci_green=None,
        config=GatesConfig(),
        max_judged=10,
        judge_factory=no_judge,
    )
    assert result.outcomes == ()
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED


def test_reopen_counts_the_finding_again(db: Path) -> None:
    """``/reopen`` restores the status and verification it held, and the gate counts it again."""
    target = _finding(db)
    forge = FakeForge(
        inline=[_root(), _reply(13, f"/resolve {REASON}"), _reply(14, "/reopen")],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    row = _row(db, target)
    assert row.status is FindingStatus.VERIFIED
    assert row.verification_result == "needs judgement (evidence outside window)"
    assert row.checked_at_sha == OLD
    assert summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD).open_blocking_ids == [target]
    assert [r.resolution for r in run.handled] == ["owner_resolve", "owner_reopen"]
    assert run.settled == []
    assert "reopened by @jwfaye" in forge.replies[1][1]


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


def test_report_line_and_json_name_the_owner(db: Path) -> None:
    """One ``owner-resolved #<id> by <login>: <reason>`` line, its JSON entry, its report section."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(42, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    line = f"owner-resolved #{target} by jwfaye: {REASON}"
    assert run.lines()[0] == line
    assert "owner commands: 1 applied, 0 ignored" in run.lines()
    assert run.as_json() == [
        {
            "finding_id": target,
            "login": "jwfaye",
            "reason": REASON,
            "comment_id": 42,
            "head_sha": HEAD,
        }
    ]
    assert run.handled_json() == [
        {
            "comment_id": 42,
            "command": "resolve",
            "author": "jwfaye",
            "finding_id": target,
            "outcome": "applied",
        }
    ]
    facts = summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD)
    report = render_ledger_report(
        db,
        pr_number=PR,
        decision=compute_merge_decision(facts),
        facts=facts,
        owner_lines=[line],
    )
    assert "## Owner-resolved findings" in report
    assert line in report


def test_unreadable_comments_apply_nothing(db: Path) -> None:
    """A forge that cannot list the comments applies nothing and says why."""
    target = _finding(db)

    class Broken(FakeForge):
        def comments(self, number: int) -> list[Comment]:
            raise ForgeError("HTTP 503")

    run = _apply(Broken(), db)
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert run.lines() == ["owner commands: not read: HTTP 503"]


def test_non_owner_is_answered_once_per_login(db: Path) -> None:
    """Many commands from one non-maintainer get one answer; each is still recorded."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[
            _pr_comment(43, f"/resolve #{target} {REASON}", author="dev"),
            _pr_comment(44, f"/resolve #{target} {REASON} again", author="dev"),
            _pr_comment(45, "/reopen", author="dev"),
        ],
        maintainers={"dev": False},
    )
    _apply(forge, db)
    assert len(forge.posted) == 1
    assert [row.outcome for row in fetch_owner_commands(db, PR)] == ["not_owner"] * 3
    assert forge.permission_reads == ["dev"]


def test_failed_reply_stays_pending_and_is_retried(db: Path) -> None:
    """A reply the forge refuses is kept pending and posted by the next posting run, once."""
    target = _finding(db)

    class Flaky(FakeForge):
        failures: int = 1

        def post_comment(self, number: int, body: str, **kwargs: object) -> Comment:
            if self.failures:
                self.failures -= 1
                raise ForgeError("HTTP 502")
            return super().post_comment(number, body, **kwargs)

    forge = Flaky(
        conversation=[_pr_comment(46, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    first = _apply(forge, db)
    assert forge.posted == [] and len(first.unposted) == 1
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED
    _apply(forge, db)
    _apply(forge, db)
    assert len(forge.posted) == 1


def test_reopen_on_a_finding_not_owner_resolved_is_a_noop(db: Path) -> None:
    """``/reopen`` of a finding no owner settled changes nothing and says so."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[_pr_comment(47, f"/reopen #{target}")], maintainers={"jwfaye": True}
    )
    run = _apply(forge, db)
    assert [row.outcome for row in run.handled] == ["noop"]
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert "not owner-resolved" in forge.posted[0]


def test_reopen_without_recorded_resolve_falls_back_to_verified(db: Path) -> None:
    """An owner-resolved row with no recorded ``/resolve`` reopens to ``verified``, counted."""
    target = _finding(db, status=FindingStatus.OWNER_RESOLVED)
    forge = FakeForge(
        conversation=[_pr_comment(48, f"/reopen #{target}")], maintainers={"jwfaye": True}
    )
    run = _apply(forge, db)
    assert [row.resolution for row in run.handled] == ["owner_reopen"]
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD).open_blocking_ids == [target]


def test_reason_is_echoed_as_inert_code(db: Path) -> None:
    """The reason quoted in the bot's reply is a code span no backtick inside can close."""
    target = _finding(db)
    reason = "pinged @someone about `ptrace_scope` = 1 on the runner host, read back"
    forge = FakeForge(
        conversation=[_pr_comment(49, f"/resolve #{target} {reason}")],
        maintainers={"jwfaye": True},
    )
    _apply(forge, db)
    assert f"`` {reason} ``" in forge.posted[0]


def test_parse_command_cuts_reason_at_max_chars() -> None:
    """A long reason is cut, control bytes are blanked, and a glued suffix is no command."""
    parsed = parse_command("/resolve #5 " + "x" * (REASON_MAX_CHARS + 500))
    assert parsed is not None and len(parsed.reason) == REASON_MAX_CHARS
    escaped = parse_command("/resolve #5 runner host\x1b[31m checked\x07 by hand today")
    assert escaped is not None
    assert escaped.reason == "runner host [31m checked by hand today"
    assert all(ch.isprintable() for ch in escaped.reason)
    assert parse_command("/resolve#5 a reason long enough to count") is None
    assert parse_command("/resolve: a reason long enough to count") is None


def test_edited_comment_is_not_a_command(db: Path) -> None:
    """An edited comment is refused with one reply, whoever wrote it, and changes nothing."""
    target = _finding(db)
    edited = _pr_comment(50, f"/resolve #{target} {REASON}").model_copy(update={"edited": True})
    forge = FakeForge(conversation=[edited], maintainers={"jwfaye": True})
    run = _apply(forge, db)
    _apply(forge, db)
    assert [row.outcome for row in run.handled] == ["edited"]
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert forge.permission_reads == []
    assert len(forge.posted) == 1 and "edited comment" in forge.posted[0]


def test_resolve_refused_by_ledger_is_recorded_as_noop(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move the ledger refuses is recorded as a no-op and answered so."""
    target = _finding(db)
    monkeypatch.setattr(owner_commands, "update_finding_status", lambda *_a, **_k: False)
    forge = FakeForge(
        conversation=[_pr_comment(51, f"/resolve #{target} {REASON}")],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert [(row.outcome, row.resolution) for row in run.handled] == [("noop", "")]
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert "could not be moved" in forge.posted[0]


def test_thread_anchoring_no_finding_is_answered_and_ignored(db: Path) -> None:
    """A thread whose root cites no finding's file and line settles nothing, answered once."""
    target = _finding(db)
    forge = FakeForge(
        inline=[
            _root(60, path="docs/other.md", line=4),
            _reply(61, f"/resolve {REASON}", root=60),
        ],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert [row.outcome for row in run.handled] == ["unknown_finding"]
    assert _row(db, target).status is FindingStatus.VERIFIED
    assert forge.replies[0][0] == 60
    assert "anchors no finding" in forge.replies[0][1]


def test_resolve_reopen_resolve_ends_resolved_again(db: Path) -> None:
    """``/resolve``, ``/reopen``, ``/resolve``: owner-resolved again, by the last reason."""
    target = _finding(db)
    forge = FakeForge(
        conversation=[
            _pr_comment(52, f"/resolve #{target} {REASON}"),
            _pr_comment(53, f"/reopen #{target}"),
            _pr_comment(54, f"/resolve #{target} {REASON}, confirmed a second time"),
        ],
        maintainers={"jwfaye": True},
    )
    run = _apply(forge, db)
    assert [row.resolution for row in run.handled] == [
        "owner_resolve",
        "owner_reopen",
        "owner_resolve",
    ]
    assert _row(db, target).status is FindingStatus.OWNER_RESOLVED
    assert [s.comment_id for s in run.settled] == [54]
