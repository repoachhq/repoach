"""The forge recorder redacts by class at record time (SP-RECORDER-REDACTS-BY-CLASS).

Personal keys and a user object's ``name`` and URLs are dropped, every
``id`` is kept, URLs get a placeholder host and namespace with their
path shape kept, logins become class words in stdout and argv, and the
recorder refuses an unclassified login or a generic personal value
without printing it. The forge readers are replayed on redacted
recordings, and the committed fixtures are a fixed point of the same
redaction (``scripts/redact_forge_fixtures.py``).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.protocol import Forge
from repoach.forge.recording import (
    PERSONAL_KEY_RE,
    Exchange,
    ReplaySpawn,
    UserIdMap,
    classify_logins,
    decode_json_values,
    is_synthetic_user_id,
    load_fixture,
    redact_payload,
    redact_stdout,
    redact_url,
)
from repoach.forge.runner import CommandResult

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
    assert redact_url("https://a.example/o/r/pull/12") == "https://forge.example.com/org/r/pull/12"
    assert (
        redact_url("https://b.example/g/s/r/-/merge_requests/7")
        == "https://forge.example.com/org/r/-/merge_requests/7"
    )
    assert redact_url("https://b.example/-/jobs/5") == "https://forge.example.com/-/jobs/5"
    assert redact_url("https://b.example/g/s/r") == "https://forge.example.com/org/r"
    assert (
        redact_url("https://a.example/o/r/actions/runs/42/job/100")
        == "https://forge.example.com/org/r/actions/runs/42/job/100"
    )
    assert (
        redact_url("https://api.a.example/repos/o/r/issues/comments/9")
        == "https://forge.example.com/repos/org/r/issues/comments/9"
    )
    assert (
        redact_url("https://b.example/api/v4/projects/g%2Fs%2Fr/merge_requests/7/merge")
        == "https://forge.example.com/api/v4/projects/org%2Fr/merge_requests/7/merge"
    )
    assert redact_url("not a url") == ""
    assert redact_payload({"web_url": "not a url", "iid": 7}) == {"iid": 7}
    assert redact_payload({"body": "see https://git.corp.internal/g/r/-/jobs/5."}) == {
        "body": "see https://forge.example.com/org/r/-/jobs/5."
    }
    assert redact_payload({"body": "https://b.example/g/r"}) == {"body": "https://b.example/g/r"}


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
    return fixture, rec.synthesize_user_ids(rec.classify_calls(calls, roles), UserIdMap())


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
    assert pr.url == "https://forge.example.com/org/r/-/merge_requests/283"
    assert pr.author == "author"
    assert handle not in json.dumps(calls) and "Some Person" not in json.dumps(calls)

    _fx, calls = _raw_calls("gitlab", "checks", {0: mr_raw, 1: jobs_raw}, roles)
    checks = _replayed(_build("gitlab", tmp_path, ""), calls, "checks", 283)
    assert [(c.name, c.link) for c in checks] == [
        ("unit:3.11", "https://forge.example.com/org/r/-/jobs/17"),
        ("lint", "https://forge.example.com/org/r/-/jobs/18"),
    ]

    _fx, calls = _raw_calls("gitlab", "ci_status", {0: mr_raw}, roles)
    status = _replayed(_build("gitlab", tmp_path, ""), calls, "ci_status", 283)
    assert [c.link for c in status.checks] == ["https://forge.example.com/org/r/-/pipelines/99"]

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
    assert pr.url == "https://forge.example.com/org/r/pull/200"

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
        ("Test suite (Python 3.11)", "https://forge.example.com/org/r/actions/runs/42/job/100"),
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


def _run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, roles: dict[str, str] | None
) -> int:
    """Run the recorder's CLI against a fake forge, optionally with fixed roles."""
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
    stdout = json.dumps([[{"id": 1, "user": {"login": "q"}, "state": "APPROVED", "body": ""}]])
    assert _run_main(monkeypatch, tmp_path, stdout, {"x": "maintainer"}) == 2
    err = capsys.readouterr()
    assert re.search(r"^unclassified: exchanges\[\d+\]\.stdout.*\.login$", err.err, re.M)
    assert '"q"' not in err.err + err.out and ": q" not in err.err + err.out
    assert not (tmp_path / "out").exists()

    body = "log: /home/someone-x/y failed"
    stdout = json.dumps([[{"id": 1, "user": {"login": "q"}, "state": "APPROVED", "body": body}]])
    assert _run_main(monkeypatch, tmp_path, stdout, {"q": "author"}) == 2
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
                        "login": "q",
                        "name": "Some Person",
                        "html_url": "https://code.example.org/q",
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
    assert '\\"q\\"' not in text and "Some Person" not in text and "code.example.org" not in text
    assert fixture["bot_username"] == "bot"
    assert json.loads(fixture["calls"][0]["stdout"])[0][0]["user"] == {
        "id": 1001,
        "login": "author",
    }
    assert fixture["result"] == [
        {"author": "author", "body": "lgtm", "comments": [], "id": 9, "state": "APPROVED"}
    ]


def test_existing_fixtures_are_a_fixed_point_of_the_redaction() -> None:
    """G4 as amended: the committed fixtures are what the recorder's redaction leaves them."""
    assert redact_fixtures.run(FIXTURES, check=True) == []
    real_host = re.compile(r"https?://([a-z0-9.-]+\.)?(github|gitlab)\.com/(?!en/|ee/)")
    hits = [p.name for p in sorted(FIXTURES.rglob("*.json")) if real_host.search(p.read_text())]
    assert hits == []


def _user_ids(node: object) -> list[object]:
    """The ``id`` of every user object of a decoded fixture."""
    if isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
        return [i for v in decode_json_values(node) or [] for i in _user_ids(v)]
    if isinstance(node, dict):
        found = [i for v in node.values() for i in _user_ids(v)]
        if ("login" in node or "username" in node) and "id" in node:
            found.append(node["id"])
        return found
    if isinstance(node, list):
        return [i for v in node for i in _user_ids(v)]
    return []


def test_fixtures_hold_no_real_user_id() -> None:
    """D1 (0.7): every user id of every fixture is synthetic."""
    loaded = [json.loads(p.read_text()) for p in sorted(FIXTURES.rglob("*.json"))]
    ids = [i for data in loaded for i in _user_ids(data)]
    assert len(ids) >= 50
    assert [i for i in ids if not is_synthetic_user_id(i)] == []


def test_user_ids_map_to_a_stable_sequence() -> None:
    """D1 (0.7): first account 1001, next 1002; a legacy node id shares its account's number."""
    node = base64.b64encode(b"04:User70707").decode()
    ids = UserIdMap()
    out = ids.apply(
        [
            {"user": {"id": 70707, "login": "author"}},
            {"author": {"id": node, "login": "author"}},
            {"user": {"username": "author-2", "id": 777}},
            {"user": {"id": 70707, "login": "author"}},
            {"id": 70707, "name": "check"},
        ]
    )
    assert out == [
        {"user": {"id": 1001, "login": "author"}},
        {"author": {"id": "U_1001", "login": "author"}},
        {"user": {"username": "author-2", "id": 1002}},
        {"user": {"id": 1001, "login": "author"}},
        {"id": 70707, "name": "check"},
    ]
    assert ids.apply_known([{"id": 777, "author": "author-2"}]) == [
        {"id": 1002, "author": "author-2"}
    ]
    kept = UserIdMap(keep_synthetic=True)
    assert kept.apply({"user": {"id": 11, "username": "a"}}) == {
        "user": {"id": 11, "username": "a"}
    }
    assert kept.apply({"user": {"id": 4321987, "username": "a"}}) == {
        "user": {"id": 1001, "username": "a"}
    }
