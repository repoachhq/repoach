"""Permanent regression corpus promoted from verifier rounds 5, 6, 7, 8 and 9 (SP-RECORDER-REDACTS-BY-CLASS 0.16).

Every realistic sample from the round-5, round-6, round-7 and round-8
verification sweeps lives here as an unbundled, single-probe, table-driven
test with fake values only:

- Planted-leak rows (:data:`PLANTED_CORPUS`): each row asserts ONE exact
  expected outcome (``WRITTEN`` with concrete redacted output and zero
  planted markers surviving, ``REFUSED`` with expected refusal prefix, or
  ``NEEDLE_ERR`` with expected error prefix).
- Ordinary public PR rows (:data:`ORDINARY_CORPUS`): realistic GitHub and
  GitLab comments, titles, code blocks, decorators, scoped packages,
  bundle paths, code attribute divisions, tracebacks, CI logs, Dependabot
  and Renovate templates, Co-authored-by trailers, public documentation
  and package hosts, balanced-parenthesis URLs, year-named ``projects/``
  paths, ``//`` line comments, ``#__init__`` anchors, and GitLab API
  documentation examples must write cleanly without refusal or corruption.
  Every row that sets ``title`` asserts the redacted ``title`` as well.
- Offline transform rows (:data:`OFFLINE_CORPUS`): mixed-case docs hosts,
  public documentation hosts, short-stem bots, placeholder-stem bots,
  uppercase ``[BOT]``, SCP userinfo, ``#__init__``, ``glab api`` project
  ids, non-JSON diffs, ``kwargs``, and system noreply / ``actions@github.com``
  mailboxes must transform cleanly, pass the closure oracle, and be
  idempotent.
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
from typing import Any, Literal, cast

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
FAKE_HOST_2 = "mirror.zqcorp.net"
FAKE_REPO_2 = "zqbillrepo"
FAKE_NS = "zqgroup"
FAKE_REPO = "zqmainrepo"
FAKE_UID = 74928163
FAKE_UID_PAD = 4815162
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
FAKE_LEGACY_PAD = _node_legacy(FAKE_UID_PAD)
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
    call_args: tuple[str, ...] | None = None
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
            "created_at": "2026-11-19T12:00:00Z",
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
                "args": list(inp.call_args)
                if inp.call_args is not None
                else ["api", inp.api_path or default_api, *inp.argv_extra],
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


@dataclass(frozen=True)
class PlantedCase:
    """One single-probe planted-leak or refusal regression case with one exact expected outcome."""

    label: str
    inp: PipelineInput
    expected_status: Literal["WRITTEN", "REFUSED", "NEEDLE_ERR"]
    markers: tuple[str, ...] = ()
    expected_error_prefix: str = ""
    must_have: tuple[str, ...] = ()
    title_must_have: tuple[str, ...] = ()
    title_must_not: tuple[str, ...] = ()


PLANTED_CORPUS: tuple[PlantedCase, ...] = (
    PlantedCase(
        label="mention_known_gh",
        inp=PipelineInput(body=f"Thanks @{FAKE_LOGIN}, merging now."),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("Thanks @author, merging now.",),
    ),
    PlantedCase(
        label="mention_unknown_colleague_gh",
        inp=PipelineInput(body=f"Thanks @{FAKE_LOGIN}, cc @{FAKE_LOGIN_2} for review."),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_LOGIN_2),
        must_have=("Thanks @author, cc @author-2 for review.",),
    ),
    PlantedCase(
        label="mention_gitlab_diff_suggestion",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"@{FAKE_LOGIN} suggestion:\n```suggestion:-0+0\nreturn {FAKE_REPO}\n```",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_REPO, FAKE_HOST),
        must_have=("@author suggestion:", "return project"),
    ),
    PlantedCase(
        label="mention_in_title",
        inp=PipelineInput(title=f"Fix bug reported by @{FAKE_LOGIN}"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        title_must_have=("Fix bug reported by @author",),
        title_must_not=(FAKE_LOGIN,),
    ),
    PlantedCase(
        label="mention_known_in_double_backticks",
        inp=PipelineInput(body=f"Use ``@{FAKE_LOGIN} `arg` `` here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("Use ``@author `arg` `` here",),
    ),
    PlantedCase(
        label="mention_unknown_in_double_backticks",
        inp=PipelineInput(body="Use ``@zqcolleague `arg` `` here"),
        expected_status="WRITTEN",
        markers=("zqcolleague",),
        must_have=("Use ``@author-2 `arg` `` here",),
    ),
    PlantedCase(
        label="mention_unknown_in_tilde_fence",
        inp=PipelineInput(body="~~~text\ncc @zqcolleague\n~~~"),
        expected_status="WRITTEN",
        markers=("zqcolleague",),
        must_have=("~~~text\ncc @author-2\n~~~",),
    ),
    PlantedCase(
        label="mention_short_stem_bot_cla",
        inp=PipelineInput(
            login="cla[bot]",
            uid=44556677,
            body="Please run @cla check to verify signature.",
        ),
        expected_status="WRITTEN",
        markers=("cla[bot]", "@cla", "44556677"),
        must_have=("Please run @bot check to verify signature.",),
    ),
    PlantedCase(
        label="mention_short_stem_bot_bob",
        inp=PipelineInput(
            login="bob[bot]",
            uid=44556688,
            body="Please run @bob retry.",
        ),
        expected_status="WRITTEN",
        markers=("bob[bot]", "@bob", "44556688"),
        must_have=("Please run @bot retry.",),
    ),
    PlantedCase(
        label="mention_short_stem_bot_ci_mention_first",
        inp=PipelineInput(
            body="cc @ci please run",
            extra_items=({"user": {"login": "ci[bot]", "id": 61112222}, "body": "Running."},),
        ),
        expected_status="WRITTEN",
        markers=("ci[bot]", "@ci", "61112222"),
        must_have=("cc @bot please run",),
    ),
    PlantedCase(
        label="mention_1char_bot_x",
        inp=PipelineInput(login="x[bot]", uid=61112233, body="Run @x check"),
        expected_status="WRITTEN",
        markers=("x[bot]", "@x", "61112233"),
        must_have=("Run @bot check",),
    ),
    PlantedCase(
        label="mention_uppercase_bot_cla",
        inp=PipelineInput(login="CLA[BOT]", uid=61112244, body="Triggered @CLA check"),
        expected_status="WRITTEN",
        markers=("cla[bot]", "@cla", "61112244"),
        must_have=("Triggered @bot check",),
    ),
    PlantedCase(
        label="gitlab_project_access_token_bot",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            login=f"project_{FAKE_PID}_bot_a1b2c3d4e5f6",
            uid=61112255,
            body="Automated merge",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID), "a1b2c3d4e5f6", "61112255"),
        must_have=('"username": "service"',),
    ),
    PlantedCase(
        label="mixed_sep_under_dash_login",
        inp=PipelineInput(body=f"see _{FAKE_LOGIN}- here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("see _author- here",),
    ),
    PlantedCase(
        label="mixed_sep_dash_under_login",
        inp=PipelineInput(body=f"see -{FAKE_LOGIN}_ here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("see -author_ here",),
    ),
    PlantedCase(
        label="mixed_sep_dot_under_login",
        inp=PipelineInput(body=f"see .{FAKE_LOGIN}_ here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("see .author_ here",),
    ),
    PlantedCase(
        label="branch_github_feat_login_repo",
        inp=PipelineInput(
            head_ref=f"feat/{FAKE_LOGIN}/{FAKE_REPO}-fix",
            base_ref=f"release/{FAKE_NS}-2026",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_REPO, FAKE_NS),
        must_have=("feat/author/project-fix", "release/org-2026"),
    ),
    PlantedCase(
        label="branch_github_login_patch_1",
        inp=PipelineInput(head_ref=f"{FAKE_LOGIN}/patch-1"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("author/patch-1",),
    ),
    PlantedCase(
        label="branch_github_renovate_repo",
        inp=PipelineInput(head_ref=f"renovate/{FAKE_REPO}-2.x"),
        expected_status="WRITTEN",
        markers=(FAKE_REPO,),
        must_have=("renovate/project-2.x",),
    ),
    PlantedCase(
        label="branch_gitlab_source_branch_ns_login",
        inp=PipelineInput(forge="gitlab", remote=GL_REMOTE, head_ref=f"{FAKE_NS}/{FAKE_LOGIN}-wip"),
        expected_status="WRITTEN",
        markers=(FAKE_NS, FAKE_LOGIN),
        must_have=("org/author-wip",),
    ),
    PlantedCase(
        label="branch_sp_login_upper",
        inp=PipelineInput(head_ref=f"SP-{FAKE_LOGIN.upper()}-FIX"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("SP-author-FIX",),
    ),
    PlantedCase(
        label="branch_sp_repo_upper",
        inp=PipelineInput(head_ref=f"feat/SP-{FAKE_REPO.upper()}"),
        expected_status="WRITTEN",
        markers=(FAKE_REPO,),
        must_have=("feat/SP-project",),
    ),
    PlantedCase(
        label="branch_with_denylist_token",
        inp=PipelineInput(head_ref=f"feat/{FAKE_DENY}-cleanup"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].headRefName",
    ),
    PlantedCase(
        label="branch_with_user_id",
        inp=PipelineInput(head_ref=f"fix/user-{FAKE_UID}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].headRefName",
    ),
    PlantedCase(
        label="branch_with_legacy_node_id",
        inp=PipelineInput(head_ref=f"feat/{FAKE_LEGACY_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].headRefName",
    ),
    PlantedCase(
        label="branch_with_new_u_node_id",
        inp=PipelineInput(head_ref=f"feat/{FAKE_NEW_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].headRefName",
    ),
    PlantedCase(
        label="node_legacy_in_url_path",
        inp=PipelineInput(body=f"https://example.com/users/{FAKE_LEGACY_NODE}/profile"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_legacy_alpha_prefix",
        inp=PipelineInput(body=f"user{FAKE_LEGACY_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_legacy_suffixed",
        inp=PipelineInput(body=f"{FAKE_LEGACY_NODE}suffix"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_legacy_padded_in_url_path",
        inp=PipelineInput(
            uid=FAKE_UID_PAD,
            body=f"https://example.com/users/{FAKE_LEGACY_PAD}/profile",
        ),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_new_u_in_url_path",
        inp=PipelineInput(body=f"https://example.com/users/{FAKE_NEW_NODE}/profile"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_new_u_alpha_prefix",
        inp=PipelineInput(body=f"abcdefg{FAKE_NEW_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_deny_legacy_in_url_path",
        inp=PipelineInput(body=f"https://example.com/users/{FAKE_DENY_LEGACY}/profile"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="node_deny_new_u_in_url_path",
        inp=PipelineInput(body=f"https://example.com/users/{FAKE_DENY_NEW}/profile"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="json_in_string_pure_login_value",
        inp=PipelineInput(body=json.dumps({"note": FAKE_LOGIN})),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=('{"note": "author"}',),
    ),
    PlantedCase(
        label="json_in_string_pure_login_key",
        inp=PipelineInput(body=json.dumps({FAKE_LOGIN: 1})),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=('{"author": 1}',),
    ),
    PlantedCase(
        label="url_github_compare_fork",
        inp=PipelineInput(
            body=f"Diff: https://github.com/{FAKE_NS}/{FAKE_REPO}/compare/main...{FAKE_LOGIN}:{FAKE_REPO}:fix"
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_NS, FAKE_REPO),
        must_have=("https://forge.example.com/org/project/compare/main...author:project:fix",),
    ),
    PlantedCase(
        label="url_raw_githubusercontent",
        inp=PipelineInput(
            body=f"File: https://raw.githubusercontent.com/{FAKE_NS}/{FAKE_REPO}/main/src/app.py"
        ),
        expected_status="WRITTEN",
        markers=(FAKE_NS, FAKE_REPO),
        must_have=("https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_gist_github_login",
        inp=PipelineInput(body=f"Gist: https://gist.github.com/{FAKE_LOGIN}/0123456789abcdef"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_github_pages_login_repo",
        inp=PipelineInput(body=f"Docs: https://{FAKE_LOGIN}.github.io/{FAKE_REPO}/guide.html"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_REPO),
        must_have=("https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_avatars_githubusercontent_uid",
        inp=PipelineInput(body=f"Avatar: https://avatars.githubusercontent.com/u/{FAKE_UID}?v=4"),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID),),
        must_have=("https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_kept_host_path_with_login",
        inp=PipelineInput(body=f"https://example.com/users/{FAKE_LOGIN}/bio"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("https://example.com/users/author/bio",),
    ),
    PlantedCase(
        label="url_kept_host_subdomain_with_login",
        inp=PipelineInput(body=f"https://{FAKE_LOGIN}.example.com/bio"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("https://author.example.com/bio",),
    ),
    PlantedCase(
        label="url_kept_host_query_with_login",
        inp=PipelineInput(body=f"https://example.com/p?u={FAKE_LOGIN}"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("https://example.com/p?u=author",),
    ),
    PlantedCase(
        label="url_kept_host_example_denylist_in_path",
        inp=PipelineInput(body=f"https://example.com/docs/{FAKE_DENY}/index.html"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="url_github_query_carrying_denylist",
        inp=PipelineInput(body=f"https://github.com/{FAKE_NS}/{FAKE_REPO}/issues?q={FAKE_DENY}"),
        expected_status="WRITTEN",
        markers=(FAKE_DENY, FAKE_NS, FAKE_REPO),
        must_have=("https://forge.example.com/org/project/issues",),
    ),
    PlantedCase(
        label="url_pypi_query_carrying_denylist",
        inp=PipelineInput(body=f"See https://pypi.org/project/ruff/?ref={FAKE_DENY}"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="url_docs_github_query_carrying_login",
        inp=PipelineInput(body=f"See https://docs.github.com/en/actions?user={FAKE_LOGIN}"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("https://docs.github.com/en/actions?user=author",),
    ),
    PlantedCase(
        label="url_docs_github_url_encoded_login",
        inp=PipelineInput(
            body="Ref https://docs.github.com/en/actions?q=%7a%71%78%6a%64%6f%65%37",
        ),
        expected_status="REFUSED",
        expected_error_prefix="residual login 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="html_entity_encoded_denylist",
        inp=PipelineInput(body="Note: &#122;qdenyval here"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="url_self_hosted_trailing_exclamation_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Check https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo! (see zqsecondrepo docs)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Check https://forge.example.com/org/project! (see project docs)",),
    ),
    PlantedCase(
        label="url_self_hosted_trailing_question_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Did you mean https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo? (see zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Did you mean https://forge.example.com/org/project? (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_trailing_semicolon_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"See https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo; zqsecondrepo owns it.",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("See https://forge.example.com/org/project; project owns it.",),
    ),
    PlantedCase(
        label="url_self_hosted_trailing_comma_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Updated https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo, check zqsecondrepo CI.",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Updated https://forge.example.com/org/project, check project CI.",),
    ),
    PlantedCase(
        label="url_self_hosted_single_quotes_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Remote 'https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo' (zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Remote 'https://forge.example.com/org/project' (project)",),
    ),
    PlantedCase(
        label="url_self_hosted_angle_brackets_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Link <https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo> for zqsecondrepo",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Link <https://forge.example.com/org/project> for project",),
    ),
    PlantedCase(
        label="url_self_hosted_double_punct_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Moved to https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo?! Verify zqsecondrepo.",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("Moved to https://forge.example.com/org/project?! Verify project.",),
    ),
    PlantedCase(
        label="url_self_hosted_footnote_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"[1]: https://{FAKE_HOST}/{FAKE_NS}/zqsecondrepo\nDetails in zqsecondrepo.",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("[1]: https://forge.example.com/org/project\nDetails in project.",),
    ),
    PlantedCase(
        label="url_self_hosted_gfm_table_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"|a|https://{FAKE_HOST}/{FAKE_NS}/zqtabrepo|\nProse: zqtabrepo",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqtabrepo"),
        must_have=("|a|https://forge.example.com/org/project|", "Prose: project"),
    ),
    PlantedCase(
        label="url_self_hosted_backtick_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"`https://{FAKE_HOST}/{FAKE_NS}/zqbtrepo` (see zqbtrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqbtrepo"),
        must_have=("`https://forge.example.com/org/project` (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_bold_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"**https://{FAKE_HOST}/{FAKE_NS}/zqboldrepo** (see zqboldrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqboldrepo"),
        must_have=("**https://forge.example.com/org/project** (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_italic_star_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"*https://{FAKE_HOST}/{FAKE_NS}/zqitalrepo* (see zqitalrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqitalrepo"),
        must_have=("*https://forge.example.com/org/project* (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_strikethrough_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"~~https://{FAKE_HOST}/{FAKE_NS}/zqstrikerepo~~ (see zqstrikerepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqstrikerepo"),
        must_have=("~~https://forge.example.com/org/project~~ (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_italic_under_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"_https://{FAKE_HOST}/{FAKE_NS}/zqunderrepo_ (see zqunderrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqunderrepo"),
        must_have=("_https://forge.example.com/org/project_ (see project)",),
    ),
    PlantedCase(
        label="url_self_hosted_markdown_link_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"See [m](https://{FAKE_HOST}/{FAKE_NS}/zqlinkrepo) for zqlinkrepo",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqlinkrepo"),
        must_have=("See [m](https://forge.example.com/org/project) for project",),
    ),
    PlantedCase(
        label="url_self_hosted_mixed_case_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"See HTTPS://GIT.ZQCORP.IO/{FAKE_NS}/zqsecondrepo (zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("See https://forge.example.com/org/project (project)",),
    ),
    PlantedCase(
        label="url_scp_mixed_case_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"git clone git@Git.ZQCorp.IO:{FAKE_NS}/zqsecondrepo.git (zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqsecondrepo"),
        must_have=("git clone git@forge.example.com:org/project.git (project)",),
    ),
    PlantedCase(
        label="url_scp_same_host_trailing_dot",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"git clone git@{FAKE_HOST}.:{FAKE_NS}/{FAKE_REPO}.git",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, FAKE_REPO),
        must_have=("git clone git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="url_scp_third_party_trailing_dot",
        inp=PipelineInput(
            remote="",
            body="git clone git@zqotherhost.io.:zqotherns/zqotherrepo.git",
        ),
        expected_status="WRITTEN",
        markers=("zqotherhost.io", "zqotherns", "zqotherrepo"),
        must_have=("git clone git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="url_https_trailing_dot_self_hosted",
        inp=PipelineInput(
            remote="",
            body="see https://git.zqother.io./zqotherns/zqotherrepo",
        ),
        expected_status="WRITTEN",
        markers=("git.zqother.io", "zqotherns", "zqotherrepo"),
        must_have=("see https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_ssh_port_and_login_userinfo",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"ssh://{FAKE_LOGIN}@{FAKE_HOST}:2222/{FAKE_NS}/{FAKE_REPO}.git",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_HOST, FAKE_NS, FAKE_REPO),
        must_have=("ssh://git@forge.example.com/org/project.git",),
    ),
    PlantedCase(
        label="url_https_credentials_query_fragment",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"https://{FAKE_LOGIN}:s3cr3t@{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}.git?ref={FAKE_LOGIN}#{FAKE_LOGIN}",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, "s3cr3t", FAKE_HOST, FAKE_NS, FAKE_REPO),
        must_have=("https://forge.example.com/org/project.git",),
    ),
    PlantedCase(
        label="url_empty_password_pat_same_host_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Clone https://glpat99:@{FAKE_HOST}/{FAKE_NS}/zqsecondrepo (see zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=("glpat99", FAKE_HOST, "zqsecondrepo"),
        must_have=("Clone https://forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_empty_password_pat_second_host_remote_empty",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"Clone https://glpat99:@{FAKE_HOST_2}/{FAKE_NS}/zqsecondrepo (see zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=("glpat99", FAKE_HOST_2, "zqsecondrepo"),
        must_have=("Clone https://forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_symbol_password_excl_and_prose",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Curl https://nkuser:pw!@{FAKE_HOST}/{FAKE_NS}/zqsecondrepo (see zqsecondrepo)",
        ),
        expected_status="WRITTEN",
        markers=("nkuser", "pw!", FAKE_HOST, "zqsecondrepo"),
        must_have=("Curl https://forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_scp_userinfo_on_example_host",
        inp=PipelineInput(body="Run alice@example.com:org/repo.git"),
        expected_status="WRITTEN",
        markers=("alice",),
        must_have=("Run git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="url_ghcr_with_planted_userinfo_secret",
        inp=PipelineInput(body=f"curl https://{FAKE_LOGIN}:s3cr3t@ghcr.io/{FAKE_NS}/{FAKE_REPO}"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_NS, FAKE_REPO, "s3cr3t"),
        must_have=("curl https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_ghcr_with_unknown_userinfo_credentials",
        inp=PipelineInput(body="curl https://alice:mysecret@ghcr.io/astral-sh/uv"),
        expected_status="WRITTEN",
        markers=("alice", "mysecret"),
        must_have=("curl https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_npmjs_with_userinfo",
        inp=PipelineInput(body=f"npm config set https://{FAKE_LOGIN}:npmtok99@registry.npmjs.org/"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, "npmtok99"),
        must_have=("npm config set https://forge.example.com/",),
    ),
    PlantedCase(
        label="url_docker_with_userinfo",
        inp=PipelineInput(body="docker login https://nkops:dockpass@docker.io/v2/"),
        expected_status="WRITTEN",
        markers=("nkops", "dockpass"),
        must_have=("docker login https://forge.example.com/org/",),
    ),
    PlantedCase(
        label="url_docs_github_with_userinfo",
        inp=PipelineInput(body="curl https://nkops:ghpass@docs.github.com/en/rest"),
        expected_status="WRITTEN",
        markers=("nkops", "ghpass"),
        must_have=("curl https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_example_org_with_userinfo",
        inp=PipelineInput(body="curl https://nkops:expass@example.org/test"),
        expected_status="WRITTEN",
        markers=("nkops", "expass"),
        must_have=("curl https://forge.example.com/org",),
    ),
    PlantedCase(
        label="url_localhost_with_userinfo",
        inp=PipelineInput(body="curl http://nkadmin:localpw@localhost:5432/db"),
        expected_status="WRITTEN",
        markers=("nkadmin", "localpw"),
        must_have=("curl http://forge.example.com/org",),
    ),
    PlantedCase(
        label="url_double_slash_github",
        inp=PipelineInput(body=f"https://github.com/{FAKE_NS}//{FAKE_REPO}/pull/42"),
        expected_status="WRITTEN",
        markers=(FAKE_NS, FAKE_REPO),
        must_have=("https://forge.example.com/org/project/pull/42",),
    ),
    PlantedCase(
        label="url_double_slash_gitlab_subgroup",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"https://{FAKE_HOST}/{FAKE_NS}//zqsub//{FAKE_REPO}//-/merge_requests/42",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, "zqsub", FAKE_REPO),
        must_have=("https://forge.example.com/org/project/-/merge_requests/42",),
    ),
    PlantedCase(
        label="url_double_slash_repos_api",
        inp=PipelineInput(body="https://github.com/repos//zqotherns/zqotherrepo"),
        expected_status="WRITTEN",
        markers=("zqotherns", "zqotherrepo"),
        must_have=("https://forge.example.com/repos/org/project",),
    ),
    PlantedCase(
        label="url_double_slash_users_api",
        inp=PipelineInput(body="https://github.com/users//zqsomeone9"),
        expected_status="WRITTEN",
        markers=("zqsomeone9",),
        must_have=("https://forge.example.com/users/org",),
    ),
    PlantedCase(
        label="url_double_slash_in_url_key",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            url="https://git.zqother.io/zqotherns//zqotherrepo",
            body="hello",
        ),
        expected_status="WRITTEN",
        markers=("git.zqother.io", "zqotherns", "zqotherrepo"),
        must_have=("https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_glab_api_project_id_without_prefix_in_comment",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Repro: `glab api projects/{FAKE_PID}/merge_requests/42`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("glab api projects/2001/merge_requests/42",),
    ),
    PlantedCase(
        label="url_glab_api_project_id_leading_slash",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Run `glab api /projects/{FAKE_PID}/pipelines`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("glab api /projects/2001/pipelines",),
    ),
    PlantedCase(
        label="url_glab_api_project_id_query",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Run `glab api projects/{FAKE_PID}?statistics=true`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("glab api projects/2001?statistics=true",),
    ),
    PlantedCase(
        label="url_glab_api_project_id_in_call_args",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            api_path=f"projects/{FAKE_PID}/merge_requests/42/notes",
            body="ok",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("projects/2001/merge_requests/42/notes",),
    ),
    PlantedCase(
        label="url_api_v3_project_id_in_comment",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"API: https://{FAKE_HOST}/api/v3/projects/{FAKE_PID}/issues",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID), FAKE_HOST),
        must_have=("https://forge.example.com/api/v3/projects/2001/issues",),
    ),
    PlantedCase(
        label="url_api_v4_project_id_only_in_comment",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"URL https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/jobs/99",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID), FAKE_HOST),
        must_have=("https://forge.example.com/api/v4/projects/2001/jobs/99",),
    ),
    PlantedCase(
        label="url_api_v4_project_id_with_bare_prose_id",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"URL https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/jobs/99 failed on project {FAKE_PID}.",
        ),
        expected_status="REFUSED",
        expected_error_prefix="residual id 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="url_api_v4_project_id_in_repo_object_and_comment",
        inp=PipelineInput(
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
        expected_status="WRITTEN",
        markers=(str(FAKE_PID), FAKE_HOST, FAKE_REPO),
        must_have=("https://forge.example.com/api/v4/projects/2001/merge_requests/42",),
    ),
    PlantedCase(
        label="url_api_v4_project_id_in_stdin",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            stdin=f"POST /api/v4/projects/{FAKE_PID}/notes",
            body="ok",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("POST /api/v4/projects/2001/notes",),
    ),
    PlantedCase(
        label="url_api_v4_project_id_in_stderr",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            stderr=f"403 GET https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/members",
            body="ok",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID), FAKE_HOST),
        must_have=("403 GET https://forge.example.com/api/v4/projects/2001/members",),
    ),
    PlantedCase(
        label="url_scheme_led_rfc1918_ipv4_and_prose_repo",
        inp=PipelineInput(
            remote="",
            body="Mirror at http://10.42.1.9:8080/zqipns/zqipproj (see zqipproj)",
        ),
        expected_status="WRITTEN",
        markers=("10.42.1.9", "zqipproj"),
        must_have=("Mirror at http://forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_ftp_private_mirror_and_prose_repo",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Artifacts at ftp://{FAKE_HOST}/{FAKE_NS}/zqftprepo (see zqftprepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqftprepo"),
        must_have=("Artifacts at ftp://forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_ftps_private_mirror_and_prose_repo",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Download ftps://{FAKE_HOST}/{FAKE_NS}/zqftprepo/dist.tgz (zqftprepo)",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqftprepo"),
        must_have=("Download ftps://forge.example.com/org/project (org)",),
    ),
    PlantedCase(
        label="url_git_plus_https_pip_install",
        inp=PipelineInput(
            body=f"pip install git+https://github.com/{FAKE_NS}/{FAKE_REPO}.git@main",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_NS, FAKE_REPO),
        must_have=("pip install git+https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_schemeless_host_seen_led_in_same_fixture",
        inp=PipelineInput(
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
        expected_status="WRITTEN",
        markers=("git.zqseen.internal", "zqfirstrepo", "zqsecondrepo"),
        must_have=("Mirror forge.example.com/org/project (see project)",),
    ),
    PlantedCase(
        label="url_in_call_args",
        inp=PipelineInput(
            argv_extra=("-f", f"body=See https://{FAKE_HOST}/zqargns/zqargrepo?u={FAKE_LOGIN}"),
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, "zqargns", "zqargrepo", FAKE_LOGIN),
        must_have=("body=See https://forge.example.com/org/project",),
    ),
    PlantedCase(
        label="url_in_top_kwargs",
        inp=PipelineInput(
            top_kwargs={
                "note": f"https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}/pull/1 by {FAKE_LOGIN}"
            },
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, FAKE_REPO, FAKE_LOGIN),
        must_have=("https://forge.example.com/org/project/pull/1 by author",),
    ),
    PlantedCase(
        label="email_noreply_github_id_plus_login",
        inp=PipelineInput(
            body=f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN}@users.noreply.github.com>",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, "github.com"),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_github_login_only",
        inp=PipelineInput(
            body=f"Co-authored-by: Dev <{FAKE_LOGIN}@users.noreply.github.com>",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, "github.com"),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_gitlab_id_dash_login",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Co-authored-by: Dev <{FAKE_UID}-{FAKE_LOGIN}@users.noreply.gitlab.com>",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, "gitlab.com"),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_self_hosted_users",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN}@users.noreply.{FAKE_HOST}>",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, FAKE_HOST),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_self_hosted_without_users",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN}@noreply.{FAKE_HOST}>",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, FAKE_HOST),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_with_denylist_in_local",
        inp=PipelineInput(
            body=f"Co-authored-by: X <12345+{FAKE_DENY}@users.noreply.github.com>",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_DENY, "github.com"),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_noreply_uppercase",
        inp=PipelineInput(
            body=f"Co-authored-by: Dev <{FAKE_UID}+{FAKE_LOGIN.upper()}@USERS.NOREPLY.GITHUB.COM>",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, "github.com"),
        must_have=("author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_system_noreply_self_hosted",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"From: GitLab <noreply@{FAKE_HOST}>",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST,),
        must_have=("noreply@forge.example.com",),
    ),
    PlantedCase(
        label="email_mailto_noreply",
        inp=PipelineInput(
            body=f"Email mailto:{FAKE_UID}+{FAKE_LOGIN}@users.noreply.github.com",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, "github.com"),
        must_have=("mailto:author@users.noreply.forge.example.com",),
    ),
    PlantedCase(
        label="email_mailto_personal_refused",
        inp=PipelineInput(body="Email [me](mailto:jane.real@privatecorp.com)"),
        expected_status="REFUSED",
        expected_error_prefix="generic class mailbox at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="email_personal_corporate_domain_refused",
        inp=PipelineInput(body="Signed-off-by: Dev <dev.person@zqcorp.io>"),
        expected_status="REFUSED",
        expected_error_prefix="generic class mailbox at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="email_personal_git_trailer_example_tld",
        inp=PipelineInput(body=f"Signed-off-by: Someone <{FAKE_LOGIN}@zqcorp.example>"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("Signed-off-by: Someone <author@zqcorp.example>",),
    ),
    PlantedCase(
        label="email_login_at_example_com",
        inp=PipelineInput(body=f"mail {FAKE_LOGIN}@example.com"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("mail author@example.com",),
    ),
    PlantedCase(
        label="email_first_login_plus_tag_example_com",
        inp=PipelineInput(body=f"mail first.{FAKE_LOGIN}+tag@example.com"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("mail first.author+tag@example.com",),
    ),
    PlantedCase(
        label="email_sp_login_upper_example_com",
        inp=PipelineInput(body=f"mail SP-{FAKE_LOGIN.upper()}@example.com"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("mail SP-author@example.com",),
    ),
    PlantedCase(
        label="email_login_at_sub_example_com_refused",
        inp=PipelineInput(body=f"Mail {FAKE_LOGIN}@sub.example.com"),
        expected_status="REFUSED",
        expected_error_prefix="generic class mailbox at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="email_private_with_port_colon_refused",
        inp=PipelineInput(body="contact real.person@private-mail.org:25"),
        expected_status="REFUSED",
        expected_error_prefix="generic class mailbox at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="email_private_with_word_colon_refused",
        inp=PipelineInput(body="contact real.person@private-mail.org:hello"),
        expected_status="REFUSED",
        expected_error_prefix="generic class mailbox at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="invented_sp_login_literal",
        inp=PipelineInput(body=f"see SP-{FAKE_LOGIN.upper()} here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("see SP-author here",),
    ),
    PlantedCase(
        label="invented_sp_fix_login_literal",
        inp=PipelineInput(body=f"see SP-FIX-{FAKE_LOGIN.upper()} here"),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN,),
        must_have=("see SP-FIX-author here",),
    ),
    PlantedCase(
        label="invented_sp_repo_literal",
        inp=PipelineInput(body=f"see SP-{FAKE_REPO.upper()} here"),
        expected_status="WRITTEN",
        markers=(FAKE_REPO,),
        must_have=("see SP-project here",),
    ),
    PlantedCase(
        label="invented_sp_ns_literal",
        inp=PipelineInput(body=f"see SP-{FAKE_NS.upper()} here"),
        expected_status="WRITTEN",
        markers=(FAKE_NS,),
        must_have=("see SP-org here",),
    ),
    PlantedCase(
        label="invented_sp_uid_literal_refused",
        inp=PipelineInput(body=f"see SP-{FAKE_UID} here"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="invented_sp_denylist_literal_refused",
        inp=PipelineInput(body=f"see SP-{FAKE_DENY.upper()} here"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_linux_home_known_login_refused",
        inp=PipelineInput(body=f'File "/home/{FAKE_LOGIN}/work/{FAKE_REPO}/app.py", line 9'),
        expected_status="REFUSED",
        expected_error_prefix="generic class home_path at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_macos_users_known_login_refused",
        inp=PipelineInput(body=f'File "/Users/{FAKE_LOGIN}/dev/{FAKE_REPO}/app.py", line 12'),
        expected_status="REFUSED",
        expected_error_prefix="generic class home_path at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_macos_users_unknown_home_refused",
        inp=PipelineInput(body='File "/Users/janesmith/src/app.py", line 42'),
        expected_status="REFUSED",
        expected_error_prefix="generic class home_path at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_linux_home_unknown_user_refused",
        inp=PipelineInput(body="Error at /home/alice_local/src/main.py:42"),
        expected_status="REFUSED",
        expected_error_prefix="generic class home_path at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_windows_users_known_login_redacted",
        inp=PipelineInput(
            body=f'File "C:\\Users\\{FAKE_LOGIN}\\work\\{FAKE_REPO}\\app.py", line 12'
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN, FAKE_REPO),
        must_have=(r"C:\Users\author\work\project\app.py",),
    ),
    PlantedCase(
        label="path_tilde_repo_denylist_refused",
        inp=PipelineInput(body=f"Edited ~/work/{FAKE_REPO}/src/{FAKE_DENY}.py"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="path_relative_ns_repo_redacted",
        inp=PipelineInput(body=f"Modified src/{FAKE_NS}/{FAKE_REPO}/mod.py"),
        expected_status="WRITTEN",
        markers=(FAKE_NS, FAKE_REPO),
        must_have=("Modified src/org/project/mod.py",),
    ),
    PlantedCase(
        label="ci_log_stderr_denylist_refused",
        inp=PipelineInput(
            remote=GL_REMOTE,
            stderr=f"fatal: could not read {FAKE_DENY} on {FAKE_HOST}",
        ),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stderr",
    ),
    PlantedCase(
        label="ci_log_stderr_git_push_error_redacted",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            stderr=(
                f"remote: Permission to {FAKE_NS}/{FAKE_REPO}.git denied to {FAKE_LOGIN}.\n"
                f"fatal: unable to access 'https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}.git/': 403"
            ),
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, FAKE_REPO, FAKE_LOGIN),
        must_have=("Permission to org/project.git denied to author.",),
    ),
    PlantedCase(
        label="ci_log_stdin_private_url_and_login_redacted",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            stdin=f"Merge https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}/pull/1 by {FAKE_LOGIN}",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, FAKE_REPO, FAKE_LOGIN),
        must_have=("Merge https://forge.example.com/org/project/pull/1 by author",),
    ),
    PlantedCase(
        label="ci_log_diff_private_url_and_denylist_refused",
        inp=PipelineInput(
            remote=GL_REMOTE,
            raw_text_stdout=f"diff --git a/README.md b/README.md\n+# See https://{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO} ({FAKE_DENY})\n",
        ),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at exchanges[0].stdout",
    ),
    PlantedCase(
        label="ci_log_diff_home_path_refused",
        inp=PipelineInput(
            raw_text_stdout="diff --git a/cfg.ini b/cfg.ini\n+root = /home/alice_local/data\n",
        ),
        expected_status="REFUSED",
        expected_error_prefix="generic class home_path at exchanges[0].stdout",
    ),
    PlantedCase(
        label="ci_log_diff_noreply_email_redacted",
        inp=PipelineInput(
            raw_text_stdout=f"diff --git a/AUTHORS b/AUTHORS\n+Dev <{FAKE_UID}+{FAKE_LOGIN}@users.noreply.github.com>\n",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_UID), FAKE_LOGIN, "github.com"),
        must_have=("+Dev <author@users.noreply.forge.example.com>",),
    ),
    PlantedCase(
        label="ci_log_public_ipv4_refused",
        inp=PipelineInput(body="Connected to runner 45.33.32.156:443"),
        expected_status="REFUSED",
        expected_error_prefix="generic class public_ipv4 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="ci_log_graphql_legacy_node_id_refused",
        inp=PipelineInput(body=f"GraphQL error: could not resolve {FAKE_LEGACY_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="ci_log_graphql_new_u_node_id_refused",
        inp=PipelineInput(body=f"GraphQL error: could not resolve {FAKE_NEW_NODE}"),
        expected_status="REFUSED",
        expected_error_prefix="residual id 1 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="ci_log_deny_numeric_legacy_node_id_refused",
        inp=PipelineInput(body=f"GraphQL error: {FAKE_DENY_LEGACY}"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="ci_log_deny_numeric_new_u_node_id_refused",
        inp=PipelineInput(body=f"GraphQL error: {FAKE_DENY_NEW}"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="case_field_with_login_refused",
        inp=PipelineInput(case=f"pr_{FAKE_LOGIN}"),
        expected_status="REFUSED",
        expected_error_prefix="residual login 1 at case",
    ),
    PlantedCase(
        label="case_field_with_repo_refused",
        inp=PipelineInput(case=f"pr_{FAKE_REPO}"),
        expected_status="REFUSED",
        expected_error_prefix="residual segment 2 at case",
    ),
    PlantedCase(
        label="case_field_with_host_refused",
        inp=PipelineInput(forge="gitlab", remote=GL_REMOTE, case=f"pr_{FAKE_HOST}"),
        expected_status="REFUSED",
        expected_error_prefix="residual host 2 at case",
    ),
    PlantedCase(
        label="case_field_with_denylist_refused",
        inp=PipelineInput(case=f"pr_{FAKE_DENY}"),
        expected_status="REFUSED",
        expected_error_prefix="deny-list entry 1 at case",
    ),
    PlantedCase(
        label="dropped_key_assignees_login_redacted",
        inp=PipelineInput(
            extra_item={"assignees": [{"login": "zqassignee9", "id": 55443322}]},
            body="Assigned to zqassignee9 for triage",
        ),
        expected_status="WRITTEN",
        markers=("zqassignee9", "55443322"),
        must_have=("Assigned to author-2 for triage",),
    ),
    PlantedCase(
        label="dropped_key_mergedby_login_redacted",
        inp=PipelineInput(
            extra_item={"mergedBy": {"login": "zqmerger8", "id": 66554433}},
            body="Merged by zqmerger8 yesterday",
        ),
        expected_status="WRITTEN",
        markers=("zqmerger8", "66554433"),
        must_have=("Merged by author-2 yesterday",),
    ),
    PlantedCase(
        label="graphql_resolvedby_and_message_headline",
        inp=PipelineInput(
            extra_item={
                "resolvedBy": {"login": FAKE_LOGIN_2, "id": 63918499},
                "messageHeadline": f"Fix requested by @{FAKE_LOGIN_2}",
            },
            body="ok",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_LOGIN_2, "63918499"),
        must_have=("Fix requested by @author-2",),
    ),
    PlantedCase(
        label="needle_err_short_dropped_key_login",
        inp=PipelineInput(extra_item={"assignees": [{"login": "sam"}]}),
        expected_status="NEEDLE_ERR",
        expected_error_prefix="needle login 2: too short",
    ),
    PlantedCase(
        label="needle_err_placeholder_repo_project",
        inp=PipelineInput(
            ns="mygroup",
            repo="project",
            remote="git@github.com:mygroup/project.git",
        ),
        expected_status="NEEDLE_ERR",
        expected_error_prefix="needle segment 2: a class word or placeholder",
    ),
    PlantedCase(
        label="needle_err_short_3char_repo_cli",
        inp=PipelineInput(
            ns="mygroup",
            repo="cli",
            remote="git@github.com:mygroup/cli.git",
        ),
        expected_status="NEEDLE_ERR",
        expected_error_prefix="needle segment 2: too short",
    ),
    PlantedCase(
        label="mention_only_mark_with_bare_word_refused",
        inp=PipelineInput(body="cc @mark please mark this ready"),
        expected_status="REFUSED",
        expected_error_prefix="residual login 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="mention_only_mark_with_hyphen_refused",
        inp=PipelineInput(body="cc @mark please fix the mark-down rendering"),
        expected_status="REFUSED",
        expected_error_prefix="residual login 2 at exchanges[0].stdout[0].body",
    ),
    PlantedCase(
        label="mention_only_check_with_underscore_refused",
        inp=PipelineInput(body="cc @check please run check_status"),
        expected_status="REFUSED",
        expected_error_prefix="residual login 2 at exchanges[0].stdout[0].body",
    ),
    *(
        PlantedCase(
            label=f"r10_userinfo_empty_password_{name}",
            inp=PipelineInput(
                forge="gitlab",
                remote="",
                body=(
                    f"Clone https://{user}:@{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2} (see {FAKE_REPO_2})"
                ),
            ),
            expected_status="WRITTEN",
            markers=(user, FAKE_HOST_2, FAKE_REPO_2),
            must_have=("Clone https://forge.example.com/org/project (see project)",),
        )
        for name, user in (
            ("x_access_token", "x-access-token"),
            ("gitlab_ci_token", "gitlab-ci-token"),
            ("oauth2_token", "oauth2-token"),
            ("deploy_key_1", "deploy-key-1"),
            ("first_last_dev", "first-last.dev"),
        )
    ),
    *(
        PlantedCase(
            label=f"r10_userinfo_hyphen_user_password_char_{name}",
            inp=PipelineInput(
                forge="gitlab",
                remote="",
                body=(
                    f"Fetch https://ci-bot:pw{char}x@{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2} "
                    f"(see {FAKE_REPO_2})"
                ),
            ),
            expected_status="WRITTEN",
            markers=("ci-bot", FAKE_HOST_2, FAKE_REPO_2),
            must_have=("Fetch https://forge.example.com/org/project (see project)",),
        )
        for name, char in (
            ("tilde", "~"),
            ("bang", "!"),
            ("dollar", "$"),
            ("amp", "&"),
            ("squote", "'"),
            ("lparen", "("),
            ("rparen", ")"),
            ("star", "*"),
            ("comma", ","),
            ("semi", ";"),
            ("eq", "="),
            ("colon", ":"),
        )
    ),
    PlantedCase(
        label="r10_userinfo_star_password_on_kept_public_host",
        inp=PipelineInput(
            body="Fetch https://x-access-token:pw*1@docs.python.org/3/library/re.html now",
        ),
        expected_status="WRITTEN",
        markers=("x-access-token", "pw*1"),
        must_have=("Fetch https://forge.example.com/org/project now",),
    ),
    PlantedCase(
        label="r10_userinfo_quote_password_on_kept_public_host",
        inp=PipelineInput(
            body="Fetch https://x-access-token:pw'1@developer.mozilla.org/en-US/docs/ now",
        ),
        expected_status="WRITTEN",
        markers=("x-access-token", "pw'1"),
        must_have=("Fetch https://forge.example.com/org/project/ now",),
    ),
    PlantedCase(
        label="r10_userinfo_dotted_user_hyphen_password",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"Clone https://first.last:my-pass@{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2} ({FAKE_REPO_2})",
        ),
        expected_status="WRITTEN",
        markers=("first.last", "my-pass", FAKE_HOST_2, FAKE_REPO_2),
        must_have=("Clone https://forge.example.com/org/project (project)",),
    ),
    PlantedCase(
        label="r10_query_string_with_at_sign_and_prose_host",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"See https://{FAKE_HOST_2}?contact=alice@example.com (host {FAKE_HOST_2})",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2,),
        must_have=("See https://forge.example.com",),
    ),
    PlantedCase(
        label="r10_adjacent_angle_urls_and_mailbox",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"See <https://{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2}><alice@example.com> now",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=("See <https://forge.example.com/org/project>",),
    ),
    PlantedCase(
        label="r10_diff_scp_added_line_git_user",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            raw_text_stdout=f"diff --git a/sub b/sub\n+git@{FAKE_HOST_2}:{FAKE_NS}/{FAKE_REPO_2}.git\n",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=("+git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="r10_diff_scp_removed_line_named_user",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            raw_text_stdout=f"diff --git a/sub b/sub\n-alice@{FAKE_HOST_2}:{FAKE_NS}/{FAKE_REPO_2}.git\n",
        ),
        expected_status="WRITTEN",
        markers=("alice", FAKE_HOST_2, FAKE_REPO_2),
        must_have=("-git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="r10_diff_scp_added_line_named_user",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            raw_text_stdout=f"diff --git a/sub b/sub\n+bob@{FAKE_HOST_2}:{FAKE_NS}/{FAKE_REPO_2}.git\n",
        ),
        expected_status="WRITTEN",
        markers=("bob", FAKE_HOST_2, FAKE_REPO_2),
        must_have=("+git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="r10_diff_scp_removed_line_named_user_on_example_host",
        inp=PipelineInput(
            raw_text_stdout="diff --git a/sub b/sub\n-alice@example.com:org/repo.git\n",
        ),
        expected_status="WRITTEN",
        markers=("alice",),
        must_have=("-git@forge.example.com:org/project.git",),
    ),
    PlantedCase(
        label="r10_scp_plus_in_user_name",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"Clone ci+bot@{FAKE_HOST_2}:{FAKE_NS}/{FAKE_REPO_2}.git for {FAKE_REPO_2}",
        ),
        expected_status="WRITTEN",
        markers=("ci+bot", FAKE_HOST_2, FAKE_REPO_2),
        must_have=("Clone git@forge.example.com:org/project.git for project",),
    ),
    PlantedCase(
        label="r10_glab_api_quoted_header_flag",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Repro: `glab api -H 'Accept: application/json' /projects/{FAKE_PID}/issues`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("Repro: `glab api -H 'Accept: application/json' /projects/2001/issues`",),
    ),
    PlantedCase(
        label="r10_glab_api_double_quoted_field_flag",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f'Run `glab api -f "body=hello world" projects/{FAKE_PID}/notes`',
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=('Run `glab api -f "body=hello world" projects/2001/notes`',),
    ),
    PlantedCase(
        label="r10_glab_api_raw_field_flag",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Run `glab api --raw-field 'title=Fix bug' projects/{FAKE_PID}/merge_requests`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("Run `glab api --raw-field 'title=Fix bug' projects/2001/merge_requests`",),
    ),
    PlantedCase(
        label="r10_glab_api_header_equals_flag",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Run `glab api --header='PRIVATE-TOKEN: x' projects/{FAKE_PID}/issues`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("Run `glab api --header='PRIVATE-TOKEN: x' projects/2001/issues`",),
    ),
    PlantedCase(
        label="r10_glab_api_method_flag_in_text",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Repro: `glab api -X POST /projects/{FAKE_PID}/notes`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("Repro: `glab api -X POST /projects/2001/notes`",),
    ),
    PlantedCase(
        label="r10_glab_api_unbalanced_quote_falls_back_safely",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Run `glab api -H 'Accept: x projects/{FAKE_PID}/issues`",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("projects/2001/issues",),
    ),
    PlantedCase(
        label="r10_glab_api_full_argv_list",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            call_args=("glab", "api", f"/projects/{FAKE_PID}/jobs"),
            body="ok",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("/projects/2001/jobs",),
    ),
    PlantedCase(
        label="r10_web_projects_numeric_merge_request_path",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"See https://{FAKE_HOST}/projects/{FAKE_PID}/merge_requests/42 for context",
        ),
        expected_status="WRITTEN",
        markers=(str(FAKE_PID),),
        must_have=("See https://forge.example.com/projects/project/merge_requests/42 for context",),
    ),
    PlantedCase(
        label="r10_protocol_relative_json_string_value",
        inp=PipelineInput(
            body=json.dumps({"cdn": f"//{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2}/app.js"}),
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=('{"cdn": "//forge.example.com/org/project"}',),
    ),
    PlantedCase(
        label="r10_protocol_relative_script_src",
        inp=PipelineInput(
            body=f'<script src="//{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2}/app.js"></script>',
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=('<script src="//forge.example.com/org/project"></script>',),
    ),
    PlantedCase(
        label="r10_protocol_relative_css_url",
        inp=PipelineInput(
            body=f"a {{ background: url(//{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2}/bg.png) }}",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=("a { background: url(//forge.example.com/org/project) }",),
    ),
    PlantedCase(
        label="r10_protocol_relative_markdown_link",
        inp=PipelineInput(
            body=f"See [x](//{FAKE_HOST_2}/{FAKE_NS}/{FAKE_REPO_2}/page) now",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=("See [x](//forge.example.com/org/project) now",),
    ),
    PlantedCase(
        label="r10_top_args_projects_path_with_api_v4_kwargs",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body="ok",
            top_args=(f"projects/{FAKE_PID}/jobs",),
            top_kwargs={"ref": f"https://{FAKE_HOST}/api/v4/projects/{FAKE_PID}/issues"},
        ),
        expected_status="REFUSED",
        expected_error_prefix="residual id 2 at args[0]",
    ),
    PlantedCase(
        label="r10_italic_scp_remote_keeps_closing_underscore",
        inp=PipelineInput(
            forge="gitlab",
            remote="",
            body=f"Use _git@{FAKE_HOST_2}:{FAKE_NS}/{FAKE_REPO_2}.git_ now",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST_2, FAKE_REPO_2),
        must_have=("Use _git@forge.example.com:org/project.git_ now",),
    ),
    PlantedCase(
        label="r10_prose_double_slash_host_known_from_remote_is_replaced_as_a_token",
        inp=PipelineInput(
            forge="gitlab",
            remote=GL_REMOTE,
            body=f"Loaded from //{FAKE_HOST}/{FAKE_NS}/{FAKE_REPO}/v1.js",
        ),
        expected_status="WRITTEN",
        markers=(FAKE_HOST, FAKE_NS, FAKE_REPO),
        must_have=("Loaded from //forge.example.com/org/project/project",),
    ),
)


@pytest.mark.parametrize(
    "case",
    PLANTED_CORPUS,
    ids=[case.label for case in PLANTED_CORPUS],
)
def test_planted_realistic_leaks_never_survive(case: PlantedCase) -> None:
    """Each single-probe planted case asserts its ONE exact expected outcome and concrete output."""
    status, msg, fixture = _run_pipeline(case.inp)
    assert status == case.expected_status, f"{case.label}: got {status} ({msg!r})"
    if case.expected_status != "WRITTEN":
        assert fixture is None, case.label
        assert case.expected_error_prefix, f"{case.label}: missing expected_error_prefix"
        assert msg.startswith(case.expected_error_prefix), (
            f"{case.label}: expected prefix {case.expected_error_prefix!r}, got {msg!r}"
        )
        return
    assert fixture is not None, case.label
    blob = json.dumps(fixture, ensure_ascii=False)
    survivors = [m for m in case.markers if m.casefold() in blob.casefold()]
    assert survivors == [], f"{case.label}: survived={survivors}"
    stdout = str(fixture["calls"][0]["stdout"])
    parts = [blob, stdout, *(text for _path, text in rec_mod.scanned_strings(fixture))]
    if case.inp.raw_text_stdout is None:
        item = json.loads(stdout)[0]
        parts.append(str(item.get("body", "")))
    searchable = "\n".join(parts)
    for expected in case.must_have:
        assert expected in searchable, f"{case.label}: missing {expected!r} in {searchable!r}"
    if case.inp.title is not None:
        out_title = str(json.loads(fixture["calls"][0]["stdout"])[0]["title"])
        for expected in case.title_must_have:
            assert expected in out_title, (
                f"{case.label}: missing title {expected!r} in {out_title!r}"
            )
        for forbidden in case.title_must_not:
            assert forbidden.casefold() not in out_title.casefold(), (
                f"{case.label}: unwanted title {forbidden!r} in {out_title!r}"
            )


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
    expected_title: str | None = None
    title_must_have: tuple[str, ...] = ()
    title_must_not: tuple[str, ...] = ()


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
        exact_body=(
            "See https://docs.github.com/en/actions/using-workflows and "
            "https://docs.github.com/en/rest/pulls and "
            "https://docs.gitlab.com/ee/api/merge_requests.html"
        ),
    ),
    OrdinaryCase(
        label="public_docs_python_peps_readthedocs_mdn_crates_so_wikipedia",
        inp=_ordinary(
            body=(
                "Python: https://docs.python.org/3/library/pathlib.html? "
                "https://www.python.org/downloads/release/python-3120/ "
                "https://peps.python.org/pep-0723/\n"
                "RTD: https://readthedocs.org/projects/pytest/ "
                "https://pytest.readthedocs.io/en/stable/how-to/fixtures.html\n"
                "MDN: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429\n"
                "Rust: https://crates.io/crates/tokio https://docs.rs/tokio/latest/tokio/\n"
                "SO: https://stackoverflow.com/questions/419163/what-does-if-name-main-do\n"
                "Wiki: https://en.wikipedia.org/wiki/Quicksort "
                "https://en.wikipedia.org/wiki/Python_(programming_language)"
            )
        ),
        exact_body=(
            "Python: https://docs.python.org/3/library/pathlib.html? "
            "https://www.python.org/downloads/release/python-3120/ "
            "https://peps.python.org/pep-0723/\n"
            "RTD: https://readthedocs.org/projects/pytest/ "
            "https://pytest.readthedocs.io/en/stable/how-to/fixtures.html\n"
            "MDN: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429\n"
            "Rust: https://crates.io/crates/tokio https://docs.rs/tokio/latest/tokio/\n"
            "SO: https://stackoverflow.com/questions/419163/what-does-if-name-main-do\n"
            "Wiki: https://en.wikipedia.org/wiki/Quicksort "
            "https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
    ),
    OrdinaryCase(
        label="trailing_exclamation_and_question_on_kept_urls",
        inp=_ordinary(
            body=(
                "Try https://example.com! Also http://localhost! "
                "Published https://pypi.org/project/ruff/! "
                "Checked https://example.com? And https://docs.python.org/3/library/re.html?"
            )
        ),
        exact_body=(
            "Try https://example.com! Also http://localhost! "
            "Published https://pypi.org/project/ruff/! "
            "Checked https://example.com? And https://docs.python.org/3/library/re.html?"
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
        exact_body=(
            "Check `https://example.com` and `http://localhost` and `https://127.0.0.1:8080/health`.\n"
            "|Tool|Link|\n|---|---|\n"
            "|Ruff|https://pypi.org/project/ruff/|\n"
            "|Docs|https://docs.github.com/en/actions|\n"
            "|Py|https://docs.python.org/3/library/re.html|\n"
        ),
    ),
    OrdinaryCase(
        label="github_url_with_dunder_init_anchor",
        inp=_ordinary(
            body="See https://github.com/python/cpython/blob/main/Lib/pathlib.py#__init__ for details."
        ),
        exact_body="See https://forge.example.com/org/project/blob/main/Lib/pathlib.py for details.",
    ),
    OrdinaryCase(
        label="repo_year_paths_projects_2026_and_2025",
        inp=_ordinary(
            body=(
                "Updated projects/2026/q1.md and /projects/2025/architecture.md "
                "and docs/projects/2026/plan.md (written in 2025)."
            )
        ),
        exact_body=(
            "Updated projects/2026/q1.md and /projects/2025/architecture.md "
            "and docs/projects/2026/plan.md (written in 2025)."
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
        exact_body=(
            "Per GitLab docs: `curl https://gitlab.example.com/api/v4/projects/1/merge_requests`\n"
            "Mock endpoint: `GET /api/v4/projects/42/issues`\n"
            "Example: `curl https://gitlab.example.com/api/v4/projects/1234/issues`"
        ),
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
        exact_body=(
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
        ),
    ),
    OrdinaryCase(
        label="go_cpp_single_line_comments_without_and_with_space",
        inp=_ordinary(body="```go\n//config.dev/env/prod\n// config.dev/env/prod\nvar x = 1\n```"),
        exact_body="```go\n//config.dev/env/prod\n// config.dev/env/prod\nvar x = 1\n```",
    ),
    OrdinaryCase(
        label="yaml_github_actions_workflow_and_shell_snippet",
        inp=_ordinary(
            body=(
                "```yaml\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - uses: actions/setup-python@v5\n"
                "        with:\n          python-version: '3.12'\n"
                "      - run: pytest -q tests/unit\n```\n"
                "```sh\ncurl -sSf https://example.com/health > /dev/null 2>&1\n```"
            )
        ),
        exact_body=(
            "```yaml\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: actions/setup-python@v5\n"
            "        with:\n          python-version: '3.12'\n"
            "      - run: pytest -q tests/unit\n```\n"
            "```sh\ncurl -sSf https://example.com/health > /dev/null 2>&1\n```"
        ),
    ),
    OrdinaryCase(
        label="js_node_stack_trace",
        inp=_ordinary(
            body=(
                "TypeError: Cannot read properties of undefined (reading 'map')\n"
                "    at renderList (src/components/List.tsx:42:18)\n"
                "    at processTicksAndRejections (node:internal/process/task_queues:95:5)"
            )
        ),
        exact_body=(
            "TypeError: Cannot read properties of undefined (reading 'map')\n"
            "    at renderList (src/components/List.tsx:42:18)\n"
            "    at processTicksAndRejections (node:internal/process/task_queues:95:5)"
        ),
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
        exact_body=(
            "Use @dataclass, @property, @Override, @Deprecated, @pytest.fixture and @lru_cache(maxsize=64).\n"
            "Import `@types/node`, `@vue/cli`, `@babel/core`, `@actions/checkout`, `@tanstack/react-query`.\n"
            "```ts\nconst url = this.base/api/v1;\nimport H from 'components.tsx/Header/index';\n```"
        ),
    ),
    OrdinaryCase(
        label="dependabot_pr_template",
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
        label="renovate_with_actions_checkout_title_and_docs_github",
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
        expected_title="chore(deps): update @actions/checkout action to v4",
    ),
    OrdinaryCase(
        label="short_stem_ci_bot_preserves_english_ci_prose",
        inp=_ordinary(
            login="ci[bot]",
            uid=52143690,
            author="ci[bot]",
            body="CI pipeline passed! Re-run with `@ci retry` or @ci rerun.",
        ),
        exact_body="CI pipeline passed! Re-run with `@author retry` or @author rerun.",
    ),
    OrdinaryCase(
        label="github_actions_bot_with_actions_login",
        inp=_ordinary(
            login="actions",
            body="Triggered by github-actions[bot]; check the actions tab",
        ),
        exact_body="Triggered by github-actions[bot]; check the author tab",
    ),
    OrdinaryCase(
        label="github_actions_bot_with_github_login_and_runner",
        inp=_ordinary(
            login="github",
            body="by github-actions[bot] on github-actions-runner",
        ),
        exact_body="by github-actions[bot] on author-actions-runner",
    ),
    OrdinaryCase(
        label="short_third_party_url_a_io",
        inp=_ordinary(body="See https://a.io/foo"),
        exact_body="See https://forge.example.com/org",
    ),
    OrdinaryCase(
        label="repoach_bot_with_repoach_repo",
        inp=_ordinary(
            ns="repoachhq",
            repo="repoach",
            remote="git@github.com:repoachhq/repoach.git",
            body="Reviewed by repoach-bot on repoach PR",
        ),
        exact_body="Reviewed by repoach-bot on project PR",
    ),
    OrdinaryCase(
        label="common_word_login_main",
        inp=_ordinary(
            login="main",
            body="Merge main into main-fix and update main_branch and domain",
        ),
        exact_body="Merge author into author-fix and update author_branch and domain",
    ),
    OrdinaryCase(
        label="common_word_login_test",
        inp=_ordinary(
            login="test",
            body="Run test and test_foo and test-bar and pytest",
        ),
        exact_body="Run author and author_foo and author-bar and pytest",
    ),
    OrdinaryCase(
        label="common_word_login_code",
        inp=_ordinary(
            login="code",
            body="Check code, code-review, code_style, and vscode",
        ),
        exact_body="Check author, author-review, author_style, and vscode",
    ),
    OrdinaryCase(
        label="common_word_login_docs",
        inp=_ordinary(
            login="docs",
            body="Update docs and docs-ci and docs/README.md",
        ),
        exact_body="Update author and author-ci and author/README.md",
    ),
    OrdinaryCase(
        label="common_word_login_python",
        inp=_ordinary(
            login="python",
            body="Bump python and python-3.12 and python_version and cpython",
        ),
        exact_body="Bump author and author-3.12 and author_version and cpython",
    ),
    OrdinaryCase(
        label="common_word_login_true",
        inp=_ordinary(
            login="true",
            body="Returns true when flag-true is set",
        ),
        exact_body="Returns author when flag-author is set",
    ),
    OrdinaryCase(
        label="common_word_login_repo_preserves_cli_placeholder",
        inp=_ordinary(
            login="repo",
            body="Check repo and repo-name and repos/:owner/:repo",
        ),
        exact_body="Check author and author-name and repos/:owner/:repo",
    ),
    OrdinaryCase(
        label="class_word_login_author_kept",
        inp=_ordinary(login="author", body="looks good"),
        exact_body="looks good",
    ),
    OrdinaryCase(
        label="short_third_party_url_x_com",
        inp=_ordinary(body="See https://x.com/user/status/123"),
        exact_body="See https://forge.example.com/org/project",
    ),
    OrdinaryCase(
        label="mailto_example_com_and_port_like_file_path",
        inp=_ordinary(body="Contact mailto:someone@example.com; see foo.py:80/bar"),
        exact_body="Contact mailto:someone@example.com; see foo.py:80/bar",
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
        exact_body=(
            "```dockerfile\nFROM docker.io/library/python:3.12-slim\n"
            "COPY --from=ghcr.io/astral-sh/uv:0.4.0 /uv /bin/uv\n```\n"
            '```go\nimport (\n\t"golang.org/x/sync/errgroup"\n'
            '\t"k8s.io/client-go/kubernetes"\n)\n```\n'
            "See https://pkg.go.dev/golang.org/x/sync/errgroup and "
            "https://proxy.golang.org/ and https://registry.npmjs.org/typescript\n"
        ),
    ),
    OrdinaryCase(
        label="self_hosted_gitlab_empty_remote_with_docs_python_and_mdn",
        inp=_ordinary(
            forge="gitlab",
            remote="",
            url=f"https://{FAKE_HOST}/zqpuborg/zqpubrepo/-/merge_requests/1#note_1",
            body=(
                "See https://docs.python.org/3/library/re.html and "
                "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API."
            ),
        ),
        exact_body=(
            "See https://docs.python.org/3/library/re.html and "
            "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API."
        ),
    ),
    OrdinaryCase(
        label="github_empty_remote_with_docs_python_and_golang_import",
        inp=_ordinary(
            remote="",
            body="See https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup",
        ),
        exact_body="See https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup",
    ),
    OrdinaryCase(
        label="github_ssh_alias_with_third_party_links",
        inp=_ordinary(
            remote="mygh:zqpuborg/zqpubrepo.git",
            body=(
                "Upgrade python and git; see https://github.com/python/cpython/pull/1, "
                "https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup"
            ),
        ),
        exact_body=(
            "Upgrade python and git; see https://forge.example.com/org/project/pull/1, "
            "https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup"
        ),
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
        exact_body=(
            'File "src/repoach/cli/main.py", line 42, in run\n'
            "Adding repoach-0.1.0.dist-info/licenses/LICENSE\n"
            "Send test payload to demo@example.com.\n"
            "Local bind 127.0.0.1:8080, private 10.0.0.5, doc 203.0.113.10 and 198.51.100.7.\n"
            "Tested on v1.2.3.4 and foo==2.3.4.5 and 3.4.5.6.tar.gz.\n"
            "curl -sSf https://example.com/health > /dev/null 2>&1"
        ),
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
    *(
        OrdinaryCase(
            label=f"r10_code_line_comment_after_code_{name}",
            inp=_ordinary(body=f"```{lang}\n{code}\n```"),
            exact_body=f"```{lang}\n{code}\n```",
        )
        for name, lang, code in (
            ("go_assign", "go", "x := 1 //cfg.dev/env/prod"),
            ("go_brace", "go", "if x > 0 { //store.io/cache/hit"),
            ("go_return", "go", "return x //metrics.net/count/ok"),
            ("js_call", "ts", "const cfg = load() //cfg.dev/env/prod"),
            ("js_export", "ts", "export default cfg //app.io/entry/main"),
            ("c_brace", "c", "if (ok) { //config.dev/env/prod"),
            ("c_semicolon", "c", "int x = 1; //store.net/cache/l1"),
            ("c_line_start", "c", "//cfg.dev/env/prod"),
            ("c_doc_comment", "c", "///api.io/v2/schema"),
        )
    ),
    OrdinaryCase(
        label="r10_go_multi_function_with_inline_comments",
        inp=_ordinary(
            body=(
                "```go\npackage main\nfunc load() int {\n\tx := 1 //cfg.dev/env/prod\n"
                "\tif x > 0 { //store.io/cache/hit\n\t\treturn x //metrics.net/count/ok\n\t}\n"
                "\treturn 0\n}\n```"
            )
        ),
        exact_body=(
            "```go\npackage main\nfunc load() int {\n\tx := 1 //cfg.dev/env/prod\n"
            "\tif x > 0 { //store.io/cache/hit\n\t\treturn x //metrics.net/count/ok\n\t}\n"
            "\treturn 0\n}\n```"
        ),
    ),
    OrdinaryCase(
        label="r10_semicolon_free_ts_block_with_inline_comments",
        inp=_ordinary(
            body=(
                "```ts\nconst cfg = load() //cfg.dev/env/prod\nconst url = this.base/api/v1\n"
                "export default cfg //app.io/entry/main\n```"
            )
        ),
        exact_body=(
            "```ts\nconst cfg = load() //cfg.dev/env/prod\nconst url = this.base/api/v1\n"
            "export default cfg //app.io/entry/main\n```"
        ),
    ),
    OrdinaryCase(
        label="r10_go_diff_with_comments_year_projects_and_docs_python",
        inp=_ordinary(
            raw_text_stdout=(
                "diff --git a/projects/2026/main.go b/projects/2026/main.go\n"
                "--- a/projects/2026/main.go\n+++ b/projects/2026/main.go\n"
                "@@ -1,3 +1,4 @@\n+//cfg.dev/env/prod\n"
                "+// See https://docs.python.org/3/library/re.html\n"
                "+x := 1 //store.io/val/2026\n"
            )
        ),
        exact_body=(
            "diff --git a/projects/2026/main.go b/projects/2026/main.go\n"
            "--- a/projects/2026/main.go\n+++ b/projects/2026/main.go\n"
            "@@ -1,3 +1,4 @@\n+//cfg.dev/env/prod\n"
            "+// See https://docs.python.org/3/library/re.html\n"
            "+x := 1 //store.io/val/2026\n"
        ),
    ),
    OrdinaryCase(
        label="r10_italic_kept_scp_example_remote",
        inp=_ordinary(
            body="Example: _git@example.com:org/demo.git_ and __git@sub.example.org:team/app.git__."
        ),
        exact_body="Example: _git@example.com:org/demo.git_ and __git@sub.example.org:team/app.git__.",
    ),
)


@pytest.mark.parametrize("case", ORDINARY_CORPUS, ids=[c.label for c in ORDINARY_CORPUS])
def test_ordinary_public_prs_are_never_refused_or_corrupted(case: OrdinaryCase) -> None:
    """Ordinary public PR payloads write cleanly without false refusal or text/title corruption."""
    status, msg, fixture = _run_pipeline(case.inp)
    assert status == "WRITTEN", f"{case.label}: {status}: {msg}"
    assert fixture is not None
    stdout = fixture["calls"][0]["stdout"]
    if case.inp.raw_text_stdout is not None:
        body = stdout
    else:
        item = json.loads(stdout)[0]
        body = item["body"]
        if case.inp.title is not None:
            title = str(item["title"])
            assert case.expected_title is not None or case.title_must_have, (
                f"{case.label}: title set on input must be asserted"
            )
            if case.expected_title is not None:
                assert title == case.expected_title, case.label
            for expected in case.title_must_have:
                assert expected in title, f"{case.label}: missing title {expected!r} in {title!r}"
            for forbidden in case.title_must_not:
                assert forbidden.casefold() not in title.casefold(), (
                    f"{case.label}: unwanted title {forbidden!r} in {title!r}"
                )
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
        "offline_public_docs_python_and_wikipedia_parens",
        {
            "user": {"login": "author", "id": 1001},
            "body": (
                "See https://DOCS.PYTHON.ORG/3/library/re.html? and "
                "https://en.wikipedia.org/wiki/Python_(programming_language)"
            ),
        },
        (
            "See https://docs.python.org/3/library/re.html? and "
            "https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
        "author",
    ),
    (
        "offline_scp_example_userinfo_and_dunder_anchor",
        {
            "user": {"login": "author", "id": 1001},
            "body": (
                "Run alice@example.com:org/repo.git and "
                "https://github.com/python/cpython/blob/main/Lib/pathlib.py#__init__"
            ),
        },
        (
            "Run git@forge.example.com:org/project.git and "
            "https://forge.example.com/org/project/blob/main/Lib/pathlib.py"
        ),
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
