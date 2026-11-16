"""SP-REVIEW-NITS-ONE-COMMENT: nits go to one digest comment, minor and worse open a thread.

The review's posting path runs against :class:`MemoryForge`, an in-memory
forge that keeps inline comments (each one a resolvable thread) apart
from conversation comments (the digest, the archive), so every test
reads the forge's comment lists as the oracle.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from repoach.forge.protocol import (
    ARCHIVE_MARKER,
    Comment,
    ForgeError,
    ForgeWriteBarrier,
    PullRequest,
    Review,
    ReviewDraft,
    ReviewThread,
    carries_marker,
    marked_body,
)
from repoach.review import orchestrator
from repoach.review.finding_post_threads import backfill_finding_posts
from repoach.review.finding_posts import (
    PROVENANCE_DIGEST,
    FindingPost,
    fetch_finding_posts,
)
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    is_valid_transition,
)
from repoach.review.findings_bridge import (
    opens_thread,
    record_findings_for_outcomes,
    severity_of,
)
from repoach.review.nit_digest import (
    NIT_DIGEST_MARKER,
    NIT_DIGEST_MAX_ITEMS,
    NitDigest,
    NitItem,
    collect_nits,
    digest_for_pr,
    post_or_edit_digest,
    render_digest,
)
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

PR = 7
HEAD_A = "a" * 40
HEAD_B = "b" * 40
PR_URL = "https://forge.example.invalid/acme/app/pull/7"


class MemoryForge:
    """A forge in memory: inline comments are threads, conversation comments are not.

    Attributes:
        name: ``github`` or ``gitlab``.
        inline: Every positioned comment posted (each opens a thread).
        conversation: Every conversation comment, by id.
        upserts: How many times the nit digest comment was written.
        refuse_edits: When set, editing an existing marked comment raises.
    """

    bot_username = "bot"

    def __init__(self, name: str = "github") -> None:
        self.name = name
        self.barrier = ForgeWriteBarrier()
        self.inline: list[Comment] = []
        self.conversation: dict[int, Comment] = {}
        self.upserts = 0
        self.refuse_edits = False
        self._ids = itertools.count(5000)

    def pull_request(self, number: int) -> PullRequest:
        return PullRequest(number=number, url=PR_URL)

    def post_review(self, number: int, review: ReviewDraft) -> Review:
        posted = [
            Comment(id=next(self._ids), author="bot", body=d.body, path=d.path, line=d.line)
            for d in review.comments
        ]
        self.inline.extend(posted)
        return Review(id=next(self._ids), author="bot", body=review.body, comments=posted)

    def post_comment(
        self,
        number: int,
        body: str,
        *,
        path: str | None = None,
        line: int | None = None,
        commit_sha: str | None = None,
    ) -> Comment:
        comment = Comment(id=next(self._ids), author="bot", body=body, path=path, line=line)
        if path is not None:
            self.inline.append(comment)
        else:
            self.conversation[comment.id] = comment
        return comment

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        for comment in self.conversation.values():
            if carries_marker(comment.body, marker) and comment.author == self.bot_username:
                return comment
        return None

    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        final_body = marked_body(marker, body)
        existing = self.find_marked_comment(number, marker)
        if existing is not None and self.refuse_edits:
            raise ForgeError("HTTP 403: note is locked")
        if marker == NIT_DIGEST_MARKER:
            self.upserts += 1
        if existing is None:
            return self.post_comment(number, final_body)
        edited = existing.model_copy(update={"body": final_body})
        self.conversation[existing.id] = edited
        return edited

    def comments(self, number: int) -> list[Comment]:
        return list(self.inline)

    def unresolved_threads(self, number: int) -> list[ReviewThread]:
        return [ReviewThread(root_id=c.id, path=c.path or "", line=c.line) for c in self.inline]

    def marked(self, marker: str = NIT_DIGEST_MARKER) -> list[Comment]:
        """The conversation comments ``marker`` opens."""
        return [c for c in self.conversation.values() if carries_marker(c.body, marker)]


def _outcome(role: BotRole, *comments: tuple[str, int, str, str]) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="ok",
        comments=[
            ReviewComment(file=path, line=line, severity=severity, body=body)
            for path, line, severity, body in comments
        ],
    )


def _record(db: Path, head: str, *outcomes: ReviewerOutcome) -> None:
    record_findings_for_outcomes(
        db, pr_number=PR, head_sha=head, outcomes=list(outcomes), round_n=1, diff=""
    )


def _nits(n: int, *, prefix: str = "rename") -> ReviewerOutcome:
    return _outcome(
        BotRole.SCRIBE,
        *[(f"src/m{i:02d}.py", 3, "nit", f"{prefix} variable number {i}") for i in range(n)],
    )


def _publish(
    forge: MemoryForge, db: Path, head: str, outcomes: list[ReviewerOutcome]
) -> TeamOutcome:
    """Run the posting path of a review at ``head`` on ``forge``."""
    orch = ReviewTeamOrchestrator(forge=forge, db_path=db, post_to_github=True, repo_root=db.parent)
    team = TeamOutcome(
        pr_number=PR, final_verdict=ReviewVerdict.APPROVE, n_blockers=0, n_majors=0, head_sha=head
    )
    team.nit_digest = digest_for_pr(db, PR, head_sha=head)
    orch._publish_all(
        pr_number=PR, outcomes=outcomes, head_sha=head, team=team, resolved_per_role={}
    )
    return team


@pytest.fixture(autouse=True)
def _no_routine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "fire_review_routine", lambda *_a, **_k: None)


def test_nit_is_recorded_as_note_and_never_posted_alone(tmp_path: Path) -> None:
    """Two nits and a minor: two noted findings, one thread, one digest listing both nits."""
    db = tmp_path / "review.db"
    outcome = _outcome(
        BotRole.ARCHITECT,
        ("src/a.py", 4, "nit", "prefer a named constant here"),
        ("src/b.py", 9, "nit", "the loop variable could be clearer"),
        ("src/c.py", 2, "minor", "this branch is never exercised"),
    )
    _record(db, HEAD_A, outcome)
    forge = MemoryForge()

    team = _publish(forge, db, HEAD_A, [outcome])

    rows = fetch_findings(db, PR)
    notes = [f for f in rows if f.severity is Severity.NOTE]
    assert [(f.file, f.status) for f in notes] == [
        ("src/a.py", FindingStatus.NOTED),
        ("src/b.py", FindingStatus.NOTED),
    ]
    assert [(c.path, c.line) for c in forge.inline] == [("src/c.py", 2)]
    [digest] = forge.marked()
    assert "`src/a.py:4` — architect — prefer a named constant here" in digest.body
    assert "`src/b.py:9` — architect — the loop variable could be clearer" in digest.body
    assert team.nits_json(with_body=False) == {
        "count": 2,
        "digest_url": f"{PR_URL}#issuecomment-{digest.id}",
        "error": None,
    }
    [archive] = forge.marked(ARCHIVE_MARKER)
    assert NIT_DIGEST_MARKER not in archive.body
    assert len(team.open_threads.threads) == 1


def test_one_digest_per_pr_edited_in_place_across_heads(tmp_path: Path) -> None:
    """Three nits at A, one at B: one marked comment, B's item under At, A's under Earlier."""
    db = tmp_path / "review.db"
    forge = MemoryForge()
    first = _nits(3)
    _record(db, HEAD_A, first)
    _publish(forge, db, HEAD_A, [first])
    [posted] = forge.marked()

    second = _outcome(BotRole.TESTER, ("src/z.py", 1, "nit", "one more nit at the new head"))
    _record(db, HEAD_B, second)
    team = _publish(forge, db, HEAD_B, [second])

    [edited] = forge.marked()
    assert edited.id == posted.id
    at_b, earlier = edited.body.split("## Earlier")
    assert f"## At {HEAD_B[:7]}" in at_b
    assert at_b.count("\n- ") == 1
    assert "`src/z.py:1` — tester — one more nit at the new head" in at_b
    assert earlier.count(f"\n- `{HEAD_A[:7]}` ") == 3
    assert team.nit_digest is not None and team.nit_digest.comment_id == str(posted.id)
    posts = [p for p in fetch_finding_posts(db, PR) if p.provenance == PROVENANCE_DIGEST]
    assert {p.comment_id for p in posts} == {str(posted.id)}
    assert len(posts) == 4

    writes = forge.upserts
    _publish(forge, db, HEAD_B, [second])
    assert forge.upserts == writes
    assert len(fetch_finding_posts(db, PR)) == 4


def test_cap_forty_and_count_the_rest(tmp_path: Path) -> None:
    """Forty-seven nits: forty listed, ``… and 7 more``, the count still forty-seven."""
    db = tmp_path / "review.db"
    _record(db, HEAD_A, _nits(NIT_DIGEST_MAX_ITEMS + 7))

    digest = digest_for_pr(db, PR, head_sha=HEAD_A)

    assert len(digest.items) == NIT_DIGEST_MAX_ITEMS
    assert digest.truncated == 7
    assert digest.count == NIT_DIGEST_MAX_ITEMS + 7
    body = render_digest(digest)
    assert body.rstrip().endswith("… and 7 more")
    assert body.count("\n- ") == NIT_DIGEST_MAX_ITEMS
    assert [item.path for item in digest.items] == sorted(item.path for item in digest.items)


def test_minor_still_opens_a_resolvable_thread(tmp_path: Path) -> None:
    """A minor is an advisory, proposed finding posted inline: a thread, no digest."""
    db = tmp_path / "review.db"
    outcome = _outcome(BotRole.TESTER, ("src/c.py", 2, "minor", "this branch is never exercised"))
    _record(db, HEAD_A, outcome)
    forge = MemoryForge()

    team = _publish(forge, db, HEAD_A, [outcome])

    [finding] = fetch_findings(db, PR)
    assert (finding.severity, finding.status) == (Severity.ADVISORY, FindingStatus.PROPOSED)
    assert [(c.path, c.line) for c in forge.inline] == [("src/c.py", 2)]
    assert forge.marked() == []
    assert team.nits_json(with_body=True) == {
        "count": 0,
        "digest_url": None,
        "error": None,
        "body": None,
    }


def test_digest_edit_failure_keeps_the_old_comment_and_names_it(tmp_path: Path) -> None:
    """A refused edit: the digest of A stays, the run names why, nothing is recorded for B."""
    db = tmp_path / "review.db"
    forge = MemoryForge()
    first = _nits(2)
    _record(db, HEAD_A, first)
    _publish(forge, db, HEAD_A, [first])
    [before] = forge.marked()

    forge.refuse_edits = True
    second = _outcome(BotRole.TESTER, ("src/z.py", 1, "nit", "one more nit at the new head"))
    _record(db, HEAD_B, second)
    team = _publish(forge, db, HEAD_B, [second])

    assert forge.marked() == [before]
    assert team.nit_digest is not None
    assert team.nit_digest.error == "HTTP 403: note is locked"
    assert team.nit_digest.report_line() == "nit digest not updated: HTTP 403: note is locked"
    assert team.nits_json(with_body=False)["error"] == "HTTP 403: note is locked"
    assert {p.head_sha for p in fetch_finding_posts(db, PR)} == {HEAD_A}


def test_severity_of_and_opens_thread_follow_the_map() -> None:
    """Each severity word maps through the one table; only a note opens no thread."""
    words = {"blocker": Severity.BLOCKING, "major": Severity.BLOCKING}
    words |= {"minor": Severity.ADVISORY, "nit": Severity.NOTE, "unheard-of": Severity.ADVISORY}
    for word, expected in words.items():
        comment = ReviewComment(file="a.py", line=1, severity=word, body="x")
        assert severity_of(comment) is expected
        assert opens_thread(comment) is (expected is not Severity.NOTE)


def test_noted_is_terminal() -> None:
    """``noted`` leaves for no status, and no status but insertion reaches it."""
    for status in FindingStatus:
        assert not is_valid_transition(FindingStatus.NOTED, status)
        assert not is_valid_transition(status, FindingStatus.NOTED)


def _note(fid: int, head: str, path: str, line: int, claim: str) -> Finding:
    return Finding(
        id=fid,
        pr_number=PR,
        head_sha=head,
        round=1,
        finder="scribe",
        claim_type=ClaimType.MISSING_DOCSTRING,
        severity=Severity.NOTE,
        file=path,
        line_start=line,
        line_end=line,
        claim=claim,
        evidence_pointer="",
        status=FindingStatus.NOTED,
    )


def test_collect_nits_orders_by_head_then_path_and_lists_a_repeat_once() -> None:
    """Current head first, then the latest earlier head; a nit raised again is listed at its latest head."""
    advisory = _note(9, HEAD_B, "src/x.py", 1, "not a nit").model_copy(
        update={"severity": Severity.ADVISORY}
    )
    findings = [
        _note(1, HEAD_A, "src/b.py", 2, "same remark\nover two lines"),
        _note(2, HEAD_A, "src/a.py", 8, "older head only"),
        _note(3, HEAD_B, "src/b.py", 2, "same remark over two lines"),
        _note(4, HEAD_B, "src/a.py", 1, "new at b"),
        advisory,
    ]

    items = collect_nits(findings, head_sha=HEAD_B)

    assert [(i.finding_id, i.head_sha, i.path) for i in items] == [
        (4, HEAD_B, "src/a.py"),
        (3, HEAD_B, "src/b.py"),
        (2, HEAD_A, "src/a.py"),
    ]
    assert items[1].text == "same remark over two lines"


def test_collect_nits_refuses_an_unstored_note() -> None:
    """A note without a ledger id cannot be joined to the digest."""
    with pytest.raises(ValueError, match="no ledger id"):
        collect_nits(
            [_note(1, HEAD_A, "a.py", 1, "x").model_copy(update={"id": None})], head_sha=HEAD_A
        )


def test_digest_for_pr_reads_the_comment_id_the_ledger_remembers(tmp_path: Path) -> None:
    """The latest ``digest`` row names the comment; posted and backfill rows do not."""
    db = tmp_path / "review.db"
    _record(db, HEAD_A, _nits(1))
    assert digest_for_pr(db, PR, head_sha=HEAD_A).comment_id is None
    [finding] = fetch_findings(db, PR)
    assert finding.id is not None
    from repoach.review.finding_posts import record_finding_post

    record_finding_post(
        db,
        FindingPost(
            finding_id=finding.id,
            pr_number=PR,
            forge="gitlab",
            head_sha=HEAD_A,
            comment_id="4242",
            provenance=PROVENANCE_DIGEST,
        ),
    )
    assert digest_for_pr(db, PR, head_sha=HEAD_A).comment_id == "4242"


def test_a_digest_row_opens_no_thread() -> None:
    """A ``digest`` row has no thread root; a ``posted`` row with the same id has one."""
    row = FindingPost(finding_id=1, pr_number=PR, forge="github", head_sha=HEAD_A, comment_id="77")
    assert row.root_id == 77 and row.is_root
    digest = row.model_copy(update={"provenance": PROVENANCE_DIGEST})
    assert digest.root_id is None and not digest.is_root


def _digest(head: str = HEAD_A) -> NitDigest:
    return NitDigest(
        pr_number=PR,
        head_sha=head,
        items=[NitItem(finding_id=1, head_sha=head, path="a.py", line=3, lens="scribe", text="x")],
    )


def test_render_digest_leaves_out_empty_sections() -> None:
    """Only current-head items: the marker, an At section, no Earlier, no count line."""
    body = render_digest(_digest())
    assert body.startswith(NIT_DIGEST_MARKER + "\n")
    assert f"## At {HEAD_A[:7]}" in body
    assert "## Earlier" not in body and "more" not in body
    earlier_only = render_digest(_digest().model_copy(update={"head_sha": HEAD_B}))
    assert "## At" not in earlier_only
    assert f"- `{HEAD_A[:7]}` `a.py:3` — scribe — x" in earlier_only


def test_report_line_and_json_of_a_digest() -> None:
    """No nit: no line; posted: the count and URL; not posted: says so."""
    empty = NitDigest(pr_number=PR, head_sha=HEAD_A, items=[])
    assert empty.report_line() == ""
    assert empty.as_json(with_body=True) == {
        "count": 0,
        "digest_url": None,
        "error": None,
        "body": None,
    }
    assert "body" not in empty.as_json(with_body=False)
    assert _digest().report_line() == "nit digest: 1 nit(s), not posted"
    posted = _digest().model_copy(update={"url": PR_URL + "#note_1"})
    assert posted.report_line() == f"nit digest: 1 nit(s), {PR_URL}#note_1"
    assert posted.as_json(with_body=True)["body"] == render_digest(posted)


@pytest.mark.parametrize(
    ("forge_name", "anchor"), [("github", "#issuecomment-"), ("gitlab", "#note_")]
)
def test_post_or_edit_digest_links_the_comment_on_each_forge(
    tmp_path: Path, forge_name: str, anchor: str
) -> None:
    """The URL is the change's page plus the forge's comment anchor."""
    db = tmp_path / "review.db"
    _record(db, HEAD_A, _nits(1))
    forge = MemoryForge(forge_name)

    done = post_or_edit_digest(forge, digest_for_pr(db, PR, head_sha=HEAD_A), db_path=db)

    [comment] = forge.marked()
    assert done.url == f"{PR_URL}{anchor}{comment.id}"
    assert [p.forge for p in fetch_finding_posts(db, PR)] == [forge_name]


def test_post_or_edit_digest_writes_nothing_without_a_nit(tmp_path: Path) -> None:
    """No item: no read, no write, the digest returned as given."""
    forge = MemoryForge()
    empty = NitDigest(pr_number=PR, head_sha=HEAD_A, items=[])
    assert post_or_edit_digest(forge, empty, db_path=tmp_path / "review.db") is empty
    assert forge.conversation == {}


def test_post_or_edit_digest_refuses_a_forge_without_comment_ids(tmp_path: Path) -> None:
    """A forge that is neither GitHub nor GitLab is refused before any write."""
    forge = MemoryForge("local")
    with pytest.raises(ValueError, match="keeps no comment ids"):
        post_or_edit_digest(forge, _digest(), db_path=tmp_path / "review.db")
    assert forge.conversation == {}


def test_backfill_never_links_a_note_to_a_thread(tmp_path: Path) -> None:
    """A minor's thread at the nit's own line is linked to the minor, never to the nit."""
    db = tmp_path / "review.db"
    outcome = _outcome(
        BotRole.TESTER,
        ("src/c.py", 2, "minor", "this branch is never exercised"),
        ("src/c.py", 2, "nit", "and the name could be shorter"),
    )
    _record(db, HEAD_A, outcome)
    forge = MemoryForge()
    forge.post_comment(PR, "minor", path="src/c.py", line=2)

    report = backfill_finding_posts(db, forge, PR, bot_login="bot")

    minor = next(f for f in fetch_findings(db, PR) if f.severity is Severity.ADVISORY)
    assert (report.findings, report.linked) == (1, 1)
    assert [p.finding_id for p in fetch_finding_posts(db, PR)] == [minor.id]
