"""Marked comments: one per marker, opened by it, and no lens text can carry another marker.

SP-REVIEW-NITS-ONE-COMMENT. The archive and the nit digest are both
bot-marked conversation comments; text a lens wrote lands in both. The
forges write a marked comment through
:func:`~repoach.forge.protocol.marked_body`, find it with
:func:`~repoach.forge.protocol.carries_marker` (the marker opens it) and
converge on one comment per marker. Each forge runs here against an
in-memory comment store behind its ``_spawn`` seam.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import (
    ARCHIVE_MARKER,
    carries_marker,
    marked_body,
    neutralise_html_comments,
)
from repoach.forge.runner import CommandResult
from repoach.review.nit_digest import NIT_DIGEST_MARKER

BOT = "repoach-bot"
PR = 7


class Store:
    """The conversation comments of one pull request, as both forges' APIs serve them.

    Attributes:
        comments: Every stored comment, ``id``, ``body``, ``author``.
        on_post: Called before each post is stored (a concurrent run's post).
    """

    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = []
        self._ids = itertools.count(100)
        self.on_post: Callable[[], None] = lambda: None

    def add(self, body: str, *, author: str = BOT) -> int:
        comment_id = next(self._ids)
        self.comments.append({"id": comment_id, "body": body, "author": author})
        return comment_id

    def bodies(self, marker: str) -> list[str]:
        return [str(c["body"]) for c in self.comments if str(c["body"]).startswith(marker)]

    def _find(self, comment_id: int) -> dict[str, object]:
        [found] = [c for c in self.comments if c["id"] == comment_id]
        return found

    def gitlab(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Answer ``glab api`` on ``merge_requests/7`` (the state check) and its ``notes``."""
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        endpoint = next(a for a in argv if "merge_requests/" in a)
        if "/notes" not in endpoint:
            return CommandResult(0, json.dumps({"iid": PR, "state": "opened"}), "", argv=argv)
        tail = endpoint.split("/notes", 1)[1].split("?", 1)[0]
        if method == "GET":
            notes = [
                {
                    "id": c["id"],
                    "body": c["body"],
                    "author": {"username": c["author"]},
                    "created_at": "t",
                    "updated_at": "t",
                    "system": False,
                    "type": None,
                }
                for c in self.comments
            ]
            return CommandResult(0, json.dumps(notes), "", argv=argv)
        if method == "POST":
            self.on_post()
            new = self.add(json.loads(input_data or "{}")["body"])
            return CommandResult(0, json.dumps(self._gitlab_note(new)), "", argv=argv)
        note_id = int(tail.strip("/"))
        if method == "PUT":
            self._find(note_id)["body"] = json.loads(input_data or "{}")["body"]
            return CommandResult(0, json.dumps(self._gitlab_note(note_id)), "", argv=argv)
        assert method == "DELETE"
        self.comments.remove(self._find(note_id))
        return CommandResult(0, "", "", argv=argv)

    def _gitlab_note(self, note_id: int) -> dict[str, object]:
        found = self._find(note_id)
        return {"id": note_id, "body": found["body"], "author": {"username": found["author"]}}

    def github(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Answer ``gh pr view`` (the state check) and ``gh api`` on the issue comments."""
        if argv[1:3] == ["pr", "view"]:
            return CommandResult(0, json.dumps({"state": "OPEN"}), "", argv=argv)
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        endpoint = next(a for a in argv if "/issues/" in a)
        if method == "GET":
            page = [
                {"id": c["id"], "body": c["body"], "user": {"login": c["author"]}}
                for c in self.comments
            ]
            return CommandResult(0, json.dumps([page]), "", argv=argv)
        if method == "POST":
            self.on_post()
            new = self.add(json.loads(input_data or "{}")["body"])
            return CommandResult(0, json.dumps({"id": new}), "", argv=argv)
        comment_id = int(endpoint.rsplit("/", 1)[-1])
        if method == "PATCH":
            self._find(comment_id)["body"] = json.loads(input_data or "{}")["body"]
            return CommandResult(0, json.dumps({"id": comment_id}), "", argv=argv)
        assert method == "DELETE"
        self.comments.remove(self._find(comment_id))
        return CommandResult(0, "", "", argv=argv)


def _forge(kind: str, tmp_path: Path, store: Store) -> GitHubForge | GitLabForge:
    binary = tmp_path / kind
    binary.write_text("")
    if kind == "gh":
        forge: GitHubForge | GitLabForge = GitHubForge(
            cwd=tmp_path, gh_path=str(binary), bot_login=BOT
        )
        forge._spawn = store.github
    else:
        forge = GitLabForge(cwd=tmp_path, glab_path=str(binary), bot_username=BOT)
        forge._spawn = store.gitlab
    return forge


FORGES = ["gh", "glab"]


def test_neutralise_html_comments_leaves_no_opener_or_closer() -> None:
    """Both halves of an HTML comment are made inert; other text is untouched."""
    text = f"see {NIT_DIGEST_MARKER} and {ARCHIVE_MARKER} -- not <b>this</b>"
    inert = neutralise_html_comments(text)
    assert "<!--" not in inert and "-->" not in inert
    assert "&lt;!-- repoach:nit-digest --&gt;" in inert
    assert "<b>this</b>" in inert and "-- not" in inert


def test_marked_body_keeps_one_leading_marker_and_inerts_the_rest() -> None:
    """A body with or without its marker is written with exactly one, at the start."""
    rendered = f"{NIT_DIGEST_MARKER}\nquote: {ARCHIVE_MARKER}\n"
    written = marked_body(NIT_DIGEST_MARKER, rendered)
    assert written.startswith(NIT_DIGEST_MARKER + "\n")
    assert written.count("<!--") == 1
    assert marked_body(NIT_DIGEST_MARKER, "plain") == f"{NIT_DIGEST_MARKER}\nplain"
    assert marked_body(NIT_DIGEST_MARKER, written) == written


def test_carries_marker_only_at_the_start() -> None:
    """A marker quoted inside a body never makes it the marked comment."""
    assert carries_marker(f"{ARCHIVE_MARKER}\nreport", ARCHIVE_MARKER)
    assert not carries_marker(f"report quoting {ARCHIVE_MARKER}", ARCHIVE_MARKER)
    assert not carries_marker("anything", "")


@pytest.mark.parametrize("kind", FORGES)
def test_a_quoted_marker_is_neither_found_nor_overwritten(kind: str, tmp_path: Path) -> None:
    """Archive -> digest direction: the archive quotes the digest marker; the digest is its own comment."""
    store = Store()
    forge = _forge(kind, tmp_path, store)
    forge.upsert_marked_comment(PR, ARCHIVE_MARKER, f"a minor says {NIT_DIGEST_MARKER} twice")

    assert forge.find_marked_comment(PR, NIT_DIGEST_MARKER) is None
    forge.upsert_marked_comment(PR, NIT_DIGEST_MARKER, f"{NIT_DIGEST_MARKER}\nnit list")

    assert len(store.comments) == 2
    [archive] = store.bodies(ARCHIVE_MARKER)
    [digest] = store.bodies(NIT_DIGEST_MARKER)
    assert "a minor says &lt;!-- repoach:nit-digest --&gt; twice" in archive
    assert digest == f"{NIT_DIGEST_MARKER}\nnit list"


@pytest.mark.parametrize("kind", FORGES)
def test_a_digest_quoting_the_archive_marker_never_replaces_the_archive(
    kind: str, tmp_path: Path
) -> None:
    """Digest -> archive direction: a nit quoting the archive marker leaves the archive alone."""
    store = Store()
    forge = _forge(kind, tmp_path, store)
    forge.upsert_marked_comment(PR, NIT_DIGEST_MARKER, f"prefer a name for {ARCHIVE_MARKER}")
    forge.upsert_marked_comment(PR, ARCHIVE_MARKER, "the report")

    [archive] = store.bodies(ARCHIVE_MARKER)
    [digest] = store.bodies(NIT_DIGEST_MARKER)
    assert archive == f"{ARCHIVE_MARKER}\nthe report"
    assert "prefer a name for &lt;!-- repoach-review-archive --&gt;" in digest
