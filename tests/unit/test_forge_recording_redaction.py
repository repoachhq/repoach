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
    NON_PERSON_LOGINS,
    PERSONAL_KEY_RE,
    SYNTHETIC_IDS_KEY,
    Exchange,
    ReplaySpawn,
    SyntheticIdsExhaustedError,
    UserIdMap,
    classify_logins,
    declared_user_ids,
    decode_json_values,
    is_synthetic_number,
    load_fixture,
    mentions,
    redact_payload,
    redact_stdout,
    redact_url,
    redact_urls_in_text,
    trusted_declaration,
    unredacted_host,
    user_id_key,
    user_id_values,
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


def _denylisted(entries: list[str]) -> list[tuple[str, int]]:
    """``(file, ordinal)`` of every deny-list entry a file under :data:`FIXTURES` holds."""
    patterns = rec.denylist_patterns(entries)
    hits: list[tuple[str, int]] = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        texts = [text]
        if path.suffix == ".json":
            texts += [s for _key, s in rec.fixture_strings(json.loads(text), "")]
        found = {rec.denylist_hit(s, patterns) for s in texts} - {None}
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
    assert entries == ["forge.example.com"]
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
    assert rec.read_denylist() == []
    config.mkdir(parents=True)
    (config / rec.DENYLIST_FILE_NAME).write_text(
        "# the owner's strings\nsomeone-handle\n\n4242424242  # an id\n"
    )
    monkeypatch.setenv(rec.DENYLIST_ENV, "someone-host,someone-handle\n")
    entries = rec.read_denylist()
    assert entries == ["someone-handle", "4242424242", "someone-host"]
    patterns = rec.denylist_patterns(entries)
    assert rec.denylist_hit("ssh someone-host", patterns) == 3
    assert rec.denylist_hit("control-someone-host", patterns) is None
    assert rec.denylist_hit(base64.b64encode(b"04:User4242424242").decode(), patterns) == 2
    assert rec.denylist_hit(_new_node(4242424242), patterns) == 2
    assert rec.denylist_hit(_new_node(4242424243), patterns) is None
    monkeypatch.setenv(rec.DENYLIST_ENV, "abc")
    with pytest.raises(rec.DenylistError, match=r"^deny-list entry 3: too short$"):
        rec.read_denylist()
    (config / rec.DENYLIST_FILE_NAME).unlink()

    body = "deployed on someone-host"
    stdout = json.dumps([[{"id": 1, "user": {"login": "q"}, "state": "APPROVED", "body": body}]])
    assert _run_main(monkeypatch, tmp_path, stdout, {"q": "author"}, "someone-host") == 2
    err = capsys.readouterr()
    assert re.search(r"^deny-list entry 1 at exchanges\[\d+\]\.stdout.*\.body$", err.err, re.M)
    assert "someone-host" not in err.err + err.out
    assert _run_main(monkeypatch, tmp_path, stdout, {"q": "author"}, "abc") == 2
    err = capsys.readouterr()
    assert err.err.strip() == "deny-list entry 1: too short"
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
    stdout = json.dumps([[{"id": 9, "user": {"login": "q"}, "state": "APPROVED", "body": ""}]])
    monkeypatch.setattr(rec, "recorded_source", lambda *_a: "recorded from /home/someone-x/y")
    assert _run_main(monkeypatch, tmp_path, stdout, {"q": "author"}) == 2
    err = capsys.readouterr()
    assert err.err.strip() == "generic class home_path at source"
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
        [[{"id": 9, "user": {"login": "q"}, "state": "APPROVED", "body": "ping @x-handle"}]]
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
    payload = {"body": body, "diff": diff, "user": {"login": "q", "id": 5}}
    calls = [{"args": ["api", "x"], "stdout": json.dumps(payload), "stderr": "", "returncode": 0}]
    roles = rec.build_roles(calls, bot="", maintainer=None, author="q")
    assert roles == {"q": "author", "@someone-x": "author-2"}
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

    stdout = json.dumps([[{"id": 9, "user": {"login": "q"}, "state": "APPROVED", "body": body}]])
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
        rec.refuse_unclassified({"source": "s", "calls": calls}, denylist=[])

        raw = [{"args": [], "stdout": json.dumps(answer), "stderr": ""}]
        unredacted = rec.classify_calls(
            raw, rec.build_roles(raw, bot="", maintainer=None, author=None)
        )
        with pytest.raises(rec.UnclassifiedValueError, match=r"^unredacted host at exchanges\[0\]"):
            rec.refuse_unclassified({"source": "s", "calls": unredacted}, denylist=[])

    fixture = {"forge": "gitlab", "calls": [_exchange({"body": "clone git@gitlab.com:g/r.git"})]}
    out = redact_fixtures.redact_fixture(fixture, forge="gitlab")
    assert (
        json.loads(out["calls"][0]["stdout"])["body"]
        == "clone git@forge.example.com:org/project.git"
    )
