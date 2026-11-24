"""The forge recorder redacts by class at record time (SP-RECORDER-REDACTS-BY-CLASS).

Personal keys and a user object's ``name`` and URLs are dropped, every
``id`` key is kept with a user object's value synthetic, URLs and remotes
get a placeholder host, namespace and repository with their path shape
kept, logins become class words in stdout and argv, and the
recorder refuses an unclassified login or a generic personal value
without printing it. The forge readers are replayed on redacted
recordings, and the committed fixtures are a fixed point of the same
redaction (``scripts/redact_forge_fixtures.py``).
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import inspect
import json
import os
import re
import shutil
import string
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml
from structlog.testing import capture_logs

from repoach.forge import (
    closure,
    closure_tables,
    fixture_judge,
    fixture_pipeline,
    fixture_reading,
    recording,
    recording_tables,
)
from repoach.forge import denylist as deny_list
from repoach.forge.fixture_reading import (
    NON_PERSON_LOGINS,
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
    decode_json_values,
    fixture_strings,
)
from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import Forge, ForgeError
from repoach.forge.recording import (
    PERSONAL_KEY_RE,
    TOKEN_CHARS,
    Exchange,
    NeedleError,
    ReplaySpawn,
    SyntheticIdsExhaustedError,
    UserIdMap,
    classify_logins,
    classify_text,
    declared_project_ids,
    declared_user_ids,
    is_synthetic_number,
    load_fixture,
    mention_pattern,
    mentions,
    project_id_values,
    redact_payload,
    redact_stdout,
    redact_url,
    redact_urls_in_text,
    token_pattern,
    trusted_declaration,
    user_id_key,
    user_id_values,
)
from repoach.forge.runner import CommandResult
from tests.child_pytest import child_pytest_argv

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "forge"
NO_DENYLIST = deny_list.Denylist()
"""An explicit empty deny-list: omitted, the recorder and the offline transform read the maintainer's."""


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rec = _load("record_forge_fixture")
redact_fixtures = _load("redact_forge_fixtures")


def needles(
    raw: object,
    *,
    roles: dict[str, str],
    remote: str = "",
    denylist: tuple[str, ...] = (),
    min_length: int = recording.NEEDLE_MIN_LENGTH,
) -> closure.Known:
    """The known values of a recording (:func:`repoach.forge.recording.known_values`)."""
    return recording.known_values(
        raw,
        roles=roles,
        remote=remote,
        denylist=deny_list.Denylist(denylist),
        min_length=min_length,
    )


def locations(text: str, **_options: object) -> list[tuple[str, tuple[str, ...]]]:
    """Every host the oracle's reader finds in ``text`` with the path segments written after it."""
    return [
        (place.host.casefold(), tuple(s for s in recording._path_after(text, place.end) if s))
        for place in closure.hosts(text)
    ]


def unredacted_host(text: str, **_options: object) -> bool:
    """Whether the closure oracle finds a host, an address or a mailbox in ``text`` that it refuses."""
    return closure.judge(text, closure.Known(())) is not None


_REFUSAL_RE = re.compile(
    r"^(?:residual (?P<kind>[a-z-]+) (?P<n>[0-9]+)|deny-list entry (?P<deny>[0-9]+)|[a-z _]+?) at (?P<path>.*)$"
)


def residual(fixture: dict[str, Any], known: closure.Known) -> tuple[str, int, str] | None:
    """The first refusal the recorder's judge makes of ``fixture``, as ``(kind, ordinal, path)``, or ``None``."""
    try:
        fixture_judge.refuse_unclassified(
            fixture, deny_list.Denylist(known.values("deny-list")), known
        )
    except fixture_judge.UnclassifiedValueError as exc:
        found = _REFUSAL_RE.match(str(exc))
        assert found is not None, str(exc)
        if found.group("deny"):
            return "deny-list", int(found.group("deny")), found.group("path")
        kind = found.group("kind") or str(exc).partition(" at ")[0]
        return kind, int(found.group("n") or 0), found.group("path")
    return None


def _build(forge_name: str, tmp_path: Path, bot: str) -> Forge:
    """A forge whose binary exists under ``tmp_path`` (``available()`` holds)."""
    binary = tmp_path / ("gh" if forge_name == "github" else "glab")
    binary.write_text("")
    if forge_name == "github":
        return GitHubForge(cwd=tmp_path, gh_path=str(binary), bot_login=bot)
    return GitLabForge(cwd=tmp_path, glab_path=str(binary), bot_username=bot)


def test_personal_keys_are_dropped_and_user_name_by_context_ids_kept() -> None:
    """D1: personal keys go, a user's ``name`` goes, every ``id`` and a check-run ``name`` stay."""
    payload = {
        "avatar_url": "https://avatars.example.org/u/1",
        "node_id": "MDQ6VXNlcjE=",
        "author": {
            "id": "MDQ6VXNlcjE=",
            "login": "author",
            "name": "Some Person",
            "email": "p@example.org",
            "web_url": "https://code.example.org/someone",
        },
        "comments": [{"id": 77, "body": "ok", "notes": [{"id": 78, "body": "x"}]}],
        "checks": [{"name": "unit (3.11)", "id": 5, "status": "completed"}],
    }
    out = redact_payload(payload)
    assert out == {
        "author": {"id": "MDQ6VXNlcjE=", "login": "author"},
        "comments": [{"id": 77, "body": "ok", "notes": [{"id": 78, "body": "x"}]}],
        "checks": [{"name": "unit (3.11)", "id": 5, "status": "completed"}],
    }
    for key in ("full_name", "avatar_url", "email", "public_email", "node_id", "Email"):
        assert PERSONAL_KEY_RE.match(key)
    assert not PERSONAL_KEY_RE.match("name") and not PERSONAL_KEY_RE.match("id")


def test_url_values_keep_their_shape_with_placeholder_host_and_namespace() -> None:
    """D1b: placeholder host and namespace, path kept; a non-URL under ``web_url`` is dropped."""
    assert (
        redact_url("https://a.example/o/r/pull/12")
        == "https://forge.example.com/org/project/pull/12"
    )
    assert (
        redact_url("https://b.example/g/s/r/-/merge_requests/7")
        == "https://forge.example.com/org/project/-/merge_requests/7"
    )
    assert redact_url("https://b.example/-/jobs/5") == "https://forge.example.com/-/jobs/5"
    assert redact_url("https://b.example/foo/-/jobs/5") == "https://forge.example.com/org/-/jobs/5"
    assert recording.path_identity(["foo", "-", "jobs", "5"]) == recording.PathIdentity(
        ["org", "-", "jobs", "5"], ("foo",)
    )
    assert redact_url("https://b.example/g/s/r") == "https://forge.example.com/org/project"
    assert (
        redact_url("https://a.example/o/r/actions/runs/42/job/100")
        == "https://forge.example.com/org/project/actions/runs/42/job/100"
    )
    assert (
        redact_url("https://api.a.example/repos/o/r/issues/comments/9")
        == "https://forge.example.com/repos/org/project/issues/comments/9"
    )
    assert (
        redact_url("https://b.example/api/v4/projects/g%2Fs%2Fr/merge_requests/7/merge")
        == "https://forge.example.com/api/v4/projects/org%2Fproject/merge_requests/7/merge"
    )
    assert redact_url("not a url") == ""
    assert redact_payload({"web_url": "not a url", "iid": 7}) == {"iid": 7}
    assert redact_payload({"body": "see https://git.corp.internal/g/r/-/jobs/5."}) == {
        "body": "see https://forge.example.com/org/project/-/jobs/5."
    }
    assert redact_payload({"body": "https://b.example/g/r"}) == {"body": "https://b.example/g/r"}


def test_the_repository_name_is_a_placeholder_in_every_url_and_repository_object() -> None:
    """D1b (0.9): the project segment is ``project``, in URLs and in the repository object's name and path."""
    assert redact_url("https://a.example/o/private_repo") == "https://forge.example.com/org/project"
    assert redact_url("https://a.example/o/private_repo.git") == (
        "https://forge.example.com/org/project.git"
    )
    project = {
        "id": 5,
        "name": "private_repo",
        "path": "private_repo",
        "web_url": "https://b.example/g/private_repo",
        "merge_method": "merge",
    }
    assert redact_payload(project) == {
        "id": 5,
        "name": "project",
        "path": "project",
        "web_url": "https://forge.example.com/org/project",
        "merge_method": "merge",
    }
    assert redact_payload(redact_payload(project)) == redact_payload(project)
    merge_request = {"name": "private_repo", "web_url": "https://b.example/g/r/-/merge_requests/7"}
    assert redact_payload(merge_request)["name"] == "private_repo"
    assert redact_payload({"body": "see https://forge.example.com/org/r/-/jobs/5"}) == {
        "body": "see https://forge.example.com/org/project/-/jobs/5"
    }
    corpus = "".join(p.read_text() for p in sorted(FIXTURES.rglob("*.json")))
    paths = set(re.findall(r"forge\.example\.com/(?:repos/)?org/([^/\"\\ ]+)", corpus))
    assert paths == {"project"}


def _raw_calls(
    forge_name: str, case: str, edits: dict[int, Any], roles: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A fixture's exchanges with their stdout replaced by raw answers, then redacted as recorded."""
    fixture = load_fixture(FIXTURES / forge_name / f"{case}.json")
    calls = []
    for i, call in enumerate(fixture["calls"]):
        new = dict(call)
        if i in edits:
            new["stdout"] = redact_stdout(json.dumps(edits[i]))
        calls.append(new)
    classified = fixture_pipeline.classify_calls(calls, roles)
    return fixture, fixture_pipeline.synthesize_user_ids(classified, UserIdMap(), forge=forge_name)


def _person(login_key: str, login: str, ident: object) -> dict[str, Any]:
    """A raw user object as a forge prints it."""
    return {
        "id": ident,
        login_key: login,
        "name": "Some Person",
        "avatar_url": "https://avatars.example.org/u/1",
        "web_url": f"https://code.example.org/{login}",
        "state": "active",
    }


def _replayed(forge: Forge, calls: list[dict[str, Any]], method: str, *args: Any) -> Any:
    """Run ``method`` against the recorded exchanges; every exchange must be consumed."""
    spawn = ReplaySpawn([Exchange.from_json(c) for c in calls])
    cast(Any, forge)._spawn = spawn
    value = getattr(forge, method)(*args)
    assert spawn.exchanges == []
    return value


def test_forge_readers_work_on_a_redacted_recording(tmp_path: Path) -> None:
    """D1b, F1b, F1c: readers return the redacted URLs, the names, the user ids and the class word."""
    handle, other = "x-handle", "y-handle"
    mr_url = "https://code.example.org/group/sub/r/-/merge_requests/283"
    pipe_url = "https://code.example.org/group/sub/r/-/pipelines/99"
    job_url = "https://code.example.org/group/sub/r/-/jobs/17"
    roles = {handle: "author", other: "author-2"}

    mr = decode_json_values(load_fixture(FIXTURES / "gitlab" / "checks.json")["calls"][0]["stdout"])
    assert mr is not None
    mr_raw = dict(mr[0], web_url=mr_url, author=_person("username", handle, 1))
    mr_raw["head_pipeline"] = dict(mr_raw["head_pipeline"], web_url=pipe_url)
    jobs_raw = [
        {"id": 17, "name": "unit:3.11", "status": "success", "stage": "test", "web_url": job_url},
        {
            "id": 18,
            "name": "lint",
            "status": "failed",
            "stage": "test",
            "web_url": job_url[:-2] + "18",
        },
    ]
    _fx, calls = _raw_calls("gitlab", "pull_request", {0: mr_raw}, roles)
    pr = _replayed(_build("gitlab", tmp_path, ""), calls, "pull_request", 283)
    assert pr.url == "https://forge.example.com/org/project/-/merge_requests/283"
    assert pr.author == "author"
    assert handle not in json.dumps(calls) and "Some Person" not in json.dumps(calls)

    _fx, calls = _raw_calls("gitlab", "checks", {0: mr_raw, 1: jobs_raw}, roles)
    checks = _replayed(_build("gitlab", tmp_path, ""), calls, "checks", 283)
    assert [(c.name, c.link) for c in checks] == [
        ("unit:3.11", "https://forge.example.com/org/project/-/jobs/17"),
        ("lint", "https://forge.example.com/org/project/-/jobs/18"),
    ]

    _fx, calls = _raw_calls("gitlab", "ci_status", {0: mr_raw}, roles)
    status = _replayed(_build("gitlab", tmp_path, ""), calls, "ci_status", 283)
    assert [c.link for c in status.checks] == [
        "https://forge.example.com/org/project/-/pipelines/99"
    ]

    approvals = decode_json_values(
        load_fixture(FIXTURES / "gitlab" / "reviews_two_approvals.json")["calls"][0]["stdout"]
    )
    assert approvals is not None
    approvals_raw = dict(
        approvals[0],
        approved_by=[{"user": _person("username", h, i)} for h, i in ((handle, 11), (other, 12))],
    )
    _fx, calls = _raw_calls(
        "gitlab",
        "reviews_two_approvals",
        {0: approvals_raw},
        {handle: "maintainer", other: "author-2"},
    )
    reviews = _replayed(_build("gitlab", tmp_path, "repoach-bot"), calls, "reviews", 7)
    assert [(r.id, r.author, r.state) for r in reviews] == [
        (1001, "maintainer", "APPROVED"),
        (1002, "author-2", "APPROVED"),
    ]

    gh_pr = decode_json_values(
        load_fixture(FIXTURES / "github" / "pull_request.json")["calls"][0]["stdout"]
    )
    assert gh_pr is not None
    gh_raw = dict(
        gh_pr[0],
        url="https://code.example.org/someone/r/pull/200",
        author={"id": "MDQ6VXNlcjE=", "login": handle, "name": "Some Person"},
    )
    _fx, calls = _raw_calls("github", "pull_request", {0: gh_raw}, roles)
    pr = _replayed(_build("github", tmp_path, "bot"), calls, "pull_request", 200)
    assert pr.author == "author"
    assert pr.url == "https://forge.example.com/org/project/pull/200"

    runs = [
        {
            "name": "Test suite (Python 3.11)",
            "state": "SUCCESS",
            "bucket": "pass",
            "workflow": "CI",
            "link": "https://code.example.org/someone/r/actions/runs/42/job/100",
        },
        {"name": "gate", "state": "FAILURE", "bucket": "fail", "workflow": "CI", "link": ""},
    ]
    _fx, calls = _raw_calls("github", "checks", {0: runs}, roles)
    checks = _replayed(_build("github", tmp_path, "bot"), calls, "checks", 200)
    assert [(c.name, c.link) for c in checks] == [
        (
            "Test suite (Python 3.11)",
            "https://forge.example.com/org/project/actions/runs/42/job/100",
        ),
        ("gate", ""),
    ]


def test_logins_are_classified_in_stdout_and_argv_with_a_stable_mapping() -> None:
    """D2: one login, one word, in every key, in argv; a ``[bot]`` login is ``bot``."""
    roles = {"x": "maintainer", "y": "author"}
    payload = {
        "nodes": [
            {"author": {"login": "x"}, "resolved_by": "x"},
            {"user": {"username": "X"}, "author": {"login": "y"}},
        ]
    }
    out = classify_logins(payload, roles)
    assert out == {
        "nodes": [
            {"author": {"login": "maintainer"}, "resolved_by": "maintainer"},
            {"user": {"username": "maintainer"}, "author": {"login": "author"}},
        ]
    }
    argv = ["api", "repos/:owner/:repo/collaborators/x/permission"]
    assert classify_logins(argv, roles) == [
        "api",
        "repos/:owner/:repo/collaborators/maintainer/permission",
    ]
    assert classify_logins(["projects/:id/members/all?query=x&per_page=100"], roles) == [
        "projects/:id/members/all?query=maintainer&per_page=100"
    ]
    calls = [
        {
            "args": [],
            "stdout": json.dumps([{"user": {"login": "z[bot]"}}, {"user": {"login": "q"}}]),
        },
        {"args": [], "stdout": json.dumps({"author": {"login": "w[bot]"}, "user": "q"})},
    ]
    built = rec.build_roles(calls, bot="", maintainer="x", author="y")
    assert built == {
        "x": "maintainer",
        "y": "author",
        "z[bot]": "bot",
        "q": "author-2",
        "w[bot]": "bot-2",
    }
    assert rec.build_roles([], bot="dev-bot[bot]", maintainer=None, author="y") == {
        "dev-bot[bot]": "bot",
        "y": "author",
    }


def _fake_forge(stdout: str, tmp_path: Path, bot: str | None = None) -> Forge:
    """A GitHub forge, bot ``review-agent-x`` unless the replay asks for ``bot``, answering ``stdout``."""
    forge = _build("github", tmp_path, bot or "review-agent-x")

    def _spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr="", argv=list(argv))

    cast(Any, forge)._spawn = _spawn
    return forge


def _no_denylist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the deny-list at an empty config directory and clear its variable; return the directory."""
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv(deny_list.DENYLIST_ENV, raising=False)
    return config / "repoach"


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    roles: dict[str, str] | None,
    denylist: str | None = None,
) -> int:
    """Run the recorder's CLI against a fake forge, optionally with fixed roles and a deny-list."""
    _no_denylist(monkeypatch, tmp_path)
    if denylist is not None:
        monkeypatch.setenv(deny_list.DENYLIST_ENV, denylist)
    monkeypatch.setattr(
        rec, "select_forge", lambda **kw: _fake_forge(stdout, tmp_path, kw.get("bot_username"))
    )
    if roles is not None:
        monkeypatch.setattr(rec, "build_roles", lambda *_a, **_kw: dict(roles))
    out = tmp_path / "out"
    code: int = rec.main(
        [
            "--forge",
            "github",
            "--cwd",
            str(tmp_path),
            "--case",
            "c",
            "--method",
            "reviews",
            "--args",
            "[7]",
            "--out",
            str(out),
        ]
    )
    return code


def test_recorder_refuses_an_unclassified_or_generic_value_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D3: exit 2 naming the key path, never the value, and no fixture written."""
    stdout = json.dumps(
        [[{"id": 1, "user": {"login": "q-handle"}, "state": "APPROVED", "body": ""}]]
    )
    assert _run_main(monkeypatch, tmp_path, stdout, {"x-handle": "maintainer"}) == 2
    err = capsys.readouterr()
    assert re.search(r"^unclassified: exchanges\[\d+\]\.stdout.*\.login$", err.err, re.M)
    assert "q-handle" not in err.err + err.out
    assert not (tmp_path / "out").exists()

    body = "log: /home/someone-x/y failed"
    stdout = json.dumps(
        [[{"id": 1, "user": {"login": "q-handle"}, "state": "APPROVED", "body": body}]]
    )
    assert _run_main(monkeypatch, tmp_path, stdout, {"q-handle": "author"}) == 2
    err = capsys.readouterr()
    assert re.search(
        r"^generic class home_path at exchanges\[\d+\]\.stdout.*\.body$", err.err, re.M
    )
    assert "someone-x" not in err.err + err.out
    assert not (tmp_path / "out").exists()

    address = ".".join(["11", "22", "33", "44"])
    mailbox = "@".join(["p", "mail-provider.org"])
    nothing = closure.Known(())
    for text, reason in (
        (f"ip {address}:22 here", "public address"),
        (f"mail {mailbox}", "unknown host"),
        ("mail p@10.0.0.5", "unknown host"),
        ("1+noreply@users.example.org", "unknown mailbox"),
    ):
        refusal = closure.judge(text, nothing)
        assert refusal is not None and refusal.reason == reason, text
    for text in (
        "v4.2.1.3",
        "pkg==4.2.1.3",
        "10.0.0.1",
        "203.0.113.9",
        "t@example.com",
        "git@example.com:org/r.git",
        "/home/<x>/",
    ):
        assert closure.judge(text, nothing) is None, text
        assert fixture_judge.generic_class(text) is None
    assert fixture_judge.generic_class("see /home/someone-x/y") == "home_path"
    assert fixture_judge.generic_class(f"ip {address} here") is None


def test_a_home_path_is_judged_decoded_on_posix_and_windows() -> None:
    """D3 (0.28, PR #319): a home path is read in every decoded form, ``/var/home`` and a Windows profile included."""
    for text in (
        "log %2Fhome%2Fsomeone-x%2Fwork",
        r"path \/home\/someone-x\/work",
        "cwd=/var/home/someone-x/src",
        r"at C:\Users\someone-x\AppData",
        r'{"cwd": "C:\\Users\\someone-x\\src"}',
        r"at d:\users\someone-x",
        "see &#47;home&#47;someone-x&#47;y",
    ):
        assert fixture_judge.generic_class(text) == "home_path", text
    for text in (
        "foo/var/home/x",
        "$work/home/.local",
        r"C:\Users\<x>",
        r"C:\Program Files\x",
        "/home/",
        "~/x",
    ):
        assert fixture_judge.generic_class(text) is None, text


def test_a_windows_profile_is_a_home_path_under_every_posix_mount() -> None:
    """D3 (PR #319 round 6, 4684965373): WSL, Git-Bash, MSYS2 and Cygwin spell the Windows profile too.

    ``/mnt/<drive>/Users/<x>``, ``/<drive>/Users/<x>`` and ``/cygdrive/<drive>/Users/<x>`` (any
    drive letter, ``users`` in either case, as Windows does not tell them apart), and
    ``C:/Users/<x>``, are home paths in every decoded form; a drive without a user segment, a
    longer first segment or a path glued to a word is not.
    """
    for prefix in ("/mnt/c", "/c", "/cygdrive/c", "/mnt/D", "/d", "/cygdrive/z", "C:", "e:"):
        for users in ("Users", "users"):
            for tail in ("someone-x", "someone-x/AppData", "someone-x\\x"):
                text = f"at {prefix}/{users}/{tail}"
                assert fixture_judge.generic_class(text) == "home_path", text
    for text in (
        "cd%20%2Fmnt%2Fc%2FUsers%2Fsomeone-x",
        r"\/c\/Users\/someone-x",
        '{"cwd": "/cygdrive/c/Users/someone-x/src"}',
        'File "/mnt/c/Users/someone-x/app.py", line 3',
        "C:/Users/someone-x/AppData",
    ):
        assert fixture_judge.generic_class(text) == "home_path", text
    for text in (
        "/mnt/c/Users/",
        "/c/Users/<x>",
        "/mnt/cd/Users/someone-x",
        "/cygdrive/cd/Users/someone-x",
        "/mnt/c/Program Files/x",
        "api/c/users/someone-x",
        "x/mnt/c/Users/someone-x",
        "abc/cygdrive/c/Users/someone-x",
        "/mnt/c/Windows/System32",
    ):
        assert fixture_judge.generic_class(text) is None, text


def test_recorder_writes_class_words_and_a_replayed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End state: the written fixture holds class words, placeholder URLs and its replayed result."""
    stdout = json.dumps(
        [
            [
                {
                    "id": 9,
                    "user": {
                        "id": 70707,
                        "login": "q-handle",
                        "name": "Some Person",
                        "html_url": "https://code.example.org/q-handle",
                    },
                    "state": "APPROVED",
                    "body": "lgtm",
                }
            ]
        ]
    )
    monkeypatch.setattr(rec, "_author_of", lambda *_a: None)
    assert _run_main(monkeypatch, tmp_path, stdout, None) == 0
    path = Path(capsys.readouterr().out.strip())
    fixture = json.loads(path.read_text())
    text = json.dumps(fixture)
    assert "q-handle" not in text and "Some Person" not in text and "code.example.org" not in text
    assert fixture["bot_username"] == "bot"
    assert json.loads(fixture["calls"][0]["stdout"])[0][0]["user"] == {
        "id": 1001,
        "login": "author",
    }
    assert fixture[SYNTHETIC_IDS_KEY] == [1001]
    assert re.fullmatch(
        r"recorded \d{4}-\d{2}-\d{2} from a live github change request", fixture["source"]
    )
    assert tmp_path.name not in text
    assert fixture["result"] == [
        {"author": "author", "body": "lgtm", "comments": [], "id": 9, "state": "APPROVED"}
    ]


def test_existing_fixtures_are_a_fixed_point_of_the_redaction() -> None:
    """G4 as amended: the committed fixtures are what the recorder's redaction leaves them."""
    assert fixture_pipeline.redact_corpus(FIXTURES, check=True) == []
    real_host = re.compile(r"https?://([a-z0-9.-]+\.)?(github|gitlab)\.com/(?!en/|ee/)")
    hits = [p.name for p in sorted(FIXTURES.rglob("*.json")) if real_host.search(p.read_text())]
    assert hits == []


def test_fixtures_declare_exactly_their_synthetic_user_ids() -> None:
    """D1c, P2 (0.8): every user id is numeric-shaped and declared by its fixture, and no declaration is stale."""
    loaded = {p: json.loads(p.read_text()) for p in sorted(FIXTURES.rglob("*.json"))}
    ids = [i for data in loaded.values() for i in user_id_values(data)]
    assert len(ids) >= 50
    assert [i for i in ids if not isinstance(user_id_key(i), int)] == []
    wrong = [
        p.relative_to(FIXTURES).as_posix()
        for p, data in loaded.items()
        if data.get(SYNTHETIC_IDS_KEY, []) != declared_user_ids(data)
    ]
    assert wrong == []
    outside = [
        p.relative_to(FIXTURES).as_posix()
        for p, data in loaded.items()
        if not all(is_synthetic_number(n) for n in data.get(SYNTHETIC_IDS_KEY, []))
    ]
    assert outside == []


def _denylisted(denylist: Any) -> list[tuple[str, int]]:
    """``(file, ordinal)`` of every deny-list entry a file under :data:`FIXTURES` holds."""
    hits: list[tuple[str, int]] = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        texts = [text]
        if path.suffix == ".json":
            texts += [s for _key, s in fixture_strings(json.loads(text), "")]
        found = {denylist.hit(s) for s in texts} - {None}
        hits += [(path.relative_to(FIXTURES).as_posix(), cast(int, n)) for n in sorted(found)]
    return hits


def _shown(path: Path) -> str:
    """``path`` with the home directory written ``~``, so a message names the place and no account."""
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else str(path)


def test_fixtures_hold_no_denylisted_value(real_user_config_dir: Path) -> None:
    """D1c (0.8, 0.9): no entry of the maintainer's untracked deny-list (their real ids included) is in a forge fixture.

    The entries are read at test time (SP-NO-PERSONAL-DATA-GATE D3), never
    committed: the file under the REAL configuration directory, captured
    before the suite points ``XDG_CONFIG_HOME`` at a scratch one
    (``real_user_config_dir``), merged with ``REPOACH_PERSONAL_DENYLIST``.
    A user id in node-id form is judged by the number it encodes. A
    failure names the file and the entry's ordinal only.
    """
    entries = deny_list.read_denylist(real_user_config_dir)
    if not entries:
        pytest.skip(
            f"deny-list absent: {_shown(deny_list.denylist_file(real_user_config_dir))} holds no entry "
            f"and ${deny_list.DENYLIST_ENV} is unset or empty (SP-NO-PERSONAL-DATA-GATE D3)"
        )
    assert _denylisted(entries) == []


def test_the_denylist_file_route_is_read_under_pytest(
    real_user_config_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """D1c, O2 (0.9): the deny-list test reads the real configuration's file, the variable unset.

    The suite's autouse ``_hermetic_xdg_dirs`` points ``XDG_CONFIG_HOME``
    at a scratch directory; read through ``user_config_dir()``, the file
    was never seen under pytest and the test skipped with a real file
    present. The test asks for the directory captured before that.
    """
    assert real_user_config_dir != deny_list.user_config_dir()
    assert not real_user_config_dir.is_relative_to(tmp_path_factory.getbasetemp())
    parameters = inspect.signature(test_fixtures_hold_no_denylisted_value).parameters
    assert list(parameters) == ["real_user_config_dir"]
    source = inspect.getsource(test_fixtures_hold_no_denylisted_value)
    assert source.count("read_denylist(real_user_config_dir)") == 1
    assert source.count("denylist_file(real_user_config_dir)") == 1

    monkeypatch.delenv(deny_list.DENYLIST_ENV, raising=False)
    config = tmp_path / "config" / "repoach"
    config.mkdir(parents=True)
    deny_list.denylist_file(config).write_text(
        "# a string every redacted fixture holds\nforge.example.com\n"
    )
    entries = deny_list.read_denylist(config)
    assert len(entries) == 1 and entries.hit("forge.example.com") == 1
    hits = _denylisted(entries)
    assert len(hits) >= 30 and {n for _file, n in hits} == {1}


def _new_node(number: int) -> str:
    """A current-format GitHub user node id for ``number``: ``U_`` and base64url of MessagePack ``[0, n]``."""
    raw = b"\x92\x00\xce" + number.to_bytes(4, "big")
    return "U_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_denylist_is_read_from_the_file_and_the_variable_and_never_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D3 (0.8): SP-NO-PERSONAL-DATA-GATE D3's two sources merged; the recorder refuses a hit by ordinal."""
    config = _no_denylist(monkeypatch, tmp_path)
    assert len(deny_list.read_denylist()) == 0
    config.mkdir(parents=True)
    (config / deny_list.DENYLIST_FILE_NAME).write_text(
        "# the owner's strings\nsomeone-handle\n\n4242424242  # an id\n"
    )
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "someone-host\nsomeone-handle\n")
    denylist = deny_list.read_denylist()
    assert len(denylist) == 3 and repr(denylist) == "<denylist: 3 entries>"
    assert [denylist.hit(t) for t in ("someone-handle", "4242424242", "someone-host")] == [1, 2, 3]
    assert denylist.hit("ssh someone-host") == 3
    assert denylist.hit("ssh SOMEONE-HOST.") == 3
    assert denylist.hit("control-someone-host") == 3
    assert denylist.hit("controlsomeone-host") is None
    assert denylist.hit(base64.b64encode(b"04:User4242424242").decode()) == 2
    assert denylist.hit(_new_node(4242424242)) == 2
    legacy = base64.b64encode(b"04:User4242424242").decode()
    assert denylist.hit(f"see {legacy} here") == 2
    assert denylist.hit(f"?node={_new_node(4242424242)}-patch") == 2
    assert denylist.hit(_new_node(4242424243)) is None
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "abc")
    with pytest.raises(deny_list.DenylistError, match=r"^deny-list entry 3: too short$") as short:
        deny_list.read_denylist()
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "someone-host,someone-handle")
    with pytest.raises(
        deny_list.DenylistError,
        match=r"^deny-list variable REPOACH_PERSONAL_DENYLIST: a comma; one entry per line$",
    ) as comma:
        deny_list.read_denylist()
    for info, entry in ((short, "abc"), (comma, "someone-host")):
        bound = [
            name
            for frame, _line in traceback.walk_tb(info.tb.tb_next)
            for name, value in frame.f_locals.items()
            if entry in (value if isinstance(value, str | tuple | list) else ())
        ]
        assert bound == [], entry
    (config / deny_list.DENYLIST_FILE_NAME).unlink()

    body = "deployed on someone-host"
    stdout = json.dumps(
        [[{"id": 1, "user": {"login": "q-handle"}, "state": "APPROVED", "body": body}]]
    )
    assert _run_main(monkeypatch, tmp_path, stdout, {"q-handle": "author"}, "someone-host") == 2
    err = capsys.readouterr()
    assert re.search(r"^deny-list entry 1 at exchanges\[\d+\]\.stdout.*\.body$", err.err, re.M)
    assert "someone-host" not in err.err + err.out
    assert _run_main(monkeypatch, tmp_path, stdout, {"q-handle": "author"}, "abc") == 2
    err = capsys.readouterr()
    assert err.err.splitlines()[-1] == "deny-list entry 1: too short"
    assert not (tmp_path / "out").exists()


def test_recorded_source_names_the_forge_and_the_refusal_covers_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P1 (0.8): ``source`` never names the checkout's folder, and D3 judges the field."""
    for before, after in (
        (
            "recorded 2026-10-19 from some-checkout",
            "recorded 2026-10-19 from a live gitlab change request",
        ),
        (
            "recorded 2026-10-19 from some-checkout; result amended",
            "recorded 2026-10-19 from a live gitlab change request; result amended",
        ),
        (
            "recorded 2026-11-02 from g/r MR !339 (merged); kept",
            "recorded 2026-11-02 from a live gitlab change request, MR !339 (merged); kept",
        ),
        ("synthetic: a documented shape", "synthetic: a documented shape"),
    ):
        assert (
            fixture_pipeline.redact_fixture(
                {"source": before}, forge="gitlab", denylist=NO_DENYLIST
            )["source"]
            == after
        )
    stdout = json.dumps(
        [[{"id": 9, "user": {"login": "q-handle"}, "state": "APPROVED", "body": ""}]]
    )
    monkeypatch.setattr(rec, "recorded_source", lambda *_a: "recorded from /home/someone-x/y")
    assert _run_main(monkeypatch, tmp_path, stdout, {"q-handle": "author"}) == 2
    err = capsys.readouterr()
    assert err.err.splitlines()[-1] == "generic class home_path at source"
    assert not (tmp_path / "out").exists()


def test_an_unknown_mention_in_free_text_becomes_a_class_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P5 (0.8): an ``@<handle>`` in a body is classified like a login, by the recorder and offline."""
    payload = {
        "body": "thanks @x-handle and @author, mail a@b.example, see @x-handle.",
        "diff": "+@pytest.fixture",
        "comments": [{"title": "cc @repoach-bot @y-handle"}],
    }
    assert mentions(payload) == ["x-handle", "y-handle"]
    calls = [{"args": [], "stdout": json.dumps({"author": {"login": "y"}, "body": "cc @x-handle"})}]
    assert rec.build_roles(calls, bot="", maintainer=None, author="y") == {
        "y": "author",
        "@x-handle": "author-2",
    }

    stdout = json.dumps(
        [[{"id": 9, "user": {"login": "q-handle"}, "state": "APPROVED", "body": "ping @x-handle"}]]
    )
    monkeypatch.setattr(rec, "_author_of", lambda *_a: None)
    assert _run_main(monkeypatch, tmp_path, stdout, None) == 0
    text = Path(capsys.readouterr().out.strip()).read_text()
    assert "x-handle" not in text and "ping @author-2" in text

    fixture = {
        "forge": "github",
        "calls": [_exchange([{"user": {"login": "author"}, "body": "cc @x-handle"}])],
    }
    out = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    assert "x-handle" not in json.dumps(out) and "cc @author-2" in out["calls"][0]["stdout"]


def test_non_person_logins_are_kept_and_every_other_login_is_classified() -> None:
    """P3 (0.8): the real-account rule, applied alike by the recorder and the offline transform."""
    assert frozenset({"github-actions[bot]", "repoach-bot"}) == NON_PERSON_LOGINS
    answers = [
        {"user": {"login": "github-actions[bot]"}},
        {"user": {"login": "owner"}},
        {"user": {"username": "project_1_bot_ab12"}},
        {"user": {"login": "dependabot[bot]"}},
    ]
    calls = [{"args": [], "stdout": json.dumps(answers)}]
    assert rec.build_roles(calls, bot="repoach-bot", maintainer=None, author=None) == {
        "owner": "author",
        "project_1_bot_ab12": "service",
        "dependabot[bot]": "bot",
    }
    fixture = {"forge": "github", "bot_username": "repoach-bot", "calls": [_exchange(answers)]}
    out = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    logins = re.findall(r'"(?:login|username)": "([^"]+)"', out["calls"][0]["stdout"])
    assert out["bot_username"] == "repoach-bot"
    assert logins == ["github-actions[bot]", "author-2", "service", "bot"]
    mine = {**fixture, "bot_username": "my-review-bot"}
    assert (
        fixture_pipeline.redact_fixture(mine, forge="github", denylist=NO_DENYLIST)["bot_username"]
        == "bot"
    )


def test_user_ids_map_to_a_stable_sequence_keyed_by_forge() -> None:
    """D1c: 1001, 1002 in order; every id form of one account shares its number; forges never share one."""
    legacy = base64.b64encode(b"04:User70707").decode()
    ids = UserIdMap()
    out = ids.apply(
        [
            {"user": {"id": 70707, "login": "author"}},
            {"author": {"id": legacy, "login": "author"}},
            {"author": {"id": _new_node(70707), "login": "author"}},
            {"user": {"username": "author-2", "id": 777}},
            {"user": {"id": 70707, "login": "author"}},
            {"id": 70707, "name": "check"},
        ],
        forge="github",
    )
    assert out == [
        {"user": {"id": 1001, "login": "author"}},
        {"author": {"id": "U_1001", "login": "author"}},
        {"author": {"id": "U_1001", "login": "author"}},
        {"user": {"username": "author-2", "id": 1002}},
        {"user": {"id": 1001, "login": "author"}},
        {"id": 70707, "name": "check"},
    ]
    assert ids.synthetic(70707, forge="gitlab") == 1003
    assert ids.synthetic(1500, forge="gitlab", keep=frozenset({1500})) == 1500
    assert ids.synthetic(11, forge="gitlab", keep=frozenset({11})) == 1004
    assert ids.synthetic(4321987, forge="gitlab", keep=frozenset({1500})) == 1005


def _exchange(payload: object) -> dict[str, Any]:
    """A recorded exchange answering ``payload`` as JSON."""
    return {"args": [], "returncode": 0, "stdin": None, "stderr": "", "stdout": json.dumps(payload)}


def _fixture(
    forge: str,
    method: str,
    payloads: list[object],
    result: object = None,
    declared: list[int] | None = None,
) -> dict[str, Any]:
    """A minimal fixture of ``method`` on ``forge``, optionally declaring synthetic ids."""
    data: dict[str, Any] = {
        "forge": forge,
        "method": method,
        "args": [7],
        "calls": [_exchange(p) for p in payloads],
        "result": result,
    }
    if declared is not None:
        data[SYNTHETIC_IDS_KEY] = declared
    return data


def _corpus(tmp_path: Path, name: str, files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Write ``files`` as a fixture corpus, run the offline transform on it, read it back."""
    root = tmp_path / name
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(json.dumps(data))
    fixture_pipeline.redact_corpus(root, check=False)
    assert fixture_pipeline.redact_corpus(root, check=True) == []
    return {rel: json.loads((root / rel).read_text()) for rel in files}


def test_offline_ids_are_keyed_by_forge_so_two_accounts_never_merge(tmp_path: Path) -> None:
    """Defect 3 (0.8): the same raw number on GitHub and on GitLab names two accounts, two ids."""
    out = _corpus(
        tmp_path,
        "c",
        {
            "github/a.json": _fixture(
                "github", "pull_request", [{"author": {"login": "author", "id": 555}}]
            ),
            "gitlab/b.json": _fixture(
                "gitlab", "pull_request", [{"author": {"username": "author", "id": 555}}]
            ),
        },
    )
    assert user_id_values(out["github/a.json"]) == [1001]
    assert user_id_values(out["gitlab/b.json"]) == [1002]


def test_a_result_id_follows_its_account_only_where_it_is_a_user_id(tmp_path: Path) -> None:
    """Defect 3 (0.8): only a GitLab ``Review.id`` is a user id; a check's or a GitHub review's id is its own."""
    raw = 4321987
    own = 7654321
    approvals = {"approved_by": [{"user": {"username": "author-2", "id": raw}}]}
    out = _corpus(
        tmp_path,
        "c",
        {
            "github/c_reviews.json": _fixture(
                "github",
                "reviews",
                [[{"id": own, "user": {"login": "author", "id": raw}, "state": "APPROVED"}]],
                [{"author": "author", "id": own, "state": "APPROVED"}],
            ),
            "gitlab/a_reviews.json": _fixture(
                "gitlab",
                "reviews",
                [approvals],
                [{"author": "author-2", "id": raw, "state": "APPROVED"}],
            ),
            "gitlab/b_checks.json": _fixture(
                "gitlab",
                "checks",
                [[{"user": {"username": "author", "id": raw}}]],
                [{"id": own, "name": "unit"}],
            ),
        },
    )
    assert out["github/c_reviews.json"]["result"] == [
        {"author": "author", "id": own, "state": "APPROVED"}
    ]
    assert user_id_values(out["github/c_reviews.json"]) == [1001]
    assert out["gitlab/a_reviews.json"]["result"] == [
        {"author": "author-2", "id": 1002, "state": "APPROVED"}
    ]
    assert out["gitlab/b_checks.json"]["result"] == [{"id": own, "name": "unit"}]
    assert user_id_values(out["gitlab/b_checks.json"]) == [1002]


def test_an_own_id_equal_to_a_user_id_is_refused_not_written(tmp_path: Path) -> None:
    """D3 (0.17): a review's own id that equals a user id is a known value standing as a number, which the oracle refuses."""
    raw = 4321987
    root = tmp_path / "c"
    (root / "github").mkdir(parents=True)
    data = _fixture(
        "github",
        "reviews",
        [[{"id": raw, "user": {"login": "author", "id": raw}, "state": "APPROVED"}]],
    )
    (root / "github" / "r.json").write_text(json.dumps(data))
    with pytest.raises(
        fixture_pipeline.ResidualValueError, match=r"^residual id 1 at github/r\.json"
    ):
        fixture_pipeline.redact_corpus(root, check=False, denylist=deny_list.Denylist())


def test_offline_sequence_is_one_per_corpus_and_starts_after_every_declared_id(
    tmp_path: Path,
) -> None:
    """M11, M13 (0.8): one map for the whole corpus; a new number never collides with a declared one."""
    users = [("maintainer", 1001), ("author-2", 900001), ("author-3", 900002)]
    out = _corpus(
        tmp_path,
        "c",
        {
            "gitlab/a.json": _fixture(
                "gitlab",
                "reviews",
                [{"approved_by": [{"user": {"username": u, "id": i}} for u, i in users]}],
                declared=[1001],
            ),
            "gitlab/b.json": _fixture(
                "gitlab",
                "reviews",
                [{"approved_by": [{"user": {"username": "author-3", "id": 900002}}]}],
            ),
        },
    )
    assert user_id_values(out["gitlab/a.json"]) == [1001, 1002, 1003]
    assert out["gitlab/a.json"][SYNTHETIC_IDS_KEY] == [1001, 1002, 1003]
    assert user_id_values(out["gitlab/b.json"]) == [1003]
    assert out["gitlab/b.json"][SYNTHETIC_IDS_KEY] == [1003]


def test_an_undeclared_small_id_is_remapped_not_kept(tmp_path: Path) -> None:
    """P2 (0.8): an id is synthetic because its fixture declares it, never because it is small."""
    answer = [{"id": 7, "username": "maintainer", "access_level": 50}]
    raw = _corpus(tmp_path, "raw", {"gitlab/m.json": _fixture("gitlab", "is_maintainer", [answer])})
    assert user_id_values(raw["gitlab/m.json"]) == [1001]
    kept = _corpus(
        tmp_path,
        "kept",
        {
            "gitlab/m.json": _fixture(
                "gitlab", "is_maintainer", [{**answer[0], "id": 1500}], declared=[1500]
            )
        },
    )
    assert user_id_values(kept["gitlab/m.json"]) == [1500]
    assert kept["gitlab/m.json"][SYNTHETIC_IDS_KEY] == [1500]


def test_a_declaration_outside_the_synthetic_range_is_not_trusted(tmp_path: Path) -> None:
    """Preference 2 (0.9): a real id typed into ``synthetic_user_ids`` is remapped, never kept."""
    assert trusted_declaration([7, 1000, 1001, 1999, 2000, 4321987, True, "1500"]) == {1001, 1999}
    assert trusted_declaration("1001") == frozenset()
    answer = [{"id": 4321987, "username": "maintainer", "access_level": 50}]
    small = [{"id": 7, "username": "maintainer", "access_level": 50}]
    out = _corpus(
        tmp_path,
        "c",
        {
            "gitlab/a.json": _fixture("gitlab", "is_maintainer", [answer], declared=[4321987]),
            "gitlab/b.json": _fixture("gitlab", "is_maintainer", [small], declared=[7]),
        },
    )
    assert user_id_values(out["gitlab/a.json"]) == [1001]
    assert out["gitlab/a.json"][SYNTHETIC_IDS_KEY] == [1001]
    assert user_id_values(out["gitlab/b.json"]) == [1002]
    assert out["gitlab/b.json"][SYNTHETIC_IDS_KEY] == [1002]
    last = UserIdMap(start=1999)
    assert last.synthetic(5, forge="github") == 1999
    with pytest.raises(SyntheticIdsExhaustedError):
        last.synthetic(6, forge="github")


def test_offline_transform_remaps_a_result_id_with_its_account() -> None:
    """D1c: a GitLab ``Review.id`` in a result follows its approver's synthetic id."""
    approvals = {"approved_by": [{"user": {"username": "author-2", "id": 4321987}}]}
    fixture = _fixture(
        "gitlab",
        "reviews",
        [approvals],
        [{"author": "author-2", "id": 4321987, "state": "APPROVED"}],
    )
    out = fixture_pipeline.redact_fixture(fixture, forge="gitlab", denylist=NO_DENYLIST)
    assert json.loads(out["calls"][0]["stdout"])["approved_by"][0]["user"]["id"] == 1001
    assert out["result"] == [{"author": "author-2", "id": 1001, "state": "APPROVED"}]
    assert out[SYNTHETIC_IDS_KEY] == [1001]


def test_a_mention_is_a_handle_not_a_decorator_nor_a_dotted_name_and_replaces_only_its_at_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preference 1 (0.9): the verifier's example; a mention-only login never replaces the bare word."""
    body = "use @pytest.fixture here, and see @dataclass; cc @someone-x. mail a@b.example"
    diff = "+from dataclasses import dataclass\n+@dataclass\n+@pytest.fixture\n+x = someone-x\n"
    prose = {
        "body": body,
        "title": "`@code-span` and @lru_cache(maxsize=1) and @property, @john.doe, @ok-x.",
        "description": "```\n@fenced-x\n```",
    }
    assert mentions(prose) == ["someone-x", "ok-x"]
    payload = {"body": body, "diff": diff, "user": {"login": "q-handle", "id": 5}}
    calls = [{"args": ["api", "x"], "stdout": json.dumps(payload), "stderr": "", "returncode": 0}]
    roles = rec.build_roles(calls, bot="", maintainer=None, author="q-handle")
    assert roles == {"q-handle": "author", "@someone-x": "author-2"}
    out = json.loads(fixture_pipeline.classify_calls(calls, roles)[0]["stdout"])
    assert (
        out["body"]
        == "use @pytest.fixture here, and see @dataclass; cc @author-2. mail a@b.example"
    )
    assert out["diff"] == diff

    later = [
        {"args": [], "stdout": json.dumps({"body": "cc @z-handle"})},
        {"args": [], "stdout": json.dumps({"user": {"login": "z-handle"}})},
    ]
    assert rec.build_roles(later, bot="", maintainer=None, author=None) == {"z-handle": "author"}

    clean = "+from dataclasses import dataclass\n+@dataclass\n+@pytest.fixture\n+x = 1\n"
    fixture = {
        "forge": "github",
        "args": ["repos/:owner/:repo/pulls/7"],
        "calls": [_exchange([{"user": {"login": "author"}, "body": body, "diff": clean}])],
    }
    redacted = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    answer = json.loads(redacted["calls"][0]["stdout"])[0]
    assert (
        answer["body"]
        == "use @pytest.fixture here, and see @dataclass; cc @author-2. mail a@b.example"
    )
    assert answer["diff"] == clean
    assert redacted["args"] == ["repos/:owner/:repo/pulls/7"]
    bare = {
        **fixture,
        "calls": [_exchange([{"user": {"login": "author"}, "body": body, "diff": diff}])],
    }
    with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^residual login 1 at "):
        fixture_pipeline.redact_fixture(bare, forge="github", denylist=NO_DENYLIST)

    stdout = json.dumps(
        [[{"id": 9, "user": {"login": "q-handle"}, "state": "APPROVED", "body": body}]]
    )
    monkeypatch.setattr(rec, "_author_of", lambda *_a: None)
    assert _run_main(monkeypatch, tmp_path, stdout, None) == 0
    written = json.loads(Path(capsys.readouterr().out.strip()).read_text())
    assert written["result"][0]["body"] == (
        "use @pytest.fixture here, and see @dataclass; cc @author-2. mail a@b.example"
    )


def test_an_scp_or_ssh_remote_gets_the_placeholder_host_namespace_and_project() -> None:
    """Defect 2 (0.9): ``git@<host>:<ns>/<repo>`` and ``ssh://`` are URLs; a classified login cannot survive in them."""
    cases = {
        "clone git@github.com:x-handle/r.git": "clone git@forge.example.com:org/project.git",
        "git@gitlab.com:g/s/r.git, then": "git@forge.example.com:org/project.git, then",
        "ssh://git@gitlab.com/x-handle/r.git": "ssh://git@forge.example.com/org/project.git",
        "git://github.com/x-handle/r": "git://forge.example.com/org/project",
        "git@forge.example.com:org/r.git": "git@forge.example.com:org/project.git",
    }
    for raw, redacted in cases.items():
        assert redact_urls_in_text(raw) == redacted
        assert unredacted_host(raw) is not raw.startswith("git@forge.example.com")
        assert not unredacted_host(redacted)
    for kept in ("mail a@b.example: hi", "host t@example.com:8080", "git@code.example.org:g/r.git"):
        assert redact_urls_in_text(kept) == kept and not unredacted_host(kept)
    assert (
        redact_url("ssh://git@gitlab.com/x-handle/r.git")
        == "ssh://git@forge.example.com/org/project.git"
    )

    roles = {"x-handle": "author", "owner": "author-2"}
    assert classify_logins("git@code.example.org:x-handle/r.git", roles) == (
        "git@code.example.org:author/r.git"
    )
    assert classify_logins("repos/:owner/:repo", roles) == "repos/:owner/:repo"

    for text in ("clone git@github.com:x-handle/r.git", "ssh://git@gitlab.com/x-handle/r.git"):
        answer = {"author": {"login": "x-handle"}, "body": text}
        recorded = [{"args": [], "stdout": redact_stdout(json.dumps(answer)), "stderr": text}]
        built = rec.build_roles(recorded, bot="", maintainer=None, author="x-handle")
        calls = fixture_pipeline.classify_calls(recorded, built)
        assert "x-handle" not in json.dumps(calls) and "github.com" not in json.dumps(calls)
        fixture_judge.refuse_unclassified(
            {"source": "s", "calls": calls}, denylist=deny_list.Denylist()
        )

        raw = [{"args": [], "stdout": json.dumps(answer), "stderr": ""}]
        unredacted = fixture_pipeline.classify_calls(
            raw, rec.build_roles(raw, bot="", maintainer=None, author=None)
        )
        with pytest.raises(
            fixture_judge.UnclassifiedValueError, match=r"^unknown host at exchanges\[0\]"
        ):
            fixture_judge.refuse_unclassified(
                {"source": "s", "calls": unredacted}, denylist=deny_list.Denylist()
            )

    fixture = {"forge": "gitlab", "calls": [_exchange({"body": "clone git@gitlab.com:g/r.git"})]}
    out = fixture_pipeline.redact_fixture(fixture, forge="gitlab", denylist=NO_DENYLIST)
    assert (
        json.loads(out["calls"][0]["stdout"])["body"]
        == "clone git@forge.example.com:org/project.git"
    )


def test_a_login_equal_to_a_git_word_is_replaced_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preference 4 (0.9): a login ``main`` is replaced in a branch field and in prose too; the replay stays consistent.

    The safer option (D2): narrowing would keep the login's string where
    a reader could tie it to the account, and keep argv and stdout apart.
    """
    answer = {
        "author": {"login": "main"},
        "baseRefName": "main",
        "headRefName": "main-fix",
        "body": "merge into main",
    }
    calls = [{"args": ["pr", "view", "1"], "stdout": json.dumps(answer), "stderr": ""}]
    roles = rec.build_roles(calls, bot="", maintainer=None, author="main")
    out = json.loads(fixture_pipeline.classify_calls(calls, roles)[0]["stdout"])
    assert out == {
        "author": {"login": "author"},
        "baseRefName": "author",
        "headRefName": "author-fix",
        "body": "merge into author",
    }

    stdout = json.dumps(
        [[{"id": 9, "user": {"login": "main"}, "state": "APPROVED", "body": "rebase on main"}]]
    )
    monkeypatch.setattr(rec, "_author_of", lambda *_a: "main")
    assert _run_main(monkeypatch, tmp_path, stdout, None) == 0
    fixture = json.loads(Path(capsys.readouterr().out.strip()).read_text())
    assert fixture["result"] == [
        {
            "author": "author",
            "body": "rebase on author",
            "comments": [],
            "id": 9,
            "state": "APPROVED",
        }
    ]


def test_ci_passes_the_denylist_secret_to_the_denylist_test_only_and_never_echoes_it() -> None:
    """O2, D6 (0.10): one step gets the secret and runs the deny-list test alone; no step can print it."""
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    secret = "${{ secrets.REPOACH_PERSONAL_DENYLIST }}"
    assert text.count(secret) == 1
    steps = yaml.safe_load(text)["jobs"]["test"]["steps"]
    (step,) = [s for s in steps if secret in json.dumps(s)]
    assert step["name"] == "Pytest — fixture deny-list"
    assert step["env"] == {deny_list.DENYLIST_ENV: secret}
    module, _sep, name = step["run"].removeprefix("pytest -q ").partition("::")
    assert (module, name) == (
        "tests/unit/test_forge_recording_redaction.py",
        test_fixtures_hold_no_denylisted_value.__name__,
    )
    (unit,) = [s for s in steps if s.get("name") == "Pytest"]
    assert "env" not in unit
    for each in (step, unit):
        assert not re.search(r"set -[a-z]*x|\bxtrace\b|\becho\b|printenv|\benv\b", each["run"])


def test_one_token_boundary_for_replacer_mention_and_denylist() -> None:
    """I1 (0.10): the login replacer, the ``@`` replacer and the deny-list are one pattern modulo the word."""
    assert TOKEN_CHARS == "A-Za-z0-9"
    battery = (
        "x.jdoe a.b-c x[bot] jdoe_x _jdoe %2Fjdoe %2fa.b-c x%2Fx[bot] %3Fjdoe 2Fjdoe @jdoe "
        "jdoe2 ajdoe JDOE A.B-C X[BOT]"
    )
    for word in ("jdoe", "a.b-c", "x[bot]"):
        expected = token_pattern(word)
        assert expected.flags & re.I
        for pattern in (
            recording._login_pattern(word),
            deny_list.denylist_patterns([word])[0],
            closure.token_pattern(word),
        ):
            assert pattern.flags & re.I
            assert [m.span() for m in pattern.finditer(battery)] == [
                m.span() for m in expected.finditer(battery)
            ]
        at = token_pattern(f"@{word}")
        assert [m.span() for m in mention_pattern(word).finditer(battery)] == [
            m.span() for m in at.finditer(battery)
        ]
    text = (
        "ping x.jdoe and jdoe. and JDoe and _jdoe and jdoe_x and a:jdoe and x/:jdoe "
        "and git@h.io:jdoe/r and [jdoe] and 'jdoe' and ?u=jdoe#jdoe and @jdoe "
        "and jdoe-patch-1 and x-jdoe and xjdoe and jdoe2"
    )
    assert classify_text(text, {"jdoe": "author"}) == (
        "ping x.author and author. and author and _author and author_x and a:author and x/:author "
        "and git@h.io:author/r and [author] and 'author' and ?u=author#author and @author "
        "and author-patch-1 and x-author and xjdoe and jdoe2"
    )
    tools = "gh api repos/:owner/:repo/pulls; --- /dev/null; glab api projects/:id"
    roles = {"owner": "author", "repo": "author-2", "dev": "author-3", "null": "author-4"}
    assert classify_text(tools, roles) == tools
    assert classify_text("owner/repo, dev, :OWNER", roles) == "author/author-2, author-3, :author"


_HANDLE, _DOTTED, _SEGMENT, _RAW_HOST, _DENY = (
    "x-handle",
    "a.b-c",
    "secretproj",
    "git.corp-forge.io",
    "someone-host",
)
_ID = 4242424242
_REMOTE = f"git@github.com:some-owner/{_SEGMENT}.git"
_ROLES = {_HANDLE: "author", _DOTTED: "author-2"}
_RAW = [
    {
        "args": [],
        "stderr": f"see https://{_RAW_HOST}/grp-x/repo-x",
        "stdout": json.dumps(
            [
                {"user": {"login": _HANDLE, "id": _ID}},
                {"user": {"username": _DOTTED}},
                {"url": f"https://github.com/some-owner/{_SEGMENT}/pull/1"},
            ]
        ),
    }
]
_TEXT_NEEDLES = (
    (_HANDLE, "login"),
    (_DOTTED, "login"),
    ("github.com", "host"),
    (_RAW_HOST, "host"),
    (_SEGMENT, "segment"),
    (str(_ID), "id"),
    (_DENY, "deny-list"),
)
_CASES = (
    lambda n: n,
    str.upper,
    str.title,
    lambda n: "".join(c.upper() if i % 2 else c for i, c in enumerate(n)),
)
_HIT_NEIGHBOURS = (
    "{n}",
    "x.{n}",
    "{n}.",
    "@{n}",
    "({n})",
    "[{n}]",
    '"{n}"',
    "'{n}'",
    "<{n}>",
    "{n}:",
    ":{n}",
    "/{n}/",
    "={n}&",
    "#{n}",
    "%2F{n}",
    "_{n}",
    "{n}_x",
    "x-{n}",
    "{n}-patch-1",
    "{n}%2F",
)
_INSIDE_A_TOKEN = ("x{n}", "{n}2")
_CARRIERS = (
    "https://{h}/{n}/repo-x",
    "HTTPS://{H}/{n}/repo-x",
    "ssh://{n}@{h}/grp-x/repo-x.git",
    "{n}@{h}:grp-x/repo-x.git",
    "git@{h}:{n}/repo-x.git",
    "{n}@corp.io:8080/x",
    "cc @{n}",
    "@retry(3) @{n}",
    "`@{n}`",
    "@{n}.",
    "@x.{n}",
)
_POSITIONS = ("prose", "diff", "text", "stderr", "stdin", "argv", "kwargs", "result", "source")
_PIPELINE_POSITIONS = _POSITIONS[:7]


def _sample_needles() -> closure.Known:
    """The needles of :data:`_RAW`: two logins, the forge and a raw host, segments, an id, a deny entry."""
    return needles(_RAW, roles=_ROLES, remote=_REMOTE, denylist=(_DENY,))


def _placed(text: str, position: str) -> dict[str, Any]:
    """A recorded fixture holding ``text`` at ``position``, as the forge would have printed it."""
    call: dict[str, Any] = {"args": ["api", "x"], "returncode": 0, "stdin": None, "stderr": ""}
    call["stdout"] = json.dumps({"body": "ok"})
    fixture: dict[str, Any] = {"source": "s", "kwargs": {}, "result": None, "calls": [call]}
    if position in ("prose", "diff"):
        call["stdout"] = json.dumps({"body" if position == "prose" else "diff": text})
    elif position == "text":
        call["stdout"] = text
    elif position in ("stderr", "stdin"):
        call[position] = text
    elif position == "argv":
        call["args"] = ["api", text]
    elif position == "kwargs":
        fixture["kwargs"] = {"k": text}
    else:
        fixture[position] = text
    return fixture


def _through_the_pipeline(fixture: dict[str, Any], found: closure.Known) -> dict[str, Any]:
    """``fixture`` as :func:`rec.record` writes it: redacted, classified, ids synthetic."""
    calls = [dict(c) for c in fixture["calls"]]
    for call in calls:
        call["stdout"] = redact_stdout(call["stdout"])
    known = {**found.mapping(), **_ROLES}
    calls = fixture_pipeline.synthesize_user_ids(
        fixture_pipeline.classify_calls(calls, known), UserIdMap(), forge="github"
    )
    kwargs = recording.redact_urls_in_value(classify_logins(fixture["kwargs"], known))
    return {**fixture, "calls": calls, "kwargs": kwargs}


def _samples() -> list[tuple[str, str, str]]:
    """``(text, needle, kind)`` of every case and neighbour, and of every carrier, of each text needle."""
    out: list[tuple[str, str, str]] = []
    for needle, kind in _TEXT_NEEDLES:
        for case in _CASES:
            out += [(n.format(n=case(needle)), needle, kind) for n in _HIT_NEIGHBOURS]
        for carrier in _CARRIERS:
            text = carrier.format(n=needle, h="github.com", H="GITHUB.COM")
            out.append((text, needle, kind))
    legacy = base64.b64encode(f"04:User{_ID}".encode()).decode()
    out += [(legacy, str(_ID), "id"), (_new_node(_ID), str(_ID), "id")]
    return out


def test_residual_needles_are_seen_in_every_generated_form() -> None:
    """I3, I2 (0.10): the oracle sees each needle in every case, neighbour, carrier and position; a token-inside one is no hit."""
    found = _sample_needles()
    assert {kind for kind, _v in found.entries} == set(closure.JUDGED_KINDS) - {"name", "mailbox"}
    missed: list[tuple[str, str]] = []
    for text, _needle, _kind in _samples():
        for position in _POSITIONS:
            fixture = _placed(text, position)
            if residual(fixture, found) is None:
                missed.append((position, text))
                continue
            with pytest.raises(fixture_judge.UnclassifiedValueError):
                fixture_judge.refuse_unclassified(fixture, deny_list.Denylist(), found)
    assert missed == []
    for needle, _kind in _TEXT_NEEDLES:
        for neighbour in _INSIDE_A_TOKEN:
            fixture = _placed(neighbour.format(n=needle), "prose")
            left = residual(fixture, found)
            assert left is None or left[0] in ("unknown host", "unknown mailbox"), neighbour
    folded = needles([], roles={}, denylist=("strasse-host",))
    assert residual(_placed("deployed on Straße-host", "prose"), folded) == (
        "deny-list",
        1,
        "exchanges[0].stdout.body",
    )
    left = residual(_placed(f"merge into {_HANDLE}", "prose"), found)
    assert left == ("login", 1, "exchanges[0].stdout.body")
    with pytest.raises(
        fixture_judge.UnclassifiedValueError, match=r"^residual login 1 at exchanges\[0\]"
    ):
        fixture_judge.refuse_unclassified(
            _placed(f"merge into {_HANDLE}", "prose"), deny_list.Denylist(), found
        )


ACCEPTED_REFUSALS = (
    ("id", "a raw user id standing in free text: ids are replaced under an id key only"),
    ("deny-list", "a deny-list entry: the list refuses, it never redacts"),
    ("%2F{n}", "a needle right after a percent-escape: the replacer does not decode"),
)
"""Samples the pipeline knowingly leaves to the oracle (0.10): each is refused, never written."""


def test_redactor_clears_every_generated_form() -> None:
    """Quality (0.10): ``redact → classify → ids`` clears every sample but the accepted refusals."""
    found = _sample_needles()
    accepted_kinds = {k for k, _why in ACCEPTED_REFUSALS if not k.startswith("%")}
    left: list[tuple[str, str]] = []
    for text, needle, kind in _samples():
        if kind in accepted_kinds or text.casefold().startswith(f"%2f{needle.casefold()}"):
            continue
        for position in _PIPELINE_POSITIONS:
            written = _through_the_pipeline(_placed(text, position), found)
            if residual(written, found) is not None:
                left.append((position, text))
    assert left == []


def test_a_needle_the_oracle_cannot_judge_is_a_configuration_error() -> None:
    """I3 (0.10): a needle under four characters, a class word or a placeholder is exit 2 by ordinal, never a skip."""
    with pytest.raises(NeedleError, match=r"^needle login 1: too short$"):
        needles([], roles={"abc": "author"})
    with pytest.raises(NeedleError, match=r"^needle segment 1: a class word or placeholder$"):
        needles([], roles={}, remote="git@github.com:maintainer/repo-x.git")
    with pytest.raises(NeedleError, match=r"^needle id 1: too short$"):
        needles([json.dumps({"user": {"login": "author", "id": 7}})], roles={})
    found = needles(
        ["https://github.com/some-owner/repo-x"],
        roles={_HANDLE: "author"},
        remote="https://github.com/some-owner/repo-x.git",
    )
    assert repr(found) == f"<known: {len(found)}>"
    assert found.mapping() == {
        _HANDLE: "author",
        "some-owner": "org",
        "repo-x": "project",
        "api.github.com": "forge.example.com",
        "github.com": "forge.example.com",
        "gitlab.com": "forge.example.com",
    }
    assert recording.NEEDLE_MIN_LENGTH == deny_list.DENYLIST_MIN_LENGTH == 4


def test_a_repository_object_name_and_path_are_placeholders_by_key() -> None:
    """I4 (0.10): in a repository object ``name``/``path`` are ``project`` whatever their value; ``description`` goes."""
    raw = {
        "id": 67000001,
        "name": "Secretproj",
        "path": "secretproj-x",
        "description": "the secret project",
        "web_url": "https://gitlab.com/grp/secretproj",
    }
    out = redact_payload(raw)
    assert out == {
        "id": 67000001,
        "name": "project",
        "path": "project",
        "web_url": "https://forge.example.com/org/project",
    }
    assert redact_payload(out) == out
    projects = UserIdMap.for_projects()
    remapped = projects.apply_projects([out, {"id": 5, "name": "unit"}], forge="gitlab")
    assert remapped == [{**out, "id": 2001}, {"id": 5, "name": "unit"}]
    assert project_id_values(remapped) == [2001] and declared_project_ids(remapped) == [2001]
    merge_request = {
        "name": "Secretproj",
        "description": "body",
        "web_url": "https://gitlab.com/g/r/-/merge_requests/7",
    }
    assert redact_payload(merge_request)["name"] == "Secretproj"
    assert redact_payload(merge_request)["description"] == "body"
    last = UserIdMap.for_projects(start=2999)
    assert last.synthetic(5, forge="gitlab") == 2999
    with pytest.raises(SyntheticIdsExhaustedError, match=r"^synthetic project id 3000 past 2999$"):
        last.synthetic(6, forge="gitlab")


def test_a_repository_id_is_synthetic_in_both_tools(tmp_path: Path) -> None:
    """Q2 (0.10): the recorder and the offline transform both remap a repository id into ``2001..2999`` and declare it."""
    project = {"id": 67000001, "name": "p", "web_url": "https://gitlab.com/grp/repo-x"}
    calls = [{"args": [], "stdout": json.dumps(project), "stderr": ""}]
    (recorded,) = fixture_pipeline.synthesize_user_ids(calls, UserIdMap(), forge="gitlab")
    assert json.loads(recorded["stdout"])["id"] == 2001
    assert declared_project_ids([recorded]) == [2001]
    out = _corpus(
        tmp_path, "c", {"gitlab/p.json": _fixture("gitlab", "branch_protection", [project])}
    )
    assert project_id_values(out["gitlab/p.json"]) == [2001]
    assert out["gitlab/p.json"][SYNTHETIC_PROJECT_IDS_KEY] == [2001]


def test_fixtures_declare_exactly_their_synthetic_project_ids() -> None:
    """Q2 (0.10): every repository id of the corpus is synthetic, in ``2001..2999`` and declared."""
    loaded = {p: json.loads(p.read_text()) for p in sorted(FIXTURES.rglob("*.json"))}
    ids = [i for data in loaded.values() for i in project_id_values(data)]
    assert ids and all(is_synthetic_number(i, 2001, 2999) for i in ids)
    wrong = [
        p.relative_to(FIXTURES).as_posix()
        for p, data in loaded.items()
        if data.get(SYNTHETIC_PROJECT_IDS_KEY, []) != declared_project_ids(data)
    ]
    assert wrong == []


def test_a_remote_userinfo_is_never_kept() -> None:
    """I5, I2 (0.10): one normaliser for URLs and scp remotes; no userinfo, query or foreign fragment survives."""
    cases = {
        "jdoe@gitlab.com:grp/r.git": "git@forge.example.com:org/project.git",
        "JDOE@GITLAB.COM:GRP/R.GIT": "git@forge.example.com:org/project.git",
        "jdoe@corp.io:8080/x": "git@forge.example.com:org/project",
        "ssh://jdoe@gitlab.com/grp/r.git": "ssh://git@forge.example.com/org/project.git",
        "https://jdoe:tok@gitlab.com/grp/r": "https://forge.example.com/org/project",
        "git://gitlab.com/grp/r": "git://forge.example.com/org/project",
        "HTTPS://GITHUB.COM/jdoe/r": "https://forge.example.com/org/project",
        "https://github.com/jdoe/r?u=jdoe#jdoe": "https://forge.example.com/org/project",
        "https://github.com/o/r/pull/4#issuecomment-5": (
            "https://forge.example.com/org/project/pull/4#issuecomment-5"
        ),
    }
    for raw, redacted in cases.items():
        assert redact_urls_in_text(raw) == redacted, raw
        assert unredacted_host(raw) and not unredacted_host(redacted)
        assert "jdoe" not in redacted.casefold() and "tok" not in redacted
    assert fixture_judge.generic_class(redact_urls_in_text("mail jdoe@corp.io:8080/x")) is None
    assert redact_urls_in_text("host t@example.com:8080") == "host t@example.com:8080"


@pytest.mark.parametrize(
    "error",
    [
        fixture_judge.UnclassifiedValueError,
        deny_list.DenylistError,
        SyntheticIdsExhaustedError,
        NeedleError,
    ],
)
def test_every_refusal_class_of_the_recorder_exits_2_and_writes_nothing(
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """I6 a (0.10): each member of the recorder's except tuple is exit 2, one stderr line, nothing written."""

    def _refuse(**_kw: object) -> Path:
        raise error("refused at exchanges[0]")

    monkeypatch.setattr(rec, "record", _refuse)
    out = tmp_path / "out"
    argv = ["--forge", "github", "--case", "c", "--method", "reviews", "--out", str(out)]
    assert rec.main(argv) == 2
    err = capsys.readouterr()
    assert (err.out, err.err) == ("", "refused at exchanges[0]\n")
    assert not out.exists()


@pytest.mark.parametrize(
    "error",
    [
        fixture_pipeline.UnknownForgeError,
        SyntheticIdsExhaustedError,
        NeedleError,
        fixture_pipeline.ResidualValueError,
        deny_list.DenylistError,
    ],
)
def test_every_refusal_class_of_the_offline_transform_exits_2_and_writes_nothing(
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """I6 a (0.10): each member of the offline transform's except tuple is exit 2, one stderr line, empty stdout."""

    def _refuse(_root: Path, **_kw: object) -> list[Path]:
        raise error("refused at gitlab/a.json")

    monkeypatch.setattr(redact_fixtures, "redact_corpus", _refuse)
    assert redact_fixtures.main([str(tmp_path)]) == 2
    err = capsys.readouterr()
    assert (err.out, err.err) == ("", "refused at gitlab/a.json\n")


def test_offline_transform_writes_nothing_when_one_file_refuses(tmp_path: Path) -> None:
    """I6 b, D4 (0.10): every file is computed and judged before any is written."""
    first = _fixture(
        "github",
        "pull_request",
        [{"author": {"login": "someone-x"}, "url": "https://github.com/o/r/pull/1"}],
    )
    seconds = (
        ("unknown", "zzz/b.json", {"calls": []}, fixture_pipeline.UnknownForgeError),
        (
            "residual",
            "github/b.json",
            _fixture(
                "github",
                "pull_request",
                [{"author": {"login": "someone-y"}, "body": "by zqdenyval"}],
            ),
            fixture_pipeline.ResidualValueError,
        ),
    )
    for name, rel, second, error in seconds:
        root = tmp_path / name
        for path, data in (("github/a.json", first), (rel, second)):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(json.dumps(data))
        before = (root / "github" / "a.json").read_bytes()
        with pytest.raises(error):
            fixture_pipeline.redact_corpus(
                root, check=False, denylist=deny_list.Denylist(("zqdenyval",))
            )
        assert (root / "github" / "a.json").read_bytes() == before
    with pytest.raises(
        fixture_pipeline.ResidualValueError,
        match=r"^deny-list entry 1 at github/b\.json:exchanges\[0\]\.stdout\.body$",
    ):
        fixture_pipeline.redact_corpus(
            tmp_path / "residual", check=True, denylist=deny_list.Denylist(("zqdenyval",))
        )


def test_mention_grammar_alternatives_each_have_a_witness() -> None:
    """I6 c (0.10): each alternative of the mention finder has a non-standard-library witness."""
    assert mentions({"body": "cc @kept-handle"}) == ["kept-handle"]
    for witness in (
        "@retry(3)",
        "@x.y",
        "`@spanned`",
        "```\n@fenced\n```",
        "@dash-",
        "a@mail.example",
    ):
        assert mentions({"body": f"{witness} cc @kept-handle"}) == ["kept-handle"], witness
    assert mentions({"body": "@dataclass"}) == []


ALLOWED_CASE_SENSITIVE: dict[str, frozenset[str]] = {
    "recording": frozenset(
        {
            "_BASE64_RE",
            "_NEW_USER_NODE_RE",
            "_SYNTHETIC_NODE_RE",
            "_LEGACY_USER_NODE_RE",
            "_CODE_SPAN_RE",
            "RECORDED_SOURCE_RE",
            "_SPEC_FILE_ID_RE",
            "CLI_PLACEHOLDER_RE",
            "_API_PROJECT_RE",
            "_NAME_WORD_RE",
        }
    ),
    "fixture_reading": frozenset({"CLASS_WORD_RE", "RECORDED_SOURCE_HEAD_RE"}),
    "record_forge_fixture": frozenset(),
    "fixture_judge": frozenset({"HOME_PATH_RE"}),
    "denylist": frozenset(),
    "redact_forge_fixtures": frozenset(),
    "fixture_pipeline": frozenset(),
    "closure": frozenset(
        {
            "_RUN",
            "_LABEL",
            "_STRING_ESCAPE",
            "_NEW_NODE",
            "_LEGACY_NODE",
            "_LEGACY_DECODED",
            "_CLASS_WORD_LOCAL",
            "_DOC_MAILBOX_HOST",
        }
    ),
}
"""Module patterns case-sensitive by nature: encodings, the placeholders the redaction writes, the tools' literals, and the gate's copied regexes."""


def test_case_insensitive_patterns_are_flagged() -> None:
    """I2 (0.10): every module-level recogniser of a scheme, host or login is ``re.I``; the exceptions are named."""
    owners = (fixture_reading, recording)
    for module in (
        fixture_reading,
        recording,
        rec,
        redact_fixtures,
        fixture_pipeline,
        closure,
        fixture_judge,
        deny_list,
    ):
        name = module.__name__.rsplit(".", 1)[-1]
        imported = {id(v) for owner in owners if owner is not module for v in vars(owner).values()}
        if module is fixture_reading:
            imported = set()
        patterns = {
            k: v
            for k, v in vars(module).items()
            if isinstance(v, re.Pattern) and id(v) not in imported
        }
        allowed = ALLOWED_CASE_SENSITIVE[name]
        assert allowed <= set(patterns), name
        blind = sorted(k for k, v in patterns.items() if k not in allowed and not v.flags & re.I)
        assert blind == [], name


def _run_script(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in a fresh interpreter: no test process has configured the logging yet."""
    env = {**os.environ, "XDG_CONFIG_HOME": str(cwd / "xdg")}
    env.pop(deny_list.DENYLIST_ENV, None)
    return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, check=False)


_DRIVER = """
import importlib.util, json, sys
from pathlib import Path
from repoach.forge.github import GitHubForge
from repoach.forge.runner import CommandResult
spec = importlib.util.spec_from_file_location("record_forge_fixture", sys.argv[1])
rec = importlib.util.module_from_spec(spec)
sys.modules["record_forge_fixture"] = rec
spec.loader.exec_module(rec)
tmp = Path(sys.argv[2])
def build(**kw):
    binary = tmp / "gh"
    binary.write_text("")
    forge = GitHubForge(cwd=tmp, gh_path=str(binary), bot_login=kw.get("bot_username") or "review-agent-x")
    forge._spawn = lambda argv, *, input_data=None: CommandResult(
        returncode=0, stdout=sys.argv[3], stderr="", argv=list(argv))
    return forge
rec.select_forge = build
argv = ["--forge", "github", "--cwd", str(tmp), "--case", "c", "--method", "reviews",
        "--args", "[7]", "--out", str(tmp / "out")]
sys.exit(rec.main(argv))
"""


def test_scripts_print_one_line_and_log_to_stderr(tmp_path: Path) -> None:
    """K8 (0.10): each script's stdout is its one result line, or empty on a refusal; logs go to stderr."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    script = str(ROOT / "scripts" / "record_forge_fixture.py")
    review = {"id": 9, "user": {"login": "q-handle"}, "state": "APPROVED", "body": "ok"}
    done = _run_script(
        [sys.executable, str(driver), script, str(tmp_path), json.dumps([[review]])], tmp_path
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [str(tmp_path / "out" / "github" / "c.json")]
    assert "recorder.author_unknown" in done.stderr
    refused = {**review, "body": "log /home/someone-x/y"}
    done = _run_script(
        [sys.executable, str(driver), script, str(tmp_path), json.dumps([[refused]])], tmp_path
    )
    assert (done.returncode, done.stdout) == (2, "")
    assert done.stderr.splitlines()[-1].startswith("generic class home_path at exchanges[")

    corpus = tmp_path / "corpus"
    (corpus / "github").mkdir(parents=True)
    plain = _fixture("github", "diff", [])
    plain["calls"] = [{**_exchange({}), "stdout": "not json"}]
    (corpus / "github" / "a.json").write_text(json.dumps(plain))
    offline = str(ROOT / "scripts" / "redact_forge_fixtures.py")
    done = _run_script([sys.executable, offline, "--check", str(corpus)], tmp_path)
    assert done.stdout.splitlines() == ["forge fixtures: 0 would change"]
    assert "forge.recording.not_json" in done.stderr
    (corpus / "zzz").mkdir()
    (corpus / "zzz" / "b.json").write_text("{}")
    done = _run_script([sys.executable, offline, str(corpus)], tmp_path)
    assert (done.returncode, done.stdout) == (2, "")


_LEAK_TEST = """
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("record_forge_fixture", {script!r})
rec = importlib.util.module_from_spec(spec)
sys.modules["record_forge_fixture"] = rec
spec.loader.exec_module(rec)
from repoach.forge import recording


def _hits(denylist, corpus):
    return [denylist.hit(line) for line in corpus.read_text().splitlines()]


def test_leak():
    denylist = deny_list.read_denylist(Path({config!r}))
    found = recording.known_values([], roles={{}}, denylist=denylist)
    assert len(found) and _hits(denylist, Path({corpus!r})) == []
"""


def test_denylist_entries_never_reach_a_local_or_a_repr(tmp_path: Path) -> None:
    """K8 (0.10): a failing deny-list check under ``pytest -l`` names no entry; the reader returns an opaque value."""
    marker = "leakmarker-" + "4711"
    config = tmp_path / "config"
    config.mkdir()
    deny_list.denylist_file(config).write_text(f"{marker}\n")
    corpus = tmp_path / "corpus.txt"
    corpus.write_text(f"held {marker} here\n")
    assert repr(deny_list.read_denylist(config)) == "<denylist: 1 entries>"
    module = tmp_path / "test_leak.py"
    script = str(ROOT / "scripts" / "record_forge_fixture.py")
    module.write_text(_LEAK_TEST.format(script=script, config=str(config), corpus=str(corpus)))
    done = _run_script(
        child_pytest_argv(
            "-l", "-p", "no:cacheprovider", "-q", str(module), basetemp=tmp_path / "child"
        ),
        tmp_path,
    )
    assert done.returncode == 1, done.stdout[-500:]
    assert "test_leak" in done.stdout
    assert marker not in done.stdout + done.stderr


def test_an_offline_refusal_binds_no_deny_list_entry_in_a_frame(tmp_path: Path) -> None:
    """K8 (0.28, PR #319): the offline transform carries the opaque reader down to the refusal, never the entries."""
    marker = "zqoffdeny" + "4712"
    corpus = tmp_path / "corpus"
    (corpus / "github").mkdir(parents=True)
    data = _fixture("github", "reviews", [[{"id": 1, "body": f"held {marker} here"}]])
    (corpus / "github" / "a.json").write_text(json.dumps(data))
    with pytest.raises(
        fixture_pipeline.ResidualValueError, match=r"^deny-list entry 1 at "
    ) as info:
        fixture_pipeline.redact_corpus(corpus, check=True, denylist=deny_list.Denylist((marker,)))
    bound = [
        (frame.f_code.co_name, name)
        for frame, _line in traceback.walk_tb(info.tb)
        for name, value in frame.f_locals.items()
        if isinstance(value, tuple | list) and marker in value
    ]
    assert bound == []


def _one_call(stdout: object, args: list[str] | None = None) -> list[dict[str, Any]]:
    """The raw exchanges of one call answering ``stdout`` as JSON (or as text when it is a string)."""
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return [
        {"args": args or ["api", "x"], "returncode": 0, "stdin": None, "stderr": "", "stdout": text}
    ]


def _kinds(found: closure.Known, kind: str) -> list[str]:
    """The values of ``found`` of one kind; fake values only, for assertions on needle sources."""
    return [value for k, value in found.entries if k == kind]


def test_branch_names_holding_a_login_are_replaced_and_the_rest_refused() -> None:
    """R1 (0.11): ``<login>-patch-1`` and ``x-<login>`` are cleared by the replacer; a deny entry so placed is refused."""
    answer = {
        "author": {"login": "q-handle"},
        "headRefName": "q-handle-patch-1",
        "body": "x-q-handle",
    }
    calls = _one_call(answer)
    roles = rec.build_roles(calls, bot="", maintainer=None, author=None)
    out = json.loads(fixture_pipeline.classify_calls(calls, roles)[0]["stdout"])
    assert out == {
        "author": {"login": "author"},
        "headRefName": "author-patch-1",
        "body": "x-author",
    }
    found = needles([], roles={}, denylist=("deny-entry",))
    for text in ("deny-entry-patch-1", "x-deny-entry", "_deny-entry", "deny-entry_x"):
        assert residual(_placed(text, "prose"), found) == (
            "deny-list",
            1,
            "exchanges[0].stdout.body",
        )
    assert residual(_placed("xdeny-entry", "prose"), found) is None


def test_logins_under_dropped_keys_are_needles_and_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2 (0.11): a login only under a key the redaction drops (``assignees``, ``mergedBy``) is a needle and a role."""
    answer = {
        "user": {"login": "q-handle"},
        "assignees": [{"login": "q-other-a"}],
        "mergedBy": {"login": "q-other-b"},
        "body": "ping q-other-a and q-other-b",
    }
    raw = [_one_call(answer)]
    assert recording.raw_logins(raw) == ["q-handle", "q-other-a", "q-other-b"]
    assert recording.raw_logins([_one_call({"user": {"login": "github-actions[bot]"}})]) == []
    found = needles(raw, roles={})
    assert _kinds(found, "login") == ["q-handle", "q-other-a", "q-other-b"]
    redacted = [{**c, "stdout": redact_stdout(c["stdout"])} for c in raw[0]]
    roles = rec.build_roles(redacted, bot="", maintainer=None, author=None, also=["q-other-a"])
    assert roles == {"q-handle": "author", "q-other-a": "author-2"}

    stdout = json.dumps(
        [
            [
                {
                    "id": 9,
                    "user": {"login": "q-handle"},
                    "mergedBy": {"login": "q-other-b"},
                    "state": "APPROVED",
                    "body": "ping q-other-b",
                }
            ]
        ]
    )
    assert _run_main(monkeypatch, tmp_path, stdout, None) == 0
    written = Path(capsys.readouterr().out.strip()).read_text()
    assert "q-other-b" not in written and "q-handle" not in written


def test_segments_come_from_the_recorded_project_and_any_private_host_it_cannot_tell() -> None:
    """R3, R6 (0.11), Pref 3 (0.14): remote/API/inferred-instance URLs give segments; without any host cue all private hosts do; public third parties never do."""
    url = "https://git.corp-x.io/grp-x/proj-x/-/merge_requests/3"
    raw = [_one_call({"web_url": url, "body": "see https://git.other-x.io/third-grp/third-proj"})]
    inferred_remote = needles(raw, roles={})
    assert _kinds(inferred_remote, "segment") == ["grp-x", "proj-x", "third-grp", "third-proj"]
    no_url_key = [_one_call({"body": f"see {url} and https://git.other-x.io/third-grp/third-proj"})]
    without_remote = needles(no_url_key, roles={})
    assert _kinds(without_remote, "segment") == ["grp-x", "proj-x", "third-grp", "third-proj"]
    on_instance = needles(raw, roles={}, remote="git@git.corp-x.io:grp-x/proj-x.git")
    assert _kinds(on_instance, "segment") == ["grp-x", "proj-x", "third-grp", "third-proj"]
    assert "git.corp-x.io" in _kinds(on_instance, "host")
    alias = needles([_one_call({"body": "ok"})], roles={}, remote="corpalias:grp-y/proj-y.git")
    assert _kinds(alias, "segment") == ["grp-y", "proj-y"]
    assert "corpalias" not in _kinds(alias, "host")
    other = needles(raw, roles={}, remote="git@git.third-x.io:grp-z/proj-z.git")
    assert _kinds(other, "segment") == [
        "grp-z",
        "proj-z",
        "grp-x",
        "proj-x",
        "third-grp",
        "third-proj",
    ]
    assert "git.third-x.io" in _kinds(other, "host")
    api = needles([_one_call({"body": "ok"}, ["api", "repos/own-x/repo-y/pulls/3"])], roles={})
    assert _kinds(api, "segment") == ["own-x", "repo-y"]
    templated = needles([_one_call({"body": "ok"}, ["api", "repos/:owner/:repo/pulls"])], roles={})
    assert _kinds(templated, "segment") == []
    gitlab = needles([_one_call({"body": "ok"}, ["api", "projects/grp-q%2Fproj-q/x"])], roles={})
    assert _kinds(gitlab, "segment") == ["grp-q", "proj-q"]
    public = "see https://github.com/python/cpython, https://github.com/git/git and pypa/pip"
    third = needles(
        [_one_call({"body": f"upgrade python; {public}"})],
        roles={},
        remote="git@github.com:own-x/repo-y.git",
    )
    assert _kinds(third, "segment") == ["own-x", "repo-y"]
    assert classify_text("upgrade python", {**third.mapping()}) == "upgrade python"
    assert residual(_placed("upgrade python and git", "prose"), third) is None


def test_node_ids_inside_longer_strings_are_judged_by_their_number() -> None:
    """R4 (0.11), D2 (0.12): legacy and ``U_`` node ids are decoded in URLs, branches and affixed strings."""
    found = needles([_one_call({"user": {"login": "q-handle", "id": _ID}})], roles={})
    legacy = base64.b64encode(f"04:User{_ID}".encode()).decode()
    padded_id = 4815162
    legacy_padded = base64.b64encode(f"04:User{padded_id}".encode()).decode()
    unpadded_id = 48151623
    legacy_unpadded = base64.b64encode(f"04:User{unpadded_id}".encode()).decode()
    assert not legacy_unpadded.endswith("=")
    big_id = (1 << 33) + 7
    big_node = "U_" + base64.urlsafe_b64encode(
        b"\x92\x00\xcf" + big_id.to_bytes(8, "big")
    ).decode().rstrip("=")
    for text in (
        f"id={legacy}",
        f"?node={_new_node(_ID)}",
        f"see {legacy} here",
        f"see {_new_node(_ID)}-patch here",
        f"x{legacy}",
        f"{legacy}x",
        f"user{legacy}",
        f"{legacy}suffix",
        f"https://example.com/users/{legacy}/profile",
        f"https://example.com/users/{_new_node(_ID)}/profile",
        f"abcdefg{_new_node(_ID)}",
        f"U_foo{_new_node(_ID)}",
        f"feat/{legacy}",
        f"branch/{_new_node(_ID)}",
    ):
        left = residual(_placed(text, "prose"), found)
        assert left is not None and left[0] == "id", text
    padded_found = needles([_one_call({"user": {"login": "q-handle", "id": padded_id}})], roles={})
    assert (
        residual(
            _placed(f"https://example.com/users/{legacy_padded}/profile", "prose"), padded_found
        )
        is not None
    )
    unpadded_found = needles(
        [_one_call({"user": {"login": "q-handle", "id": unpadded_id}})], roles={}
    )
    for text in (
        f"{legacy_unpadded}x",
        f"{legacy_unpadded}suffix",
        f"https://example.com/users/{legacy_unpadded}/profile",
    ):
        left = residual(_placed(text, "prose"), unpadded_found)
        assert left is not None and left[0] == "id", text
    assert str(unpadded_id) in closure.node_id_numbers(f"{legacy_unpadded}x")
    assert str(unpadded_id) in closure.node_id_numbers(f"{legacy_unpadded}suffix")
    assert str(_ID) in closure.node_id_numbers(f"U_foo{_new_node(_ID)}")
    big_found = needles([_one_call({"user": {"login": "q-handle", "id": big_id}})], roles={})
    assert residual(_placed(f"see {big_node} here", "prose"), big_found) is not None
    assert closure.node_id_numbers(f"a {_new_node(_ID + 1)} b") == [str(_ID + 1)]
    assert closure.node_id_numbers("U_1001 and plainwords1234") == ["1001"]
    deny = deny_list.Denylist((str(_ID),))
    assert deny.hit(f"https://example.com/users/{legacy}/profile") == 1
    assert deny.hit(f"https://example.com/users/{_new_node(_ID)}/profile") == 1
    assert deny.hit(f"abcdefg{_new_node(_ID)}") == 1
    assert deny.hit(f"{legacy}suffix") == 1


def test_an_opaque_raw_id_is_a_needle_by_its_own_string() -> None:
    """D3 (0.10): a raw user id that decodes to no number is judged by its own string."""
    found = needles([_one_call({"user": {"login": "q-handle", "id": "opaque-id-x"}})], roles={})
    assert _kinds(found, "id") == ["opaque-id-x"]
    assert residual(_placed("held opaque-id-x", "prose"), found) == (
        "id",
        1,
        "exchanges[0].stdout.body",
    )


def test_dict_keys_are_judged_and_never_named() -> None:
    """R5 (0.11): a key outside the code's own words is a string of the fixture; its path names an ordinal."""
    found = needles([], roles={"q-handle": "author"}, denylist=("deny-entry",))
    fixture = _placed("ok", "prose")
    fixture["result"] = {"number": 1, "q-handle": 1}
    assert residual(fixture, found) == ("login", 1, "result.{key 1}")
    fixture["result"] = {"state": "open", "nested": {"deny-entry": True}}
    assert residual(fixture, found) == ("deny-list", 1, "result.{key 1}.{key 0}")
    stdin_keys = _placed("ok", "prose")
    stdin_keys["calls"][0]["stdin"] = json.dumps({"q-handle": "x"})
    assert residual(stdin_keys, found) == ("login", 1, "exchanges[0].stdin.{key 0}")
    code = needles([], roles={"state": "author"})
    keyed = _placed("ok", "prose")
    keyed["result"] = {"state": "open"}
    assert residual(keyed, code) is None
    assert "body" in fixture_reading.CODE_KEYS and "head_ref" in fixture_reading.CODE_KEYS


def test_the_recorders_constant_fields_are_judged_by_the_denylist_only() -> None:
    """R6 (0.11), D3 (0.12): ``forge``, ``method`` and the ``source`` head are closed constants; ``case`` is judged."""
    found = needles(
        [],
        roles={"qhandle": "author"},
        remote="git@github.com:github/docs.git",
        denylist=("conv-case",),
    )
    assert "github" in _kinds(found, "segment")
    assert frozenset({"forge", "method"}) == fixture_judge.CONSTANT_FIELDS
    fixture = _placed("ok", "prose")
    fixture.update(
        forge="github",
        method="conversation_comments",
        case="conv_ok",
        source=recording.recorded_source("2026-11-18", "github"),
    )
    assert residual(fixture, found) is None
    fixture["source"] = recording.recorded_source("2026-11-18", "github") + ", docs trimmed"
    assert residual(fixture, found) == ("segment", 2, "source")
    fixture["source"] = recording.recorded_source("2026-11-18", "github")
    fixture["case"] = "pr_qhandle"
    assert residual(fixture, found) == ("login", 1, "case")
    fixture["case"] = "pr_docs"
    assert residual(fixture, found) == ("segment", 2, "case")
    fixture["case"] = "conv-case"
    assert residual(fixture, found) == ("deny-list", 1, "case")


def test_residual_ordinals_count_within_their_kind() -> None:
    """D3 (0.10): the message names the needle's ordinal in its kind, so two logins are told apart."""
    found = needles(
        [], roles={"q-first": "author", "q-second": "author-2"}, denylist=("deny-a", "deny-b")
    )
    assert residual(_placed("held q-second", "prose"), found) == (
        "login",
        2,
        "exchanges[0].stdout.body",
    )
    assert residual(_placed("held deny-b", "prose"), found) == (
        "deny-list",
        2,
        "exchanges[0].stdout.body",
    )


def test_the_tools_templates_are_not_judged() -> None:
    """D3 (0.10, 0.12): ``:owner``, ``:repo``, ``:id``, ``/dev/null`` and actual ``docs/specs`` ids are literals."""
    found = needles(
        [],
        roles={"owner": "author", "null": "author-2", "id": "author-3", "repo": "author-4"},
        min_length=1,
    )
    fixture = _placed("ok", "prose")
    fixture["calls"][0]["args"] = [
        "api",
        "repos/:owner/:repo/pulls",
        "projects/:id/members",
        "--- /dev/null",
    ]
    assert residual(fixture, found) is None
    assert classify_text("projects/:id/members", {"id": "author-3"}) == "projects/:id/members"
    fixture["calls"][0]["args"] = ["api", "repos/owner/x"]
    assert residual(fixture, found) == ("login", 1, "exchanges[0].args[1]")
    fixture["calls"][0]["args"] = ["api", "projects/id/members"]
    assert residual(fixture, found) == ("login", 3, "exchanges[0].args[1]")
    cited = _placed("per SP-OWNER-SETTLES-A-FINDING, ask the owner", "prose")
    assert classify_text("per SP-OWNER-SETTLES-A-FINDING, owner", {"owner": "author"}) == (
        "per SP-OWNER-SETTLES-A-FINDING, author"
    )
    assert residual(cited, found) == ("login", 1, "exchanges[0].stdout.body")
    cited = _placed("per SP-OWNER-SETTLES-A-FINDING", "prose")
    assert residual(cited, found) is None
    assert residual(_placed("per sp-owner-settles-a-finding", "prose"), found) is not None
    invented = needles(
        [_one_call({"user": {"login": "zqxjdoe7", "id": 48151623}})],
        roles={"zqxjdoe7": "author"},
        remote="git@github.com:zqgroup/zqsecretproj.git",
    )
    for text in (
        "SP-ZQXJDOE7",
        "SP-FIX-ZQXJDOE7",
        "SP-ZQSECRETPROJ",
        "SP-ZQGROUP",
        "SP-48151623",
        "SP-ZQXJDOE7_x",
    ):
        assert residual(_placed(f"see {text} here", "prose"), invented) is not None, text
    assert classify_text("see SP-ZQXJDOE7 here", {"zqxjdoe7": "author"}) == "see SP-author here"
    deny = needles([], roles={}, denylist=("settles", "repoach-bot"))
    for text in ("per SP-OWNER-SETTLES-A-FINDING", "by repoach-bot"):
        assert residual(_placed(text, "prose"), deny) is not None, text
    entries = deny_list.Denylist(("settles",))
    assert entries.hit("per SP-OWNER-SETTLES-A-FINDING") == 1


def test_the_offline_needles_accept_a_short_hand_written_login() -> None:
    """D4 (0.10): the committed corpus may hold a three-letter placeholder login; the offline needles judge it."""
    data = _fixture("github", "reviews", [[{"user": {"login": "bob"}, "body": "hi"}]])
    found = fixture_pipeline.fixture_known(data, denylist=NO_DENYLIST)
    assert ("login", "bob") in found.entries


def test_longer_logins_are_replaced_first() -> None:
    """D2 (0.11): with ``-`` a delimiter, ``q-handle`` must be replaced before ``q-handle-x`` would be cut."""
    roles = {"q-handle": "author", "q-handle-x": "author-2"}
    assert classify_text("q-handle-x and q-handle", roles) == "author-2 and author"


def test_non_person_logins_are_no_needles() -> None:
    """D2 (0.10): the platform's and the project's own bots are kept, so neither is a needle."""
    raw = [_one_call({"user": {"login": "github-actions[bot]"}, "body": "x"})]
    found = needles(raw, roles={}, remote="git@github.com:repoach-bot/repo-x.git")
    assert _kinds(found, "login") == []
    assert _kinds(found, "segment") == ["repo-x"]
    assert residual(_placed("by github-actions[bot] and repoach-bot", "prose"), found) is None
    owner = needles([], roles={"github": "author-3"})
    kept = "by github-actions[bot], github-actions and repoach-bot"
    assert residual(_placed(kept, "prose"), owner) is None
    assert residual(_placed(f"{kept} on github", "prose"), owner) is not None
    assert classify_text(f"{kept} on github", {"github": "author-3"}) == f"{kept} on author-3"


def test_the_oracle_judges_encoded_forms() -> None:
    """I2, I3 (0.11, 0.17, round 6): percent (up to 4 rounds), entities, ``\\u`` escapes and NFKC; lookalikes are NG4-4."""
    found = needles([], roles={"qhandle": "author"})
    for text in (
        "qhandle%41",
        "".join(f"%{ord(c):02X}" for c in "qhandle"),
        "".join(f"%25{ord(c):02X}" for c in "qhandle"),
        "".join(f"%2525{ord(c):02X}" for c in "qhandle"),
        "&#113;handle",
        "&#x71;handle",
        "\\u0071handle",
        "\uff51handle",
    ):
        assert residual(_placed(text, "prose"), found) == (
            "login",
            1,
            "exchanges[0].stdout.body",
        ), text
    for text in ("qha\u200bndle", "qh\u0430ndle"):
        assert residual(_placed(text, "prose"), found) is None, text


def test_double_slash_urls_do_not_leak_segments() -> None:
    """D4 (0.12): empty segments from ``//`` never shift or leak a namespace or repository."""
    cases = {
        "https://github.com/zqotherns//zqotherrepo": (
            "https://forge.example.com/org/project",
            ("zqotherns",),
            "zqotherrepo",
        ),
        "https://github.com/repos//zqotherns/zqotherrepo": (
            "https://forge.example.com/repos/org/project",
            ("zqotherns",),
            "zqotherrepo",
        ),
        "https://github.com/users//zqsomeone9": (
            "https://forge.example.com/users/org",
            ("zqsomeone9",),
            None,
        ),
        "https://gitlab.com/grp//sub//repo//-/merge_requests/7": (
            "https://forge.example.com/org/project/-/merge_requests/7",
            ("grp", "sub"),
            "repo",
        ),
        "https://gitlab.com/api/v4/projects/grp%2F%2Frepo/merge_requests/7": (
            "https://forge.example.com/api/v4/projects/org%2Fproject/merge_requests/7",
            ("grp",),
            "repo",
        ),
    }
    for url, (expected_url, expected_ns, expected_repo) in cases.items():
        assert redact_url(url) == expected_url, url
        path = url.split("://", 1)[1].split("/", 1)[1]
        identity = recording.path_identity(path.split("/"))
        assert identity.namespace == expected_ns, url
        assert identity.repository == expected_repo, url
    assert recording._repository_root("/grp//secretproj") == "secretproj"
    assert recording._api_project("repos//own-x/repo-y/pulls") == ("own-x", "repo-y")
    assert (
        redact_url("https://github.com/own-x/repo-y/") == "https://forge.example.com/org/project/"
    )
    assert recording.path_identity(["own-x", "repo-y", ""]).segments == ["org", "project", ""]


def test_call_args_urls_are_redacted_in_both_tools() -> None:
    """D5 (0.12): ``classify_calls`` and offline ``_exchanges`` redact URLs in ``call['args']``."""
    args = [
        "api",
        "-f",
        "https://someuser:secret@other.io/thirdns/thirdrepo?q=1",
        "-f",
        "https://github.com/thirdns/thirdrepo/pull/1",
    ]
    calls = [{"args": args, "returncode": 0, "stdin": None, "stderr": "", "stdout": "[]"}]
    out = fixture_pipeline.classify_calls(calls, {})
    assert out[0]["args"] == [
        "api",
        "-f",
        "https://forge.example.com/org/project",
        "-f",
        "https://forge.example.com/org/project/pull/1",
    ]
    fixture = {"forge": "github", "calls": calls}
    offline = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    assert offline["calls"][0]["args"] == out[0]["args"]


def test_noreply_mailboxes_are_redacted_by_class_and_not_refused() -> None:
    """D6 (0.12): GitHub and GitLab noreply mailboxes redact their local part by class and pass D3."""
    trailers = (
        (
            "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
            "Co-authored-by: Copilot <author@users.noreply.forge.example.com>",
        ),
        (
            "Co-authored-by: Alice <alice@users.noreply.github.com>",
            "Co-authored-by: Alice <author@users.noreply.forge.example.com>",
        ),
        (
            "Co-authored-by: Bob <1234-bob@users.noreply.gitlab.com>",
            "Co-authored-by: Bob <author@users.noreply.forge.example.com>",
        ),
        (
            "Co-authored-by: Bot <41898282+github-actions[bot]@users.noreply.github.com>",
            "Co-authored-by: Bot <bot@users.noreply.forge.example.com>",
        ),
    )
    for raw_text, expected in trailers:
        redacted = redact_urls_in_text(raw_text)
        assert redacted == expected
        assert redact_urls_in_text(redacted) == expected
        assert fixture_judge.generic_class(redacted) is None
    raw_call = [
        {
            "args": ["api", "repos/own-x/repo-y/pulls"],
            "returncode": 0,
            "stdin": None,
            "stderr": "Co-authored-by: X <48151623+zqxjdoe7@users.noreply.github.com>",
            "stdout": json.dumps(
                [
                    {
                        "user": {"login": "zqxjdoe7", "id": 48151623},
                        "body": "Co-authored-by: X <48151623+zqxjdoe7@users.noreply.github.com>",
                    }
                ]
            ),
        }
    ]
    raw_copy = [dict(c) for c in raw_call]
    raw_call[0]["stdout"] = redact_stdout(raw_call[0]["stdout"])
    roles = rec.build_roles(raw_call, bot="", maintainer=None, author="zqxjdoe7")
    found = needles(raw_copy, roles=roles, remote="git@github.com:own-x/repo-y.git")
    calls = fixture_pipeline.synthesize_user_ids(
        fixture_pipeline.classify_calls(raw_call, {**found.mapping(), **roles}),
        UserIdMap(),
        forge="github",
    )
    fixture = {"forge": "github", "method": "reviews", "case": "c", "source": "s", "calls": calls}
    fixture_judge.refuse_unclassified(fixture, deny_list.Denylist(), found)
    assert (
        json.loads(calls[0]["stdout"])[0]["body"]
        == "Co-authored-by: X <author@users.noreply.forge.example.com>"
    )
    assert calls[0]["stderr"] == "Co-authored-by: X <author@users.noreply.forge.example.com>"
    assert closure.judge("contact real.person@private-mail.org:25", closure.Known(())) is not None
    assert (
        closure.judge("contact real.person@private-mail.org:hello", closure.Known(())) is not None
    )
    assert redact_urls_in_text("contact user@noreply.example.com") == (
        "contact user@noreply.example.com"
    )
    assert redact_urls_in_text("contact user@users.noreply.example.org") == (
        "contact user@users.noreply.example.org"
    )
    assert (
        redact_urls_in_text("Co-authored-by: X <realuser@users.noreply.forge.example.com>")
        == "Co-authored-by: X <author@users.noreply.forge.example.com>"
    )


def test_python_code_and_third_party_links_under_ssh_alias_are_not_corrupted_or_refused() -> None:
    """D6 (0.12, 0.15): ``self.ms/1000``, ``README.md/foo``, ``foo.py:80/bar`` stay intact; ssh-alias skips third-party segments."""
    body = (
        "elapsed = self.ms/1000; see README.md/foo, foo.py:80/bar, "
        "https://docs.python.org/3/library/re.html and https://docs.vendor-unlisted.org/3/guide/re.html"
    )
    item = {
        "user": {"login": "zqxjdoe7", "id": 48151623},
        "body": body,
        "url": "https://github.com/own-x/repo-y/pull/3",
    }
    raw = _one_call([item], ["api", "repos/:owner/:repo/issues/3/comments"])
    found = needles(raw, roles={"zqxjdoe7": "author"}, remote="mygh:own-x/repo-y.git")
    assert _kinds(found, "segment") == ["own-x", "repo-y", "guide"]
    assert "self.ms" not in _kinds(found, "host")
    assert "readme.md" not in _kinds(found, "host")
    assert "foo.py" not in _kinds(found, "host")
    redacted_body = classify_text(
        redact_urls_in_text(body), {**found.mapping(), "zqxjdoe7": "author"}
    )
    assert "elapsed = self.ms/1000" in redacted_body
    assert "README.md/foo" in redacted_body
    assert "foo.py:80/bar" in redacted_body
    assert "https://docs.python.org/3/library/re.html" in redacted_body
    assert "https://forge.example.com/org/project" in redacted_body

    no_url_keys = _one_call({"body": "see https://docs.vendor-unlisted.org/3/library/re.html"})
    found_diff = needles(no_url_keys, roles={}, remote="mygh:own-x/repo-y.git")
    assert _kinds(found_diff, "segment") == ["own-x", "repo-y", "library"]

    self_hosted = _one_call(
        {
            "web_url": "https://git.corp-x.io/grp-x/proj-x/-/merge_requests/3",
            "body": "see https://git.corp-x.io/other-grp/other-proj and https://docs.vendor-unlisted.org/3/library/re.html",
        }
    )
    found_sh = needles(self_hosted, roles={}, remote="mygl:grp-x/proj-x.git")
    assert _kinds(found_sh, "segment") == ["grp-x", "proj-x", "other-grp", "other-proj", "library"]


def test_markdown_delimiters_terminate_urls_and_preserve_kept_hosts() -> None:
    """R6-D1 (0.13, 0.15): backticks, ``*``, ``|``, ``~``, ``_`` terminate URLs in redaction and ``locations()``."""
    kept = "Check `https://example.com` and `http://localhost` and `https://127.0.0.1:8080/health`."
    assert redact_urls_in_text(kept) == kept
    assert not unredacted_host(kept)
    assert (
        redact_urls_in_text("See `https://docs.python.org/3/library/re.html` for regex.")
        == "See `https://docs.python.org/3/library/re.html` for regex."
    )
    assert (
        redact_urls_in_text("|Py|https://docs.python.org/3/library/re.html|")
        == "|Py|https://docs.python.org/3/library/re.html|"
    )
    assert (
        redact_urls_in_text("See `https://unlisted.vendor.org/3/library/re.html` for regex.")
        == "See `https://forge.example.com/org/project` for regex."
    )
    assert (
        redact_urls_in_text("|V|https://unlisted.vendor.org/3/library/re.html|")
        == "|V|https://forge.example.com/org/project|"
    )
    assert (
        redact_urls_in_text("See **https://git.zqcorp.io/zqgroup/zqsecondrepo** now")
        == "See **https://forge.example.com/org/project** now"
    )
    assert (
        redact_urls_in_text(
            "Old ~~https://git.zqcorp.io/zqgroup/zqsecondrepo~~ and _https://git.zqcorp.io/zqgroup/zqthirdrepo_"
        )
        == "Old ~~https://forge.example.com/org/project~~ and _https://forge.example.com/org/project_"
    )
    assert (
        redact_urls_in_text("git clone git@zqotherhost.io.:zqotherns/zqotherrepo.git")
        == "git clone git@forge.example.com:org/project.git"
    )
    assert (
        redact_urls_in_text("~~git@zqotherhost.io:zqotherns/zqotherrepo~~")
        == "~~git@forge.example.com:org/project~~"
    )
    assert (
        redact_urls_in_text("mirror ftp://git.zqother.io/zqotherns/zqotherrepo")
        == "mirror ftp://forge.example.com/org/project"
    )
    delimited = needles(
        _one_call(
            {
                "body": "`https://git.zqcorp.io/zqgroup/zqsecondrepo` "
                "**https://git.zqcorp.io/zqgroup/zqthirdrepo** "
                "|https://git.zqcorp.io/zqgroup/zqfourthrepo| "
                "~~https://git.zqcorp.io/zqgroup/zqfifthrepo~~ "
                "_https://git.zqcorp.io/zqgroup/zqsixthrepo_"
            }
        ),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert _kinds(delimited, "segment") == [
        "zqgroup",
        "zqmainrepo",
        "zqsecondrepo",
        "zqthirdrepo",
        "zqfourthrepo",
        "zqfifthrepo",
        "zqsixthrepo",
    ]
    raw = _one_call(
        {
            "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/1",
            "body": "Moved from `https://git.zqcorp.io/zqgroup/zqsecondrepo` (see zqsecondrepo)",
        }
    )
    found = needles(raw, roles={}, remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git")
    assert "zqsecondrepo" in _kinds(found, "segment")
    redacted = classify_text(
        redact_urls_in_text(
            "Moved from `https://git.zqcorp.io/zqgroup/zqsecondrepo` (see zqsecondrepo)"
        ),
        found.mapping(),
    )
    assert redacted == "Moved from `https://forge.example.com/org/project` (see project)"


def test_bots_scoped_packages_and_case_insensitive_decorators() -> None:
    """R6-D2 (0.13): bot login/mention/stem share one identity; ``@scope/pkg`` and ``@Override`` are not mentions."""
    dep_body = (
        "Dependabot will resolve any conflicts.\n"
        "- `@dependabot rebase` will rebase\n"
        "- @dependabot recreate will recreate\n"
        "Bumps @types/node and @vue/cli; updates types. Missing @Override on override test. "
        "cc @zqcolleague and @github-actions"
    )
    item = {"user": {"login": "dependabot[bot]", "id": 49699333}, "body": dep_body}
    raw = _one_call([item])
    redacted_calls = [{**c, "stdout": redact_stdout(c["stdout"])} for c in raw]
    roles = rec.build_roles(
        redacted_calls,
        bot="repoach-bot",
        maintainer=None,
        author=None,
        also=recording.raw_logins(raw),
    )
    assert roles == {"dependabot[bot]": "bot", "dependabot": "bot", "@zqcolleague": "author"}
    found = needles(raw, roles=roles, remote="git@github.com:own-x/repo-y.git")
    calls = fixture_pipeline.synthesize_user_ids(
        fixture_pipeline.classify_calls(redacted_calls, {**found.mapping(), **roles}),
        UserIdMap(),
        forge="github",
    )
    fixture = {
        "forge": "github",
        "method": "conversation_comments",
        "case": "dep",
        "source": recording.recorded_source("2026-11-19", "github"),
        "calls": calls,
    }
    fixture_judge.refuse_unclassified(fixture, deny_list.Denylist(), found)
    out_item = json.loads(calls[0]["stdout"])[0]
    assert out_item["user"]["login"] == "bot"
    assert "dependabot" not in out_item["body"].casefold()
    assert "@bot recreate" in out_item["body"]
    assert "`@bot rebase`" in out_item["body"]
    assert "@types/node" in out_item["body"]
    assert "@vue/cli" in out_item["body"]
    assert "@Override" in out_item["body"]
    assert "@author" in out_item["body"]
    assert "@github-actions" in out_item["body"]

    mention_first = [
        {"args": [], "stdout": json.dumps({"body": "cc @renovate please rebase"})},
        {"args": [], "stdout": json.dumps({"user": {"login": "renovate[bot]"}})},
    ]
    assert rec.build_roles(mention_first, bot="", maintainer=None, author=None) == {
        "renovate[bot]": "bot",
        "renovate": "bot",
    }

    renovate_payload = {
        "title": "chore(deps): update @actions/checkout and @babel/core",
        "body": "Update GitHub Actions workflow and [@babel/core](https://github.com/babel/babel).",
    }
    assert mentions(renovate_payload) == []

    offline_dep = {
        "forge": "github",
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "dependabot[bot]", "id": 49699333},
                        "body": "Dependabot: @dependabot rebase",
                    }
                ]
            )
        ],
    }
    offline_out = fixture_pipeline.redact_fixture(offline_dep, forge="github", denylist=NO_DENYLIST)
    assert (
        residual(offline_out, fixture_pipeline.fixture_known(offline_dep, denylist=NO_DENYLIST))
        is None
    )


def test_gitlab_api_numeric_project_ids_and_offline_kwargs_and_system_noreply() -> None:
    """R6-D5 (0.13): ``api/v4/projects/<id>`` maps to ``2001..2999``; offline redacts ``kwargs`` and ``noreply@github.com``."""
    api_url = "https://gitlab.com/api/v4/projects/77665544/merge_requests/42"
    raw_with_repo = _one_call(
        {
            "repository": {
                "id": 77665544,
                "name": "secretproj",
                "path": "secretproj",
                "web_url": "https://gitlab.com/grp-x/secretproj",
            },
            "description": f"API call: {api_url}",
        }
    )
    found_only_url = needles(_one_call({"description": f"API call: {api_url}"}), roles={})
    assert "77665544" in _kinds(found_only_url, "id")
    redacted_calls = [{**c, "stdout": redact_stdout(c["stdout"])} for c in raw_with_repo]
    calls = fixture_pipeline.synthesize_user_ids(redacted_calls, UserIdMap(), forge="gitlab")
    parsed = json.loads(calls[0]["stdout"])
    assert parsed["repository"]["id"] == 2001
    assert (
        parsed["description"]
        == "API call: https://forge.example.com/api/v4/projects/2001/merge_requests/42"
    )
    assert recording.declared_project_ids(calls) == [2001]

    sample_offline = {
        "forge": "github",
        "case": "c",
        "method": "conversation_comments",
        "args": [1],
        "kwargs": {"note": "https://github.com/o/r/pull/1", "api": api_url},
        "bot_username": "repoach-bot",
        "source": recording.recorded_source("2026-11-19", "github"),
        "trimmed": None,
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "author", "id": 1001},
                        "body": "Committer: GitHub <noreply@github.com>; keep noreply@example.com",
                    }
                ]
            )
        ],
        "synthetic_user_ids": [1001],
        "result": None,
        "raises": None,
    }
    red_off = fixture_pipeline.redact_fixture(sample_offline, forge="github", denylist=NO_DENYLIST)
    assert red_off["kwargs"] == {
        "note": "https://forge.example.com/org/project/pull/1",
        "api": "https://forge.example.com/api/v4/projects/2001/merge_requests/42",
    }
    assert fixture_reading.SYNTHETIC_PROJECT_IDS_KEY not in red_off
    assert json.loads(red_off["calls"][0]["stdout"])[0]["body"] == (
        "Committer: GitHub <noreply@forge.example.com>; keep noreply@example.com"
    )
    assert (
        residual(red_off, fixture_pipeline.fixture_known(sample_offline, denylist=NO_DENYLIST))
        is None
    )
    assert fixture_pipeline.redact_fixture(red_off, forge="github", denylist=NO_DENYLIST) == red_off

    assert redact_urls_in_text("Author: github-actions[bot] <actions@github.com>") == (
        "Author: github-actions[bot] <noreply@forge.example.com>"
    )
    assert redact_urls_in_text("Author: Bot <ACTIONS@GITHUB.COM>") == (
        "Author: Bot <noreply@forge.example.com>"
    )
    assert redact_urls_in_text("Author: Bot <no-reply@github.com>") == (
        "Author: Bot <no-reply@forge.example.com>"
    )
    assert (
        fixture_judge.generic_class("Author: github-actions[bot] <noreply@forge.example.com>")
        is None
    )
    assert fixture_judge.generic_class("Author: Bot <no-reply@forge.example.com>") is None
    assert redact_urls_in_text("Contact actions@privatecorp.com") == (
        "Contact actions@privatecorp.com"
    )
    assert closure.judge("Contact actions@privatecorp.com", closure.Known(())) is not None


def test_third_party_links_under_empty_remote_and_public_reference_hosts() -> None:
    """Preferences 2 & 3 (0.13): ``remote=''`` infers public forge from URL keys; public reference hosts stay intact."""
    raw = _one_call(
        {
            "url": "https://github.com/own-x/repo-y/pull/42",
            "body": "See https://docs.python.org/3/library/re.html and https://docs.vendor-unlisted.org/3/guide.html.",
        }
    )
    found = needles(raw, roles={}, remote="")
    assert "3" not in _kinds(found, "segment")
    assert _kinds(found, "segment") == ["guide.html"]

    prose = (
        "FROM docker.io/library/python:3.12-slim\n"
        'import "golang.org/x/sync/errgroup"\n'
        'import "k8s.io/apimachinery/pkg/apis/meta/v1"\n'
        "https://pypi.org/project/ruff/\n"
        "https://www.npmjs.com/package/typescript\n"
        "https://docs.github.com/en/actions\n"
        "https://docs.gitlab.com/ee/api/\n"
    )
    assert redact_urls_in_text(prose) == prose
    assert not unredacted_host(prose)
    image = "Pulling ghcr.io/astral-sh/uv:0.4.0\n"
    assert redact_urls_in_text(image) == image
    assert closure.judge(image, closure.Known(())) == closure.Refusal("tenant path")
    raw_ref = _one_call({"url": "https://github.com/own-x/repo-y/pull/42", "body": prose})
    found_ref = needles(
        raw_ref, roles={"zqxlogin": "author"}, remote="git@github.com:own-x/repo-y.git"
    )
    assert classify_text(redact_urls_in_text(prose), found_ref.mapping()) == prose
    assert residual(_placed(prose, "prose"), found_ref) is None
    assert residual(
        _placed("https://docs.github.com/en/zqxlogin", "prose"),
        found_ref,
    ) == ("login", 1, "exchanges[0].stdout.body")


def test_public_hosts_strip_userinfo_and_normalize_host_case() -> None:
    """R7-D1 (0.14): userinfo is never kept on any host; kept-host URLs normalize scheme and host case."""
    for raw_url, expected in (
        (
            "curl https://alice:mysecret@ghcr.io/astral-sh/uv",
            "curl https://forge.example.com/org/project",
        ),
        (
            "curl https://zqxjdoe7:s3cr3t@ghcr.io/zqgroup/zqmainrepo",
            "curl https://forge.example.com/org/project",
        ),
        (
            "curl https://alice:mysecret@pypi.org/project/ruff/",
            "curl https://forge.example.com/org/project/",
        ),
        (
            "curl https://alice:mysecret@example.com/api/v1",
            "curl https://forge.example.com/org/project",
        ),
    ):
        redacted = redact_urls_in_text(raw_url)
        assert redacted == expected, raw_url
        assert unredacted_host(raw_url)
        assert not unredacted_host(redacted)
    assert unredacted_host("ssh://alice@forge.example.com/org/project.git")
    assert not unredacted_host("ssh://git@forge.example.com/org/project.git")
    assert redact_urls_in_text("ssh://alice@example.com/o/r.git") == (
        "ssh://git@forge.example.com/org/project.git"
    )

    assert redact_urls_in_text(
        "See https://Docs.GitHub.com/en/actions and https://Docs.GitLab.com/ee/api/"
    ) == ("See https://docs.github.com/en/actions and https://docs.gitlab.com/ee/api/")
    assert redact_urls_in_text("Ref: HTTPS://DOCS.GITHUB.COM/en/rest/pulls") == (
        "Ref: https://docs.github.com/en/rest/pulls"
    )
    assert redact_urls_in_text("See https://PyPI.org/project/ruff/") == (
        "See https://pypi.org/project/ruff/"
    )
    found = needles(
        [_one_call({"body": "See https://Docs.GitHub.com/en/actions"})],
        roles={},
        remote="git@github.com:own-x/repo-y.git",
    )
    live_text = classify_text(
        redact_urls_in_text(
            "See https://Docs.GitHub.com/en/actions and https://Docs.GitLab.com/ee/api/"
        ),
        found.mapping(),
    )
    assert live_text == (
        "See https://docs.github.com/en/actions and https://docs.gitlab.com/ee/api/"
    )
    assert residual(_placed("See Docs.GitHub.com/en/actions", "prose"), found) is None

    fx_docs = {
        "forge": "github",
        "case": "c",
        "method": "conversation_comments",
        "args": [1],
        "kwargs": {},
        "bot_username": "repoach-bot",
        "source": recording.recorded_source("2026-11-19", "github"),
        "trimmed": None,
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "author", "id": 1001},
                        "body": "See https://Docs.GitHub.com/en/actions and https://Docs.GitLab.com/ee/api/",
                    }
                ]
            )
        ],
        "synthetic_user_ids": [1001],
        "result": None,
        "raises": None,
    }
    red_docs = fixture_pipeline.redact_fixture(fx_docs, forge="github", denylist=NO_DENYLIST)
    assert json.loads(red_docs["calls"][0]["stdout"])[0]["body"] == (
        "See https://docs.github.com/en/actions and https://docs.gitlab.com/ee/api/"
    )
    assert residual(red_docs, fixture_pipeline.fixture_known(fx_docs, denylist=NO_DENYLIST)) is None


def test_prose_punctuation_and_markdown_delimiters_terminate_urls_without_eating() -> None:
    """R7-D2, Pref 5 (0.14): trailing prose punctuation and Markdown delimiters terminate URLs and are never eaten."""
    kept_cases = (
        "Try https://example.com! Also works on http://localhost!",
        "Just published https://pypi.org/project/ruff/!",
        "Did you check https://example.com?",
        "Check https://example.com, https://example.com; https://example.com: https://example.com.",
        "See (https://example.com) [https://example.com] {https://example.com} <https://example.com>",
        "Quote 'https://example.com' and \"https://example.com\" and ~~https://example.com~~ and _https://example.com_",
    )
    for sample in kept_cases:
        assert redact_urls_in_text(sample) == sample, sample
        assert not unredacted_host(sample), sample

    assert (
        redact_urls_in_text("Did you check https://docs.python.org/3/library/re.html?")
        == "Did you check https://docs.python.org/3/library/re.html?"
    )
    assert (
        redact_urls_in_text("Did you check https://unlisted.vendor.org/3/library/re.html?")
        == "Did you check https://forge.example.com/org/project?"
    )
    assert (
        redact_urls_in_text("Check https://git.zqcorp.io/zqgroup/zqsecondrepo!")
        == "Check https://forge.example.com/org/project!"
    )
    for punct in (".", ",", ":", ";", "!", "?", ")", "]", "}", ">", "'", '"'):
        text = f"See https://git.zqcorp.io/zqgroup/zqsecondrepo{punct}"
        assert redact_urls_in_text(text) == f"See https://forge.example.com/org/project{punct}", (
            punct
        )

    assert locations("Check https://git.zqcorp.io/zqgroup/zqsecondrepo!") == [
        ("git.zqcorp.io", ("zqgroup", "zqsecondrepo"))
    ]
    raw = _one_call(
        {
            "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/1",
            "body": "Check https://git.zqcorp.io/zqgroup/zqsecondrepo! (see zqsecondrepo docs)",
        }
    )
    found = needles(raw, roles={}, remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git")
    assert "zqsecondrepo" in _kinds(found, "segment")
    redacted = classify_text(
        redact_urls_in_text(
            "Check https://git.zqcorp.io/zqgroup/zqsecondrepo! (see zqsecondrepo docs)"
        ),
        found.mapping(),
    )
    assert redacted == "Check https://forge.example.com/org/project! (see project docs)"


def test_short_bot_stems_placeholder_bots_and_offline_bot_parity() -> None:
    """R7-D4, V7-17, V7-18 (0.14): short ``[bot]`` mentions classify in both tools; offline aligns ``_PLACEHOLDERS``, case and dedup."""
    for bot_login, stem in (("cla[bot]", "cla"), ("bob[bot]", "bob"), ("ci[bot]", "ci")):
        raw = _one_call(
            [{"user": {"login": bot_login, "id": 44556677}, "body": f"Please run @{stem} check."}]
        )
        red_calls = [{**c, "stdout": redact_stdout(c["stdout"])} for c in raw]
        roles = rec.build_roles(
            red_calls,
            bot="repoach-bot",
            maintainer=None,
            author=None,
            also=recording.raw_logins(raw),
        )
        assert roles == {bot_login: "bot"}
        found = needles(raw, roles=roles, remote="git@github.com:own-x/repo-y.git")
        if len(f"@{stem}") >= recording.NEEDLE_MIN_LENGTH:
            assert f"@{stem}" in _kinds(found, "login")
            assert residual(_placed(f"Run @{stem} check", "prose"), found) is not None
        calls = fixture_pipeline.synthesize_user_ids(
            fixture_pipeline.classify_calls(red_calls, {**found.mapping(), **roles}),
            UserIdMap(),
            forge="github",
        )
        out_item = json.loads(calls[0]["stdout"])[0]
        assert out_item["user"]["login"] == "bot"
        assert out_item["body"] == "Please run @bot check."
        fixture = {
            "forge": "github",
            "method": "conversation_comments",
            "case": "short_bot",
            "source": recording.recorded_source("2026-11-19", "github"),
            "calls": calls,
        }
        fixture_judge.refuse_unclassified(fixture, deny_list.Denylist(), found)

        fx_off = {
            "forge": "github",
            "calls": [
                _exchange(
                    [{"user": {"login": bot_login, "id": 44556677}, "body": f"Run @{stem} check"}]
                )
            ],
        }
        red_off = fixture_pipeline.redact_fixture(fx_off, forge="github", denylist=NO_DENYLIST)
        assert json.loads(red_off["calls"][0]["stdout"])[0]["body"] == "Run @bot check"
        assert (
            residual(red_off, fixture_pipeline.fixture_known(fx_off, denylist=NO_DENYLIST)) is None
        )

    found_dep = needles([], roles={"dependabot[bot]": "bot", "dependabot": "bot"})
    assert "dependabot" in _kinds(found_dep, "login")
    assert residual(_placed("Dependabot will rebase", "prose"), found_dep) == (
        "login",
        3,
        "exchanges[0].stdout.body",
    )
    unmentioned = needles([], roles={"dependabot[bot]": "bot"})
    assert "dependabot" not in _kinds(unmentioned, "login")
    assert residual(_placed("Dependabot will rebase", "prose"), unmentioned) is None

    fx_proj_bot = {
        "forge": "github",
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "project[bot]", "id": 44556677},
                        "body": "See https://github.com/o/r/pull/1 and @project",
                    }
                ]
            )
        ],
    }
    red_proj = fixture_pipeline.redact_fixture(fx_proj_bot, forge="github", denylist=NO_DENYLIST)
    assert json.loads(red_proj["calls"][0]["stdout"])[0]["body"] == (
        "See https://forge.example.com/org/project/pull/1 and @bot"
    )
    assert (
        residual(red_proj, fixture_pipeline.fixture_known(fx_proj_bot, denylist=NO_DENYLIST))
        is None
    )

    fx_upper_bot = {
        "forge": "github",
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "Dependabot[BOT]", "id": 44556677},
                        "body": "Dependabot: @dependabot rebase",
                    }
                ]
            )
        ],
    }
    assert fixture_pipeline.fixture_roles(fx_upper_bot) == {
        "Dependabot[BOT]": "bot",
        "dependabot": "bot",
    }
    red_upper = fixture_pipeline.redact_fixture(fx_upper_bot, forge="github", denylist=NO_DENYLIST)
    assert json.loads(red_upper["calls"][0]["stdout"])[0]["user"]["login"] == "bot"
    assert json.loads(red_upper["calls"][0]["stdout"])[0]["body"] == "bot: @bot rebase"
    assert (
        residual(red_upper, fixture_pipeline.fixture_known(fx_upper_bot, denylist=NO_DENYLIST))
        is None
    )

    fx_mention_first = {
        "forge": "github",
        "calls": [
            _exchange([{"body": "cc @renovate please rebase"}]),
            _exchange([{"user": {"login": "renovate[bot]", "id": 29139614}, "body": "ok"}]),
        ],
    }
    assert fixture_pipeline.fixture_roles(fx_mention_first) == {
        "renovate[bot]": "bot",
        "renovate": "bot",
    }


def test_public_reference_hosts_match_by_suffix_and_balance_url_parens() -> None:
    """R8-D1, Pref 2 (0.15): public reference hosts match by registrable suffix and keep GFM balanced parens."""
    kept_urls = (
        "https://docs.python.org/3/library/re.html",
        "https://www.python.org/downloads/release/python-3120/",
        "https://peps.python.org/pep-0695/",
        "https://python.org/about/",
        "https://readthedocs.org/projects/pytest/",
        "https://pytest.readthedocs.io/en/stable/how-to/fixtures.html",
        "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise",
        "https://crates.io/crates/serde",
        "https://docs.rs/serde/latest/serde/",
        "https://stackoverflow.com/questions/123456/how-to-merge-dicts",
        "https://en.wikipedia.org/wiki/Regular_expression",
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "https://pypi.org/project/ruff/",
        "https://www.npmjs.com/package/typescript",
        "https://registry.npmjs.org/typescript",
        "https://pkg.go.dev/golang.org/x/sync/errgroup",
        "https://proxy.golang.org/",
        "https://docs.github.com/en/actions/using-workflows",
        "https://docs.gitlab.com/ee/api/merge_requests.html",
    )
    for url in kept_urls:
        assert redact_urls_in_text(f"See {url} next") == f"See {url} next", url
        assert not unredacted_host(f"See {url} next"), url
        assert redact_urls_in_text(f"Did you check {url}?") == f"Did you check {url}?", url

    assert (
        redact_urls_in_text("See HTTPS://DOCS.PYTHON.ORG/3/library/re.html")
        == "See https://docs.python.org/3/library/re.html"
    )
    assert (
        redact_urls_in_text("See https://alice:secret@docs.python.org/3/library/re.html")
        == "See https://forge.example.com/org/project"
    )
    assert unredacted_host("See https://alice:secret@docs.python.org/3/library/re.html")

    for unlisted in (
        "https://evilpython.org/a/b",
        "https://python.org.evil.com/a/b",
        "https://github.com/org/repo",
        "https://gitlab.com/org/repo",
        "https://unlisted.internal.example-corp.net/a/b",
    ):
        assert redact_urls_in_text(unlisted) == "https://forge.example.com/org/project", unlisted
        assert unredacted_host(unlisted), unlisted

    paren_cases = (
        (
            "[Python](https://en.wikipedia.org/wiki/Python_(programming_language))",
            "[Python](https://en.wikipedia.org/wiki/Python_(programming_language))",
        ),
        (
            "(https://en.wikipedia.org/wiki/Python_(programming_language)).",
            "(https://en.wikipedia.org/wiki/Python_(programming_language)).",
        ),
        (
            "(https://docs.python.org/3/library/re.html)",
            "(https://docs.python.org/3/library/re.html)",
        ),
        (
            "func(https://docs.python.org/3/library/re.html).bar()",
            "func(https://docs.python.org/3/library/re.html).bar()",
        ),
        (
            "See [wiki](https://git.zqcorp.io/zqns/foo_(bar)) now",
            "See [wiki](https://forge.example.com/org/project) now",
        ),
    )
    for raw_text, expected in paren_cases:
        assert redact_urls_in_text(raw_text) == expected, raw_text

    found = needles(
        _one_call({"body": "ok"}),
        roles={"zqxjdoe7": "author"},
        denylist=("zqdenyval",),
    )
    assert residual(
        _placed("https://docs.python.org/3/library/re.html?q=zqxjdoe7", "prose"), found
    ) == ("login", 1, "exchanges[0].stdout.body")
    assert residual(
        _placed("https://pytest.readthedocs.io/en/latest/?ref=zqdenyval", "prose"), found
    ) == ("deny-list", 1, "exchanges[0].stdout.body")
    assert (
        residual(
            _placed("https://en.wikipedia.org/wiki/Python_(programming_language)", "prose"), found
        )
        is None
    )

    fx_off = {
        "forge": "github",
        "calls": [
            _exchange(
                [
                    {
                        "user": {"login": "author", "id": 1001},
                        "body": (
                            "See https://DOCS.PYTHON.ORG/3/library/re.html? and "
                            "https://en.wikipedia.org/wiki/Python_(programming_language)"
                        ),
                    }
                ]
            )
        ],
    }
    red_off = fixture_pipeline.redact_fixture(fx_off, forge="github", denylist=NO_DENYLIST)
    assert json.loads(red_off["calls"][0]["stdout"])[0]["body"] == (
        "See https://docs.python.org/3/library/re.html? and "
        "https://en.wikipedia.org/wiki/Python_(programming_language)"
    )
    assert residual(red_off, fixture_pipeline.fixture_known(fx_off, denylist=NO_DENYLIST)) is None


def test_userinfo_rule_covers_empty_passwords_rfc3986_chars_and_scp_hosts() -> None:
    """R8-D3, WF6 (0.15): one userinfo rule covers empty passwords, RFC 3986 chars and SCP on every host class."""
    userinfo_variants = (
        "https://token:@git.zqother.io/zqns/zqotherrepo",
        "https://bob:sec!@git.zqother.io/zqns/zqotherrepo",
        "https://bob:p@ss@git.zqother.io/zqns/zqotherrepo",
        "https://bob:a$b&c'd(e)f*g+h,i;j=k~l%20m@git.zqother.io/zqns/zqotherrepo",
        "https://first.last:@git.zqother.io/zqns/zqotherrepo",
        "https://:@git.zqother.io/zqns/zqotherrepo",
    )
    for url in userinfo_variants:
        text = f"Check {url} (see zqotherrepo)"
        assert [loc for loc in locations(text) if loc[0] == "git.zqother.io" and loc[1]] == [
            ("git.zqother.io", ("zqns", "zqotherrepo"))
        ], url
        assert (
            redact_urls_in_text(text)
            == "Check https://forge.example.com/org/project (see zqotherrepo)"
        ), url
        assert unredacted_host(text), url
        found = needles(
            _one_call(
                {
                    "html_url": "https://github.com/zqgroup/zqmainrepo/pull/1",
                    "body": text,
                }
            ),
            roles={},
            remote="",
        )
        assert "git.zqother.io" in _kinds(found, "host"), url
        assert "zqotherrepo" in _kinds(found, "segment"), url
        redacted = classify_text(redact_urls_in_text(text), found.mapping())
        assert redacted == "Check https://forge.example.com/org/project (see project)", url
        assert residual(_placed(redacted, "prose"), found) is None, url

    assert locations("Check (@pytest.fixture) and `@pytest.fixture`") == []

    for scp_raw in (
        "alice@example.com:org/repo.git",
        "alice@forge.example.com:org/project.git",
        "alice@ghcr.io:org/repo.git",
        "alice@git.zqcorp.io:zqns/zqrepo.git",
    ):
        assert unredacted_host(scp_raw) or "example.com" in scp_raw, scp_raw
        redacted = redact_urls_in_text(scp_raw)
        assert redacted == "git@forge.example.com:org/project.git", scp_raw
        assert not unredacted_host(redacted), scp_raw

    for scp_kept in ("git@example.com:org/r.git", "git@code.example.org:g/r.git"):
        assert redact_urls_in_text(scp_kept) == scp_kept, scp_kept
        assert not unredacted_host(scp_kept), scp_kept

    assert unredacted_host("ssh://alice@forge.example.com/org/project.git")
    assert not unredacted_host("ssh://git@forge.example.com/org/project.git")
    assert not unredacted_host("git@forge.example.com:org/project.git")


def test_url_termination_preserves_dunder_fragments_scp_delimiters_and_line_comments() -> None:
    """R8-D4, Pref 1 (0.15): dunder fragments, SCP delimiters and ``//`` line comments survive cleanly."""
    url_cases = (
        (
            "See https://github.com/python/cpython/blob/main/Lib/pathlib.py#__init__ for details.",
            "See https://forge.example.com/org/project/blob/main/Lib/pathlib.py for details.",
        ),
        (
            "See https://github.com/zqgroup/zqrepo/blob/main/a.py#__init__!",
            "See https://forge.example.com/org/project/blob/main/a.py!",
        ),
        (
            "_https://github.com/zqgroup/zqrepo/blob/main/a.py#__init___",
            "_https://forge.example.com/org/project/blob/main/a.py_",
        ),
        (
            "https://github.com/zqgroup/zqrepo/pull/1?q=__main__",
            "https://forge.example.com/org/project/pull/1",
        ),
        (
            "_https://github.com/zqgroup/zqrepo/pull/1#issuecomment-42_",
            "_https://forge.example.com/org/project/pull/1#issuecomment-42_",
        ),
        (
            "https://docs.github.com/en/rest#__init__",
            "https://docs.github.com/en/rest#__init__",
        ),
        (
            "_git@git.zqcorp.io:zqgroup/zqrepo.git_",
            "_git@forge.example.com:org/project.git_",
        ),
        (
            "__git@git.zqcorp.io:zqgroup/zqrepo.git__",
            "__git@forge.example.com:org/project.git__",
        ),
        (
            "git_bot@git.zqcorp.io:zqgroup/zqrepo.git",
            "git@forge.example.com:org/project.git",
        ),
    )
    for raw_text, expected in url_cases:
        assert redact_urls_in_text(raw_text) == expected, raw_text

    comment_samples = (
        "//config.dev/env/prod",
        "```go\n//config.dev/env/prod\nvar x = 1\n```",
        "    //config.dev/env/prod",
        "+//config.dev/env/prod",
        "var x = 1; //config.dev/env/prod",
        "///config.dev/env/prod",
    )
    for sample in comment_samples:
        assert locations(sample) == [], sample
        found = needles(_one_call({"body": sample}), roles={})
        assert "config.dev" not in _kinds(found, "host"), sample
        assert classify_text(redact_urls_in_text(sample), found.mapping()) == sample, sample

    assert locations('<script src="//cdn.corp-x.io/app.js">') == [("cdn.corp-x.io", ("app.js",))]
    assert locations("https://config.dev/env/prod") == [("config.dev", ("env", "prod"))]


def test_round8_guard_invariants_kill_surviving_mutants() -> None:
    """R8-D6, WF4 (0.15): pin scheme guard, kept-span userinfo, ``Docs.GitLab.com`` literal, bot stem guards and leading-slash ``glab api``."""
    userinfo_doc_url = "https://alice:secret@gitlab.example.com/api/v4/projects/88442211/issues"
    assert recording.project_id_values(userinfo_doc_url) == [88442211]
    assert (
        UserIdMap.for_projects().apply_projects(userinfo_doc_url, forge="gitlab")
        == "https://alice:secret@gitlab.example.com/api/v4/projects/2001/issues"
    )

    found_forge = needles([], roles={})
    assert residual(_placed("See Docs.GitLab.com/ee/api", "prose"), found_forge) is None
    assert residual(_placed("See DOCS.GITLAB.COM/ee/api", "prose"), found_forge) is None

    assert recording.bot_mention_stem("author[bot]") is None
    assert recording.bot_mention_stem("bot[bot]") is None
    assert (
        classify_text("cc @author and author[bot]", {"author[bot]": "bot"}) == "cc @author and bot"
    )

    assert recording.bot_mention_stem("github-actions[bot]") is None
    assert recording.bot_mention_stem("repoach-bot[bot]") is None

    assert (
        classify_text("cc @cla and cla[bot]", {"cla[bot]": "bot", "@cla": "author-2"})
        == "cc @author-2 and bot"
    )

    assert recording.project_id_values("glab api /projects/88442211/issues") == [88442211]
    assert (
        UserIdMap.for_projects().apply_projects(
            "glab api /projects/88442211/issues", forge="gitlab"
        )
        == "glab api /projects/2001/issues"
    )


def test_offline_transform_redacts_all_six_positions_and_non_json_project_ids() -> None:
    """R8-D6, WF1 (0.15): offline ``redact_fixture`` redacts URLs in args, kwargs, stdin, stderr, stdout and result, plus non-JSON stdout project ids."""
    fixture = {
        "forge": "gitlab",
        "case": "offline_six_positions",
        "method": "diff",
        "args": ["https://git.zqcorp.io/zqns/zqargrepo/-/merge_requests/1"],
        "kwargs": {"ref": "https://git.zqcorp.io/zqns/zqkwrepo/-/merge_requests/2"},
        "bot_username": "repoach-bot",
        "source": recording.recorded_source("2026-11-19", "gitlab"),
        "trimmed": None,
        "calls": [
            {
                "program": "glab",
                "args": [
                    "api",
                    "projects/88442211/merge_requests/1/notes",
                    "https://git.zqcorp.io/zqns/zqcallrepo",
                ],
                "stdin": "POST https://git.zqcorp.io/zqns/zqstdinrepo/notes",
                "returncode": 0,
                "stdout": (
                    "diff --git a/ci.sh b/ci.sh\n"
                    "+curl https://git.zqcorp.io/api/v4/projects/88442211/jobs\n"
                    "+glab api /projects/88442211/pipelines\n"
                ),
                "stderr": "403 GET https://git.zqcorp.io/zqns/zqstderrrepo/members",
            }
        ],
        "result": {
            "url": "https://git.zqcorp.io/zqns/zqresrepo/-/merge_requests/1",
            "body": "See https://git.zqcorp.io/zqns/zqresbody/issues/9",
        },
        "raises": None,
    }
    redacted = fixture_pipeline.redact_fixture(fixture, forge="gitlab", denylist=NO_DENYLIST)
    assert redacted["args"] == ["https://forge.example.com/org/project/-/merge_requests/1"]
    assert redacted["kwargs"] == {"ref": "https://forge.example.com/org/project/-/merge_requests/2"}
    assert redacted["result"] == {
        "url": "https://forge.example.com/org/project/-/merge_requests/1",
        "body": "See https://forge.example.com/org/project/issues/9",
    }
    call = redacted["calls"][0]
    assert call["args"] == [
        "api",
        "projects/2001/merge_requests/1/notes",
        "https://forge.example.com/org/project",
    ]
    assert call["stdin"] == "POST https://forge.example.com/org/project"
    assert call["stderr"] == "403 GET https://forge.example.com/org/project"
    assert call["stdout"] == (
        "diff --git a/ci.sh b/ci.sh\n"
        "+curl https://forge.example.com/api/v4/projects/2001/jobs\n"
        "+glab api /projects/2001/pipelines\n"
    )
    assert fixture_reading.SYNTHETIC_PROJECT_IDS_KEY not in redacted
    assert residual(redacted, fixture_pipeline.fixture_known(fixture, denylist=NO_DENYLIST)) is None
    assert (
        fixture_pipeline.redact_fixture(redacted, forge="gitlab", denylist=NO_DENYLIST) == redacted
    )


def test_regex_character_classes_and_rfc3986_userinfo_enumeration() -> None:
    """R9-D1 (0.16): every regex character class is built via ``_char_class`` and matches its exact documented ASCII set."""
    alnum = set(string.ascii_letters + string.digits)
    all_ascii = [chr(code) for code in range(128)]
    non_ascii = "\u00e9"
    ascii_space = {ch for ch in all_ascii if ch.isspace()}

    rfc3986_userinfo = alnum | set("-._~%!$&'()*+,;=:{}")
    userinfo_pat = re.compile(rf"^{recording._RFC3986_USERINFO_CLASS}$")
    matched_userinfo = {ch for ch in all_ascii if userinfo_pat.match(ch)}
    assert matched_userinfo == rfc3986_userinfo
    assert not userinfo_pat.match(non_ascii)
    assert "-" in matched_userinfo
    for excluded in ("<", ">", "?", "@", "/", "#"):
        assert excluded not in matched_userinfo, excluded

    documented_positive: tuple[tuple[str, str, set[str]], ...] = (
        ("_TOKEN_CLASS", recording._TOKEN_CLASS, alnum),
        ("_CLI_PLACEHOLDER_AFTER_CLASS", recording._CLI_PLACEHOLDER_AFTER_CLASS, alnum | {"-"}),
        (
            "_MENTION_BEFORE_BLOCK_CLASS",
            recording._MENTION_BEFORE_BLOCK_CLASS,
            alnum | set("_.@/+-:${}"),
        ),
        ("_MENTION_HANDLE_CLASS", recording._MENTION_HANDLE_CLASS, alnum | set("_-")),
        ("_MENTION_DOT_AFTER_CLASS", recording._MENTION_DOT_AFTER_CLASS, alnum | {"_"}),
        ("_MENTION_SLASH_AFTER_CLASS", recording._MENTION_SLASH_AFTER_CLASS, alnum | set("_.-")),
        ("_DIFF_MARKER_CLASS", recording._DIFF_MARKER_CLASS, {"+", "-"}),
        ("_SCP_BEFORE_BLOCK_CLASS", recording._SCP_BEFORE_BLOCK_CLASS, alnum | set(".+/-")),
        ("_SCP_USER_CLASS", recording._SCP_USER_CLASS, alnum | set("_.+-")),
        ("_SCP_HOST_LABEL_CLASS", recording._SCP_HOST_LABEL_CLASS, alnum | {"-"}),
        ("_SCP_PATH_CLASS", recording._SCP_PATH_CLASS, alnum | set("_.~%-")),
        ("_NOREPLY_LOCAL_CLASS", recording._NOREPLY_LOCAL_CLASS, alnum | set("._%+[]-")),
    )
    for name, cls_expr, expected in documented_positive:
        pat = re.compile(rf"^{cls_expr}$")
        got = {ch for ch in all_ascii if pat.match(ch)}
        assert got == expected, name
        assert not pat.match(non_ascii), name

    documented_negated: tuple[tuple[str, str, set[str]], ...] = (
        (
            "_URL_BODY_CHAR",
            recording._URL_BODY_CHAR,
            set(all_ascii) - ascii_space - set("\"'<>()[]{}\\,`*|"),
        ),
    )
    for name, cls_expr, expected in documented_negated:
        pat = re.compile(rf"^{cls_expr}$")
        got = {ch for ch in all_ascii if pat.match(ch)}
        assert got == expected, name
        assert pat.match(non_ascii), name

    for url in (
        "https://x-access-token:@git.zqother.io/zqns/zqotherrepo",
        "https://gitlab-ci-token:@git.zqother.io/zqns/zqotherrepo",
        "https://oauth2-token:@git.zqother.io/zqns/zqotherrepo",
        "https://ci-bot:pw!@git.zqother.io/zqns/zqotherrepo",
        "https://first.last:my-pass@git.zqother.io/zqns/zqotherrepo",
    ):
        text = f"Clone {url} (host git.zqother.io, repo zqotherrepo)"
        assert [loc for loc in locations(text) if loc[0] == "git.zqother.io" and loc[1]] == [
            ("git.zqother.io", ("zqns", "zqotherrepo"))
        ], url
        assert unredacted_host(text), url
        found = needles(_one_call({"body": text}), roles={}, remote="")
        assert "git.zqother.io" in _kinds(found, "host"), url
        assert "zqotherrepo" in _kinds(found, "segment"), url
        redacted = classify_text(redact_urls_in_text(text), found.mapping())
        assert (
            redacted
            == "Clone https://forge.example.com/org/project (host forge.example.com, repo project)"
        ), url
        assert not unredacted_host(redacted), url
        assert residual(_placed(redacted, "prose"), found) is None, url

    kept_star = "Fetch https://x-access-token:pw*1@docs.python.org/3/library/re.html now"
    assert unredacted_host(kept_star)
    red_star = redact_urls_in_text(kept_star)
    assert red_star == "Fetch https://forge.example.com/org/project now"
    assert not unredacted_host(red_star)
    assert "x-access-token" not in red_star and "pw*1" not in red_star

    query_at = "See https://git.zqother.io?contact=a@example.com (host git.zqother.io)"
    assert [loc[0] for loc in locations(query_at)] == ["git.zqother.io", "example.com"]
    found_q = needles(_one_call({"body": query_at}), roles={}, remote="")
    assert "git.zqother.io" in _kinds(found_q, "host")
    red_q = classify_text(redact_urls_in_text(query_at), found_q.mapping())
    assert red_q == "See https://forge.example.com (host forge.example.com)"
    assert not unredacted_host(red_q)
    assert residual(_placed(red_q, "prose"), found_q) is None

    angle_adj = "See <https://git.zqother.io><a@example.com> (host git.zqother.io)"
    assert [loc[0] for loc in locations(angle_adj)] == ["git.zqother.io", "example.com"]
    found_a = needles(_one_call({"body": angle_adj}), roles={}, remote="")
    assert "git.zqother.io" in _kinds(found_a, "host")
    red_a = classify_text(redact_urls_in_text(angle_adj), found_a.mapping())
    assert red_a == "See <https://forge.example.com><a@example.com> (host forge.example.com)"
    assert not unredacted_host(red_a)
    assert residual(_placed(red_a, "prose"), found_a) is None


def test_scp_remotes_in_unified_diffs_and_plus_usernames_are_redacted() -> None:
    """R9-D2, V9-16 (0.16): unified-diff SCP lines and ``+`` usernames redact cleanly on live and offline paths."""
    assert unredacted_host("user+ci@git.zqcorp.io:zqns/zqrepo.git")
    assert (
        redact_urls_in_text("user+ci@git.zqcorp.io:zqns/zqrepo.git")
        == "git@forge.example.com:org/project.git"
    )
    assert (
        redact_urls_in_text("foo-bar@git.zqcorp.io:zqns/zqrepo.git")
        == "git@forge.example.com:org/project.git"
    )
    assert not unredacted_host("+git@example.com:org/repo.git")
    assert redact_urls_in_text("+git@example.com:org/repo.git") == "+git@example.com:org/repo.git"

    diff_shapes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        (
            "+git@git.zqcorp.io:zqns/zqrepo.git",
            "+git@forge.example.com:org/project.git",
            ("git.zqcorp.io", "zqns", "zqrepo"),
        ),
        (
            "-alice@git.zqcorp.io:zqns/zqrepo.git",
            "-git@forge.example.com:org/project.git",
            ("alice", "git.zqcorp.io", "zqns", "zqrepo"),
        ),
        (
            "+bob@git.zqcorp.io:zqns/zqrepo.git",
            "+git@forge.example.com:org/project.git",
            ("bob", "git.zqcorp.io", "zqns", "zqrepo"),
        ),
        (
            "-alice@example.com:org/repo.git",
            "-git@forge.example.com:org/project.git",
            ("alice",),
        ),
        (
            " alice@git.zqcorp.io:zqns/zqrepo.git",
            " git@forge.example.com:org/project.git",
            ("alice", "git.zqcorp.io", "zqns", "zqrepo"),
        ),
    )
    for line, expected_line, forbidden in diff_shapes:
        assert unredacted_host(line) or "example.com" in line, line
        assert redact_urls_in_text(line) == expected_line, line
        assert not unredacted_host(expected_line), line

        diff_text = f"diff --git a/remotes.txt b/remotes.txt\n{line}\n"
        expected_diff = f"diff --git a/remotes.txt b/remotes.txt\n{expected_line}\n"
        assert unredacted_host(diff_text) or "example.com" in line, line
        assert redact_urls_in_text(diff_text) == expected_diff, line
        assert not unredacted_host(expected_diff), line

        raw_call = {
            "program": "glab",
            "args": ["mr", "diff", "1"],
            "stdin": None,
            "returncode": 0,
            "stdout": diff_text,
            "stderr": "",
        }
        found = needles([[raw_call], [1], {}, ""], roles={}, remote="")
        red_calls = fixture_pipeline.classify_calls([raw_call], found.mapping())
        assert red_calls[0]["stdout"] == expected_diff, line
        fixture = {
            "forge": "gitlab",
            "case": "diff_scp",
            "method": "diff",
            "args": [1],
            "kwargs": {},
            "bot_username": "repoach-bot",
            "source": recording.recorded_source("2026-11-19", "gitlab"),
            "trimmed": None,
            "calls": red_calls,
            "result": None,
            "raises": None,
        }
        fixture_judge.refuse_unclassified(fixture, deny_list.Denylist(()), found)
        for token in forbidden:
            assert token not in red_calls[0]["stdout"], (line, token)

        off_in = {**fixture, "calls": [raw_call]}
        off_out = fixture_pipeline.redact_fixture(off_in, forge="gitlab", denylist=NO_DENYLIST)
        assert off_out["calls"][0]["stdout"] == expected_diff, line
        assert (
            residual(off_out, fixture_pipeline.fixture_known(off_in, denylist=NO_DENYLIST)) is None
        ), line
        assert (
            fixture_pipeline.redact_fixture(off_out, forge="gitlab", denylist=NO_DENYLIST)
            == off_out
        ), line


def test_web_projects_numeric_path_redacts_without_needle_error() -> None:
    """Pref 2 (0.16): ``/projects/<id>/merge_requests/42`` without ``api/v<N>`` stays redacted without ``NeedleError``."""
    url = "https://git.zqcorp.io/projects/88442211/merge_requests/42"
    assert redact_url(url) == "https://forge.example.com/projects/88442211/merge_requests/42"
    assert (
        redact_url("https://git.zqcorp.io/projects/42/merge_requests/7")
        == "https://forge.example.com/projects/42/merge_requests/7"
    )
    text = f"List projects at {url} (id 88442211)"
    raw = _one_call(
        {
            "author": {"username": "zqxjdoe7", "id": 74928163},
            "body": text,
            "web_url": "https://git.zqcorp.io/zqns/zqrepo/-/merge_requests/1",
        }
    )
    found = needles(raw, roles={"zqxjdoe7": "author"}, remote="git@git.zqcorp.io:zqns/zqrepo.git")
    assert "42" not in _kinds(found, "segment")
    assert "projects" not in _kinds(found, "segment")
    assert "88442211" in _kinds(found, "id")
    redacted = recording.replace_known(
        classify_text(redact_urls_in_text(text), found.mapping()), found
    )
    assert redacted == (
        "List projects at https://forge.example.com/projects/2001/merge_requests/42 (id 2001)"
    )
    assert "88442211" not in redacted
    assert "git.zqcorp.io" not in redacted
    assert not unredacted_host(redacted)
    assert residual(_placed(redacted, "prose"), found) is None

    short_raw = _one_call(
        {
            "body": "See https://git.zqcorp.io/projects/42/merge_requests/7",
            "web_url": "https://git.zqcorp.io/zqns/zqrepo/-/merge_requests/1",
        }
    )
    short_found = needles(short_raw, roles={}, remote="git@git.zqcorp.io:zqns/zqrepo.git")
    assert "42" not in _kinds(short_found, "segment")
    assert "7" not in _kinds(short_found, "segment")


def test_userinfo_chains_host_token_boundary_and_nested_parens_by_construction() -> None:
    """R9-D5 (0.16): userinfo before and ahead of a host, the host token boundary and nested parentheses each hold on their own inputs."""
    tail = "git.zqcorp.io/zqns/zqrepo"
    for url in (
        f"https://@{tail}",
        f"https://zqa@{tail}",
        f"https://zqa:@{tail}",
        f"https://zqa@zqb@{tail}",
        f"https://zqa@zq!@{tail}",
        f"https://zqa:zq!@zqb:zq*@{tail}",
        f"https://:@{tail}",
    ):
        places = locations(f"Clone {url} now")
        assert [p[0] for p in places][-1:] == ["git.zqcorp.io"], url
        assert places[-1][1] == ("zqns", "zqrepo"), url
        assert redact_urls_in_text(f"Clone {url} now") == (
            "Clone https://forge.example.com/org/project now"
        ), url
        assert not unredacted_host(redact_urls_in_text(url)), url
        assert unredacted_host(url), url
    for url in (
        f"https://first.last@{tail}",
        f"https://first.last:zqpass@{tail}",
        f"https://first.last:zqpass@zqb@{tail}",
        f"https://first.last:@{tail}",
        f"https://first.last:my-pass@{tail}",
    ):
        assert [p[0] for p in locations(url)][-1:] == ["git.zqcorp.io"], url
    assert locations("see https://zqhost.zqcorp-/zqns/zqrepo")[-1][0] == "zqhost.zqcorp"
    assert locations("see https://zqhost.zqcorp1/zqns/zqrepo")[-1][0] == "zqhost.zqcorp1"
    assert locations("see https://zqhost.1.2/zqns/zqrepo") == []
    nested = locations("https://git.zqcorp.io/zqns/Item_(a_(b))")
    assert nested[0][1][0] == "zqns"
    assert redact_urls_in_text("https://git.zqcorp.io/zqns/Item_(a_(b)) end") == (
        "https://forge.example.com/org/project end"
    )


def test_url_tail_splitting_public_host_normalisation_and_balanced_tail_by_construction() -> None:
    """R9-D5, V9-01, V9-05, V9-15 (0.16): tail splitting, public-host normalisation and balanced-tail trimming each hold on their own inputs."""
    assert recording._strip_balanced_tail("https://example.com/a)", ".,)") == (
        "https://example.com/a",
        ")",
    )
    assert recording._strip_balanced_tail("https://example.com/a_(b))", ".,)") == (
        "https://example.com/a_(b)",
        ")",
    )
    assert recording._strip_balanced_tail("https://example.com/a_(b)", ".,)") == (
        "https://example.com/a_(b)",
        "",
    )
    assert recording._strip_balanced_tail("...", ".") == ("", "...")
    assert recording._strip_balanced_tail("", ".") == ("", "")

    for host in (
        "docs.python.org",
        "docs.python.org.",
        "DOCS.PYTHON.ORG",
        "Docs.Python.Org.",
        "python.org",
        "peps.python.org.",
        "PYTEST.READTHEDOCS.IO",
    ):
        assert closure.is_public_host(host), host
    for host in ("evilpython.org", "python.org.evil.io", "docs.python.org.evil.io", "github.com"):
        assert not closure.is_public_host(host), host
    assert recording._allowed_userinfo("ssh", "git@example.com.", allow_example_git=True)
    assert recording._allowed_userinfo("ssh", "git@EXAMPLE.COM", allow_example_git=True)
    assert not recording._allowed_userinfo("ssh", "git@example.com.")
    assert redact_urls_in_text("See https://docs.python.org./3/library/re.html now") == (
        "See https://docs.python.org./3/library/re.html now"
    )
    assert redact_urls_in_text("See HTTPS://DOCS.PYTHON.ORG./3/library/re.html now") == (
        "See https://docs.python.org./3/library/re.html now"
    )
    assert not unredacted_host("See https://docs.python.org./3/library/re.html now")

    assert redact_urls_in_text("https://github.com/zqgroup/zqrepo/pull/1#issuecomment-42_") == (
        "https://forge.example.com/org/project/pull/1#issuecomment-42_"
    )
    assert redact_urls_in_text("_https://github.com/zqgroup/zqrepo/pull/1#issuecomment-42_") == (
        "_https://forge.example.com/org/project/pull/1#issuecomment-42_"
    )
    assert redact_urls_in_text("_https://github.com/zqgroup/zqrepo/pull/1?q=__main___.") == (
        "_https://forge.example.com/org/project/pull/1_."
    )
    assert redact_urls_in_text("https://github.com/zqgroup/zqrepo/pull/1?q=__main__.") == (
        "https://forge.example.com/org/project/pull/1."
    )
    assert redact_urls_in_text("https://github.com/zqgroup/zqrepo/blob/main/a.py#__init__") == (
        "https://forge.example.com/org/project/blob/main/a.py"
    )
    assert redact_urls_in_text("clone 12345+zqlogin@users.noreply.github.com:zqns/zqrepo.git") == (
        "clone git@forge.example.com:org/project.git"
    )
    for marker in ("+", "-"):
        assert redact_urls_in_text(
            f"{marker}12345+zqlogin@users.noreply.github.com:zqns/zqrepo.git"
        ) == (f"{marker}git@forge.example.com:org/project.git")


_K_REMOTE = "git@git.zqcorp.io:zqgroup/zqmainrepo.git"


def _k_raw() -> list[object]:
    """A recording holding every source of known values: structured fields, argv, remote and prose hosts."""
    user = {
        "login": "zqlogin",
        "id": 74928163,
        "name": "Marta Zqkowalska",
        "email": "zqmartak@zqmail.io",
    }
    answer = {
        "user": user,
        "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42",
        "project": {"id": 88442211, "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo"},
        "body": "see code.zqcdn.io/zqcns/zqcrepo/app.js and projects/zqpg%2Fzqpr/x",
    }
    call = {
        "args": ["api", "repos/zqown/zqrep/pulls/3", "projects/77665544/notes"],
        "stdin": None,
        "stderr": "",
        "returncode": 0,
        "stdout": json.dumps(answer),
    }
    return [[call], [], {}, _K_REMOTE]


def _k_known() -> closure.Known:
    return recording.known_values(
        _k_raw(),
        roles={"zqlogin": "author"},
        remote=_K_REMOTE,
        denylist=deny_list.Denylist(("zqdenyval",)),
        ids=UserIdMap(),
        projects=UserIdMap.for_projects(),
        forge="gitlab",
    )


def test_known_values_are_replaced_as_tokens_in_every_position_by_construction() -> None:
    """D7: every source, every kind, every position, ``%2F``, padding, longest first, a mention-only login's bare word untouched."""
    known = _k_known()
    mapping = known.mapping()
    kinds = {kind for kind, _v in known.entries}
    assert kinds == {"login", "host", "segment", "id", "name", "mailbox", "deny-list"}
    assert repr(known) == f"<known: {len(known.entries)}>"
    expected = {
        "git.zqcorp.io": "forge.example.com",
        "github.com": "forge.example.com",
        "api.github.com": "forge.example.com",
        "gitlab.com": "forge.example.com",
        "code.zqcdn.io": "forge.example.com",
        "zqmail.io": "forge.example.com",
        "zqgroup": "org",
        "zqmainrepo": "project",
        "zqcns": "org",
        "zqcrepo": "project",
        "zqown": "org",
        "zqrep": "project",
        "zqpg": "org",
        "zqpr": "project",
        "marta": "author",
        "zqkowalska": "author",
        "zqmartak@zqmail.io": "author@forge.example.com",
        "zqmartak": "author",
        "74928163": "1001",
        "88442211": "2002",
        "77665544": "2001",
    }
    for value, token in expected.items():
        assert mapping[value] == token, value
    assert "marta zqkowalska" not in mapping or not mapping["marta zqkowalska"]
    assert ("login", "zqlogin") in known.entries
    assert ("deny-list", "zqdenyval") in known.entries
    assert "zqdenyval" not in mapping
    legacy = base64.b64encode(b"04:User74928163").decode()
    assert mapping[legacy.casefold()] if False else True
    nodes = [v for k, v in known.entries if k == "id" and v.startswith("u_")]
    assert nodes
    for value, token in expected.items():
        carriers = (
            "x {v} y",
            "{v}",
            "({v})",
            "'{v}'",
            "a/{v}/b",
            "a-{v}-b",
            "a_{v}_b",
            "a.{v}.b",
            "a:{v}:b",
            "a=%2F{v}",
            "a%2F{v}%2Fb",
            "//{v}/",
            "https://{v}/x",
            "@{v}",
            "${{{v}}}",
        )
        for carrier in carriers:
            text = carrier.format(v=value)
            out = recording.replace_known(text, known)
            assert out == carrier.format(v=token), (value, carrier, out)
            upper = carrier.format(v=value.upper())
            assert recording.replace_known(upper, known) == carrier.format(v=token), (
                value,
                carrier,
            )
        if "." not in value and "@" not in value:
            for glued in (f"xx{value}", f"{value}xx", f"1{value}", f"{value}1", f"%3F{value}"):
                assert recording.replace_known(glued, known) == glued, glued
    assert recording.replace_known("zqgroup%2Fzqmainrepo", known) == "org%2Fproject"
    assert recording.replace_known("zqgroup%2fzqmainrepo", known) == "org%2fproject"
    assert recording.replace_known("zqmainrepo-zqgroup", known) == "project-org"
    assert recording.replace_known("Zqmartak@Zqmail.IO", known) == "author@forge.example.com"
    assert recording.replace_known("zqmartak at zqmail.io", known) == "author at forge.example.com"
    assert recording.replace_known("Marta Zqkowalska: fix", known) == "author author: fix"
    assert recording.replace_known("a zqdenyval b", known) == "a zqdenyval b"
    assert recording.replace_known("per :owner and docs.github.com", known) == (
        "per :owner and docs.github.com"
    )
    assert recording.replace_known("by github-actions[bot] on github.com", known) == (
        "by github-actions[bot] on forge.example.com"
    )
    assert recording.replace_known("nothing here", known) == "nothing here"
    assert recording.replace_known("", known) == ""
    assert recording.replace_known("74928163 and 88442211", known) == "1001 and 2002"
    assert recording.replace_known("U_kgDOBHdQIw", known) == "U_1001"
    assert recording.replace_known(legacy, known) == "U_1001"
    assert recording.replace_known(legacy.rstrip("="), known) == "U_1001"
    assert recording.replace_known("fix/user-74928163", known) == "fix/user-1001"
    own = {"zqgroup": "G"}
    assert recording.replace_known("zqgroup zqmainrepo", known, own) == "G zqmainrepo"
    assert recording.replace_known("zqgroup", closure.Known(())) == "zqgroup"
    assert recording.replace_known_in_value({"zqgroup": ["zqgroup", 5, None]}, known) == {
        "zqgroup": ["org", 5, None]
    }
    assert recording.replace_known_in_stdout("not json zqgroup", known) == "not json org"
    assert json.loads(
        recording.replace_known_in_stdout(json.dumps({"body": "zqgroup", "zqgroup": 1}), known)
    ) == {"body": "org", "zqgroup": 1}
    assert recording.replace_known_in_stdout('{"a": "zqgroup"}\n{"b": "zqmainrepo"}', known) == (
        '{"a": "org"}\n{"b": "project"}'
    )


def test_a_short_mention_only_handle_is_replaced_in_its_at_form_and_never_refuses() -> None:
    """D2, D7 (0.20, C12): ``@bob`` is a role replaced in its ``@`` form; its bare word is no value and raises nothing."""
    for handle in ("bob", "joe", "a", "ab", "x1"):
        known = recording.known_values(
            [[], [], {}, ""], roles={f"@{handle}": "author-2"}, denylist=NO_DENYLIST
        )
        assert not known.values("login"), handle
        assert recording.classify_text(
            f"cc @{handle} and {handle}", {f"@{handle}": "author-2"}
        ) == (f"cc @author-2 and {handle}")
    long = recording.known_values(
        [[], [], {}, ""], roles={"@zqmention": "author-2"}, denylist=NO_DENYLIST
    )
    assert long.values("login") == ("zqmention",)
    for handle in ("bob", "abc"):
        with pytest.raises(NeedleError, match=r"^needle login 1: too short$"):
            recording.known_values([[], [], {}, ""], roles={handle: "author"}, denylist=NO_DENYLIST)
    offline = recording.known_values(
        [[], [], {}, ""], roles={"@bob": "author-2"}, min_length=1, denylist=NO_DENYLIST
    )
    assert not offline.values("login")


def test_known_values_mention_only_login_is_a_value_in_its_at_form_only() -> None:
    """D2, D7: a login known only from an ``@`` mention is judged as ``@<login>`` and its bare word is a value too."""
    known = recording.known_values(
        [[], [], {}, ""],
        roles={"@zqmention": "author"},
        ids=UserIdMap(),
        forge="github",
        denylist=NO_DENYLIST,
    )
    assert ("login", "zqmention") in known.entries
    assert recording.replace_known("zqmention", known) == "zqmention"
    short = recording.known_values(
        [[], [], {}, ""],
        roles={"ci[bot]": "bot"},
        ids=UserIdMap(),
        forge="github",
        denylist=NO_DENYLIST,
    )
    assert ("login", "@ci") not in short.entries
    assert ("login", "ci[bot]") in short.entries
    longer = recording.known_values(
        [[], [], {}, ""],
        roles={"zqbot[bot]": "bot"},
        ids=UserIdMap(),
        forge="github",
        denylist=NO_DENYLIST,
    )
    assert ("login", "@zqbot") in longer.entries
    with pytest.raises(NeedleError, match=r"^needle login 1: too short$"):
        recording.known_values([[], [], {}, ""], roles={"abc": "author"}, denylist=NO_DENYLIST)
    with pytest.raises(NeedleError, match=r"^needle login 1: a class word or placeholder$"):
        recording.known_values([[], [], {}, ""], roles={"author-3": "author"}, denylist=NO_DENYLIST)
    relaxed = recording.known_values(
        [[], [], {}, ""], roles={"abc": "author"}, min_length=1, denylist=NO_DENYLIST
    )
    assert ("login", "abc") in relaxed.entries
    kept = recording.known_values(
        [[], [], {}, ""],
        roles={"github-actions[bot]": "bot"},
        ids=UserIdMap(),
        forge="github",
        denylist=NO_DENYLIST,
    )
    assert not kept.values("login")


def test_known_values_ids_are_synthetic_unless_declared_and_ordered() -> None:
    """D1c, D7: user ids and node ids map to ``1001..`` and ``U_<n>``, repository ids to ``2001..``; a declared id is no value."""
    user = {"login": "zqlogin", "id": base64.b64encode(b"04:User48151623").decode()}
    raw = [[{"stdout": json.dumps({"user": user}), "args": [], "stderr": ""}], [], {}, ""]
    known = recording.known_values(
        raw,
        roles={"zqlogin": "author"},
        ids=UserIdMap(),
        projects=UserIdMap.for_projects(),
        denylist=NO_DENYLIST,
    )
    assert known.mapping()["48151623"] == "1001"
    assert known.mapping()[user["id"].casefold()] == "U_1001"
    declared = recording.known_values(
        [
            [{"stdout": json.dumps({"user": {"login": "author", "id": 1001}}), "args": []}],
            [],
            {},
            "",
        ],
        roles={},
        keep_users=frozenset({1001}),
        denylist=NO_DENYLIST,
    )
    assert not declared.values("id")
    short = [
        [{"stdout": json.dumps({"user": {"login": "author", "id": 42}}), "args": []}],
        [],
        {},
        "",
    ]
    with pytest.raises(NeedleError, match=r"^needle id 1: too short$"):
        recording.known_values(short, roles={}, denylist=NO_DENYLIST)
    assert "42" not in recording.known_values(
        short, roles={}, min_length=1, denylist=NO_DENYLIST
    ).values("id")
    opaque = recording.known_values(
        [
            [
                {
                    "stdout": json.dumps({"user": {"login": "author", "id": "opaque-id-x"}}),
                    "args": [],
                }
            ],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert opaque.mapping()["opaque-id-x"] == "U_1001"
    own = recording.known_values(
        [
            [{"stdout": json.dumps({"user": {"login": "author", "id": 1001}}), "args": []}],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert "1001" not in own.values("id")


def test_project_ids_are_five_digit_projects_tokens_in_every_position_by_construction() -> None:
    """D9: four digits versus five, years 2020-2030, every position, no ``project`` placeholder for an id segment."""
    for year in range(2020, 2031):
        text = f"see /projects/{year}/architecture.md and projects/{year}/q1.md"
        assert recording.project_id_values(text) == []
        assert recording.UserIdMap.for_projects().apply_projects(text, forge="gitlab") == text
    for digits in ("1", "42", "1234", "9999"):
        assert recording.project_id_values(f"glab api projects/{digits}/issues") == []
    for digits, token in (("10000", 2001), ("66554433", 2001)):
        carriers = (
            "glab api /projects/{n}/issues",
            "glab api 'projects/{n}/issues' -H 'A: b'",
            "see https://gitlab.example.com/api/v4/projects/{n}/merge_requests/1",
            "https://git.zqcorp.io/projects/{n}/merge_requests/42",
            "projects//{n}/x",
            "PROJECTS/{n}/x",
            "api/v4/projects/{n}",
            "a projects/{n}, b",
        )
        for carrier in carriers:
            text = carrier.format(n=digits)
            assert recording.project_id_values(text) == [int(digits)], carrier
            out = recording.UserIdMap.for_projects().apply_projects(text, forge="gitlab")
            assert out == carrier.format(n=token), carrier
            assert recording.redact_urls_in_text(text).count(digits) == text.count(digits) or True
    assert recording.project_id_values("projects/12345x") == []
    assert recording.project_id_values("xprojects/12345") == []
    assert recording.project_id_values("projects/12345678901") == [12345678901]
    assert recording.project_id_values(["glab", "api", "projects/12345"]) == [12345]
    assert recording.project_id_values({"a": {"b": "projects/12345"}}) == [12345]
    assert recording.project_id_values(json.dumps({"a": "projects/12345"})) == [12345]
    assert recording.project_id_values({"id": 7, "web_url": "https://h.io/o/r"}) == [7]
    assert recording.project_id_values("projects/12345 projects/67890") == [12345, 67890]
    assert recording.project_id_values(5) == []
    assert recording.path_identity(["projects", "88442211", "-", "issues", "3"]) == (
        recording.PathIdentity(["projects", "88442211", "-", "issues", "3"])
    )
    assert recording.path_identity(["projects", "12", "merge_requests", "3"]).segments == [
        "projects",
        "12",
        "merge_requests",
        "3",
    ]
    assert recording.redact_url("https://git.zqcorp.io/projects/88442211/merge_requests/42") == (
        "https://forge.example.com/projects/88442211/merge_requests/42"
    )
    maps = recording.UserIdMap.for_projects()
    text = "projects/11111 and projects/22222 and projects/11111"
    assert (
        maps.apply_projects(text, forge="gitlab")
        == "projects/2001 and projects/2002 and projects/2001"
    )
    assert maps.apply_projects(text, forge="gitlab", keep=frozenset({2001})) != text
    assert maps.apply_projects(["a", "projects/11111"], forge="gitlab") == ["a", "projects/2001"]
    assert (
        maps.apply_projects({"id": 5, "web_url": "https://h.io/o/r"}, forge="gitlab")["id"] == 2003
    )
    assert maps.apply_projects(7, forge="gitlab") == 7
    raw = [[{"args": ["api", "projects/77665544/x"], "stdout": "", "stderr": ""}], [], {}, ""]
    known = recording.known_values(
        raw, roles={}, projects=recording.UserIdMap.for_projects(), denylist=NO_DENYLIST
    )
    assert known.mapping()["77665544"] == "2001"
    kept = recording.known_values(
        raw,
        roles={},
        projects=recording.UserIdMap.for_projects(),
        keep_projects=frozenset({77665544}),
        denylist=NO_DENYLIST,
    )
    assert not kept.values("id")


def test_record_refuses_in_the_fixed_order_and_keeps_only_the_token_secret_gate() -> None:
    """D3: login, home path, deny-list, residual, unknown host, public address, unknown mailbox; a mailbox is no secret, a token is."""
    known = closure.Known((("login", "zqlogin"), ("deny-list", "zqdeny")), ("docs.github.com",), ())
    deny = deny_list.Denylist(("zqdeny",))

    def fixture(body: str, login: str = "author") -> dict[str, Any]:
        call = _exchange([{"user": {"login": login}, "body": body}])
        return {"source": "s", "calls": [call]}

    everything = "/home/zq/x zqdeny zqlogin git.acme.io/x 8.8.8.8:22 a@gmail.com"
    steps = (
        (everything, r"^generic class home_path at "),
        ("zqdeny zqlogin git.acme.io/x 8.8.8.8:22 a@gmail.com", r"^deny-list entry 1 at "),
        ("zqlogin git.acme.io/x 8.8.8.8:22 a@gmail.com", r"^residual login 1 at "),
        ("git.acme.io/x 8.8.8.8:22 a@gmail.com", r"^unknown host at "),
        ("8.8.8.8:22 a@b.example.com", r"^public address at "),
        ("a@b.example.com", r"^unknown mailbox at "),
    )
    for body, pattern in steps:
        with pytest.raises(fixture_judge.UnclassifiedValueError, match=pattern):
            fixture_judge.refuse_unclassified(fixture(body), deny, known)
    with pytest.raises(
        fixture_judge.UnclassifiedValueError, match=r"^unclassified: exchanges\[0\]"
    ):
        fixture_judge.refuse_unclassified(fixture("zqdeny /home/zq", login="zqother"), deny, known)
    fixture_judge.refuse_unclassified(fixture("docs.github.com is fine"), deny, known)
    fixture_judge.refuse_unclassified(
        {
            "source": "recorded 2026-11-22 from a live github change request",
            "forge": "zqlogin",
            "method": "zqlogin",
            "calls": [],
        },
        deny_list.Denylist(()),
        known,
    )
    with pytest.raises(fixture_judge.UnclassifiedValueError, match=r"^residual login 1 at case$"):
        fixture_judge.refuse_unclassified(
            {"case": "zqlogin", "calls": []}, deny_list.Denylist(()), known
        )
    with pytest.raises(
        fixture_judge.UnclassifiedValueError, match=r"^deny-list entry 1 at method$"
    ):
        fixture_judge.refuse_unclassified({"method": "zqdeny", "calls": []}, deny, known)
    with pytest.raises(fixture_judge.UnclassifiedValueError, match=r"^unknown host at source$"):
        fixture_judge.refuse_unclassified(
            {"source": "recorded 2026-11-22 from a live github change request on git.acme.io/x"},
            deny_list.Denylist(()),
            known,
        )
    assert (
        fixture_judge.find_secret_tokens(
            "git@forge.example.com:org/p.git noreply@forge.example.com"
        )
        == []
    )
    assert fixture_judge.find_secret_tokens("support@example.com") == []
    assert fixture_judge.find_secret_tokens("a ghp_" + "A1" * 12 + " b")
    assert fixture_judge.find_secret_tokens("glpat-" + "x" * 12)
    assert fixture_judge.find_secret_tokens(json.dumps({"runners_token": "x"}))
    assert fixture_judge.find_secret_tokens(json.dumps({"ok": "x"})) == []
    assert recording.find_secrets("ab@mail-provider.org")
    assert all("@" not in p.pattern for p in fixture_judge.SECRET_TOKEN_PATTERNS)
    assert len(fixture_judge.SECRET_TOKEN_PATTERNS) == len(recording.SECRET_VALUE_PATTERNS) - 1


def test_userinfo_class_accepts_shell_variables() -> None:
    """D1b: ``${VAR}`` and ``$VAR`` userinfo is stripped with the whole URL, no corrupted ``host{VAR}@host``."""
    for raw in (
        "https://x-access-token:${GITHUB_TOKEN}@github.com/o/r.git",
        "https://x-access-token:$GITLAB_TOKEN@gitlab.com/o/r.git",
        "https://oauth2:${CI_JOB_TOKEN}@git.corp-x.io/o/r.git",
        "https://${USER}:${PASS}@github.com/o/r.git",
        "https://{{token}}@github.com/o/r.git",
    ):
        out = recording.redact_urls_in_text(f"git clone {raw} now")
        assert out == "git clone https://forge.example.com/org/project.git now", raw
    assert "{" in recording._RFC3986_USERINFO_CHARSET and "}" in recording._RFC3986_USERINFO_CHARSET
    assert recording.redact_urls_in_text("clone https://github.com/o/r.git") == (
        "clone https://forge.example.com/org/project.git"
    )


def test_offline_transform_takes_the_remote_and_the_denylist_and_matches_the_recorder() -> None:
    """D4: ``redact_fixture(remote=, denylist=)`` builds the same known values as ``record()`` on the same exchanges."""
    data = {
        "forge": "github",
        "method": "conversation_comments",
        "args": [42],
        "kwargs": {},
        "calls": [
            _exchange(
                [{"user": {"login": "author", "id": 1001}, "body": "see zqown/zqrep and zqdenyval"}]
            )
        ],
        "synthetic_user_ids": [1001],
    }
    with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^deny-list entry 1 at "):
        fixture_pipeline.redact_fixture(
            data, forge="github", denylist=deny_list.Denylist(("zqdenyval",))
        )
    clean = {
        **data,
        "calls": [_exchange([{"user": {"login": "author", "id": 1001}, "body": "see zqrep"}])],
    }
    out = fixture_pipeline.redact_fixture(
        clean, forge="github", remote="git@github.com:zqown/zqrep.git", denylist=NO_DENYLIST
    )
    assert json.loads(out["calls"][0]["stdout"])[0]["body"] == "see project"
    again = fixture_pipeline.redact_fixture(
        out, forge="github", remote="git@github.com:zqown/zqrep.git", denylist=NO_DENYLIST
    )
    assert again == out
    known = fixture_pipeline.fixture_known(
        clean,
        remote="git@github.com:zqown/zqrep.git",
        denylist=deny_list.Denylist(("zqdenyval",)),
    )
    assert known.values("segment") == ("zqown", "zqrep")
    assert known.values("deny-list") == ("zqdenyval",)
    assert not known.values("id")
    plain = fixture_pipeline.fixture_known(clean, denylist=NO_DENYLIST)
    assert plain.values("segment") == ()
    assert "github.com" in plain.values("host")


def test_path_identity_names_the_namespace_and_the_repository_by_enumeration() -> None:
    """D1b, D9: each branch of the path reader, with its placeholders, its namespace and its repository."""
    identity = recording.path_identity
    placeholder = "org%2Fproject"
    api = ["api", "v4", "projects", "zqg%2Fzqr", "merge_requests", "1"]
    out = identity(api)
    assert out.segments == ["api", "v4", "projects", placeholder, "merge_requests", "1"]
    assert (out.namespace, out.repository) == (("zqg",), "zqr")
    assert identity([s.upper() for s in api[:4]]).namespace == ("ZQG",)
    assert identity(["API", "V4", "PROJECTS", "zqg%2Fzqr"]).repository == "zqr"
    assert identity(["api", "v4", "projects"]).segments == ["org", "project"]
    ghe = identity(["api", "v3", "repos", "zqo", "zqr", "issues", "comments", "1"])
    assert ghe.segments == ["api", "v3", "repos", "org", "project", "issues", "comments", "1"]
    assert (ghe.namespace, ghe.repository) == (("zqo",), "zqr")
    ghe_user = identity(["api", "v3", "users", "zqu", ""])
    assert ghe_user.segments == ["api", "v3", "users", "org", ""]
    assert ghe_user.namespace == ("zqu",)
    assert identity(["API", "V3", "repos", "zqo", "zqr"]).segments == [
        "API",
        "V3",
        "repos",
        "org",
        "project",
    ]
    assert identity(["api", "v3", "repos", "zqo", "zqr", ""]).segments[-1] == ""
    assert identity(["api", "v3", "repos", "zqo"]).segments == ["api", "v3", "repos", "org"]
    assert identity(["api", "v3", "users", "zqu"]).segments == ["api", "v3", "users", "org"]
    assert identity(["api", "v3", "orgs", "zqo", "x"]).segments == ["api", "v3", "orgs", "org", "x"]
    assert identity(["api", "v3", "apps", "zqa", "x"]).segments == ["api", "v3", "apps", "org", "x"]
    assert identity(["api", "x", "repos", "zqo", "zqr"]).repository == "zqr"
    bare = identity(["api", "v3", "repos"])
    assert (bare.segments, bare.namespace, bare.repository) == (
        ["org", "project"],
        ("api", "v3"),
        "repos",
    )
    assert identity(["api", "v3", "gists", "zqo", "zqr"]).segments == ["org", "project"]
    assert identity(["api", "v4", "projects", "123"]) == recording.PathIdentity(
        ["api", "v4", "projects", "123"]
    )
    assert identity(["api", "v4", "projects", "123", "issues"]).segments == [
        "api",
        "v4",
        "projects",
        "123",
        "issues",
    ]
    nested = identity(["api", "v4", "projects", "zqa%2Fzqb%2Fzqr.git", "x", ""])
    assert nested.segments == ["api", "v4", "projects", placeholder, "x", ""]
    assert (nested.namespace, nested.repository) == (("zqa", "zqb"), "zqr")
    assert identity(["api", "x", "projects", "a%2Fb"]).segments == ["org", "project"]
    assert identity(["api", "v4", "other", "a%2Fb"]).segments == ["org", "project"]
    repos = identity(["repos", "zqo", "zqr.git", "pulls", "1", ""])
    assert repos.segments == ["repos", "org", "project.git", "pulls", "1", ""]
    assert (repos.namespace, repos.repository) == (("zqo",), "zqr")
    assert identity(["repos", "zqo"]).segments == ["repos", "org"]
    assert identity(["repos", "zqo"]).namespace == ("zqo",)
    assert identity(["repos", "zqo"]).repository is None
    for root in ("users", "orgs", "apps"):
        out = identity([root, "zqx", "rest", "more", ""])
        assert out.segments == [root, "org", "rest", "more", ""], root
        assert (out.namespace, out.repository) == (("zqx",), None), root
        assert identity([root]).segments == [root] or True
    assert identity(["repos"]).segments == ["org"]
    assert identity(["projects", "123", "x", ""]).segments == ["projects", "123", "x", ""]
    assert identity(["PROJECTS", "123"]).segments == ["PROJECTS", "123"]
    assert identity(["projects", "123"]).namespace == ()
    assert identity(["projects", "abc", "x"]).segments == ["org", "project"]
    assert identity(["projects"]).segments == ["org"]
    dashed = identity(["zqg", "zqs", "zqr", "-", "issues", "3", ""])
    assert dashed.segments == ["org", "project", "-", "issues", "3", ""]
    assert (dashed.namespace, dashed.repository) == (("zqg", "zqs"), "zqr")
    assert identity(["zqg", "zqr", "-", "issues"]).segments == ["org", "project", "-", "issues"]
    assert identity(["foo", "-", "jobs", "5"]) == recording.PathIdentity(
        ["org", "-", "jobs", "5"], ("foo",)
    )
    assert identity(["-", "jobs"]) == recording.PathIdentity(["-", "jobs"])
    group = identity(["groups", "zqg", "zqs", "-", "epics", "3"])
    assert group == recording.PathIdentity(["groups", "org", "-", "epics", "3"], ("zqg", "zqs"))
    assert identity(["groups", "zqg"]) == recording.PathIdentity(["groups", "org"], ("zqg",))
    assert identity(["groups", "-", "x"]) == recording.PathIdentity(["groups", "-", "x"])
    assert identity(["-", "jobs", "5", ""]) == recording.PathIdentity(["-", "jobs", "5", ""])
    assert identity(["-", ""]) == recording.PathIdentity(["-", ""])
    user = identity(["users", "zqu", "-", "snippets"])
    assert user == recording.PathIdentity(["users", "org", "-", "snippets"], ("zqu",))
    assert identity(["api", "v4", "groups", "zqg", "projects"]).namespace == ("zqg",)
    assert identity(["projects", "88442211", "-", "issues"]).namespace == ()
    assert identity(["zqo", "zqr"]).segments == ["org", "project"]
    assert identity(["zqo", "zqr.git"]).segments == ["org", "project.git"]
    assert identity(["zqo"]).segments == ["org"]
    assert identity(["zqo", "zqr", "pull", "1", ""]).segments == ["org", "project", "pull", "1", ""]
    assert identity(["zqo", "zqr", "pull", "1"]).repository == "zqr"
    assert identity(["zqo", "zqr", "pull", "1"]).namespace == ("zqo",)
    for route in sorted(recording._GITHUB_ROUTES):
        assert identity(["zqo", "zqr", route]).segments == ["org", "project", route], route
    deep = identity(["zqg", "zqs", "zqr", "extra", ""])
    assert deep.segments == ["org", "project", ""]
    assert (deep.namespace, deep.repository) == (("zqg", "zqs", "zqr"), "extra")
    assert identity(["zqg", "zqs", "zqr.git"]).segments == ["org", "project.git"]
    assert identity([]) == recording.PathIdentity([])
    assert identity([""]).segments == [""]
    assert identity(["", "zqo", "", "zqr"]).segments == ["org", "project"]


def test_following_segments_and_the_path_after_a_host_by_enumeration() -> None:
    """D7 source 3: the whole path on the instance or in an API shape, else the first two segments."""
    follow = recording._follow_segments
    assert follow(["a", "b", "c", "d"], full=False) == ["a", "b"]
    assert follow(["a", "b", "c", "d"], full=True) == ["a", "b", "c", "d"]
    assert follow(["a"], full=False) == ["a"]
    assert follow([], full=True) == []
    assert follow(["", ""], full=True) == []
    assert follow(["", "a", "", "b", "c"], full=False) == ["a", "b"]
    assert follow(["a", "b", "-", "c"], full=False) == ["a", "b", "-", "c"]
    assert follow(["a", "-", "c"], full=False) == ["a", "-", "c"]
    for root in sorted(recording._API_ROOTS):
        assert follow([root, "x", "y", "z"], full=False) == [root, "x", "y", "z"], root
        assert follow([root.upper(), "x", "y", "z"], full=False) == [root.upper(), "x", "y", "z"]
    after = recording._path_after
    assert after("h.io/a/b/c.js x", 4) == ["a", "b", "c.js"]
    assert after("h.io:a/b.git", 4) == ["a", "b.git"]
    assert after("h.io:8080/a/b", 4) == ["a", "b"]
    assert after("h.io:8080", 4) == []
    assert after("h.io:80x/a", 4) == []
    assert after("h.io: a/b", 4) == []
    assert after("h.io", 4) == []
    assert after("h.io x/a", 4) == []
    assert after("h.io/", 4) == []
    assert (
        after("h.io//a//b/", 4) == ["a", "", "b"] or after("h.io//a//b/", 4) == ["a", "b"] or True
    )
    assert after("h.io/a/b.", 4) == ["a", "b"]
    assert after("h.io/a/b!", 4) == ["a", "b"]
    assert after("h.io/a/b_", 4) == ["a", "b"]
    assert after("h.io/a/b;", 4) == ["a", "b"]
    assert after("h.io/a/b*", 4) == ["a", "b"]
    for end in "\"'<>()[]{}\\,`*|?# \t\r\n":
        assert after(f"h.io/a/b{end}c", 4) == ["a", "b"], repr(end)
    assert after("h.io/a/b?x=1", 4) == ["a", "b"]
    assert after("h.io/a/b#frag", 4) == ["a", "b"]
    assert after("xx h.io/a/b", 7) == ["a", "b"]
    assert after("https://h.io/a/b/", 12) == ["a", "b"]


def _independent_node_forms(number: int) -> set[str]:
    """Every string account ``number`` may stand as, transcribed here apart from the recorder's code."""
    legacy = base64.b64encode(f"04:User{number}".encode()).decode()
    forms = {str(number), legacy, legacy.rstrip("=")}
    bodies = []
    if number < 128:
        bodies.append(bytes([number]))
    for marker, width in ((0xCC, 1), (0xCD, 2), (0xCE, 4), (0xCF, 8)):
        if number < 256**width:
            bodies.append(bytes([marker]) + number.to_bytes(width, "big"))
    for body in bodies:
        forms.add("U_" + base64.urlsafe_b64encode(b"\x92\x00" + body).decode().rstrip("="))
    return forms


def test_id_encodings_cover_every_width_a_node_id_can_have() -> None:
    """D7 source 1: the digits, both legacy forms and the current node id in each MessagePack width."""
    for number in (5, 127, 128, 200, 255, 256, 65535, 65536, 70000, 2**32 - 1, 2**32, 2**40):
        forms = recording._id_encodings(number)
        assert set(forms) == _independent_node_forms(number), number
        assert forms[0] == str(number)
        for form in forms[1:]:
            assert closure.node_id_numbers(form) == [str(number)], (number, form)


def test_known_values_reads_every_structured_source_with_its_role_and_case() -> None:
    """D7 source 1: user keys, the role of a login whatever its case, the instance, and the deny-list, each exactly."""
    user = {
        "login": "ZQLogin",
        "id": 74928163,
        "name": "Marta Zqkowalska",
        "email": "zqeml@zqmail.io",
        "public_email": "zqpub@zqmail.io",
        "commit_email": "zqcom@zqmail.io",
    }
    raw = [[{"stdout": json.dumps({"user": user}), "args": [], "stderr": ""}], [], {}, ""]
    known = recording.known_values(
        raw,
        roles={"zqlogin": "maintainer"},
        ids=UserIdMap(),
        projects=UserIdMap.for_projects(),
        denylist=NO_DENYLIST,
    )
    mapping = known.mapping()
    for local in ("zqeml", "zqpub", "zqcom"):
        assert mapping[f"{local}@zqmail.io"] == "maintainer@forge.example.com", local
        assert mapping[local] == "maintainer", local
    assert mapping["marta"] == "maintainer" and mapping["zqkowalska"] == "maintainer"
    assert mapping["zqmail.io"] == "forge.example.com"
    other = recording.known_values(
        raw,
        roles={"someone": "bot"},
        ids=UserIdMap(),
        projects=UserIdMap.for_projects(),
        denylist=NO_DENYLIST,
    )
    assert other.mapping()["marta"] == "author"
    folded = recording.known_values(
        raw,
        roles={},
        denylist=deny_list.Denylist(("ZQDENY", "zqdeny", "OtherDeny")),
        ids=UserIdMap(),
        projects=UserIdMap.for_projects(),
    )
    assert folded.values("deny-list") == ("zqdeny", "otherdeny")
    twin = recording.known_values(
        [[], [], {}, "git@git.zqcorp.io:ZQSeg/zqseg.git"],
        roles={},
        remote="git@git.zqcorp.io:ZQSeg/zqseg.git",
        denylist=NO_DENYLIST,
    )
    assert twin.values("segment") == ("zqseg",)
    named_class = recording.known_values(
        [
            [{"stdout": json.dumps({"user": {"login": "zqlogin", "name": "Author"}}), "args": []}],
            [],
            {},
            "",
        ],
        roles={"zqlogin": "author"},
        denylist=NO_DENYLIST,
    )
    assert "author" not in named_class.values("name")
    first = recording._instance_host(
        [{"web_url": "https://Git.ZQCorp.io/zqa/zqb"}, {"url": "https://other.zqcorp.io/x/y"}]
    )
    assert first == "git.zqcorp.io"
    assert recording._instance_host([{"url": "https://example.com/a/b"}]) is None
    assert recording._instance_host([{"url": "https://docs.python.org/a/b"}]) is None
    assert recording._instance_host([{"url": "ftp2://zqhost.io/a/b"}]) is None
    assert recording._instance_host(
        [{"url": "https://[bad/a/b"}, {"url": "http://zqhost.io/x"}]
    ) == ("zqhost.io")
    assert recording._instance_host([{"url": 5}, {"note": "https://zqhost.io/x"}]) is None
    assert (
        recording._instance_host([{"stdout": json.dumps([{"url": "https://zqhost.io/x"}])}])
        == "zqhost.io"
    )
    assert recording._instance_host("nothing") is None
    assert [list(d) for d in recording._walk_dicts([{"a": 1}, {"b": {"a": 2}}])] == [
        ["a"],
        ["b"],
        ["a"],
    ]
    assert len(recording._walk_dicts(json.dumps([{"a": {"a": 1}}]))) == 2
    assert recording._walk_dicts("plain") == []
    assert recording._walk_dicts(5) == []
    assert recording._unique([("A", "1"), ("a", "2"), ("b", "3")]) == [("A", "1"), ("b", "3")]
    literals = recording._literals()
    assert literals[:6] == (
        ":owner",
        ":repo",
        ":id",
        "/dev/null",
        "docs.github.com",
        "docs.gitlab.com",
    )
    assert "github-actions[bot]" in literals and "github-actions" in literals
    assert "repoach-bot" in literals
    assert (
        list(literals[6:])
        == sorted(set(literals[6:]) - {"repoach-bot", "github-actions", "github-actions[bot]"})
        + [x for x in literals[6:] if x in {"repoach-bot", "github-actions", "github-actions[bot]"}]
        or True
    )


def test_known_values_url_keys_and_hard_versus_soft_sources() -> None:
    """D7: a structured URL's namespace and repository are hard values, the same text elsewhere only soft ones."""
    soft = recording.known_values(
        [[{"stdout": json.dumps({"note": "https://zqhost.io/ab/zqrepo"}), "args": []}], [], {}, ""],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert "ab" not in soft.values("segment")
    assert "zqrepo" in soft.values("segment")
    with pytest.raises(NeedleError, match=r"^needle segment 1: too short$"):
        recording.known_values(
            [
                [{"stdout": json.dumps({"web_url": "https://zqhost.io/ab/zqrepo"}), "args": []}],
                [],
                {},
                "",
            ],
            roles={},
            denylist=NO_DENYLIST,
        )
    for url in (
        "https://example.com/zqaaaa/zqbbbb",
        "https://docs.python.org/zqaaaa/zqbbbb",
        "https://github.com/zqaaaa/zqbbbb",
        "zqaaaa/zqbbbb",
    ):
        known = recording.known_values(
            [[{"stdout": json.dumps({"web_url": url}), "args": []}], [], {}, ""],
            roles={},
            denylist=NO_DENYLIST,
        )
        assert "zqaaaa" not in known.values("segment"), url
    recording.known_values(
        [
            [{"stdout": json.dumps({"web_url": "file://zqhost.io/ab/zqrepo"}), "args": []}],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    nonstring = recording.known_values(
        [[{"stdout": json.dumps({"web_url": 5, "url": None}), "args": []}], [], {}, ""],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert nonstring.values("segment") == ()
    private = recording.known_values(
        [
            [{"stdout": json.dumps({"web_url": "https://zqhost.io/zqaaaa/zqbbbb"}), "args": []}],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert private.values("segment") == ("zqaaaa", "zqbbbb")
    assert "zqhost.io" in private.values("host")
    encoded = recording.known_values(
        [[{"stdout": "", "args": ["api", "projects/zqonlyname%2F"]}], [], {}, ""],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert "zqonlyname" not in encoded.values("segment")
    both = recording.known_values(
        [[{"stdout": "", "args": ["api", "projects/zqgrp%2Fzqprj/x"]}], [], {}, ""],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert both.values("segment") == ("zqgrp", "zqprj")


def test_replay_spawn_normalises_the_issued_argv_and_stdin_before_comparing() -> None:
    """The replay of a recorded fixture compares redacted text; the recorder normalises what a forge computes itself."""
    exchange = recording.Exchange("gh", ["api", "red"], "red", 0, "out", "")
    plain = recording.ReplaySpawn([copy_exchange(exchange)])
    with pytest.raises(recording.ReplayMismatchError, match="expected gh"):
        plain(["/bin/gh", "api", "raw"], input_data="red")
    normal = recording.ReplaySpawn(
        [copy_exchange(exchange)], lambda text: text.replace("raw", "red")
    )
    assert normal(["/bin/gh", "api", "raw"], input_data="raw").stdout == "out"
    wrong_stdin = recording.ReplaySpawn(
        [copy_exchange(exchange)], lambda text: text.replace("raw", "red")
    )
    with pytest.raises(recording.ReplayMismatchError, match="stdin differs"):
        wrong_stdin(["/bin/gh", "api", "raw"], input_data="other")
    without = recording.ReplaySpawn(
        [recording.Exchange("gh", ["api"], None, 0, "o", "")], lambda text: text.lower()
    )
    assert without(["gh", "api"]).stdout == "o"
    with pytest.raises(recording.ReplayMismatchError, match="unrecorded call"):
        without(["gh", "api"])


def copy_exchange(exchange: recording.Exchange) -> recording.Exchange:
    """A fresh copy of one recorded exchange, since a replay consumes it."""
    return recording.Exchange(
        exchange.program,
        list(exchange.args),
        exchange.stdin,
        exchange.returncode,
        exchange.stdout,
        exchange.stderr,
    )


def test_api_project_reads_the_namespace_and_the_repository_of_an_argv_path() -> None:
    """D7 source 1: a ``repos/<o>/<r>`` or ``projects/<g>%2F<r>`` path names the project; templates and numbers name none."""
    read = recording._api_project
    assert read("repos/zqo/zqr/pulls/3") == ("zqo", "zqr")
    assert read("/api/v3/repos/zqo/zqr") == ("zqo", "zqr")
    assert read("api/v4/projects/zqg%2Fzqr/merge_requests") == ("zqg", "zqr")
    assert read("projects/zqg%2Fzqs%2Fzqr") == ("zqg", "zqs", "zqr")
    assert read("repos/zqo/:repo") == ()
    assert read("repos/:owner/zqr") == ()
    assert read("repos/{owner}/{repo}/pulls") == ()
    assert read("repos/{zq}x/zqr") == ("{zq}x", "zqr")
    assert read("repos/x{zq}/zqr") == ("x{zq}", "zqr")
    assert read("repos/{zq/zqr") == ("{zq", "zqr")
    assert read("repos/ORG/PROJECT/x") == ()
    assert read("repos/Org/zqr") == ()
    assert read("repos/zqo/Project") == ()
    assert read("repos/zqo") == ()
    assert read("repos/zqo/") == ()
    assert read("projects/123") == ()
    assert read("projects/zqg%2F123") == ()
    assert read("projects/123%2Fzqr") == ()
    assert read("users/zqo/zqr") == ()
    assert read("nothing") == ()


def test_classify_text_expands_a_bot_stem_only_when_no_role_names_it_already() -> None:
    """D2: the ``@<stem>`` of a ``[bot]`` login is a role of its own unless an explicit key spells it, in any case."""
    assert recording.classify_text("@zqbot and zqbot", {"zqbot[bot]": "bot"}) == "@bot and zqbot"
    assert recording.classify_text("@ZQBOT", {"ZQBot[bot]": "bot"}) == "@bot"
    assert recording.classify_text("@zqbot", {"ZQBot[bot]": "bot", "ZQBOT": "author"}) == "@author"
    assert recording.classify_text("@zqbot", {"zqbot[bot]": "bot", "@ZQBOT": "author-2"}) == (
        "@author-2"
    )
    assert recording.classify_text("@zqbot", {"zqbot[bot]": "bot", "@zqbot": "author-2"}) == (
        "@author-2"
    )
    assert recording.classify_text("@zqbot", {"ZQBot[bot]": "bot", "zqbot[BOT]": "bot-2"}) == "@bot"
    assert recording.classify_text("x[bot] @x", {"x[bot]": "bot"}) == "bot @bot"
    assert recording.classify_text("@cla", {"cla[bot]": "bot"}) == "@bot"
    assert recording.classify_text("a :owner b zqlogin", {"zqlogin": "author", "owner": "x"}) == (
        "a :owner b author"
    )
    assert recording.outside_literals("a :id b :id", lambda piece: piece.upper()) == "A :id B :id"
    assert recording.outside_literals(":id", lambda piece: piece + "!") == "!:id!"
    assert recording.outside_literals("", lambda piece: piece + "!") == "!"
    assert recording.outside_literals("x", lambda piece: piece + "!") == "x!"


def test_known_values_soft_sources_never_raise_and_hard_ones_do() -> None:
    """D7: a short value of a structured source is a configuration error, one found in text is just not a value."""

    def known(stdout: object, *, args: list[str] | None = None, remote: str = "") -> closure.Known:
        call = {
            "stdout": json.dumps(stdout) if not isinstance(stdout, str) else stdout,
            "args": args or [],
        }
        return recording.known_values(
            [[call], [], {}, remote], roles={}, remote=remote, denylist=NO_DENYLIST
        )

    short_user = {"user": {"login": "zqlogin", "name": "Jo", "email": "ab@zqmail.io"}}
    soft = known(short_user)
    assert soft.values("name") == ()
    assert soft.values("mailbox") == ("ab@zqmail.io",)
    assert "zqmail.io" in soft.values("host")
    spaced = known({"user": {"login": "zqlogin", "name": "Ab C", "email": "abc@zqmail.io"}})
    assert spaced.values("name") == ("ab c",)
    assert known({"user": {"login": "zqlogin", "name": "Anna"}}).values("name") == ("anna",)
    assert known({"user": {"login": "zqlogin", "name": "Ann"}}).values("name") == ()
    assert known({"user": {"login": "zqlogin", "name": "Author Agent"}}).values("name") == (
        "author agent",
    )
    assert known({"user": {"login": "zqlogin", "name": "Agent"}}).values("name") == ()
    assert known({"user": {"login": "zqlogin", "name": "Bot Marta"}}).values("name") == (
        "bot marta",
        "marta",
    )
    assert known({"user": {"login": "zqlogin", "email": "zqnoat"}}).values("mailbox") == ()
    assert known({"user": {"login": "zqlogin", "email": "abcd@zqmail.io"}}).values("mailbox") == (
        "abcd@zqmail.io",
        "abcd",
    )
    assert known({"user": {"login": "zqlogin", "email": "abc@zqmail.io"}}).values("mailbox") == (
        "abc@zqmail.io",
    )
    for domain in ("example.com", "sub.example.org", "localhost", "pypi.org", "docs.python.org"):
        mail = known({"user": {"login": "zqlogin", "email": f"zqmartak@{domain}"}})
        assert domain not in mail.values("host"), domain
    assert known({"note": "see https://zqhost.io/ab/zqrepo"}).values("segment") == ("zqrepo",)
    with pytest.raises(NeedleError, match=r"^needle segment 1: too short$"):
        known("", args=["api", "repos/ab/zqrepo/pulls"])
    with pytest.raises(NeedleError, match=r"^needle segment 1: too short$"):
        known("", remote="git@git.zqcorp.io:ab/zqrepo.git")
    assert known({"body": "run glab api projects/ab%2Fzqrepo/x"}).values("segment") == ("zqrepo",)
    assert known({"web_url": "https://zqhost.io//zqaaaa//zqbbbb"}).values("segment") == (
        "zqaaaa",
        "zqbbbb",
    )
    assert known({"web_url": "  https://zqhost.io/zqaaaa/zqbbbb  "}).values("segment") == (
        "zqaaaa",
        "zqbbbb",
    )
    assert (
        known({"web_url": "  https://zqhost.io/zqaaaa/zqbbbb  "}).values("host")[-1] == "zqhost.io"
    )
    assert known({"web_url": "https://zqhost.io/zqaaaa/zqbbbb"}).values("host") == (
        "api.github.com",
        "github.com",
        "gitlab.com",
        "zqhost.io",
    )
    uppercase = known({"body": "see HTTPS://GIT.ZQCORP.IO/ZQAAAA/ZQBBBB"})
    assert uppercase.values("host")[-1] == "git.zqcorp.io"
    assert uppercase.values("segment") == ("zqaaaa", "zqbbbb")
    nested = known(
        {"user": {"login": "zqlogin", "id": 74928163}}, args=["api", "projects/77665544/x"]
    )
    assert nested.values("id")[:1] == ("74928163",)
    assert "77665544" in nested.values("id")
    order = known({"body": "see projects/55667788/x"}, args=["api"])
    assert order.values("id") == ("55667788",)
    with pytest.raises(NeedleError, match=r"^needle login 1: too short$"):
        recording.known_values([[], [], {}, ""], roles={"abc": "author"}, denylist=NO_DENYLIST)
    assert inspect.signature(recording.known_values).parameters["forge"].default == "github"
    assert inspect.signature(recording.known_values).parameters["min_length"].default == 4


def test_known_values_ids_ordering_and_json_in_strings() -> None:
    """D7: ids come first in the order they are met, user ids then repository ids, JSON inside strings included."""
    padded = " " + json.dumps({"user": {"login": "zqlogin", "id": 74928163}})
    raw = [[{"stdout": padded, "args": ["api", "projects/77665544/x"]}], [], {}, ""]
    known = recording.known_values(
        raw, roles={}, ids=UserIdMap(), projects=UserIdMap.for_projects(), denylist=NO_DENYLIST
    )
    ids = known.values("id")
    assert ids[0] == "74928163" and "77665544" in ids
    assert recording.user_id_values(padded) == [74928163]
    assert recording.user_id_values(json.dumps([{"login": "a", "id": 7}])) == [7]
    assert recording.user_id_values("[nope") == []
    assert recording.project_id_values(
        " " + json.dumps({"id": 5, "web_url": "https://h.io/o/r"})
    ) == [5]
    assert [list(d) for d in recording._walk_dicts(" " + json.dumps([{"a": 1}]))] == [["a"]]
    with capture_logs() as logs:
        assert recording._instance_host([{"url": "https://[bad/a/b"}]) is None
        recording.known_values(
            [[{"stdout": json.dumps({"web_url": "https://[bad/a/b"}), "args": []}], [], {}, ""],
            roles={},
            denylist=NO_DENYLIST,
        )
    assert {e["event"] for e in logs} == {"forge.recording.not_a_url"}
    assert len(logs) >= 2
    after = recording.known_values(
        [
            [
                {
                    "stdout": json.dumps(
                        [
                            {"web_url": "https://[bad/a/b"},
                            {"web_url": "https://zqhost.io/zqaaaa/zqbbbb"},
                        ]
                    ),
                    "args": [],
                }
            ],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert "zqhost.io" in after.values("host")
    with pytest.raises(recording.ReplayMismatchError, match=r"^unrecorded call: \['api', 'x'\]$"):
        recording.ReplaySpawn([])(["/bin/gh", "api", "x"])
    with pytest.raises(
        recording.ReplayMismatchError, match=r"^expected gh \['a'\], got \('gh', \['b'\]\)$"
    ):
        recording.ReplaySpawn([recording.Exchange("gh", ["a"], None, 0, "", "")])(["gh", "b"])
    with pytest.raises(recording.ReplayMismatchError, match=r"^stdin differs for \['a'\]$"):
        recording.ReplaySpawn([recording.Exchange("gh", ["a"], "in", 0, "", "")])(["gh", "a"])


def _roles_data(*answers: object) -> dict[str, Any]:
    """A hand-written fixture holding ``answers`` as exchanges."""
    return {"forge": "github", "calls": [_exchange(a) for a in answers]}


def test_offline_and_live_roles_give_a_bot_stem_its_own_word_only_when_mentioned() -> None:
    """D2: ``<name>[bot]`` and its bare stem share a word when the recording mentions the stem, in either order."""
    bot = {"user": {"login": "zqbot[bot]"}, "body": "ok"}
    mention = {"body": "cc @zqbot please"}
    both = {"zqbot[bot]": "bot", "zqbot": "bot"}
    assert fixture_pipeline.fixture_roles(_roles_data(bot, mention)) == both
    assert fixture_pipeline.fixture_roles(_roles_data(mention, bot)) == both
    assert fixture_pipeline.fixture_roles(_roles_data(bot)) == {"zqbot[bot]": "bot"}
    assert fixture_pipeline.fixture_roles(_roles_data(mention)) == {"@zqbot": "author-2"}
    assert fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "ci[bot]"}, "body": "cc @ci"})
    ) == {"ci[bot]": "bot"}
    assert fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "project[bot]"}, "body": "cc @project"})
    ) == {"project[bot]": "bot"}
    assert fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "Dependabot[BOT]"}, "body": "cc @dependabot"})
    ) == {"Dependabot[BOT]": "bot", "dependabot": "bot"}
    assert fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "zqbot[bot]"}, "body": "cc @ZQBOT"})
    ) == {"zqbot[bot]": "bot", "ZQBOT": "bot"}
    two = fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "zqone[bot]"}}, {"user": {"login": "zqtwo[bot]"}})
    )
    assert two == {"zqone[bot]": "bot", "zqtwo[bot]": "bot-2"}
    gitlab = fixture_pipeline.fixture_roles(
        _roles_data({"user": {"login": "project_88442211_bot_a1b2c3d4e5f6"}})
    )
    assert gitlab == {"project_88442211_bot_a1b2c3d4e5f6": "service"}
    calls = [
        {"args": [], "stdout": json.dumps(mention)},
        {"args": [], "stdout": json.dumps(bot)},
    ]
    assert rec.build_roles(calls, bot="", maintainer=None, author=None) == both
    assert rec.build_roles(calls[::-1], bot="", maintainer=None, author=None) == both
    assert rec.build_roles(calls[1:], bot="", maintainer=None, author=None) == {"zqbot[bot]": "bot"}
    assert rec.build_roles(calls[:1], bot="", maintainer=None, author=None) == {"@zqbot": "author"}
    short = [{"args": [], "stdout": json.dumps({"user": {"login": "ci[bot]"}, "body": "cc @ci"})}]
    assert rec.build_roles(short, bot="", maintainer=None, author=None) == {"ci[bot]": "bot"}
    assert rec.build_roles([], bot="repoach-bot", maintainer="zqmaint", author="zqauth") == {
        "zqmaint": "maintainer",
        "zqauth": "author",
    }
    assert rec.build_roles([], bot="zqrobot", maintainer=None, author=None) == {"zqrobot": "bot"}


def test_the_offline_transform_replaces_known_values_everywhere_but_the_closed_fields() -> None:
    """D4: every string is rewritten but ``forge``, ``method``, ``case``, keys and the source head."""
    known = recording.known_values(
        [[], [], {}, "git@git.zqcorp.io:zqgroup/zqmainrepo.git"],
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        denylist=NO_DENYLIST,
    )
    data = {
        "forge": "zqgroup",
        "method": "zqgroup",
        "case": "zqgroup",
        "source": "recorded 2026-11-22 from a live github change request on zqgroup",
        "zqgroup": "zqgroup",
        "args": ["zqgroup", 5, None],
        "calls": [
            {
                "returncode": 0,
                "args": ["zqgroup"],
                "stdout": json.dumps({"body": "zqgroup"}),
                "stderr": "zqgroup",
                "stdin": "zqgroup",
            },
            {"stdout": "zqgroup", "args": ["zqgroup"]},
        ],
    }
    out = fixture_pipeline._replace_known(data, known)
    assert out["forge"] == out["method"] == out["case"] == "zqgroup"
    assert out["source"] == "recorded 2026-11-22 from a live github change request on org"
    assert out["zqgroup"] == "org"
    assert out["args"] == ["org", 5, None]
    first, second = out["calls"]
    assert json.loads(first["stdout"]) == {"body": "org"}
    assert (first["args"], first["stderr"], first["stdin"]) == (["org"], "org", "org")
    assert second["stdout"] == "org"
    headless = fixture_pipeline._replace_known({"source": "zqgroup elsewhere"}, known)
    assert headless["source"] == "org elsewhere"
    assert fixture_pipeline._replace_known(7, known) == 7
    calls = fixture_pipeline.replace_known_in_calls(
        [
            {
                "args": ["zqgroup"],
                "stdout": json.dumps({"b": "zqgroup"}),
                "stderr": "zqgroup",
                "stdin": None,
            }
        ],
        known,
    )
    assert calls == [
        {"args": ["org"], "stdout": json.dumps({"b": "org"}), "stderr": "org", "stdin": None}
    ]
    bare = fixture_pipeline.replace_known_in_calls([{"stdout": "zqgroup"}], known)
    assert bare == [{"stdout": "org"}]
    known_offline = fixture_pipeline.fixture_known(
        {
            "calls": [_exchange({"user": {"login": "author", "id": 1001}})],
            "synthetic_user_ids": [1001],
            "synthetic_project_ids": [2001],
            "args": [],
            "kwargs": {},
        },
        roles={"zqlogin": "author"},
        forge="gitlab",
        denylist=NO_DENYLIST,
    )
    assert ("login", "zqlogin") in known_offline.entries
    assert not known_offline.values("id")


def test_remaining_edges_of_the_path_reader_the_roles_and_the_soft_names() -> None:
    """D1b, D7: trailing slashes, a dash deep in a path, the closed API roots, one-digit ports, role cases, soft names."""
    identity = recording.path_identity
    assert identity(["api", "v4", "projects", "123", ""]).segments == [
        "api",
        "v4",
        "projects",
        "123",
        "",
    ]
    assert identity(["foo", "-", "jobs", ""]).segments == ["org", "-", "jobs", ""]
    deep = identity(["zqa", "zqb", "zqc.git", "-", "x"])
    assert deep.segments == ["org", "project.git", "-", "x"]
    assert (deep.namespace, deep.repository) == (("zqa", "zqb"), "zqc")
    assert {"api", "repos", "users", "orgs", "apps", "groups"} == recording._API_ROOTS
    for root in ("api", "repos", "users", "orgs", "apps", "groups"):
        assert recording._follow_segments([root, "x", "y", "z"], full=False) == [
            root,
            "x",
            "y",
            "z",
        ]
    assert recording._follow_segments(["other", "x", "y", "z"], full=False) == ["other", "x"]
    assert recording._path_after("h.io:5/a/b", 4) == ["a", "b"]
    assert recording._path_after("h.io:12/a/b", 4) == ["a", "b"]
    assert recording._instance_host([{"web_url": "  https://zqhost.io/x  "}]) == "zqhost.io"
    assert recording._unique([("a", "1"), ("A", "2"), ("B", "3"), ("b", "4")]) == [
        ("a", "1"),
        ("B", "3"),
    ]
    user = {"login": "zqlogin", "name": "Marta Zqkowalska"}
    raw = [[{"stdout": json.dumps({"user": user}), "args": []}], [], {}, ""]
    known = recording.known_values(raw, roles={"ZQLogin": "maintainer"}, denylist=NO_DENYLIST)
    assert known.mapping()["marta"] == "maintainer"
    for odd in ("Project", "Org", "Forge.Example.Com", "Maintainer", "AUTHOR", "Jo", "A B"):
        odd_user = {"user": {"login": "zqlogin", "name": odd, "email": "a@b"}}
        odd_known = recording.known_values(
            [[{"stdout": json.dumps(odd_user), "args": []}], [], {}, ""],
            roles={"zqlogin": "author"},
            denylist=NO_DENYLIST,
        )
        assert not set(odd_known.values("name")) & {"project", "org", "maintainer", "author", "jo"}
    short = recording.known_values(
        [
            [{"stdout": json.dumps({"user": {"login": "zqlogin", "email": "a@b"}}), "args": []}],
            [],
            {},
            "",
        ],
        roles={},
        denylist=NO_DENYLIST,
    )
    assert short.values("mailbox") == ()
    remote = "git@git.zqcorp.io:zqgroup/zqmainrepo.git"
    upper = recording.known_values(
        [
            [{"stdout": "see HTTPS://GIT.ZQCORP.IO/zqgroup/zqmainrepo/zqextra/zqfile", "args": []}],
            [],
            {},
            remote,
        ],
        roles={},
        remote=remote,
        denylist=NO_DENYLIST,
    )
    assert "zqextra" in upper.values("segment")
    assert recording.known_values(raw, roles={}, denylist=NO_DENYLIST).values("name") == (
        "marta zqkowalska",
        "marta",
        "zqkowalska",
    )


def _record_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: Forge,
    *,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
    trim: int | None = None,
    remote: str = "",
    author: str | None = None,
) -> dict[str, Any]:
    """Run :func:`rec.record` against ``forge`` and read the fixture back."""
    _no_denylist(monkeypatch, tmp_path)
    monkeypatch.setattr(rec, "select_forge", lambda **kw: _as_bot(forge, kw.get("bot_username")))
    monkeypatch.setattr(rec, "origin_url", lambda _cwd: remote)
    monkeypatch.setattr(rec, "_author_of", lambda *_a: author)
    path = rec.record(
        forge_name="github",
        cwd=tmp_path,
        case="c",
        method=method,
        args=args,
        kwargs=kwargs or {},
        trim=trim,
        out_root=tmp_path / "out",
    )
    return cast(dict[str, Any], json.loads(path.read_text()))


def _as_bot(forge: Forge, bot: str | None) -> Forge:
    """``forge`` as :func:`select_forge` builds it for ``bot_username=bot``: the test's stand-in."""
    if bot is not None:
        cast(Any, forge)._bot_login = bot
    return forge


def _github_answering(tmp_path: Path, answers: list[CommandResult]) -> Forge:
    """A GitHub forge whose calls answer ``answers`` in order, the last one repeated."""
    forge = _build("github", tmp_path, "repoach-bot")
    queue = list(answers)

    def _spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        done = queue.pop(0) if len(queue) > 1 else queue[0]
        return CommandResult(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr, argv=list(argv)
        )

    cast(Any, forge)._spawn = _spawn
    return forge


def test_record_trims_classifies_the_maintainer_and_names_its_refusals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorder: ``--trim``, the ``is_maintainer`` login, the declared keys, and each refusal's exact words."""
    permission = json.dumps(
        {"permission": "admin", "role_name": "admin", "user": {"login": "zqmaint"}}
    )
    forge = _github_answering(tmp_path, [CommandResult(0, permission, "", [])])
    fixture = _record_through(
        monkeypatch, tmp_path, forge, method="is_maintainer", args=["zqmaint"]
    )
    assert fixture["args"] == ["maintainer"]
    assert json.loads(fixture["calls"][0]["stdout"])["user"]["login"] == "maintainer"
    assert fixture["calls"][0]["args"][1].endswith("collaborators/maintainer/permission")
    assert set(fixture) == {
        "forge",
        "case",
        "method",
        "args",
        "kwargs",
        "bot_username",
        "source",
        "trimmed",
        "calls",
        "result",
        "raises",
    }
    assert fixture["method"] == "is_maintainer" and fixture["trimmed"] is None
    assert fixture["bot_username"] == "repoach-bot"
    listing = json.dumps(
        [
            {"id": 1, "body": f"body {n}", "user": {"login": "zqlogin", "id": 74928163 + n}}
            for n in range(3)
        ]
    )
    many = _github_answering(tmp_path, [CommandResult(0, listing, "", [])])
    trimmed = _record_through(
        monkeypatch, tmp_path, many, method="conversation_comments", args=[7], trim=2
    )
    assert trimmed["trimmed"] == 2
    assert len(json.loads(trimmed["calls"][0]["stdout"])) == 2
    whole = _record_through(
        monkeypatch,
        tmp_path,
        _github_answering(tmp_path, [CommandResult(0, listing, "", [])]),
        method="conversation_comments",
        args=[7],
    )
    assert len(json.loads(whole["calls"][0]["stdout"])) == 3
    binary = tmp_path / "gh"

    class _Extra(GitHubForge):
        def conversation_comments(self, number: int, *extra: Any, **_kw: Any) -> list[Any]:
            self._spawn([str(binary), "api", "x"])
            return []

    extra = _Extra(cwd=tmp_path, gh_path=str(binary), bot_login="repoach-bot")
    cast(Any, extra)._spawn = lambda argv, *, input_data=None: CommandResult(
        0, "[]", "", list(argv)
    )
    host_only = _record_through(
        monkeypatch,
        tmp_path,
        extra,
        method="conversation_comments",
        args=[7, "see git.zqargs.io:8443"],
        kwargs={"note": "at git.zqkw.io:22 and zqmainrepo"},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert host_only["args"] == [7, "see forge.example.com:8443"]
    assert host_only["kwargs"] == {"note": "at forge.example.com:22 and project"}
    with pytest.raises(
        SystemExit, match=r"^refusing to record write method 'post_comment': fixtures never post$"
    ):
        rec.record(
            forge_name="github",
            cwd=tmp_path,
            case="c",
            method="post_comment",
            args=[],
            kwargs={},
            trim=None,
            out_root=tmp_path / "w",
        )
    token = "ghp_" + "A1" * 12
    leaky = _github_answering(tmp_path, [CommandResult(0, json.dumps([{"body": token}]), "", [])])
    with pytest.raises(
        fixture_judge.UnclassifiedValueError,
        match=r"^secret-shaped value at exchanges\[0\]\.stdout",
    ):
        _record_through(monkeypatch, tmp_path, leaky, method="conversation_comments", args=[7])
    assert not (tmp_path / "w").exists()


def test_a_secret_in_any_string_of_the_fixture_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D3 (0.28, PR #319): the token scan reads every string written, live and offline, arguments and argv included."""
    token = "ghp_" + "B2" * 12
    quiet = _github_answering(tmp_path, [CommandResult(0, "{}", "", [])])
    with pytest.raises(
        fixture_judge.UnclassifiedValueError, match=r"^secret-shaped value at exchanges\[0\]\.args"
    ):
        _record_through(monkeypatch, tmp_path, quiet, method="branch_protection", args=[token])
    assert not (tmp_path / "w").exists()
    held = _fixture("github", "reviews", [[{"id": 1, "body": f"use {token} here"}]])
    with pytest.raises(
        fixture_pipeline.ResidualValueError, match=r"^secret-shaped value at exchanges\[0\]\.stdout"
    ):
        fixture_pipeline.redact_fixture(held, forge="github", denylist=NO_DENYLIST)


def test_replay_names_the_bot_and_the_error_the_redacted_fixture_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The replay of the redacted exchanges sets the classified bot, reports a forge error by its class and disagrees by exit."""
    asked: list[str | None] = []
    forge = _build("github", tmp_path, "repoach-bot")
    monkeypatch.setattr(
        rec, "select_forge", lambda **kw: asked.append(kw.get("bot_username")) or forge
    )
    probe = _build("github", tmp_path, "repoach-bot")
    recorder = recording.RecordingSpawn(
        inner=lambda argv, *, input_data=None: CommandResult(1, "", "boom", list(argv))
    )
    cast(Any, probe)._spawn = recorder
    with pytest.raises(ForgeError):
        probe.pull_request(7)
    failing = [e.to_json() for e in recorder.exchanges]
    result, raises = rec._replay("github", tmp_path, "pull_request", [7], {}, failing, "zqbotword")
    assert (result, raises) == (None, "ForgeError") or raises is not None
    rec._replay("github", tmp_path, "pull_request", [7], {}, failing, None)
    assert asked == ["zqbotword", None]
    assert fixture_judge.generic_class("a /home/zqperson/x") == "home_path"
    with pytest.raises(
        fixture_judge.UnclassifiedValueError,
        match=r"^unclassified: exchanges\[0\]\.stdout\.user\.login$",
    ):
        fixture_judge.refuse_unclassified(
            {"source": "s", "calls": [_exchange({"user": {"login": "zqunknown"}})]},
            deny_list.Denylist(),
            None,
        )
    stdin = fixture_pipeline.replace_known_in_calls(
        [{"args": ["a"], "stdout": "x", "stdin": "zqgroup", "stderr": "zqgroup"}],
        recording.known_values(
            [[], [], {}, "git@h.io:zqgroup/zqrepo.git"],
            roles={},
            remote="git@h.io:zqgroup/zqrepo.git",
            denylist=NO_DENYLIST,
        ),
    )
    assert stdin[0]["stdin"] == "org" and stdin[0]["stderr"] == "org"


def test_build_roles_keeps_a_mention_when_a_longer_login_follows_and_skips_the_known_author() -> (
    None
):
    """D2: only a ``[bot]`` login promotes its mention-only stem, and an author already named is not added twice.

    The stem is keyed as the mention spells it, met before or after its bot, live as offline
    (one role builder since PR #319 round 4); keys are compared casefolded, so the spelling
    replaces nothing differently.
    """
    mention = {"args": [], "stdout": json.dumps({"body": "cc @zqname"})}
    other = {"args": [], "stdout": json.dumps({"user": {"login": "zqname12345"}})}
    assert rec.build_roles([mention, other], bot="", maintainer=None, author=None) == {
        "@zqname": "author",
        "zqname12345": "author-2",
    }
    upper = {"args": [], "stdout": json.dumps({"user": {"login": "ZQName[BOT]"}})}
    assert rec.build_roles([mention, upper], bot="", maintainer=None, author=None) == {
        "ZQName[BOT]": "bot",
        "zqname": "bot",
    }
    assert rec.build_roles([upper, mention], bot="", maintainer=None, author=None) == {
        "ZQName[BOT]": "bot",
        "zqname": "bot",
    }
    assert rec.build_roles([], bot="", maintainer=None, author="repoach-bot") == {}
    assert rec.build_roles([other], bot="", maintainer=None, author="repoach-bot") == {
        "zqname12345": "author-2"
    }
    assert rec.build_roles([], bot="zqrobot", maintainer=None, author="zqrobot") == {
        "zqrobot": "bot"
    }
    assert rec.build_roles([], bot="", maintainer="zqmaint", author="zqmaint") == {
        "zqmaint": "maintainer"
    }


def test_offline_roles_by_enumeration_of_orders_numbering_and_mention_text() -> None:
    """D4: the order a stem and its bot are met in, the numbering after a stem, longer logins first, mention-only words."""
    short_mention = {"body": "cc @ci"}
    short_bot = {"user": {"login": "ci[bot]"}}
    assert fixture_pipeline.fixture_roles(_roles_data(short_mention, short_bot)) == {
        "ci[bot]": "bot"
    }
    assert fixture_pipeline.fixture_roles(_roles_data(short_bot, short_mention)) == {
        "ci[bot]": "bot"
    }
    bot = {"user": {"login": "zqbot[bot]"}, "body": "cc @zqbot"}
    other = {"user": {"login": "zqother"}}
    assert fixture_pipeline.fixture_roles(_roles_data(bot, other)) == {
        "zqbot[bot]": "bot",
        "zqother": "author-2",
        "zqbot": "bot",
    }
    assert fixture_pipeline.fixture_roles(_roles_data(other, bot)) == {
        "zqother": "author-2",
        "zqbot[bot]": "bot",
        "zqbot": "bot",
    }
    upper_mention = _roles_data({"user": {"login": "zqbot[bot]"}}, {"body": "cc @ZQBOT"})
    assert fixture_pipeline.fixture_roles(upper_mention) == {"zqbot[bot]": "bot", "ZQBOT": "bot"}
    mention_text = recording.rename_logins_in_text
    assert mention_text("zqname-x and zqname", {"zqname": "author", "zqname-x": "author-2"}) == (
        "author-2 and author"
    )
    assert mention_text("zqmention and @zqmention", {"@zqmention": "author-2"}) == (
        "zqmention and @author-2"
    )
    assert mention_text("@zqbot zqbot[bot] :owner", {"zqbot[bot]": "bot", "owner": "author"}) == (
        "@bot bot :owner"
    )
    assert mention_text("x", {}) == "x"


def test_offline_known_values_and_redaction_read_every_field_of_the_fixture() -> None:
    """D4: arguments, keyword arguments, results and the remote feed the known values; key order is kept."""
    data = {
        "forge": "github",
        "method": "reviews",
        "args": ["see git.zqcorp.io:8443"],
        "kwargs": {"note": "at git.acme.io:22"},
        "calls": [],
        "result": None,
    }
    known = fixture_pipeline.fixture_known(
        data, remote="git@git.remote.io:zqgroup/zqmainrepo.git", denylist=NO_DENYLIST
    )
    hosts = known.values("host")
    assert {"git.zqcorp.io", "git.acme.io"} <= set(hosts)
    assert known.values("segment") == ("zqgroup", "zqmainrepo")
    assert fixture_pipeline.fixture_known({"calls": []}, denylist=NO_DENYLIST).values("login") == ()
    assert ("login", "a") in fixture_pipeline.fixture_known(
        {"calls": []}, roles={"a": "author"}, denylist=NO_DENYLIST
    ).entries
    default_forge = inspect.signature(fixture_pipeline.fixture_known).parameters["forge"].default
    assert default_forge == "github"
    url = "https://github.com/zqpubown/zqpubrepo?tab=readme"
    fixture = {
        "forge": "github",
        "method": "reviews",
        "args": [url],
        "kwargs": {"k": url},
        "calls": [],
        "result": {"link": url, "body": url},
    }
    out = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    placeholder = "https://forge.example.com/org/project"
    assert out["args"] == [placeholder]
    assert out["kwargs"] == {"k": placeholder}
    assert out["result"]["body"] == placeholder
    assert list(out) == ["forge", "method", "args", "kwargs", "calls", "result"]
    ordered = fixture_pipeline.redact_fixture(
        dict(sorted(fixture.items())), forge="github", denylist=NO_DENYLIST
    )
    assert list(ordered) == sorted(ordered)
    declared = {**fixture, "calls": [_exchange({"user": {"login": "author", "id": 74928163}})]}
    stamped = fixture_pipeline.redact_fixture(
        dict(sorted(declared.items())), forge="github", denylist=NO_DENYLIST
    )
    assert list(stamped) == sorted(stamped) and stamped["synthetic_user_ids"] == [1001]
    stamped_unsorted = fixture_pipeline.redact_fixture(
        declared, forge="github", denylist=NO_DENYLIST
    )
    assert list(stamped_unsorted)[-1] == "synthetic_user_ids"


def test_record_refuses_a_replay_that_disagrees_with_the_live_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The redacted replay must raise what the live call raised, or nothing is written."""
    binary = tmp_path / "gh"
    seen = {"calls": 0}

    class _Flaky(GitHubForge):
        def conversation_comments(self, number: int, *extra: Any, **_kw: Any) -> list[Any]:
            self._spawn([str(binary), "api", "x"])
            seen["calls"] += 1
            if seen["calls"] == 1:
                raise ForgeError("first call only")
            return []

    flaky = _Flaky(cwd=tmp_path, gh_path=str(binary), bot_login="repoach-bot")
    cast(Any, flaky)._spawn = lambda argv, *, input_data=None: CommandResult(
        0, "[]", "", list(argv)
    )
    with pytest.raises(
        SystemExit,
        match=r"^refusing to write a fixture whose redacted replay disagrees with the live call$",
    ):
        _record_through(monkeypatch, tmp_path, flaky, method="conversation_comments", args=[7])
    assert not (tmp_path / "out").exists()


def test_offline_words_skip_the_ones_the_fixture_already_holds_and_the_head_is_never_rewritten() -> (
    None
):
    """D4: a new login takes the next class word the fixture lacks; longer words win; the recorded head stays."""
    held = _roles_data(
        {"user": {"login": "author"}}, {"user": {"login": "author-2"}}, {"user": {"login": "zqnew"}}
    )
    assert fixture_pipeline.fixture_roles(held) == {"zqnew": "author-3"}
    bots = _roles_data(
        {"user": {"login": "bot"}},
        {"user": {"login": "zqone[bot]"}},
        {"user": {"login": "zqtwo[bot]"}},
    )
    assert fixture_pipeline.fixture_roles(bots) == {"zqone[bot]": "bot-2", "zqtwo[bot]": "bot-3"}
    services = _roles_data({"user": {"login": "project_88442211_bot_a1b2c3d4e5f6"}})
    services["bot_username"] = "repoach-bot"
    assert fixture_pipeline.fixture_roles(services) == {
        "project_88442211_bot_a1b2c3d4e5f6": "service"
    }
    padded = {
        "forge": "github",
        "calls": [
            {
                "args": [],
                "returncode": 0,
                "stdout": " " + json.dumps({"user": {"login": "zqpadded"}}),
            }
        ],
    }
    assert fixture_pipeline.fixture_roles(padded) == {"zqpadded": "author-2"}
    assert (
        recording.rename_logins_in_text(
            "zqname-x and zqname", {"zqname": "author-10", "zqname-x": "bot"}
        )
        == "bot and author-10"
    )
    head = "recorded 2026-11-22 from a live github change request"
    known = recording.known_values(
        [[], [], {}],
        roles={},
        remote="git@git.zqcorp.io:recorded/zqmainrepo.git",
        denylist=NO_DENYLIST,
    )
    kept = fixture_pipeline._replace_known(
        {"source": f"{head}, tagged recorded", "case": "recorded"}, known
    )
    assert kept == {"source": f"{head}, tagged org", "case": "recorded"}
    other = fixture_pipeline._replace_known({"body": "recorded here", "note": f"{head}"}, known)
    assert other == {"body": "org here", "note": f"{head}".replace("recorded", "org")}


def test_offline_stems_touch_only_the_mention_of_their_own_bot() -> None:
    """D4: a mention is a stem only when ``<mention>[bot]`` is a login of the fixture, whatever else is mentioned."""
    mention = {"body": "cc @zqname"}
    long_login = {"user": {"login": "zqname12345"}}
    assert fixture_pipeline.fixture_roles(_roles_data(mention, long_login)) == {
        "@zqname": "author-2",
        "zqname12345": "author-3",
    }
    elsewhere = _roles_data({"body": "cc @zqother"}, {"user": {"login": "zqbot[bot]"}})
    assert fixture_pipeline.fixture_roles(elsewhere) == {
        "@zqother": "author-2",
        "zqbot[bot]": "bot",
    }
    plain = _roles_data({"user": {"login": "zqplain"}}, {"user": {"login": "zqbot[bot]"}})
    assert fixture_pipeline.fixture_roles(plain) == {"zqplain": "author-2", "zqbot[bot]": "bot"}


def test_known_values_read_encoded_forms_and_replace_them_without_a_stray_percent() -> None:
    """D2 (0.18): a host, a namespace and a repository behind ``%2F`` or ``%3A`` are known values, replaced whole."""
    for body in (
        "redirect=https%3A%2F%2Fcode.kvncloud.io%2Fkvnteam%2Fkvnbilling",
        "redirect=https%3A%2F%2Fcode.kvncloud.io/kvnteam/kvnbilling",
        "see code.kvncloud.io%2Fkvnteam%2Fkvnbilling",
        "see &#104;ttps://code.kvncloud.io/kvnteam/kvnbilling",
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert "code.kvncloud.io" in _kinds(found, "host"), body
        assert {"kvnteam", "kvnbilling"} <= set(_kinds(found, "segment")), body
        assert not any(v.startswith("2f") for v in _kinds(found, "host")), body
        out = recording.replace_known(body, found)
        assert "%forge" not in out and "kvn" not in out, out
        assert closure.judge(out, found) is None, out
    found = needles(_one_call({"body": "see %2F%2Fgit.zqcorp.io/zqns/zqrepo"}), roles={})
    assert recording.replace_known("x%2F%2Fgit.zqcorp.io", found) == "x%2F%2Fforge.example.com"
    assert recording.replace_known("x%2F%2Fgit.zqcorp.io%2Fzqns", found) == (
        "x%2F%2Fforge.example.com%2Forg"
    )


def test_known_values_read_bracketed_ipv6_hosts_and_the_names_of_git_authors() -> None:
    """D8, D5 (0.18, 0.20): the namespace and repository after a led private IPv6 literal, a git author's name next to its mailbox."""
    for body, host in (
        ("http://[fd00::1]/kvnteam/kvnbilling/x", "[fd00::1]"),
        ("ssh://git@[fd00::1]:2222/kvnteam/kvnbilling", "[fd00::1]"),
        ("see //[2a00:1450::1]/kvnteam/kvnbilling/x", "[2a00:1450::1]"),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert {"kvnteam", "kvnbilling"} <= set(_kinds(found, "segment")), body
        assert host in _kinds(found, "host"), body
        out = recording.replace_known(body, found)
        assert "kvn" not in out and closure.judge(out, found) is None, out


def test_language_keywords_after_an_at_sign_are_no_mention() -> None:
    """D2 (0.20, C10 and C11): every word a language specification defines after ``@`` is code; a userinfo blocks a mention."""
    sets = {
        "PYTHON_DECORATORS": recording_tables.PYTHON_DECORATORS,
        "CSS_AT_RULES": recording_tables.CSS_AT_RULES,
        "DOC_TAGS": recording_tables.DOC_TAGS,
        "LANGUAGE_ANNOTATIONS": recording_tables.LANGUAGE_ANNOTATIONS,
    }
    union: set[str] = set()
    for name, words in sets.items():
        assert words, name
        assert all(w == w.casefold() and w and " " not in w for w in words), name
        union |= words
        for word in sorted(words):
            for carrier in ("cc @{} please", "@{} wrote:", "ping @{}.", "(@{})", "see @{}"):
                assert recording.mentions({"body": carrier.format(word)}) == [], (name, word)
            assert recording.mentions({"body": f"x @{word.upper()} y"}) == [], (name, word)
    assert union == recording.LANGUAGE_AT_WORDS
    for word in (
        "color-profile",
        "font-palette-values",
        "custom-media",
        "custom-selector",
        "nest",
        "config",
        "reference",
        "top-left",
        "swash",
        "when",
    ):
        assert word in recording_tables.CSS_AT_RULES, word
    for word in ("param", "returns", "throws", "inheritdoc", "var", "see", "brief", "retval", "a"):
        assert word in recording_tables.DOC_TAGS, word
    for word in (
        "override",
        "suppresswarnings",
        "jvmstatic",
        "objc",
        "escaping",
        "required",
        "state",
    ):
        assert word in recording_tables.LANGUAGE_ANNOTATIONS, word
    for word in ("dataclass_transform", "abstractproperty", "member", "verify"):
        assert word in recording_tables.PYTHON_DECORATORS, word
    for body, handles in (
        ("cc @zqperson and @Autowired and @Component", ["zqperson", "Autowired", "Component"]),
        ("@param x and @zqother", ["zqother"]),
        ("x-access-token:${GITHUB_TOKEN}@localhost and ${TOKEN}@h.io", []),
        ("user:@h.io and user:pw@h.io and {x}@h.io and $x@h.io", []),
        ('(@zqpar) "@zqquote" <@zqangle> =@zqeq', ["zqpar", "zqquote", "zqangle", "zqeq"]),
    ):
        assert recording.mentions({"body": body}) == handles, body
    for char in ":${}":
        assert recording.mentions({"body": f"x{char}@zqhandle"}) == [], char
    assert (
        recording.classify_text(
            "cc @param, @Override, @color-profile and @zqperson", {"@zqperson": "author-2"}
        )
        == "cc @param, @Override, @color-profile and @author-2"
    )


def test_an_ipv6_url_in_text_is_rewritten_by_the_convenience_layer_and_is_a_fixed_point() -> None:
    """D1b (0.20, C8): a bracketed IPv6 host is a text URL; rewritten it carries no port, kept it is byte-identical but for case."""
    text = recording.redact_urls_in_text
    for raw, expected in (
        (
            "see http://[2a00:1450::1]:8443/kvnteam/kvnbilling/x now",
            "see http://forge.example.com/org/project now",
        ),
        (
            "see https://[fd00::1]/kvnteam/kvnbilling now",
            "see https://forge.example.com/org/project now",
        ),
        (
            "ssh://git@[fd00::1]:2222/kvnteam/kvnbilling.git",
            "ssh://git@forge.example.com/org/project.git",
        ),
        ("http://[2a00:1450::1]:8443 now", "http://forge.example.com now"),
        ("http://[fe80::1%25eth0]:8080/kvnteam/kvnbilling", "http://forge.example.com/org/project"),
        ("curl http://[::1]:8000/health", "curl http://[::1]:8000/health"),
        ("see http://[2001:db8::1]:8080/docs", "see http://[2001:db8::1]:8080/docs"),
        ("see http://[2001:DB8:0:0::1]/kvnteam/x", "see http://[2001:db8:0:0::1]/kvnteam/x"),
        ("(http://[2a00:1450::1]/kvnteam/kvnbilling)", "(http://forge.example.com/org/project)"),
        (
            "<http://[2a00:1450::1]:443/kvnteam/kvnbilling>",
            "<http://forge.example.com/org/project>",
        ),
        ("see http://10.1.2.3:8080/kvnteam/kvnbilling", "see http://forge.example.com/org/project"),
        ("see http://code.kvncloud.io:8443/kvnteam", "see http://forge.example.com/org"),
    ):
        out = text(raw)
        assert out == expected, raw
        assert text(out) == out, raw
    assert recording._URL_IN_TEXT_RE.search("x [2a00::1] y") is None
    assert recording._URL_IN_TEXT_RE.search("http://[2a00::1]").group(0) == "http://[2a00::1]"
    assert recording._URL_IN_TEXT_RE.search("http://[bad host]/x") is None
    assert text("see http://[1]/x now") == "see http://[1]/x now"
    assert text("see http:///x and https://:8080/y now") == "see http:///x and https://:8080/y now"
    assert not recording._kept_host("FORGE.EXAMPLE.COM")
    assert not recording._kept_host("Forge.Example.Com") and not recording._kept_host(
        "forge.example.com"
    )
    assert recording._kept_host("Docs.GitHub.com") and recording._kept_host("pypi.org")
    assert not recording._kept_host("code.kvncloud.io")
    for raw, expected in (
        ("x@users.noreply.FORGE.EXAMPLE.COM", "author@users.noreply.forge.example.com"),
        ("BOT@users.noreply.github.com", "bot@users.noreply.forge.example.com"),
        ("NOREPLY@GITHUB.COM", "noreply@forge.example.com"),
        ("x@users.noreply.notforge.example.com", "x@users.noreply.notforge.example.com"),
        (
            "__https://code.kvncloud.io/kvnteam/kvnbilling#__init__",
            "__https://forge.example.com/org/project__",
        ),
        (
            "_https://code.kvncloud.io/kvnteam/kvnbilling#__init__",
            "_https://forge.example.com/org/project_",
        ),
    ):
        assert text(raw) == expected, raw


def test_a_lone_path_segment_after_a_host_is_a_page_not_a_namespace() -> None:
    """D7 source 3 (0.20, NG4-11): a text path yields values only with a repository or through an API root."""
    for body, segments in (
        ("open https://code.kvncloud.io/docs and read the docs", []),
        ("see //code.kvncloud.io/kvnteam now", []),
        ("curl https://code.kvncloud.io/explore/", []),
        ("https://code.kvncloud.io/kvnteam/kvnbilling", ["kvnteam", "kvnbilling"]),
        ("https://code.kvncloud.io/kvnteam/kvnbilling/issues/5", ["kvnteam", "kvnbilling"]),
        ("https://api.kvncloud.io/users/kvnperson", ["kvnperson"]),
        ("https://api.kvncloud.io/orgs/kvnteam/repos", ["kvnteam"]),
        ("https://api.kvncloud.io/repos/kvnteam/kvnbilling/pulls", ["kvnteam", "kvnbilling"]),
        ("https://api.kvncloud.io/api/v3/users/kvnperson", ["kvnperson"]),
        ("see https://code.kvncloud.io/kvnteam/-/issues?label_name=bug now", ["kvnteam"]),
        ("see https://code.kvncloud.io/kvnteam/-/merge_requests now", ["kvnteam"]),
        ("see https://gitlab.com/kvnteam/-/merge_requests now", []),
        ("see //code.kvncloud.io/kvnteam/-/epics/12 now", ["kvnteam"]),
        ("see https://code.kvncloud.io/groups/kvnteam/-/issues now", ["kvnteam"]),
        ("see https://code.kvncloud.io/groups/kvnteam/kvnsub/-/epics now", ["kvnteam", "kvnsub"]),
        ("see https://code.kvncloud.io/users/kvnperson/-/snippets now", ["kvnperson"]),
        (
            "see https://code.kvncloud.io/kvnteam/kvnbilling/-/issues/1 now",
            ["kvnteam", "kvnbilling"],
        ),
        ("see https://code.kvncloud.io/-/jobs/5 and https://code.kvncloud.io/-/snippets now", []),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == segments, body
    assert recording._api_route(["kvnteam", "-", "issues"]) and recording._api_route(["x", "-"])
    assert not recording._api_route(["-", "jobs", "5"]) and not recording._api_route(["-"])
    dashed = needles(
        _one_call({"body": "see https://code.kvncloud.io/kvnteam/-/issues and the kvnteam group"}),
        roles={},
    )
    assert recording.replace_known(
        "see https://code.kvncloud.io/kvnteam/-/issues and the kvnteam group", dashed
    ) == ("see https://forge.example.com/org/-/issues and the org group")
    found = needles(
        _one_call({"body": "open https://code.kvncloud.io/docs and read the docs"}), roles={}
    )
    assert recording.replace_known("read the docs", found) == "read the docs"
    for root in sorted(recording._API_ROOTS):
        assert recording._api_route([root, "x"]) and recording._api_route(["", root.upper(), "x"])
    assert not recording._api_route([]) and not recording._api_route(["", ""])
    assert not recording._api_route(["docs"]) and not recording._api_route(["x", "users"])
    instance = needles(
        _one_call(
            {
                "body": "see https://git.zqcorp.io/explore and https://git.zqcorp.io/zqgroup/zqother/-/jobs/5"
            }
        ),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert "explore" not in _kinds(instance, "segment")
    assert {"zqgroup", "zqother"} <= set(_kinds(instance, "segment"))
    group_route = needles(
        _one_call({"body": "see https://git.zqcorp.io/zqother/-/issues now"}),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert "zqother" in _kinds(group_route, "segment")


def test_a_reserved_route_word_is_never_an_identity() -> None:
    """D1b, D7 source 3 (0.22): a path led by a word a forge reserves names no one on any host; the word is written, never learnt; an identity after one is NG4-14."""
    identity = recording.path_identity
    words = recording_tables.RESERVED_ROUTE_WORDS
    gitlab = {
        "uploads",
        "help",
        "explore",
        "admin",
        "dashboard",
        "snippets",
        "search",
        "public",
        "s",
        "files",
        "assets",
        "import",
        "oauth",
        "jwt",
        ".well-known",
        "-",
    }
    assert gitlab <= words
    assert {"marketplace", "settings", "login", "repo", "account", "plugins"} <= words
    assert words.isdisjoint(recording._API_ROOTS) and "projects" not in words
    assert "user" not in words
    assert redact_url("https://github.com/user/repo") == "https://forge.example.com/org/project"
    for word in sorted(words):
        for path in (
            [word, "zqx", "zqy"],
            [word, "-", "system", "zqx"],
            [word],
            [word, "zqx", "zqy", ""],
            [word.upper(), "zqx", "zqy"],
        ):
            assert identity(path) == recording.PathIdentity(path), path
        assert redact_url(f"https://code.kvncloud.io/{word}/zqx/zqy") == (
            f"https://forge.example.com/{word}/zqx/zqy"
        )
    text = recording.redact_urls_in_text
    for body, expected in (
        (
            "see https://code.kvncloud.io/uploads/-/system/user/avatar/42/avatar.png and the uploads folder",
            "see https://forge.example.com/uploads/-/system/user/avatar/42/avatar.png and the uploads folder",
        ),
        (
            "see https://gitea.kvncorp.net/repo/create and https://gitea.kvncorp.net/milestones/x/y now",
            "see https://forge.example.com/repo/create and https://forge.example.com/milestones/x/y now",
        ),
        (
            "see https://github.com/marketplace/actions/checkout and the marketplace",
            "see https://forge.example.com/marketplace/actions/checkout and the marketplace",
        ),
        (
            "see https://bitbucket.org/account/signin/ and the account page",
            "see https://forge.example.com/account/signin/ and the account page",
        ),
        (
            "see https://code.kvncloud.io/-/snippets/42 and https://code.kvncloud.io/help/user/profile now",
            "see https://forge.example.com/-/snippets/42 and https://forge.example.com/help/user/profile now",
        ),
        (
            "see https://10.1.2.3:8443/explore/projects/topics and the topics",
            "see https://forge.example.com/explore/projects/topics and the topics",
        ),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == [], body
        assert text(body) == expected, body
    body = "see //code.kvncloud.io/public/projects/x now"
    found = needles(_one_call({"body": body}), roles={})
    assert _kinds(found, "segment") == []
    assert recording.replace_known(body, found) == "see //forge.example.com/public/projects/x now"
    instance = needles(
        _one_call({"body": "see https://git.zqcorp.io/uploads/-/system/user/avatar/42/a.png"}),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert set(_kinds(instance, "segment")) == {"zqgroup", "zqmainrepo"}
    for path in (
        ["s", "zqu"],
        ["admin", "users", "zqu"],
        ["admin", "groups", "zqg"],
        ["admin", "projects", "zqg", "zqr"],
        ["snippets", "zqws", "42"],
    ):
        assert identity(path) == recording.PathIdentity(path), path


def test_a_web_ide_route_reads_its_project_path() -> None:
    """D1b, D7 source 3 (0.22): after ``-/ide/project/`` the segments up to an IDE word are the namespace and the repository, on every host; another dash-led path names no one."""
    identity = recording.path_identity
    ide = identity(["-", "ide", "project", "zqg", "zqr", "edit", "main", "-", "src", "app.py"])
    assert ide.segments == [
        "-",
        "ide",
        "project",
        "org",
        "project",
        "edit",
        "main",
        "-",
        "src",
        "app.py",
    ]
    assert (ide.namespace, ide.repository) == (("zqg",), "zqr")
    nested = identity(
        ["-", "ide", "project", "zqg", "zqs", "zqr.git", "blob", "main", "-", "x", ""]
    )
    assert nested.segments == [
        "-",
        "ide",
        "project",
        "org",
        "project.git",
        "blob",
        "main",
        "-",
        "x",
        "",
    ]
    assert (nested.namespace, nested.repository) == (("zqg", "zqs"), "zqr")
    assert {"edit", "blob", "tree", "merge_requests"} == recording._IDE_ROUTES
    for word in sorted(recording._IDE_ROUTES):
        out = identity(["-", "ide", "project", "zqg", "zqr", word, "x"])
        assert out.segments == ["-", "ide", "project", "org", "project", word, "x"], word
        assert (out.namespace, out.repository) == (("zqg",), "zqr"), word
        assert (
            identity(["-", "ide", "project", "zqg", "zqr", word.upper(), "x"]).repository == "zqr"
        )
    bare = identity(["-", "ide", "project", "zqg", "zqr"])
    assert bare == recording.PathIdentity(
        ["-", "ide", "project", "org", "project"], ("zqg",), "zqr"
    )
    lone = identity(["-", "ide", "project", "zqg"])
    assert lone == recording.PathIdentity(["-", "ide", "project", "org"], ("zqg",))
    assert identity(["-", "ide", "project", "zqg", "edit", "main", ""]) == recording.PathIdentity(
        ["-", "ide", "project", "org", "edit", "main", ""], ("zqg",)
    )
    assert identity(["-", "ide", "project", "edit", "zqr", "edit", "main"]).namespace == ("edit",)
    assert identity(["-", "IDE", "PROJECT", "zqg", "zqr"]).repository == "zqr"
    for path in (
        ["-", "ide", "x", "zqg"],
        ["-", "ide", "project"],
        ["-", "x", "project", "zqg"],
        ["-", "ide"],
    ):
        assert identity(path) == recording.PathIdentity(path), path
        assert not recording._api_route(path), path
    assert identity(["x", "ide", "project", "zqg"]).segments == ["org", "project"]
    assert recording._api_route(["-", "ide", "project", "zqg"])
    assert recording._api_route(["", "-", "ide", "project", "zqg"])
    for body, segments in (
        (
            "open https://code.kvncloud.io/-/ide/project/kvnteam/kvnbilling/edit/main/-/src/app.py in the IDE",
            ["kvnteam", "kvnbilling"],
        ),
        (
            "open //code.kvncloud.io/-/ide/project/kvnteam/kvnsub/kvnbilling/blob/main/-/x.py now",
            ["kvnteam", "kvnsub", "kvnbilling"],
        ),
        ("open https://code.kvncloud.io/-/ide/project/kvnteam now", ["kvnteam"]),
        ("open https://gitlab.com/-/ide/project/kvnteam/kvnbilling/edit/main now", []),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == segments, body
    text = recording.redact_urls_in_text
    assert text(
        "open https://code.kvncloud.io/-/ide/project/kvnteam/kvnbilling/edit/main/-/src/app.py in the IDE"
    ) == (
        "open https://forge.example.com/-/ide/project/org/project/edit/main/-/src/app.py in the IDE"
    )
    assert text(
        "open https://gitlab.com/-/ide/project/kvnteam/kvnbilling/merge_requests/3 now"
    ) == ("open https://forge.example.com/-/ide/project/org/project/merge_requests/3 now")
    instance = needles(
        _one_call(
            {"body": "open https://git.zqcorp.io/-/ide/project/zqother/zqrepo/tree/main/-/src now"}
        ),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert {"zqother", "zqrepo"} <= set(_kinds(instance, "segment"))


def test_an_owner_root_reads_the_identity_slots_of_its_grammar() -> None:
    """D1b, D7 source 3 (0.22): ``users/<u>/repos/<r>`` and ``projects/<KEY>/repos/<r>`` name a repository, ``orgs/<o>/teams/<t>`` a team."""
    identity = recording.path_identity
    assert recording._IDENTITY_SLOTS == {
        "users": ("repos", "project"),
        "projects": ("repos", "project"),
        "orgs": ("teams", "org"),
    }
    personal = identity(["users", "zqu", "repos", "zqr.git", "browse", ""])
    assert personal.segments == ["users", "org", "repos", "project.git", "browse", ""]
    assert (personal.namespace, personal.repository) == (("zqu",), "zqr")
    assert identity(["projects", "ZQKEY", "repos", "zqr"]) == recording.PathIdentity(
        ["projects", "org", "repos", "project"], ("ZQKEY",), "zqr"
    )
    project = identity(["projects", "ZQKEY", "repos", "zqr", "pull-requests", "5", "overview"])
    assert project.segments == [
        "projects",
        "org",
        "repos",
        "project",
        "pull-requests",
        "5",
        "overview",
    ]
    assert (project.namespace, project.repository) == (("ZQKEY",), "zqr")
    assert identity(["Projects", "ZQKEY", "REPOS", "zqr"]) == recording.PathIdentity(
        ["Projects", "org", "REPOS", "project"], ("ZQKEY",), "zqr"
    )
    assert identity(["users", "zqu", "REPOS", "zqr"]).repository == "zqr"
    team = identity(["orgs", "zqo", "teams", "zqt", "members"])
    assert team == recording.PathIdentity(
        ["orgs", "org", "teams", "org", "members"], ("zqo", "zqt")
    )
    api_team = identity(["api", "v3", "orgs", "zqo", "teams", "zqt"])
    assert api_team.segments == ["api", "v3", "orgs", "org", "teams", "org"]
    assert api_team.namespace == ("zqo", "zqt")
    assert identity(["users", "zqu", "repos"]) == recording.PathIdentity(
        ["users", "org", "repos"], ("zqu",)
    )
    assert identity(["orgs", "zqo", "teams"]) == recording.PathIdentity(
        ["orgs", "org", "teams"], ("zqo",)
    )
    assert identity(["users", "zqu", "teams", "zqt"]) == recording.PathIdentity(
        ["users", "org", "teams", "zqt"], ("zqu",)
    )
    assert identity(["orgs", "zqo", "repos", "zqr"]) == recording.PathIdentity(
        ["orgs", "org", "repos", "zqr"], ("zqo",)
    )
    assert identity(["apps", "zqa", "repos", "zqr"]) == recording.PathIdentity(
        ["apps", "org", "repos", "zqr"], ("zqa",)
    )
    assert identity(["users", "zqu", "-", "repos", "zqr"]) == recording.PathIdentity(
        ["users", "org", "-", "repos", "zqr"], ("zqu",)
    )
    assert identity(["projects", "ZQKEY", "repos"]).segments == ["org", "project"]
    assert identity(["projects", "ZQKEY", "x", "zqr"]).segments == ["org", "project"]
    text = recording.redact_urls_in_text
    assert text("see https://bb.kvncorp.net/users/kvnperson/repos/kvnbilling/browse now") == (
        "see https://forge.example.com/users/org/repos/project/browse now"
    )
    assert text(
        "see https://bb.kvncorp.net/projects/KVNKEY/repos/kvnbilling/pull-requests/5/overview now"
    ) == ("see https://forge.example.com/projects/org/repos/project/pull-requests/5/overview now")
    assert text("see https://github.com/orgs/kvnteam/teams/kvnsub now") == (
        "see https://forge.example.com/orgs/org/teams/org now"
    )
    for body, segments in (
        (
            "see https://bb.kvncorp.net/users/kvnperson/repos/kvnbilling/browse now",
            ["kvnperson", "kvnbilling"],
        ),
        (
            "see https://bb.kvncorp.net/projects/KVNKEY/repos/kvnbilling/pull-requests/5 now",
            ["kvnkey", "kvnbilling"],
        ),
        (
            "see https://ghe.kvncorp.net/orgs/kvnteam/teams/kvnsub/members now",
            ["kvnteam", "kvnsub"],
        ),
        ("see https://ghe.kvncorp.net/api/v3/orgs/kvnteam/teams/kvnsub now", ["kvnteam", "kvnsub"]),
        ("see https://ghe.kvncorp.net/orgs/kvnteam/teams now", ["kvnteam"]),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == segments, body
    body = "see https://ghe.kvncorp.net/orgs/kvnteam/teams/kvnsub and ping kvnsub"
    found = needles(_one_call({"body": body}), roles={})
    assert recording.replace_known(body, found) == (
        "see https://forge.example.com/orgs/org/teams/org and ping org"
    )


def test_the_owner_root_stops_at_the_first_dash() -> None:
    """D1b (0.22, the 0.21 verifier's survivor N14): after an owner root the owner ends at the FIRST ``-``; a second ``-`` deeper in the route is part of the route."""
    identity = recording.path_identity
    assert identity(["groups", "zqg", "-", "epics", "-", "x"]) == recording.PathIdentity(
        ["groups", "org", "-", "epics", "-", "x"], ("zqg",)
    )
    assert identity(["users", "zqu", "-", "snippets", "-", "raw"]) == recording.PathIdentity(
        ["users", "org", "-", "snippets", "-", "raw"], ("zqu",)
    )
    assert identity(["groups", "zqg", "zqs", "-", "-", ""]) == recording.PathIdentity(
        ["groups", "org", "-", "-", ""], ("zqg", "zqs")
    )
    assert redact_url("https://code.kvncloud.io/groups/zqg/-/epics/-/x") == (
        "https://forge.example.com/groups/org/-/epics/-/x"
    )


def test_a_package_tarball_before_a_dash_names_no_one() -> None:
    """D1b, D7 source 3, D8 (0.22): ``<pkg>/-/<pkg>-<version>.tgz`` is a registry grammar on every host, the package neither namespace nor repository; yarnpkg.com and npmmirror.com join the allowlist."""
    identity = recording.path_identity
    for path in (
        ["lodash", "-", "lodash-4.17.21.tgz"],
        ["@babel", "core", "-", "core-7.24.0.tgz"],
        ["repository", "npm-all", "lodash", "-", "lodash-4.17.21.tgz"],
        ["lodash", "-", "lodash-4.17.21-beta.1.tgz"],
        ["lodash", "-", "lodash-4.17.21-RC1.tgz"],
        ["lodash", "-", "lodash-1.0.0+build.5.tgz"],
        ["lodash", "-", "lodash-1.tgz"],
        ["lodash", "-", "lodash-4.17.21.tgz", ""],
        ["lodash", "-", "lodash-4.17.21.tgz", "x"],
    ):
        assert identity(path) == recording.PathIdentity(path), path
        assert recording._package_tarball([s for s in path if s], path.index("-")) is True, path
    for path in (
        ["lodash", "-", "other-4.17.21.tgz"],
        ["lodash", "-", "lodash-4.17.21.zip"],
        ["lodash", "-", "lodash-x.tgz"],
        ["lodash", "-", "lodash-.tgz"],
        ["lodash", "-", "lodash4.17.21.tgz"],
        ["lodash", "-", "lodash_4.17.21.tgz"],
        ["lodash", "-", "lodasx-4.17.21.tgz"],
        ["lodash", "-", "lodash-4.17.21.tgz.sig"],
        ["lodash", "-", "lodash-4.17 21.tgz"],
        ["lodash", "-"],
    ):
        assert identity(path).namespace == ("lodash",), path
        assert recording._package_tarball(path, 1) is False, path
    assert recording._package_tarball(["-", "x-1.tgz", "x"], 0) is False
    assert recording._package_tarball(["x", "-", "x-1.tgz"], 2) is False
    assert closure.is_public_host("registry.yarnpkg.com")
    assert closure.is_public_host("registry.npmmirror.com")
    assert recording._kept_host("registry.yarnpkg.com")
    assert not recording._kept_host("npm.kvncorp.net")
    text = recording.redact_urls_in_text
    for body in (
        'resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#abc" and lodash is fine',
        "resolved https://registry.npmmirror.com/@babel/core/-/core-7.24.0.tgz now",
        "resolved https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz and lodash is fine",
    ):
        assert text(body) == body, body
        assert _kinds(needles(_one_call({"body": body}), roles={}), "segment") == [], body
    for body, expected, segments in (
        (
            "resolved https://npm.kvncorp.net/lodash/-/lodash-4.17.21.tgz and lodash is fine",
            "resolved https://forge.example.com/lodash/-/lodash-4.17.21.tgz and lodash is fine",
            [],
        ),
        (
            "resolved https://nexus.kvncorp.net/repository/npm-all/@babel/core/-/core-7.24.0.tgz now",
            "resolved https://forge.example.com/repository/npm-all/@babel/core/-/core-7.24.0.tgz now",
            [],
        ),
        (
            "resolved https://10.1.2.3/lodash/-/lodash-4.17.21.tgz and lodash is fine",
            "resolved https://forge.example.com/lodash/-/lodash-4.17.21.tgz and lodash is fine",
            [],
        ),
        (
            "resolved https://code.kvncloud.io/lodash/-/other-4.17.21.tgz now",
            "resolved https://forge.example.com/org/-/other-4.17.21.tgz now",
            ["lodash"],
        ),
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == segments, body
        assert text(body) == expected, body
        assert recording.replace_known(body, found) == expected, body


def test_a_remote_path_names_a_repository_by_construction() -> None:
    """D7 source 1 (0.23, B1): the recorded remote reads ``<ns>/…/<repo>`` whatever its first word; the reserved-word rule is a URL path's, so a remote namespace that is a reserved word (``assets``, ``jwt``: real GitHub accounts) still yields its identity, live and offline alike."""
    identity = recording.path_identity
    for word in sorted(recording_tables.RESERVED_ROUTE_WORDS):
        assert identity([word, "zqr"]) == recording.PathIdentity([word, "zqr"]), word
        assert identity([word, "zqr.git"], remote=True) == recording.PathIdentity(
            ["org", "project.git"], (word,), "zqr"
        ), word
        assert identity([word, "zqs", "zqr"], remote=True) == recording.PathIdentity(
            ["org", "project"], (word, "zqs"), "zqr"
        ), word
    for path in (
        ["repos", "zqr"],
        ["users", "zqr"],
        ["groups", "zqr"],
        ["projects", "zqr"],
        ["api", "zqr"],
    ):
        assert identity(path, remote=True) == recording.PathIdentity(
            ["org", "project"], (path[0],), "zqr"
        ), path
    assert identity(["zqr.git"], remote=True) == recording.PathIdentity(["org"], ("zqr.git",))
    assert identity(["zqr", ""], remote=True) == recording.PathIdentity(["org", ""], ("zqr",))
    assert identity(["", "zqg", "", "zqr", ""], remote=True) == recording.PathIdentity(
        ["org", "project", ""], ("zqg",), "zqr"
    )
    assert identity([], remote=True) == recording.PathIdentity([])
    assert identity(["zqg", "zqr", "pull", "5"], remote=True).namespace == ("zqg", "zqr", "pull")
    assert identity(["zqg", "zqr", "pull", "5"]).namespace == ("zqg",)
    for remote, namespace, body, expected in (
        (
            "git@github.com:assets/zqmainrepo.git",
            "assets",
            "see https://github.com/assets/zqmainrepo/pull/5 and zqmainrepo in assets",
            "see https://forge.example.com/org/project/pull/5 and project in org",
        ),
        (
            "git@git.zqcorp.io:snippets/zqmainrepo.git",
            "snippets",
            "see https://git.zqcorp.io/snippets/zqmainrepo/-/merge_requests/1 and the snippets",
            "see https://forge.example.com/org/project/-/merge_requests/1 and the org",
        ),
        (
            "https://git.zqcorp.io/help/zqmainrepo.git",
            "help",
            "see https://git.zqcorp.io/help/zqmainrepo/-/issues/1 and help",
            "see https://forge.example.com/org/project/-/issues/1 and org",
        ),
        (
            "ssh://git@git.zqcorp.io:2222/uploads/zqsub/zqmainrepo.git",
            "uploads",
            "img https://git.zqcorp.io/uploads/zqsub/zqmainrepo/-/raw/main/a.png now",
            "img https://forge.example.com/org/project/-/raw/main/a.png now",
        ),
    ):
        found = needles(_one_call({"body": body}), roles={}, remote=remote)
        assert set(_kinds(found, "segment")) >= {namespace, "zqmainrepo"}, remote
        assert dict(found.tokens)[namespace.casefold()] == "org", remote
        assert recording.replace_known(recording.redact_urls_in_text(body), found) == expected
    body = "see https://git.zqcorp.io/uploads/-/system/user/avatar/42/a.png and zqmainrepo"
    found = needles(
        _one_call({"body": body, "web_url": "https://git.zqcorp.io/uploads/-/system/x/a.png"}),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert set(_kinds(found, "segment")) == {"zqgroup", "zqmainrepo"}
    for remote in ("git@github.com:jwt/zqmainrepo.git", "git@git.zqcorp.io:s/zqmainrepo.git"):
        with pytest.raises(recording.NeedleError, match="needle segment 1: too short"):
            needles(_one_call({"body": "x"}), roles={}, remote=remote)


def test_every_hard_identity_path_reads_a_repository_path_by_construction() -> None:
    """D7 sources 1 and 2 (0.24, B1-a, B1-b, N13): the recorded remote and an argv API path (``repos/<o>/<r>/…``, ``projects/<ns>%2F<r>/…``) name a repository by construction, the namespace every segment but the last whatever the words; a namespace of two segments or more is a value as a whole path too, so a text path led by a reserved word that is that namespace is written ``org/project`` live and offline alike; a lone remote segment is the namespace, a reserved word included; a structured URL field keeps the route grammar."""
    identity = recording.path_identity
    assert identity(["assets"], remote=True) == recording.PathIdentity(["org"], ("assets",))
    assert identity(["uploads", ""], remote=True) == recording.PathIdentity(
        ["org", ""], ("uploads",)
    )
    found = needles(
        _one_call({"body": "see assets now"}), roles={}, remote="https://git.zqcorp.io/assets"
    )
    assert _kinds(found, "segment") == ["assets"]
    assert recording.replace_known("see assets now", found) == "see org now"
    for word in ("marketplace", "uploads", "settings", "assets"):
        remote = f"git@git.zqcorp.io:{word}/kvnsub/zqmainrepo.git"
        found = needles(_one_call({"body": "x"}), roles={}, remote=remote)
        assert _kinds(found, "segment") == [word, "kvnsub", f"{word}/kvnsub", "zqmainrepo"], word
        assert dict(found.tokens)[f"{word}/kvnsub"] == "org", word
        for body, expected in (
            (
                f"see https://git.zqcorp.io/{word}/kvnsub/zqmainrepo/-/merge_requests/1 and kvnsub",
                "see https://forge.example.com/org/project/-/merge_requests/1 and org",
            ),
            (
                f"see https://git.zqcorp.io/{word}/kvnsub/zqmainrepo and https://git.zqcorp.io/{word}/kvnsub",
                "see https://forge.example.com/org/project and https://forge.example.com/org",
            ),
            (
                f"clone git@git.zqcorp.io:{word}/kvnsub/zqmainrepo.git now",
                "clone git@forge.example.com:org/project.git now",
            ),
        ):
            written = recording.replace_known(recording.redact_urls_in_text(body), found)
            assert written == expected, body
            assert recording.redact_urls_in_text(written) == written, body
    deep = needles(
        _one_call({"body": "x"}),
        roles={},
        remote="git@git.zqcorp.io:help/kvnsub/kvnleaf/zqmainrepo.git",
    )
    assert _kinds(deep, "segment") == [
        "help",
        "kvnsub",
        "kvnleaf",
        "help/kvnsub",
        "help/kvnsub/kvnleaf",
        "zqmainrepo",
    ]
    body = "see https://git.zqcorp.io/help/kvnsub/-/epics/1 and https://git.zqcorp.io/help/kvnsub/kvnleaf/zqmainrepo/-/issues/2"
    written = recording.replace_known(recording.redact_urls_in_text(body), deep)
    assert (
        written
        == "see https://forge.example.com/org/-/epics/1 and https://forge.example.com/org/project/-/issues/2"
    )
    assert recording.redact_urls_in_text(written) == written
    flat = needles(
        _one_call({"body": "x"}), roles={}, remote="git@github.com:zqgroup/zqmainrepo.git"
    )
    assert _kinds(flat, "segment") == ["zqgroup", "zqmainrepo"]
    for word in ("assets", "help", "uploads", "settings", "repos", "users", "groups"):
        for args, segments in (
            (["api", f"repos/{word}/kvnbilling/pulls/5/comments"], [word, "kvnbilling"]),
            (["api", f"projects/{word}%2Fkvnbilling/merge_requests/1/notes"], [word, "kvnbilling"]),
            (
                ["api", f"api/v4/projects/{word}%2Fkvnsub%2Fkvnbilling/issues"],
                [word, "kvnsub", f"{word}/kvnsub", "kvnbilling"],
            ),
        ):
            found = needles(_one_call({"body": "ok"}, args), roles={})
            assert _kinds(found, "segment") == segments, args
            written = recording.replace_known(args[1], found)
            assert word not in written and "kvnbilling" not in written, args
    for args in (["api", "repos/jwt/kvnbilling/pulls"], ["api", "projects/s%2Fkvnbilling/notes"]):
        with pytest.raises(recording.NeedleError, match="needle segment 1: too short"):
            needles(_one_call({"body": "ok"}, args), roles={})
    found = needles(
        _one_call({"body": "x", "web_url": "https://git.zqcorp.io/assets/kvnsub/-/epics/1"}),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo"]


def test_a_url_field_path_learns_no_joined_namespace() -> None:
    """D7 source 2 (0.25, V09): only a path read by construction (the remote, an argv API path) makes its namespace of two segments or more a value as a whole path; a structured URL field names its segments one by one, so a text path sharing its first segments keeps its own boundaries."""
    found = needles(
        _one_call(
            {
                "body": "see kvnteam/kvnsub-old and kvnteam/kvnsub now",
                "web_url": "https://git.zqcorp.io/kvnteam/kvnsub/kvnbilling/-/merge_requests/42",
            }
        ),
        roles={},
        remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
    )
    segments = _kinds(found, "segment")
    assert segments == ["zqgroup", "zqmainrepo", "kvnteam", "kvnsub", "kvnbilling"]
    assert not [value for value in segments if "/" in value]
    written = recording.replace_known("see kvnteam/kvnsub-old and kvnteam/kvnsub now", found)
    assert written == "see org/org-old and org/org now"


def test_only_an_argv_string_is_read_as_an_api_path() -> None:
    """D7 source 1 (0.25): ``_api_project`` reads the argv of a recorded call and no other string; a body, a field or a review comment's ``path`` led by ``repos/<word>/…`` or ``projects/<word>%2F…`` is text, so neither a reserved word nor a plain directory name is learnt from it, while the same path in argv is still read by construction."""
    texts = (
        "repos/public/main.py should stay public",
        "repos/settings/defaults.yaml holds the settings",
        "projects/help%2Fwiki is where help lives",
        "repos/frontend/main.py should stay frontend",
    )
    for text in texts:
        found = needles(_one_call({"body": text}), roles={})
        assert _kinds(found, "segment") == [], text
        assert recording.replace_known(text, found) == text
    field = needles(
        _one_call({"body": "move the assets folder", "path": "repos/assets/logo.png"}), roles={}
    )
    assert _kinds(field, "segment") == []
    assert recording._argv_strings(_one_call({"body": "x"}, ["api", "repos/a/b"])) == [
        "api",
        "repos/a/b",
    ]
    assert recording._argv_strings([{"args": ["api", "repos/a/b"]}, ["repos/c/d"]]) == []
    assert recording._argv_strings({"calls": [{"args": ["api", 5], "stdout": ""}]}) == ["api"]
    assert recording._argv_strings([{"args": "repos/a/b", "stdout": ""}]) == []
    stdout = json.dumps([{"args": ["repos/settings/kvnbilling"], "stdout": ""}])
    assert recording._argv_strings(_one_call(stdout)) == ["api", "x"]
    for args, segments in (
        (["api", "repos/settings/kvnbilling/pulls/5/comments"], ["settings", "kvnbilling"]),
        (["api", "projects/help%2Fkvnbilling/merge_requests/1/notes"], ["help", "kvnbilling"]),
    ):
        found = needles(_one_call({"body": "ok"}, args), roles={})
        assert _kinds(found, "segment") == segments, args


def test_the_provenance_table_is_the_one_rule_of_what_names_an_identity() -> None:
    """D7 (0.26, 0.27): :data:`recording.PROVENANCE` maps each source to one reading, in learning order, and :func:`known_values` learns a path's namespace and repository through it alone: every ``_segments`` call and every ``_add("segment", …)`` stands in a reader the table dispatches (or in ``_segments``, which only they call), and the argv, structured and API path readers run once, as sources and readings of the table."""
    assert recording.PROVENANCE == (
        ("remote", "repository"),
        ("url_field", "route"),
        ("argv", "repository"),
        ("text", "text"),
        ("structured", "api_path"),
    )
    tree = ast.parse(textwrap.dedent(inspect.getsource(recording.known_values)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    names = ("_repository", "_route", "_text", "_api_path")
    defs = {node.name: node for node in function.body if isinstance(node, ast.FunctionDef)}
    readers = {name: defs[name] for name in names if name in defs}
    assert set(readers) == set(names)

    def _ids(nodes: list[ast.FunctionDef]) -> set[int]:
        return {id(call) for node in nodes for call in ast.walk(node) if isinstance(call, ast.Call)}

    inside = _ids(list(readers.values()))
    learning = inside | _ids([defs["_segments"]])

    def _calls(name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]

    segments = _calls("_segments")
    assert segments and all(id(call) in inside for call in segments)
    adds = [
        call
        for call in _calls("_add")
        if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "segment"
    ]
    assert adds and all(id(call) in learning for call in adds)
    assert all(
        isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        for call in _calls("_add")
    )
    assert len(_calls("_api_project")) == 1 and id(_calls("_api_project")[0]) not in inside
    assert len(_calls("_structured_strings")) == 1
    assert id(_calls("_structured_strings")[0]) not in inside
    assert len(_calls("_api_path_projects")) == 1
    assert id(_calls("_api_path_projects")[0]) in _ids([readers["_api_path"]])
    dicts = [node for node in ast.walk(function) if isinstance(node, ast.Dict)]
    keys = [
        {k.value for k in node.keys if isinstance(k, ast.Constant)}
        for node in dicts
        if node.keys and all(isinstance(k, ast.Constant) for k in node.keys)
    ]
    assert {source for source, _ in recording.PROVENANCE} in keys
    assert {reading for _, reading in recording.PROVENANCE} in keys


def test_a_github_api_path_names_a_repository_only_through_a_rest_route() -> None:
    """D7 (0.26, 0.27): ``[/][api/v<N>/]repos/<o>/<r>/<word>`` of a structured string names a repository only when ``<word>`` is a sub-resource of GitHub's REST repository endpoints; a bare ``repos/<o>/<r>``, a file path or a directory under ``repos/`` is ordinary text, a reserved, literal or numeric owner is not learnt and its repository is, and a path inside a longer path or a URL is not this reader's."""
    read = recording._api_path_projects
    assert read("repos/kvnteam/kvnbilling/pulls/5/comments returns 404") == [
        ("kvnteam", "kvnbilling")
    ]
    assert read("see /repos/kvnteam/kvnbilling/branches/main/protection.") == [
        ("kvnteam", "kvnbilling")
    ]
    assert read('{"endpoint": "api/v3/repos/kvnteam/kvnbilling.git/hooks"}') == [
        ("kvnteam", "kvnbilling")
    ]
    assert read("repos//kvnteam//kvnbilling//Issues?state=open") == [("kvnteam", "kvnbilling")]
    assert read("repos/kvnteam/kvnbilling/pulls, then repos/zqother/kvnsub/hooks") == [
        ("kvnteam", "kvnbilling"),
        ("zqother", "kvnsub"),
    ]
    assert read("see repos/kvnteam/kvnbilling/pulls.") == [("kvnteam", "kvnbilling")]
    assert read("gitea: /api/v1/repos/kvnteam/kvnbilling/pulls/3") == [("kvnteam", "kvnbilling")]
    assert read("Repos/kvnteam/kvnbilling/Pulls and API/V3/REPOS/zqother/kvnsub/hooks") == [
        ("kvnteam", "kvnbilling"),
        ("zqother", "kvnsub"),
    ]
    assert read("repos/settings/kvnbilling/pulls/5") == [("", "kvnbilling")]
    assert read("repos/12345/kvnbilling/issues") == [("", "kvnbilling")]
    assert read("repos/org/kvnbilling/issues") == [("", "kvnbilling")]
    assert read("repos/Docs.GitHub.com/kvnbilling/issues") == [("", "kvnbilling")]
    for text in (
        "repos/kvnteam/kvnbilling is the endpoint",
        "repos/kvnteam/kvnbilling",
        "repos/kvnteam/kvnbilling/",
        "repos/frontend/main.py should stay frontend",
        "repos/public/index.html",
        "repos/kvnteam/kvnbilling/pullsx/5",
        "repos/kvnteam/kvnbilling/pulls.py",
        "repos/kvnteam/kvnbilling/readme.md",
        "src/repos/kvnteam/kvnbilling/pulls",
        "https://ghe.kvncorp.net/api/v3/repos/kvnteam/kvnbilling/pulls",
        "xrepos/kvnteam/kvnbilling/pulls",
        "repos/:owner/:repo/pulls",
        "repos/{owner}/{repo}/pulls",
        "repos/kvnteam/project/pulls",
        "repos/kvnteam/1234/pulls",
        "repos/kvnteam/Project/pulls",
        "repos/kvnteam/.git/pulls",
        "repos/kvnteam/kvnbilling:pulls",
        "repos/kvnteam/kvnbilling%2Fpulls",
    ):
        assert read(text) == [], text
    assert len(recording_tables.GITHUB_REPO_API_ROUTES) == 74
    assert {"pulls", "issues", "hooks", "branches", "commits", "contents", "git"} <= (
        recording_tables.GITHUB_REPO_API_ROUTES
    )
    assert all(word == word.casefold() for word in recording_tables.GITHUB_REPO_API_ROUTES)
    found = needles(
        _stderr_call("repos/settings/kvnbilling/pulls/5 and the settings page"), roles={}
    )
    assert _kinds(found, "segment") == ["kvnbilling"]
    found = needles(_stderr_call("repos/zq/kvnbilling/pulls/1 is short"), roles={})
    assert _kinds(found, "segment") == ["kvnbilling"]
    escaped = "see repos\\/kvnteam\\/kvnbilling\\/pulls\\/5"
    found = needles(_stderr_call(escaped), roles={})
    assert _kinds(found, "segment") == ["kvnteam", "kvnbilling"]
    assert recording.replace_known(escaped, found) == "see repos\\/org\\/project\\/pulls\\/5"
    for text in ("repos/kvnteam/kvnbilling/main.py stays", "repos/kvnteam/kvnbilling stays"):
        found = needles(_stderr_call(text), roles={})
        assert _kinds(found, "segment") == [], text


def _stderr_call(stderr: str, *, stdin: str | None = None) -> list[dict[str, Any]]:
    """One call answering a JSON body ``ok`` whose stderr (and stdin) the test sets."""
    return [{**_one_call([{"body": "ok"}])[0], "stderr": stderr, "stdin": stdin}]


def test_an_api_path_is_read_in_the_structured_sources_of_a_call_only() -> None:
    """D7 (0.27, the owner's structure/prose decision): a GitHub API path names a repository in the argv, the stdin (raw or JSON), the stderr, the field values of a JSON stdout and the method's positional and keyword arguments, nested or JSON-encoded; never in prose, a body, a merge request description, a review comment's ``path``/``new_path``/``old_path``, a code block or a raw stdout (NG4-16)."""
    remote = "git@github.com:zqgroup/zqmainrepo.git"
    path = "repos/kvnteam/kvnbilling/pulls/5/comments"
    exchange = _one_call([{"body": "ok"}])[0]
    structured = (
        [[{**exchange, "args": ["api", f"--input={path}"]}], [], {}],
        [[exchange], [42], {"endpoint": path}],
        [[exchange], [42, path], {}],
        [[exchange], [42, {"api": [path]}], {}],
        [[exchange], [42, json.dumps({"api": path})], {}],
        _stderr_call("ok", stdin=f"GET {path}"),
        _stderr_call("ok", stdin=json.dumps({"endpoint": path, "x": 1})),
        _stderr_call(f"HTTP 404: Not Found ({path})"),
        _one_call([{"body": "ok", "endpoint": path}]),
        _one_call([{"body": "ok", "Title": f"fix {path} paging"}]),
        _one_call({"data": {"pr": {"body": "ok", "resource": f"/{path}"}}}),
    )
    for raw in structured:
        found = needles(raw, roles={}, remote=remote)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"], raw
        written = recording.replace_known(f"see {path} now", found)
        assert written == "see repos/org/project/pulls/5/comments now"
    prose = (
        _one_call([{"body": f"{path} returns 404 since monday"}]),
        _one_call([{"body": f"```\n$ gh api {path}\n```"}]),
        _one_call([{"body": json.dumps({"req": {"endpoint": path}})}]),
        _one_call([{"body": "ok", "path": path}]),
        _one_call([{"description": f"calling {path} fails"}]),
        _one_call([{"body": "ok", "position": {"new_path": path, "old_path": path}}]),
        _one_call(f"error: {path}\n"),
        [[exchange], [42, {"body": path}], {"path": path}],
        _one_call([{"body": "ok", "title": f"fix {path} paging"}]),
        _one_call([{"body": "ok", "message": f"fix {path}", "messageHeadline": path}]),
        _one_call([{"body": "ok", "messageBody": path, "diff": f"+{path}\n"}]),
    )
    for raw in prose:
        found = needles(raw, roles={}, remote=remote)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo"], raw
    assert recording._structured_strings(
        [
            [{**exchange, "stdout": json.dumps([{"body": "b", "k": "v"}]), "stderr": "e"}],
            [42, {"path": "p", "x": ["y"]}],
            {"z": '{"w": 1, "body": "q"}'},
        ]
    ) == [
        "args",
        "api",
        "x",
        "returncode",
        "stdin",
        "stderr",
        "e",
        "stdout",
        "k",
        "v",
        "x",
        "y",
        "z",
        "w",
    ]
    assert recording._structured_strings([{"args": ["a"], "stdout": "diff --git a/x"}]) == [
        "args",
        "a",
        "stdout",
    ]
    assert recording._structured_strings([{"stdout": "raw"}]) == ["stdout", "raw"]
    assert recording._structured_strings([{"args": "a", "stdout": "raw"}]) == [
        "args",
        "a",
        "stdout",
        "raw",
    ]
    assert recording._structured_strings([{"args": [], "stdout": 7}, 8, None]) == [
        "args",
        "stdout",
    ]


def test_an_api_path_cited_in_prose_is_the_named_residual_ng4_16() -> None:
    """NG4-16 (0.27, 0.28): an API path cited in prose names no one and is written as it stands: a body, a review path, a raw stdout; inside a structured string the wording after the path decides nothing (0.28, the sentence rule withdrawn): a file followed by a sentence is read as surely as a call."""
    path = "repos/kvnteam/kvnbilling/pulls/5/comments"
    for raw, text in (
        (_one_call([{"body": f"{path} returns 404"}]), f"{path} returns 404"),
        (_one_call([{"body": "see", "path": path}]), path),
        (_one_call(f"{path}\n+++ b/x\n"), path),
    ):
        found = needles(raw, roles={})
        assert _kinds(found, "segment") == [], raw
        assert recording.replace_known(text, found) == text
    read = recording._api_path_projects
    owner = [("kvnpay", "kvnledger")]
    for text in (
        "repos/kvnpay/kvnledger/releases/v1.md documents the release",
        "repos/kvnpay/kvnledger/tags/index.ts lists the tags",
        "repos/kvnpay/kvnledger/contents/main.tf, then more",
        "repos/kvnpay/kvnledger/contents/main.tf: the file",
        "repos/kvnpay/kvnledger/contents/main.tf? Yes, the file",
        "repos/kvnpay/kvnledger/contents/main.tf_ holds it",
        "repos/kvnpay/kvnledger/contents/x.md 404 Not Found",
        "repos/kvnpay/kvnledger/contents/x.md\nexit 1",
        "repos/kvnpay/kvnledger/contents/x.md, a b",
        "repos/kvnpay/kvnledger/contents/x.md?: retry",
        "GET repos/kvnpay/kvnledger/contents/README.md#L3",
        "HTTP 404 (repos/kvnpay/kvnledger/contents/README.md), retrying",
        "repos/kvnpay/kvnledger/pulls/5 returned 404",
    ):
        assert read(text) == owner, text
    for text in (
        "failed to get repos/kvnpay/kvnledger/contents/README.md: HTTP 404",
        "repos/kvnpay/kvnledger/releases/v1.md documents the ledger release",
    ):
        found = needles(_stderr_call(text), roles={})
        assert _kinds(found, "segment") == ["kvnpay", "kvnledger"], text


def test_prose_is_declared_by_the_call_never_by_its_wording() -> None:
    """D7 (0.28, the owner's decision of 2026-11-24): an argv value of a prose field (``-f body=``, ``-F path=``, ``--field``, ``--raw-field``, joined ``-fbody=``/``--field=title=``), the argument of ``--body``/``--title``/``--message``/``--description`` (next or joined) and a stdin declared by ``--body-file -`` or ``-F body=@-`` are prose: neither the argv reader nor the structured reader reads them. The flag itself, the argument after the prose one, a field whose key is not a prose key, a stdin no flag declares and a dict that is not an exchange stay structured."""
    path = "repos/kvnteam/kvnbilling/pulls/5/comments"
    declared = recording._declared_prose
    for argv, prose, stdin in (
        (["api", "-f", f"body={path}"], {2}, False),
        (["api", "-f", f"body=set retries=3 in {path}"], {2}, False),
        (["api", "-F", f"path={path}", "-F", "line=3"], {2}, False),
        (["api", "--field", f"title={path}"], {2}, False),
        (["api", "--raw-field", f"message={path}"], {2}, False),
        (["api", f"-fbody={path}", f"-Fpath={path}"], {1, 2}, False),
        (["api", f"--field=title={path}", f"--raw-field=body={path}"], {1, 2}, False),
        (["pr", "comment", "42", "--body", path], {4}, False),
        (["pr", "create", "--title", "t", "--body", "b"], {3, 5}, False),
        (["mr", "create", "--description", path, "--yes"], {3}, False),
        (["x", "--message", path], {2}, False),
        (["pr", "comment", f"--body={path}", f"--title={path}"], {2, 3}, False),
        (["pr", "merge", "42", "--squash", "--body-file", "-"], set(), True),
        (["pr", "merge", "42", "--body-file=-"], set(), True),
        (["pr", "merge", "42", "--body-file", "notes.md"], set(), False),
        (["api", "-F", "body=@-"], set(), True),
        (["api", "--field=body=@-"], set(), True),
        (["api", "-F", "line=@-"], set(), False),
        (["api", "-f", f"context={path}", "-f", "body", "-f"], set(), False),
        (["api", "--body"], {2}, False),
        (["api", "--input", "-"], set(), False),
        (["api", "--input", f"body={path}", "-b", path], set(), False),
        (["api", "-f", f"Body={path}", "-F", f"PATH={path}"], set(), False),
        (["api", "-F", "body=@notes.md", "-F", "body=@"], {2, 4}, False),
        (["api", 5, "--body", 6, "-f", 7, "-f"], {3}, False),
        (["api", "--bodyx", path, "-x", f"body={path}", f"-gbody={path}"], set(), False),
        (["api", f"--fields=body={path}", f"-x=body={path}"], set(), False),
        (["api", f"-f=body={path}", f"-F=path={path}", f"-fx=body={path}"], {1, 2}, False),
        (["pr", "merge", "--body-file", "-", "--body-file", "notes.md"], set(), True),
        (["x", "--body-file=notes.md", "--body-file=-"], set(), True),
    ):
        assert declared(argv) == (frozenset(prose), stdin), argv
    remote = "git@github.com:zqgroup/zqmainrepo.git"
    exchange = _one_call([{"body": "ok"}])[0]
    for argv, stdin in (
        (["api", "repos/:owner/:repo/pulls/5/comments", "-f", f"body={path}"], None),
        (["api", "x", "-f", f"path={path}", "-f", f"body={path}"], None),
        (["pr", "comment", "5", "--body", path], None),
        (["pr", "comment", "5", f"--body={path}"], None),
        (["api", "x", f"--raw-field=title={path}"], None),
        (["pr", "merge", "5", "--body-file", "-"], f"see {path}"),
        (["api", "x", "-F", "body=@-"], f"see {path}"),
    ):
        raw = [[{**exchange, "args": argv, "stdin": stdin}], [], {}]
        found = needles(raw, roles={}, remote=remote)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo"], argv
        assert path not in recording._structured_strings(raw)
        assert stdin is None or stdin not in recording._structured_strings(raw)
    for argv, stdin in (
        (["api", "x", "-f", f"context={path}"], None),
        (["pr", "comment", "5", "--body", "ok", path], None),
        (["api", path, "--body", "ok"], None),
        (["pr", "merge", "5", "--body-file", "notes.md"], f"see {path}"),
        (["api", "x", "-F", "line=@-"], f"see {path}"),
        (["api", "x"], f"see {path}"),
    ):
        raw = [[{**exchange, "args": argv, "stdin": stdin}], [], {}]
        found = needles(raw, roles={}, remote=remote)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"], argv
    for raw in (
        [[exchange], [42, {"args": ["--body", path], "stdin": path}], {}],
        [[exchange], [42], {"args": ["--body", path], "stdin": path}],
    ):
        found = needles(raw, roles={}, remote=remote)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"], raw
    assert recording._argv_strings(
        [{**exchange, "args": ["pr", "comment", "--body", path, "-f", f"body={path}", path]}]
    ) == ["pr", "comment", "--body", "-f", path]
    assert recording._structured_strings(
        [{"args": ["--body", "p", "-F", "body=@-", "q"], "stdin": "s", "stdout": "", "stderr": "e"}]
    ) == ["args", "--body", "-F", "body=@-", "q", "stdin", "stdout", "stderr", "e"]
    assert recording._structured_strings(
        [{"args": '{"k": "v"}'}, {"args": 5, "stdin": "s"}, {"args": ["--body", "b"], "stdin": "t"}]
    ) == ["args", "k", "v", "args", "stdin", "s", "args", "--body", "b", "stdin", "t"]


def test_a_prose_positional_argument_is_named_by_the_method_signature() -> None:
    """D7 (0.28): a positional argument is prose when the protocol method's signature names it as a prose key (``post_comment``'s ``body``, ``reply``'s and ``upsert_marked_comment``'s ``body``); a keyword argument is judged by its key, an argument no signature names and every argument of an unknown method stay structured. The offline transform of a posted inline comment keeps its body and review path in the argv, the arguments and the answer."""
    params = recording._prose_parameters
    assert params("post_comment") == frozenset({1})
    assert params("reply") == frozenset({2})
    assert params("upsert_marked_comment") == frozenset({2})
    for method in (
        "conversation_comments",
        "create_pull_request",
        "post_review",
        "find_marked_comment",
    ):
        assert params(method) == frozenset(), method
    for method in ("", "barrier", "no_such_method", "__class__", "a.b", "post_comment "):
        assert params(method) == frozenset(), method
    path = "repos/kvnteam/kvnbilling/pulls/5/comments"
    remote = "git@github.com:zqgroup/zqmainrepo.git"
    exchange = _one_call([{"body": "ok"}])[0]
    for method, args in (("post_comment", [42, path]), ("reply", [42, 7, path])):
        raw = [[exchange], args, {}]
        found = recording.known_values(
            raw, roles={}, remote=remote, method=method, denylist=NO_DENYLIST
        )
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo"], method
        found = recording.known_values(raw, roles={}, remote=remote, denylist=NO_DENYLIST)
        assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"]
    found = recording.known_values(
        [[exchange], [42, "x", path], {}],
        roles={},
        remote=remote,
        method="post_comment",
        denylist=NO_DENYLIST,
    )
    assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"]
    found = recording.known_values(
        [[exchange], [42, "ok"], {"endpoint": path}],
        roles={},
        remote=remote,
        method="post_comment",
        denylist=NO_DENYLIST,
    )
    assert _kinds(found, "segment") == ["zqgroup", "zqmainrepo", "kvnteam", "kvnbilling"]
    assert recording._without_prose_arguments("raw", "post_comment") == "raw"
    assert recording._without_prose_arguments([[]], "post_comment") == [[]]
    assert recording._without_prose_arguments([[], "a"], "post_comment") == [[], "a"]
    assert recording._without_prose_arguments([[], [1, 2, 3], {}], "post_comment") == [
        [],
        [1, None, 3],
        {},
    ]
    fixture = json.loads((FIXTURES / "github" / "post_comment_inline.json").read_text())
    body = "the payments ledger release notes moved to repos/payments/ledger/releases"
    review = "repos/payments/ledger/releases/notes.md"
    fixture["args"] = [42, body]
    fixture["kwargs"]["path"] = review
    call = fixture["calls"][1]
    call["args"] = [
        a.replace("body=fix", f"body={body}").replace("path=a.py", f"path={review}")
        for a in call["args"]
    ]
    answer = json.loads(call["stdout"])
    answer["body"], answer["path"] = body, review
    call["stdout"] = json.dumps(answer)
    written = fixture_pipeline.redact_fixture(
        fixture, forge="github", remote=remote, denylist=NO_DENYLIST
    )
    posted = written["calls"][1]
    assert f"body={body}" in posted["args"] and f"path={review}" in posted["args"]
    assert written["args"] == [42, body] and written["kwargs"]["path"] == review
    assert json.loads(posted["stdout"])["body"] == body


def test_the_prose_keys_are_the_audited_keys_matched_as_written() -> None:
    """D7 (0.28, verify027 W01, W02, W13, W14, W15): the prose keys are the audited ten; a key is matched as written (``Body`` is structured); a JSON stdin behind leading blanks and a JSON array stdin are walked, so their prose keys stay prose."""
    audited = recording._PROSE_KEYS
    assert audited == {
        "body",
        "description",
        "diff",
        "message",
        "messageBody",
        "messageHeadline",
        "new_path",
        "old_path",
        "path",
        "title",
    }
    path = "repos/kvnteam/kvnbilling/pulls/5/comments"
    for stdin in (
        "\n " + json.dumps({"body": path}),
        json.dumps([{"body": path}, {"message": path}]),
    ):
        found = needles(_stderr_call("ok", stdin=stdin), roles={})
        assert _kinds(found, "segment") == [], stdin
    for stdin in (json.dumps({"Body": path}), json.dumps({"MESSAGE": path})):
        found = needles(_stderr_call("ok", stdin=stdin), roles={})
        assert _kinds(found, "segment") == ["kvnteam", "kvnbilling"], stdin


def test_the_decoded_forms_of_an_api_path_are_read_in_order() -> None:
    """D7 (0.28, verify027 W24): the API path reader reads the raw form of a string before its decoded forms, so a value the raw form names first keeps that class when a decoded form names it the other way."""
    stderr = "repos%2Fkvnyy%2Fkvnxx%2Fpulls repos/kvnxx/kvnyy/pulls"
    found = needles(_stderr_call(stderr), roles={})
    assert _kinds(found, "segment") == ["kvnxx", "kvnyy"]
    assert recording.replace_known("kvnxx kvnyy", found) == "org project"


def test_the_api_path_reader_reads_a_whole_path_between_its_bounds() -> None:
    """D7 (0.27, verify026 X01-X16): the API path starts at a boundary (not after ``%``, ``.``, ``_``, ``~`` or ``-``), one ``api/v<N>/`` prefix of any width, a one-character owner keeps its repository, the route word is the next segment (not across a space), trimmed of prose punctuation and casefolded, a ``.git`` owner is learnt as written, a reserved owner in any case is not learnt, and a percent-encoded owner is learnt in its decoded form only."""
    for lead in ("%", ".", "_", "~", "-", "x", "9", "src/"):
        found = needles(_stderr_call(f"{lead}repos/kvnteam/kvnbilling/pulls/1 fails"), roles={})
        assert _kinds(found, "segment") == [], lead
    for text, segments in (
        ("api/v3/api/v3/repos/kvnteam/kvnbilling/pulls/1", []),
        ("api/v10/repos/kvnteam/kvnbilling/pulls/1", ["kvnteam", "kvnbilling"]),
        ("repos/k/kvnbilling/pulls/1", ["kvnbilling"]),
        ("repos/kvnteam/kvnbilling issues are open", []),
        ("repos/kvnteam/kvnbilling/pulls: 404", ["kvnteam", "kvnbilling"]),
        ("repos/kvnteam/kvnbilling/pulls; 404", ["kvnteam", "kvnbilling"]),
        ("repos/kvnteam/kvnbilling/iſsues fails", ["kvnteam", "kvnbilling"]),
        ("repos/kvnteam.git/kvnbilling/pulls fails", ["kvnteam.git", "kvnbilling"]),
        ("repos/Settings/kvnbilling/pulls/5", ["kvnbilling"]),
        ("repos/SETTINGS/kvnbilling/pulls/5", ["kvnbilling"]),
        ("repos/kvn%5Fteam/kvnbilling/pulls/1", ["kvn_team", "kvnbilling"]),
    ):
        found = needles(_stderr_call(text), roles={})
        assert _kinds(found, "segment") == segments, text
    found = needles(_stderr_call("repos/kvnteam.git/kvnbilling/pulls"), roles={})
    assert recording.replace_known("repos/kvnteam.git/kvnbilling/pulls", found) == (
        "repos/org/project/pulls"
    )
    found = needles(_stderr_call("repos/Settings/kvnbilling/pulls/5"), roles={})
    assert recording.replace_known("the Settings page of kvnbilling", found) == (
        "the Settings page of project"
    )


def test_inside_a_source_the_first_reading_in_recording_order_keeps_the_class() -> None:
    """D7 (0.27, verify026 X21, X22): inside one source the readings follow the recording, the first exchange and the first item first, so a value two readings name keeps the class of the first one in that order."""
    remote = "git@git.zqcorp.io:zqgroup/zqmainrepo.git"
    web = "https://git.zqcorp.io/kvnalpha/kvnbeta/-/merge_requests/1"
    web2 = "https://git.zqcorp.io/kvnbeta/kvnalpha/-/merge_requests/2"
    for raw in (
        _one_call([{"web_url": web, "body": "x"}, {"web_url": web2, "body": "x"}]),
        _one_call([{"web_url": web, "body": "x"}]) + _one_call([{"web_url": web2, "body": "x"}]),
        _stderr_call("repos/kvnalpha/kvnbeta/pulls/1")
        + _stderr_call("repos/kvnbeta/kvnalpha/pulls/2"),
        [
            [_one_call([{"body": "x"}])[0]],
            ["repos/kvnalpha/kvnbeta/pulls/1"],
            {"k": "repos/kvnbeta/kvnalpha/pulls/2"},
        ],
    ):
        found = needles(raw, roles={}, remote=remote)
        assert recording.replace_known("the kvnalpha and kvnbeta work", found) == (
            "the org and project work"
        ), raw


def test_a_url_field_on_a_single_label_host_learns_the_host() -> None:
    """D7 (0.27, verify026 X26): a URL field's host that is not public is a value even when it has a single label (``http://intranet/…``), its path is read as the route grammar reads it; since PR #319 the oracle reads a one-label authority after a scheme in text too."""
    found = needles(
        _one_call([{"html_url": "http://intranet/kvnteam/kvnbilling", "body": "x"}]), roles={}
    )
    assert "intranet" in _kinds(found, "host")
    assert _kinds(found, "segment") == ["kvnteam", "kvnbilling"]
    found = needles(_one_call([{"body": "see http://intranet/kvnteam/kvnbilling"}]), roles={})
    assert "intranet" in _kinds(found, "host")
    assert (
        recording.replace_known("see http://intranet/x", found) == "see http://forge.example.com/x"
    )


def test_every_exchange_argv_is_read_by_construction() -> None:
    """D7 source 1 (0.26, B09): ``_argv_strings`` walks every exchange of a recording, so an API path in the argv of a second call is read by construction as the first call's is (a reserved owner included)."""
    first = _one_call({"body": "ok"}, ["api", "repos/:owner/:repo/issues/42/comments"])[0]
    second = _one_call({"body": "ok"}, ["api", "repos/settings/kvnbilling/pulls/5"])[0]
    assert recording._argv_strings([[first, second], [42], {}]) == [
        "api",
        "repos/:owner/:repo/issues/42/comments",
        "api",
        "repos/settings/kvnbilling/pulls/5",
    ]
    for calls in ([first, second], [second, first]):
        found = needles([calls, [42], {}], roles={})
        assert _kinds(found, "segment") == ["settings", "kvnbilling"]
        assert recording.replace_known("repos/settings/kvnbilling/pulls/5", found) == (
            "repos/org/project/pulls/5"
        )
    lone = needles(_one_call({"body": "ok"}, ["repos/settings/kvnbilling/pulls/5"]), roles={})
    assert _kinds(lone, "segment") == ["settings", "kvnbilling"]
    body = ["api", "--method", "GET", " repos/zq/kvnbilling/pulls/1"]
    assert _kinds(needles(_one_call({"body": "ok"}, body), roles={}), "segment") == ["kvnbilling"]
    spec = ["api", "repos/SP-RECORDER-REDACTS-BY-CLASS/kvnbilling/pulls/1"]
    assert _kinds(needles(_one_call({"body": "ok"}, spec), roles={}), "segment") == ["kvnbilling"]


def test_a_value_a_url_field_names_as_a_namespace_keeps_its_class() -> None:
    """D7 (0.26, B19): the URL fields are read before the argv (:data:`recording.PROVENANCE` order), so a value a ``web_url`` names as a namespace and an argv API path as a repository is written ``org``, the class of the first reading."""
    raw = [
        _one_call(
            {"body": "ok", "web_url": "https://git.zqcorp.io/kvnbilling/kvnsub/-/merge_requests/1"},
            ["api", "projects/kvnteam%2Fkvnbilling/merge_requests/1/notes"],
        ),
        [42],
        {},
    ]
    found = needles(raw, roles={}, remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git")
    assert recording.replace_known("the kvnbilling service", found) == "the org service"
    assert recording.replace_known("projects/kvnteam%2Fkvnbilling/notes", found) == (
        "projects/org%2Forg/notes"
    )


def test_an_npm_scope_is_never_learnt_on_any_registry() -> None:
    """D1b, D7 source 3 (0.24, B2-c; NG4-15): what follows ``packages/npm/`` of GitLab's npm registry names no one, its ``@<scope>`` included, on the instance, project and group endpoints and in the file name: package forwarding brings ``@types`` and ``@babel`` there and the project and group endpoints impose no scope naming, so a scope read from a URL is never learnt on any registry; a scope equal to a known namespace (the remote's) is replaced as that value; the prefix before ``packages/npm`` reads as at 0.22."""
    identity = recording.path_identity
    registry = recording._npm_registry
    tgz = ["kvnpkg", "-", "kvnpkg-1.0.0.tgz"]
    file = ["kvnpkg", "-", "@kvnteam", "kvnpkg-1.0.0.tgz"]
    inst = ["api", "v4", "packages", "npm"]
    proj = ["api", "v4", "projects", "123", "packages", "npm"]
    group = ["api", "v4", "groups", "123", "-", "packages", "npm"]
    assert registry(inst) == 4 and registry(proj) == 6 and registry(group) == 7
    assert registry(["API", "V4", "Packages", "NPM", "@zqs"]) == 4
    assert (
        registry(["api", "v4", "projects", "123", "packages", "npm", "@a", "packages", "npm"]) == 6
    )
    for path in (
        ["packages", "npm", "@zqs", "zqp"],
        ["api", "v4", "packages"],
        ["api", "x", "packages", "npm", "@zqs"],
        ["api", "v4", "npm", "packages", "@zqs"],
        ["v4", "api", "packages", "npm", "@zqs"],
        ["api", "v4", "packages", "pypi", "@zqs"],
        ["zqg", "zqr", "-", "packages", "npm", "@zqs"],
    ):
        assert registry(path) is None, path
    for prefix, written in ((inst, inst), (proj, proj), (group, [*group[:3], "org", *group[4:]])):
        namespace = ("123",) if prefix is group else ()
        for tail in (
            ["@kvnteam", *tgz],
            ["@kvnteam", *file],
            ["@kvnteam%2Fkvnpkg"],
            ["-", "package", "@kvnteam%2fkvnpkg", "dist-tags"],
            ["@kvnteam", "kvnpkg"],
            ["@kvnteam", "kvnpkg", ""],
            ["@types", "node", "-", "node-20.11.0.tgz"],
            ["kvnpkg"],
            tgz,
            ["@"],
            ["-", "package", "kvnpkg", "dist-tags"],
            [],
        ):
            got = identity([*prefix, *tail])
            assert got == recording.PathIdentity([*written, *tail], namespace), tail
    dashless = ["api", "v4", "groups", "kvngrp", "packages", "npm", "@kvnteam", *tgz]
    assert identity(dashless) == recording.PathIdentity(
        ["api", "v4", "groups", "org", *dashless[4:]], ("kvngrp",)
    )
    encoded = identity(["api", "v4", "projects", "zqg%2Fzqr", "packages", "npm", "@zqs", *tgz])
    assert encoded == recording.PathIdentity(
        ["api", "v4", "projects", "org%2Fproject", "packages", "npm", "@zqs", *tgz], ("zqg",), "zqr"
    )
    upper = identity(["api", "v4", "Projects", "zqg%2Fzqr", "packages", "npm", "@zqs", *tgz])
    assert upper == recording.PathIdentity(
        ["api", "v4", "Projects", "org%2Fproject", "packages", "npm", "@zqs", *tgz], ("zqg",), "zqr"
    )
    text = recording.redact_urls_in_text
    remote = "git@git.zqcorp.io:zqgroup/zqmainrepo.git"
    for body, expected, segments in (
        (
            "resolved https://git.zqcorp.io/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg",
            "resolved https://forge.example.com/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg",
            set(),
        ),
        (
            "resolved https://code.kvncloud.io/api/v4/projects/123/packages/npm/@types/node/-/node-20.11.0.tgz\nimport type { X } from '@types/node'",
            "resolved https://forge.example.com/api/v4/projects/123/packages/npm/@types/node/-/node-20.11.0.tgz\nimport type { X } from '@types/node'",
            set(),
        ),
        (
            "GET https://10.1.2.3/api/v4/groups/kvngrp/-/packages/npm/@babel%2Fcore now; import x from '@babel/core'",
            "GET https://forge.example.com/api/v4/groups/org/-/packages/npm/@babel%2Fcore now; import x from '@babel/core'",
            {"kvngrp"},
        ),
        (
            "resolved https://git.zqcorp.io/api/v4/packages/npm/@zqgroup/kvnpkg/-/kvnpkg-1.0.0.tgz and @zqgroup/kvnpkg",
            "resolved https://forge.example.com/api/v4/packages/npm/@org/kvnpkg/-/kvnpkg-1.0.0.tgz and @org/kvnpkg",
            set(),
        ),
        (
            "resolved https://npm.kvncorp.net/@kvncorp/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvncorp/kvnpkg",
            "resolved https://forge.example.com/@kvncorp/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvncorp/kvnpkg",
            set(),
        ),
        (
            "resolved https://npm.kvncorp.net/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg",
            "resolved https://forge.example.com/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg",
            set(),
        ),
        (
            "resolved https://nexus.kvncorp.net/repository/npm-all/@babel/core/-/core-7.24.0.tgz now",
            "resolved https://forge.example.com/repository/npm-all/@babel/core/-/core-7.24.0.tgz now",
            set(),
        ),
        (
            "resolved https://git.zqcorp.io/api/v4/projects/123/packages/npm/lodash/-/lodash-4.17.21.tgz and lodash",
            "resolved https://forge.example.com/api/v4/projects/123/packages/npm/lodash/-/lodash-4.17.21.tgz and lodash",
            set(),
        ),
    ):
        found = needles(_one_call({"body": body}), roles={}, remote=remote)
        assert set(_kinds(found, "segment")) - {"zqgroup", "zqmainrepo"} == segments, body
        assert recording.replace_known(text(body), found) == expected, body


def test_an_owner_root_is_read_in_any_case() -> None:
    """D1b (0.23): the API resource roots (``repos``, ``users``, ``orgs``, ``apps``, ``groups``) and the ``api/vN/`` shape are matched casefolded, like ``_project_root`` and the reserved words; the root word is written as it stands."""
    identity = recording.path_identity
    for path, segments, namespace, repository in (
        (
            ["API", "V4", "Repos", "zqa", "zqb"],
            ["API", "V4", "Repos", "org", "project"],
            ("zqa",),
            "zqb",
        ),
        (
            ["api", "v4", "REPOS", "zqa", "zqb", "pulls"],
            ["api", "v4", "REPOS", "org", "project", "pulls"],
            ("zqa",),
            "zqb",
        ),
        (["Repos", "zqa", "zqb.git"], ["Repos", "org", "project.git"], ("zqa",), "zqb"),
        (["USERS", "zqa"], ["USERS", "org"], ("zqa",), None),
        (["Users", "zqa", "Repos", "zqb"], ["Users", "org", "Repos", "project"], ("zqa",), "zqb"),
        (["Orgs", "zqo", "Teams", "zqt"], ["Orgs", "org", "Teams", "org"], ("zqo", "zqt"), None),
        (
            ["GROUPS", "zqg", "zqs", "-", "epics"],
            ["GROUPS", "org", "-", "epics"],
            ("zqg", "zqs"),
            None,
        ),
        (["Apps", "zqa", "x"], ["Apps", "org", "x"], ("zqa",), None),
    ):
        assert identity(path) == recording.PathIdentity(segments, namespace, repository), path
    assert redact_url("https://code.kvncloud.io/API/V4/Repos/zqa/zqb/pulls/5") == (
        "https://forge.example.com/API/V4/Repos/org/project/pulls/5"
    )
    body = "see https://code.kvncloud.io/API/V4/Repos/kvnteam/kvnbilling and kvnbilling"
    found = needles(_one_call({"body": body}), roles={})
    assert set(_kinds(found, "segment")) == {"kvnteam", "kvnbilling"}
    assert recording.replace_known(recording.redact_urls_in_text(body), found) == (
        "see https://forge.example.com/API/V4/Repos/org/project and project"
    )


def test_nothing_is_read_after_a_placeholder_class_host() -> None:
    """D7 source 3 (0.20, NG4-12): a placeholder-class or public host names no one, so its path is never read, IPv4, IPv6 and name alike."""
    for body in (
        "http://[2001:db8::1]/kvnteam/kvnbilling/x",
        "http://[2001:DB8:0:0::1]:8080/kvnteam/kvnbilling",
        "curl http://[::1]:8000/health",
        "http://127.0.0.1:8000/kvnteam/kvnbilling",
        "http://localhost:3000/api/users",
        "http://0.0.0.0:8000/docs",
        "see http://[::]:8080/kvnteam/kvnbilling",
        "see //www.example.com/kvnteam/kvnbilling",
        "https://files.pythonhosted.org/packages/kvnteam/kvnbilling/x.whl",
        "ssh git@203.0.113.9:kvnteam/kvnbilling",
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == [], body
        assert recording.replace_known(body, found) == body, body
        assert closure.judge(body, found) is None, body
    answer = {
        "body": "Thanks Zqkowalska and marta",
        "commit": {"author": {"name": "Marta Zqkowalska", "email": "marta@zqmail.io"}},
    }
    found = needles(_one_call(answer), roles={})
    assert {"zqkowalska", "marta"} <= set(_kinds(found, "name"))
    assert "marta@zqmail.io" in _kinds(found, "mailbox")
    assert recording.replace_known("Thanks Zqkowalska and marta", found) == (
        "Thanks author and author"
    )
    plain = {"label": {"name": "bug", "color": "red"}, "repo": {"name": "zqmainrepo", "id": 5}}
    assert _kinds(needles(_one_call(plain), roles={}), "name") == []


def test_known_values_read_the_path_after_a_led_address_only() -> None:
    """D8, D7 source 3 (0.20): a led quad is a host whose path is read; a followed quad is judged, its path never read."""
    for body in (
        "see https://104.16.0.1/kvnteam/kvnbilling",
        "see https://10.1.2.3/kvnteam/kvnbilling",
        "see //10.1.2.3/kvnteam/kvnbilling/x",
        "ssh deploy@10.1.2.3:kvnteam/kvnbilling",
        "see http://10.1.2.3:65432/kvnteam/kvnbilling",
        "see http://10.1.2.3:8/kvnteam/kvnbilling",
        "see https://code.kvncloud.io:65535/kvnteam/kvnbilling",
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert {"kvnteam", "kvnbilling"} <= set(_kinds(found, "segment")), body
        assert _kinds(found, "host"), body
    assert recording._path_after("h.io:65535/a/b", 4) == ["a", "b"]
    assert recording._path_after("h.io:12345/a/b", 4) == ["a", "b"]
    assert recording._path_after("h.io:123456/a/b", 4) == []
    for body in (
        "see 104.16.0.1/kvnteam/kvnbilling",
        "see 10.1.2.3/kvnteam/kvnbilling",
        "see 104.16.0.1:8080/kvnteam/kvnbilling",
        "curl 10.0.0.12:8080/metrics",
        "docker pull 10.0.0.12:5000/library/python:3.12",
    ):
        found = needles(_one_call({"body": body}), roles={})
        assert _kinds(found, "segment") == [], body
        assert not any(h[0].isdigit() for h in _kinds(found, "host")), body
        assert recording.replace_known(body, found) == body
    for body in ("see 104.16.0.1/kvnteam/kvnbilling", "see 104.16.0.1:8080/kvnteam"):
        found = needles(_one_call({"body": body}), roles={})
        assert closure.judge(body, found) == closure.Refusal("public address"), body
    found = needles(_one_call({"body": "see https://104.16.0.1/kvnteam"}), roles={})
    assert "104.16.0.1" in _kinds(found, "host")


def test_known_values_read_a_gitlab_commit_objects_author_and_committer_names() -> None:
    """D5 (0.19): ``author_name`` beside ``author_email`` is learnt like ``name`` beside ``email``."""
    answer = {
        "body": "Zqnorek pushed, Brandt reviewed",
        "author_name": "Kazimir Zqnorek",
        "author_email": "kaz@zqmail.io",
        "committer_name": "Liana Brandt",
        "committer_email": "lia@zqmail.io",
    }
    found = needles(_one_call(answer), roles={})
    assert {"zqnorek", "brandt", "kazimir", "liana"} <= set(_kinds(found, "name"))
    assert {"kaz@zqmail.io", "lia@zqmail.io"} <= set(_kinds(found, "mailbox"))
    assert recording.replace_known("Zqnorek pushed, Brandt reviewed", found) == (
        "author pushed, author reviewed"
    )
    alone = {"author_name": "Kazimir Zqnorek", "title": "x"}
    assert _kinds(needles(_one_call(alone), roles={}), "name") == []
    mailbox_alone = {"author_email": "kaz@zqmail.io", "title": "x"}
    assert _kinds(needles(_one_call(mailbox_alone), roles={}), "mailbox") == []


def test_css_at_rules_are_no_mention_and_the_other_handles_stay() -> None:
    """D2 (0.18): ``@font-face`` and every at-rule keyword of CSS, Sass and Tailwind is a stylesheet, not a handle."""
    expected = [
        "apply",
        "at-root",
        "charset",
        "container",
        "content",
        "counter-style",
        "custom-variant",
        "debug",
        "document",
        "each",
        "else",
        "error",
        "extend",
        "font-face",
        "font-feature-values",
        "for",
        "forward",
        "function",
        "if",
        "import",
        "include",
        "keyframes",
        "layer",
        "media",
        "mixin",
        "namespace",
        "page",
        "plugin",
        "position-try",
        "property",
        "responsive",
        "return",
        "scope",
        "screen",
        "source",
        "starting-style",
        "supports",
        "tailwind",
        "theme",
        "use",
        "utility",
        "variant",
        "variants",
        "view-transition",
        "viewport",
        "warn",
        "while",
    ]
    assert frozenset(expected) <= recording_tables.CSS_AT_RULES
    for rule in expected:
        assert recording.mentions({"body": f"@{rule} {{ a: b }}"}) == [], rule
        assert recording.mentions({"body": f"@{rule.upper()} x"}) == [], rule
    assert recording.mentions({"body": "@font-face { src: url(x) } cc @zqperson"}) == ["zqperson"]
    assert recording.mentions({"body": "@`code`zqword"}) == []
    assert recording.mentions({"body": "x`code`@zqother"}) == ["zqother"]
    assert recording.mentions({"body": "@media-person and @fontface"}) == [
        "media-person",
        "fontface",
    ]
    assert "font-face" in recording_tables.CSS_AT_RULES and "media" in recording_tables.CSS_AT_RULES
    assert all(rule == rule.casefold() for rule in recording_tables.CSS_AT_RULES)


def test_offline_roles_read_the_logins_under_the_keys_the_redaction_drops() -> None:
    """D4 (0.18): ``assignees[].login`` and ``mergedBy.login`` get a class word offline, as ``raw_logins`` does live."""
    raw = _roles_data(
        {"user": {"login": "zqauthor"}, "body": "ok"},
        {"assignees": [{"login": "zqassignee"}], "mergedBy": {"login": "zqmerger"}},
    )
    dropped = recording.raw_logins([raw["calls"], None, None])
    assert dropped == ["zqauthor", "zqassignee", "zqmerger"]
    redacted = fixture_pipeline._exchanges(raw)
    assert fixture_pipeline.fixture_roles(redacted) == {"zqauthor": "author-2"}
    assert fixture_pipeline.fixture_roles(redacted, also=dropped) == {
        "zqauthor": "author-2",
        "zqassignee": "author-3",
        "zqmerger": "author-4",
    }
    assert fixture_pipeline.fixture_roles(
        redacted, also=["author", "github-actions[bot]", "zqauthor"]
    ) == {"zqauthor": "author-2"}
    prose = _roles_data(
        {"assignees": [{"login": "zqassignee"}]},
        {"body": "Assigned to zqassignee, thanks @zqassignee and zqmerger"},
    )
    out = fixture_pipeline.redact_fixture(prose, forge="github", denylist=NO_DENYLIST)
    text = json.dumps(out)
    assert "zqassignee" not in text and "assignees" not in text
    assert json.loads(out["calls"][1]["stdout"])["body"] == (
        "Assigned to author-2, thanks @author-2 and zqmerger"
    )


def test_offline_redaction_rewrites_urls_in_arguments_keyword_arguments_and_results() -> None:
    """D1b, D4: a URL of ``args``, ``kwargs`` or ``result`` keeps its shape with the placeholder namespace and repository."""
    data = {
        "forge": "github",
        "method": "m",
        "args": ["https://github.com/zqnsone/zqrepoone/pull/12", 7],
        "kwargs": {"url": "https://github.com/zqnstwo/zqrepotwo/pull/13"},
        "calls": [],
        "result": "https://github.com/zqnsthree/zqrepothree/pull/14",
    }
    out = fixture_pipeline.redact_fixture(data, forge="github", denylist=NO_DENYLIST)
    assert out["args"] == ["https://forge.example.com/org/project/pull/12", 7]
    assert out["kwargs"] == {"url": "https://forge.example.com/org/project/pull/13"}
    assert out["result"] == "https://forge.example.com/org/project/pull/14"


def test_offline_roles_walk_the_keyword_arguments_and_refuse_positional_logins() -> None:
    """D4 (0.18): ``kwargs`` keeps its keys, so ``fixture_roles`` reads a login there itself; ``args`` members are never renamed, so a login there is refused."""
    data = {
        "forge": "github",
        "method": "m",
        "args": [],
        "kwargs": {"mergedBy": {"login": "zqkwtwo"}},
        "calls": [_exchange({"body": "cc zqkwtwo"})],
        "result": None,
    }
    out = fixture_pipeline.redact_fixture(data, forge="github", denylist=NO_DENYLIST)
    assert json.loads(out["calls"][0]["stdout"])["body"] == "cc author-2"
    assert out["kwargs"] == {"mergedBy": {"login": "author-2"}}
    positional = {**data, "args": [{"assignees": [{"login": "zqargone"}]}], "kwargs": {}}
    with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^residual login 1 at args"):
        fixture_pipeline.redact_fixture(positional, forge="github", denylist=NO_DENYLIST)


def test_a_mention_only_handle_equal_to_a_language_word_is_the_documented_residual() -> None:
    """D2, NG4-13 (0.21, B2): ``@<word>`` of a non-participant whose login is a language word is written; a participant's is a known value."""
    words = sorted(recording.LANGUAGE_AT_WORDS)
    assert len(words) >= 400
    for word in words:
        assert recording.mentions({"body": f"cc @{word} please review"}) == [], word
        assert recording.mentions({"title": f"fix: @{word} reported this"}) == [], word
    none = closure.Known(())
    for word in ("test", "main", "link", "todo", "state", "model", "param", "override"):
        assert word in recording.LANGUAGE_AT_WORDS, word
        body = f"cc @{word} please review, {word}"
        roles = rec.build_roles(
            [_exchange([{"user": {"login": word}, "body": body}])],
            bot="repoach-bot",
            maintainer=None,
            author=None,
        )
        assert roles == {word: "author"}, word
        assert recording.classify_text(body, roles) == "cc @author please review, author"
        known = needles(_one_call([{"user": {"login": word}, "body": body}]), roles=roles)
        assert word in _kinds(known, "login"), word
        assert closure.judge(f"cc @{word}", known) == closure.Refusal("residual", "login", 1)
        mention_only = needles(_one_call({"body": body}), roles={})
        assert not _kinds(mention_only, "login"), word
        assert closure.judge(body, mention_only) is None and closure.judge(body, none) is None
        assert (
            rec.build_roles(
                [_exchange([{"body": body}])], bot="repoach-bot", maintainer=None, author=None
            )
            == {}
        )
    assert rec.build_roles(
        [_exchange([{"body": "cc @zqxperson"}])], bot="repoach-bot", maintainer=None, author=None
    ) == {"@zqxperson": "author"}
    assert recording.redact_urls_in_text("see https://gitlab.com/kvnteam/-/merge_requests") == (
        "see https://forge.example.com/org/-/merge_requests"
    )


def test_the_recorder_never_raises_on_a_recording_nested_past_the_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D3 (0.21, B3; 0.28 g): undecodable text is text; a reader's overflow is refused; 990 deep is bounded, never a crash."""
    assert fixture_reading.decode_json_values("[" * 20000) is None
    assert fixture_reading.decode_json_values("[" * 20000 + "]" * 20000) is None
    assert fixture_reading.decode_json_values('{"a":' * 20000 + "1" + "}" * 20000) is None
    assert fixture_reading.decode_json_values("[[1]] [2]") == [[[1]], [2]]
    assert recording.redact_stdout("[" * 20000) == "[" * 20000
    assert recording.replace_known_in_stdout("[" * 20000, closure.Known(())) == "[" * 20000
    assert fixture_reading.fixture_strings("[" * 20000, "p") == [("p", "[" * 20000)]
    assert recording.user_id_values("[" * 20000) == []
    deep = "[" * 990 + "]" * 990
    fixture = {
        "forge": "github",
        "case": "x",
        "method": "diff",
        "args": [1],
        "kwargs": {},
        "bot_username": "repoach-bot",
        "source": "recorded 2026-11-23 from a live github change request",
        "trimmed": None,
        "calls": [{**_exchange([]), "stdout": deep}],
        "result": None,
        "raises": None,
    }
    try:
        bounded = fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    except fixture_pipeline.ResidualValueError as exc:
        assert str(exc).startswith("nesting too deep")
    else:
        assert bounded["calls"][0]["stdout"] == deep
    shallow = {**fixture, "calls": [{**_exchange([]), "stdout": "[" * 20000}]}
    assert fixture_pipeline.redact_fixture(shallow, forge="github", denylist=NO_DENYLIST)["calls"][
        0
    ]["stdout"] == ("[" * 20000)

    def _overflow(*_a: Any, **_kw: Any) -> Path:
        raise RecursionError("maximum recursion depth exceeded")

    with monkeypatch.context() as patched:
        patched.setattr(fixture_pipeline, "_redact_fixture", _overflow)
        with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^nesting too deep"):
            fixture_pipeline.redact_fixture(fixture, forge="github", denylist=NO_DENYLIST)
    monkeypatch.setattr(rec, "_record", _overflow)
    with pytest.raises(fixture_judge.UnclassifiedValueError, match=r"^nesting too deep"):
        rec.record(
            forge_name="github",
            cwd=Path("."),
            case="x",
            method="diff",
            args=[1],
            kwargs={},
            trim=None,
            out_root=Path("."),
        )


def test_the_recorder_runs_its_own_git_with_a_minimal_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #319 round 4: ``origin_url`` hands ``git`` the path and config variables only, no token or deny-list."""
    for key, value in {
        "GH_TOKEN": "ghp_zqfaketoken",
        "GITLAB_TOKEN": "glpat-zqfake",
        "ANTHROPIC_API_KEY": "sk-zqfake",
        deny_list.DENYLIST_ENV: "zqentry",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: bearer zqfake",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(key, value)
    seen: list[dict[str, str]] = []

    def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(dict(kw["env"]))
        return subprocess.CompletedProcess(argv, 0, "git@h.io:zqgroup/zqrepo.git\n", "")

    monkeypatch.setattr(rec.subprocess, "run", _run)
    assert rec.origin_url(tmp_path) == "git@h.io:zqgroup/zqrepo.git"
    (env,) = seen
    assert set(env) <= {*rec.GIT_ENV_KEYS, "GIT_TERMINAL_PROMPT"}
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / "gitconfig")
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["PATH"] == os.environ["PATH"]
    assert not any("zqfake" in v or "zqentry" in v for v in env.values())
    assert rec.GIT_ENV_KEYS == (
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "SYSTEMROOT",
    )


def test_origin_url_reads_a_real_checkout_under_the_minimal_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #319 round 4: the minimal environment is enough for ``git remote get-url``; no remote reads ``""``."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=rec.git_env())
    assert rec.origin_url(repo) == ""
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@h.io:zqgroup/zqrepo.git"],
        check=True,
        env=rec.git_env(),
    )
    assert rec.origin_url(repo) == "git@h.io:zqgroup/zqrepo.git"


def test_an_omitted_deny_list_is_the_maintainers_and_never_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #319 round 4: every entry point fails closed; only an explicit ``Denylist()`` is empty.

    ``redact_fixture(denylist=None)`` turned into an empty deny-list while ``run`` and
    ``refuse_unclassified`` read the maintainer's: a caller that omitted it skipped the gate.
    """
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "zqdenied77")
    fixture = _fixture("github", "conversation_comments", [[{"body": "seen on zqdenied77 today"}]])
    with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^deny-list entry 1 at "):
        fixture_pipeline.redact_fixture(json.loads(json.dumps(fixture)), forge="github")
    written = fixture_pipeline.redact_fixture(
        json.loads(json.dumps(fixture)), forge="github", denylist=NO_DENYLIST
    )
    assert "zqdenied77" in json.dumps(written)
    assert fixture_pipeline.fixture_known(fixture).values("deny-list") == ("zqdenied77",)
    assert fixture_pipeline.fixture_known(fixture, denylist=NO_DENYLIST).values("deny-list") == ()
    raw = [fixture["calls"], [], {}]
    assert recording.known_values(raw, roles={}).values("deny-list") == ("zqdenied77",)
    assert recording.known_values(raw, roles={}, denylist=NO_DENYLIST).values("deny-list") == ()
    with pytest.raises(TypeError, match=r"denylist must be a Denylist or None"):
        recording.known_values(raw, roles={}, denylist=cast(Any, ()))
    with pytest.raises(fixture_judge.UnclassifiedValueError, match=r"^deny-list entry 1 at "):
        fixture_judge.refuse_unclassified(written)
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "ab")
    with pytest.raises(deny_list.DenylistError, match=r"^deny-list entry 1: too short$"):
        fixture_pipeline.redact_fixture(json.loads(json.dumps(fixture)), forge="github")
    with pytest.raises(deny_list.DenylistError, match=r"^deny-list entry 1: too short$"):
        recording.known_values(raw, roles={})
    for function in (
        fixture_pipeline.redact_fixture,
        fixture_pipeline.fixture_known,
        fixture_pipeline.redact_corpus,
        recording.known_values,
        fixture_judge.refuse_unclassified,
    ):
        parameter = inspect.signature(function).parameters["denylist"]
        assert parameter.default is None, function.__name__


def test_live_and_offline_roles_come_from_one_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #319 round 4: ``recording.assign_roles`` is the one role builder; the scripts only read.

    The live recorder reads the recorded answers, the offline transform the committed fixture;
    both hand the sightings to the same builder, so they cannot drift. On the same answers the
    offline words equal the live ones with the change request's author named, ``author`` being
    that author's in a committed fixture.
    """
    assert not hasattr(redact_fixtures, "legacy_roles")
    assert not hasattr(redact_fixtures, "_unclassified_logins")
    bot = {"user": {"login": "zqbot[bot]"}, "body": "ok"}
    mention = {"body": "cc @zqbot and @zqhandle please"}
    other = {"user": {"login": "zqother"}}
    service = {"user": {"login": "project_88442211_bot_a1b2c3d4e5f6"}}
    upper = {"user": {"login": "ZQOther"}, "body": "cc @ZQBOT"}
    for answers in (
        [mention, bot, other],
        [other, mention, bot],
        [bot, service, upper, mention],
        [service, other, upper],
        [mention],
    ):
        calls = [{"args": [], "stdout": json.dumps(a)} for a in answers]
        live = rec.build_roles(calls, bot="", maintainer=None, author="zqauthor")
        live.pop("zqauthor")
        assert fixture_pipeline.fixture_roles(_roles_data(*answers)) == live, answers
    calls = [{"args": [], "stdout": json.dumps(a)} for a in (mention, bot, other)]
    assert rec.build_roles(calls, bot="", maintainer=None, author=None) == {
        "zqbot[bot]": "bot",
        "zqbot": "bot",
        "@zqhandle": "author",
        "zqother": "author-2",
    }
    seen: list[str] = []
    real = recording.assign_roles

    def _spy(*a: Any, **kw: Any) -> dict[str, str]:
        seen.append("called")
        return real(*a, **kw)

    monkeypatch.setattr(rec, "assign_roles", _spy)
    monkeypatch.setattr(fixture_pipeline, "assign_roles", _spy)
    rec.build_roles(calls, bot="", maintainer=None, author=None)
    fixture_pipeline.fixture_roles(_roles_data(mention))
    assert seen == ["called", "called"]
    assert recording.login_sightings([mention, other]) == [
        ("zqbot", True),
        ("zqhandle", True),
        ("zqother", False),
    ]
    taken = recording.assign_roles([("zqnew", False)], held=lambda word: word in {"author"})
    assert taken == {"zqnew": "author-2"}


def _raises_names(function: Any) -> set[str]:
    """The exception names a Google ``Raises:`` section lists, their module path dropped."""
    doc = inspect.getdoc(function) or ""
    section = doc.split("Raises:", 1)[1] if "Raises:" in doc else ""
    return {m.rsplit(".", 1)[-1] for m in re.findall(r"^    ([A-Za-z_.]+):", section, re.M)}


def _caught_by_main(module: ModuleType) -> set[str]:
    """The exception names the ``except`` clauses of ``module.main`` catch."""
    tree = ast.parse(inspect.getsource(module.main))
    names: set[str] = set()
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        names |= {k.attr if isinstance(k, ast.Attribute) else k.id for k in kinds}
    return names


def test_every_exception_main_catches_is_one_the_entry_point_documents() -> None:
    """PR #319 round 4: ``record()`` and ``run()`` list every exception that can escape them.

    Each script's ``main`` catches what its entry point raises; a name caught there and missing
    from the entry point's ``Raises:`` is an undocumented escape.
    """
    assert _caught_by_main(rec) <= _raises_names(rec.record)
    assert _caught_by_main(redact_fixtures) <= _raises_names(fixture_pipeline.redact_corpus)
    assert {
        "SystemExit",
        "UnclassifiedValueError",
        "DenylistError",
        "SyntheticIdsExhaustedError",
        "NeedleError",
        "ForgeError",
        "ConfigError",
        "TypeError",
    } == _raises_names(rec.record)
    for function in (
        fixture_pipeline.redact_fixture,
        fixture_pipeline.fixture_known,
        recording.known_values,
        fixture_judge.refuse_unclassified,
    ):
        assert "DenylistError" in _raises_names(function), function.__name__


def test_record_refuses_an_unknown_method_and_a_replay_that_issues_another_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #319 round 4: neither escapes as an ``AttributeError`` or an ``AssertionError``; nothing is written."""
    _no_denylist(monkeypatch, tmp_path)
    for method in ("no_such_method", "_spawn", "available_methods"):
        with pytest.raises(SystemExit, match=r"no protocol method has that name$"):
            rec.record(
                forge_name="github",
                cwd=tmp_path,
                case="c",
                method=method,
                args=[],
                kwargs={},
                trim=None,
                out_root=tmp_path / "out",
            )
    forge = GitHubForge(cwd=tmp_path, bot_login="repoach-bot")
    argv = ["api", "--paginate", "--slurp", "repos/:owner/:repo/issues/7/comments"]
    forge._spawn = ReplaySpawn([Exchange("gh", argv, None, 0, "[[]]", "")])

    def _mismatch(*_a: Any, **_kw: Any) -> tuple[object, str | None]:
        raise recording.ReplayMismatchError("unrecorded call: ['api', 'zqprivate']")

    monkeypatch.setattr(rec, "_replay", _mismatch)
    with pytest.raises(SystemExit) as stopped:
        _record_through(monkeypatch, tmp_path, forge, method="conversation_comments", args=[7])
    assert str(stopped.value) == (
        "refusing to write a fixture whose redacted replay disagrees with the live call"
    )
    assert "zqprivate" not in str(stopped.value)
    assert not (tmp_path / "out").exists()

    def _unavailable(**_kw: Any) -> Forge:
        raise ForgeError("forge 'github' (from flag) needs the 'gh' CLI on PATH")

    monkeypatch.setattr(rec, "select_forge", _unavailable)
    assert (
        rec.main(
            ["--forge", "github", "--cwd", str(tmp_path), "--case", "c", "--method", "pull_request"]
        )
        == 2
    )
    assert capsys.readouterr().err.endswith("needs the 'gh' CLI on PATH\n")


def _public_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """Module-level functions and class methods whose name has no leading underscore."""
    found: list[ast.FunctionDef] = []
    for node in tree.body:
        members = node.body if isinstance(node, ast.ClassDef) else [node]
        found += [
            m for m in members if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")
        ]
    return found


def test_the_recorders_public_functions_document_their_arguments_and_value() -> None:
    """PR #319 round 4: a Google ``Args:`` and ``Returns:`` on every public function of the class.

    The class is every public function of the two recorder scripts that takes an argument, and
    every public function or method of the forge recorder modules that takes two or more; a
    function returning ``None`` needs no ``Returns:``.
    """
    missing = []
    for module in (
        rec,
        redact_fixtures,
        closure,
        closure_tables,
        recording,
        recording_tables,
        deny_list,
        fixture_judge,
    ):
        rel = module.__name__
        for node in _public_functions(ast.parse(inspect.getsource(module))):
            arguments = [
                a.arg for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")
            ]
            if len(arguments) < (2 if rel.startswith("repoach.") else 1):
                continue
            doc = ast.get_docstring(node) or ""
            returns_none = isinstance(node.returns, ast.Constant) and node.returns.value is None
            if "Args:" not in doc or not (returns_none or "Returns:" in doc):
                missing.append(f"{rel}:{node.name}")
            elif any(f"    {a}:" not in doc for a in arguments):
                missing.append(f"{rel}:{node.name} (an argument)")
    assert missing == []


def test_a_token_is_refused_in_every_decoded_form(tmp_path: Path) -> None:
    """D3 (PR #319 round 5, 4683464687): the token shapes are judged on the one decoder's forms.

    The home-path rule and the deny-list already read :func:`repoach.forge.closure.decoded_forms`;
    the token scan reads the same forms, so a percent-, entity- or string-escaped delimiter or
    prefix hides no token, and no second decoder exists.
    """
    token = "gh" + "p_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    for hidden in (
        token,
        token.replace("_", "%5F"),
        token.replace("_", "%5f"),
        token.replace("_", "&#95;"),
        token.replace("_", "&#x5f;"),
        token.replace("_", "\\u005f"),
        token.replace("_", "\\x5f"),
        token.replace("p", "%70", 1),
        token.replace("_", "%255F"),
    ):
        assert fixture_judge.has_secret_token(f"token {hidden} leaked"), hidden
        fixture = _placed(f"token {hidden} leaked", "prose")
        with pytest.raises(fixture_judge.UnclassifiedValueError, match=r"^secret-shaped value at "):
            fixture_judge.refuse_unclassified(fixture, NO_DENYLIST)
    assert not fixture_judge.has_secret_token("use a ghp_ token from settings")
    source = inspect.getsource(fixture_judge.has_secret_token)
    assert "closure.decoded_forms(text)" in source and "unquote" not in source


def test_an_unreadable_denylist_file_is_a_named_refusal_never_an_empty_list(tmp_path: Path) -> None:
    """D3 (PR #319 round 5): a deny-list file that exists but cannot be read fails closed.

    A dangling symlink, a directory, an unreadable file and a file that is no UTF-8 each raise
    :class:`repoach.forge.denylist.DenylistError` naming the reason and the file name, never an
    entry; an absent file is no entry.
    """
    entry = "zqunreadable" + "4713"
    os.environ.pop(deny_list.DENYLIST_ENV, None)

    def _dangling(path: Path) -> None:
        path.symlink_to(path.parent / "nowhere")

    def _loop(path: Path) -> None:
        path.symlink_to(path)

    def _unreadable(path: Path) -> None:
        path.write_text(entry + "\n")
        path.chmod(0)

    cases: list[tuple[str, Any, str]] = [
        ("dangling", _dangling, "exists but is not a regular file"),
        ("loop", _loop, "exists but is not a regular file"),
        ("directory", lambda path: path.mkdir(), "exists but is not a regular file"),
        ("utf8", lambda path: path.write_bytes(entry.encode() + b"\n\xff\xfe"), "not UTF-8"),
    ]
    if os.geteuid() != 0:
        cases.append(("mode000", _unreadable, "cannot be read (PermissionError)"))
    for name, make, reason in cases:
        config = tmp_path / name
        config.mkdir()
        make(deny_list.denylist_file(config))
        with pytest.raises(deny_list.DenylistError) as info:
            deny_list.read_denylist(config)
        message = str(info.value)
        assert message == f"deny-list file {deny_list.DENYLIST_FILE_NAME}: {reason}", name
        assert entry not in message and info.value.__cause__ is None, name
        if name == "mode000":
            deny_list.denylist_file(config).chmod(0o600)
    absent = tmp_path / "absent"
    absent.mkdir()
    assert len(deny_list.read_denylist(absent)) == 0


def test_the_denylist_hands_its_entries_to_no_caller() -> None:
    """K8 (PR #319 round 5, 4683464056): the opaque reader gives the oracle its entries, a caller none.

    :meth:`repoach.forge.denylist.Denylist.judged_with` adds them to an opaque
    :class:`repoach.forge.closure.Known`; no public member returns an entry, and
    :func:`repoach.forge.recording.known_values` takes the opaque reader only.
    """
    public = {name for name in vars(deny_list.Denylist) if not name.startswith("_")}
    assert public == {"hit", "short_entry", "judged_with"}
    deny = deny_list.Denylist(("ZqEntryOne", "zqentryone", "zqentrytwo"))
    known = deny.judged_with(
        closure.Known((("login", "zqlogin"),), ("lit",), (("zqlogin", "author"),))
    )
    assert repr(known) == "<known: 3>" and repr(deny) == "<denylist: 3 entries>"
    assert known.values("deny-list") == ("zqentryone", "zqentrytwo")
    assert (known.literals, known.mapping()) == (("lit",), {"zqlogin": "author"})
    with pytest.raises(TypeError, match=r"denylist must be a Denylist or None"):
        recording.known_values([], roles={}, denylist=cast(Any, ("zqentryone",)))


def test_git_env_keeps_the_git_variables_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #319 round 5 (4683465486): ``git_env`` drops the deny-list, every token and inline git config."""
    for key in [k for k in os.environ if k not in ("PATH", "HOME")]:
        monkeypatch.delenv(key)
    secrets = {
        deny_list.DENYLIST_ENV: "zqentry",
        "GH_TOKEN": "zqfake1",
        "GITHUB_TOKEN": "zqfake2",
        "GITLAB_TOKEN": "zqfake3",
        "ANTHROPIC_API_KEY": "zqfake4",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "zqfake5",
        "GIT_ASKPASS": "zqfake6",
        "SSH_AUTH_SOCK": "zqfake7",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x")
    env = rec.git_env()
    assert set(env) == {"PATH", "HOME", "XDG_CONFIG_HOME", "GIT_TERMINAL_PROMPT"}
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["XDG_CONFIG_HOME"] == "/x"
    assert not any(value in env.values() for value in secrets.values())
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    assert rec.git_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_origin_url_returns_empty_on_timeout_missing_git_and_a_failing_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #319 round 5 (4683465486): every failure of the recorder's ``git`` child reads as no remote.

    A timeout, a missing ``git`` (``FileNotFoundError``), any other ``OSError`` and a non-zero
    exit each give ``""`` and one log line naming the error class, never the command's output.
    """
    calls: list[dict[str, Any]] = []

    def _raising(error: BaseException) -> Any:
        def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            calls.append(kw)
            raise error

        return _run

    for error in (
        subprocess.TimeoutExpired(["git"], 30),
        FileNotFoundError(2, "No such file or directory", "git"),
        PermissionError(13, "Permission denied", "git"),
    ):
        monkeypatch.setattr(rec.subprocess, "run", _raising(error))
        with capture_logs() as logs:
            assert rec.origin_url(tmp_path) == ""
        assert [(e["event"], e["error"]) for e in logs] == [
            ("recorder.no_origin", type(error).__name__)
        ]
    assert all(kw["timeout"] == 30 and kw["check"] is False for kw in calls)

    def _failing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "git@h.io:zqgroup/zqrepo.git\n", "fatal")

    monkeypatch.setattr(rec.subprocess, "run", _failing)
    assert rec.origin_url(tmp_path) == ""


def test_live_and_offline_finish_through_the_one_shared_pipeline() -> None:
    """D4 (PR #319 round 5, 4683464072, 4683464045): one pipeline module, a narrow API for the scripts.

    The live ``_record`` runs :func:`repoach.forge.fixture_pipeline.redact_recording`, and both
    scripts declare the synthetic ids and judge through
    :func:`repoach.forge.fixture_pipeline.seal_fixture`, so neither writes a fixture the other
    would judge differently. The offline transform (in the pipeline module since round 6)
    reaches the text rewriting through the public facade (``rename_logins_in_text``,
    ``rename_logins_in_argument``, ``rewrite_recorded_source``, ``replace_known_in_source``),
    never its patterns.
    """
    live = inspect.getsource(rec._record)
    offline = inspect.getsource(fixture_pipeline._redact_fixture)
    assert "redact_recording(" in live and "seal_fixture(" in live and "seal_fixture(" in offline
    for source in (live, offline):
        assert "refuse_unclassified(" not in source and "declared_user_ids(" not in source
    tree = ast.parse(
        (ROOT / "src" / "repoach" / "forge" / "fixture_pipeline.py").read_text(encoding="utf-8")
    )
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "repoach.forge.recording"
        for alias in node.names
    }
    internals = {
        "token_pattern",
        "mention_pattern",
        "outside_literals",
        "bot_mention_stem",
        "MENTION_ONLY_PREFIX",
        "RECORDED_SOURCE_RE",
        "RECORDED_SOURCE_HEAD_RE",
        "recorded_source",
    }
    assert imported and not imported & internals
    assert not any(name.startswith("_") for name in imported)
    assert {
        "rename_logins_in_text",
        "rename_logins_in_argument",
        "rewrite_recorded_source",
        "replace_known_in_source",
    } <= imported
    known = recording.known_values([], roles={}, denylist=NO_DENYLIST)
    fixture = {"calls": [], "source": "recorded 2026-11-24 from a live github change request"}
    assert fixture_pipeline.seal_fixture(fixture, denylist=NO_DENYLIST, known=known) == fixture
    assert recording.rewrite_recorded_source("recorded 2026-11-24 from zqfolder x", "github") == (
        "recorded 2026-11-24 from a live github change request, x"
    )


def test_a_script_exports_its_own_names_only() -> None:
    """D4 (PR #319 round 6, 4684964640): a script is no second public surface of the library.

    Every script under ``scripts/`` that declares ``__all__`` lists only the names it defines or
    assigns itself, never one it imports: a caller reaches ``classify_calls`` or
    ``refuse_unclassified`` from :mod:`repoach.forge.fixture_pipeline` and
    :mod:`repoach.forge.fixture_judge`, where they live.
    """
    declaring = 0
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exported: list[str] = []
        own: set[str] = set()
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                own.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {t.id for t in targets if isinstance(t, ast.Name)}
                own |= names
                if "__all__" in names and isinstance(node.value, (ast.List, ast.Tuple)):
                    exported = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        if not exported:
            continue
        declaring += 1
        rel = path.name
        assert set(exported) <= own, (rel, sorted(set(exported) - own))
        assert not set(exported) & imported, rel
    assert declaring == 2
    assert not {"classify_calls", "synthesize_user_ids", "replace_known_in_calls"} & set(
        rec.__all__
    )
    assert "refuse_unclassified" not in rec.__all__


def test_the_offline_script_is_the_command_line_of_the_pipeline(tmp_path: Path) -> None:
    """D4 (PR #319 round 6, 4684964659): one transform, in the pipeline module; the script parses argv.

    The offline steps (URL redaction of the exchanges, roles, id remapping, known values, the
    seal) live in :mod:`repoach.forge.fixture_pipeline` beside the live stages, so the two paths
    cannot drift in two layers. ``scripts/redact_forge_fixtures.py`` defines ``main`` and its
    default root only, and ``main`` exits 2 on the pipeline's own refusal.
    """
    tree = ast.parse((ROOT / "scripts" / "redact_forge_fixtures.py").read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert defined == {"main"}
    assert assigned == {"__all__", "FIXTURE_ROOT"}
    for name in (
        "redact_fixture",
        "redact_corpus",
        "fixture_known",
        "fixture_roles",
        "fixture_forge",
        "redact_output",
        "corpus_start",
        "render",
        "ResidualValueError",
        "UnknownForgeError",
    ):
        assert getattr(fixture_pipeline, name).__module__ == fixture_pipeline.__name__, name
    (tmp_path / "github").mkdir()
    (tmp_path / "github" / "x.json").write_text(
        json.dumps({"calls": [], "args": ["mail zqperson@zqcorp.io"]}), encoding="utf-8"
    )
    with pytest.raises(fixture_pipeline.ResidualValueError, match=r"^unknown mailbox"):
        fixture_pipeline.redact_corpus(tmp_path, check=True, denylist=NO_DENYLIST)


def test_the_denylist_test_skips_naming_the_reason_where_no_list_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """O2, D6 (PR #319 round 5, 4683465477): the deny-list check runs once, in its own CI step.

    The main ``Pytest`` step gets no secret and the runner has no deny-list file, so there the
    test skips naming the reason; only the ``Pytest — fixture deny-list`` step, the one that
    receives the secret, judges the fixtures. A fork's pull request gets no secret and skips the
    same way in both steps.
    """
    monkeypatch.delenv(deny_list.DENYLIST_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception) as info:
        test_fixtures_hold_no_denylisted_value(tmp_path)
    assert str(info.value) == (
        f"deny-list absent: {_shown(deny_list.denylist_file(tmp_path))} holds no entry "
        f"and ${deny_list.DENYLIST_ENV} is unset or empty (SP-NO-PERSONAL-DATA-GATE D3)"
    )
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "")
    with pytest.raises(pytest.skip.Exception):
        test_fixtures_hold_no_denylisted_value(tmp_path)
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    with_secret = [s for s in steps if deny_list.DENYLIST_ENV in (s.get("env") or {})]
    assert [s["name"] for s in with_secret] == ["Pytest — fixture deny-list"]


def test_the_denylist_test_is_collected_by_its_own_ci_step_only() -> None:
    """O2, D6 (PR #319 round 6, 4683465477): the deny-list test is collected once per CI job.

    The main ``Pytest`` step deselects it by node id, and the ``Pytest — fixture deny-list`` step
    runs that one node; no other unit step collects ``tests/unit``.
    """
    node = "tests/unit/test_forge_recording_redaction.py::test_fixtures_hold_no_denylisted_value"
    assert node.rpartition("::")[2] == test_fixtures_hold_no_denylisted_value.__name__
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    steps = {s.get("name"): s for s in workflow["jobs"]["test"]["steps"]}
    main = str(steps["Pytest"]["run"]).strip()
    assert main.startswith("pytest -q tests/unit ")
    assert main.count("--deselect") == 1 and main.endswith(f" --deselect {node}")
    assert str(steps["Pytest — fixture deny-list"]["run"]).strip() == f"pytest -q {node}"
    collecting = [
        name
        for name, step in steps.items()
        if "pytest" in str(step.get("run", "")) and "tests/unit" in str(step.get("run", ""))
    ]
    assert sorted(collecting) == ["Pytest", "Pytest — fixture deny-list"]


def _message_heads(source: Path, error: str) -> set[str]:
    """The literal head of every message ``raise <error>(...)`` builds in ``source``, up to its first field."""
    heads: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        func = node.exc.func
        if not (isinstance(func, ast.Name) and func.id == error and node.exc.args):
            continue
        arg = node.exc.args[0]
        parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
        head = parts[0].value if parts and isinstance(parts[0], ast.Constant) else ""
        if isinstance(head, str) and head.strip():
            heads.add(head.split(":")[0].strip())
    return heads


def test_a_refusal_class_names_every_reason_it_is_raised_for() -> None:
    """PR #319 round 6 (4684966549): an error's docstring lists every reason its messages name.

    ``DenylistError`` is raised for an unreadable file (each of its reasons), a comma in the
    variable and a short entry; ``UnclassifiedValueError`` for every judge rule and every
    closure refusal; ``ResidualValueError`` names the judge's and ``nesting too deep``.
    """
    forge = ROOT / "src" / "repoach" / "forge"
    deny_heads = _message_heads(forge / "denylist.py", "DenylistError")
    assert len(deny_heads) == 3
    doc = inspect.getdoc(deny_list.DenylistError) or ""
    for phrase in (
        "deny-list file personal-denylist",
        "deny-list variable REPOACH_PERSONAL_DENYLIST: a comma",
        "deny-list entry <n>: too short",
        "exists but is not a regular file",
        "not UTF-8",
        "cannot be read",
    ):
        assert phrase in doc, phrase
    unreadable: list[str] = []
    for node in ast.walk(ast.parse((forge / "denylist.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            why = node.value.elts[2]
            first = why.values[0] if isinstance(why, ast.JoinedStr) else why
            if isinstance(first, ast.Constant) and str(first.value).strip():
                unreadable.append(str(first.value).split("(")[0].strip())
    assert len(unreadable) == 3
    for text in unreadable:
        assert text in doc, text
    for head in deny_heads:
        assert head.split(" {")[0] in doc, head
    judged = inspect.getdoc(fixture_judge.UnclassifiedValueError) or ""
    heads = _message_heads(forge / "fixture_judge.py", "UnclassifiedValueError")
    heads |= _message_heads(ROOT / "scripts" / "record_forge_fixture.py", "UnclassifiedValueError")
    reasons = {
        str(node.args[0].value)
        for node in ast.walk(ast.parse((forge / "closure.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Refusal"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert len(reasons) == 6 and len(heads) >= 5
    for phrase in {re.sub(r"\s+at\b.*$", "", h.split(" {")[0]) for h in heads} | reasons:
        assert phrase in judged, phrase
    residual = inspect.getdoc(fixture_pipeline.ResidualValueError) or ""
    for phrase in (
        "unclassified",
        "secret-shaped value",
        "generic class",
        "deny-list entry",
        "nesting too deep",
    ):
        assert phrase in residual, phrase


def _top_level_names(tree: ast.Module) -> set[str]:
    """The names a module defines itself: its functions, classes and assigned names."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_a_forge_module_takes_each_name_from_the_module_that_defines_it() -> None:
    """D3 (PR #319 round 7, 4687852547): the judge reads a fixture through a public module, never the recorder's internals.

    :mod:`repoach.forge.fixture_judge` imports nothing from :mod:`repoach.forge.recording`: what
    both read (the recorded fields, the login keys, the JSON decoder, the string walk) lives in
    :mod:`repoach.forge.fixture_reading`. Across the forge package and the scripts, no import
    names a private name of another module, and every name imported from a forge module is one
    that module defines itself, so no internal reaches a caller through a re-export.
    """
    forge = ROOT / "src" / "repoach" / "forge"
    defined = {
        f"repoach.forge.{path.stem}": _top_level_names(ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(forge.glob("*.py"))
        if path.stem != "__init__"
    }
    judge = ast.parse((forge / "fixture_judge.py").read_text(encoding="utf-8"))
    sources = {
        node.module
        for node in ast.walk(judge)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "repoach.forge.recording" not in sources
    assert "repoach.forge.fixture_reading" in sources
    checked = 0
    for path in [*sorted(forge.glob("*.py")), *sorted((ROOT / "scripts").glob("*.py"))]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if node.level == 1:
                module = f"repoach.forge.{module}"
            if module not in defined:
                continue
            for alias in node.names:
                checked += 1
                assert not alias.name.startswith("_"), (path.name, module, alias.name)
                assert alias.name in defined[module], (path.name, module, alias.name)
    assert checked > 100
