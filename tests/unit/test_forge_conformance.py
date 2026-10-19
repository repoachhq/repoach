"""SP-UNIVERSAL-FORGE G6 — one conformance suite, run against both forges.

Every test is parametrised over ``github`` and ``gitlab`` and replays a
fixture from ``tests/fixtures/forge/<forge>/<case>.json``: the ordered
subprocess exchanges (``gh`` / ``glab`` / ``git`` argv, stdin, exit
code, stdout, stderr) one protocol call made, and the normalised result
or error class it produced. :class:`~repoach.forge.recording.ReplaySpawn`
fails on the first argv that differs from the recording and the test
fails when a recorded call is left unconsumed, so each fixture pins the
exact calls the implementation makes — for GitHub, the same argv
repoach sent before the protocol existed.

Fixtures whose ``source`` starts with ``recorded`` were captured by
``scripts/record_forge_fixture.py`` against repoachhq/repoach PR #200
(merged, head branch deleted) and josephwfaye/yoob_2 MR !283 / !287
(merged, source branch deleted); ``synthetic`` ones cover what no
read-only live call can produce (writes, merges, 403s, forged markers)
and say why. On top of the byte-level replay, every test asserts the
forge-neutral contract of the method.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repoach.forge import (
    ARCHIVE_MARKER,
    Comment,
    Forge,
    ForgeForbiddenError,
    ForgeMergeRefusedError,
    ForgeNotFoundError,
)
from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.recording import (
    Exchange,
    ReplaySpawn,
    coerce_call,
    load_fixture,
    serialize_result,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "forge"
FORGES = ["github", "gitlab"]


def _build(forge_name: str, tmp_path: Path, bot: str) -> Forge:
    """A forge whose binary exists (so ``available()`` holds) under ``tmp_path``."""
    binary = tmp_path / ("gh" if forge_name == "github" else "glab")
    binary.write_text("")
    if forge_name == "github":
        return GitHubForge(cwd=tmp_path, gh_path=str(binary), bot_login=bot)
    return GitLabForge(cwd=tmp_path, glab_path=str(binary), bot_username=bot)


def _replay(forge_name: str, case: str, tmp_path: Path) -> tuple[object, Exception | None]:
    """Replay one fixture; assert the calls and the result match the recording.

    Returns:
        ``(raw_result, raised_exception)`` for case-specific assertions.
    """
    fixture: dict[str, Any] = load_fixture(FIXTURES / forge_name / f"{case}.json")
    forge = _build(forge_name, tmp_path, str(fixture["bot_username"]))
    spawn = ReplaySpawn([Exchange.from_json(c) for c in fixture["calls"]])
    forge._spawn = spawn
    args, kwargs = coerce_call(fixture["method"], fixture["args"], fixture["kwargs"])
    raw: object = None
    raised: Exception | None = None
    try:
        raw = getattr(forge, fixture["method"])(*args, **kwargs)
    except Exception as exc:
        raised = exc
    assert spawn.exchanges == [], f"recorded calls never made: {[e.args for e in spawn.exchanges]}"
    if fixture["raises"] is not None:
        assert raised is not None and type(raised).__name__ == fixture["raises"], raised
    else:
        assert raised is None, raised
        assert serialize_result(raw) == fixture["result"]
    return raw, raised


def test_fixture_sets_are_paired() -> None:
    """Every case is recorded for both forges — the suite is one suite."""
    github = {p.stem for p in (FIXTURES / "github").glob("*.json")}
    gitlab = {p.stem for p in (FIXTURES / "gitlab").glob("*.json")}
    assert github == gitlab
    assert len(github) >= 18


@pytest.mark.parametrize("forge_name", FORGES)
def test_available(forge_name: str, tmp_path: Path) -> None:
    """``available()`` is true with the binary present, false without it."""
    assert _build(forge_name, tmp_path, "bot").available() is True
    missing = tmp_path / "absent"
    absent: Forge = (
        GitHubForge(cwd=tmp_path, gh_path=str(missing), bot_login="bot")
        if forge_name == "github"
        else GitLabForge(cwd=tmp_path, glab_path=str(missing), bot_username="bot")
    )
    assert absent.available() is False


@pytest.mark.parametrize("forge_name", FORGES)
def test_pull_request(forge_name: str, tmp_path: Path) -> None:
    """A merged change whose head branch was deleted still reports its refs."""
    pr, _ = _replay(forge_name, "pull_request", tmp_path)
    assert pr.state == "merged"
    assert pr.head_ref and pr.base_ref == "develop"
    assert len(pr.head_sha) == 40


@pytest.mark.parametrize("forge_name", FORGES)
def test_pull_request_not_found(forge_name: str, tmp_path: Path) -> None:
    """A missing PR / MR raises :class:`ForgeNotFoundError` carrying its number."""
    _, raised = _replay(forge_name, "pull_request_not_found", tmp_path)
    assert isinstance(raised, ForgeNotFoundError) and raised.number == 999999


@pytest.mark.parametrize("forge_name", FORGES)
def test_pull_request_forbidden(forge_name: str, tmp_path: Path) -> None:
    """An insufficient scope raises :class:`ForgeForbiddenError` with the forge text."""
    _, raised = _replay(forge_name, "pull_request_forbidden", tmp_path)
    assert isinstance(raised, ForgeForbiddenError) and "403" in raised.message


@pytest.mark.parametrize("forge_name", FORGES)
def test_pr_state(forge_name: str, tmp_path: Path) -> None:
    """The state is normalised to ``open|merged|closed``."""
    state, _ = _replay(forge_name, "pr_state", tmp_path)
    assert state == "merged"


@pytest.mark.parametrize("forge_name", FORGES)
def test_head_sha(forge_name: str, tmp_path: Path) -> None:
    """The head SHA is a full 40-hex SHA."""
    sha, _ = _replay(forge_name, "head_sha", tmp_path)
    assert isinstance(sha, str) and len(sha) == 40


@pytest.mark.parametrize("forge_name", FORGES)
def test_diff(forge_name: str, tmp_path: Path) -> None:
    """The diff carries one ``diff --git`` header per changed file (W1 A2)."""
    diff, _ = _replay(forge_name, "diff", tmp_path)
    assert diff.files
    assert diff.text.count("diff --git a/") == len(diff.files)


@pytest.mark.parametrize("forge_name", FORGES)
def test_diff_fallback(forge_name: str, tmp_path: Path) -> None:
    """When the API diff is refused, the fallback still yields git-format headers."""
    diff, _ = _replay(forge_name, "diff_fallback", tmp_path)
    assert diff.files and diff.text.startswith("diff --git a/")


@pytest.mark.parametrize("forge_name", FORGES)
def test_comments(forge_name: str, tmp_path: Path) -> None:
    """Inline comments of a recorded change are normalised (possibly none)."""
    comments, _ = _replay(forge_name, "comments", tmp_path)
    assert all(isinstance(c, Comment) and c.path for c in comments)


@pytest.mark.parametrize("forge_name", FORGES)
def test_comments_threaded(forge_name: str, tmp_path: Path) -> None:
    """A thread is a root plus replies pointing at it; non-diff notes are not inline."""
    comments, _ = _replay(forge_name, "comments_threaded", tmp_path)
    root, reply = comments
    assert root.in_reply_to is None and root.path == "a.py" and root.line == 3
    assert reply.in_reply_to == root.id


@pytest.mark.parametrize("forge_name", FORGES)
def test_conversation_comments(forge_name: str, tmp_path: Path) -> None:
    """Conversation comments carry no anchor and no system notes."""
    comments, _ = _replay(forge_name, "conversation_comments", tmp_path)
    assert all(c.path is None for c in comments)


@pytest.mark.parametrize("forge_name", FORGES)
def test_reviews(forge_name: str, tmp_path: Path) -> None:
    """Reviews / approvals are normalised with an author and a state."""
    reviews, _ = _replay(forge_name, "reviews", tmp_path)
    assert all(r.author and r.state for r in reviews)


@pytest.mark.parametrize("forge_name", FORGES)
def test_reviews_two_approvals(forge_name: str, tmp_path: Path) -> None:
    """Two approvals become two ``APPROVED`` reviews carrying their authors."""
    reviews, _ = _replay(forge_name, "reviews_two_approvals", tmp_path)
    assert [(r.author, r.state) for r in reviews] == [("alice", "APPROVED"), ("bob", "APPROVED")]


@pytest.mark.parametrize("forge_name", FORGES)
def test_post_comment(forge_name: str, tmp_path: Path) -> None:
    """An unanchored comment is posted as a conversation comment / note."""
    comment, _ = _replay(forge_name, "post_comment", tmp_path)
    assert comment.body == "hello" and comment.path is None


@pytest.mark.parametrize("forge_name", FORGES)
def test_post_comment_inline(forge_name: str, tmp_path: Path) -> None:
    """An anchored comment comes back anchored with its new id."""
    comment, _ = _replay(forge_name, "post_comment_inline", tmp_path)
    assert (comment.path, comment.line) == ("a.py", 3) and comment.id == 6


@pytest.mark.parametrize("forge_name", FORGES)
def test_post_comment_unanchorable(forge_name: str, tmp_path: Path) -> None:
    """A line outside the diff: GitHub refuses (422); GitLab falls back to a note."""
    comment, raised = _replay(forge_name, "post_comment_unanchorable", tmp_path)
    if forge_name == "github":
        assert raised is not None and "422" in str(raised)
    else:
        assert comment.path is None and "a.py:999" in comment.body


@pytest.mark.parametrize("forge_name", FORGES)
def test_post_review(forge_name: str, tmp_path: Path) -> None:
    """A review posts its inline comments and returns their ids."""
    review, _ = _replay(forge_name, "post_review", tmp_path)
    assert [(c.path, c.line, c.id) for c in review.comments] == [("a.py", 3, 8)]
    assert review.state == "REQUEST_CHANGES"


@pytest.mark.parametrize("forge_name", FORGES)
def test_post_review_bare(forge_name: str, tmp_path: Path) -> None:
    """A verdict without anchors is one submission (GitHub review / GitLab note)."""
    review, _ = _replay(forge_name, "post_review_bare", tmp_path)
    assert review.comments == [] and review.body == "lgtm"


@pytest.mark.parametrize("forge_name", FORGES)
def test_reply(forge_name: str, tmp_path: Path) -> None:
    """A reply lands in the root's thread."""
    comment, _ = _replay(forge_name, "reply", tmp_path)
    assert comment.in_reply_to == 1 and comment.body == "evidence"


@pytest.mark.parametrize("forge_name", FORGES)
def test_reply_unknown_thread(forge_name: str, tmp_path: Path) -> None:
    """Replying to a comment no thread holds raises :class:`ForgeNotFoundError`."""
    _, raised = _replay(forge_name, "reply_unknown_thread", tmp_path)
    assert isinstance(raised, ForgeNotFoundError)


@pytest.mark.parametrize("forge_name", FORGES)
def test_find_marked_comment(forge_name: str, tmp_path: Path) -> None:
    """The recorded archive lookup returns a trusted marker comment or none."""
    found, _ = _replay(forge_name, "find_marked_comment", tmp_path)
    assert found is None or ARCHIVE_MARKER in found.body


@pytest.mark.parametrize("forge_name", FORGES)
def test_find_marked_comment_forged(forge_name: str, tmp_path: Path) -> None:
    """A forged marker from another author (or a system note) is never trusted."""
    found, _ = _replay(forge_name, "find_marked_comment_forged", tmp_path)
    assert found is not None and found.id == 11 and found.author == "repoach-bot"


@pytest.mark.parametrize("forge_name", FORGES)
def test_upsert_marked_comment(forge_name: str, tmp_path: Path) -> None:
    """An existing marker comment is updated in place, never duplicated."""
    comment, _ = _replay(forge_name, "upsert_marked_comment", tmp_path)
    assert comment.id == 11 and comment.body.startswith(ARCHIVE_MARKER)


@pytest.mark.parametrize("forge_name", FORGES)
def test_upsert_marked_comment_create(forge_name: str, tmp_path: Path) -> None:
    """With no marker comment yet, one is created carrying the marker."""
    comment, _ = _replay(forge_name, "upsert_marked_comment_create", tmp_path)
    assert comment.id == 13 and comment.body.startswith(ARCHIVE_MARKER)


@pytest.mark.parametrize("forge_name", FORGES)
def test_ci_status(forge_name: str, tmp_path: Path) -> None:
    """The recorded merged change's CI fact is one of the four states."""
    status, _ = _replay(forge_name, "ci_status", tmp_path)
    assert status.state in {"success", "failure", "pending", "missing"}
    if forge_name == "gitlab":
        assert status.state == "success"


@pytest.mark.parametrize("forge_name", FORGES)
def test_ci_status_failed(forge_name: str, tmp_path: Path) -> None:
    """A red required check / pipeline is ``failure`` with the culprit named."""
    status, _ = _replay(forge_name, "ci_status_failed", tmp_path)
    assert status.state == "failure" and status.failed


@pytest.mark.parametrize("forge_name", FORGES)
def test_ci_status_pending(forge_name: str, tmp_path: Path) -> None:
    """A running check / pipeline is ``pending``."""
    status, _ = _replay(forge_name, "ci_status_pending", tmp_path)
    assert status.state == "pending" and status.pending


@pytest.mark.parametrize("forge_name", FORGES)
def test_ci_status_missing(forge_name: str, tmp_path: Path) -> None:
    """A required check never reported / no pipeline is ``missing``."""
    status, _ = _replay(forge_name, "ci_status_missing", tmp_path)
    assert status.state == "missing" and status.missing


@pytest.mark.parametrize("forge_name", FORGES)
def test_checks(forge_name: str, tmp_path: Path) -> None:
    """Every check carries a name and a coarse bucket."""
    checks, _ = _replay(forge_name, "checks", tmp_path)
    assert checks and all(c.name and c.bucket for c in checks)


@pytest.mark.parametrize("forge_name", FORGES)
def test_check_log(forge_name: str, tmp_path: Path) -> None:
    """A failed check's log is read from its link."""
    log, _ = _replay(forge_name, "check_log", tmp_path)
    assert "AssertionError" in log


@pytest.mark.parametrize("forge_name", FORGES)
def test_check_log_no_link(forge_name: str, tmp_path: Path) -> None:
    """A link naming no job makes no call and returns ``None``."""
    log, _ = _replay(forge_name, "check_log_no_link", tmp_path)
    assert log is None


@pytest.mark.parametrize("forge_name", FORGES)
def test_merge(forge_name: str, tmp_path: Path) -> None:
    """A merge with the configured method reports the resulting commit."""
    result, _ = _replay(forge_name, "merge", tmp_path)
    assert result.merged and result.sha and len(result.sha) == 40


@pytest.mark.parametrize("forge_name", FORGES)
def test_merge_refused(forge_name: str, tmp_path: Path) -> None:
    """A refusal no read fact explains (GitLab: a bare 405) is ``refused``, never pending."""
    _, raised = _replay(forge_name, "merge_refused", tmp_path)
    assert isinstance(raised, ForgeMergeRefusedError)
    assert raised.reason == "refused" and raised.pending is False


@pytest.mark.parametrize("forge_name", FORGES)
def test_branch_protection(forge_name: str, tmp_path: Path) -> None:
    """Unprotected (GitHub 404) is unreadable; GitLab reads its merge rules."""
    protection, _ = _replay(forge_name, "branch_protection", tmp_path)
    if forge_name == "github":
        assert protection.readable is False and protection.required_checks == []
    else:
        assert protection.readable and protection.merge_method == "merge"


@pytest.mark.parametrize("forge_name", FORGES)
def test_branch_protection_forbidden(forge_name: str, tmp_path: Path) -> None:
    """A 403 is a value (``readable=False``), never an exception."""
    protection, _ = _replay(forge_name, "branch_protection_forbidden", tmp_path)
    assert protection.readable is False


@pytest.mark.parametrize("forge_name", FORGES)
def test_open_pull_requests(forge_name: str, tmp_path: Path) -> None:
    """Open changes targeting the base are listed as open."""
    prs, _ = _replay(forge_name, "open_pull_requests", tmp_path)
    assert prs and all(p.state == "open" and p.base_ref == "develop" for p in prs)


@pytest.mark.parametrize("forge_name", FORGES)
def test_merged_shas(forge_name: str, tmp_path: Path) -> None:
    """Merged changes yield the commits they landed on the base."""
    shas, _ = _replay(forge_name, "merged_shas", tmp_path)
    assert shas and all(len(s) == 40 for s in shas)


@pytest.mark.parametrize("forge_name", FORGES)
def test_create_pull_request(forge_name: str, tmp_path: Path) -> None:
    """Opening a change returns its number and URL."""
    pr, _ = _replay(forge_name, "create_pull_request", tmp_path)
    assert pr.number in {43, 8} and pr.url.startswith("https://")


@pytest.mark.parametrize("forge_name", FORGES)
def test_fetch_head(forge_name: str, tmp_path: Path) -> None:
    """The head is fetched from the forge's own server-side ref (W1 A10)."""
    ref, _ = _replay(forge_name, "fetch_head", tmp_path)
    assert ref in {"refs/pull/42/head", "refs/merge-requests/7/head"}


@pytest.mark.parametrize("forge_name", FORGES)
def test_git(forge_name: str, tmp_path: Path) -> None:
    """``git`` runs in the forge's clone and returns the captured result."""
    res, _ = _replay(forge_name, "git", tmp_path)
    assert res.ok and res.stdout.strip() == "a" * 40


@pytest.mark.parametrize("forge_name", FORGES)
def test_is_trusted_author(forge_name: str, tmp_path: Path) -> None:
    """Trust fails closed: empty bot or empty author never matches (W1 A11)."""
    forge = _build(forge_name, tmp_path, "repoach-bot")
    assert forge.is_trusted_author(Comment(id=1, author="repoach-bot"))
    assert not forge.is_trusted_author(Comment(id=1, author="someone"))
    assert not forge.is_trusted_author(Comment(id=1, author=""))
    assert not _build(forge_name, tmp_path, "").is_trusted_author(Comment(id=1, author=""))


@pytest.mark.parametrize("forge_name", FORGES)
def test_bot_username(forge_name: str, tmp_path: Path) -> None:
    """The configured bot identity is what the forge exposes."""
    assert _build(forge_name, tmp_path, "repoach-bot").bot_username == "repoach-bot"
