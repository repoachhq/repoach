"""Permanent regression corpus promoted from verifier rounds 5, 6 and 7 (SP-RECORDER-REDACTS-BY-CLASS 0.14).

Every realistic sample from the round-5, round-6 and round-7 verification
sweeps lives here as a table-driven test with fake values only:

- Planted-leak rows: realistic exposures (mentions, short-stem bots,
  branches, URLs, userinfo credentials, Markdown delimiters, prose
  punctuation, GitLab API project ids, noreply and personal mailboxes,
  home paths, diffs, stderr, stdin, args, kwargs, case, dropped keys,
  node ids) must either be refused or written with zero planted markers
  surviving.
- Ordinary public PR rows: realistic GitHub and GitLab comments, code
  blocks, decorators, scoped packages, bundle paths, code attribute
  divisions, tracebacks, CI logs, Dependabot/Renovate templates,
  Co-authored-by trailers, public reference hosts, mixed-case docs hosts,
  trailing punctuation, and GitLab API documentation examples must write
  cleanly without refusal or unintended corruption.
- Offline transform rows: mixed-case docs hosts, short-stem bots,
  placeholder-stem bots, uppercase ``[BOT]``, ``kwargs``, and system
  noreply / ``actions@github.com`` mailboxes must transform cleanly,
  pass the closure oracle, and be idempotent.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from repoach.forge import recording as rec_mod

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> ModuleType:
    """Load a script under ``scripts/`` once, reusing any already-loaded module."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REC = _load_script("record_forge_fixture")
OFF = _load_script("redact_forge_fixtures")

FAKE_LOGIN = "zqxjdoe7"
FAKE_LOGIN_2 = "zqreviewer"
FAKE_HOST = "git.zqcorp.io"
FAKE_NS = "zqgroup"
FAKE_REPO = "zqmainrepo"
FAKE_UID = 74928163
FAKE_PID = 88442211
FAKE_DENY = "zqdenyval"
FAKE_DENY_ID = "91827364"


def _node_legacy(number: int) -> str:
    """Encode ``number`` as a legacy GitHub ``04:User<n>`` base64 node id."""
    return base64.b64encode(f"04:User{number}".encode()).decode()


def _node_new(number: int) -> str:
    """Encode ``number`` as a current-format ``U_`` MessagePack node id."""
    raw = b"\x92\x00\xce" + number.to_bytes(4, "big")
    return "U_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


FAKE_LEGACY_NODE = _node_legacy(FAKE_UID)
FAKE_NEW_NODE = _node_new(FAKE_UID)
FAKE_DENY_LEGACY = _node_legacy(int(FAKE_DENY_ID))
FAKE_DENY_NEW = _node_new(int(FAKE_DENY_ID))


@dataclass(frozen=True)
class PipelineInput:
    """Inputs for one synthetic recording run through the live redaction pipeline."""

    forge: str = "github"
    body: str = "Looks good to me."
    title: str | None = None
    login: str = FAKE_LOGIN
    uid: int = FAKE_UID
    ns: str = FAKE_NS
    repo: str = FAKE_REPO
    remote: str = f"git@github.com:{FAKE_NS}/{FAKE_REPO}.git"
    url: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    argv_extra: tuple[str, ...] = ()
    api_path: str | None = None
    stderr: str = ""
    stdin: str | None = None
    case: str = "sample"
    extra_item: dict[str, Any] = field(default_factory=dict)
    extra_items: tuple[dict[str, Any], ...] = ()
    raw_text_stdout: str | None = None
    top_args: tuple[Any, ...] = (42,)
    top_kwargs: dict[str, Any] = field(default_factory=dict)
    author: str | None = None
    bot: str = "repoach-bot"
    deny: tuple[str, ...] = (FAKE_DENY, FAKE_DENY_ID)


def _run_pipeline(inp: PipelineInput) -> tuple[str, str, dict[str, Any] | None]:
    """Run the recorder's exact redaction, synthesis and refusal pipeline without live CLI calls."""
    if inp.raw_text_stdout is not None:
        raw_calls: list[dict[str, Any]] = [
            {
                "program": "gh" if inp.forge == "github" else "glab",
                "args": ["pr", "diff", "42", *inp.argv_extra],
                "stdin": inp.stdin,
                "returncode": 0,
                "stdout": inp.raw_text_stdout,
                "stderr": inp.stderr,
            }
        ]
    else:
        user_key = "login" if inp.forge == "github" else "username"
        item: dict[str, Any] = {
            "user" if inp.forge == "github" else "author": {user_key: inp.login, "id": inp.uid},
            "body": inp.body,
            "html_url" if inp.forge == "github" else "web_url": inp.url
            or (
                f"https://github.com/{inp.ns}/{inp.repo}/pull/42#issuecomment-100"
                if inp.forge == "github"
                else f"https://{FAKE_HOST}/{inp.ns}/{inp.repo}/-/merge_requests/42#note_100"
            ),
        }
        if inp.title is not None:
            item["title"] = inp.title
        if inp.head_ref is not None:
            item["headRefName" if inp.forge == "github" else "source_branch"] = inp.head_ref
        if inp.base_ref is not None:
            item["baseRefName" if inp.forge == "github" else "target_branch"] = inp.base_ref
        if inp.extra_item:
            item.update(copy.deepcopy(inp.extra_item))
        items = [item, *(copy.deepcopy(x) for x in inp.extra_items)]
        default_api = (
            "repos/:owner/:repo/issues/42/comments"
            if inp.forge == "github"
            else "projects/:id/merge_requests/42/notes"
        )
        raw_calls = [
            {
                "program": "gh" if inp.forge == "github" else "glab",
                "args": ["api", inp.api_path or default_api, *inp.argv_extra],
                "stdin": inp.stdin,
                "returncode": 0,
                "stdout": json.dumps(items),
                "stderr": inp.stderr,
            }
        ]
    args = copy.deepcopy(list(inp.top_args))
    kwargs = copy.deepcopy(dict(inp.top_kwargs))
    raw: list[object] = [
        copy.deepcopy(raw_calls),
        copy.deepcopy(args),
        copy.deepcopy(kwargs),
        inp.remote,
    ]
    red_calls = [{**c, "stdout": rec_mod.redact_stdout(c["stdout"])} for c in raw_calls]
    roles = REC.build_roles(
        red_calls,
        bot=inp.bot,
        maintainer=None,
        author=inp.author,
        also=rec_mod.raw_logins(raw),
    )
    deny = REC.Denylist(inp.deny)
    try:
        found = rec_mod.needles(raw, roles=roles, remote=inp.remote, denylist=deny.needle_values())
    except rec_mod.NeedleError as exc:
        return "NEEDLE_ERR", str(exc), None
    known = {**found.mapping(), **roles}
    red_calls = REC.classify_calls(red_calls, known)
    projects = rec_mod.UserIdMap.for_projects()
    red_calls = REC.synthesize_user_ids(
        red_calls, rec_mod.UserIdMap(), forge=inp.forge, projects=projects
    )
    args = list(
        cast(
            list[Any],
            projects.apply_projects(
                rec_mod.classify_logins(rec_mod.redact_urls_in_value(args), known),
                forge=inp.forge,
            ),
        )
    )
    kwargs = dict(
        cast(
            dict[str, Any],
            projects.apply_projects(
                rec_mod.classify_logins(rec_mod.redact_urls_in_value(kwargs), known),
                forge=inp.forge,
            ),
        )
    )
    fixture: dict[str, Any] = {
        "forge": inp.forge,
        "case": inp.case,
        "method": "conversation_comments" if inp.raw_text_stdout is None else "diff",
        "args": args,
        "kwargs": kwargs,
        "bot_username": roles.get(inp.bot, inp.bot),
        "source": rec_mod.recorded_source("2026-11-19", inp.forge),
        "trimmed": None,
        "calls": red_calls,
        "result": None,
        "raises": None,
    }
    declared = rec_mod.declared_user_ids(red_calls)
    if declared:
        fixture[rec_mod.SYNTHETIC_IDS_KEY] = declared
    declared_p = rec_mod.declared_project_ids(fixture)
    if declared_p:
        fixture[rec_mod.SYNTHETIC_PROJECT_IDS_KEY] = declared_p
    try:
        REC.refuse_unclassified(fixture, deny, found)
    except (
        REC.UnclassifiedValueError,
        REC.DenylistError,
        rec_mod.SyntheticIdsExhaustedError,
    ) as exc:
        return "REFUSED", str(exc), None
    return "WRITTEN", "", fixture


GL_REMOTE = f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git"

PLANTED_CORPUS: tuple[tuple[str, PipelineInput, tuple[str, ...]], ...] = (
    (
        "mention_known_and_unknown_gh",
        PipelineInput(body=f"Thanks @{FAKE_LOGIN}, cc @{FAKE_LOGIN_2} for review."),
        (FAKE_LOGIN, FAKE_LOGIN_2),
    ),
    (
        "mention_gitlab_diff_suggestion",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"@{FAKE_LOGIN} suggestion:\n```suggestion:-0+0\nreturn {FAKE_REPO}\n```",
        ),
        (FAKE_LOGIN, FAKE_REPO, FAKE_HOST),
    ),
    (
        "mention_double_backtick_and_tilde_fence",
        PipelineInput(
            body=f"Use ``@{FAKE_LOGIN} `arg` `` and ~~~text\ncc @zqcolleague\n~~~",
        ),
        (FAKE_LOGIN, "zqcolleague"),
    ),
    (
        "mention_short_stem_bot_cla",
        PipelineInput(
            login="cla[bot]",
            uid=44556677,
            body="Please run @cla check to verify signature.",
        ),
        ("cla[bot]", "@cla", "44556677"),
    ),
    (
        "mention_short_stem_bot_bob",
        PipelineInput(
            login="bob[bot]",
            uid=44556688,
            body="Please run @bob retry.",
        ),
        ("bob[bot]", "@bob", "44556688"),
    ),
    (
        "branch_login_repo_namespace",
        PipelineInput(
            head_ref=f"feat/{FAKE_LOGIN}/{FAKE_REPO}-fix",
            base_ref=f"release/{FAKE_NS}-2026",
        ),
        (FAKE_LOGIN, FAKE_REPO, FAKE_NS),
    ),
    (
        "branch_denylist_uid_and_node_ids",
        PipelineInput(
            head_ref=f"feat/{FAKE_DENY}-{FAKE_UID}-{FAKE_LEGACY_NODE}-{FAKE_NEW_NODE}",
        ),
        (FAKE_DENY, str(FAKE_UID), FAKE_LEGACY_NODE, FAKE_NEW_NODE),
    ),
    (
        "url_github_compare_raw_gist_pages_avatar",
        PipelineInput(
            body=(
                f"https://github.com/{FAKE_NS}/{FAKE_REPO}/compare/main...{FAKE_LOGIN}:{FAKE_REPO}:fix "
                f"https://raw.githubusercontent.com/{FAKE_NS}/{FAKE_REPO}/main/src/app.py "
                f"https://gist.github.com/{FAKE_LOGIN}/0123456789abcdef "
                f"https://{FAKE_LOGIN}.github.io/{FAKE_REPO}/guide.html "
                f"https://avatars.githubusercontent.com/u/{FAKE_UID}?v=4"
            ),
        ),
        (FAKE_LOGIN, FAKE_NS, FAKE_REPO, str(FAKE_UID)),
    ),
    (
        "url_self_hosted_trailing_exclamation_and_prose",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Check https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo! (see zqsecondrepo docs)",
        ),
        (FAKE_HOST, "zqsecondrepo"),
    ),
    (
        "url_self_hosted_trailing_question_and_prose",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Did you mean https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo? (see zqsecondrepo)",
        ),
        (FAKE_HOST, "zqsecondrepo"),
    ),
    (
        "url_self_hosted_markdown_delimiters_and_prose",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=(
                f"|a|https://{FAKE_HOST}/{FAKE_NS}/zqtabrepo|\n"
                f"`https://{FAKE_HOST}/{FAKE_NS}/zqbtrepo` "
                f"**https://{FAKE_HOST}/{FAKE_NS}/zqboldrepo** "
                f"*https://{FAKE_HOST}/{FAKE_NS}/zqitalrepo* "
                f"~~https://{FAKE_HOST}/{FAKE_NS}/zqstrikerepo~~ "
                f"_https://{FAKE_HOST}/{FAKE_NS}/zqunderrepo_ "
                f"[m](https://{FAKE_HOST}/{FAKE_NS}/zqlinkrepo)\n"
                "Prose: zqtabrepo zqbtrepo zqboldrepo zqitalrepo zqstrikerepo zqunderrepo zqlinkrepo"
            ),
        ),
        (
            FAKE_HOST,
            "zqtabrepo",
            "zqbtrepo",
            "zqboldrepo",
            "zqitalrepo",
            "zqstrikerepo",
            "zqunderrepo",
            "zqlinkrepo",
        ),
    ),
    (
        "url_scp_trailing_dot_and_ssh_port",
        PipelineInput(
            remote="",
            body=(
                "git clone git@zqotherhost.io.:zqotherns/zqotherrepo.git "
                f"ssh://{FAKE_LOGIN}@{FAKE_HOST}:2222/{FAKE_NS}/{FAKE_REPO}.git"
            ),
        ),
        ("zqotherhost.io", "zqotherns", "zqotherrepo", FAKE_LOGIN, FAKE_HOST, FAKE_NS, FAKE_REPO),
    ),
    (
        "url_double_slash_github_and_gitlab",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=(
                f"https://github.com/{FAKE_NS}//{FAKE_REPO}/pull/42 "
                f"https://{FAKE_HOST}/{FAKE_NS}//zqsub//{FAKE_REPO}//-/merge_requests/42"
            ),
        ),
        (FAKE_HOST, FAKE_NS, "zqsub", FAKE_REPO),
    ),
    (
        "url_glab_api_project_id_without_prefix_in_comment",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Repro: `glab api projects/{FAKE_PID}/merge_requests/42`",
        ),
        (str(FAKE_PID),),
    ),
    (
        "url_glab_api_project_id_without_prefix_in_call_args",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            api_path=f"projects/{FAKE_PID}/merge_requests/42/notes",
            body="ok",
        ),
        (str(FAKE_PID),),
    ),
    (
        "url_api_v4_project_id_with_bare_prose_id",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"URL https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/jobs/99 failed on project {FAKE_PID}.",
        ),
        (str(FAKE_PID), FAKE_HOST),
    ),
    (
        "url_api_v4_project_id_in_repo_object_and_comment",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            extra_item={
                "repository": {
                    "id": FAKE_PID,
                    "name": FAKE_REPO,
                    "path": FAKE_REPO,
                    "web_url": f"https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}",
                }
            },
            body=f"API call: https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/merge_requests/42",
        ),
        (str(FAKE_PID), FAKE_HOST, FAKE_REPO),
    ),
    (
        "url_ghcr_with_planted_userinfo_secret",
        PipelineInput(
            body=f"curl https://{FAKE_LOGIN}:s3cr3t@ghcr.io/{FAKE_NS}/{FAKE_REPO}",
        ),
        (FAKE_LOGIN, FAKE_NS, FAKE_REPO, "s3cr3t"),
    ),
    (
        "url_ghcr_with_unknown_userinfo_credentials",
        PipelineInput(
            body="curl https://alice:mysecret@ghcr.io/astral-sh/uv",
        ),
        ("alice", "mysecret"),
    ),
    (
        "url_public_reference_host_query_carrying_denylist_and_login",
        PipelineInput(
            body=(
                f"See https://pypi.org/project/ruff/?ref={FAKE_DENY} "
                f"and https://docs.github.com/en/actions?user={FAKE_LOGIN}"
            ),
        ),
        (FAKE_DENY, FAKE_LOGIN),
    ),
    (
        "url_scheme_led_rfc1918_ipv4_and_prose_repo",
        PipelineInput(
            remote="",
            body="Mirror at http://10.42.1.9:8080/zqipns/zqipproj (see zqipproj)",
        ),
        ("10.42.1.9", "zqipproj"),
    ),
    (
        "url_ftp_private_mirror_and_prose_repo",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Artifacts at ftp://{FAKE_HOST}/{FAKE_NS}/zqftprepo (see zqftprepo)",
        ),
        (FAKE_HOST, "zqftprepo"),
    ),
    (
        "url_git_plus_https_pip_install",
        PipelineInput(
            body=f"pip install git+https://github.com/{FAKE_NS}/{FAKE_REPO}.git@main",
        ),
        (FAKE_NS, FAKE_REPO),
    ),
    (
        "url_schemeless_host_seen_led_in_same_fixture",
        PipelineInput(
            forge="gitlab",
            remote="",
            url="https://git.zqseen.internal/zqgroup/zqmainrepo/-/merge_requests/42",
            body="Primary https://git.zqseen.internal/zqgroup/zqfirstrepo",
            extra_items=(
                {
                    "author": {"username": FAKE_LOGIN, "id": FAKE_UID},
                    "body": "Mirror git.zqseen.internal/zqgroup/zqsecondrepo (see zqsecondrepo)",
                },
            ),
        ),
        ("git.zqseen.internal", "zqfirstrepo", "zqsecondrepo"),
    ),
    (
        "url_in_call_args_and_top_kwargs",
        PipelineInput(
            argv_extra=("-f", f"body=See https://{FAKE_HOST}/zqargns/zqargrepo?u={FAKE_LOGIN}"),
            top_kwargs={
                "note": f"https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}/pull/1 by {FAKE_LOGIN}"
            },
        ),
        (FAKE_HOST, FAKE_LOGIN),
    ),
    (
        "email_noreply_github_gitlab_self_hosted_and_uppercase",
        PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=(
                f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN}@users.noreply.github.com>\n"
                f"Co-authored-by: Dev <{FAKE_UID}-{FAKE_LOGIN}@users.noreply.gitlab.com>\n"
                f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN}@users.noreply.{FAKE_HOST}>\n"
                f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN.upper()}@USERS.NOREPLY.GITHUB.COM>\n"
                f"From: GitLab <noreply@{FAKE_HOST}>"
            ),
        ),
        (str(FAKE_UID), FAKE_LOGIN, FAKE_HOST, "github.com", "gitlab.com"),
    ),
    (
        "email_personal_mailboxes_refused",
        PipelineInput(body="Signed-off-by: Dev <dev.person@zqcorp.io>"),
        ("dev.person", "zqcorp.io"),
    ),
    (
        "path_linux_macos_windows_known_login_and_unknown_home",
        PipelineInput(
            body=(
                f'File "/home/{FAKE_LOGIN}/work/{FAKE_REPO}/app.py", line 9\n'
                'File "/Users/janesmith/src/app.py", line 42\n'
                f'File "C:\\Users\\{FAKE_LOGIN}\\work\\{FAKE_REPO}\\app.py", line 12'
            ),
        ),
        (FAKE_LOGIN, FAKE_REPO, "janesmith"),
    ),
    (
        "ci_log_stderr_stdin_diff_public_ipv4_and_node_ids",
        PipelineInput(
            remote=GL_REMOTE,
            stderr=f"fatal: could not read {FAKE_DENY} on {FAKE_HOST}",
            stdin=f"Merge https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}/pull/1 by {FAKE_LOGIN}",
            raw_text_stdout=(
                f"diff --git a/README.md b/README.md\n"
                f"+# See https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO} ({FAKE_DENY}) 45.33.32.156 "
                f"{FAKE_DENY_LEGACY} {FAKE_DENY_NEW}\n"
            ),
        ),
        (FAKE_DENY, FAKE_HOST, FAKE_NS, FAKE_REPO, FAKE_LOGIN, "45.33.32.156"),
    ),
    (
        "case_field_and_dropped_keys_assignees_mergedby",
        PipelineInput(
            case=f"pr_{FAKE_LOGIN}",
            extra_item={
                "assignees": [{"login": "zqassignee9", "id": 55443322}],
                "mergedBy": {"login": "zqmerger8", "id": 66554433},
            },
            body="Assigned to zqassignee9 and merged by zqmerger8",
        ),
        (FAKE_LOGIN, "zqassignee9", "zqmerger8"),
    ),
)


@pytest.mark.parametrize(
    ("label", "inp", "markers"),
    PLANTED_CORPUS,
    ids=[row[0] for row in PLANTED_CORPUS],
)
def test_planted_realistic_leaks_never_survive(
    label: str, inp: PipelineInput, markers: tuple[str, ...]
) -> None:
    """Every realistic planted exposure is either refused or written with zero markers surviving."""
    status, _msg, fixture = _run_pipeline(inp)
    if status != "WRITTEN":
        assert status in ("REFUSED", "NEEDLE_ERR"), label
        return
    assert fixture is not None
    blob = json.dumps(fixture, ensure_ascii=False).casefold()
    survivors = [m for m in markers if m.casefold() in blob]
    assert survivors == [], f"{label}: survived={survivors}"


ORDINARY_BASE = {
    "login": "zqxauthor",
    "uid": 48151623,
    "ns": "zqpuborg",
    "repo": "zqpubrepo",
    "remote": "git@github.com:zqpuborg/zqpubrepo.git",
    "deny": ("zqdenyval",),
}


@dataclass(frozen=True)
class OrdinaryCase:
    """One ordinary public PR sample that must write cleanly without refusal or corruption."""

    label: str
    inp: PipelineInput
    must_have: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    exact_body: str | None = None


def _ordinary(**overrides: Any) -> PipelineInput:
    """Build a :class:`PipelineInput` from :data:`ORDINARY_BASE` with safe non-personal metadata."""
    merged = {**ORDINARY_BASE, **overrides}
    return PipelineInput(**merged)


ORDINARY_CORPUS: tuple[OrdinaryCase, ...] = (
    OrdinaryCase(
        label="plain_review_prose_and_task_list",
        inp=_ordinary(
            body="LGTM! Tested locally:\n- [x] unit tests pass\n- [x] ruff clean\n\nReady to merge."
        ),
        exact_body="LGTM! Tested locally:\n- [x] unit tests pass\n- [x] ruff clean\n\nReady to merge.",
    ),
    OrdinaryCase(
        label="gitlab_suggestion_block",
        inp=_ordinary(
            forge="gitlab",
            remote="git@gitlab.com:zqpuborg/zqpubrepo.git",
            url="https://gitlab.com/zqpuborg/zqpubrepo/-/merge_requests/42",
            body=(
                "Small nit:\n```suggestion:-0+0\n"
                "def parse(raw: str) -> dict[str, int]:\n"
                "    return {k: int(v) for k, v in raw.items()}\n"
                "```\nLooks solid otherwise."
            ),
        ),
        exact_body=(
            "Small nit:\n```suggestion:-0+0\n"
            "def parse(raw: str) -> dict[str, int]:\n"
            "    return {k: int(v) for k, v in raw.items()}\n"
            "```\nLooks solid otherwise."
        ),
    ),
    OrdinaryCase(
        label="mixed_case_docs_github_and_docs_gitlab",
        inp=_ordinary(
            body=(
                "See https://Docs.GitHub.com/en/actions/using-workflows and "
                "HTTPS://DOCS.GITHUB.COM/en/rest/pulls and "
                "https://Docs.GitLab.com/ee/api/merge_requests.html"
            )
        ),
        must_have=(
            "https://docs.github.com/en/actions/using-workflows",
            "https://docs.github.com/en/rest/pulls",
            "https://docs.gitlab.com/ee/api/merge_requests.html",
        ),
        must_not=("forge.example.com",),
    ),
    OrdinaryCase(
        label="trailing_exclamation_and_question_on_kept_and_redacted_urls",
        inp=_ordinary(
            body=(
                "Try https://example.com! Also http://localhost! "
                "Published https://pypi.org/project/ruff/! "
                "Checked https://example.com? And https://docs.python.org/3/library/re.html?"
            )
        ),
        must_have=(
            "https://example.com!",
            "http://localhost!",
            "https://pypi.org/project/ruff/!",
            "https://example.com?",
            "https://forge.example.com/org/project?",
        ),
    ),
    OrdinaryCase(
        label="gfm_table_and_backticked_urls",
        inp=_ordinary(
            body=(
                "Check `https://example.com` and `http://localhost` and `https://127.0.0.1:8080/health`.\n"
                "|Tool|Link|\n|---|---|\n"
                "|Ruff|https://pypi.org/project/ruff/|\n"
                "|Docs|https://docs.github.com/en/actions|\n"
                "|Py|https://docs.python.org/3/library/re.html|\n"
            )
        ),
        must_have=(
            "`https://example.com`",
            "`http://localhost`",
            "`https://127.0.0.1:8080/health`",
            "|Ruff|https://pypi.org/project/ruff/|",
            "|Docs|https://docs.github.com/en/actions|",
            "|Py|https://forge.example.com/org/project|",
        ),
    ),
    OrdinaryCase(
        label="gitlab_api_doc_examples_never_refuse_or_corrupt",
        inp=_ordinary(
            body=(
                "Per GitLab docs: `curl https://gitlab.example.com/api/v4/projects/1/merge_requests`\n"
                "Mock endpoint: `GET /api/v4/projects/42/issues`\n"
                "Example: `curl https://gitlab.example.com/api/v4/projects/1234/issues`"
            )
        ),
        must_have=(
            "https://gitlab.example.com/api/v4/projects/1/merge_requests",
            "/api/v4/projects/42/issues",
            "https://gitlab.example.com/api/v4/projects/1234/issues",
        ),
        must_not=("2001",),
    ),
    OrdinaryCase(
        label="macos_bundle_paths_and_tld_matching_code_attributes",
        inp=_ordinary(
            body=(
                "Using Xcode.app/Contents/Developer/usr/bin/clang and MyApp.app/Contents/MacOS/app\n"
                "```python\n"
                "path = config.dev/env/prod\n"
                "cert = ssl.ca/certs/root.pem\n"
                "buf = stream.io/read/write\n"
                "layer = torch.net/layers/conv\n"
                "ratio = self.ms/1000\n"
                "cfg = config.dir/sub/file.toml\n"
                "val = obj.total/count/100\n"
                "root = tmp.root/src/app.py\n"
                "result = a.b/c/d\n"
                "```"
            )
        ),
        must_have=(
            "Xcode.app/Contents/Developer/usr/bin/clang",
            "MyApp.app/Contents/MacOS/app",
            "config.dev/env/prod",
            "ssl.ca/certs/root.pem",
            "stream.io/read/write",
            "torch.net/layers/conv",
            "self.ms/1000",
            "config.dir/sub/file.toml",
            "obj.total/count/100",
            "tmp.root/src/app.py",
            "a.b/c/d",
        ),
        must_not=("forge.example.com",),
    ),
    OrdinaryCase(
        label="decorators_annotations_and_scoped_npm_packages",
        inp=_ordinary(
            body=(
                "Use @dataclass, @property, @Override, @Deprecated, @pytest.fixture and @lru_cache(maxsize=64).\n"
                "Import `@types/node`, `@vue/cli`, `@babel/core`, `@actions/checkout`, `@tanstack/react-query`.\n"
                "```ts\nconst url = this.base/api/v1;\nimport H from 'components.tsx/Header/index';\n```"
            )
        ),
        must_have=(
            "@dataclass",
            "@property",
            "@Override",
            "@Deprecated",
            "@types/node",
            "@vue/cli",
            "@babel/core",
            "@actions/checkout",
            "@tanstack/react-query",
            "this.base/api/v1",
            "components.tsx/Header/index",
        ),
        must_not=("forge.example.com", "@author"),
    ),
    OrdinaryCase(
        label="dependabot_and_renovate_pr_templates",
        inp=_ordinary(
            login="dependabot[bot]",
            uid=49699333,
            author="dependabot[bot]",
            body=(
                "Bumps ruff from 0.14.0 to 0.15.4.\n"
                "Dependabot will resolve any conflicts.\n"
                "- `@dependabot rebase` will rebase this PR\n"
                "- @dependabot recreate will recreate this PR\n"
                "Also updates @types/node and @vue/cli."
            ),
        ),
        must_have=("@author rebase", "@author recreate", "@types/node", "@vue/cli"),
        must_not=("dependabot",),
    ),
    OrdinaryCase(
        label="renovate_with_npm_and_docs_github",
        inp=_ordinary(
            login="renovate[bot]",
            uid=29139614,
            author="renovate[bot]",
            title="chore(deps): update @actions/checkout action to v4",
            body=(
                "| [@babel/core](https://www.npmjs.com/package/@babel/core) | `7.24.0` -> `7.25.0` |\n"
                "Renovate will rebase automatically. See https://docs.github.com/en/actions.\n"
                "Trigger @renovate rebase if needed."
            ),
        ),
        must_have=(
            "@babel/core",
            "https://www.npmjs.com/package/@babel/core",
            "https://docs.github.com/en/actions",
            "@author rebase",
        ),
        must_not=("renovate",),
    ),
    OrdinaryCase(
        label="git_log_co_authored_by_noreply_and_actions_github",
        inp=_ordinary(
            body=(
                "commit deadbeef0000\n"
                "Author: Dev <12345+someone@users.noreply.github.com>\n"
                "Committer: GitHub <noreply@github.com>\n"
                "Author: github-actions[bot] <actions@github.com>\n"
                "Co-authored-by: Tool <175728472+Copilot@users.noreply.github.com>\n"
                "Co-authored-by: Bob <999-bob@users.noreply.gitlab.com>\n"
                "Co-authored-by: Claude <noreply@anthropic.com>\n"
            )
        ),
        must_have=(
            "author@users.noreply.forge.example.com",
            "noreply@forge.example.com",
            "github-actions[bot]",
        ),
        must_not=("12345", "175728472", "someone", "Copilot", "github.com", "gitlab.com"),
    ),
    OrdinaryCase(
        label="docker_go_k8s_pypi_npm_public_reference_hosts",
        inp=_ordinary(
            body=(
                "```dockerfile\nFROM docker.io/library/python:3.12-slim\n"
                "COPY --from=ghcr.io/astral-sh/uv:0.4.0 /uv /bin/uv\n```\n"
                '```go\nimport (\n\t"golang.org/x/sync/errgroup"\n'
                '\t"k8s.io/client-go/kubernetes"\n)\n```\n'
                "See https://pkg.go.dev/golang.org/x/sync/errgroup and "
                "https://proxy.golang.org/ and https://registry.npmjs.org/typescript\n"
            )
        ),
        must_have=(
            "docker.io/library/python:3.12-slim",
            "ghcr.io/astral-sh/uv:0.4.0",
            "golang.org/x/sync/errgroup",
            "k8s.io/client-go/kubernetes",
            "https://pkg.go.dev/golang.org/x/sync/errgroup",
            "https://proxy.golang.org/",
            "https://registry.npmjs.org/typescript",
        ),
        must_not=("forge.example.com",),
    ),
    OrdinaryCase(
        label="self_hosted_gitlab_empty_remote_with_docs_python_org",
        inp=_ordinary(
            forge="gitlab",
            remote="",
            url=f"https://{FAKE_HOST}/zqpuborg/zqpubrepo/-/merge_requests/1#note_1",
            body="See https://docs.python.org/3/library/re.html for regex help.",
        ),
        must_have=("https://forge.example.com/org/project",),
    ),
    OrdinaryCase(
        label="github_empty_remote_and_ssh_alias_with_third_party_links",
        inp=_ordinary(
            remote="mygh:zqpuborg/zqpubrepo.git",
            body=(
                "Upgrade python and git; see https://github.com/python/cpython/pull/1, "
                "https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup"
            ),
        ),
        must_have=("Upgrade python and git", "golang.org/x/sync/errgroup"),
    ),
    OrdinaryCase(
        label="ci_log_wheel_dist_info_tracebacks_and_rfc_examples",
        inp=_ordinary(
            body=(
                'File "src/repoach/cli/main.py", line 42, in run\n'
                "Adding repoach-0.1.0.dist-info/licenses/LICENSE\n"
                "Send test payload to demo@example.com.\n"
                "Local bind 127.0.0.1:8080, private 10.0.0.5, doc 203.0.113.10 and 198.51.100.7.\n"
                "Tested on v1.2.3.4 and foo==2.3.4.5 and 3.4.5.6.tar.gz.\n"
                "curl -sSf https://example.com/health > /dev/null 2>&1"
            )
        ),
        must_have=(
            "repoach-0.1.0.dist-info/licenses/LICENSE",
            "demo@example.com",
            "203.0.113.10",
            "v1.2.3.4",
            "/dev/null",
        ),
        must_not=("forge.example.com",),
    ),
    OrdinaryCase(
        label="non_json_unified_diff_code_and_decorators",
        inp=_ordinary(
            raw_text_stdout=(
                "diff --git a/src/mod.py b/src/mod.py\n"
                "--- a/src/mod.py\n+++ b/src/mod.py\n"
                "@@ -1,4 +1,5 @@\n @pytest.fixture\n def f(cfg):\n"
                "-    return self.ms/1000\n+    return cfg.root/sub/file\n"
            ),
        ),
        exact_body=(
            "diff --git a/src/mod.py b/src/mod.py\n"
            "--- a/src/mod.py\n+++ b/src/mod.py\n"
            "@@ -1,4 +1,5 @@\n @pytest.fixture\n def f(cfg):\n"
            "-    return self.ms/1000\n+    return cfg.root/sub/file\n"
        ),
    ),
)


@pytest.mark.parametrize("case", ORDINARY_CORPUS, ids=[c.label for c in ORDINARY_CORPUS])
def test_ordinary_public_prs_are_never_refused_or_corrupted(case: OrdinaryCase) -> None:
    """Ordinary public PR payloads write cleanly without false refusal or text corruption."""
    status, msg, fixture = _run_pipeline(case.inp)
    assert status == "WRITTEN", f"{case.label}: {status}: {msg}"
    assert fixture is not None
    stdout = fixture["calls"][0]["stdout"]
    body = stdout if case.inp.raw_text_stdout is not None else json.loads(stdout)[0]["body"]
    if case.exact_body is not None:
        assert body == case.exact_body, case.label
    for expected in case.must_have:
        assert expected in body, f"{case.label}: missing {expected!r} in {body!r}"
    for forbidden in case.must_not:
        assert forbidden.casefold() not in body.casefold(), (
            f"{case.label}: unwanted {forbidden!r} in {body!r}"
        )


OFFLINE_CORPUS: tuple[tuple[str, dict[str, Any], str, str], ...] = (
    (
        "offline_mixed_case_docs_github_and_gitlab",
        {
            "user": {"login": "author", "id": 1001},
            "body": "See https://Docs.GitHub.com/en/actions and https://Docs.GitLab.com/ee/api/",
        },
        "See https://docs.github.com/en/actions and https://docs.gitlab.com/ee/api/",
        "author",
    ),
    (
        "offline_short_stem_cla_bot",
        {"user": {"login": "cla[bot]", "id": 44556677}, "body": "Run @cla check"},
        "Run @bot check",
        "bot",
    ),
    (
        "offline_uppercase_bot_suffix",
        {
            "user": {"login": "Dependabot[BOT]", "id": 44556677},
            "body": "Dependabot: @dependabot rebase",
        },
        "bot: @bot rebase",
        "bot",
    ),
    (
        "offline_placeholder_stem_project_bot",
        {
            "user": {"login": "project[bot]", "id": 44556677},
            "body": "See https://github.com/o/r/pull/1 and @project",
        },
        "See https://forge.example.com/org/project/pull/1 and @bot",
        "bot",
    ),
    (
        "offline_system_noreply_and_actions_github",
        {
            "user": {"login": "author", "id": 1001},
            "body": "GitHub <noreply@github.com>; Actions <actions@github.com>; keep `https://example.com`",
        },
        "GitHub <noreply@forge.example.com>; Actions <noreply@forge.example.com>; keep `https://example.com`",
        "author",
    ),
)


@pytest.mark.parametrize(
    ("label", "item", "expected_body", "expected_login"),
    OFFLINE_CORPUS,
    ids=[row[0] for row in OFFLINE_CORPUS],
)
def test_offline_transform_regression_corpus(
    label: str, item: dict[str, Any], expected_body: str, expected_login: str
) -> None:
    """Offline transform stays consistent with the recorder, passes the oracle, and is idempotent."""
    fixture: dict[str, Any] = {
        "forge": "github",
        "case": label,
        "method": "conversation_comments",
        "args": [1],
        "kwargs": {"note": "https://github.com/o/r/pull/1"},
        "bot_username": "repoach-bot",
        "source": rec_mod.recorded_source("2026-11-19", "github"),
        "trimmed": None,
        "calls": [
            {
                "program": "gh",
                "args": ["api", "repos/:owner/:repo/issues/1/comments"],
                "stdin": None,
                "returncode": 0,
                "stdout": json.dumps([item]),
                "stderr": "",
            }
        ],
        "result": None,
        "raises": None,
    }
    redacted = OFF.redact_fixture(fixture, forge="github")
    out_item = json.loads(redacted["calls"][0]["stdout"])[0]
    assert out_item["body"] == expected_body, label
    assert out_item["user"]["login"] == expected_login, label
    assert redacted["kwargs"] == {"note": "https://forge.example.com/org/project/pull/1"}
    assert rec_mod.residual(redacted, OFF.fixture_needles(fixture)) is None, label
    assert OFF.redact_fixture(redacted, forge="github") == redacted, label
