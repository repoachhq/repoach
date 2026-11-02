"""SP-OPEN-THREADS-BLOCK-THE-MERGE: both forges list every open review thread, every page.

GitHub answers ``gh api graphql --paginate --slurp`` with one GraphQL
response per page of 100 threads; GitLab is asked page after page
(``per_page=100&page=<n>``) until it answers an empty page. In both, the
only open thread sits on page 3: a read that stopped at the first page
would count zero and merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.forge import ForgeError
from repoach.forge import gitlab as gitlab_module
from repoach.forge.github import GhResult, GitHubForge, unresolved_threads_from_github
from repoach.forge.gitlab import GitLabForge, unresolved_threads_from_discussions
from repoach.forge.runner import CommandResult
from tests.forge_fakes import thread_node, threads_page


class ScriptedGh(GitHubForge):
    """A GitHub whose one ``gh api graphql`` answer is scripted."""

    def __init__(self, tmp_path: Path, answer: GhResult) -> None:
        super().__init__(cwd=tmp_path, gh_path="/nonexistent/gh", bot_login="bot")
        self._answer = answer
        self.argvs: list[list[str]] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        self.argvs.append(args)
        return self._answer


def _resolved_page(first_id: int) -> dict[str, object]:
    nodes = [thread_node(first_id + i, resolved=True, path="a.py", line=i) for i in range(100)]
    return threads_page(nodes, more=True)


def test_github_reads_the_only_open_thread_on_page_three(tmp_path: Path) -> None:
    """Three slurped pages of 100: the one open thread, on page 3, is counted and named."""
    pages = [
        _resolved_page(1000),
        _resolved_page(2000),
        threads_page([thread_node(3007, resolved=False, path="src/x.py", line=41)]),
    ]
    forge = ScriptedGh(tmp_path, GhResult(0, json.dumps(pages), "", argv=["gh"]))

    threads = forge.unresolved_threads(281)

    assert [(t.root_id, t.path, t.line, t.resolved) for t in threads] == [
        (3007, "src/x.py", 41, False)
    ]
    argv = forge.argvs[0]
    assert argv[:4] == ["api", "graphql", "--paginate", "--slurp"]
    assert "number=281" in argv
    query = argv[-1]
    assert "reviewThreads(first:100,after:$endCursor)" in query
    assert "pageInfo{hasNextPage endCursor}" in query
    assert "isResolved path line originalLine" in query


def test_github_counts_an_open_thread_without_a_root_id_and_falls_back_to_the_original_line() -> (
    None
):
    """An open node without ``databaseId`` still counts; an outdated thread keeps its original line."""
    outdated = thread_node(5, resolved=False, path="src/y.py", line=None)
    outdated["originalLine"] = 12
    rootless = {"isResolved": False, "path": "src/z.py", "line": 3, "comments": {"nodes": []}}
    threads = unresolved_threads_from_github([threads_page([outdated, rootless])])
    assert [(t.root_id, t.path, t.line) for t in threads] == [
        (5, "src/y.py", 12),
        (0, "src/z.py", 3),
    ]


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (json.dumps([threads_page([], more=True)]), "listing truncated"),
        (json.dumps([{"errors": [{"message": "rate limited"}]}]), "no data object"),
        (json.dumps([{"data": {"repository": {"pullRequest": None}}}]), "no thread connection"),
        ("", "returned no page"),
        ("[]", "returned no page"),
        ("not json", "JSON decode failed"),
    ],
)
def test_github_refuses_a_listing_it_cannot_count(
    tmp_path: Path, stdout: str, message: str
) -> None:
    """A truncated, errored, empty or undecodable answer raises instead of counting zero."""
    forge = ScriptedGh(tmp_path, GhResult(0, stdout, "", argv=["gh"]))
    with pytest.raises(ForgeError, match=message):
        forge.unresolved_threads(7)


def test_github_failed_call_raises(tmp_path: Path) -> None:
    """A failed ``gh`` call is a ``ForgeError``, not an empty listing."""
    forge = ScriptedGh(tmp_path, GhResult(1, "", "HTTP 502: bad gateway", argv=["gh"]))
    with pytest.raises(ForgeError):
        forge.unresolved_threads(7)


def _note(note_id: int, *, resolvable: bool, resolved: bool, position: bool = True) -> dict:
    note: dict[str, object] = {
        "id": note_id,
        "type": "DiffNote" if position else None,
        "resolvable": resolvable,
        "resolved": resolved,
    }
    if position:
        note["position"] = {
            "new_path": "src/x.py",
            "new_line": note_id % 50,
            "old_path": "src/x.py",
        }
    return note


def _discussion(note_id: int, **kwargs: bool) -> dict[str, object]:
    return {"id": f"{note_id:040x}", "notes": [_note(note_id, **kwargs)]}


class PagedGlab:
    """A GitLab MR whose discussions span ``pages`` pages, then answer ``[]``."""

    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.endpoints: list[str] = []

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        endpoint = next(a for a in argv[1:] if "/" in a and not a.startswith("-"))
        self.endpoints.append(endpoint)
        assert "--paginate" not in argv
        page = int(endpoint.rsplit("page=", 1)[1])
        answer = self.pages[page - 1] if page <= len(self.pages) else []
        return CommandResult(returncode=0, stdout=json.dumps(answer), stderr="", argv=argv)


def _gitlab(tmp_path: Path, spawner: object) -> GitLabForge:
    binary = tmp_path / "glab"
    binary.write_text("")
    forge = GitLabForge(cwd=tmp_path, glab_path=str(binary), bot_username="bot")
    forge._spawn = spawner
    return forge


def test_gitlab_reads_pages_until_an_empty_one_and_finds_the_open_thread_on_page_three(
    tmp_path: Path,
) -> None:
    """Pages 1 and 2 hold 100 settled discussions each; page 3 the only open one; page 4 is empty."""
    settled = [_discussion(i, resolvable=True, resolved=True) for i in range(1, 51)]
    plain = [
        _discussion(i, resolvable=False, resolved=False, position=False) for i in range(51, 101)
    ]
    page3 = [_discussion(307, resolvable=True, resolved=False)]
    spawner = PagedGlab([settled + plain, settled + plain, page3])
    forge = _gitlab(tmp_path, spawner)

    threads = forge.unresolved_threads(339)

    assert [(t.root_id, t.thread_id, t.path, t.line) for t in threads] == [
        (307, f"{307:040x}", "src/x.py", 7)
    ]
    assert spawner.endpoints == [
        f"projects/:id/merge_requests/339/discussions?per_page=100&page={page}"
        for page in (1, 2, 3, 4)
    ]


def test_gitlab_counts_a_general_resolvable_discussion() -> None:
    """A resolvable discussion outside the diff counts, with no file; resolved and plain ones do not."""
    threads = unresolved_threads_from_discussions(
        [
            {"id": "a" * 40, "notes": [{"id": 1, "type": "DiscussionNote", "resolvable": True}]},
            _discussion(2, resolvable=True, resolved=True),
            _discussion(3, resolvable=False, resolved=False, position=False),
            {"id": "b" * 40, "notes": []},
            "not a discussion",
        ]
    )
    assert [(t.root_id, t.thread_id, t.path, t.line) for t in threads] == [(1, "a" * 40, "", None)]


def test_gitlab_refuses_a_listing_past_the_page_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing still going after ``DISCUSSION_PAGES_MAX`` pages is refused, never truncated."""
    monkeypatch.setattr(gitlab_module, "DISCUSSION_PAGES_MAX", 2)
    page = [_discussion(1, resolvable=True, resolved=True)]
    forge = _gitlab(tmp_path, PagedGlab([page, page, page]))
    with pytest.raises(ForgeError, match="exceed 2 pages"):
        forge.unresolved_threads(339)


def test_gitlab_refuses_a_page_that_is_not_an_array_or_fails(tmp_path: Path) -> None:
    """An object where a page should be, or an HTTP error, raises."""

    def as_object(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        return CommandResult(
            returncode=0, stdout=json.dumps({"message": "x"}), stderr="", argv=argv
        )

    def failing(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr="glab: 502 (HTTP 502)", argv=argv)

    with pytest.raises(ForgeError, match="not a JSON array"):
        _gitlab(tmp_path, as_object).unresolved_threads(339)
    with pytest.raises(ForgeError):
        _gitlab(tmp_path, failing).unresolved_threads(339)
