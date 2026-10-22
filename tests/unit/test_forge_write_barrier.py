"""SP-DRY-RUN-NEVER-POSTS G1/G5: one barrier every forge write passes through.

Both forges run unmodified on a fake process spawner (``_spawn``, the one
process boundary), so every argv a method would send is observed. A
read-only handle refuses each of the six write methods before anything
is spawned; the private argv builders reach the process boundary only
through ``_write``; and a mutating argv handed to the read path is
refused as a bypass.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import repoach.forge.github as github_module
import repoach.forge.gitlab as gitlab_module
from repoach.forge import (
    ForgeWriteBarrier,
    ForgeWriteBypassError,
    ForgeWriteRefusedError,
    InlineCommentDraft,
    MergeMethod,
    ReviewDraft,
    WriteRefusal,
)
from repoach.forge.github import GhCli, GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import argv_head, is_write_argv
from repoach.forge.runner import CommandResult

_BOT = "repoach-bot"


_GH_VIEW = {
    "number": 7,
    "state": "OPEN",
    "headRefOid": "abc",
    "mergeStateStatus": "CLEAN",
    "mergeable": "MERGEABLE",
    "isDraft": False,
    "statusCheckRollup": [],
    "commits": [],
}
_GL_MR = {
    "iid": 7,
    "state": "opened",
    "sha": "abc",
    "detailed_merge_status": "mergeable",
    "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "abc"},
}
_GL_DISCUSSIONS = [{"id": "d1", "notes": [{"id": 100, "type": "DiffNote"}]}]


def _answer(args: list[str]) -> str:
    """A GitHub / GitLab that has PR / MR 7 open, mergeable, with one thread on note 100."""
    if is_write_argv(args):
        return json.dumps({"id": 1, "notes": [{"id": 2}]})
    if args[:2] == ["pr", "view"]:
        return json.dumps(_GH_VIEW)
    endpoint = next((a for a in args if "/" in a and not a.startswith("-")), "")
    if endpoint.endswith("merge_requests/7"):
        return json.dumps(_GL_MR)
    if "/discussions" in endpoint:
        return json.dumps(_GL_DISCUSSIONS)
    if "--slurp" in args:
        return "[[]]"
    return "[]"


class _Spawner:
    """Records every argv and answers it as :func:`_answer` does."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        self.argvs.append(list(argv))
        return CommandResult(returncode=0, stdout=_answer(argv[1:]), stderr="", argv=argv)

    def writes(self) -> list[list[str]]:
        return [argv for argv in self.argvs if is_write_argv(argv[1:])]


def _binary(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("")
    return str(path)


def _forge(
    kind: str, tmp_path: Path, *, read_only: bool, spawner: _Spawner
) -> GitHubForge | GitLabForge:
    forge: GitHubForge | GitLabForge
    if kind == "github":
        forge = GhCli(
            cwd=tmp_path, gh_path=_binary(tmp_path, "gh"), bot_login=_BOT, read_only=read_only
        )
    else:
        forge = GitLabForge(
            cwd=tmp_path,
            glab_path=_binary(tmp_path, "glab"),
            bot_username=_BOT,
            read_only=read_only,
        )
    forge._spawn = spawner
    return forge


WRITE_METHODS: dict[str, tuple[Callable[[Any], object], str]] = {
    "post_comment": (lambda f: f.post_comment(7, "body"), "post_comment #7"),
    "post_comment_inline": (
        lambda f: f.post_comment(7, "body", path="src/x.py", line=3, commit_sha="abc"),
        "post_comment #7",
    ),
    "post_review": (
        lambda f: f.post_review(
            7,
            ReviewDraft(
                verdict="COMMENT",
                body="b",
                commit_sha="abc",
                comments=[InlineCommentDraft(path="src/x.py", line=3, body="c")],
            ),
        ),
        "post_review #7",
    ),
    "reply": (lambda f: f.reply(7, 100, "body"), "reply #7"),
    "upsert_marked_comment": (
        lambda f: f.upsert_marked_comment(7, "<!-- m -->", "body"),
        "upsert_marked_comment #7",
    ),
    "merge": (
        lambda f: f.merge(7, method=MergeMethod.SQUASH, delete_branch=True, head_sha="abc"),
        "merge #7",
    ),
    "create_pull_request": (
        lambda f: f.create_pull_request(head="feat/x", base="develop", title="t", body="b"),
        "create_pull_request",
    ),
}


@pytest.mark.parametrize("kind", ["github", "gitlab"])
@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
def test_read_only_write_method_refuses_before_any_spawn(
    tmp_path: Path, kind: str, method: str
) -> None:
    spawner = _Spawner()
    forge = _forge(kind, tmp_path, read_only=True, spawner=spawner)
    call, head = WRITE_METHODS[method]

    with pytest.raises(ForgeWriteRefusedError) as refused:
        call(forge)

    assert spawner.argvs == []
    assert forge.refused_writes == [head]
    assert refused.value.head == head
    assert refused.value.reason is WriteRefusal.POSTING_DISABLED


@pytest.mark.parametrize("kind", ["github", "gitlab"])
@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
def test_writable_write_method_sends_its_writes_through_the_barrier(
    tmp_path: Path, kind: str, method: str
) -> None:
    """On an open barrier each write method sends at least one write, all admitted.

    The read path refuses a write argv that was not admitted
    (``ForgeWriteBypassError``), so a method that reaches the process
    boundary without ``_write`` fails here.
    """
    spawner = _Spawner()
    forge = _forge(kind, tmp_path, read_only=False, spawner=spawner)
    call, _head = WRITE_METHODS[method]

    call(forge)

    assert spawner.writes(), f"{method} sent no write"
    assert forge.refused_writes == []


def test_spec_interface_gh_cli_read_only(tmp_path: Path) -> None:
    spawner = _Spawner()
    forge = _forge("github", tmp_path, read_only=True, spawner=spawner)
    assert forge.read_only is True
    assert forge.barrier.refusal is WriteRefusal.POSTING_DISABLED
    assert _forge("github", tmp_path, read_only=False, spawner=spawner).read_only is False


GITHUB_ARGV_WRITES: dict[str, Callable[[GitHubForge], object]] = {
    "_pr_create": lambda f: f._pr_create(head="feat/x", base="develop", title="t", body="b"),
    "_pr_review_comment_conversation": lambda f: f._pr_review_comment(7, body="b"),
    "_pr_review_comment_inline": lambda f: f._pr_review_comment(
        7, body="b", commit_sha="abc", file="src/x.py", line=3
    ),
    "_pr_review_submit": lambda f: f._pr_review_submit(7, verdict="APPROVE", summary="s"),
    "_pr_review_submit_batch": lambda f: f._pr_review_submit_batch(
        7, commit_sha="abc", verdict="COMMENT", body="b", comments=[]
    ),
    "_reply_to_review_comment": lambda f: f._reply_to_review_comment(7, comment_id=1, body="b"),
    "_post_or_patch_post": lambda f: f._post_or_patch(7, "b", None),
    "_post_or_patch_patch": lambda f: f._post_or_patch(7, "b", 42),
    "_upsert_archive_comment": lambda f: f._upsert_archive_comment(7, body="b"),
}

GITLAB_ARGV_WRITES: dict[str, Callable[[GitLabForge], object]] = {
    "_post_note": lambda f: f._post_note(7, "b"),
    "_api_text_post": lambda f: f._api_text("projects/:id/x", method="POST", payload={"a": 1}),
    "_api_text_put": lambda f: f._api_text("projects/:id/x", method="PUT", payload={"a": 1}),
    "_api_text_delete": lambda f: f._api_text("projects/:id/x", method="DELETE"),
}


@pytest.mark.parametrize("name", sorted(GITHUB_ARGV_WRITES))
def test_github_argv_writes_go_through_the_barrier(tmp_path: Path, name: str) -> None:
    spawner = _Spawner()
    forge = _forge("github", tmp_path, read_only=True, spawner=spawner)

    with pytest.raises(ForgeWriteRefusedError):
        GITHUB_ARGV_WRITES[name](forge)

    assert spawner.writes() == []
    assert len(forge.refused_writes) == 1

    open_spawner = _Spawner()
    writable = _forge("github", tmp_path, read_only=False, spawner=open_spawner)
    GITHUB_ARGV_WRITES[name](writable)
    assert len(open_spawner.writes()) == 1
    assert writable.refused_writes == []


@pytest.mark.parametrize("name", sorted(GITLAB_ARGV_WRITES))
def test_gitlab_argv_writes_go_through_the_barrier(tmp_path: Path, name: str) -> None:
    spawner = _Spawner()
    forge = _forge("gitlab", tmp_path, read_only=True, spawner=spawner)

    with pytest.raises(ForgeWriteRefusedError):
        GITLAB_ARGV_WRITES[name](forge)

    assert spawner.argvs == []
    assert len(forge.refused_writes) == 1

    open_spawner = _Spawner()
    writable = _forge("gitlab", tmp_path, read_only=False, spawner=open_spawner)
    GITLAB_ARGV_WRITES[name](writable)
    assert len(open_spawner.writes()) == 1


@pytest.mark.parametrize("kind", ["github", "gitlab"])
@pytest.mark.parametrize(
    "args",
    [
        ["api", "--method", "POST", "repos/:owner/:repo/issues/7/comments", "-f", "body=x"],
        ["api", "repos/:owner/:repo/issues/7/comments", "-f", "body=x"],
        ["pr", "comment", "7", "--body", "x"],
        ["mr", "merge", "7", "--yes"],
    ],
)
def test_a_write_argv_on_the_read_path_is_a_bypass(
    tmp_path: Path, kind: str, args: list[str]
) -> None:
    spawner = _Spawner()
    forge = _forge(kind, tmp_path, read_only=False, spawner=spawner)

    with pytest.raises(ForgeWriteBypassError):
        forge._run(args)

    assert spawner.argvs == []


@pytest.mark.parametrize("kind", ["github", "gitlab"])
def test_reads_pass_the_read_path_on_a_read_only_handle(tmp_path: Path, kind: str) -> None:
    spawner = _Spawner()
    forge = _forge(kind, tmp_path, read_only=True, spawner=spawner)

    forge._run(["api", "--paginate", "--slurp", "repos/:owner/:repo/pulls/7/comments"])
    forge._run(["pr", "view", "7", "--json", "state"])

    assert len(spawner.argvs) == 2
    assert forge.refused_writes == []


@pytest.mark.parametrize(
    ("args", "writes"),
    [
        (["api", "--method", "POST", "x/y"], True),
        (["api", "-X", "patch", "x/y"], True),
        (["api", "--method=DELETE", "x/y"], True),
        (["api", "--method", "GET", "x/y", "-f", "q=1"], False),
        (["api", "x/y", "--input", "-"], True),
        (["api", "--paginate", "--slurp", "x/y"], False),
        (["pr", "create", "--head", "h"], True),
        (["pr", "review", "7", "--approve"], True),
        (["pr", "merge", "7"], True),
        (["pr", "view", "7"], False),
        (["pr", "diff", "7"], False),
        (["pr", "checks", "7"], False),
        (["run", "view", "1", "--log-failed"], False),
        (["mr", "note", "7"], True),
        ([], False),
    ],
)
def test_is_write_argv(args: list[str], writes: bool) -> None:
    assert is_write_argv(args) is writes


def test_argv_head_names_the_endpoint_without_the_body() -> None:
    head = argv_head(
        ["api", "--method", "POST", "repos/:owner/:repo/pulls/7/comments/1/replies", "-f", "b=x"]
    )
    assert head == "api --method POST repos/:owner/:repo/pulls/7/comments/1/replies"
    assert argv_head(["pr", "comment", "7", "--body", "secret"]) == "pr comment 7"


def test_closing_is_one_way_and_keeps_the_first_reason() -> None:
    barrier = ForgeWriteBarrier()
    assert barrier.read_only is False
    barrier.refuse_if_closed("anything")
    barrier.close(WriteRefusal.DRY_RUN)
    barrier.close(WriteRefusal.ADVISORY_POST_OFF)
    assert barrier.refusal is WriteRefusal.DRY_RUN
    with pytest.raises(ForgeWriteRefusedError, match="dry run attempted a forge write: x"):
        barrier.refuse_if_closed("x")
    assert barrier.refused == ["x"]


_HTTP_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _constant_argv(node: ast.List) -> list[str]:
    return [
        elt.value if isinstance(elt, ast.Constant) and isinstance(elt.value, str) else "<expr>"
        for elt in node.elts
    ]


def _argvs_bound_to(name: str, func: ast.AST) -> list[list[str]]:
    """Every list literal assigned or ``+=``-extended to ``name`` inside ``func``."""
    found: list[list[str]] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        ):
            found.append(_constant_argv(node.value))
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.List)
        ):
            found[-1:] = [[*(found[-1] if found else []), *_constant_argv(node.value)]]
    return found


def _self_calls(func: ast.AST, method: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]


def _is_self_method(node: ast.expr, method: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == method
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _get_gated(node: ast.expr) -> bool:
    """True for ``self._run if <x> == "GET" else self._write`` — the one safe conditional alias."""
    return (
        isinstance(node, ast.IfExp)
        and _is_self_method(node.body, "_run")
        and _is_self_method(node.orelse, "_write")
        and isinstance(node.test, ast.Compare)
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "GET"
    )


def _may_be_run(node: ast.expr) -> bool:
    """True when an expression can evaluate to ``self._run``."""
    if _is_self_method(node, "_run"):
        return True
    if isinstance(node, ast.IfExp):
        return _may_be_run(node.body) or _may_be_run(node.orelse)
    return False


def _run_calls(func: ast.AST) -> list[ast.Call]:
    """Every call that can reach ``self._run``: direct, or through a local alias.

    ``run = self._run`` (or any conditional that can pick it) makes ``run(...)``
    a ``_run`` call; ``run = self._run if method == "GET" else self._write``
    is the one exception, since its ``_run`` branch only ever carries a GET.
    """
    aliases = {
        target.id
        for node in ast.walk(func)
        if isinstance(node, ast.Assign) and _may_be_run(node.value) and not _get_gated(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (
            _is_self_method(node.func, "_run")
            or (isinstance(node.func, ast.Name) and node.func.id in aliases)
        )
    ]


def test_aliasing_run_is_seen_by_the_ast_guard() -> None:
    """``run = self._run`` then ``run(write argv)`` counts as a ``_run`` call; the GET gate does not."""
    func = ast.parse(
        "def f(self, method):\n"
        "    run = self._run\n"
        "    gated = self._run if method == 'GET' else self._write\n"
        "    run(['api', '--method', 'POST', 'x/y'])\n"
        "    gated(['api', '--method', method, 'x/y'])\n"
    ).body[0]
    calls = _run_calls(func)
    assert [c.func.id for c in calls if isinstance(c.func, ast.Name)] == ["run"]


def _functions(source: Path) -> list[ast.FunctionDef]:
    tree = ast.parse(source.read_text())
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


@pytest.mark.parametrize(
    "source",
    [Path(str(github_module.__file__)), Path(str(gitlab_module.__file__))],
    ids=["github", "gitlab"],
)
def test_every_write_argv_is_built_for_the_barrier(source: Path) -> None:
    """AC3: no forge method hands a mutating argv to ``_run``; every HTTP write literal feeds ``_write``.

    A ``"POST"``/``"PATCH"``/``"PUT"``/``"DELETE"`` literal is legitimate
    only in a function that calls ``self._write``, or as the ``method=``
    of ``self._api`` / ``self._api_text`` (whose non-GET calls go through
    ``_write``, pinned by the GitLab argv-write tests above). Calls through
    a local alias of ``self._run`` are checked like direct ones
    (:func:`_run_calls`).

    This is a static net, not the guard: it cannot follow an alias stored
    on ``self``, passed as an argument or built dynamically. The runtime
    tests above are the real guard — ``_run`` itself refuses any mutating
    argv not admitted by the barrier (``ForgeWriteBypassError``), and each
    write method is driven through a real spawner on both forges.
    """
    for func in _functions(source):
        for call in _run_calls(func):
            if not call.args:
                continue
            first = call.args[0]
            argvs: list[list[str]] = []
            if isinstance(first, ast.List):
                argvs = [_constant_argv(first)]
            elif isinstance(first, ast.Name):
                argvs = _argvs_bound_to(first.id, func)
            for argv in argvs:
                assert not is_write_argv(argv), f"{func.name} passes a write argv to _run: {argv}"
        literals = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Constant) and node.value in _HTTP_WRITE_METHODS
        ]
        if not literals:
            continue
        api_methods = {
            kw.value.value
            for name in ("_api", "_api_text")
            for call in _self_calls(func, name)
            for kw in call.keywords
            if kw.arg == "method" and isinstance(kw.value, ast.Constant)
        }
        stray = [lit.value for lit in literals if lit.value not in api_methods]
        assert not stray or _self_calls(func, "_write"), (
            f"{func.name} builds a {stray} write without calling self._write"
        )
