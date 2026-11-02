"""SP-FINDING-THREAD-IDS: every posted finding records its forge comment and thread.

The posting path writes one ``finding_posts`` row per inline comment it
posts for a recorded finding, from the forge's own answer: GitHub's
review comment id, root comment id and batched review id; GitLab's note
id and discussion id. A dry run posts nothing and writes nothing. The
owner commands and the closure detection read the recorded thread.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from repoach.forge.github import GhResult, GitHubForge
from repoach.forge.protocol import Comment, ForgeError, ReviewThread
from repoach.review.finding_posts import (
    FindingPost,
    FindingPostIntegrityError,
    detect_closed_threads,
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

PR = 7
HEAD = "c0ffee" + "0" * 34
BOT = "repoach-bot"
DISCUSSION = "5f665901a9c4fedc6f4528231c3e5137e7ffa112"


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
