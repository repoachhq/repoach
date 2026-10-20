"""Pin the gh->glab shim (SP-GH-GLAB-SHIM): the five read argv shapes and the write refusal.

The fake below stands in for the ``glab`` executable at its boundary: it
answers the GitLab endpoints the shim requests with payloads recorded from
a real merge request, and records every argv so the tests can assert the
exact translation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SHIM = Path(__file__).resolve().parents[2] / "scripts" / "gh_glab_shim.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("gh_glab_shim", _SHIM)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shim = _load_shim()

_MR = {
    "iid": 283,
    "title": "feat(soc): wire spi, i2c and gpio into the generated soc",
    "description": "## Why",
    "source_branch": "feat/wire-spi-gpio-i2c-into-the-soc",
    "target_branch": "develop",
    "sha": "a9f7001cd82170b482a534d94b7bc0fede20f272",
    "author": {"username": "josephwfaye"},
    "state": "merged",
    "web_url": "https://gitlab.com/josephwfaye/yoob_2/-/merge_requests/283",
}

_DISCUSSIONS_PAGE_1 = [
    {"notes": [{"id": 1, "type": None, "system": True, "body": "merged"}]},
    {
        "notes": [
            {
                "id": 10,
                "type": "DiffNote",
                "body": "root",
                "author": {"username": "reviewer"},
                "position": {"new_path": "src/a.py", "new_line": 7},
            },
            {
                "id": 11,
                "type": "DiffNote",
                "body": "reply",
                "author": {"username": "josephwfaye"},
                "position": {"new_path": "src/a.py", "new_line": 7},
            },
        ]
    },
]

_DISCUSSIONS_PAGE_2 = [
    {
        "notes": [
            {
                "id": 20,
                "type": "DiffNote",
                "body": "deleted line",
                "author": {"username": "reviewer"},
                "position": {"old_path": "src/b.py", "new_path": None, "old_line": 3},
            }
        ]
    }
]


class FakeGlab:
    """Records argv and serves canned GitLab responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(args)
        endpoint = [a for a in args if not a.startswith("-")][-1]
        if endpoint.endswith("/raw_diffs"):
            return shim.GlabResult(0, "diff --git a/x b/x\n", "")
        if endpoint.endswith("/discussions"):
            pages = json.dumps(_DISCUSSIONS_PAGE_1) + json.dumps(_DISCUSSIONS_PAGE_2)
            return shim.GlabResult(0, pages, "")
        if endpoint.endswith("/notes"):
            notes = [
                {"id": 5, "system": True, "body": "approved", "author": {"username": "x"}},
                {"id": 6, "system": False, "body": "archive", "author": {"username": "bot"}},
            ]
            return shim.GlabResult(0, json.dumps(notes), "")
        if endpoint.endswith("/merge_requests/283"):
            return shim.GlabResult(0, json.dumps(_MR), "")
        return shim.GlabResult(1, "", "404")


def test_pr_diff_uses_raw_diffs_to_keep_git_headers() -> None:
    fake = FakeGlab()
    result = shim.dispatch(["pr", "diff", "283"], glab=fake)
    assert result.returncode == 0
    assert result.stdout.startswith("diff --git ")
    assert fake.calls == [["api", "projects/:id/merge_requests/283/raw_diffs"]]


def test_pr_view_maps_every_field_review_pr_reads() -> None:
    fields = "number,title,body,headRefName,baseRefName,author,state,url"
    result = shim.dispatch(["pr", "view", "283", "--json", fields], glab=FakeGlab())
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "number": 283,
        "title": _MR["title"],
        "body": "## Why",
        "headRefName": "feat/wire-spi-gpio-i2c-into-the-soc",
        "baseRefName": "develop",
        "author": {"login": "josephwfaye"},
        "state": "MERGED",
        "url": _MR["web_url"],
    }


def test_pr_view_head_and_base_for_diff_fallback_and_head_sha() -> None:
    fake = FakeGlab()
    both = shim.dispatch(["pr", "view", "283", "--json", "baseRefName,headRefOid"], glab=fake)
    head = shim.dispatch(["pr", "view", "283", "--json", "headRefOid"], glab=fake)
    assert json.loads(both.stdout) == {"baseRefName": "develop", "headRefOid": _MR["sha"]}
    assert json.loads(head.stdout) == {"headRefOid": _MR["sha"]}


def test_pull_comments_maps_diff_notes_across_pages_into_slurped_github_shape() -> None:
    fake = FakeGlab()
    result = shim.dispatch(
        ["api", "--paginate", "--slurp", "repos/:owner/:repo/pulls/283/comments"], glab=fake
    )
    pages = json.loads(result.stdout)
    assert result.returncode == 0
    assert len(pages) == 1
    assert pages[0] == [
        {
            "id": 10,
            "in_reply_to_id": None,
            "path": "src/a.py",
            "line": 7,
            "body": "root",
            "user": {"login": "reviewer"},
        },
        {
            "id": 11,
            "in_reply_to_id": 10,
            "path": "src/a.py",
            "line": 7,
            "body": "reply",
            "user": {"login": "josephwfaye"},
        },
        {
            "id": 20,
            "in_reply_to_id": None,
            "path": "src/b.py",
            "line": 3,
            "body": "deleted line",
            "user": {"login": "reviewer"},
        },
    ]
    assert fake.calls == [["api", "--paginate", "projects/:id/merge_requests/283/discussions"]]


def test_review_id_comments_is_empty_because_gitlab_has_no_review_object() -> None:
    fake = FakeGlab()
    result = shim.dispatch(["api", "repos/:owner/:repo/pulls/283/reviews/99/comments"], glab=fake)
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert fake.calls == []


def test_issue_comments_drops_system_notes() -> None:
    result = shim.dispatch(
        ["api", "--paginate", "--slurp", "repos/:owner/:repo/issues/283/comments"],
        glab=FakeGlab(),
    )
    assert json.loads(result.stdout) == [[{"id": 6, "body": "archive", "user": {"login": "bot"}}]]


def test_every_write_is_refused_without_calling_glab() -> None:
    fake = FakeGlab()
    writes = [
        ["pr", "comment", "283", "--body", "x"],
        ["pr", "review", "283", "--approve"],
        ["pr", "merge", "283", "--squash"],
        ["pr", "create", "--title", "t"],
        ["api", "--method", "POST", "repos/:owner/:repo/pulls/283/comments/10/replies"],
        ["api", "-X", "PATCH", "repos/:owner/:repo/issues/comments/6"],
        ["api", "repos/:owner/:repo/pulls/283/reviews", "--input", "-"],
        ["api", "repos/:owner/:repo/issues/283/comments", "-f", "body=x"],
    ]
    for argv in writes:
        assert shim.dispatch(argv, glab=fake).returncode == shim.EXIT_REFUSED_WRITE, argv
    assert fake.calls == []


def test_unknown_calls_are_unsupported() -> None:
    fake = FakeGlab()
    assert shim.dispatch(["pr", "checks", "283"], glab=fake).returncode == shim.EXIT_UNSUPPORTED
    assert shim.dispatch(["repo", "view"], glab=fake).returncode == shim.EXIT_UNSUPPORTED
    assert shim.dispatch([], glab=fake).returncode == shim.EXIT_UNSUPPORTED


def test_main_appends_one_json_line_per_call(tmp_path, monkeypatch, capsys) -> None:
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("GH_GLAB_SHIM_LOG", str(log))
    assert shim.main(["pr", "merge", "283"]) == shim.EXIT_REFUSED_WRITE
    entry = json.loads(log.read_text().strip())
    assert entry["argv"] == ["pr", "merge", "283"]
    assert entry["kind"] == "refused-write"
    assert "refused write" in capsys.readouterr().err
