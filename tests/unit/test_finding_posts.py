"""SP-FINDING-THREAD-IDS: every posted finding records its forge comment and thread.

The posting path writes one ``finding_posts`` row per inline comment it
posts for a recorded finding, from the forge's own answer: GitHub's
review comment id, root comment id and batched review id; GitLab's note
id and discussion id. A dry run posts nothing and writes nothing. The
owner commands and the closure detection read the recorded thread.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import pytest

from repoach.forge.github import GhResult, GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import Comment, ForgeError, InlineCommentDraft, ReviewThread
from repoach.forge.runner import CommandResult
from repoach.review import orchestrator
from repoach.review.finding_post_record import pair_posted
from repoach.review.finding_post_threads import detect_closed_threads
from repoach.review.finding_posts import (
    FindingPost,
    FindingPostIntegrityError,
    fetch_finding_posts,
    init_finding_posts_schema,
    record_finding_post,
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
from repoach.review.findings_bridge import record_findings_for_outcomes
from repoach.review.merge_gate import compute_merge_decision, summarise_ledger_facts
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.report import render_ledger_report
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict
from tests.fixtures.generated_artefacts.harness import Repo, install, review

PR = 7
HEAD = "c0ffee" + "0" * 34
BOT = "repoach-bot"
DISCUSSION = "5f665901a9c4fedc6f4528231c3e5137e7ffa112"


def _outcome(role: BotRole, comments: list[ReviewComment]) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="changes",
        comments=comments,
        model_used="fake",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="{}",
    )


def _team() -> TeamOutcome:
    return TeamOutcome(
        pr_number=PR, final_verdict=ReviewVerdict.REQUEST_CHANGES, n_blockers=0, n_majors=0
    )


def _record(db: Path, outcomes: list[ReviewerOutcome]) -> None:
    record_findings_for_outcomes(
        db, pr_number=PR, head_sha=HEAD, outcomes=outcomes, round_n=1, diff=""
    )


class BatchForge(GitHubForge):
    """GitHub answering one batched review (id 900) and listing its comments by id."""

    def __init__(self, created: list[dict[str, object]]) -> None:
        super().__init__(cwd=Path("/tmp"), gh_path="/nonexistent/gh", bot_login=BOT)
        self._created = created

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:2] == ["pr", "view"] and args[-1] == "state":
            return GhResult(0, json.dumps({"state": "OPEN"}), "", argv=args)
        return super()._run(args, input_data=input_data)

    def _pr_review_submit_batch(self, pr_number: int, **kwargs: object) -> GhResult:
        return GhResult(0, json.dumps({"id": 900}), "", argv=["gh", "api"])

    def _list_review_id_comments(self, pr_number: int, review_id: int) -> list[dict[str, object]]:
        return self._created


class GitLabSpawner:
    """A GitLab MR !7 that opens a diff discussion per positioned note."""

    def __init__(self) -> None:
        self._ids = itertools.count(501)

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        args = argv[1:]
        endpoint = next((a for a in args if "/" in a and not a.startswith("-")), "")
        payload = json.loads(input_data) if input_data else {}
        if endpoint.endswith("merge_requests/7"):
            answer: object = {
                "iid": 7,
                "state": "opened",
                "sha": HEAD,
                "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": HEAD},
            }
        elif endpoint.endswith("/discussions"):
            note = {
                "id": next(self._ids),
                "type": "DiffNote",
                "body": payload.get("body", ""),
                "author": {"username": BOT},
                "position": payload.get("position", {}),
            }
            answer = {"id": DISCUSSION, "notes": [note]}
        elif endpoint.endswith("/notes"):
            answer = {"id": next(self._ids), "body": payload.get("body", "")}
        else:
            answer = []
        return CommandResult(returncode=0, stdout=json.dumps(answer), stderr="", argv=argv)


def _gitlab(tmp_path: Path) -> GitLabForge:
    binary = tmp_path / "glab"
    binary.write_text("")
    forge = GitLabForge(cwd=tmp_path, glab_path=str(binary), bot_username=BOT)
    forge._spawn = GitLabSpawner()
    return forge


def test_each_posted_comment_records_one_row_with_the_forge_ids(tmp_path: Path) -> None:
    """AC1: one row per posted inline comment, joined to the finding of its reviewer and line."""
    db = tmp_path / "review.db"
    outcome = _outcome(
        BotRole.SENTINEL,
        [
            ReviewComment(file="src/a.py", line=3, severity="blocker", body="a guard is missing"),
            ReviewComment(file="src/b.py", line=9, severity="major", body="the loop never ends"),
        ],
    )
    _record(db, [outcome])
    forge = BatchForge(
        [
            {"id": 11, "path": "src/a.py", "line": 3, "body": "rendered elsewhere"},
            {"id": 12, "path": "src/b.py", "line": 9, "body": "rendered elsewhere"},
        ]
    )
    team = _team()

    ReviewTeamOrchestrator(forge=forge, db_path=db)._publish_outcome(
        pr_number=PR, outcome=outcome, head_sha=HEAD, team=team, posted_ids={}
    )

    findings = {(f.file, f.line_start): f for f in fetch_findings(db, PR)}
    posts = fetch_finding_posts(db, PR)
    assert team.finding_posts == 2
    assert [
        (p.finding_id, p.comment_id, p.thread_id, p.review_id, p.forge, p.provenance) for p in posts
    ] == [
        (findings[("src/a.py", 3)].id, "11", "11", "900", "github", "posted"),
        (findings[("src/b.py", 9)].id, "12", "12", "900", "github", "posted"),
    ]
    assert {findings[("src/a.py", 3)].finder} == {"sentinel"}
    assert all(p.head_sha == HEAD and p.posted_at for p in posts)


def test_two_reviewers_on_one_line_get_two_rows(tmp_path: Path) -> None:
    """Two findings at the same place, posted twice, keep two distinct comment ids."""
    db = tmp_path / "review.db"
    sentinel = _outcome(
        BotRole.SENTINEL,
        [ReviewComment(file="src/a.py", line=3, severity="blocker", body="the token leaks")],
    )
    tester = _outcome(
        BotRole.TESTER,
        [ReviewComment(file="src/a.py", line=3, severity="major", body="no test covers this")],
    )
    _record(db, [sentinel, tester])
    orch = ReviewTeamOrchestrator(forge=BatchForge([]), db_path=db)
    team = _team()
    for outcome, comment_id in ((sentinel, 21), (tester, 22)):
        orch._forge = BatchForge([{"id": comment_id, "path": "src/a.py", "line": 3}])
        orch._publish_outcome(
            pr_number=PR, outcome=outcome, head_sha=HEAD, team=team, posted_ids={}
        )

    by_finder = {f.id: f.finder for f in fetch_findings(db, PR)}
    posts = fetch_finding_posts(db, PR)
    assert [(by_finder[p.finding_id], p.comment_id) for p in posts] == [
        ("sentinel", "21"),
        ("tester", "22"),
    ]


def test_a_forge_answer_without_an_id_writes_no_row(tmp_path: Path) -> None:
    """The per-comment path with an unparseable answer posts, logs and records nothing."""
    db = tmp_path / "review.db"
    outcome = _outcome(
        BotRole.ARCHITECT,
        [ReviewComment(file="src/a.py", line=3, severity="major", body="split this module")],
    )
    _record(db, [outcome])

    class NoIdForge(BatchForge):
        def _pr_review_comment(self, pr_number: int, **kwargs: object) -> GhResult:
            return GhResult(0, "not json", "", argv=["gh", "api"])

        def _pr_review_submit(self, pr_number: int, **kwargs: object) -> GhResult:
            return GhResult(0, "", "", argv=["gh", "pr", "review"])

    team = _team()
    ReviewTeamOrchestrator(forge=NoIdForge([]), db_path=db)._publish_outcome(
        pr_number=PR, outcome=outcome, head_sha=None, team=team, posted_ids={}
    )

    assert team.posted_comments >= 1
    assert team.finding_posts == 0
    assert fetch_finding_posts(db, PR) == []


def test_pair_posted_falls_back_to_a_unique_line_and_skips_an_ambiguous_one() -> None:
    """A rewritten body pairs on a unique path and line; two drafts on one line pair on body only."""
    comments = [
        ReviewComment(file="src/a.py", line=3, severity="minor", body="one"),
        ReviewComment(file="src/b.py", line=5, severity="minor", body="two"),
        ReviewComment(file="src/b.py", line=5, severity="minor", body="three"),
    ]
    drafts = [
        InlineCommentDraft(path=c.file, line=c.line, body=f"rendered {c.body}") for c in comments
    ]
    created = [
        Comment(id=31, path="src/a.py", line=3, body="rewritten by the forge"),
        Comment(id=32, path="src/b.py", line=5, body="rewritten two"),
        Comment(id=33, path="src/b.py", line=5, body="rendered three"),
    ]

    pairs = pair_posted(comments, drafts, created)

    assert [(comment.body, made.id) for comment, made in pairs] == [("one", 31), ("three", 33)]


def test_gitlab_post_records_the_discussion_and_note_ids(tmp_path: Path) -> None:
    """AC2: on GitLab the thread is the 40-hex discussion, the comment the note, no review id."""
    db = tmp_path / "review.db"
    outcome = _outcome(
        BotRole.SENTINEL,
        [ReviewComment(file="src/a.py", line=3, severity="blocker", body="a guard is missing")],
    )
    _record(db, [outcome])
    team = _team()

    ReviewTeamOrchestrator(forge=_gitlab(tmp_path), db_path=db)._publish_outcome(
        pr_number=PR, outcome=outcome, head_sha=HEAD, team=team, posted_ids={}
    )

    [post] = fetch_finding_posts(db, PR)
    assert post.forge == "gitlab"
    assert post.thread_id == DISCUSSION
    assert len(post.thread_id) == 40
    assert post.comment_id == "501"
    assert post.review_id is None
    assert post.finding_id == fetch_findings(db, PR)[0].id


def test_a_post_for_an_unknown_finding_is_refused(tmp_path: Path) -> None:
    """A row naming a finding the PR does not have is never inserted."""
    db = tmp_path / "review.db"
    init_findings_schema(db)
    with pytest.raises(FindingPostIntegrityError):
        record_finding_post(
            db,
            FindingPost(finding_id=41, pr_number=PR, forge="github", head_sha=HEAD, comment_id="1"),
        )
    assert fetch_finding_posts(db, PR) == []


def test_an_older_table_gains_the_missing_columns(tmp_path: Path) -> None:
    """An older ``finding_posts`` keeps its rows and gains the columns added since."""
    db = tmp_path / "review.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE finding_posts (id INTEGER PRIMARY KEY, finding_id INTEGER NOT NULL, "
            "pr_number INTEGER NOT NULL, forge TEXT NOT NULL, head_sha TEXT NOT NULL, "
            "comment_id TEXT NOT NULL, provenance TEXT NOT NULL, posted_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO finding_posts VALUES (1, 5, 7, 'github', 'abc', '77', 'posted', 'now')"
        )

    init_finding_posts_schema(db)

    [post] = fetch_finding_posts(db, PR)
    assert (post.comment_id, post.thread_id, post.thread_closed_at_head) == ("77", None, None)


def test_dry_run_writes_no_post_and_reports_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1 / AC6: a dry run posts nothing, writes no row, and its JSON says ``finding_posts: 0``."""
    bench = install(monkeypatch, tmp_path)

    class Commenting:
        def __init__(self, **_kwargs: object) -> None:
            self.role = BotRole.SENTINEL

        def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
            return _outcome(
                BotRole.SENTINEL,
                [ReviewComment(file="src/x.py", line=1, severity="minor", body="rename x")],
            )

    monkeypatch.setattr(orchestrator, "Sentinel", Commenting)
    repo = Repo(tmp_path / "repo", {"README.md": "base\n"})
    head = repo.push_head({"src/x.py": "x = 1\n"})

    result = review(repo, monkeypatch, capsys, head=head)

    assert result.payload["finding_posts"] == 0
    assert result.payload["threads_closed"] == []
    assert any(f.file == "src/x.py" for f in fetch_findings(bench.db_path, PR))
    with sqlite3.connect(bench.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "finding_posts" in tables
        assert conn.execute("SELECT count(*) FROM finding_posts").fetchone() == (0,)


class ThreadForge(BatchForge):
    """Serves review threads and inline comments; ``jwfaye`` is the only maintainer."""

    def __init__(self, threads: list[ReviewThread], inline: list[Comment] | None = None) -> None:
        super().__init__([])
        self._threads = threads
        self._inline = inline or []
        self.thread_reads = 0

    def review_threads(self, number: int) -> list[ReviewThread]:
        self.thread_reads += 1
        return self._threads

    def comments(self, number: int) -> list[Comment]:
        return self._inline

    def conversation_comments(self, number: int) -> list[Comment]:
        return []

    def is_maintainer(self, login: str) -> bool:
        return login == "jwfaye"


def _blocking(db: Path, *, line: int = 10) -> int:
    init_findings_schema(db)
    return record_finding(
        db,
        Finding(
            pr_number=PR,
            head_sha=HEAD,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SPEC_GAP,
            severity=Severity.BLOCKING,
            file="src/x.py",
            line_start=line,
            line_end=line,
            claim="the environment is not protected",
            evidence_pointer=f"src/x.py:{line}",
            status=FindingStatus.VERIFIED,
            verification_method="refuter",
            verification_result="confirmed",
        ),
    )


def _post(db: Path, finding_id: int, comment_id: int) -> None:
    record_finding_post(
        db,
        FindingPost(
            finding_id=finding_id,
            pr_number=PR,
            forge="github",
            head_sha=HEAD,
            comment_id=str(comment_id),
            thread_id=str(comment_id),
        ),
    )


def test_a_thread_resolved_by_a_maintainer_is_recorded_and_settles_nothing(
    tmp_path: Path,
) -> None:
    """The closure is recorded with its head and login; the finding keeps its status."""
    db = tmp_path / "review.db"
    finding_id = _blocking(db)
    _post(db, finding_id, 77)
    forge = ThreadForge([ReviewThread(root_id=77, resolved=True, resolved_by="jwfaye")])

    run = detect_closed_threads(forge, db, pr_number=PR, head_sha=HEAD)

    assert run.lines() == [f"thread closed by jwfaye for #{finding_id}"]
    [post] = fetch_finding_posts(db, PR)
    assert (post.thread_closed_at_head, post.thread_closed_by) == (HEAD, "jwfaye")
    assert fetch_findings(db, PR)[0].status is FindingStatus.VERIFIED
    again = detect_closed_threads(forge, db, pr_number=PR, head_sha="later")
    assert again.closures == []
    assert forge.thread_reads == 1


def test_a_thread_resolved_by_the_bot_is_recorded(tmp_path: Path) -> None:
    """The role that opened the thread (the bot login) may close it."""
    db = tmp_path / "review.db"
    finding_id = _blocking(db)
    _post(db, finding_id, 77)
    forge = ThreadForge([ReviewThread(root_id=77, resolved=True, resolved_by="Repoach-Bot")])

    run = detect_closed_threads(forge, db, pr_number=PR, head_sha=HEAD)

    assert [c.counted for c in run.closures] == [True]
    assert fetch_finding_posts(db, PR)[0].thread_closed_by == "Repoach-Bot"


def test_a_thread_resolved_by_someone_else_is_reported_not_recorded(tmp_path: Path) -> None:
    """A resolver who is neither the bot nor a maintainer is named and not counted."""
    db = tmp_path / "review.db"
    finding_id = _blocking(db)
    _post(db, finding_id, 77)
    forge = ThreadForge([ReviewThread(root_id=77, resolved=True, resolved_by="author")])

    run = detect_closed_threads(forge, db, pr_number=PR, head_sha=HEAD)

    assert run.lines() == [
        f"thread resolved by author for #{finding_id}: not counted, neither the bot nor a "
        "maintainer"
    ]
    assert fetch_finding_posts(db, PR)[0].thread_closed_at_head is None


def test_no_recorded_thread_reads_no_forge_thread(tmp_path: Path) -> None:
    """With no post on record the forge is not asked about its threads."""
    db = tmp_path / "review.db"
    _blocking(db)
    forge = ThreadForge([])
    assert detect_closed_threads(forge, db, pr_number=PR, head_sha=HEAD).closures == []
    assert forge.thread_reads == 0


def test_unreadable_threads_record_nothing_and_say_why(tmp_path: Path) -> None:
    """A forge that cannot list its threads records nothing and reports the reason."""
    db = tmp_path / "review.db"
    _post(db, _blocking(db), 77)

    class Broken(ThreadForge):
        def review_threads(self, number: int) -> list[ReviewThread]:
            raise ForgeError("HTTP 502")

    run = detect_closed_threads(Broken([]), db, pr_number=PR, head_sha=HEAD)
    assert run.lines() == ["thread closures unread: HTTP 502"]


def test_render_ledger_report_includes_closed_threads_section_only_when_thread_lines_given(
    tmp_path: Path,
) -> None:
    """The archive report lists the closures under ``## Closed threads``, and only when there are some."""
    db = tmp_path / "review.db"
    finding_id = _blocking(db)
    facts = summarise_ledger_facts(db, pr_number=PR, head_sha=HEAD)
    line = f"thread closed by jwfaye for #{finding_id}"

    with_closure = render_ledger_report(
        db, pr_number=PR, decision=compute_merge_decision(facts), facts=facts, thread_lines=[line]
    )
    without = render_ledger_report(
        db, pr_number=PR, decision=compute_merge_decision(facts), facts=facts, thread_lines=[]
    )

    assert "## Closed threads" in with_closure
    assert line in with_closure
    assert "## Closed threads" not in without
