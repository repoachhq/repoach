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

import base64
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

from repoach.forge import recording
from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import Forge
from repoach.forge.recording import (
    NON_PERSON_LOGINS,
    PERSONAL_KEY_RE,
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
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
    decode_json_values,
    fixture_strings,
    is_synthetic_number,
    load_fixture,
    mention_pattern,
    mentions,
    needles,
    project_id_values,
    redact_payload,
    redact_stdout,
    redact_url,
    redact_urls_in_text,
    residual,
    token_pattern,
    trusted_declaration,
    unredacted_host,
    user_id_key,
    user_id_values,
)
from repoach.forge.runner import CommandResult
from tests.child_pytest import child_pytest_argv

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "forge"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rec = _load("record_forge_fixture")
redact_fixtures = _load("redact_forge_fixtures")


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
    classified = rec.classify_calls(calls, roles)
    return fixture, rec.synthesize_user_ids(classified, UserIdMap(), forge=forge_name)


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


def _fake_forge(stdout: str, tmp_path: Path) -> Forge:
    """A GitHub forge, bot ``review-agent-x``, whose every call answers ``stdout``."""
    forge = _build("github", tmp_path, "review-agent-x")

    def _spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr="", argv=list(argv))

    cast(Any, forge)._spawn = _spawn
    return forge


def _no_denylist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the deny-list at an empty config directory and clear its variable; return the directory."""
    config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv(rec.DENYLIST_ENV, raising=False)
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
        monkeypatch.setenv(rec.DENYLIST_ENV, denylist)
    monkeypatch.setattr(rec, "select_forge", lambda **_kw: _fake_forge(stdout, tmp_path))
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
    for text, rule in ((f"ip {address} here", "public_ipv4"), (f"mail {mailbox}", "mailbox")):
        assert rec.generic_class(text) == rule
    for text in (
        "v4.2.1.3",
        "pkg==4.2.1.3",
        "10.0.0.1",
        "203.0.113.9",
        "t@example.com",
        "git@example.com:org/r.git",
        "1+noreply@users.example.org",
        "/home/<x>/",
    ):
        assert rec.generic_class(text) is None


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
    assert redact_fixtures.run(FIXTURES, check=True) == []
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
    entries = rec.read_denylist(real_user_config_dir)
    if not entries:
        pytest.skip(
            f"deny-list absent: {_shown(rec.denylist_file(real_user_config_dir))} holds no entry "
            f"and ${rec.DENYLIST_ENV} is unset or empty (SP-NO-PERSONAL-DATA-GATE D3)"
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
    assert real_user_config_dir != rec.user_config_dir()
    assert not real_user_config_dir.is_relative_to(tmp_path_factory.getbasetemp())
    parameters = inspect.signature(test_fixtures_hold_no_denylisted_value).parameters
    assert list(parameters) == ["real_user_config_dir"]
    source = inspect.getsource(test_fixtures_hold_no_denylisted_value)
    assert source.count("read_denylist(real_user_config_dir)") == 1
    assert source.count("denylist_file(real_user_config_dir)") == 1

    monkeypatch.delenv(rec.DENYLIST_ENV, raising=False)
    config = tmp_path / "config" / "repoach"
    config.mkdir(parents=True)
    rec.denylist_file(config).write_text(
        "# a string every redacted fixture holds\nforge.example.com\n"
    )
    entries = rec.read_denylist(config)
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
    assert len(rec.read_denylist()) == 0
    config.mkdir(parents=True)
    (config / rec.DENYLIST_FILE_NAME).write_text(
        "# the owner's strings\nsomeone-handle\n\n4242424242  # an id\n"
    )
    monkeypatch.setenv(rec.DENYLIST_ENV, "someone-host,someone-handle\n")
    denylist = rec.read_denylist()
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
    monkeypatch.setenv(rec.DENYLIST_ENV, "abc")
    with pytest.raises(rec.DenylistError, match=r"^deny-list entry 3: too short$"):
        rec.read_denylist()
    (config / rec.DENYLIST_FILE_NAME).unlink()

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
        assert redact_fixtures.redact_fixture({"source": before}, forge="gitlab")["source"] == after
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
    out = redact_fixtures.redact_fixture(fixture, forge="github")
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
    out = redact_fixtures.redact_fixture(fixture, forge="github")
    logins = re.findall(r'"(?:login|username)": "([^"]+)"', out["calls"][0]["stdout"])
    assert out["bot_username"] == "repoach-bot"
    assert logins == ["github-actions[bot]", "author-2", "service", "bot"]
    mine = {**fixture, "bot_username": "my-review-bot"}
    assert redact_fixtures.redact_fixture(mine, forge="github")["bot_username"] == "bot"


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
    redact_fixtures.run(root, check=False)
    assert redact_fixtures.run(root, check=True) == []
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
    approvals = {"approved_by": [{"user": {"username": "author-2", "id": raw}}]}
    out = _corpus(
        tmp_path,
        "c",
        {
            "github/c_reviews.json": _fixture(
                "github",
                "reviews",
                [[{"id": raw, "user": {"login": "author", "id": raw}, "state": "APPROVED"}]],
                [{"author": "author", "id": raw, "state": "APPROVED"}],
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
                [{"id": raw, "name": "unit"}],
            ),
        },
    )
    assert out["github/c_reviews.json"]["result"] == [
        {"author": "author", "id": raw, "state": "APPROVED"}
    ]
    assert user_id_values(out["github/c_reviews.json"]) == [1001]
    assert out["gitlab/a_reviews.json"]["result"] == [
        {"author": "author-2", "id": 1002, "state": "APPROVED"}
    ]
    assert out["gitlab/b_checks.json"]["result"] == [{"id": raw, "name": "unit"}]
    assert user_id_values(out["gitlab/b_checks.json"]) == [1002]


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
    out = redact_fixtures.redact_fixture(fixture, forge="gitlab")
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
    out = json.loads(rec.classify_calls(calls, roles)[0]["stdout"])
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

    fixture = {
        "forge": "github",
        "args": ["repos/:owner/:repo/pulls/someone-x"],
        "calls": [_exchange([{"user": {"login": "author"}, "body": body, "diff": diff}])],
    }
    redacted = redact_fixtures.redact_fixture(fixture, forge="github")
    answer = json.loads(redacted["calls"][0]["stdout"])[0]
    assert (
        answer["body"]
        == "use @pytest.fixture here, and see @dataclass; cc @author-2. mail a@b.example"
    )
    assert answer["diff"] == diff
    assert redacted["args"] == ["repos/:owner/:repo/pulls/someone-x"]

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
        calls = rec.classify_calls(recorded, built)
        assert "x-handle" not in json.dumps(calls) and "github.com" not in json.dumps(calls)
        rec.refuse_unclassified({"source": "s", "calls": calls}, denylist=rec.Denylist())

        raw = [{"args": [], "stdout": json.dumps(answer), "stderr": ""}]
        unredacted = rec.classify_calls(
            raw, rec.build_roles(raw, bot="", maintainer=None, author=None)
        )
        with pytest.raises(rec.UnclassifiedValueError, match=r"^unredacted host at exchanges\[0\]"):
            rec.refuse_unclassified({"source": "s", "calls": unredacted}, denylist=rec.Denylist())

    fixture = {"forge": "gitlab", "calls": [_exchange({"body": "clone git@gitlab.com:g/r.git"})]}
    out = redact_fixtures.redact_fixture(fixture, forge="gitlab")
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
    out = json.loads(rec.classify_calls(calls, roles)[0]["stdout"])
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
    assert step["env"] == {rec.DENYLIST_ENV: secret}
    assert step["run"] == "pytest -q tests/unit/test_forge_recording_redaction.py -k denylisted"
    selected = [n for n in globals() if n.startswith("test_") and "denylisted" in n]
    assert selected == ["test_fixtures_hold_no_denylisted_value"]
    (unit,) = [s for s in steps if s.get("name") == "Pytest"]
    assert "env" not in unit
    for each in (step, unit):
        assert not re.search(r"set -[a-z]*x|\bxtrace\b|\becho\b|printenv|\benv\b", each["run"])


def test_one_token_boundary_for_replacer_mention_and_denylist() -> None:
    """I1 (0.10): the login replacer, the ``@`` replacer and the deny-list are one pattern modulo the word."""
    assert TOKEN_CHARS == "A-Za-z0-9"
    for word in ("jdoe", "a.b-c", "x[bot]"):
        expected = token_pattern(word)
        assert expected.flags & re.I
        for pattern in (recording._login_pattern(word), rec.denylist_patterns([word])[0]):
            assert (pattern.pattern, pattern.flags) == (expected.pattern, expected.flags)
        at = token_pattern(f"@{word}")
        assert (mention_pattern(word).pattern, mention_pattern(word).flags) == (
            at.pattern,
            at.flags,
        )
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


def _sample_needles() -> recording.Needles:
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


def _through_the_pipeline(fixture: dict[str, Any], found: recording.Needles) -> dict[str, Any]:
    """``fixture`` as :func:`rec.record` writes it: redacted, classified, ids synthetic."""
    calls = [dict(c) for c in fixture["calls"]]
    for call in calls:
        call["stdout"] = redact_stdout(call["stdout"])
    known = {**found.mapping(), **_ROLES}
    calls = rec.synthesize_user_ids(rec.classify_calls(calls, known), UserIdMap(), forge="github")
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
    assert {kind for kind, _v in found.entries} == set(recording.NEEDLE_KINDS)
    missed: list[tuple[str, str]] = []
    for text, _needle, _kind in _samples():
        for position in _POSITIONS:
            fixture = _placed(text, position)
            if residual(fixture, found) is None:
                missed.append((position, text))
                continue
            with pytest.raises(rec.UnclassifiedValueError):
                rec.refuse_unclassified(fixture, rec.Denylist(), found)
    assert missed == []
    for needle, _kind in _TEXT_NEEDLES:
        for neighbour in _INSIDE_A_TOKEN:
            fixture = _placed(neighbour.format(n=needle), "prose")
            assert residual(fixture, found) is None, neighbour
    folded = needles([], roles={}, denylist=("strasse-host",))
    assert residual(_placed("deployed on Straße-host", "prose"), folded) == (
        "deny-list",
        1,
        "exchanges[0].stdout.body",
    )
    left = residual(_placed(f"merge into {_HANDLE}", "prose"), found)
    assert left == ("login", 1, "exchanges[0].stdout.body")
    with pytest.raises(rec.UnclassifiedValueError, match=r"^residual login 1 at exchanges\[0\]"):
        rec.refuse_unclassified(_placed(f"merge into {_HANDLE}", "prose"), rec.Denylist(), found)


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
    assert repr(found) == f"<needles: {len(found)}>"
    assert found.mapping() == {
        "some-owner": "org",
        "repo-x": "project",
        "api.github.com": "forge.example.com",
        "github.com": "forge.example.com",
        "gitlab.com": "forge.example.com",
    }
    assert recording.NEEDLE_MIN_LENGTH == rec.DENYLIST_MIN_LENGTH == 4


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
    (recorded,) = rec.synthesize_user_ids(calls, UserIdMap(), forge="gitlab")
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
    assert rec.generic_class(redact_urls_in_text("mail jdoe@corp.io:8080/x")) is None
    assert redact_urls_in_text("host t@example.com:8080") == "host t@example.com:8080"


@pytest.mark.parametrize(
    "error",
    [
        rec.UnclassifiedValueError,
        rec.DenylistError,
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
        redact_fixtures.UnknownForgeError,
        SyntheticIdsExhaustedError,
        NeedleError,
        redact_fixtures.ResidualValueError,
        rec.DenylistError,
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

    monkeypatch.setattr(redact_fixtures, "run", _refuse)
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
        ("unknown", "zzz/b.json", {"calls": []}, redact_fixtures.UnknownForgeError),
        (
            "residual",
            "github/b.json",
            _fixture(
                "github",
                "pull_request",
                [{"author": {"login": "someone-y"}, "body": "by someone-y"}],
            ),
            redact_fixtures.ResidualValueError,
        ),
    )
    for name, rel, second, error in seconds:
        root = tmp_path / name
        for path, data in (("github/a.json", first), (rel, second)):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(json.dumps(data))
        before = (root / "github" / "a.json").read_bytes()
        with pytest.raises(error):
            redact_fixtures.run(root, check=False, denylist=rec.Denylist())
        assert (root / "github" / "a.json").read_bytes() == before
    with pytest.raises(
        redact_fixtures.ResidualValueError,
        match=r"^residual login 1 at github/b\.json:exchanges\[0\]\.stdout\.body$",
    ):
        redact_fixtures.run(tmp_path / "residual", check=True, denylist=rec.Denylist())


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
            "CLI_PLACEHOLDER_RE",
            "CLASS_WORD_RE",
            "_NODE_IN_TEXT_RE",
            "_JSON_U_ESCAPE_RE",
            "_API_PROJECT_RE",
            "RECORDED_SOURCE_HEAD_RE",
        }
    ),
    "record_forge_fixture": frozenset(
        {"HOME_PATH_RE", "IPV4_RE", "IPV4_VERSION_BEFORE_RE", "MAILBOX_RE", "URL_USERINFO_RE"}
    ),
    "redact_forge_fixtures": frozenset(),
}
"""Module patterns case-sensitive by nature: encodings, the placeholders the redaction writes, the tools' literals, and the gate's copied regexes."""


def test_case_insensitive_patterns_are_flagged() -> None:
    """I2 (0.10): every module-level recogniser of a scheme, host or login is ``re.I``; the exceptions are named."""
    for module in (recording, rec, redact_fixtures):
        name = module.__name__.rsplit(".", 1)[-1]
        imported = {id(v) for v in vars(recording).values()} if module is not recording else set()
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
    env.pop(rec.DENYLIST_ENV, None)
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
def build(**_kw):
    binary = tmp / "gh"
    binary.write_text("")
    forge = GitHubForge(cwd=tmp, gh_path=str(binary), bot_login="review-agent-x")
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


def _hits(denylist, corpus):
    return [denylist.hit(line) for line in corpus.read_text().splitlines()]


def test_leak():
    denylist = rec.read_denylist(Path({config!r}))
    found = rec.needles([], roles={{}}, denylist=denylist.needle_values())
    assert len(found) and _hits(denylist, Path({corpus!r})) == []
"""


def test_denylist_entries_never_reach_a_local_or_a_repr(tmp_path: Path) -> None:
    """K8 (0.10): a failing deny-list check under ``pytest -l`` names no entry; the reader returns an opaque value."""
    marker = "leakmarker-" + "4711"
    config = tmp_path / "config"
    config.mkdir()
    rec.denylist_file(config).write_text(f"{marker}\n")
    corpus = tmp_path / "corpus.txt"
    corpus.write_text(f"held {marker} here\n")
    assert repr(rec.read_denylist(config)) == "<denylist: 1 entries>"
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


def _one_call(stdout: object, args: list[str] | None = None) -> list[dict[str, Any]]:
    """The raw exchanges of one call answering ``stdout`` as JSON (or as text when it is a string)."""
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return [
        {"args": args or ["api", "x"], "returncode": 0, "stdin": None, "stderr": "", "stdout": text}
    ]


def _kinds(found: recording.Needles, kind: str) -> list[str]:
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
    out = json.loads(rec.classify_calls(calls, roles)[0]["stdout"])
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
    """R3, R6 (0.11): the remote's path, the argv's API path and the instance's URLs give segments; a public third party does not."""
    url = "https://git.corp-x.io/grp-x/proj-x/-/merge_requests/3"
    raw = [_one_call({"web_url": url, "body": "see https://git.other-x.io/third-grp/third-proj"})]
    without_remote = needles(raw, roles={})
    assert _kinds(without_remote, "segment") == ["grp-x", "proj-x", "third-grp", "third-proj"]
    on_instance = needles(raw, roles={}, remote="git@git.corp-x.io:grp-x/proj-x.git")
    assert _kinds(on_instance, "segment") == ["grp-x", "proj-x"]
    assert "git.corp-x.io" in _kinds(on_instance, "host")
    alias = needles([_one_call({"body": "ok"})], roles={}, remote="corpalias:grp-y/proj-y.git")
    assert _kinds(alias, "segment") == ["grp-y", "proj-y"]
    assert "corpalias" not in _kinds(alias, "host")
    other = needles(raw, roles={}, remote="git@git.third-x.io:grp-z/proj-z.git")
    assert _kinds(other, "segment") == ["grp-z", "proj-z"]
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


def test_hosts_are_found_without_the_redactors_regexes() -> None:
    """I3 (0.11): :func:`locations` reads schemeless and scp hosts; file names and decorators are not hosts."""
    seen = recording.locations(
        "hosted at gitlab.corp-x.io/grp-x/proj-x, clone u@git.corp-x.io:g/r.git, "
        "port h.corp-x.io:8080/p and //cdn.corp-x.io/a, https://lone.corp-x.io and u@mail.corp-x.io "
        "not src/setup.py, dir/setup.py/x, setup.py, test_x.py:12: or @pytest.fixture"
    )
    assert [(p.host, p.segments) for p in seen] == [
        ("gitlab.corp-x.io", ("grp-x", "proj-x")),
        ("git.corp-x.io", ("g", "r.git")),
        ("h.corp-x.io", ("p",)),
        ("cdn.corp-x.io", ("a",)),
        ("lone.corp-x.io", ()),
        ("mail.corp-x.io", ()),
    ]
    assert recording.remote_location("") == (None, ())
    assert recording.remote_location("ssh://u@H.corp-x.io:22/g/r.git") == (
        "h.corp-x.io",
        ("g", "r.git"),
    )
    assert recording.is_public_forge_host("objects.githubusercontent.com")
    assert not recording.is_public_forge_host("notgithub.com")


def test_node_ids_inside_longer_strings_are_judged_by_their_number() -> None:
    """R4 (0.11): ``id=<node>``, ``?node=<node>`` and ``see <node> here`` name the account as ``<node>`` does."""
    found = needles([_one_call({"user": {"login": "q-handle", "id": _ID}})], roles={})
    legacy = base64.b64encode(f"04:User{_ID}".encode()).decode()
    for text in (
        f"id={legacy}",
        f"?node={_new_node(_ID)}",
        f"see {legacy} here",
        f"see {_new_node(_ID)}-patch here",
        f"x{legacy}",
    ):
        left = residual(_placed(text, "prose"), found)
        assert left is not None and left[0] == "id", text
    assert recording.embedded_id_numbers(f"a {_new_node(_ID + 1)} b") == [str(_ID + 1)]
    assert recording.embedded_id_numbers("U_1001 and plainwords1234") == ["1001"]


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
    assert "body" in recording.CODE_KEYS and "head_ref" in recording.CODE_KEYS


def test_the_recorders_constant_fields_are_judged_by_the_denylist_only() -> None:
    """R6 (0.11): ``forge``, ``method``, ``case`` and the ``source`` head are the recorder's words, not a leak."""
    found = needles([], roles={}, remote="git@github.com:github/docs.git", denylist=("conv-case",))
    assert "github" in _kinds(found, "segment")
    fixture = _placed("ok", "prose")
    fixture.update(
        forge="github",
        method="conversation_comments",
        case="github",
        source=recording.recorded_source("2026-11-18", "github"),
    )
    assert residual(fixture, found) is None
    fixture["source"] = recording.recorded_source("2026-11-18", "github") + ", docs trimmed"
    assert residual(fixture, found) == ("segment", 2, "source")
    fixture["source"] = recording.recorded_source("2026-11-18", "github")
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
    """D3 (0.10): ``:owner``, ``:repo``, ``:id`` and ``/dev/null`` are the tools' literals, never a login."""
    found = needles([], roles={"owner": "author", "null": "author-2"}, min_length=1)
    fixture = _placed("ok", "prose")
    fixture["calls"][0]["args"] = ["api", "repos/:owner/:repo/pulls", "--- /dev/null"]
    assert residual(fixture, found) is None
    fixture["calls"][0]["args"] = ["api", "repos/owner/x"]
    assert residual(fixture, found) == ("login", 1, "exchanges[0].args[1]")
    cited = _placed("per SP-OWNER-SETTLES-A-FINDING, ask the owner", "prose")
    assert classify_text("per SP-OWNER-SETTLES-A-FINDING, owner", {"owner": "author"}) == (
        "per SP-OWNER-SETTLES-A-FINDING, author"
    )
    assert residual(cited, found) == ("login", 1, "exchanges[0].stdout.body")
    cited = _placed("per SP-OWNER-SETTLES-A-FINDING", "prose")
    assert residual(cited, found) is None
    assert residual(_placed("per sp-owner-settles", "prose"), found) is not None
    deny = needles([], roles={}, denylist=("secretword", "repoach-bot"))
    for text in ("per SP-SECRETWORD-X", "by repoach-bot"):
        assert residual(_placed(text, "prose"), deny) is not None, text
    entries = rec.Denylist(("secretword",))
    assert entries.hit("per SP-SECRETWORD-X") == 1


def test_the_offline_needles_accept_a_short_hand_written_login() -> None:
    """D4 (0.10): the committed corpus may hold a three-letter placeholder login; the offline needles judge it."""
    data = _fixture("github", "reviews", [[{"user": {"login": "bob"}, "body": "hi"}]])
    found = redact_fixtures.fixture_needles(data)
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


def test_the_oracle_judges_encoded_and_lookalike_forms() -> None:
    """I2, I3 (0.11): percent, double percent, entities, ``\\u`` escapes, full-width, invisible and lookalike letters."""
    found = needles([], roles={"qhandle": "author"})
    full = "".join(chr(ord(c) + 0xFEE0) for c in "qhandle")
    for text in (
        "qhandle%41",
        "".join(f"%{ord(c):02X}" for c in "qhandle"),
        "".join(f"%25{ord(c):02X}" for c in "qhandle"),
        "&#113;handle",
        "&#x71;handle",
        "\\u0071handle",
        full,
        "qha​ndle",
        "qha­ndle",
        "qh\u0430ndle",
    ):
        assert residual(_placed(text, "prose"), found) == (
            "login",
            1,
            "exchanges[0].stdout.body",
        ), text
