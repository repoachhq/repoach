"""Acceptance corpus of SP-RECORDER-REDACTS-BY-CLASS 0.17: one row, one input, one outcome.

Every row runs through the public ``record`` path of
``scripts/record_forge_fixture.py`` against a fake forge (live) and
through ``redact_fixture`` of ``scripts/redact_forge_fixtures.py``
(offline). Each row carries exactly one expected outcome:

- ``Written``: the exact text at the row's position (``body``, ``title``,
  raw ``stdout``, ``stderr``, ``stdin``, ``args``, ``kwargs`` or the
  exchange argv), the exact ``title`` when the input has one, named
  item fields such as ``headRefName``, and no planted marker surviving
  anywhere in the written fixture (casefolded);
- ``Refused``: the refusal's reason prefix, nothing written;
- ``Bounded``: past the interpreter's bound (0.21, B3), written as it
  stands or refused with the reason prefix, never a crash.

Sources: every row of ``tests/unit/test_forge_recording_regression_corpus.py``
(rounds 5 to 9), every planted survivor, refusal and corruption of
verifier round 10 (``tmp/verify10/``, ``tmp/REPORT-r10.md``) and new rows
generated along the 0.17 axes (host class, position, syntactic carrier,
ordinary code in eight languages). Values are synthetic.

The corpus is frozen: a row is never edited to match an implementation.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from repoach.forge import denylist as deny_list
from repoach.forge import recording as rec_mod
from repoach.forge.github import GitHubForge, comment_from_github
from repoach.forge.gitlab import GitLabForge, note_to_comment
from repoach.forge.protocol import Diff, Forge, diff_files
from repoach.forge.runner import CommandResult

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
FAKE_NS = "zqgroup"
FAKE_REPO = "zqmainrepo"
FAKE_UID = 74928163
FAKE_PID = 88442211
FAKE_DENY = "zqdenyval"
FAKE_DENY_ID = "91827364"
PLACEHOLDER = rec_mod.PLACEHOLDER_HOST


@dataclass(frozen=True)
class Sample:
    """One synthetic recording: the change request's answers and the recorder's inputs."""

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
    second_api_path: str | None = None
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


@dataclass(frozen=True)
class Written:
    """The fixture is written: the exact text at the row's position, title and named fields."""

    text: str
    title: str | None = None
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Refused:
    """The recorder refuses with this reason prefix and writes nothing."""

    prefix: str


@dataclass(frozen=True)
class Bounded:
    """Past the readers' bound, which is the interpreter's: written as it stands, or refused with this prefix."""

    prefix: str


@dataclass(frozen=True)
class Row:
    """One acceptance row: an id, its source probe, its input and its one outcome.

    A row is **planted** when it carries a marker that must not survive, when it is refused, or
    when it is a ``residual``: a planted shape that NG4 documents as accepted and that is
    therefore written as it stands. Every other row is ordinary text. ``url_field_class`` declares
    a row whose class is decided by a value only a ``URL_KEYS`` field names: the offline transform
    reads a committed fixture, where that field is already written, so the raw-text parity half
    cannot hold and is not judged (the fixed-point half still is).
    """

    id: str
    source: str
    inp: Sample
    outcome: Written | Refused | Bounded
    markers: tuple[str, ...] = ()
    residual: bool = False
    url_field_class: bool = False


ROWS: tuple[Row, ...] = (
    Row(
        id="reg_mention_known_gh",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Thanks @zqxjdoe7, merging now."),
        outcome=Written(text="Thanks @author, merging now."),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_mention_unknown_colleague_gh",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Thanks @zqxjdoe7, cc @zqreviewer for review."),
        outcome=Written(text="Thanks @author, cc @author-2 for review."),
        markers=("zqxjdoe7", "zqreviewer"),
    ),
    Row(
        id="reg_mention_gitlab_diff_suggestion",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="@zqxjdoe7 suggestion:\n```suggestion:-0+0\nreturn zqmainrepo\n```",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="@author suggestion:\n```suggestion:-0+0\nreturn project\n```"),
        markers=("zqxjdoe7", "zqmainrepo", "git.zqcorp.io"),
    ),
    Row(
        id="reg_mention_in_title",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(title="Fix bug reported by @zqxjdoe7"),
        outcome=Written(text="Looks good to me.", title="Fix bug reported by @author"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_mention_known_in_double_backticks",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Use ``@zqxjdoe7 `arg` `` here"),
        outcome=Written(text="Use ``@author `arg` `` here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_mention_unknown_in_double_backticks",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Use ``@zqcolleague `arg` `` here"),
        outcome=Written(text="Use ``@author-2 `arg` `` here"),
        markers=("zqcolleague",),
    ),
    Row(
        id="reg_mention_unknown_in_tilde_fence",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="~~~text\ncc @zqcolleague\n~~~"),
        outcome=Written(text="~~~text\ncc @author-2\n~~~"),
        markers=("zqcolleague",),
    ),
    Row(
        id="reg_mention_short_stem_bot_cla",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="Please run @cla check to verify signature.", login="cla[bot]", uid=44556677
        ),
        outcome=Written(text="Please run @bot check to verify signature."),
        markers=("cla[bot]", "@cla", "44556677"),
    ),
    Row(
        id="reg_mention_short_stem_bot_bob",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Please run @bob retry.", login="bob[bot]", uid=44556688),
        outcome=Written(text="Please run @bot retry."),
        markers=("bob[bot]", "@bob", "44556688"),
    ),
    Row(
        id="reg_mention_short_stem_bot_ci_mention_first",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="cc @ci please run",
            extra_items=({"user": {"login": "ci[bot]", "id": 61112222}, "body": "Running."},),
        ),
        outcome=Written(text="cc @bot please run"),
        markers=("ci[bot]", "@ci", "61112222"),
    ),
    Row(
        id="reg_mention_1char_bot_x",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Run @x check", login="x[bot]", uid=61112233),
        outcome=Written(text="Run @bot check"),
        markers=("x[bot]", "@x", "61112233"),
    ),
    Row(
        id="reg_mention_uppercase_bot_cla",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Triggered @CLA check", login="CLA[BOT]", uid=61112244),
        outcome=Written(text="Triggered @bot check"),
        markers=("cla[bot]", "@cla", "61112244"),
    ),
    Row(
        id="reg_gitlab_project_access_token_bot",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Automated merge",
            login="project_88442211_bot_a1b2c3d4e5f6",
            uid=61112255,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Automated merge"),
        markers=("88442211", "a1b2c3d4e5f6", "61112255"),
    ),
    Row(
        id="reg_mixed_sep_under_dash_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see _zqxjdoe7- here"),
        outcome=Written(text="see _author- here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_mixed_sep_dash_under_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see -zqxjdoe7_ here"),
        outcome=Written(text="see -author_ here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_mixed_sep_dot_under_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see .zqxjdoe7_ here"),
        outcome=Written(text="see .author_ here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_branch_github_feat_login_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="feat/zqxjdoe7/zqmainrepo-fix", base_ref="release/zqgroup-2026"),
        outcome=Written(text="Looks good to me."),
        markers=("zqxjdoe7", "zqmainrepo", "zqgroup"),
    ),
    Row(
        id="reg_branch_github_login_patch_1",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="zqxjdoe7/patch-1"),
        outcome=Written(text="Looks good to me."),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_branch_github_renovate_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="renovate/zqmainrepo-2.x"),
        outcome=Written(text="Looks good to me."),
        markers=("zqmainrepo",),
    ),
    Row(
        id="reg_branch_gitlab_source_branch_ns_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            head_ref="zqgroup/zqxjdoe7-wip",
        ),
        outcome=Written(text="Looks good to me."),
        markers=("zqgroup", "zqxjdoe7"),
    ),
    Row(
        id="reg_branch_sp_login_upper",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="SP-ZQXJDOE7-FIX"),
        outcome=Written(text="Looks good to me."),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_branch_sp_repo_upper",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="feat/SP-ZQMAINREPO"),
        outcome=Written(text="Looks good to me."),
        markers=("zqmainrepo",),
    ),
    Row(
        id="reg_branch_with_denylist_token",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="feat/zqdenyval-cleanup"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].headRefName"),
    ),
    Row(
        id="reg_branch_with_user_id",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="fix/user-74928163"),
        outcome=Written(text="Looks good to me.", fields=(("headRefName", "fix/user-1001"),)),
    ),
    Row(
        id="reg_branch_with_legacy_node_id",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="feat/MDQ6VXNlcjc0OTI4MTYz"),
        outcome=Written(text="Looks good to me.", fields=(("headRefName", "feat/U_1001"),)),
    ),
    Row(
        id="reg_branch_with_new_u_node_id",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(head_ref="feat/U_kgDOBHdQIw"),
        outcome=Written(text="Looks good to me.", fields=(("headRefName", "feat/U_1001"),)),
    ),
    Row(
        id="reg_node_legacy_in_url_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/MDQ6VXNlcjc0OTI4MTYz/profile"),
        outcome=Written(text="https://example.com/users/U_1001/profile"),
    ),
    Row(
        id="reg_node_legacy_alpha_prefix",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="userMDQ6VXNlcjc0OTI4MTYz"),
        outcome=Refused("residual id 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_node_legacy_suffixed",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="MDQ6VXNlcjc0OTI4MTYzsuffix"),
        outcome=Refused("residual id 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_node_legacy_padded_in_url_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/MDQ6VXNlcjQ4MTUxNjI=/profile", uid=4815162),
        outcome=Written(text="https://example.com/users/U_1001/profile"),
    ),
    Row(
        id="reg_node_new_u_in_url_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/U_kgDOBHdQIw/profile"),
        outcome=Written(text="https://example.com/users/U_1001/profile"),
    ),
    Row(
        id="reg_node_new_u_alpha_prefix",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="abcdefgU_kgDOBHdQIw"),
        outcome=Refused("residual id 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_node_deny_legacy_in_url_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/MDQ6VXNlcjkxODI3MzY0/profile"),
        outcome=Refused("deny-list entry 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_node_deny_new_u_in_url_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/U_kgDOBXkspA/profile"),
        outcome=Refused("deny-list entry 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_json_in_string_pure_login_value",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='{"note": "zqxjdoe7"}'),
        outcome=Written(text='{"note": "author"}'),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_json_in_string_pure_login_key",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='{"zqxjdoe7": 1}'),
        outcome=Written(text='{"author": 1}'),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_github_compare_fork",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="Diff: https://github.com/zqgroup/zqmainrepo/compare/main...zqxjdoe7:zqmainrepo:fix"
        ),
        outcome=Written(
            text="Diff: https://forge.example.com/org/project/compare/main...author:project:fix"
        ),
        markers=("zqxjdoe7", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_raw_githubusercontent",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="File: https://raw.githubusercontent.com/zqgroup/zqmainrepo/main/src/app.py"
        ),
        outcome=Written(text="File: https://forge.example.com/org/project"),
        markers=("zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_gist_github_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Gist: https://gist.github.com/zqxjdoe7/0123456789abcdef"),
        outcome=Written(text="Gist: https://forge.example.com/org/project"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_github_pages_login_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Docs: https://zqxjdoe7.github.io/zqmainrepo/guide.html"),
        outcome=Written(text="Docs: https://forge.example.com/org/project"),
        markers=("zqxjdoe7", "zqmainrepo"),
    ),
    Row(
        id="reg_url_avatars_githubusercontent_uid",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Avatar: https://avatars.githubusercontent.com/u/74928163?v=4"),
        outcome=Written(text="Avatar: https://forge.example.com/org/project"),
        markers=("74928163",),
    ),
    Row(
        id="reg_url_kept_host_path_with_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/users/zqxjdoe7/bio"),
        outcome=Written(text="https://example.com/users/author/bio"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_kept_host_subdomain_with_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://zqxjdoe7.example.com/bio"),
        outcome=Written(text="https://author.example.com/bio"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_kept_host_query_with_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/p?u=zqxjdoe7"),
        outcome=Written(text="https://example.com/p?u=author"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_kept_host_example_denylist_in_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://example.com/docs/zqdenyval/index.html"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_url_github_query_carrying_denylist",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://github.com/zqgroup/zqmainrepo/issues?q=zqdenyval"),
        outcome=Written(text="https://forge.example.com/org/project/issues"),
        markers=("zqdenyval", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_pypi_query_carrying_denylist",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="See https://pypi.org/project/ruff/?ref=zqdenyval"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_url_docs_github_query_carrying_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="See https://docs.github.com/en/actions?user=zqxjdoe7"),
        outcome=Written(text="See https://docs.github.com/en/actions?user=author"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_url_docs_github_url_encoded_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Ref https://docs.github.com/en/actions?q=%7a%71%78%6a%64%6f%65%37"),
        outcome=Refused("residual login 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_html_entity_encoded_denylist",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Note: &#122;qdenyval here"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_url_self_hosted_trailing_exclamation_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Check https://git.zqcorp.io/zqgroup/zqsecondrepo! (see zqsecondrepo docs)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Check https://forge.example.com/org/project! (see project docs)"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_trailing_question_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Did you mean https://git.zqcorp.io/zqgroup/zqsecondrepo? (see zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Did you mean https://forge.example.com/org/project? (see project)"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_trailing_semicolon_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See https://git.zqcorp.io/zqgroup/zqsecondrepo; zqsecondrepo owns it.",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="See https://forge.example.com/org/project; project owns it."),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_trailing_comma_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Updated https://git.zqcorp.io/zqgroup/zqsecondrepo, check zqsecondrepo CI.",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Updated https://forge.example.com/org/project, check project CI."),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_single_quotes_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Remote 'https://git.zqcorp.io/zqgroup/zqsecondrepo' (zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Remote 'https://forge.example.com/org/project' (project)"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_angle_brackets_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Link <https://git.zqcorp.io/zqgroup/zqsecondrepo> for zqsecondrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Link <https://forge.example.com/org/project> for project"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_double_punct_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Moved to https://git.zqcorp.io/zqgroup/zqsecondrepo?! Verify zqsecondrepo.",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Moved to https://forge.example.com/org/project?! Verify project."),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_footnote_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="[1]: https://git.zqcorp.io/zqgroup/zqsecondrepo\nDetails in zqsecondrepo.",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="[1]: https://forge.example.com/org/project\nDetails in project."),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_self_hosted_gfm_table_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="|a|https://git.zqcorp.io/zqgroup/zqtabrepo|\nProse: zqtabrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="|a|https://forge.example.com/org/project|\nProse: project"),
        markers=("git.zqcorp.io", "zqtabrepo"),
    ),
    Row(
        id="reg_url_self_hosted_backtick_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="`https://git.zqcorp.io/zqgroup/zqbtrepo` (see zqbtrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="`https://forge.example.com/org/project` (see project)"),
        markers=("git.zqcorp.io", "zqbtrepo"),
    ),
    Row(
        id="reg_url_self_hosted_bold_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="**https://git.zqcorp.io/zqgroup/zqboldrepo** (see zqboldrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="**https://forge.example.com/org/project** (see project)"),
        markers=("git.zqcorp.io", "zqboldrepo"),
    ),
    Row(
        id="reg_url_self_hosted_italic_star_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="*https://git.zqcorp.io/zqgroup/zqitalrepo* (see zqitalrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="*https://forge.example.com/org/project* (see project)"),
        markers=("git.zqcorp.io", "zqitalrepo"),
    ),
    Row(
        id="reg_url_self_hosted_strikethrough_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="~~https://git.zqcorp.io/zqgroup/zqstrikerepo~~ (see zqstrikerepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="~~https://forge.example.com/org/project~~ (see project)"),
        markers=("git.zqcorp.io", "zqstrikerepo"),
    ),
    Row(
        id="reg_url_self_hosted_italic_under_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="_https://git.zqcorp.io/zqgroup/zqunderrepo_ (see zqunderrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="_https://forge.example.com/org/project_ (see project)"),
        markers=("git.zqcorp.io", "zqunderrepo"),
    ),
    Row(
        id="reg_url_self_hosted_markdown_link_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See [m](https://git.zqcorp.io/zqgroup/zqlinkrepo) for zqlinkrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="See [m](https://forge.example.com/org/project) for project"),
        markers=("git.zqcorp.io", "zqlinkrepo"),
    ),
    Row(
        id="reg_url_self_hosted_mixed_case_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See HTTPS://GIT.ZQCORP.IO/zqgroup/zqsecondrepo (zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="See https://forge.example.com/org/project (project)"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_scp_mixed_case_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="git clone git@Git.ZQCorp.IO:zqgroup/zqsecondrepo.git (zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="git clone git@forge.example.com:org/project.git (project)"),
        markers=("git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_scp_same_host_trailing_dot",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="git clone git@git.zqcorp.io.:zqgroup/zqmainrepo.git",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="git clone git@forge.example.com:org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_scp_third_party_trailing_dot",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="git clone git@zqotherhost.io.:zqotherns/zqotherrepo.git", remote=""),
        outcome=Written(text="git clone git@forge.example.com:org/project.git"),
        markers=("zqotherhost.io", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="reg_url_https_trailing_dot_self_hosted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see https://git.zqother.io./zqotherns/zqotherrepo", remote=""),
        outcome=Written(text="see https://forge.example.com/org/project"),
        markers=("git.zqother.io", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="reg_url_ssh_port_and_login_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ssh://zqxjdoe7@git.zqcorp.io:2222/zqgroup/zqmainrepo.git",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="ssh://git@forge.example.com/org/project.git"),
        markers=("zqxjdoe7", "git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_https_credentials_query_fragment",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="https://zqxjdoe7:s3cr3t@git.zqcorp.io/zqgroup/zqmainrepo.git?ref=zqxjdoe7#zqxjdoe7",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="https://forge.example.com/org/project.git"),
        markers=("zqxjdoe7", "s3cr3t", "git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_empty_password_pat_same_host_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://glpat99:@git.zqcorp.io/zqgroup/zqsecondrepo (see zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("glpat99", "git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_empty_password_pat_second_host_remote_empty",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://glpat99:@mirror.zqcorp.net/zqgroup/zqsecondrepo (see zqsecondrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("glpat99", "mirror.zqcorp.net", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_symbol_password_excl_and_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Curl https://nkuser:pw!@git.zqcorp.io/zqgroup/zqsecondrepo (see zqsecondrepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Curl https://forge.example.com/org/project (see project)"),
        markers=("nkuser", "pw!", "git.zqcorp.io", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_scp_userinfo_on_example_host",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Run alice@example.com:org/repo.git"),
        outcome=Written(text="Run git@forge.example.com:org/project.git"),
        markers=("alice",),
    ),
    Row(
        id="reg_url_ghcr_with_planted_userinfo_secret",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="curl https://zqxjdoe7:s3cr3t@ghcr.io/zqgroup/zqmainrepo"),
        outcome=Written(text="curl https://forge.example.com/org/project"),
        markers=("zqxjdoe7", "zqgroup", "zqmainrepo", "s3cr3t"),
    ),
    Row(
        id="reg_url_ghcr_with_unknown_userinfo_credentials",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="curl https://alice:mysecret@ghcr.io/astral-sh/uv"),
        outcome=Written(text="curl https://forge.example.com/org/project"),
        markers=("alice", "mysecret"),
    ),
    Row(
        id="reg_url_npmjs_with_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="npm config set https://zqxjdoe7:npmtok99@registry.npmjs.org/"),
        outcome=Written(text="npm config set https://forge.example.com/"),
        markers=("zqxjdoe7", "npmtok99"),
    ),
    Row(
        id="reg_url_docker_with_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="docker login https://nkops:dockpass@docker.io/v2/"),
        outcome=Written(text="docker login https://forge.example.com/org/"),
        markers=("nkops", "dockpass"),
    ),
    Row(
        id="reg_url_docs_github_with_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="curl https://nkops:ghpass@docs.github.com/en/rest"),
        outcome=Written(text="curl https://forge.example.com/org/project"),
        markers=("nkops", "ghpass"),
    ),
    Row(
        id="reg_url_example_org_with_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="curl https://nkops:expass@example.org/test"),
        outcome=Written(text="curl https://forge.example.com/org"),
        markers=("nkops", "expass"),
    ),
    Row(
        id="reg_url_localhost_with_userinfo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="curl http://nkadmin:localpw@localhost:5432/db"),
        outcome=Written(text="curl http://forge.example.com/org"),
        markers=("nkadmin", "localpw"),
    ),
    Row(
        id="reg_url_double_slash_github",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://github.com/zqgroup//zqmainrepo/pull/42"),
        outcome=Written(text="https://forge.example.com/org/project/pull/42"),
        markers=("zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_double_slash_gitlab_subgroup",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="https://git.zqcorp.io/zqgroup//zqsub//zqmainrepo//-/merge_requests/42",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="https://forge.example.com/org/project/-/merge_requests/42"),
        markers=("git.zqcorp.io", "zqgroup", "zqsub", "zqmainrepo"),
    ),
    Row(
        id="reg_url_double_slash_repos_api",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://github.com/repos//zqotherns/zqotherrepo"),
        outcome=Written(text="https://forge.example.com/repos/org/project"),
        markers=("zqotherns", "zqotherrepo"),
    ),
    Row(
        id="reg_url_double_slash_users_api",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="https://github.com/users//zqsomeone9"),
        outcome=Written(text="https://forge.example.com/users/org"),
        markers=("zqsomeone9",),
    ),
    Row(
        id="reg_url_double_slash_in_url_key",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="hello",
            remote="",
            url="https://git.zqother.io/zqotherns//zqotherrepo",
        ),
        outcome=Written(text="hello"),
        markers=("git.zqother.io", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="reg_url_glab_api_project_id_without_prefix_in_comment",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api projects/88442211/merge_requests/42`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Repro: `glab api projects/2001/merge_requests/42`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_url_glab_api_project_id_leading_slash",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api /projects/88442211/pipelines`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Run `glab api /projects/2001/pipelines`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_url_glab_api_project_id_query",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api projects/88442211?statistics=true`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Run `glab api projects/2001?statistics=true`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_url_glab_api_project_id_in_call_args",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ok",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            api_path="projects/88442211/merge_requests/42/notes",
        ),
        outcome=Written(text='["api", "projects/2001/merge_requests/42/notes"]'),
        markers=("88442211",),
    ),
    Row(
        id="reg_url_api_v3_project_id_in_comment",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="API: https://git.zqcorp.io/api/v3/projects/88442211/issues",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="API: https://forge.example.com/api/v3/projects/2001/issues"),
        markers=("88442211", "git.zqcorp.io"),
    ),
    Row(
        id="reg_url_api_v4_project_id_only_in_comment",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="URL https://git.zqcorp.io/api/v4/projects/88442211/jobs/99",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="URL https://forge.example.com/api/v4/projects/2001/jobs/99"),
        markers=("88442211", "git.zqcorp.io"),
    ),
    Row(
        id="reg_url_api_v4_project_id_with_bare_prose_id",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="URL https://git.zqcorp.io/api/v4/projects/88442211/jobs/99 failed on project 88442211.",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(
            text="URL https://forge.example.com/api/v4/projects/2001/jobs/99 failed on project 2001."
        ),
    ),
    Row(
        id="reg_url_api_v4_project_id_in_repo_object_and_comment",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="API call: https://git.zqcorp.io/api/v4/projects/88442211/merge_requests/42",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            extra_item={
                "repository": {
                    "id": 88442211,
                    "name": "zqmainrepo",
                    "path": "zqmainrepo",
                    "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo",
                }
            },
        ),
        outcome=Written(
            text="API call: https://forge.example.com/api/v4/projects/2001/merge_requests/42"
        ),
        markers=("88442211", "git.zqcorp.io", "zqmainrepo"),
    ),
    Row(
        id="reg_url_api_v4_project_id_in_stdin",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ok",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            stdin="POST /api/v4/projects/88442211/notes",
        ),
        outcome=Written(text="POST /api/v4/projects/2001/notes"),
        markers=("88442211",),
    ),
    Row(
        id="reg_url_api_v4_project_id_in_stderr",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ok",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            stderr="403 GET https://git.zqcorp.io/api/v4/projects/88442211/members",
        ),
        outcome=Written(text="403 GET https://forge.example.com/api/v4/projects/2001/members"),
        markers=("88442211", "git.zqcorp.io"),
    ),
    Row(
        id="reg_url_scheme_led_rfc1918_ipv4_and_prose_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="Mirror at http://10.42.1.9:8080/zqipns/zqipproj (see zqipproj)", remote=""
        ),
        outcome=Written(text="Mirror at http://forge.example.com/org/project (see project)"),
        markers=("10.42.1.9", "zqipproj"),
    ),
    Row(
        id="reg_url_ftp_private_mirror_and_prose_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Artifacts at ftp://git.zqcorp.io/zqgroup/zqftprepo (see zqftprepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Artifacts at ftp://forge.example.com/org/project (see project)"),
        markers=("git.zqcorp.io", "zqftprepo"),
    ),
    Row(
        id="reg_url_ftps_private_mirror_and_prose_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Download ftps://git.zqcorp.io/zqgroup/zqftprepo/dist.tgz (zqftprepo)",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Download ftps://forge.example.com/org/project (org)"),
        markers=("git.zqcorp.io", "zqftprepo"),
    ),
    Row(
        id="reg_url_git_plus_https_pip_install",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="pip install git+https://github.com/zqgroup/zqmainrepo.git@main"),
        outcome=Written(text="pip install git+https://forge.example.com/org/project"),
        markers=("zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_url_schemeless_host_seen_led_in_same_fixture",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Primary https://git.zqseen.internal/zqgroup/zqfirstrepo",
            remote="",
            url="https://git.zqseen.internal/zqgroup/zqmainrepo/-/merge_requests/42",
            extra_items=(
                {
                    "author": {"username": "zqxjdoe7", "id": 74928163},
                    "body": "Mirror git.zqseen.internal/zqgroup/zqsecondrepo (see zqsecondrepo)",
                },
            ),
        ),
        outcome=Written(text="Primary https://forge.example.com/org/project"),
        markers=("git.zqseen.internal", "zqfirstrepo", "zqsecondrepo"),
    ),
    Row(
        id="reg_url_in_call_args",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            argv_extra=("-f", "body=See https://git.zqcorp.io/zqargns/zqargrepo?u=zqxjdoe7")
        ),
        outcome=Written(text="Looks good to me."),
        markers=("git.zqcorp.io", "zqargns", "zqargrepo", "zqxjdoe7"),
    ),
    Row(
        id="reg_url_in_top_kwargs",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            top_kwargs={"note": "https://git.zqcorp.io/zqgroup/zqmainrepo/pull/1 by zqxjdoe7"}
        ),
        outcome=Written(text='{"note": "https://forge.example.com/org/project/pull/1 by author"}'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7"),
    ),
    Row(
        id="reg_email_noreply_github_id_plus_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Co-authored-by: Dev <74928163+zqxjdoe7@users.noreply.github.com>"),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("74928163", "zqxjdoe7", "github.com"),
    ),
    Row(
        id="reg_email_noreply_github_login_only",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Co-authored-by: Dev <zqxjdoe7@users.noreply.github.com>"),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("zqxjdoe7", "github.com"),
    ),
    Row(
        id="reg_email_noreply_gitlab_id_dash_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Co-authored-by: Dev <74928163-zqxjdoe7@users.noreply.gitlab.com>",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("74928163", "zqxjdoe7", "gitlab.com"),
    ),
    Row(
        id="reg_email_noreply_self_hosted_users",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Co-authored-by: Dev <74928163+zqxjdoe7@users.noreply.git.zqcorp.io>",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("74928163", "zqxjdoe7", "git.zqcorp.io"),
    ),
    Row(
        id="reg_email_noreply_self_hosted_without_users",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Co-authored-by: Dev <74928163+zqxjdoe7@noreply.git.zqcorp.io>",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("74928163", "zqxjdoe7", "git.zqcorp.io"),
    ),
    Row(
        id="reg_email_noreply_with_denylist_in_local",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Co-authored-by: X <12345+zqdenyval@users.noreply.github.com>"),
        outcome=Written(text="Co-authored-by: X <author@users.noreply.forge.example.com>"),
        markers=("zqdenyval", "github.com"),
    ),
    Row(
        id="reg_email_noreply_uppercase",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Co-authored-by: Dev <74928163+ZQXJDOE7@USERS.NOREPLY.GITHUB.COM>"),
        outcome=Written(text="Co-authored-by: Dev <author@users.noreply.forge.example.com>"),
        markers=("74928163", "zqxjdoe7", "github.com"),
    ),
    Row(
        id="reg_email_system_noreply_self_hosted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="From: GitLab <noreply@git.zqcorp.io>",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="From: GitLab <noreply@forge.example.com>"),
        markers=("git.zqcorp.io",),
    ),
    Row(
        id="reg_email_mailto_noreply",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Email mailto:74928163+zqxjdoe7@users.noreply.github.com"),
        outcome=Written(text="Email mailto:author@users.noreply.forge.example.com"),
        markers=("74928163", "zqxjdoe7", "github.com"),
    ),
    Row(
        id="reg_email_mailto_personal_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Email [me](mailto:jane.real@privatecorp.com)"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_email_personal_corporate_domain_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Signed-off-by: Dev <dev.person@zqcorp.io>"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_email_personal_git_trailer_example_tld",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Signed-off-by: Someone <zqxjdoe7@zqcorp.example>"),
        outcome=Written(text="Signed-off-by: Someone <author@zqcorp.example>"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_email_login_at_example_com",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="mail zqxjdoe7@example.com"),
        outcome=Written(text="mail author@example.com"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_email_first_login_plus_tag_example_com",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="mail first.zqxjdoe7+tag@example.com"),
        outcome=Written(text="mail first.author+tag@example.com"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_email_sp_login_upper_example_com",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="mail SP-ZQXJDOE7@example.com"),
        outcome=Written(text="mail SP-author@example.com"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_email_login_at_sub_example_com_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Mail zqxjdoe7@sub.example.com"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_email_private_with_port_colon_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="contact real.person@private-mail.org:25"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_email_private_with_word_colon_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="contact real.person@private-mail.org:hello"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_invented_sp_login_literal",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-ZQXJDOE7 here"),
        outcome=Written(text="see SP-author here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_invented_sp_fix_login_literal",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-FIX-ZQXJDOE7 here"),
        outcome=Written(text="see SP-FIX-author here"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="reg_invented_sp_repo_literal",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-ZQMAINREPO here"),
        outcome=Written(text="see SP-project here"),
        markers=("zqmainrepo",),
    ),
    Row(
        id="reg_invented_sp_ns_literal",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-ZQGROUP here"),
        outcome=Written(text="see SP-org here"),
        markers=("zqgroup",),
    ),
    Row(
        id="reg_invented_sp_uid_literal_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-74928163 here"),
        outcome=Written(text="see SP-1001 here"),
    ),
    Row(
        id="reg_invented_sp_denylist_literal_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="see SP-ZQDENYVAL here"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_linux_home_known_login_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='File "/home/zqxjdoe7/work/zqmainrepo/app.py", line 9'),
        outcome=Refused("generic class home_path at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_macos_users_known_login_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='File "/Users/zqxjdoe7/dev/zqmainrepo/app.py", line 12'),
        outcome=Refused("generic class home_path at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_macos_users_unknown_home_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='File "/Users/janesmith/src/app.py", line 42'),
        outcome=Refused("generic class home_path at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_linux_home_unknown_user_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Error at /home/alice_local/src/main.py:42"),
        outcome=Refused("generic class home_path at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_windows_users_known_login_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='File "C:\\Users\\zqxjdoe7\\work\\zqmainrepo\\app.py", line 12'),
        outcome=Refused("generic class home_path at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_tilde_repo_denylist_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Edited ~/work/zqmainrepo/src/zqdenyval.py"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_path_relative_ns_repo_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Modified src/zqgroup/zqmainrepo/mod.py"),
        outcome=Written(text="Modified src/org/project/mod.py"),
        markers=("zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_ci_log_stderr_denylist_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            stderr="fatal: could not read zqdenyval on git.zqcorp.io",
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stderr"),
    ),
    Row(
        id="reg_ci_log_stderr_git_push_error_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            stderr="remote: Permission to zqgroup/zqmainrepo.git denied to zqxjdoe7.\nfatal: unable to access 'https://git.zqcorp.io/zqgroup/zqmainrepo.git/': 403",
        ),
        outcome=Written(
            text="remote: Permission to org/project.git denied to author.\nfatal: unable to access 'https://forge.example.com/org/project.git/': 403"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7"),
    ),
    Row(
        id="reg_ci_log_stdin_private_url_and_login_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            stdin="Merge https://git.zqcorp.io/zqgroup/zqmainrepo/pull/1 by zqxjdoe7",
        ),
        outcome=Written(text="Merge https://forge.example.com/org/project/pull/1 by author"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7"),
    ),
    Row(
        id="reg_ci_log_diff_private_url_and_denylist_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            raw_text_stdout="diff --git a/README.md b/README.md\n+# See https://git.zqcorp.io/zqgroup/zqmainrepo (zqdenyval)\n",
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout"),
    ),
    Row(
        id="reg_ci_log_diff_home_path_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            raw_text_stdout="diff --git a/cfg.ini b/cfg.ini\n+root = /home/alice_local/data\n"
        ),
        outcome=Refused("generic class home_path at exchanges[0].stdout"),
    ),
    Row(
        id="reg_ci_log_diff_noreply_email_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            raw_text_stdout="diff --git a/AUTHORS b/AUTHORS\n+Dev <74928163+zqxjdoe7@users.noreply.github.com>\n"
        ),
        outcome=Written(
            text="diff --git a/AUTHORS b/AUTHORS\n+Dev <author@users.noreply.forge.example.com>\n"
        ),
        markers=("74928163", "zqxjdoe7", "github.com"),
    ),
    Row(
        id="reg_ci_log_public_ipv4_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Connected to runner 45.33.32.156:443"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_ci_log_graphql_legacy_node_id_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="GraphQL error: could not resolve MDQ6VXNlcjc0OTI4MTYz"),
        outcome=Written(text="GraphQL error: could not resolve U_1001"),
    ),
    Row(
        id="reg_ci_log_graphql_new_u_node_id_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="GraphQL error: could not resolve U_kgDOBHdQIw"),
        outcome=Written(text="GraphQL error: could not resolve U_1001"),
    ),
    Row(
        id="reg_ci_log_deny_numeric_legacy_node_id_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="GraphQL error: MDQ6VXNlcjkxODI3MzY0"),
        outcome=Refused("deny-list entry 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_ci_log_deny_numeric_new_u_node_id_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="GraphQL error: U_kgDOBXkspA"),
        outcome=Refused("deny-list entry 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_case_field_with_login_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(case="pr_zqxjdoe7"),
        outcome=Refused("residual login 1 at case"),
    ),
    Row(
        id="reg_case_field_with_repo_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(case="pr_zqmainrepo"),
        outcome=Refused("residual segment 2 at case"),
    ),
    Row(
        id="reg_case_field_with_host_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            case="pr_git.zqcorp.io",
        ),
        outcome=Refused("residual host 2 at case"),
    ),
    Row(
        id="reg_case_field_with_denylist_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(case="pr_zqdenyval"),
        outcome=Refused("deny-list entry 1 at case"),
    ),
    Row(
        id="reg_dropped_key_assignees_login_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="Assigned to zqassignee9 for triage",
            extra_item={"assignees": [{"login": "zqassignee9", "id": 55443322}]},
        ),
        outcome=Written(text="Assigned to author-2 for triage"),
        markers=("zqassignee9", "55443322"),
    ),
    Row(
        id="reg_dropped_key_mergedby_login_redacted",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="Merged by zqmerger8 yesterday",
            extra_item={"mergedBy": {"login": "zqmerger8", "id": 66554433}},
        ),
        outcome=Written(text="Merged by author-2 yesterday"),
        markers=("zqmerger8", "66554433"),
    ),
    Row(
        id="reg_graphql_resolvedby_and_message_headline",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            body="ok",
            extra_item={
                "resolvedBy": {"login": "zqreviewer", "id": 63918499},
                "messageHeadline": "Fix requested by @zqreviewer",
            },
        ),
        outcome=Written(text="ok"),
        markers=("zqreviewer", "63918499"),
    ),
    Row(
        id="reg_needle_err_short_dropped_key_login",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(extra_item={"assignees": [{"login": "sam"}]}),
        outcome=Refused("needle login 2: too short"),
    ),
    Row(
        id="reg_needle_err_placeholder_repo_project",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(ns="mygroup", repo="project", remote="git@github.com:mygroup/project.git"),
        outcome=Refused("needle segment 2: a class word or placeholder"),
    ),
    Row(
        id="reg_needle_err_short_3char_repo_cli",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(ns="mygroup", repo="cli", remote="git@github.com:mygroup/cli.git"),
        outcome=Refused("needle segment 2: too short"),
    ),
    Row(
        id="reg_mention_only_mark_with_bare_word_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="cc @mark please mark this ready"),
        outcome=Refused("residual login 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_mention_only_mark_with_hyphen_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="cc @mark please fix the mark-down rendering"),
        outcome=Refused("residual login 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_mention_only_check_with_underscore_refused",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="cc @check please run check_status"),
        outcome=Refused("residual login 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="reg_r10_userinfo_empty_password_x_access_token",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://x-access-token:@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("x-access-token", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_empty_password_gitlab_ci_token",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://gitlab-ci-token:@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("gitlab-ci-token", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_empty_password_oauth2_token",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://oauth2-token:@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("oauth2-token", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_empty_password_deploy_key_1",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://deploy-key-1:@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("deploy-key-1", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_empty_password_first_last_dev",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://first-last.dev:@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (see project)"),
        markers=("first-last.dev", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_tilde",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw~x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_bang",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw!x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_dollar",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw$x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_amp",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw&x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_squote",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw'x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_lparen",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw(x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_rparen",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw)x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_star",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw*x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_comma",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw,x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_semi",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw;x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_eq",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw=x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_hyphen_user_password_char_colon",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Fetch https://ci-bot:pw:x@mirror.zqcorp.net/zqgroup/zqbillrepo (see zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Fetch https://forge.example.com/org/project (see project)"),
        markers=("ci-bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_userinfo_star_password_on_kept_public_host",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Fetch https://x-access-token:pw*1@docs.python.org/3/library/re.html now"),
        outcome=Written(text="Fetch https://forge.example.com/org/project now"),
        markers=("x-access-token", "pw*1"),
    ),
    Row(
        id="reg_r10_userinfo_quote_password_on_kept_public_host",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="Fetch https://x-access-token:pw'1@developer.mozilla.org/en-US/docs/ now"),
        outcome=Written(text="Fetch https://forge.example.com/org/project/ now"),
        markers=("x-access-token", "pw'1"),
    ),
    Row(
        id="reg_r10_userinfo_dotted_user_hyphen_password",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone https://first.last:my-pass@mirror.zqcorp.net/zqgroup/zqbillrepo (zqbillrepo)",
            remote="",
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project (project)"),
        markers=("first.last", "my-pass", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_query_string_with_at_sign_and_prose_host",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See https://mirror.zqcorp.net?contact=alice@example.com (host mirror.zqcorp.net)",
            remote="",
        ),
        outcome=Written(text="See https://forge.example.com (host forge.example.com)"),
        markers=("mirror.zqcorp.net",),
    ),
    Row(
        id="reg_r10_adjacent_angle_urls_and_mailbox",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See <https://mirror.zqcorp.net/zqgroup/zqbillrepo><alice@example.com> now",
            remote="",
        ),
        outcome=Written(text="See <https://forge.example.com/org/project><alice@example.com> now"),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_diff_scp_added_line_git_user",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="",
            raw_text_stdout="diff --git a/sub b/sub\n+git@mirror.zqcorp.net:zqgroup/zqbillrepo.git\n",
        ),
        outcome=Written(text="diff --git a/sub b/sub\n+git@forge.example.com:org/project.git\n"),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_diff_scp_removed_line_named_user",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="",
            raw_text_stdout="diff --git a/sub b/sub\n-alice@mirror.zqcorp.net:zqgroup/zqbillrepo.git\n",
        ),
        outcome=Written(text="diff --git a/sub b/sub\n-git@forge.example.com:org/project.git\n"),
        markers=("alice", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_diff_scp_added_line_named_user",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            remote="",
            raw_text_stdout="diff --git a/sub b/sub\n+bob@mirror.zqcorp.net:zqgroup/zqbillrepo.git\n",
        ),
        outcome=Written(text="diff --git a/sub b/sub\n+git@forge.example.com:org/project.git\n"),
        markers=("bob", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_diff_scp_removed_line_named_user_on_example_host",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(raw_text_stdout="diff --git a/sub b/sub\n-alice@example.com:org/repo.git\n"),
        outcome=Written(text="diff --git a/sub b/sub\n-git@forge.example.com:org/project.git\n"),
        markers=("alice",),
    ),
    Row(
        id="reg_r10_scp_plus_in_user_name",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Clone ci+bot@mirror.zqcorp.net:zqgroup/zqbillrepo.git for zqbillrepo",
            remote="",
        ),
        outcome=Written(text="Clone git@forge.example.com:org/project.git for project"),
        markers=("ci+bot", "mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_glab_api_quoted_header_flag",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -H 'Accept: application/json' /projects/88442211/issues`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(
            text="Repro: `glab api -H 'Accept: application/json' /projects/2001/issues`"
        ),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_double_quoted_field_flag",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body='Run `glab api -f "body=hello world" projects/88442211/notes`',
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text='Run `glab api -f "body=hello world" projects/2001/notes`'),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_raw_field_flag",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api --raw-field 'title=Fix bug' projects/88442211/merge_requests`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(
            text="Run `glab api --raw-field 'title=Fix bug' projects/2001/merge_requests`"
        ),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_header_equals_flag",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api --header='PRIVATE-TOKEN: x' projects/88442211/issues`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Run `glab api --header='PRIVATE-TOKEN: x' projects/2001/issues`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_method_flag_in_text",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -X POST /projects/88442211/notes`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Repro: `glab api -X POST /projects/2001/notes`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_unbalanced_quote_falls_back_safely",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api -H 'Accept: x projects/88442211/issues`",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Run `glab api -H 'Accept: x projects/2001/issues`"),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_glab_api_full_argv_list",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ok",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            call_args=("glab", "api", "/projects/88442211/jobs"),
        ),
        outcome=Written(text='["glab", "api", "/projects/2001/jobs"]'),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_web_projects_numeric_merge_request_path",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="See https://git.zqcorp.io/projects/88442211/merge_requests/42 for context",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(
            text="See https://forge.example.com/projects/2001/merge_requests/42 for context"
        ),
        markers=("88442211",),
    ),
    Row(
        id="reg_r10_protocol_relative_json_string_value",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='{"cdn": "//mirror.zqcorp.net/zqgroup/zqbillrepo/app.js"}'),
        outcome=Written(text='{"cdn": "//forge.example.com/org/project/app.js"}'),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_protocol_relative_script_src",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body='<script src="//mirror.zqcorp.net/zqgroup/zqbillrepo/app.js"></script>'),
        outcome=Written(text='<script src="//forge.example.com/org/project/app.js"></script>'),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_protocol_relative_css_url",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="a { background: url(//mirror.zqcorp.net/zqgroup/zqbillrepo/bg.png) }"),
        outcome=Written(text="a { background: url(//forge.example.com/org/project/bg.png) }"),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_protocol_relative_markdown_link",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(body="See [x](//mirror.zqcorp.net/zqgroup/zqbillrepo/page) now"),
        outcome=Written(text="See [x](//forge.example.com/org/project/page) now"),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_top_args_projects_path_with_api_v4_kwargs",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="ok",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            top_args=("projects/88442211/jobs",),
            top_kwargs={"ref": "https://git.zqcorp.io/api/v4/projects/88442211/issues"},
        ),
        outcome=Written(text='["projects/2001/jobs"]'),
    ),
    Row(
        id="reg_r10_italic_scp_remote_keeps_closing_underscore",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab", body="Use _git@mirror.zqcorp.net:zqgroup/zqbillrepo.git_ now", remote=""
        ),
        outcome=Written(text="Use _git@forge.example.com:org/project.git_ now"),
        markers=("mirror.zqcorp.net", "zqbillrepo"),
    ),
    Row(
        id="reg_r10_prose_double_slash_host_known_from_remote_is_replaced_as_a_token",
        source="tests/unit/test_forge_recording_regression_corpus.py PLANTED",
        inp=Sample(
            forge="gitlab",
            body="Loaded from //git.zqcorp.io/zqgroup/zqmainrepo/v1.js",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
        ),
        outcome=Written(text="Loaded from //forge.example.com/org/project/v1.js"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="reg_plain_review_prose_and_task_list",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="LGTM! Tested locally:\n- [x] unit tests pass\n- [x] ruff clean\n\nReady to merge.",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="LGTM! Tested locally:\n- [x] unit tests pass\n- [x] ruff clean\n\nReady to merge."
        ),
    ),
    Row(
        id="reg_gitlab_suggestion_block",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            forge="gitlab",
            body="Small nit:\n```suggestion:-0+0\ndef parse(raw: str) -> dict[str, int]:\n    return {k: int(v) for k, v in raw.items()}\n```\nLooks solid otherwise.",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@gitlab.com:zqpuborg/zqpubrepo.git",
            url="https://gitlab.com/zqpuborg/zqpubrepo/-/merge_requests/42",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Small nit:\n```suggestion:-0+0\ndef parse(raw: str) -> dict[str, int]:\n    return {k: int(v) for k, v in raw.items()}\n```\nLooks solid otherwise."
        ),
    ),
    Row(
        id="reg_mixed_case_docs_github_and_docs_gitlab",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="See https://Docs.GitHub.com/en/actions/using-workflows and HTTPS://DOCS.GITHUB.COM/en/rest/pulls and https://Docs.GitLab.com/ee/api/merge_requests.html",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="See https://docs.github.com/en/actions/using-workflows and https://docs.github.com/en/rest/pulls and https://docs.gitlab.com/ee/api/merge_requests.html"
        ),
    ),
    Row(
        id="reg_public_docs_python_peps_readthedocs_mdn_crates_so_wikipedia",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Python: https://docs.python.org/3/library/pathlib.html? https://www.python.org/downloads/release/python-3120/ https://peps.python.org/pep-0723/\nRTD: https://readthedocs.org/projects/pytest/ https://pytest.readthedocs.io/en/stable/how-to/fixtures.html\nMDN: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429\nRust: https://crates.io/crates/tokio https://docs.rs/tokio/latest/tokio/\nSO: https://stackoverflow.com/questions/419163/what-does-if-name-main-do\nWiki: https://en.wikipedia.org/wiki/Quicksort https://en.wikipedia.org/wiki/Python_(programming_language)",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Python: https://docs.python.org/3/library/pathlib.html? https://www.python.org/downloads/release/python-3120/ https://peps.python.org/pep-0723/\nRTD: https://readthedocs.org/projects/pytest/ https://pytest.readthedocs.io/en/stable/how-to/fixtures.html\nMDN: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429\nRust: https://crates.io/crates/tokio https://docs.rs/tokio/latest/tokio/\nSO: https://stackoverflow.com/questions/419163/what-does-if-name-main-do\nWiki: https://en.wikipedia.org/wiki/Quicksort https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
    ),
    Row(
        id="reg_trailing_exclamation_and_question_on_kept_urls",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Try https://example.com! Also http://localhost! Published https://pypi.org/project/ruff/! Checked https://example.com? And https://docs.python.org/3/library/re.html?",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Try https://example.com! Also http://localhost! Published https://pypi.org/project/ruff/! Checked https://example.com? And https://docs.python.org/3/library/re.html?"
        ),
    ),
    Row(
        id="reg_gfm_table_and_backticked_urls",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Check `https://example.com` and `http://localhost` and `https://127.0.0.1:8080/health`.\n|Tool|Link|\n|---|---|\n|Ruff|https://pypi.org/project/ruff/|\n|Docs|https://docs.github.com/en/actions|\n|Py|https://docs.python.org/3/library/re.html|\n",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Check `https://example.com` and `http://localhost` and `https://127.0.0.1:8080/health`.\n|Tool|Link|\n|---|---|\n|Ruff|https://pypi.org/project/ruff/|\n|Docs|https://docs.github.com/en/actions|\n|Py|https://docs.python.org/3/library/re.html|\n"
        ),
    ),
    Row(
        id="reg_github_url_with_dunder_init_anchor",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="See https://github.com/python/cpython/blob/main/Lib/pathlib.py#__init__ for details.",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="See https://forge.example.com/org/project/blob/main/Lib/pathlib.py for details."
        ),
    ),
    Row(
        id="reg_repo_year_paths_projects_2026_and_2025",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Updated projects/2026/q1.md and /projects/2025/architecture.md and docs/projects/2026/plan.md (written in 2025).",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Updated projects/2026/q1.md and /projects/2025/architecture.md and docs/projects/2026/plan.md (written in 2025)."
        ),
    ),
    Row(
        id="reg_gitlab_api_doc_examples_never_refuse_or_corrupt",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Per GitLab docs: `curl https://gitlab.example.com/api/v4/projects/1/merge_requests`\nMock endpoint: `GET /api/v4/projects/42/issues`\nExample: `curl https://gitlab.example.com/api/v4/projects/1234/issues`",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Per GitLab docs: `curl https://gitlab.example.com/api/v4/projects/1/merge_requests`\nMock endpoint: `GET /api/v4/projects/42/issues`\nExample: `curl https://gitlab.example.com/api/v4/projects/1234/issues`"
        ),
    ),
    Row(
        id="reg_macos_bundle_paths_and_tld_matching_code_attributes",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Using Xcode.app/Contents/Developer/usr/bin/clang and MyApp.app/Contents/MacOS/app\n```python\npath = config.dev/env/prod\ncert = ssl.ca/certs/root.pem\nbuf = stream.io/read/write\nlayer = torch.net/layers/conv\nratio = self.ms/1000\ncfg = config.dir/sub/file.toml\nval = obj.total/count/100\nroot = tmp.root/src/app.py\nresult = a.b/c/d\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Using Xcode.app/Contents/Developer/usr/bin/clang and MyApp.app/Contents/MacOS/app\n```python\npath = config.dev/env/prod\ncert = ssl.ca/certs/root.pem\nbuf = stream.io/read/write\nlayer = torch.net/layers/conv\nratio = self.ms/1000\ncfg = config.dir/sub/file.toml\nval = obj.total/count/100\nroot = tmp.root/src/app.py\nresult = a.b/c/d\n```"
        ),
    ),
    Row(
        id="reg_go_cpp_single_line_comments_without_and_with_space",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```go\n//config.dev/env/prod\n// config.dev/env/prod\nvar x = 1\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="```go\n//config.dev/env/prod\n// config.dev/env/prod\nvar x = 1\n```"
        ),
    ),
    Row(
        id="reg_yaml_github_actions_workflow_and_shell_snippet",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```yaml\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n      - run: pytest -q tests/unit\n```\n```sh\ncurl -sSf https://example.com/health > /dev/null 2>&1\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="```yaml\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n      - run: pytest -q tests/unit\n```\n```sh\ncurl -sSf https://example.com/health > /dev/null 2>&1\n```"
        ),
    ),
    Row(
        id="reg_js_node_stack_trace",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="TypeError: Cannot read properties of undefined (reading 'map')\n    at renderList (src/components/List.tsx:42:18)\n    at processTicksAndRejections (node:internal/process/task_queues:95:5)",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="TypeError: Cannot read properties of undefined (reading 'map')\n    at renderList (src/components/List.tsx:42:18)\n    at processTicksAndRejections (node:internal/process/task_queues:95:5)"
        ),
    ),
    Row(
        id="reg_decorators_annotations_and_scoped_npm_packages",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Use @dataclass, @property, @Override, @Deprecated, @pytest.fixture and @lru_cache(maxsize=64).\nImport `@types/node`, `@vue/cli`, `@babel/core`, `@actions/checkout`, `@tanstack/react-query`.\n```ts\nconst url = this.base/api/v1;\nimport H from 'components.tsx/Header/index';\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Use @dataclass, @property, @Override, @Deprecated, @pytest.fixture and @lru_cache(maxsize=64).\nImport `@types/node`, `@vue/cli`, `@babel/core`, `@actions/checkout`, `@tanstack/react-query`.\n```ts\nconst url = this.base/api/v1;\nimport H from 'components.tsx/Header/index';\n```"
        ),
    ),
    Row(
        id="reg_dependabot_pr_template",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Bumps ruff from 0.14.0 to 0.15.4.\nDependabot will resolve any conflicts.\n- `@dependabot rebase` will rebase this PR\n- @dependabot recreate will recreate this PR\nAlso updates @types/node and @vue/cli.",
            login="dependabot[bot]",
            uid=49699333,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            author="dependabot[bot]",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Bumps ruff from 0.14.0 to 0.15.4.\nauthor will resolve any conflicts.\n- `@author rebase` will rebase this PR\n- @author recreate will recreate this PR\nAlso updates @types/node and @vue/cli."
        ),
    ),
    Row(
        id="reg_renovate_with_actions_checkout_title_and_docs_github",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="| [@babel/core](https://www.npmjs.com/package/@babel/core) | `7.24.0` -> `7.25.0` |\nRenovate will rebase automatically. See https://docs.github.com/en/actions.\nTrigger @renovate rebase if needed.",
            title="chore(deps): update @actions/checkout action to v4",
            login="renovate[bot]",
            uid=29139614,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            author="renovate[bot]",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="| [@babel/core](https://www.npmjs.com/package/@babel/core) | `7.24.0` -> `7.25.0` |\nauthor will rebase automatically. See https://docs.github.com/en/actions.\nTrigger @author rebase if needed.",
            title="chore(deps): update @actions/checkout action to v4",
        ),
    ),
    Row(
        id="reg_short_stem_ci_bot_preserves_english_ci_prose",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="CI pipeline passed! Re-run with `@ci retry` or @ci rerun.",
            login="ci[bot]",
            uid=52143690,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            author="ci[bot]",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="CI pipeline passed! Re-run with `@author retry` or @author rerun."),
    ),
    Row(
        id="reg_github_actions_bot_with_actions_login",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Triggered by github-actions[bot]; check the actions tab",
            login="actions",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Triggered by github-actions[bot]; check the author tab"),
    ),
    Row(
        id="reg_github_actions_bot_with_github_login_and_runner",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="by github-actions[bot] on github-actions-runner",
            login="github",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="by github-actions[bot] on author-actions-runner"),
    ),
    Row(
        id="reg_short_third_party_url_a_io",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="See https://a.io/foo",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="See https://forge.example.com/org"),
    ),
    Row(
        id="reg_repoach_bot_with_repoach_repo",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Reviewed by repoach-bot on repoach PR",
            login="zqxauthor",
            uid=48151623,
            ns="repoachhq",
            repo="repoach",
            remote="git@github.com:repoachhq/repoach.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Reviewed by repoach-bot on project PR"),
    ),
    Row(
        id="reg_common_word_login_main",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Merge main into main-fix and update main_branch and domain",
            login="main",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Merge author into author-fix and update author_branch and domain"),
    ),
    Row(
        id="reg_common_word_login_test",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Run test and test_foo and test-bar and pytest",
            login="test",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Run author and author_foo and author-bar and pytest"),
    ),
    Row(
        id="reg_common_word_login_code",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Check code, code-review, code_style, and vscode",
            login="code",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Check author, author-review, author_style, and vscode"),
    ),
    Row(
        id="reg_common_word_login_docs",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Update docs and docs-ci and docs/README.md",
            login="docs",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Update author and author-ci and author/README.md"),
    ),
    Row(
        id="reg_common_word_login_python",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Bump python and python-3.12 and python_version and cpython",
            login="python",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Bump author and author-3.12 and author_version and cpython"),
    ),
    Row(
        id="reg_common_word_login_true",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Returns true when flag-true is set",
            login="true",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Returns author when flag-author is set"),
    ),
    Row(
        id="reg_common_word_login_repo_preserves_cli_placeholder",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Check repo and repo-name and repos/:owner/:repo",
            login="repo",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Check author and author-name and repos/:owner/:repo"),
    ),
    Row(
        id="reg_class_word_login_author_kept",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="looks good",
            login="author",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="looks good"),
    ),
    Row(
        id="reg_short_third_party_url_x_com",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="See https://x.com/user/status/123",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project"),
    ),
    Row(
        id="reg_mailto_example_com_and_port_like_file_path",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Contact mailto:someone@example.com; see foo.py:80/bar",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="Contact mailto:someone@example.com; see foo.py:80/bar"),
    ),
    Row(
        id="reg_git_log_co_authored_by_noreply_and_actions_github",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="commit deadbeef0000\nAuthor: Dev <12345+someone@users.noreply.github.com>\nCommitter: GitHub <noreply@github.com>\nAuthor: github-actions[bot] <actions@github.com>\nCo-authored-by: Tool <175728472+Copilot@users.noreply.github.com>\nCo-authored-by: Bob <999-bob@users.noreply.gitlab.com>\nCo-authored-by: Claude <noreply@anthropic.com>\n",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="commit deadbeef0000\nAuthor: Dev <author@users.noreply.forge.example.com>\nCommitter: GitHub <noreply@forge.example.com>\nAuthor: github-actions[bot] <noreply@forge.example.com>\nCo-authored-by: Tool <author@users.noreply.forge.example.com>\nCo-authored-by: Bob <author@users.noreply.forge.example.com>\nCo-authored-by: Claude <noreply@forge.example.com>\n"
        ),
    ),
    Row(
        id="reg_docker_go_k8s_pypi_npm_public_reference_hosts",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body='```dockerfile\nFROM docker.io/library/python:3.12-slim\nCOPY --from=ghcr.io/astral-sh/uv:0.4.0 /uv /bin/uv\n```\n```go\nimport (\n\t"golang.org/x/sync/errgroup"\n\t"k8s.io/client-go/kubernetes"\n)\n```\nSee https://pkg.go.dev/golang.org/x/sync/errgroup and https://proxy.golang.org/ and https://registry.npmjs.org/typescript\n',
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text='```dockerfile\nFROM docker.io/library/python:3.12-slim\nCOPY --from=ghcr.io/astral-sh/uv:0.4.0 /uv /bin/uv\n```\n```go\nimport (\n\t"golang.org/x/sync/errgroup"\n\t"k8s.io/client-go/kubernetes"\n)\n```\nSee https://pkg.go.dev/golang.org/x/sync/errgroup and https://proxy.golang.org/ and https://registry.npmjs.org/typescript\n'
        ),
    ),
    Row(
        id="reg_self_hosted_gitlab_empty_remote_with_docs_python_and_mdn",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            forge="gitlab",
            body="See https://docs.python.org/3/library/re.html and https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API.",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="",
            url="https://git.zqcorp.io/zqpuborg/zqpubrepo/-/merge_requests/1#note_1",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="See https://docs.python.org/3/library/re.html and https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API."
        ),
    ),
    Row(
        id="reg_github_empty_remote_with_docs_python_and_golang_import",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="See https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="See https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup"
        ),
    ),
    Row(
        id="reg_github_ssh_alias_with_third_party_links",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Upgrade python and git; see https://github.com/python/cpython/pull/1, https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="mygh:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Upgrade python and git; see https://forge.example.com/org/project/pull/1, https://docs.python.org/3/library/re.html and golang.org/x/sync/errgroup"
        ),
    ),
    Row(
        id="reg_ci_log_wheel_dist_info_tracebacks_and_rfc_examples",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body='File "src/repoach/cli/main.py", line 42, in run\nAdding repoach-0.1.0.dist-info/licenses/LICENSE\nSend test payload to demo@example.com.\nLocal bind 127.0.0.1:8080, private 10.0.0.5, doc 203.0.113.10 and 198.51.100.7.\nTested on v1.2.3.4 and foo==2.3.4.5 and 3.4.5.6.tar.gz.\ncurl -sSf https://example.com/health > /dev/null 2>&1',
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text='File "src/repoach/cli/main.py", line 42, in run\nAdding repoach-0.1.0.dist-info/licenses/LICENSE\nSend test payload to demo@example.com.\nLocal bind 127.0.0.1:8080, private 10.0.0.5, doc 203.0.113.10 and 198.51.100.7.\nTested on v1.2.3.4 and foo==2.3.4.5 and 3.4.5.6.tar.gz.\ncurl -sSf https://example.com/health > /dev/null 2>&1'
        ),
    ),
    Row(
        id="reg_non_json_unified_diff_code_and_decorators",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            raw_text_stdout="diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1,4 +1,5 @@\n @pytest.fixture\n def f(cfg):\n-    return self.ms/1000\n+    return cfg.root/sub/file\n",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1,4 +1,5 @@\n @pytest.fixture\n def f(cfg):\n-    return self.ms/1000\n+    return cfg.root/sub/file\n"
        ),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_go_assign",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```go\nx := 1 //cfg.dev/env/prod\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```go\nx := 1 //cfg.dev/env/prod\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_go_brace",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```go\nif x > 0 { //store.io/cache/hit\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```go\nif x > 0 { //store.io/cache/hit\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_go_return",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```go\nreturn x //metrics.net/count/ok\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```go\nreturn x //metrics.net/count/ok\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_js_call",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```ts\nconst cfg = load() //cfg.dev/env/prod\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```ts\nconst cfg = load() //cfg.dev/env/prod\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_js_export",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```ts\nexport default cfg //app.io/entry/main\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```ts\nexport default cfg //app.io/entry/main\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_c_brace",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```c\nif (ok) { //config.dev/env/prod\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```c\nif (ok) { //config.dev/env/prod\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_c_semicolon",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```c\nint x = 1; //store.net/cache/l1\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```c\nint x = 1; //store.net/cache/l1\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_c_line_start",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```c\n//cfg.dev/env/prod\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```c\n//cfg.dev/env/prod\n```"),
    ),
    Row(
        id="reg_r10_code_line_comment_after_code_c_doc_comment",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```c\n///api.io/v2/schema\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(text="```c\n///api.io/v2/schema\n```"),
    ),
    Row(
        id="reg_r10_go_multi_function_with_inline_comments",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```go\npackage main\nfunc load() int {\n\tx := 1 //cfg.dev/env/prod\n\tif x > 0 { //store.io/cache/hit\n\t\treturn x //metrics.net/count/ok\n\t}\n\treturn 0\n}\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="```go\npackage main\nfunc load() int {\n\tx := 1 //cfg.dev/env/prod\n\tif x > 0 { //store.io/cache/hit\n\t\treturn x //metrics.net/count/ok\n\t}\n\treturn 0\n}\n```"
        ),
    ),
    Row(
        id="reg_r10_semicolon_free_ts_block_with_inline_comments",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="```ts\nconst cfg = load() //cfg.dev/env/prod\nconst url = this.base/api/v1\nexport default cfg //app.io/entry/main\n```",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="```ts\nconst cfg = load() //cfg.dev/env/prod\nconst url = this.base/api/v1\nexport default cfg //app.io/entry/main\n```"
        ),
    ),
    Row(
        id="reg_r10_go_diff_with_comments_year_projects_and_docs_python",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            raw_text_stdout="diff --git a/projects/2026/main.go b/projects/2026/main.go\n--- a/projects/2026/main.go\n+++ b/projects/2026/main.go\n@@ -1,3 +1,4 @@\n+//cfg.dev/env/prod\n+// See https://docs.python.org/3/library/re.html\n+x := 1 //store.io/val/2026\n",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="diff --git a/projects/2026/main.go b/projects/2026/main.go\n--- a/projects/2026/main.go\n+++ b/projects/2026/main.go\n@@ -1,3 +1,4 @@\n+//cfg.dev/env/prod\n+// See https://docs.python.org/3/library/re.html\n+x := 1 //store.io/val/2026\n"
        ),
    ),
    Row(
        id="reg_r10_italic_kept_scp_example_remote",
        source="tests/unit/test_forge_recording_regression_corpus.py ORDINARY",
        inp=Sample(
            body="Example: _git@example.com:org/demo.git_ and __git@sub.example.org:team/app.git__.",
            login="zqxauthor",
            uid=48151623,
            ns="zqpuborg",
            repo="zqpubrepo",
            remote="git@github.com:zqpuborg/zqpubrepo.git",
            deny=("zqdenyval",),
        ),
        outcome=Written(
            text="Example: _git@example.com:org/demo.git_ and __git@sub.example.org:team/app.git__."
        ),
    ),
    Row(
        id="r10_p01_ci_fetch_ci_token_pat",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout="$ git fetch https://gitlab-ci-token:glcbt-64_AbCdEf@gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git\nFrom https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="$ git fetch https://forge.example.com/org/project.git\nFrom https://forge.example.com/org/project\n"
        ),
        markers=("gitlab.qwzcorp.internal", "qwzpayments", "glcbt-64_AbCdEf"),
    ),
    Row(
        id="r10_p02_actions_x_access_token_clone",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            raw_text_stdout="Run git clone https://x-access-token:ghs_16C7e42F292c6912E7710c838347Ae178B4a@github.com/kvnteam/kvnbilling\nCloning into 'kvnbilling'...\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Run git clone https://forge.example.com/org/project\nCloning into 'kvnbilling'...\n"
        ),
        markers=("ghs_16C7e42F292c6912E7710c838347Ae178B4a", "x-access-token"),
    ),
    Row(
        id="r10_p03_pip_index_token_private_registry",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="pip install --index-url https://__token__:pypi-AgEIcHlwaS5vcmc@gitlab.qwzcorp.internal/api/v4/projects/77665544/packages/pypi/simple qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="pip install --index-url https://forge.example.com/api/v4/projects/2001/packages/pypi/simple qwzpayments"
        ),
        markers=("gitlab.qwzcorp.internal", "pypi-AgEIcHlwaS5vcmc", "77665544"),
    ),
    Row(
        id="r10_p03b_pip_index_token_private_registry_remote_known",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="pip install --index-url https://__token__:pypi-AgEIcHlwaS5vcmc@gitlab.qwzcorp.internal/api/v4/projects/77665544/packages/pypi/simple qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="pip install --index-url https://forge.example.com/api/v4/projects/2001/packages/pypi/simple project"
        ),
        markers=("gitlab.qwzcorp.internal", "pypi-AgEIcHlwaS5vcmc", "77665544", "qwzpayments"),
    ),
    Row(
        id="r10_p04_npm_registry_token_private_host",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="npm ERR! 401 Unauthorized - GET https://npm-token:npm_AbC123@gitlab.qwzcorp.internal/api/v4/projects/77665544/packages/npm/qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="npm ERR! 401 Unauthorized - GET https://forge.example.com/api/v4/projects/2001/packages/npm/qwzpayments"
        ),
        markers=("gitlab.qwzcorp.internal", "npm_AbC123", "77665544"),
    ),
    Row(
        id="r10_p05_docker_login_deploy_token_port",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="docker login https://deploy-token-7:gldt-XyZ@gitlab.qwzcorp.internal:5050/qwzplatform/qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="docker login https://forge.example.com/org/project"),
        markers=("gitlab.qwzcorp.internal", "gldt-XyZ", "qwzpayments"),
    ),
    Row(
        id="r10_p06_curl_private_token_api_v4_id",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="curl --header 'PRIVATE-TOKEN: glpat-abc' https://gitlab.qwzcorp.internal/api/v4/projects/77665544/jobs",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="curl --header 'PRIVATE-TOKEN: glpat-abc' https://forge.example.com/api/v4/projects/2001/jobs"
        ),
        markers=("gitlab.qwzcorp.internal", "77665544"),
    ),
    Row(
        id="r10_p07_ssh_oauth2_user_and_port",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="git remote add origin ssh://oauth2:glpat-abc@gitlab.qwzcorp.internal:2222/qwzplatform/qwzpayments.git",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="git remote add origin ssh://git@forge.example.com/org/project.git"),
        markers=("gitlab.qwzcorp.internal", "oauth2", "glpat-abc", "qwzpayments"),
    ),
    Row(
        id="r10_p08_diff_marker_before_token_url",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout="+  url = https://gitlab-ci-token:tok@gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="+  url = https://forge.example.com/org/project.git\n"),
        markers=("gitlab.qwzcorp.internal", "gitlab-ci-token", "qwzpayments"),
    ),
    Row(
        id="r10_p09_gitmodules_plus_url_scp_alice",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout='diff --git a/.gitmodules b/.gitmodules\n+[submodule "vendor/qwzpayments"]\n+\tpath = vendor/qwzpayments\n+\turl = alice@gitlab.qwzcorp.internal:qwzplatform/qwzpayments.git\n',
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text='diff --git a/.gitmodules b/.gitmodules\n+[submodule "vendor/project"]\n+\tpath = vendor/project\n+\turl = git@forge.example.com:org/project.git\n'
        ),
        markers=("alice@", "gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p10_gitmodules_minus_url_scp_user_plus_ci",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/.gitmodules b/.gitmodules\n-\turl = user+ci@gitlab.qwzcorp.internal:qwzplatform/qwzpayments.git\n+\turl = https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="diff --git a/.gitmodules b/.gitmodules\n-\turl = git@forge.example.com:org/project.git\n+\turl = https://forge.example.com/org/project.git\n"
        ),
        markers=("user+ci", "gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p11_git_config_insteadof_scp_and_https",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout='diff --git a/.git/config b/.git/config\n+[url "bob@gitlab.qwzcorp.internal:"]\n+\tinsteadOf = https://gitlab.qwzcorp.internal/\n',
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Refused("unknown mailbox at exchanges[0].stdout"),
        markers=("bob@", "gitlab.qwzcorp.internal"),
    ),
    Row(
        id="r10_p12_git_config_pushurl_user_on_example",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            raw_text_stdout="diff --git a/.git/config b/.git/config\n+\tpushurl = carol@example.com:org/repo.git\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="diff --git a/.git/config b/.git/config\n+\tpushurl = git@forge.example.com:org/project.git\n"
        ),
        markers=("carol@",),
    ),
    Row(
        id="r10_p13_gitmodules_context_line_scp",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/.gitmodules b/.gitmodules\n \turl = dave@gitlab.qwzcorp.internal:qwzplatform/qwzpayments.git\n+\tbranch = main\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="diff --git a/.gitmodules b/.gitmodules\n \turl = git@forge.example.com:org/project.git\n+\tbranch = main\n"
        ),
        markers=("dave@", "gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p14_gitmodules_noreply_looking_scp_user",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            raw_text_stdout="diff --git a/.gitmodules b/.gitmodules\n+\turl = 12345+kvn_dev9@github.com:kvnteam/kvnbilling.git\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="diff --git a/.gitmodules b/.gitmodules\n+\turl = git@forge.example.com:org/project.git\n"
        ),
        markers=("kvn_dev9",),
    ),
    Row(
        id="r10_p15_glab_api_quoted_header_and_id",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -H 'Accept: application/json' /projects/77665544/issues`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Repro: `glab api -H 'Accept: application/json' /projects/2001/issues`"
        ),
        markers=("77665544",),
    ),
    Row(
        id="r10_p16_glab_api_header_equals_in_code_block",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="```sh\nglab api --header='PRIVATE-TOKEN: x' projects/77665544/merge_requests\n```",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="```sh\nglab api --header='PRIVATE-TOKEN: x' projects/2001/merge_requests\n```"
        ),
        markers=("77665544",),
    ),
    Row(
        id="r10_p17_glab_api_quoted_body_with_spaces",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body='Run `glab api -X POST -f "body=hello world" projects/77665544/notes`',
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='Run `glab api -X POST -f "body=hello world" projects/2001/notes`'),
        markers=("77665544",),
    ),
    Row(
        id="r10_p18_glab_api_unbalanced_quote_and_id",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api -f 'title=Don't fail projects/77665544/issues`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Run `glab api -f 'title=Don't fail projects/2001/issues`"),
        markers=("77665544",),
    ),
    Row(
        id="r10_p19_glab_api_argv_header_and_id",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            call_args=("api", "-H", "Accept: application/json", "/projects/77665544/issues"),
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='["api", "-H", "Accept: application/json", "/projects/2001/issues"]'),
        markers=("77665544",),
    ),
    Row(
        id="r10_p20_glab_api_two_commands_one_line",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="`glab api projects/77665544/issues && glab api projects/77665545/notes`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="`glab api projects/2001/issues && glab api projects/2002/notes`"),
        markers=("77665544", "77665545"),
    ),
    Row(
        id="r10_p21_gh_api_authorization_token_header",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="gh api -H 'Authorization: token ghp_AbC' https://api.github.com/repos/kvnteam/kvnbilling/issues",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="gh api -H 'Authorization: token ghp_AbC' https://forge.example.com/repos/org/project/issues"
        ),
        markers=("kvnbilling", "api.github.com"),
    ),
    Row(
        id="r10_p21b_gh_api_realistic_ghp_token_refused",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="gh api -H 'Authorization: token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8' https://api.github.com/repos/kvnteam/kvnbilling/issues",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Refused("refusing to write a fixture carrying"),
        markers=("ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",),
    ),
    Row(
        id="r10_p22_html_script_private_cdn_proto_rel",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='<script src="//code.kvncloud.io/kvnteam/kvnbilling/dist/app.js"></script>',
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='<script src="//forge.example.com/org/project/dist/app.js"></script>'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p23_css_url_private_cdn_font",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="@font-face { src: url(//code.kvncloud.io/kvnteam/kvnbilling/fonts/a.woff2); }",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="@font-face { src: url(//forge.example.com/org/project/fonts/a.woff2); }"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p24_js_const_cdn_single_quote_and_comment",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```js\nconst CDN = '//code.kvncloud.io/kvnteam/kvnbilling/' //cfg.dev/env/prod\n```",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="```js\nconst CDN = '//forge.example.com/org/project/' //cfg.dev/env/prod\n```"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p25_json_manifest_proto_rel_private_url",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='{"name": "app", "cdn": "//code.kvncloud.io/kvnteam/kvnbilling/v2/"}',
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='{"name": "app", "cdn": "//forge.example.com/org/project/v2/"}'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p26_diff_adding_link_href_proto_rel",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            raw_text_stdout="diff --git a/index.html b/index.html\n+<link href='//code.kvncloud.io/kvnteam/kvnbilling/a.css' rel='stylesheet'>\n",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="diff --git a/index.html b/index.html\n+<link href='//forge.example.com/org/project/a.css' rel='stylesheet'>\n"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p27_markdown_link_proto_rel_private",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Assets: [bundle](//code.kvncloud.io/kvnteam/kvnbilling/bundle.js)",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Assets: [bundle](//forge.example.com/org/project/bundle.js)"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p28_yaml_quoted_proto_rel_private",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='```yaml\ncdn_base: "//code.kvncloud.io/kvnteam/kvnbilling/"\n```',
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='```yaml\ncdn_base: "//forge.example.com/org/project/"\n```'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_p29_italic_private_url_dunder_anchor_prose_repo",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="See _https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments/blob/main/x.py#__init___ and qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="See _https://forge.example.com/org/project/blob/main/x.py_ and project"
        ),
        markers=("gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p30_private_url_balanced_parens_prose_repo",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="Doc: [x](https://gitlab.qwzcorp.internal/qwzplatform/qwzpayments/wiki/Release_(2026)) about qwzpayments_(2026)",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Doc: [x](https://forge.example.com/org/project) about project_(2026)"
        ),
        markers=("gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p31_bold_private_scp_dunder_and_prose",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="Use __git@gitlab.qwzcorp.internal:qwzplatform/qwzpayments.git__ for qwzpayments",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Use __git@forge.example.com:org/project.git__ for project"),
        markers=("gitlab.qwzcorp.internal", "qwzpayments"),
    ),
    Row(
        id="r10_p32_mention_inside_parens_and_underscores",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Thanks (_@kvn_dev9_) for the review",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Thanks (_@author_) for the review"),
        markers=("kvn_dev9",),
    ),
    Row(
        id="r10_p33_denylist_value_in_markdown_table",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="|key|value|\n|---|---|\n|secret|kvnsecret42|",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
        markers=("kvnsecret42",),
    ),
    Row(
        id="r10_p34_denylist_id_in_api_v4_url_in_stderr",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            stderr="404 https://git.kvnworks.net/api/v4/projects/84729163/jobs",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="404 https://forge.example.com/api/v4/projects/2001/jobs"),
        markers=("84729163",),
    ),
    Row(
        id="r10_o01_actions_log_checkout_public_urls",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Run actions/checkout@v4\nSyncing repository: pubteam9/pubrepo9\nFetching https://github.com/pubteam9/pubrepo9\nFrom https://pypi.org/simple/ruff/",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Run actions/checkout@v4\nSyncing repository: org/project\nFetching https://forge.example.com/org/project\nFrom https://pypi.org/simple/ruff/"
        ),
    ),
    Row(
        id="r10_o02_pip_download_files_pythonhosted",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Collecting ruff\n  Downloading https://files.pythonhosted.org/packages/ab/cd/ruff-0.15.4-py3-none-manylinux.whl (12.3 MB)\nSuccessfully installed ruff-0.15.4",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Collecting ruff\n  Downloading https://files.pythonhosted.org/packages/ab/cd/ruff-0.15.4-py3-none-manylinux.whl (12.3 MB)\nSuccessfully installed ruff-0.15.4"
        ),
    ),
    Row(
        id="r10_o03_npm_install_registry_npmjs",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="npm http fetch GET 200 https://registry.npmjs.org/zod/-/zod-3.23.8.tgz 120ms\nadded 1 package",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="npm http fetch GET 200 https://registry.npmjs.org/zod/-/zod-3.23.8.tgz 120ms\nadded 1 package"
        ),
    ),
    Row(
        id="r10_o04_docker_pull_docker_io_ghcr",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Pulling from docker.io/library/python:3.12-slim\nPulling from ghcr.io/astral-sh/uv:latest",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Pulling from docker.io/library/python:3.12-slim\nPulling from ghcr.io/astral-sh/uv:latest"
        ),
    ),
    Row(
        id="r10_o05_go_mod_download_golang_and_github",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="go: downloading golang.org/x/sync v0.7.0\ngo: downloading github.com/stretchr/testify v1.9.0",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="go: downloading golang.org/x/sync v0.7.0\ngo: downloading forge.example.com/stretchr/testify v1.9.0"
        ),
    ),
    Row(
        id="r10_o06_gitmodules_public_submodule_scp",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout='diff --git a/.gitmodules b/.gitmodules\n+[submodule "vendor/cpython"]\n+\tpath = vendor/cpython\n+\turl = git@github.com:python/cpython.git\n',
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='diff --git a/.gitmodules b/.gitmodules\n+[submodule "vendor/cpython"]\n+\tpath = vendor/cpython\n+\turl = git@forge.example.com:org/project.git\n'
        ),
    ),
    Row(
        id="r10_o07_gitmodules_https_public_submodule",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout="diff --git a/.gitmodules b/.gitmodules\n-\turl = https://github.com/psf/requests.git\n+\turl = https://github.com/psf/requests\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/.gitmodules b/.gitmodules\n-\turl = https://forge.example.com/org/project.git\n+\turl = https://forge.example.com/org/project\n"
        ),
    ),
    Row(
        id="r10_o08_git_config_example_remote_refspec",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout='diff --git a/.git/config b/.git/config\n+[remote "origin"]\n+\turl = git@example.com:org/demo.git\n+\tfetch = +refs/heads/*:refs/remotes/origin/*\n',
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='diff --git a/.git/config b/.git/config\n+[remote "origin"]\n+\turl = git@example.com:org/demo.git\n+\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        ),
    ),
    Row(
        id="r10_o09_pr_diff_headers_and_hunks",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n+import os\n-x = 1\n+x = 2 # see docs\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n+import os\n-x = 1\n+x = 2 # see docs\n"
        ),
    ),
    Row(
        id="r10_o10_glab_api_docs_two_digit_mock_id",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Try `glab api /projects/42/issues` as in the docs",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Try `glab api /projects/42/issues` as in the docs"),
    ),
    Row(
        id="r10_o11_gh_api_owner_repo_placeholders",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Run `gh api repos/:owner/:repo/pulls -f state=open --jq '.[].number'`",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Run `gh api repos/:owner/:repo/pulls -f state=open --jq '.[].number'`"
        ),
    ),
    Row(
        id="r10_o12_glab_mr_list_ci_view_graphql",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Use `glab mr list --state merged` then `glab ci view 1234` and `glab api graphql -f query='{ currentUser { id } }'`",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Use `glab mr list --state merged` then `glab ci view 1234` and `glab api graphql -f query='{ currentUser { id } }'`"
        ),
    ),
    Row(
        id="r10_o13_glab_api_projects_year_path",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Docs moved to `glab api projects/2026/q1.md`? no, see projects/2026/q1.md",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Docs moved to `glab api projects/2026/q1.md`? no, see projects/2026/q1.md"
        ),
    ),
    Row(
        id="r10_o14_react_component_double_slash_comments",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```tsx\nconst App = () => { //cfg.dev/env/prod\n  const url = base + '/api' //svc.io/v1/items\n  return <div className=\"app\" /> // render.io/root/node\n}\nexport default App //app.io/entry/main\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```tsx\nconst App = () => { //cfg.dev/env/prod\n  const url = base + '/api' //svc.io/v1/items\n  return <div className=\"app\" /> // render.io/root/node\n}\nexport default App //app.io/entry/main\n```"
        ),
    ),
    Row(
        id="r10_o15_cdnjs_script_tag_https",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>'
        ),
    ),
    Row(
        id="r10_o16_jsdelivr_script_and_html_comment",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='```html\n<script src="https://cdn.jsdelivr.net/npm/vue@3"></script> <!-- //cfg.dev/env/prod -->\n```',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='```html\n<script src="https://cdn.jsdelivr.net/npm/vue@3"></script> <!-- //cfg.dev/env/prod -->\n```'
        ),
    ),
    Row(
        id="r10_o17_css_url_data_and_local_path",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```css\n.a { background: url(/static/bg.png); }\n.b { background: url(data:image/png;base64,iVBORw0KGgo=); }\n.c { background: url(//docs.python.org/3/static/py.svg); }\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```css\n.a { background: url(/static/bg.png); }\n.b { background: url(data:image/png;base64,iVBORw0KGgo=); }\n.c { background: url(//docs.python.org/3/static/py.svg); }\n```"
        ),
    ),
    Row(
        id="r10_o18_go_embed_and_rust_doc_comments",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```go\n//go:embed static/*\nvar files embed.FS //fs.io/embed/static\n```\n```rust\n//! Crate docs: see docs.rs/serde\n/// Returns x.\nfn x() -> i32 { 1 } //ret.io/i32/one\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```go\n//go:embed static/*\nvar files embed.FS //fs.io/embed/static\n```\n```rust\n//! Crate docs: see docs.rs/serde\n/// Returns x.\nfn x() -> i32 { 1 } //ret.io/i32/one\n```"
        ),
    ),
    Row(
        id="r10_o19_java_override_comments_and_attribute_division",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```java\n@Override //spring.io/web/ctl\npublic String get() { return svc.base/api/v1; } //svc.io/v1/get\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```java\n@Override //spring.io/web/ctl\npublic String get() { return svc.base/api/v1; } //svc.io/v1/get\n```"
        ),
    ),
    Row(
        id="r10_o20_js_integer_division_comment",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```js\nconst half = total // 2 is not JS\nconst idx = Math.floor(n / 2) //math.io/floor/half\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```js\nconst half = total // 2 is not JS\nconst idx = Math.floor(n / 2) //math.io/floor/half\n```"
        ),
    ),
    Row(
        id="r10_o21_markdown_italic_parens_underscores",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Fixed _the_ bug in `__init__.py` (see _notes_(draft)_ and (parenthetical (nested)) text).",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Fixed _the_ bug in `__init__.py` (see _notes_(draft)_ and (parenthetical (nested)) text)."
        ),
    ),
    Row(
        id="r10_o22_markdown_table_underscores_kept_urls",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="|name|link|\n|---|---|\n|`re_sub`|https://docs.python.org/3/library/re.html#re.sub|\n|`__slots__`|https://docs.python.org/3/reference/datamodel.html#object.__slots__|",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="|name|link|\n|---|---|\n|`re_sub`|https://docs.python.org/3/library/re.html#re.sub|\n|`__slots__`|https://docs.python.org/3/reference/datamodel.html#object.__slots__|"
        ),
    ),
    Row(
        id="r10_o23_snake_case_and_dunder_in_prose",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Renamed get_user_id to fetch_user_id; __repr__ now calls __str__ (and __eq__).",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Renamed get_user_id to fetch_user_id; __repr__ now calls __str__ (and __eq__)."
        ),
    ),
    Row(
        id="r10_o24_wikipedia_parens_and_mdn_list",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="- [Go](https://en.wikipedia.org/wiki/Go_(programming_language))\n- [map](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/map)",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="- [Go](https://en.wikipedia.org/wiki/Go_(programming_language))\n- [map](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/map)"
        ),
    ),
    Row(
        id="r10_o25_redacted_github_link_issue_ref_italic",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="See https://github.com/python/cpython/pull/9 (issue #12) and _https://github.com/python/cpython/pull/9#issuecomment-5_.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="See https://forge.example.com/org/project/pull/9 (issue #12) and _https://forge.example.com/org/project/pull/9#issuecomment-5_."
        ),
    ),
    Row(
        id="r10_o26_bold_url_trailing_punct_and_pipes",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="**https://docs.python.org/3/**, `https://pypi.org/project/ruff/`| and (https://crates.io/crates/serde)!",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="**https://docs.python.org/3/**, `https://pypi.org/project/ruff/`| and (https://crates.io/crates/serde)!"
        ),
    ),
    Row(
        id="r10_o27_co_authored_noreply_and_example_signoff",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Co-authored-by: Dev <1234+dev@users.noreply.github.com>\nSigned-off-by: Ops <ops@example.com>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Co-authored-by: Dev <author@users.noreply.forge.example.com>\nSigned-off-by: Ops <ops@example.com>"
        ),
    ),
    Row(
        id="r10_o28_dependabot_body_compare_links_and_mention",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Bumps [ruff](https://github.com/astral-sh/ruff) from 0.15.3 to 0.15.4.\n- [Release notes](https://github.com/astral-sh/ruff/releases)\n@dependabot rebase",
            login="dependabot[bot]",
            uid=49699333,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            author="dependabot[bot]",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Bumps [ruff](https://forge.example.com/org/project) from 0.15.3 to 0.15.4.\n- [Release notes](https://forge.example.com/org/project/releases)\n@author rebase"
        ),
    ),
    Row(
        id="r10_o29_pytest_failure_paths_and_lines",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='FAILED tests/unit/test_x.py::test_y - assert 1 == 2\n  File "src/pkg/mod.py", line 42, in run\n1 failed, 120 passed in 3.2s',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='FAILED tests/unit/test_x.py::test_y - assert 1 == 2\n  File "src/pkg/mod.py", line 42, in run\n1 failed, 120 passed in 3.2s'
        ),
    ),
    Row(
        id="r10_o30_ruff_mypy_output_paths_line_col",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="src/a.py:12:5: F401 `os` imported but unused\nsrc/b.py:3: error: Incompatible types\nFound 2 errors in 2 files",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="src/a.py:12:5: F401 `os` imported but unused\nsrc/b.py:3: error: Incompatible types\nFound 2 errors in 2 files"
        ),
    ),
    Row(
        id="r10_o31_gitlab_runner_headers_and_job_url",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            forge="gitlab",
            body="Running with gitlab-runner 17.0.0\nJob succeeded\nSee https://gitlab.com/pubteam9/pubrepo9/-/jobs/1234567890",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@gitlab.com:pubteam9/pubrepo9.git",
            url="https://gitlab.com/pubteam9/pubrepo9/-/merge_requests/42",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Running with gitlab-runner 17.0.0\nJob succeeded\nSee https://forge.example.com/org/project/-/jobs/1234567890"
        ),
    ),
    Row(
        id="r10_o32_renovate_mend_io_and_docs_renovatebot",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="This PR was generated by [Mend Renovate](https://www.mend.io/). See https://docs.renovatebot.com/configuration-options/.",
            login="renovate[bot]",
            uid=29139614,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            author="renovate[bot]",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="This PR was generated by [Mend Renovate](https://www.mend.io/). See https://docs.renovatebot.com/configuration-options/."
        ),
    ),
    Row(
        id="r10_o33_yaml_workflow_uses_and_curl_example",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```yaml\n- uses: actions/setup-python@v5\n  with:\n    python-version: '3.12'\n- run: curl -sSf https://example.com/health\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```yaml\n- uses: actions/setup-python@v5\n  with:\n    python-version: '3.12'\n- run: curl -sSf https://example.com/health\n```"
        ),
    ),
    Row(
        id="r10_o34_dockerfile_from_arg_pip",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```dockerfile\nFROM python:3.12-slim\nARG PIP_INDEX_URL=https://pypi.org/simple\nRUN pip install --no-cache-dir ruff==0.15.4\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```dockerfile\nFROM python:3.12-slim\nARG PIP_INDEX_URL=https://pypi.org/simple\nRUN pip install --no-cache-dir ruff==0.15.4\n```"
        ),
    ),
    Row(
        id="r10_o35_python_floor_division_and_comments",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="```python\nhalf = n // 2  # floor division\nratio = a.b // c.d\nurl = base + '/x'  # see docs.python.org\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```python\nhalf = n // 2  # floor division\nratio = a.b // c.d\nurl = base + '/x'  # see docs.python.org\n```"
        ),
    ),
    Row(
        id="r10_o36_prose_glab_api_words_eight_digit_projects_path",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="The glab api is nice; glab apis differ; use glab-api wrapper for projects/12345678 docs",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="The glab api is nice; glab apis differ; use glab-api wrapper for projects/2001 docs"
        ),
    ),
    Row(
        id="r10_o37_gitlab_docs_and_example_api_v4_single_digit",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Per https://docs.gitlab.com/ee/api/projects.html use `curl https://gitlab.example.com/api/v4/projects/5/issues`",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Per https://docs.gitlab.com/ee/api/projects.html use `curl https://gitlab.example.com/api/v4/projects/5/issues`"
        ),
    ),
    Row(
        id="r10_o38_monorepo_year_paths_in_diff_header",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout="diff --git a/projects/2026/plan.md b/projects/2026/plan.md\n+# Plan 2026\n+See projects/2025/retro.md\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/projects/2026/plan.md b/projects/2026/plan.md\n+# Plan 2026\n+See projects/2025/retro.md\n"
        ),
    ),
    Row(
        id="r10_o39_windows_paths_and_unc",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Build at C:\\ci\\projects\\2026\\out and \\\\fileserver\\share\\dist (see .\\projects\\2024)",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Build at C:\\ci\\projects\\2026\\out and \\\\fileserver\\share\\dist (see .\\projects\\2024)"
        ),
    ),
    Row(
        id="r10_o40_semver_private_ips_and_ports",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Tested on 127.0.0.1:8000, 10.0.0.12:5432 and v1.2.3.4; see localhost:3000/health",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Tested on 127.0.0.1:8000, 10.0.0.12:5432 and v1.2.3.4; see localhost:3000/health"
        ),
    ),
    Row(
        id="r10_o41_scoped_packages_and_decorators",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body="Uses @types/node, @babel/core and @pytest.fixture; ping @dataclass",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Uses @types/node, @babel/core and @pytest.fixture; ping @dataclass"),
    ),
    Row(
        id="r10_o42_html_mailto_and_tel_links",
        source="verify10/r10_fresh_sweep.py",
        inp=Sample(
            body='<a href="mailto:support@example.com">mail</a> <a href="tel:+15551234567">call</a>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='<a href="mailto:support@example.com">mail</a> <a href="tel:+15551234567">call</a>'
        ),
    ),
    Row(
        id="r10_a_proto_rel_html_attr_spaces",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='<script src = "//code.kvncloud.io/kvnteam/kvnbilling/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_html_attr_nospace",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='<script src="//code.kvncloud.io/kvnteam/kvnbilling/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src="//forge.example.com/org/project/app.js"></script>'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_const_dq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='const CDN = "//code.kvncloud.io/kvnteam/kvnbilling/";',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='const CDN = "//forge.example.com/org/project/";'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_const_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="const CDN = '//code.kvncloud.io/kvnteam/kvnbilling/';",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="const CDN = '//forge.example.com/org/project/';"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_const_nospace",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='const CDN="//code.kvncloud.io/kvnteam/kvnbilling/";',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='const CDN="//forge.example.com/org/project/";'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_template",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="const u = `//code.kvncloud.io/kvnteam/kvnbilling/${v}.js`",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="const u = `//forge.example.com/org/project/${v}.js`"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_object_dq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='const cfg = { cdn: "//code.kvncloud.io/kvnteam/kvnbilling/" }',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='const cfg = { cdn: "//forge.example.com/org/project/" }'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_js_object_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="const cfg = { cdn: '//code.kvncloud.io/kvnteam/kvnbilling/' }",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="const cfg = { cdn: '//forge.example.com/org/project/' }"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_py_assign_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="CDN = '//code.kvncloud.io/kvnteam/kvnbilling/static/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="CDN = '//forge.example.com/org/project/static/'"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_py_assign_dq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='CDN = "//code.kvncloud.io/kvnteam/kvnbilling/static/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='CDN = "//forge.example.com/org/project/static/"'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_toml",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='cdn = "//code.kvncloud.io/kvnteam/kvnbilling/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='cdn = "//forge.example.com/org/project/"'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_ini_nospace",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="cdn=//code.kvncloud.io/kvnteam/kvnbilling/",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn=//forge.example.com/org/project/"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_dotenv_dq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='CDN_URL="//code.kvncloud.io/kvnteam/kvnbilling/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='CDN_URL="//forge.example.com/org/project/"'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_dotenv_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="CDN_URL='//code.kvncloud.io/kvnteam/kvnbilling/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="CDN_URL='//forge.example.com/org/project/'"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_yaml_dq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='cdn: "//code.kvncloud.io/kvnteam/kvnbilling/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='cdn: "//forge.example.com/org/project/"'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_yaml_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="cdn: '//code.kvncloud.io/kvnteam/kvnbilling/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_yaml_bare",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="cdn: //code.kvncloud.io/kvnteam/kvnbilling/",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: //forge.example.com/org/project/"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_go_string",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='var cdn = "//code.kvncloud.io/kvnteam/kvnbilling/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='var cdn = "//forge.example.com/org/project/"'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_rust_string",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='let cdn = "//code.kvncloud.io/kvnteam/kvnbilling/";',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='let cdn = "//forge.example.com/org/project/";'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_java_string",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='String cdn = "//code.kvncloud.io/kvnteam/kvnbilling/";',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='String cdn = "//forge.example.com/org/project/";'),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_php",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="$cdn = '//code.kvncloud.io/kvnteam/kvnbilling/';",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="$cdn = '//forge.example.com/org/project/';"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_ruby",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="CDN = '//code.kvncloud.io/kvnteam/kvnbilling/'.freeze",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="CDN = '//forge.example.com/org/project/'.freeze"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_css_url_spaces",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="background: url( //code.kvncloud.io/kvnteam/kvnbilling/bg.png );",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="background: url( //forge.example.com/org/project/bg.png );"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_md_link",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="[cdn](//code.kvncloud.io/kvnteam/kvnbilling/x.js)",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="[cdn](//forge.example.com/org/project/x.js)"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a_proto_rel_diff_js_const",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="+const CDN = '//code.kvncloud.io/kvnteam/kvnbilling/';",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="+const CDN = '//forge.example.com/org/project/';"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a2_proto_rel_with_https_html_attr_spaces",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='Primary https://code.kvncloud.io/kvnteam/kvnbilling\n<script src = "//code.kvncloud.io/kvnteam/kvnbilling/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='Primary https://forge.example.com/org/project\n<script src = "//forge.example.com/org/project/app.js"></script>'
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a2_proto_rel_with_https_js_const_sq",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="Primary https://code.kvncloud.io/kvnteam/kvnbilling\nconst CDN = '//code.kvncloud.io/kvnteam/kvnbilling/';",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Primary https://forge.example.com/org/project\nconst CDN = '//forge.example.com/org/project/';"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a2_proto_rel_with_https_toml",
        source="verify10/r10_followup.py",
        inp=Sample(
            body='Primary https://code.kvncloud.io/kvnteam/kvnbilling\ncdn = "//code.kvncloud.io/kvnteam/kvnbilling/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='Primary https://forge.example.com/org/project\ncdn = "//forge.example.com/org/project/"'
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a2_proto_rel_with_https_yaml_bare",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="Primary https://code.kvncloud.io/kvnteam/kvnbilling\ncdn: //code.kvncloud.io/kvnteam/kvnbilling/",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Primary https://forge.example.com/org/project\ncdn: //forge.example.com/org/project/"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_a2_proto_rel_with_https_css_url_spaces",
        source="verify10/r10_followup.py",
        inp=Sample(
            body="Primary https://code.kvncloud.io/kvnteam/kvnbilling\nbackground: url( //code.kvncloud.io/kvnteam/kvnbilling/bg.png );",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Primary https://forge.example.com/org/project\nbackground: url( //forge.example.com/org/project/bg.png );"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_b_git_config_diff_scp_token_insteadof",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout='diff --git a/.git/config b/.git/config\n-\turl = alice@git.mvkother.io:mvkns2/mvksecretrepo.git\n+\turl = https://x-access-token:glpat-xyz@git.mvkother.io/mvkns2/mvksecretrepo.git\n [url "git@git.mvkother.io:"]\n-\tinsteadOf = https://git.mvkother.io/\n',
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text='diff --git a/.git/config b/.git/config\n-\turl = git@forge.example.com:org/project.git\n+\turl = https://forge.example.com/org/project.git\n [url "git@forge.example.com:"]\n-\tinsteadOf = https://forge.example.com/\n'
        ),
        markers=("git.mvkother.io", "mvksecretrepo", "alice@", "glpat-xyz"),
    ),
    Row(
        id="r10_b_git_config_only_insteadof_block",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            raw_text_stdout='[url "git@git.mvkother.io:"]\n\tinsteadOf = https://git.mvkother.io/\n',
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text='[url "git@forge.example.com:"]\n\tinsteadOf = https://forge.example.com/\n'
        ),
        markers=("git.mvkother.io",),
    ),
    Row(
        id="r10_c_glab_api_apostrophe_unbalanced",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -f 'title=Don't fail projects/66554433/issues`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Repro: `glab api -f 'title=Don't fail projects/2001/issues`"),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_c_glab_api_apostrophe_closed",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -f 'title=Don't fail' projects/66554433/issues`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Repro: `glab api -f 'title=Don't fail' projects/2001/issues`"),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_c_glab_api_apostrophe_double_quoted",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body='Repro: `glab api -f "title=Don\'t fail" projects/66554433/issues`',
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text='Repro: `glab api -f "title=Don\'t fail" projects/2001/issues`'),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_c_glab_api_double_apostrophe",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api -f 'body=it''s' projects/66554433/notes`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Repro: `glab api -f 'body=it''s' projects/2001/notes`"),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_c_glab_api_second_id_in_field_value",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api projects/66554433/issues -f 'body=see projects/66554434/notes'`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Repro: `glab api projects/2001/issues -f 'body=see projects/2002/notes'`"
        ),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_d_glab_api_url_encoded_recorded_ns_sibling_repo",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api projects/kvnteam%2Fkvnbilling/issues`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Run `glab api projects/org%2Fproject/issues`"),
        markers=("kvnteam",),
    ),
    Row(
        id="r10_d_glab_api_url_encoded_third_party_and_url",
        source="verify10/r10_followup.py",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api projects/otherns%2Fotherrepo/issues` and https://git.kvnworks.net/otherns/otherrepo",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Run `glab api projects/org%2Fproject/issues` and https://forge.example.com/org/project"
        ),
        markers=("git.kvnworks.net",),
    ),
    Row(
        id="r10_rg_prose_space_proto_rel_unknown_host",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body="Loaded from //code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Loaded from //forge.example.com/org/project/v1.js"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_rg_line_start_proto_rel_unknown_host",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="//code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="//forge.example.com/org/project/v1.js"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_rg_md_reference_proto_rel_unknown_host",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="[cdn]: //code.kvncloud.io/kvnteam/kvnbilling/app.js",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="[cdn]: //forge.example.com/org/project/app.js"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_rg_ci_insteadof_scp_github",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body='git config --global url."git@github.com:".insteadOf "https://github.com/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='git config --global url."git@forge.example.com:".insteadOf "https://forge.example.com/"'
        ),
        markers=("github.com",),
    ),
    Row(
        id="r10_rg_ci_insteadof_ci_job_token_var",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body='git config --global url."https://gitlab-ci-token:${CI_JOB_TOKEN}@gitlab.com/".insteadOf "https://gitlab.com/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='git config --global url."https://forge.example.com/".insteadOf "https://forge.example.com/"'
        ),
        markers=("gitlab.com", "CI_JOB_TOKEN"),
    ),
    Row(
        id="r10_rg_ci_clone_github_token_var",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@github.com/pubteam9/pubrepo9.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("github.com", "GITHUB_TOKEN", "pubteam9"),
    ),
    Row(
        id="r10_rg_ci_clone_gitlab_token_bare_var",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body="git clone https://oauth2:$GITLAB_TOKEN@gitlab.com/pubteam9/pubrepo9.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("gitlab.com", "GITLAB_TOKEN", "pubteam9"),
    ),
    Row(
        id="r10_rg_go_code_comment_two_label",
        source="verify10/r10_regress_probe.py",
        inp=Sample(
            body="x := 1 //cfg.dev/env/prod",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="x := 1 //cfg.dev/env/prod"),
    ),
    Row(
        id="r10_ci_insteadof_ssh_scheme",
        source="verify10/r10_ci_insteadof.py",
        inp=Sample(
            body='git config --global url."ssh://git@github.com/".insteadOf "https://github.com/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='git config --global url."ssh://git@forge.example.com/".insteadOf "https://forge.example.com/"'
        ),
        markers=("github.com",),
    ),
    Row(
        id="r10_ci_insteadof_github_token_var",
        source="verify10/r10_ci_insteadof.py",
        inp=Sample(
            body='git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='git config --global url."https://forge.example.com/".insteadOf "https://forge.example.com/"'
        ),
        markers=("github.com", "GITHUB_TOKEN"),
    ),
    Row(
        id="r10_ci_insteadof_config_block",
        source="verify10/r10_ci_insteadof.py",
        inp=Sample(
            body='[url "git@github.com:"]\n\tinsteadOf = https://github.com/',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='[url "git@forge.example.com:"]\n\tinsteadOf = https://forge.example.com/'
        ),
        markers=("github.com",),
    ),
    Row(
        id="r10_ci_goprivate_insteadof",
        source="verify10/r10_ci_insteadof.py",
        inp=Sample(
            body='GOPRIVATE=github.com/pubteam9/* git config --global url."git@github.com:".insteadOf https://github.com/',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='GOPRIVATE=forge.example.com/org/* git config --global url."git@forge.example.com:".insteadOf https://forge.example.com/'
        ),
        markers=("github.com", "pubteam9"),
    ),
    Row(
        id="r10_ci_prose_scp_style_remotes",
        source="verify10/r10_ci_insteadof.py",
        inp=Sample(
            body="See git@github.com: style remotes vs https",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See git@forge.example.com: style remotes vs https"),
        markers=("github.com",),
    ),
    Row(
        id="r10_i1_d1_range_live_prose_repo_after_url",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            forge="gitlab",
            body="Open https://git.mvkother.io/mvkns2/mvksecretrepo and mvksecretrepo",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Open https://forge.example.com/org/project and project"),
        markers=("git.mvkother.io", "mvksecretrepo"),
    ),
    Row(
        id="r10_i1_d4_unbalanced_two_ids",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            forge="gitlab",
            body="Repro: `glab api projects/66554433/issues projects/66554434/notes -f 'x`",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Repro: `glab api projects/2001/issues projects/2002/notes -f 'x`"),
        markers=("66554433", "66554434"),
    ),
    Row(
        id="r10_i1_d4_argv_second_id_in_field",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            call_args=("api", "projects/66554433/issues", "-f", "body=see projects/66554433/notes"),
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text='["api", "projects/2001/issues", "-f", "body=see projects/2001/notes"]'
        ),
        markers=("66554433",),
    ),
    Row(
        id="r10_i1_item3_proto_rel_host_from_remote",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            forge="gitlab",
            body="Loaded from //git.kvnworks.net/kvnteam/kvnportal/v1.js",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@git.kvnworks.net:kvnteam/kvnportal.git",
            url="https://git.kvnworks.net/kvnteam/kvnportal/-/merge_requests/42#note_100",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Loaded from //forge.example.com/org/project/v1.js"),
        markers=("git.kvnworks.net", "kvnportal"),
    ),
    Row(
        id="r10_i1_item3_proto_rel_host_seen_https",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="Primary https://code.kvncloud.io/kvnteam/kvnbilling\nLoaded from //code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Primary https://forge.example.com/org/project\nLoaded from //forge.example.com/org/project/v1.js"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_i1_item3_proto_rel_host_seen_scp",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="Mirror git@code.kvncloud.io:kvnteam/kvnbilling.git\nLoaded from //code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(
            text="Mirror git@forge.example.com:org/project.git\nLoaded from //forge.example.com/org/project/v1.js"
        ),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_i1_item3_proto_rel_host_only_proto",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="Loaded from //code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Loaded from //forge.example.com/org/project/v1.js"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_i1_item3_proto_rel_host_seen_in_other_call",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body="Loaded from //code.kvncloud.io/kvnteam/kvnbilling/v1.js",
            login="kvn_dev9",
            uid=73194826,
            ns="kvnteam",
            repo="kvnportal",
            remote="git@github.com:kvnteam/kvnportal.git",
            extra_items=(
                {
                    "user": {"login": "kvn_dev9", "id": 73194826},
                    "body": "See https://code.kvncloud.io/kvnteam/kvnbilling",
                },
            ),
            deny=("kvnsecret42", "84729163"),
        ),
        outcome=Written(text="Loaded from //forge.example.com/org/project/v1.js"),
        markers=("code.kvncloud.io", "kvnbilling"),
    ),
    Row(
        id="r10_i1_d3_cdnjs_proto_rel_kept",
        source="verify10/r10_item1_rules.py",
        inp=Sample(
            body='<script src="//cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js">',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='<script src="//cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js">'
        ),
    ),
    Row(
        id="g_forge_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://github.com/zqgroup/zqmainrepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@github.com/zqgroup/zqmainrepo.git now",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("github.com", "zqgroup", "zqmainrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_forge_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@github.com/zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("github.com", "zqgroup", "zqmainrepo", "alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_forge_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@github.com:zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("github.com", "zqgroup", "zqmainrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_forge_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@github.com:zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//github.com/zqgroup/zqmainrepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//github.com/zqgroup/zqmainrepo/'",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://github.com/zqgroup/zqmainrepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://github.com/zqgroup/zqmainrepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://github.com/zqgroup/zqmainrepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_forge_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'zqmainrepo'...\nFrom https://github.com/zqgroup/zqmainrepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("github.com", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_https_prose",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="See https://git.zqcorp.io/zqgroup/zqmainrepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_https_userinfo",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Clone https://alice:s3cr3t@git.zqcorp.io/zqgroup/zqmainrepo.git now",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_private_known_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="git clone https://x-access-token:${GITHUB_TOKEN}@git.zqcorp.io/zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_private_known_scp_named_user",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="git remote add m alice@git.zqcorp.io:zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_private_known_scp_git_user",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="git remote add m git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='<script src = "//git.zqcorp.io/zqgroup/zqmainrepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="cdn: '//git.zqcorp.io/zqgroup/zqmainrepo/'",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_markdown_link",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Docs: [here](https://git.zqcorp.io/zqgroup/zqmainrepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_angle_brackets",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Link: <https://git.zqcorp.io/zqgroup/zqmainrepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_json_in_text",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='{"url": "https://git.zqcorp.io/zqgroup/zqmainrepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_known_ci_log_line",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Cloning into 'zqmainrepo'...\nFrom https://git.zqcorp.io/zqgroup/zqmainrepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="g_private_unknown_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://mirror.zqcorp.net/zqotherns/zqotherrepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@mirror.zqcorp.net/zqotherns/zqotherrepo.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_private_unknown_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@mirror.zqcorp.net/zqotherns/zqotherrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=(
            "mirror.zqcorp.net",
            "zqotherns",
            "zqotherrepo",
            "alice",
            "s3cr3t",
            "GITHUB_TOKEN",
        ),
    ),
    Row(
        id="g_private_unknown_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@mirror.zqcorp.net:zqotherns/zqotherrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_private_unknown_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@mirror.zqcorp.net:zqotherns/zqotherrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//mirror.zqcorp.net/zqotherns/zqotherrepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//mirror.zqcorp.net/zqotherns/zqotherrepo/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://mirror.zqcorp.net/zqotherns/zqotherrepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://mirror.zqcorp.net/zqotherns/zqotherrepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://mirror.zqcorp.net/zqotherns/zqotherrepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_private_unknown_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'zqotherrepo'...\nFrom https://mirror.zqcorp.net/zqotherns/zqotherrepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="g_public_allowlisted_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://docs.python.org/3/library/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://docs.python.org/3/library/pull/7 for details."),
    ),
    Row(
        id="g_public_allowlisted_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@docs.python.org/3/library.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("alice", "s3cr3t"),
    ),
    Row(
        id="g_public_allowlisted_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@docs.python.org/3/library.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_public_allowlisted_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//docs.python.org/3/library/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//docs.python.org/3/library/app.js"></script>'),
    ),
    Row(
        id="g_public_allowlisted_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//docs.python.org/3/library/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//docs.python.org/3/library/'"),
    ),
    Row(
        id="g_public_allowlisted_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://docs.python.org/3/library/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://docs.python.org/3/library/wiki/Home) ok"),
    ),
    Row(
        id="g_public_allowlisted_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://docs.python.org/3/library/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://docs.python.org/3/library/issues/3>"),
    ),
    Row(
        id="g_public_allowlisted_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://docs.python.org/3/library", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://docs.python.org/3/library", "n": 1}'),
    ),
    Row(
        id="g_public_allowlisted_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'library'...\nFrom https://docs.python.org/3/library\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'library'...\nFrom https://docs.python.org/3/library\n * branch main -> FETCH_HEAD"
        ),
    ),
    Row(
        id="g_public_unlisted_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://docs.vendor-unlisted.org/zqthirdns/zqthirdrepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@docs.vendor-unlisted.org/zqthirdns/zqthirdrepo.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_public_unlisted_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@docs.vendor-unlisted.org/zqthirdns/zqthirdrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=(
            "docs.vendor-unlisted.org",
            "zqthirdns",
            "zqthirdrepo",
            "alice",
            "s3cr3t",
            "GITHUB_TOKEN",
        ),
    ),
    Row(
        id="g_public_unlisted_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@docs.vendor-unlisted.org:zqthirdns/zqthirdrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_public_unlisted_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@docs.vendor-unlisted.org:zqthirdns/zqthirdrepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//docs.vendor-unlisted.org/zqthirdns/zqthirdrepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//docs.vendor-unlisted.org/zqthirdns/zqthirdrepo/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://docs.vendor-unlisted.org/zqthirdns/zqthirdrepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://docs.vendor-unlisted.org/zqthirdns/zqthirdrepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://docs.vendor-unlisted.org/zqthirdns/zqthirdrepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_public_unlisted_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'zqthirdrepo'...\nFrom https://docs.vendor-unlisted.org/zqthirdns/zqthirdrepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("docs.vendor-unlisted.org", "zqthirdns", "zqthirdrepo"),
    ),
    Row(
        id="g_rfc2606_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://gitlab.example.com/demo/sample/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://gitlab.example.com/demo/sample/pull/7 for details."),
    ),
    Row(
        id="g_rfc2606_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@gitlab.example.com/demo/sample.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("alice", "s3cr3t"),
    ),
    Row(
        id="g_rfc2606_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@gitlab.example.com/demo/sample.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_rfc2606_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@gitlab.example.com:demo/sample.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("alice", "s3cr3t"),
    ),
    Row(
        id="g_rfc2606_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@gitlab.example.com:demo/sample.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@gitlab.example.com:demo/sample.git"),
    ),
    Row(
        id="g_rfc2606_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//gitlab.example.com/demo/sample/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//gitlab.example.com/demo/sample/app.js"></script>'),
    ),
    Row(
        id="g_rfc2606_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//gitlab.example.com/demo/sample/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//gitlab.example.com/demo/sample/'"),
    ),
    Row(
        id="g_rfc2606_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://gitlab.example.com/demo/sample/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://gitlab.example.com/demo/sample/wiki/Home) ok"),
    ),
    Row(
        id="g_rfc2606_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://gitlab.example.com/demo/sample/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://gitlab.example.com/demo/sample/issues/3>"),
    ),
    Row(
        id="g_rfc2606_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://gitlab.example.com/demo/sample", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://gitlab.example.com/demo/sample", "n": 1}'),
    ),
    Row(
        id="g_rfc2606_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'sample'...\nFrom https://gitlab.example.com/demo/sample\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'sample'...\nFrom https://gitlab.example.com/demo/sample\n * branch main -> FETCH_HEAD"
        ),
    ),
    Row(
        id="g_ipv4_private_led_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://10.20.30.40/zqipns/zqiprepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@10.20.30.40/zqipns/zqiprepo.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("10.20.30.40", "zqipns", "zqiprepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_ipv4_private_led_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@10.20.30.40/zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("10.20.30.40", "zqipns", "zqiprepo", "alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_ipv4_private_led_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@10.20.30.40:zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("10.20.30.40", "zqipns", "zqiprepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_ipv4_private_led_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@10.20.30.40:zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//10.20.30.40/zqipns/zqiprepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//10.20.30.40/zqipns/zqiprepo/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://10.20.30.40/zqipns/zqiprepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://10.20.30.40/zqipns/zqiprepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://10.20.30.40/zqipns/zqiprepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_private_led_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'zqiprepo'...\nFrom https://10.20.30.40/zqipns/zqiprepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("10.20.30.40", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_https_prose",
        source="spec017-new",
        inp=Sample(
            body="See https://11.22.33.44/zqipns/zqiprepo/pull/7 for details.",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/7 for details."),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_https_userinfo",
        source="spec017-new",
        inp=Sample(
            body="Clone https://alice:s3cr3t@11.22.33.44/zqipns/zqiprepo.git now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Clone https://forge.example.com/org/project.git now"),
        markers=("11.22.33.44", "zqipns", "zqiprepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_ipv4_public_led_https_var_userinfo",
        source="spec017-new",
        inp=Sample(
            body="git clone https://x-access-token:${GITHUB_TOKEN}@11.22.33.44/zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git clone https://forge.example.com/org/project.git"),
        markers=("11.22.33.44", "zqipns", "zqiprepo", "alice", "s3cr3t", "GITHUB_TOKEN"),
    ),
    Row(
        id="g_ipv4_public_led_scp_named_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m alice@11.22.33.44:zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("11.22.33.44", "zqipns", "zqiprepo", "alice", "s3cr3t"),
    ),
    Row(
        id="g_ipv4_public_led_scp_git_user",
        source="spec017-new",
        inp=Sample(
            body="git remote add m git@11.22.33.44:zqipns/zqiprepo.git",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="git remote add m git@forge.example.com:org/project.git"),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_proto_rel_attr_spaces",
        source="spec017-new",
        inp=Sample(
            body='<script src = "//11.22.33.44/zqipns/zqiprepo/app.js"></script>',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='<script src = "//forge.example.com/org/project/app.js"></script>'),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_proto_rel_yaml_sq",
        source="spec017-new",
        inp=Sample(
            body="cdn: '//11.22.33.44/zqipns/zqiprepo/'",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cdn: '//forge.example.com/org/project/'"),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_markdown_link",
        source="spec017-new",
        inp=Sample(
            body="Docs: [here](https://11.22.33.44/zqipns/zqiprepo/wiki/Home) ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: [here](https://forge.example.com/org/project) ok"),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_angle_brackets",
        source="spec017-new",
        inp=Sample(
            body="Link: <https://11.22.33.44/zqipns/zqiprepo/issues/3>",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Link: <https://forge.example.com/org/project/issues/3>"),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_json_in_text",
        source="spec017-new",
        inp=Sample(
            body='{"url": "https://11.22.33.44/zqipns/zqiprepo", "n": 1}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"url": "https://forge.example.com/org/project", "n": 1}'),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_led_ci_log_line",
        source="spec017-new",
        inp=Sample(
            body="Cloning into 'zqiprepo'...\nFrom https://11.22.33.44/zqipns/zqiprepo\n * branch main -> FETCH_HEAD",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Cloning into 'project'...\nFrom https://forge.example.com/org/project\n * branch main -> FETCH_HEAD"
        ),
        markers=("11.22.33.44", "zqipns", "zqiprepo"),
    ),
    Row(
        id="g_ipv4_public_bare_prose_written_NG4_9",
        source="spec017-new",
        inp=Sample(
            body="ssh: connect to host 11.22.33.44 port 22: Connection refused",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="ssh: connect to host 11.22.33.44 port 22: Connection refused"),
        residual=True,
    ),
    Row(
        id="g_ipv4_private_bare_prose_written",
        source="spec017-new",
        inp=Sample(
            body="ssh: connect to host 10.20.30.40 port 22: Connection refused",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="ssh: connect to host 10.20.30.40 port 22: Connection refused"),
    ),
    Row(
        id="g_ipv4_rfc5737_bare_prose_written",
        source="spec017-new",
        inp=Sample(
            body="curl http://203.0.113.9:8080/health",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="curl http://203.0.113.9:8080/health"),
    ),
    Row(
        id="pos_title_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="see title",
            title="Mirror of https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="see title",
            title="Mirror of https://forge.example.com/org/project/-/merge_requests/9",
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_title_private_scp_and_login",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="see title",
            title="zqxjdoe7: use git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="see title", title="author: use git@forge.example.com:org/project.git"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7"),
    ),
    Row(
        id="pos_second_comment_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="first",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_items=(
                {
                    "author": {"username": "zqreviewer", "id": 55667788},
                    "body": "second: https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9",
                },
            ),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="first"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqreviewer", "55667788"),
    ),
    Row(
        id="pos_diff_plus_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/README.md b/README.md\n+See https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/README.md b/README.md\n+See https://forge.example.com/org/project/-/merge_requests/9\n"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_diff_minus_private_scp",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/.gitmodules b/.gitmodules\n-\turl = git@git.zqcorp.io:zqgroup/zqmainrepo.git\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/.gitmodules b/.gitmodules\n-\turl = git@forge.example.com:org/project.git\n"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_diff_context_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/doc.md b/doc.md\n See https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9\n+new line\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="diff --git a/doc.md b/doc.md\n See https://forge.example.com/org/project/-/merge_requests/9\n+new line\n"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_diff_plus_login_token",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/OWNERS b/OWNERS\n+reviewer: zqxjdoe7\n",
            author="zqxjdoe7",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="diff --git a/OWNERS b/OWNERS\n+reviewer: author\n"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="pos_raw_stdout_log_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="Running with gitlab-runner 17.0.0\nFetching changes from https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9\n",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Running with gitlab-runner 17.0.0\nFetching changes from https://forge.example.com/org/project/-/merge_requests/9\n"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_stderr_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stderr="error: https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9 not found",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="error: https://forge.example.com/org/project/-/merge_requests/9 not found"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_stderr_private_scp",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stderr="fatal: could not read from git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="fatal: could not read from git@forge.example.com:org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_stderr_login",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stderr="warning: zqxjdoe7 has no access",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="warning: author has no access"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="pos_stdin_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stdin='{"body": "see https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9"}',
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='{"body": "see https://forge.example.com/org/project/-/merge_requests/9"}'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_stdin_login_and_scp",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stdin="cc zqxjdoe7: git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cc author: git@forge.example.com:org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7"),
    ),
    Row(
        id="pos_top_args_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            top_args=(42, "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='[42, "https://forge.example.com/org/project/-/merge_requests/9"]'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_top_args_login",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            top_args=(42, "zqxjdoe7"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='[42, "author"]'),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="pos_top_kwargs_private_scp",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            top_kwargs={"remote": "git@git.zqcorp.io:zqgroup/zqmainrepo.git"},
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"remote": "git@forge.example.com:org/project.git"}'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_top_kwargs_login",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            top_kwargs={"assignee": "zqxjdoe7"},
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"assignee": "author"}'),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="pos_call_args_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            call_args=(
                "api",
                "projects/:id/merge_requests/42/notes",
                "-f",
                "body=see https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9",
            ),
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='["api", "projects/:id/merge_requests/42/notes", "-f", "body=see https://forge.example.com/org/project/-/merge_requests/9"]'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_call_args_login_segment",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            call_args=("api", "projects/:id/members/all?query=zqxjdoe7"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "projects/:id/members/all?query=author"]'),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="pos_api_path_encoded_project",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            api_path="projects/zqgroup%2Fzqmainrepo/merge_requests/42/notes",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "projects/org%2Fproject/merge_requests/42/notes"]'),
        markers=("zqgroup", "zqmainrepo"),
    ),
    Row(
        id="pos_body_login_in_many_forms",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="@zqxjdoe7 zqxjdoe7/x x-zqxjdoe7 ZQXJDOE7 git@git.zqcorp.io:zqxjdoe7/r",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="@author author/x x-author author git@forge.example.com:org/project"),
        markers=("zqxjdoe7", "git.zqcorp.io"),
    ),
    Row(
        id="api_glab_argv_plain_id",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            call_args=("api", "projects/88442211/merge_requests/42/notes"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "projects/2001/merge_requests/42/notes"]'),
        markers=("88442211",),
    ),
    Row(
        id="api_glab_argv_value_flag_then_id",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            call_args=("api", "-X", "GET", "-f", "per_page=100", "/projects/88442211/issues"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "-X", "GET", "-f", "per_page=100", "/projects/2001/issues"]'),
        markers=("88442211",),
    ),
    Row(
        id="api_glab_argv_two_ids",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="ok",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            call_args=("api", "projects/88442211/issues", "-f", "body=see projects/88442212/notes"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='["api", "projects/2001/issues", "-f", "body=see projects/2002/notes"]'
        ),
        markers=("88442211", "88442212"),
    ),
    Row(
        id="api_gh_argv_owner_repo_templates",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            call_args=("api", "repos/:owner/:repo/issues/42/comments", "--paginate"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "repos/:owner/:repo/issues/42/comments", "--paginate"]'),
    ),
    Row(
        id="api_gh_argv_real_namespace",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            call_args=("api", "repos/pubteam9/pubrepo9/issues/42/comments"),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='["api", "repos/org/project/issues/42/comments"]'),
        markers=("pubteam9", "pubrepo9"),
    ),
    Row(
        id="api_text_glab_api_five_digit_id",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Run `glab api projects/12345/issues`",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Run `glab api projects/2001/issues`"),
        markers=("12345",),
    ),
    Row(
        id="api_text_glab_api_four_digit_doc_id_kept",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Docs: `glab api projects/1234/issues` (example id)",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Docs: `glab api projects/1234/issues` (example id)"),
    ),
    Row(
        id="api_text_api_v4_four_digit_on_example_host_kept",
        source="spec017-new",
        inp=Sample(
            body="curl https://gitlab.example.com/api/v4/projects/1234/merge_requests",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="curl https://gitlab.example.com/api/v4/projects/1234/merge_requests"),
    ),
    Row(
        id="api_text_api_v4_eight_digit_on_private_host",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="curl https://git.zqcorp.io/api/v4/projects/88442211/merge_requests",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="curl https://forge.example.com/api/v4/projects/2001/merge_requests"),
        markers=("git.zqcorp.io", "88442211"),
    ),
    Row(
        id="api_text_web_projects_numeric_path",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="See https://git.zqcorp.io/projects/88442211/merge_requests/42",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/projects/2001/merge_requests/42"),
        markers=("git.zqcorp.io", "88442211"),
    ),
    Row(
        id="api_text_year_path_and_created_at",
        source="spec017-new",
        inp=Sample(
            body="Moved projects/2026/q1.md and /projects/2025/architecture.md",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Moved projects/2026/q1.md and /projects/2025/architecture.md"),
    ),
    Row(
        id="api_text_projects_word_no_digits",
        source="spec017-new",
        inp=Sample(
            body="The projects/ folder holds projects/alpha and projects/beta-2",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="The projects/ folder holds projects/alpha and projects/beta-2"),
    ),
    Row(
        id="api_text_gh_api_graphql_with_login_var",
        source="spec017-new",
        inp=Sample(
            body="gh api graphql -f query='{ user(login: \"zqxjdoe7\") { id } }'",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="gh api graphql -f query='{ user(login: \"author\") { id } }'"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="val_json_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='{"remote": "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9", "retries": 3}',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='{"remote": "https://forge.example.com/org/project/-/merge_requests/9", "retries": 3}'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_json_public_url",
        source="spec017-new",
        inp=Sample(
            body='{"remote": "https://pypi.org/project/ruff/", "retries": 3}',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"remote": "https://pypi.org/project/ruff/", "retries": 3}'),
    ),
    Row(
        id="val_json_scp_private",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='{"remote": "git@git.zqcorp.io:zqgroup/zqmainrepo.git", "retries": 3}',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='{"remote": "git@forge.example.com:org/project.git", "retries": 3}'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_yaml_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="remote: https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9\nretries: 3",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="remote: https://forge.example.com/org/project/-/merge_requests/9\nretries: 3"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_yaml_public_url",
        source="spec017-new",
        inp=Sample(
            body="remote: https://pypi.org/project/ruff/\nretries: 3",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="remote: https://pypi.org/project/ruff/\nretries: 3"),
    ),
    Row(
        id="val_yaml_scp_private",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="remote: git@git.zqcorp.io:zqgroup/zqmainrepo.git\nretries: 3",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="remote: git@forge.example.com:org/project.git\nretries: 3"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_toml_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='remote = "https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9"\nretries = 3',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='remote = "https://forge.example.com/org/project/-/merge_requests/9"\nretries = 3'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_toml_public_url",
        source="spec017-new",
        inp=Sample(
            body='remote = "https://pypi.org/project/ruff/"\nretries = 3',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='remote = "https://pypi.org/project/ruff/"\nretries = 3'),
    ),
    Row(
        id="val_toml_scp_private",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body='remote = "git@git.zqcorp.io:zqgroup/zqmainrepo.git"\nretries = 3',
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text='remote = "git@forge.example.com:org/project.git"\nretries = 3'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_ini_private_url",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="[origin]\nremote=https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/9\nretries=3",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="[origin]\nremote=https://forge.example.com/org/project/-/merge_requests/9\nretries=3"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="val_ini_public_url",
        source="spec017-new",
        inp=Sample(
            body="[origin]\nremote=https://pypi.org/project/ruff/\nretries=3",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="[origin]\nremote=https://pypi.org/project/ruff/\nretries=3"),
    ),
    Row(
        id="val_ini_scp_private",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="[origin]\nremote=git@git.zqcorp.io:zqgroup/zqmainrepo.git\nretries=3",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="[origin]\nremote=git@forge.example.com:org/project.git\nretries=3"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="code_go_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body='```go\npackage main //pkg.io/main/entry\nimport "net/http" //std.net/http/client\nfunc main() { x := cfg.Load() //cfg.dev/env/prod\n\tsrv := http.Server{Addr: ":8080"} //srv.io/listen/addr\n}\n```',
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text='```go\npackage main //pkg.io/main/entry\nimport "net/http" //std.net/http/client\nfunc main() { x := cfg.Load() //cfg.dev/env/prod\n\tsrv := http.Server{Addr: ":8080"} //srv.io/listen/addr\n}\n```'
        ),
    ),
    Row(
        id="code_js_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```js\nconst cfg = load() //cfg.dev/env/prod\nconst api = axios.create({ baseURL: base + '/v1' }) //api.io/client/v1\nmodule.exports = { cfg, api } //mod.io/export/all\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```js\nconst cfg = load() //cfg.dev/env/prod\nconst api = axios.create({ baseURL: base + '/v1' }) //api.io/client/v1\nmodule.exports = { cfg, api } //mod.io/export/all\n```"
        ),
    ),
    Row(
        id="code_ts_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```ts\nexport const store = createStore<State>() //store.io/cache/hit\nconst n: number = total // 2 is not a comment start in TS\ntype Cfg = typeof config.dev //types.dev/cfg/ro\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```ts\nexport const store = createStore<State>() //store.io/cache/hit\nconst n: number = total // 2 is not a comment start in TS\ntype Cfg = typeof config.dev //types.dev/cfg/ro\n```"
        ),
    ),
    Row(
        id="code_c_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```c\n#include <stdio.h> //io.std/printf/fmt\nint main(void) { //main.c/entry/point\n    if (ok) { //config.dev/env/prod\n        return 0; }\n}\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```c\n#include <stdio.h> //io.std/printf/fmt\nint main(void) { //main.c/entry/point\n    if (ok) { //config.dev/env/prod\n        return 0; }\n}\n```"
        ),
    ),
    Row(
        id="code_cpp_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```cpp\n#include <torch/torch.h> //torch.net/layers/conv\nauto m = torch::nn::Linear(2, 2); //nn.io/linear/init\nstd::cout << m->weight.sizes(); //log.io/weight/dims\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```cpp\n#include <torch/torch.h> //torch.net/layers/conv\nauto m = torch::nn::Linear(2, 2); //nn.io/linear/init\nstd::cout << m->weight.sizes(); //log.io/weight/dims\n```"
        ),
    ),
    Row(
        id="code_rust_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```rust\n//! Crate docs: see docs.rs/serde\nuse std::io; //std.io/read/write\nfn run() -> io::Result<()> { Ok(()) } //ret.io/result/ok\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```rust\n//! Crate docs: see docs.rs/serde\nuse std::io; //std.io/read/write\nfn run() -> io::Result<()> { Ok(()) } //ret.io/result/ok\n```"
        ),
    ),
    Row(
        id="code_java_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```java\npackage com.example.app; //pkg.io/reverse/dns\nimport org.apache.commons.io.FileUtils;\n@Override //spring.io/web/ctl\npublic String get() { return svc.base/api/v1; } //svc.io/v1/get\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```java\npackage com.example.app; //pkg.io/reverse/dns\nimport org.apache.commons.io.FileUtils;\n@Override //spring.io/web/ctl\npublic String get() { return svc.base/api/v1; } //svc.io/v1/get\n```"
        ),
    ),
    Row(
        id="code_python_comments_and_dotted_identifiers",
        source="spec017-new",
        inp=Sample(
            body="```python\nimport os.path  # os.path.join\nhalf = n // 2  # floor division\nratio = a.b // c.d\nnp.array(self.config.dev)  # see torch.net docs\n```",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="```python\nimport os.path  # os.path.join\nhalf = n // 2  # floor division\nratio = a.b // c.d\nnp.array(self.config.dev)  # see torch.net docs\n```"
        ),
    ),
    Row(
        id="code_macos_bundle_and_ssl_stream_torch",
        source="spec017-new",
        inp=Sample(
            body="Open Xcode.app/Contents/Developer; load ssl.ca/certs/root.pem; use stream.io/read/write and torch.net/layers/conv",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Open Xcode.app/Contents/Developer; load ssl.ca/certs/root.pem; use stream.io/read/write and torch.net/layers/conv"
        ),
    ),
    Row(
        id="code_three_label_filenames_extension_tlds",
        source="spec017-new",
        inp=Sample(
            body="Edit config.h.in, CHANGELOG.v1.md, settings.local.py and deploy.prod.sh then run",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Edit config.h.in, CHANGELOG.v1.md, settings.local.py and deploy.prod.sh then run"
        ),
    ),
    Row(
        id="code_reverse_dns_package_names",
        source="spec017-new",
        inp=Sample(
            body="applicationId com.example.app; groupId org.apache.commons.io; io.github.zqlib.core",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="applicationId com.example.app; groupId org.apache.commons.io; io.github.zqlib.core"
        ),
    ),
    Row(
        id="code_python_dotted_modules_and_files",
        source="spec017-new",
        inp=Sample(
            body="Moved tests.unit.test_x to tests/unit/test_x.py; README.md and file.py unchanged; os.path and np.array ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Moved tests.unit.test_x to tests/unit/test_x.py; README.md and file.py unchanged; os.path and np.array ok"
        ),
    ),
    Row(
        id="code_three_label_private_host_schemeless_prose",
        source="spec017-new",
        inp=Sample(
            body="Mirror at mirror.zqcorp.net/zqotherns/zqotherrepo is stale",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Mirror at forge.example.com/org/project is stale"),
        markers=("mirror.zqcorp.net", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="code_three_label_private_host_schemeless_no_path",
        source="spec017-new",
        inp=Sample(
            body="Host mirror.zqcorp.net is down",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Host mirror.zqcorp.net is down"),
        residual=True,
    ),
    Row(
        id="code_two_label_apex_host_schemeless_prose_residual",
        source="spec017-new",
        inp=Sample(
            body="Mirror at zqcorp.io/zqotherns/zqotherrepo is stale",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Mirror at zqcorp.io/zqotherns/zqotherrepo is stale"),
        residual=True,
    ),
    Row(
        id="code_two_label_apex_host_scheme_led",
        source="spec017-new",
        inp=Sample(
            body="Mirror at https://zqcorp.io/zqotherns/zqotherrepo is stale",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Mirror at https://forge.example.com/org/project is stale"),
        markers=("zqcorp.io", "zqotherns", "zqotherrepo"),
    ),
    Row(
        id="name_user_object_name_dropped_and_prose_word",
        source="spec017-new",
        inp=Sample(
            body="Thanks Zqkowalska and marta for the review",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            extra_item={"user": {"login": "zqxjdoe7", "id": 51928374, "name": "Marta Zqkowalska"}},
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Thanks author and author for the review"),
        markers=("zqxjdoe7", "Zqkowalska"),
    ),
    Row(
        id="name_multiword_name_in_title",
        source="spec017-new",
        inp=Sample(
            body="ok",
            title="Marta Zqkowalska: fix parser",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            extra_item={"user": {"login": "zqxjdoe7", "id": 51928374, "name": "Marta Zqkowalska"}},
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="ok", title="author author: fix parser"),
        markers=("zqxjdoe7", "Zqkowalska"),
    ),
    Row(
        id="email_dropped_key_local_part_in_prose",
        source="spec017-new",
        inp=Sample(
            body="Contact zqmartak on the channel",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            extra_item={
                "user": {"login": "zqxjdoe7", "id": 51928374, "email": "zqmartak@zqmail.io"}
            },
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Contact author on the channel"),
        markers=("zqxjdoe7", "zqmartak", "zqmail.io"),
    ),
    Row(
        id="email_dropped_key_mailbox_in_prose_refused",
        source="spec017-new",
        inp=Sample(
            body="Mail zqmartak@zqmail.io please",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            extra_item={
                "user": {"login": "zqxjdoe7", "id": 51928374, "email": "zqmartak@zqmail.io"}
            },
            deny=("kvnsecret42",),
        ),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
        markers=("zqxjdoe7", "zqmartak", "zqmail.io"),
    ),
    Row(
        id="email_personal_on_private_known_host_refused",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="Mail alice@git.zqcorp.io please",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
        markers=("git.zqcorp.io",),
    ),
    Row(
        id="email_example_com_literal_written",
        source="spec017-new",
        inp=Sample(
            body="Mail support@example.com or ops@example.org please",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Mail support@example.com or ops@example.org please"),
    ),
    Row(
        id="email_sub_example_com_refused_like_the_gate",
        source="spec017-new",
        inp=Sample(
            body="Mail alice@sub.example.com please",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="deny_in_title",
        source="spec017-new",
        inp=Sample(
            body="ok",
            title="Rotate zqdenyval now",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].title"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_in_stderr",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            stderr="warning: zqdenyval expired",
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stderr"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_in_stdin",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            stdin="note zqdenyval",
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdin"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_in_top_args",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            top_args=(42, "zqdenyval"),
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at args[1]"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_in_top_kwargs",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            top_kwargs={"note": "zqdenyval"},
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at kwargs.{key 0}"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_in_diff_plus",
        source="spec017-new",
        inp=Sample(
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            raw_text_stdout="+key = zqdenyval\n",
            deny=("zqdenyval",),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout"),
        markers=("zqdenyval",),
    ),
    Row(
        id="deny_numeric_id_in_branch",
        source="spec017-new",
        inp=Sample(
            body="ok",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            head_ref="fix/91827364-crash",
            deny=("91827364",),
        ),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].headRefName"),
        markers=("91827364",),
    ),
    Row(
        id="mention_dotted_gitlab_username_known_login",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="cc @zq.marta please",
            login="zq.marta",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cc @author please"),
        markers=("zq.marta",),
    ),
    Row(
        id="mention_unknown_handle_each_position",
        source="spec017-new",
        inp=Sample(
            body="cc @zqcolleague and @zqcolleague2",
            title="cc @zqcolleague",
            login="pubdev99",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            extra_items=(
                {"user": {"login": "pubdev99", "id": 51928374}, "body": "ack @zqcolleague"},
            ),
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="cc @author-2 and @author-3", title="cc @author-2"),
        markers=("zqcolleague",),
    ),
    Row(
        id="uid_known_number_in_prose_replaced_by_synthetic",
        source="spec017-new",
        inp=Sample(
            body="user id 74928163 reported it",
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="user id 1001 reported it"),
        markers=("74928163",),
    ),
    Row(
        id="uid_known_legacy_node_in_prose_replaced_by_synthetic",
        source="spec017-new",
        inp=Sample(
            body="node MDQ6VXNlcjc0OTI4MTYz seen",
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="node U_1001 seen"),
        markers=("MDQ6VXNlcjc0OTI4MTYz",),
    ),
    Row(
        id="pid_known_repository_id_in_prose_replaced_by_synthetic",
        source="spec017-new",
        inp=Sample(
            forge="gitlab",
            body="project 88442211 was archived",
            login="pubdev99",
            uid=51928374,
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "project": {
                    "id": 88442211,
                    "name": "Zqmainrepo",
                    "path": "zqmainrepo",
                    "web_url": "https://git.zqcorp.io/zqgroup/zqmainrepo",
                }
            },
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="project 2001 was archived"),
        markers=("88442211",),
    ),
    Row(
        id="reg_offline_mixed_case_docs_github_and_gitlab",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="See https://Docs.GitHub.com/en/actions and https://Docs.GitLab.com/ee/api/",
            login="author",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="See https://docs.github.com/en/actions and https://docs.gitlab.com/ee/api/"
        ),
    ),
    Row(
        id="reg_offline_public_docs_python_and_wikipedia_parens",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="See https://DOCS.PYTHON.ORG/3/library/re.html? and https://en.wikipedia.org/wiki/Python_(programming_language)",
            login="author",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="See https://docs.python.org/3/library/re.html? and https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
    ),
    Row(
        id="reg_offline_scp_example_userinfo_and_dunder_anchor",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="Run alice@example.com:org/repo.git and https://github.com/python/cpython/blob/main/Lib/pathlib.py#__init__",
            login="author",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="Run git@forge.example.com:org/project.git and https://forge.example.com/org/project/blob/main/Lib/pathlib.py"
        ),
        markers=("alice",),
    ),
    Row(
        id="reg_offline_short_stem_cla_bot",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="Run @cla check",
            login="cla[bot]",
            uid=44556677,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="Run @bot check"),
        markers=("cla[bot]", "@cla"),
    ),
    Row(
        id="reg_offline_uppercase_bot_suffix",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="Dependabot: @dependabot rebase",
            login="Dependabot[BOT]",
            uid=44556677,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="bot: @bot rebase"),
        markers=("dependabot",),
    ),
    Row(
        id="reg_offline_placeholder_stem_project_bot",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="See https://github.com/o/r/pull/1 and @project",
            login="project[bot]",
            uid=44556677,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(text="See https://forge.example.com/org/project/pull/1 and @bot"),
        markers=("@project",),
    ),
    Row(
        id="reg_offline_system_noreply_and_actions_github",
        source="tests/unit/test_forge_recording_regression_corpus.py OFFLINE",
        inp=Sample(
            body="GitHub <noreply@github.com>; Actions <actions@github.com>; keep `https://example.com`",
            login="author",
            uid=51928374,
            ns="pubteam9",
            repo="pubrepo9",
            remote="git@github.com:pubteam9/pubrepo9.git",
            deny=("kvnsecret42",),
        ),
        outcome=Written(
            text="GitHub <noreply@forge.example.com>; Actions <noreply@forge.example.com>; keep `https://example.com`"
        ),
    ),
    Row(
        id="v17_cookie_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="Set-Cookie: host=git.zqcorp.io; Path=/",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="Set-Cookie: host=forge.example.com; Path=/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_upper_path",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="See GIT.ZQCORP.IO/ZQGROUP/ZQMAINREPO now",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="See forge.example.com/org/project now"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_pct_dots_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="host git%2Ezqcorp%2Eio here",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Refused("residual host 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_html_entity_dots_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="host git&#46;zqcorp&#46;io here",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Refused("residual host 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_json_u_escape_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="host \\u0067it.zqcorp.io here",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Refused("residual host 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_pct_encoded_url_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="next=https%3A%2F%2Fgit.zqcorp.io%2Fzqgroup%2Fzqmainrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="next=https%3A%2F%2Fforge.example.com%2Forg%2Fproject"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_pct_ns_repo",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="path zqgroup%2Fzqmainrepo here",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="path org%2Fproject here"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_double_pct_ns_repo",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="path zqgroup%252Fzqmainrepo here",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Refused("residual segment 2 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_host_port_path",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="pull git.zqcorp.io:8443/zqgroup/zqmainrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="pull forge.example.com:8443/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_unc_path",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="share \\\\git.zqcorp.io\\zqmainrepo\\x",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="share \\\\forge.example.com\\project\\x"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_git_plus_ssh_port",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="git+ssh://git@git.zqcorp.io:2222/zqgroup/zqmainrepo.git",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="git+ssh://git@forge.example.com/org/project.git"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_md_link_login",
        source="verify017/probes.py",
        inp=Sample(body="by [zqxjdoe7](https://github.com/zqxjdoe7)"),
        outcome=Written(text="by [author](https://forge.example.com/org)"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_coauthor_login_local",
        source="verify017/probes.py",
        inp=Sample(body="Co-authored-by: Marta <zqxjdoe7@zqmail.io>"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_login_possessive",
        source="verify017/probes.py",
        inp=Sample(body="zqxjdoe7's branch is stale"),
        outcome=Written(text="author's branch is stale"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_login_upper",
        source="verify017/probes.py",
        inp=Sample(body="ping ZQXJDOE7 please"),
        outcome=Written(text="ping author please"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_uid_query",
        source="verify017/probes.py",
        inp=Sample(body="https://example.com/x?user_id=74928163&v=4"),
        outcome=Written(text="https://example.com/x?user_id=1001&v=4"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_login_github_io",
        source="verify017/probes.py",
        inp=Sample(body="https://zqxjdoe7.github.io/zqmainrepo/"),
        outcome=Written(text="https://forge.example.com/org/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_ns_gitlab_io",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="pages at https://zqgroup.gitlab.io/zqmainrepo/",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="pages at https://forge.example.com/org/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_ssh_config",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="Host git.zqcorp.io\n  User zqxjdoe7\n  Port 2222",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="Host forge.example.com\n  User author\n  Port 2222"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_docker_registry_port",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="FROM git.zqcorp.io:5000/zqgroup/zqmainrepo:latest",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="FROM forge.example.com:5000/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_pip_git_https",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="pip install git+https://git.zqcorp.io/zqgroup/zqmainrepo.git@v1",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="pip install git+https://forge.example.com/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_go_get",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="go get git.zqcorp.io/zqgroup/zqmainrepo@latest",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="go get forge.example.com/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_go_import_unknown_private",
        source="verify017/probes.py",
        inp=Sample(body='import "code.kvncloud.io/kvnteam/kvnbilling/pkg"'),
        outcome=Written(text='import "forge.example.com/org/project/pkg"'),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_unknown_private_userinfo_port",
        source="verify017/probes.py",
        inp=Sample(body="https://deploy:tok@code.kvncloud.io:8443/kvnteam/kvnbilling"),
        outcome=Written(text="https://forge.example.com/org/project"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_apex_two_label_scheme_led",
        source="verify017/probes.py",
        inp=Sample(body="https://kvncloud.io/kvnteam/kvnbilling"),
        outcome=Written(text="https://forge.example.com/org/project"),
        markers=("kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_apex_two_label_unled_NG4_3",
        source="verify017/probes.py",
        inp=Sample(body="see kvncloud.io/kvnteam/kvnbilling"),
        outcome=Written(text="see kvncloud.io/kvnteam/kvnbilling"),
        residual=True,
    ),
    Row(
        id="v17_apex_two_label_pct_scheme",
        source="verify017/probes.py",
        inp=Sample(body="redirect=https%3A%2F%2Fkvncloud.io%2Fkvnteam%2Fkvnbilling"),
        outcome=Written(text="redirect=https%3A%2F%2Fforge.example.com%2Forg%2Fproject"),
        markers=("kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_three_label_pct_scheme",
        source="verify017/probes.py",
        inp=Sample(body="redirect=https%3A%2F%2Fcode.kvncloud.io%2Fkvnteam%2Fkvnbilling"),
        outcome=Written(text="redirect=https%3A%2F%2Fforge.example.com%2Forg%2Fproject"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_three_label_pct_path_only",
        source="verify017/probes.py",
        inp=Sample(body="see code.kvncloud.io%2Fkvnteam%2Fkvnbilling"),
        outcome=Written(text="see forge.example.com%2Forg%2Fproject"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_mailbox_entity_at",
        source="verify017/probes.py",
        inp=Sample(body="mail kvnperson&#64;kvnmail.io"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_mailbox_pct_at",
        source="verify017/probes.py",
        inp=Sample(body="mail kvnperson%40kvnmail.io"),
        outcome=Refused("residual host 4 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_mailto_known_login",
        source="verify017/probes.py",
        inp=Sample(body="mailto:zqxjdoe7@zqmail.io"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_personal_mailbox_unknown",
        source="verify017/probes.py",
        inp=Sample(body="write to kvnperson@kvnmail.io"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_mailbox_example_subdomain",
        source="verify017/probes.py",
        inp=Sample(body="write to kvnperson@mail.example.com"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_ipv6_doc_url_path_NG4_12",
        source="verify017/probes.py",
        inp=Sample(body="http://[2001:db8::1]/kvnteam/kvnbilling/x"),
        outcome=Written(text="http://[2001:db8::1]/kvnteam/kvnbilling/x"),
        residual=True,
    ),
    Row(
        id="v17_ipv6_url_known_path",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="http://[2001:db8::1]/zqgroup/zqmainrepo/x",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="http://[2001:db8::1]/org/project/x"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_public_ipv4_led_path",
        source="verify017/probes.py",
        inp=Sample(body="http://51.15.20.30/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("51.15.20.30", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_public_ipv4_unled",
        source="verify017/probes.py",
        inp=Sample(body="ssh to 51.15.20.30:22"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_private_ipv4_path_followed_NG4_2",
        source="verify017/probes.py",
        inp=Sample(body="see 10.1.2.3/kvnteam/kvnbilling"),
        outcome=Written(text="see 10.1.2.3/kvnteam/kvnbilling"),
        residual=True,
    ),
    Row(
        id="v17_stderr_known_url",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stderr="fatal: unable to access 'https://git.zqcorp.io/zqgroup/zqmainrepo.git/'",
        ),
        outcome=Written(
            text="fatal: unable to access 'https://forge.example.com/org/project.git/'"
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_stdin_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            stdin='{"body": "cc zqxjdoe7 at git.zqcorp.io"}',
        ),
        outcome=Written(text='{"body": "cc author at forge.example.com"}'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_kwargs_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            top_kwargs={"ref": "zqgroup/zqmainrepo@git.zqcorp.io"},
        ),
        outcome=Refused("unknown mailbox at kwargs.{key 0}"),
    ),
    Row(
        id="v17_argv_encoded_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            api_path="projects/zqgroup%2Fzqmainrepo/merge_requests/42/notes",
        ),
        outcome=Written(text='["api", "projects/org%2Fproject/merge_requests/42/notes"]'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_diff_repo_dir",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            raw_text_stdout="diff --git a/zqmainrepo/x.py b/zqmainrepo/x.py\n+import zqmainrepo\n",
        ),
        outcome=Written(text="diff --git a/project/x.py b/project/x.py\n+import project\n"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_title_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            title="zqgroup/zqmainrepo: fix",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="Looks good to me."),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_json_key_known_segment",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={"zqgroup": {"zqmainrepo": 1}},
        ),
        outcome=Written(text="Looks good to me."),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_json_key_known_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={"git.zqcorp.io": 1},
        ),
        outcome=Written(text="Looks good to me."),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_node_id_in_stderr",
        source="verify017/probes.py",
        inp=Sample(
            stderr="GraphQL: Could not resolve to a node with the global id of 'MDQ6VXNlcjc0OTI4MTYz'"
        ),
        outcome=Written(text="GraphQL: Could not resolve to a node with the global id of 'U_1001'"),
        markers=("MDQ6VXNlcjc0OTI4MTYz",),
    ),
    Row(
        id="v17_deny_pct",
        source="verify017/probes.py",
        inp=Sample(body="x zqd%65nyval y"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v17_deny_argv",
        source="verify017/probes.py",
        inp=Sample(api_path="repos/:owner/:repo/issues/42/comments?q=zqdenyval"),
        outcome=Refused("deny-list entry 1 at exchanges[0].args[1]"),
    ),
    Row(
        id="v17_deny_key",
        source="verify017/probes.py",
        inp=Sample(extra_item={"zqdenyval": 1}),
        outcome=Written(text="Looks good to me."),
        markers=("zqdenyval",),
    ),
    Row(
        id="v17_deny_in_title",
        source="verify017/probes.py",
        inp=Sample(title="zqdenyval cleanup"),
        outcome=Refused("deny-list entry 1 at exchanges[0].stdout[0].title"),
    ),
    Row(
        id="v17_prefixed_label_known_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="cdn-git.zqcorp.io/assets",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="forge.example.com/assets"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_repo_as_subdomain",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="https://zqmainrepo.git.zqcorp.io/",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="https://forge.example.com/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_login_as_subdomain_unknown",
        source="verify017/probes.py",
        inp=Sample(body="https://zqxjdoe7.kvncloud.io/"),
        outcome=Written(text="https://forge.example.com/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163", "kvncloud.io"),
    ),
    Row(
        id="v17_s3_bucket_ns",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="https://zqgroup.s3.amazonaws.com/zqmainrepo/a.tgz",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="https://forge.example.com/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_autolink_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="<https://git.zqcorp.io/zqgroup/zqmainrepo>",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="<https://forge.example.com/org/project>"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_html_comment_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="<!-- mirror: git.zqcorp.io -->",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="<!-- mirror: forge.example.com -->"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_py_import_repo",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="from zqmainrepo.core import x",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="from project.core import x"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_hash_uid",
        source="verify017/probes.py",
        inp=Sample(body="see #74928163 on the tracker"),
        outcome=Written(text="see #1001 on the tracker"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_host_trailing_dot",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="resolve git.zqcorp.io. first",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="resolve forge.example.com. first"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_login_curly_apostrophe",
        source="verify017/probes.py",
        inp=Sample(body="zqxjdoe7’s fix"),
        outcome=Written(text="author’s fix"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_insteadof_token_var",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body='git config url."https://x:${TOKEN}@git.zqcorp.io/".insteadOf ssh://git@git.zqcorp.io/',
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(
            text='git config url."https://forge.example.com/".insteadOf ssh://git@forge.example.com/'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_json_in_json_string",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body='{"payload": "{\\"host\\": \\"git.zqcorp.io\\", \\"ns\\": \\"zqgroup\\"}"}',
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(
            text='{"payload": "{\\"host\\": \\"forge.example.com\\", \\"ns\\": \\"org\\"}"}'
        ),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_upper_scheme_host",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="HTTPS://GIT.ZQCORP.IO/ZQGROUP/ZQMAINREPO/-/ISSUES/1",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="https://forge.example.com/org/project/-/ISSUES/1"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_avatar_uid",
        source="verify017/probes.py",
        inp=Sample(body="https://avatars.githubusercontent.com/u/74928163?v=4"),
        outcome=Written(text="https://forge.example.com/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_commit_author_name_no_login",
        source="verify017/probes.py",
        inp=Sample(
            body="Thanks Zqkowalska",
            extra_item={
                "commit": {"author": {"name": "Marta Zqkowalska", "email": "marta@zqmail.io"}}
            },
        ),
        outcome=Written(text="Thanks author"),
        markers=("Zqkowalska", "zqmail.io"),
    ),
    Row(
        id="v17_commits_authors_name",
        source="verify017/probes.py",
        inp=Sample(
            body="Thanks Zqkowalska",
            extra_item={
                "commits": [
                    {
                        "authors": [
                            {
                                "login": "zqxjdoe7",
                                "id": 74928163,
                                "name": "Marta Zqkowalska",
                                "email": "marta@zqmail.io",
                            }
                        ],
                        "messageHeadline": "x",
                    }
                ]
            },
        ),
        outcome=Written(text="Thanks author"),
        markers=("Zqkowalska", "zqmail.io"),
    ),
    Row(
        id="v17_assignee_no_prose_dropped_key_only",
        source="verify017/probes.py",
        inp=Sample(body="ok", extra_item={"assignees": [{"login": "zqassignee9", "id": 55443322}]}),
        outcome=Written(text="ok"),
        markers=("zqassignee9", "55443322"),
    ),
    Row(
        id="v17_reviewer_in_requested_reviewers",
        source="verify017/probes.py",
        inp=Sample(
            body="cc zqrev5",
            extra_item={"requested_reviewers": [{"login": "zqrev5", "id": 55443399}]},
        ),
        outcome=Written(text="cc author-2"),
        markers=("zqrev5", "55443399"),
    ),
    Row(
        id="v17_gitlab_user_web_url_selfhosted",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="profile https://git.zqcorp.io/zqxjdoe7",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="profile https://forge.example.com/org"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_known_host_old_gitlab_mr_route",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="see https://git.zqcorp.io/zqgroup/zqmainrepo/merge_requests/7",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="see https://forge.example.com/org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_ghe_api_url_key",
        source="verify017/probes.py",
        inp=Sample(
            extra_item={
                "url": "https://ghe.zqcorp.io/api/v3/repos/zqgroup/zqmainrepo/issues/comments/1"
            }
        ),
        outcome=Written(text="Looks good to me."),
        markers=("ghe.zqcorp.io", "zqgroup", "zqmainrepo"),
    ),
    Row(
        id="v17_gitlab_group_web_url_key",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={"group": {"web_url": "https://git.zqcorp.io/groups/zqgroup"}},
        ),
        outcome=Written(text="Looks good to me."),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_ftp_known",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="ftp://git.zqcorp.io/zqgroup/zqmainrepo/",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="ftp://forge.example.com/org/project/"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_ws_scheme_unknown",
        source="verify017/probes.py",
        inp=Sample(body="wss://code.kvncloud.io/kvnteam/kvnbilling/socket"),
        outcome=Written(text="wss://forge.example.com/org/project/socket"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v17_legacy_node_odd_padding",
        source="verify017/probes.py",
        inp=Sample(body="id MDQ6VXNlcjc0OTI4MTYz=== x"),
        outcome=Written(text="id U_1001=== x"),
        markers=("MDQ6VXNlcjc0OTI4MTYz",),
    ),
    Row(
        id="v17_legacy_node_lower",
        source="verify017/probes.py",
        inp=Sample(body="id mdq6vxnlcjc0oti4mtyz x"),
        outcome=Written(text="id U_1001 x"),
        markers=("mdq6vxnlcjc0oti4mtyz",),
    ),
    Row(
        id="v17_zero_width_split_NG4_4",
        source="verify017/probes.py",
        inp=Sample(body="zqx\u200bjdoe7 here"),
        outcome=Written(text="zqx\u200bjdoe7 here"),
        residual=True,
    ),
    Row(
        id="v17_host_in_dotenv_no_scheme",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="GIT_HOST=git.zqcorp.io\nGIT_REPO=zqgroup/zqmainrepo",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="GIT_HOST=forge.example.com\nGIT_REPO=org/project"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_priv_host_two_label_dotenv",
        source="verify017/probes.py",
        inp=Sample(body="CDN_HOST=kvncloud.io\nCDN_PATH=/kvnteam/kvnbilling"),
        outcome=Written(text="CDN_HOST=kvncloud.io\nCDN_PATH=/kvnteam/kvnbilling"),
    ),
    Row(
        id="v17_uid_padded_in_html_attr",
        source="verify017/probes.py",
        inp=Sample(body='<img data-user="74928163">'),
        outcome=Written(text='<img data-user="1001">'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_pid_projects_prose",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="glab api projects/88442211",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={"project": {"id": 88442211}},
        ),
        outcome=Written(text="glab api projects/2001"),
        markers=("88442211",),
    ),
    Row(
        id="v17_known_repo_in_c_include",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body='#include "zqmainrepo/api.h"',
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text='#include "project/api.h"'),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_known_in_yaml_anchor",
        source="verify017/probes.py",
        inp=Sample(
            forge="gitlab",
            body="repo: &r zqgroup/zqmainrepo\nhost: git.zqcorp.io",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
        ),
        outcome=Written(text="repo: &r org/project\nhost: forge.example.com"),
        markers=("git.zqcorp.io", "zqgroup", "zqmainrepo", "zqxjdoe7", "74928163"),
    ),
    Row(
        id="v17_js_response_data_id",
        source="verify017/probes.py",
        inp=Sample(body="```js\nconst userId = response.data.id;\n```"),
        outcome=Written(text="```js\nconst userId = response.data.id;\n```"),
    ),
    Row(
        id="v17_js_event_target_name",
        source="verify017/probes.py",
        inp=Sample(body="```js\nconst field = event.target.name;\n```"),
        outcome=Written(text="```js\nconst field = event.target.name;\n```"),
    ),
    Row(
        id="v17_py_self_logger_info",
        source="verify017/probes.py",
        inp=Sample(body='```py\nself.logger.info("started")\n```'),
        outcome=Written(text='```py\nself.logger.info("started")\n```'),
    ),
    Row(
        id="v17_py_request_user_email",
        source="verify017/probes.py",
        inp=Sample(body="```py\nreturn request.user.email\n```"),
        outcome=Written(text="```py\nreturn request.user.email\n```"),
    ),
    Row(
        id="v17_py_self_task_run",
        source="verify017/probes.py",
        inp=Sample(body="```py\nself.task.run()\n```"),
        outcome=Written(text="```py\nself.task.run()\n```"),
    ),
    Row(
        id="v17_py_self_server_host",
        source="verify017/probes.py",
        inp=Sample(body="```py\nhost = self.server.host\n```"),
        outcome=Written(text="```py\nhost = self.server.host\n```"),
    ),
    Row(
        id="v17_py_app_config_name",
        source="verify017/probes.py",
        inp=Sample(body="```py\nprint(app.config.name)\n```"),
        outcome=Written(text="```py\nprint(app.config.name)\n```"),
    ),
    Row(
        id="v17_js_this_props_id",
        source="verify017/probes.py",
        inp=Sample(body="```jsx\n<Item key={this.props.id} />\n```"),
        outcome=Written(text="```jsx\n<Item key={this.props.id} />\n```"),
    ),
    Row(
        id="v17_js_arr_items_at",
        source="verify017/probes.py",
        inp=Sample(body="```js\nconst last = arr.items.at(-1);\n```"),
        outcome=Written(text="```js\nconst last = arr.items.at(-1);\n```"),
    ),
    Row(
        id="v17_py_user_profile_group",
        source="verify017/probes.py",
        inp=Sample(body="```py\ng = user.profile.group\n```"),
        outcome=Written(text="```py\ng = user.profile.group\n```"),
    ),
    Row(
        id="v17_py_self_browser_page",
        source="verify017/probes.py",
        inp=Sample(body="```py\nawait self.browser.page.goto(url)\n```"),
        outcome=Written(text="```py\nawait self.browser.page.goto(url)\n```"),
    ),
    Row(
        id="v17_ts_obj_meta_info",
        source="verify017/probes.py",
        inp=Sample(body="```ts\nconst v = pkg.meta.info;\n```"),
        outcome=Written(text="```ts\nconst v = pkg.meta.info;\n```"),
    ),
    Row(
        id="v17_java_system_out_println",
        source="verify017/probes.py",
        inp=Sample(body="```java\nSystem.out.println(x);\n```"),
        outcome=Written(text="```java\nSystem.out.println(x);\n```"),
    ),
    Row(
        id="v17_java_this_config_build",
        source="verify017/probes.py",
        inp=Sample(body="```java\nreturn this.config.build();\n```"),
        outcome=Written(text="```java\nreturn this.config.build();\n```"),
    ),
    Row(
        id="v17_java_reverse_dns",
        source="verify017/probes.py",
        inp=Sample(
            body="```java\nimport com.fasterxml.jackson.core.JsonParser;\nimport org.apache.commons.io.IOUtils;\n```"
        ),
        outcome=Written(
            text="```java\nimport com.fasterxml.jackson.core.JsonParser;\nimport org.apache.commons.io.IOUtils;\n```"
        ),
    ),
    Row(
        id="v17_go_comment_pkg",
        source="verify017/probes.py",
        inp=Sample(body="```go\nx := cfg.Server.Host // default host\nfmt.Println(x)\n```"),
        outcome=Written(text="```go\nx := cfg.Server.Host // default host\nfmt.Println(x)\n```"),
    ),
    Row(
        id="v17_go_struct_fields",
        source="verify017/probes.py",
        inp=Sample(body="```go\nreturn c.Config.App.Name\n```"),
        outcome=Written(text="```go\nreturn c.Config.App.Name\n```"),
    ),
    Row(
        id="v17_rust_mod_path",
        source="verify017/probes.py",
        inp=Sample(body="```rust\nuse crate::config::app::Name;\nlet v = self.state.id;\n```"),
        outcome=Written(text="```rust\nuse crate::config::app::Name;\nlet v = self.state.id;\n```"),
    ),
    Row(
        id="v17_c_struct_access",
        source="verify017/probes.py",
        inp=Sample(body="```c\nreturn ctx->cfg.net.host; // host\n```"),
        outcome=Written(text="```c\nreturn ctx->cfg.net.host; // host\n```"),
    ),
    Row(
        id="v17_cpp_namespace",
        source="verify017/probes.py",
        inp=Sample(body="```cpp\nstd::cout << app.config.name << std::endl;\n```"),
        outcome=Written(text="```cpp\nstd::cout << app.config.name << std::endl;\n```"),
    ),
    Row(
        id="v17_ts_window_app_config",
        source="verify017/probes.py",
        inp=Sample(body="```ts\nwindow.app.config.dev = true;\n```"),
        outcome=Written(text="```ts\nwindow.app.config.dev = true;\n```"),
    ),
    Row(
        id="v17_py_os_path",
        source="verify017/probes.py",
        inp=Sample(body="```py\np = os.path.join(a, b)\nsys.stdout.write(p)\n```"),
        outcome=Written(text="```py\np = os.path.join(a, b)\nsys.stdout.write(p)\n```"),
    ),
    Row(
        id="v17_py_np_linalg_norm",
        source="verify017/probes.py",
        inp=Sample(body="```py\nn = np.linalg.norm(v)\n```"),
        outcome=Written(text="```py\nn = np.linalg.norm(v)\n```"),
    ),
    Row(
        id="v17_py_pandas_df_loc",
        source="verify017/probes.py",
        inp=Sample(body="```py\nv = df.loc[0].name\n```"),
        outcome=Written(text="```py\nv = df.loc[0].name\n```"),
    ),
    Row(
        id="v17_py_decorators",
        source="verify017/probes.py",
        inp=Sample(
            body="```py\n@pytest.mark.parametrize('x', [1])\n@app.route('/x')\n@dataclass\n```"
        ),
        outcome=Written(
            text="```py\n@pytest.mark.parametrize('x', [1])\n@app.route('/x')\n@dataclass\n```"
        ),
    ),
    Row(
        id="v17_css_at_rules",
        source="verify017/probes.py",
        inp=Sample(
            body="```css\n@media (max-width: 600px) { body { margin: 0; } }\n@import url('base.css');\n@keyframes spin { }\n```"
        ),
        outcome=Written(
            text="```css\n@media (max-width: 600px) { body { margin: 0; } }\n@import url('base.css');\n@keyframes spin { }\n```"
        ),
    ),
    Row(
        id="v17_css_font_face",
        source="verify017/probes.py",
        inp=Sample(body="```css\n@font-face { font-family: X; src: url(fonts/a.woff2); }\n```"),
        outcome=Written(
            text="```css\n@font-face { font-family: X; src: url(fonts/a.woff2); }\n```"
        ),
    ),
    Row(
        id="v17_scss_mixins",
        source="verify017/probes.py",
        inp=Sample(body="```scss\n@mixin flex { display: flex; }\n@include flex;\n```"),
        outcome=Written(text="```scss\n@mixin flex { display: flex; }\n@include flex;\n```"),
    ),
    Row(
        id="v17_version_four_part_jdk",
        source="verify017/probes.py",
        inp=Sample(body="Upgraded the JDK to 1.8.0.252 and dotnet to 4.8.1.0"),
        outcome=Written(text="Upgraded the JDK to 1.8.0.252 and dotnet to 4.8.1.0"),
    ),
    Row(
        id="v17_version_four_part_pinned_space",
        source="verify017/probes.py",
        inp=Sample(body="requires pkg == 1.2.3.4"),
        outcome=Written(text="requires pkg == 1.2.3.4"),
    ),
    Row(
        id="v17_ipv4_private_bind",
        source="verify017/probes.py",
        inp=Sample(body="Listening on 0.0.0.0:8080 and 127.0.0.1:5432"),
        outcome=Written(text="Listening on 0.0.0.0:8080 and 127.0.0.1:5432"),
    ),
    Row(
        id="v17_ipv4_doc_range",
        source="verify017/probes.py",
        inp=Sample(body="example peer 203.0.113.9 and 192.0.2.1"),
        outcome=Written(text="example peer 203.0.113.9 and 192.0.2.1"),
    ),
    Row(
        id="v17_ci_log_gha",
        source="verify017/probes.py",
        inp=Sample(
            body="Run actions/checkout@v4\n  with: fetch-depth: 0\nnpm ERR! code ERESOLVE\nnpm ERR! peer dep missing"
        ),
        outcome=Written(
            text="Run actions/checkout@v4\n  with: fetch-depth: 0\nnpm ERR! code ERESOLVE\nnpm ERR! peer dep missing"
        ),
    ),
    Row(
        id="v17_ci_log_pytest",
        source="verify017/probes.py",
        inp=Sample(
            body="FAILED tests/unit/test_x.py::test_y - AssertionError\n1 failed, 21 passed in 0.42s"
        ),
        outcome=Written(
            text="FAILED tests/unit/test_x.py::test_y - AssertionError\n1 failed, 21 passed in 0.42s"
        ),
    ),
    Row(
        id="v17_ci_log_docker_pull",
        source="verify017/probes.py",
        inp=Sample(
            body="Pulling from library/python\nDigest: sha256:ab12\nStatus: Downloaded newer image for python:3.12-slim"
        ),
        outcome=Written(
            text="Pulling from library/python\nDigest: sha256:ab12\nStatus: Downloaded newer image for python:3.12-slim"
        ),
    ),
    Row(
        id="v17_ci_log_maven",
        source="verify017/probes.py",
        inp=Sample(
            body="[INFO] Downloading from central: https://repo.maven.apache.org/maven2/org/apache/commons/commons-io/2.11.0/commons-io-2.11.0.pom"
        ),
        outcome=Written(
            text="[INFO] Downloading from central: https://repo.maven.apache.org/maven2/org/apache/commons/commons-io/2.11.0/commons-io-2.11.0.pom"
        ),
    ),
    Row(
        id="v17_yaml_k8s",
        source="verify017/probes.py",
        inp=Sample(
            body="```yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 2\n```"
        ),
        outcome=Written(
            text="```yaml\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 2\n```"
        ),
    ),
    Row(
        id="v17_yaml_gitlab_ci",
        source="verify017/probes.py",
        inp=Sample(
            body="```yaml\nimage: python:3.12\nstages: [test]\ntest:\n  script:\n    - pip install -e .\n    - pytest -q\n```"
        ),
        outcome=Written(
            text="```yaml\nimage: python:3.12\nstages: [test]\ntest:\n  script:\n    - pip install -e .\n    - pytest -q\n```"
        ),
    ),
    Row(
        id="v17_toml_pyproject",
        source="verify017/probes.py",
        inp=Sample(
            body='```toml\n[project]\nname = "demo"\nversion = "1.2.3"\nrequires-python = ">=3.12"\n```'
        ),
        outcome=Written(
            text='```toml\n[project]\nname = "demo"\nversion = "1.2.3"\nrequires-python = ">=3.12"\n```'
        ),
    ),
    Row(
        id="v17_ini_setup_cfg",
        source="verify017/probes.py",
        inp=Sample(body="```ini\n[tool:pytest]\naddopts = -q\ntestpaths = tests\n```"),
        outcome=Written(text="```ini\n[tool:pytest]\naddopts = -q\ntestpaths = tests\n```"),
    ),
    Row(
        id="v17_dockerfile",
        source="verify017/probes.py",
        inp=Sample(
            body='```dockerfile\nFROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -e .\nCMD ["python", "-m", "app"]\n```'
        ),
        outcome=Written(
            text='```dockerfile\nFROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install -e .\nCMD ["python", "-m", "app"]\n```'
        ),
    ),
    Row(
        id="v17_makefile",
        source="verify017/probes.py",
        inp=Sample(body="```make\ntest:\n\tpytest -q\nlint:\n\truff check .\n```"),
        outcome=Written(text="```make\ntest:\n\tpytest -q\nlint:\n\truff check .\n```"),
    ),
    Row(
        id="v17_nginx_conf",
        source="verify017/probes.py",
        inp=Sample(
            body="```nginx\nserver {\n  listen 80;\n  server_name _;\n  location / { proxy_pass http://127.0.0.1:8000; }\n}\n```"
        ),
        outcome=Written(
            text="```nginx\nserver {\n  listen 80;\n  server_name _;\n  location / { proxy_pass http://127.0.0.1:8000; }\n}\n```"
        ),
    ),
    Row(
        id="v17_json_package",
        source="verify017/probes.py",
        inp=Sample(
            body='```json\n{"name": "demo", "version": "1.0.0", "scripts": {"test": "vitest run"}}\n```'
        ),
        outcome=Written(
            text='```json\n{"name": "demo", "version": "1.0.0", "scripts": {"test": "vitest run"}}\n```'
        ),
    ),
    Row(
        id="v17_md_table_prose",
        source="verify017/probes.py",
        inp=Sample(
            body="| col | val |\n|---|---|\n| a | 1 |\n\nLGTM, one nit: rename `foo.bar.baz` to `qux`."
        ),
        outcome=Written(
            text="| col | val |\n|---|---|\n| a | 1 |\n\nLGTM, one nit: rename `foo.bar.baz` to `qux`."
        ),
    ),
    Row(
        id="v17_md_file_names_three_labels",
        source="verify017/probes.py",
        inp=Sample(
            body="Touched `settings.local.py`, `config.h.in`, `CHANGELOG.v1.md`, `deploy.prod.sh`, `index.d.ts`, `app.module.ts`, `jest.config.js`."
        ),
        outcome=Written(
            text="Touched `settings.local.py`, `config.h.in`, `CHANGELOG.v1.md`, `deploy.prod.sh`, `index.d.ts`, `app.module.ts`, `jest.config.js`."
        ),
    ),
    Row(
        id="v17_ts_angular_module",
        source="verify017/probes.py",
        inp=Sample(
            body="```ts\nimport { AppModule } from './app.module';\nexport const env = environment.api.url;\n```"
        ),
        outcome=Written(
            text="```ts\nimport { AppModule } from './app.module';\nexport const env = environment.api.url;\n```"
        ),
    ),
    Row(
        id="v17_py_django_settings",
        source="verify017/probes.py",
        inp=Sample(
            body="```py\nfrom django.conf import settings\nsettings.DATABASES['default']['NAME']\n```"
        ),
        outcome=Written(
            text="```py\nfrom django.conf import settings\nsettings.DATABASES['default']['NAME']\n```"
        ),
    ),
    Row(
        id="v17_py_sqlalchemy",
        source="verify017/probes.py",
        inp=Sample(body="```py\nq = session.query(User).filter(User.email == email).one()\n```"),
        outcome=Written(
            text="```py\nq = session.query(User).filter(User.email == email).one()\n```"
        ),
    ),
    Row(
        id="v17_js_process_env",
        source="verify017/probes.py",
        inp=Sample(body="```js\nconst port = process.env.PORT || 3000;\napp.listen(port);\n```"),
        outcome=Written(
            text="```js\nconst port = process.env.PORT || 3000;\napp.listen(port);\n```"
        ),
    ),
    Row(
        id="v17_js_express_req",
        source="verify017/probes.py",
        inp=Sample(
            body="```js\napp.get('/u/:id', (req, res) => res.json({ id: req.params.id }));\n```"
        ),
        outcome=Written(
            text="```js\napp.get('/u/:id', (req, res) => res.json({ id: req.params.id }));\n```"
        ),
    ),
    Row(
        id="v17_public_hosts_kept",
        source="verify017/probes.py",
        inp=Sample(
            body="See https://docs.python.org/3/library/re.html and https://pypi.org/project/ruff/ and https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
        outcome=Written(
            text="See https://docs.python.org/3/library/re.html and https://pypi.org/project/ruff/ and https://en.wikipedia.org/wiki/Python_(programming_language)"
        ),
    ),
    Row(
        id="v17_github_docs_kept",
        source="verify017/probes.py",
        inp=Sample(
            body="Per https://docs.github.com/en/rest/pulls and https://docs.gitlab.com/ee/api/"
        ),
        outcome=Written(
            text="Per https://docs.github.com/en/rest/pulls and https://docs.gitlab.com/ee/api/"
        ),
    ),
    Row(
        id="v17_shell_pipeline",
        source="verify017/probes.py",
        inp=Sample(
            body="```sh\ncat a.txt | grep -v '^#' | sort -u > b.txt\nchmod +x deploy.sh && ./deploy.sh\n```"
        ),
        outcome=Written(
            text="```sh\ncat a.txt | grep -v '^#' | sort -u > b.txt\nchmod +x deploy.sh && ./deploy.sh\n```"
        ),
    ),
    Row(
        id="v17_regex_in_code",
        source="verify017/probes.py",
        inp=Sample(body="```py\nRE = re.compile(r'^[a-z0-9.-]+\\.(com|org|io)$')\n```"),
        outcome=Written(text="```py\nRE = re.compile(r'^[a-z0-9.-]+\\.(com|org|io)$')\n```"),
    ),
    Row(
        id="v17_semver_ranges",
        source="verify017/probes.py",
        inp=Sample(
            body="Bumps react from 18.2.0 to 18.3.1 and @types/node from 20.11.0 to 20.12.7."
        ),
        outcome=Written(
            text="Bumps react from 18.2.0 to 18.3.1 and @types/node from 20.11.0 to 20.12.7."
        ),
    ),
    Row(
        id="v17_macos_bundle",
        source="verify017/probes.py",
        inp=Sample(body="Crash in /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild"),
        outcome=Written(
            text="Crash in /Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild"
        ),
    ),
    Row(
        id="v17_windows_paths",
        source="verify017/probes.py",
        inp=Sample(body="Fails on C:\\Program Files\\Git\\bin\\git.exe with exit 128"),
        outcome=Written(text="Fails on C:\\Program Files\\Git\\bin\\git.exe with exit 128"),
    ),
    Row(
        id="v17_email_rfc2606",
        source="verify017/probes.py",
        inp=Sample(body="Contact support@example.com or admin@example.org"),
        outcome=Written(text="Contact support@example.com or admin@example.org"),
    ),
    Row(
        id="v17_prose_plain_review",
        source="verify017/probes.py",
        inp=Sample(
            body="LGTM. Two nits inline; the retry loop should back off exponentially. Merging after CI."
        ),
        outcome=Written(
            text="LGTM. Two nits inline; the retry loop should back off exponentially. Merging after CI."
        ),
    ),
    Row(
        id="v17_prose_ordinary_words",
        source="verify017/probes.py",
        inp=Sample(
            body="We run the build at night, host the docs on the site and email the team the page."
        ),
        outcome=Written(
            text="We run the build at night, host the docs on the site and email the team the page."
        ),
    ),
    Row(
        id="v17_py_three_label_call_chain",
        source="verify017/probes.py",
        inp=Sample(
            body="```py\nresult = client.users.get(id=5)\nitems = resp.json().get('items')\n```"
        ),
        outcome=Written(
            text="```py\nresult = client.users.get(id=5)\nitems = resp.json().get('items')\n```"
        ),
    ),
    Row(
        id="v17_py_self_config_dev_pinned",
        source="verify017/probes.py",
        inp=Sample(body="```py\nif self.config.dev: pass\n```"),
        outcome=Written(text="```py\nif self.config.dev: pass\n```"),
    ),
    Row(
        id="v17_js_date_locale",
        source="verify017/probes.py",
        inp=Sample(
            body="```js\nconst s = new Date().toLocaleDateString('en-US');\nconsole.error(err.response.status);\n```"
        ),
        outcome=Written(
            text="```js\nconst s = new Date().toLocaleDateString('en-US');\nconsole.error(err.response.status);\n```"
        ),
    ),
    Row(
        id="v17_go_mod_file",
        source="verify017/probes.py",
        inp=Sample(
            body="```\nmodule example.com/demo\n\ngo 1.22\n\nrequire github.com/stretchr/testify v1.9.0\n```"
        ),
        outcome=Written(
            text="```\nmodule example.com/demo\n\ngo 1.22\n\nrequire forge.example.com/stretchr/testify v1.9.0\n```"
        ),
    ),
    Row(
        id="v17_rust_cargo",
        source="verify017/probes.py",
        inp=Sample(
            body='```toml\n[dependencies]\nserde = { version = "1.0", features = ["derive"] }\ntokio = "1"\n```'
        ),
        outcome=Written(
            text='```toml\n[dependencies]\nserde = { version = "1.0", features = ["derive"] }\ntokio = "1"\n```'
        ),
    ),
    Row(
        id="v17_c_includes",
        source="verify017/probes.py",
        inp=Sample(
            body='```c\n#include <stdio.h>\n#include "config.h"\nint main(void) { return 0; }\n```'
        ),
        outcome=Written(
            text='```c\n#include <stdio.h>\n#include "config.h"\nint main(void) { return 0; }\n```'
        ),
    ),
    Row(
        id="v17_cpp_templates",
        source="verify017/probes.py",
        inp=Sample(
            body="```cpp\nstd::vector<std::pair<int, std::string>> v;\nauto it = std::find_if(v.begin(), v.end(), pred);\n```"
        ),
        outcome=Written(
            text="```cpp\nstd::vector<std::pair<int, std::string>> v;\nauto it = std::find_if(v.begin(), v.end(), pred);\n```"
        ),
    ),
    Row(
        id="v17_java_annotations",
        source="verify017/probes.py",
        inp=Sample(
            body="```java\n@Override\npublic String toString() { return this.name; }\n@Test void t() {}\n```"
        ),
        outcome=Written(
            text="```java\n@Override\npublic String toString() { return this.name; }\n@Test void t() {}\n```"
        ),
    ),
    Row(
        id="v17_ci_log_gitlab_runner",
        source="verify017/probes.py",
        inp=Sample(
            body='Running with gitlab-runner 17.0.0\nPreparing the "docker" executor\nUsing Docker executor with image python:3.12 ...\nJob succeeded'
        ),
        outcome=Written(
            text='Running with gitlab-runner 17.0.0\nPreparing the "docker" executor\nUsing Docker executor with image python:3.12 ...\nJob succeeded'
        ),
    ),
    Row(
        id="v18_o_es_1",
        source="verify018/probes.py",
        inp=Sample(body="El despliegue falló en la versión 3.4.0.12; volvemos a la 3.3.9.1."),
        outcome=Written(text="El despliegue falló en la versión 3.4.0.12; volvemos a la 3.3.9.1."),
    ),
    Row(
        id="v18_o_pt_1",
        source="verify018/probes.py",
        inp=Sample(body="O pipeline quebrou na versão 1.2.0.33; revertendo para 1.1.9.8."),
        outcome=Written(text="O pipeline quebrou na versão 1.2.0.33; revertendo para 1.1.9.8."),
    ),
    Row(
        id="v18_cfg_4",
        source="verify018/probes.py",
        inp=Sample(body="resolver 1.1.1.1 8.8.8.8 valid=300s;"),
        outcome=Written(text="resolver 1.1.1.1 8.8.8.8 valid=300s;"),
    ),
    Row(
        id="v18_ver_1",
        source="verify018/probes.py",
        inp=Sample(body="Bump to 1.2.3.4 (build 20261122)"),
        outcome=Written(text="Bump to 1.2.3.4 (build 20261122)"),
    ),
    Row(
        id="v18_ver_7",
        source="verify018/probes.py",
        inp=Sample(body="firmware 2.4.1.9 -> 2.4.2.0"),
        outcome=Written(text="firmware 2.4.1.9 -> 2.4.2.0"),
    ),
    Row(
        id="v18_ver_8",
        source="verify018/probes.py",
        inp=Sample(body="pkg = 1.2.3.4"),
        outcome=Written(text="pkg = 1.2.3.4"),
    ),
    Row(
        id="v18_ipv_2",
        source="verify018/probes.py",
        inp=Sample(body="DNS: 9.9.9.9 (Quad9)"),
        outcome=Written(text="DNS: 9.9.9.9 (Quad9)"),
    ),
    Row(
        id="v18_ipv_3",
        source="verify018/probes.py",
        inp=Sample(body="release 24.4.1.2 of the appliance fixes the leak"),
        outcome=Written(text="release 24.4.1.2 of the appliance fixes the leak"),
    ),
    Row(
        id="v18_ipv_6",
        source="verify018/probes.py",
        inp=Sample(body="agent 7.52.3.101 installed"),
        outcome=Written(text="agent 7.52.3.101 installed"),
    ),
    Row(
        id="v18_ipv_8",
        source="verify018/probes.py",
        inp=Sample(body="Cisco IOS 15.2(4)M and ASA 9.12.4.1"),
        outcome=Written(text="Cisco IOS 15.2(4)M and ASA 9.12.4.1"),
    ),
    Row(
        id="v18_v01_zone_id_pct_global_led",
        source="verify018/probes.py",
        inp=Sample(body="http://[2a00:1450::1%25eth0]/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("2a00:1450::1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v18_v02_zone_id_raw_linklocal_led",
        source="verify018/probes.py",
        inp=Sample(body="http://[fe80::1%eth0]/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v18_v05_ipv4_mapped_led",
        source="verify018/probes.py",
        inp=Sample(body="http://[::ffff:51.15.20.30]/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("51.15.20.30", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v18_a05_gitlab_commit_author_name_email_keys",
        source="verify018/probes.py",
        inp=Sample(
            forge="gitlab",
            body="Zqnorek pushed",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "author_name": "Kaz Zqnorek",
                "author_email": "kaz@zqmail.io",
                "committer_name": "Kaz Zqnorek",
            },
        ),
        outcome=Written(text="author pushed"),
        markers=("Zqnorek", "zqmail.io"),
    ),
    Row(
        id="v18_v04_unbracketed_global_unled",
        source="verify018/probes.py",
        inp=Sample(body="ping 2a00:1450:4001:80b::200e now"),
        outcome=Written(text="ping 2a00:1450:4001:80b::200e now"),
        residual=True,
    ),
    Row(
        id="v18_v13_ipv6_unled_ula_path_NG4_10",
        source="verify018/probes.py",
        inp=Sample(body="see [fd00::1]/kvnteam/kvnbilling"),
        outcome=Written(text="see [fd00::1]/kvnteam/kvnbilling"),
        residual=True,
    ),
    Row(
        id="v18_followed_public_ipv4_port",
        source="verify018/probes.py",
        inp=Sample(body="connect to 104.16.0.1:8080 now"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v18_followed_public_ipv4_segment",
        source="verify018/probes.py",
        inp=Sample(body="see 104.16.0.1/kvnteam"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v18_followed_public_ipv4_port_and_path",
        source="verify018/probes.py",
        inp=Sample(body="see 104.16.0.1:8443/kvnteam/kvnbilling"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v18_bare_public_ipv4_in_prose_NG4_9",
        source="verify018/probes.py",
        inp=Sample(body="ping 104.16.0.1 now"),
        outcome=Written(text="ping 104.16.0.1 now"),
        residual=True,
    ),
    Row(
        id="v18_followed_private_ipv4_port_path_NG4_2",
        source="verify018/probes.py",
        inp=Sample(body="see 10.1.2.3:8080/kvnteam/kvnbilling"),
        outcome=Written(text="see 10.1.2.3:8080/kvnteam/kvnbilling"),
        residual=True,
    ),
    Row(
        id="v18_led_public_ipv4_mapped_in_ipv6",
        source="verify018/probes.py",
        inp=Sample(body="ssh://[::ffff:104.16.0.1]/kvnteam/kvnbilling"),
        outcome=Written(text="ssh://forge.example.com/org/project"),
        markers=("104.16.0.1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v18_gitlab_commit_author_name",
        source="verify018/probes.py",
        inp=Sample(
            forge="gitlab",
            body="Zqnorek pushed",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "commit": {"author_name": "Kazimir Zqnorek", "author_email": "kazmail@zqmail.io"}
            },
        ),
        outcome=Written(text="author pushed"),
        markers=("Zqnorek", "zqmail.io"),
    ),
    Row(
        id="v18_gitlab_commit_committer_name",
        source="verify018/probes.py",
        inp=Sample(
            forge="gitlab",
            body="Brandt reviewed",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "commit": {"committer_name": "Liana Brandt", "committer_email": "liamail@zqmail.io"}
            },
        ),
        outcome=Written(text="author reviewed"),
        markers=("Brandt", "zqmail.io"),
    ),
    Row(
        id="v18_gitlab_commit_author_mailbox_local_part",
        source="verify018/probes.py",
        inp=Sample(
            forge="gitlab",
            body="mail kazmail please",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "commit": {"author_name": "Kazimir Zqnorek", "author_email": "kazmail@zqmail.io"}
            },
        ),
        outcome=Written(text="mail author please"),
        markers=("kazmail", "zqmail.io"),
    ),
    Row(
        id="v18_gitlab_commit_committer_mailbox_local_part",
        source="verify018/probes.py",
        inp=Sample(
            forge="gitlab",
            body="mail liamail please",
            remote="git@git.zqcorp.io:zqgroup/zqmainrepo.git",
            url="https://git.zqcorp.io/zqgroup/zqmainrepo/-/merge_requests/42#note_100",
            extra_item={
                "commit": {"committer_name": "Liana Brandt", "committer_email": "liamail@zqmail.io"}
            },
        ),
        outcome=Written(text="mail author please"),
        markers=("liamail", "zqmail.io"),
    ),
    Row(
        id="v18_name_only_user_NG4_1",
        source="verify018/probes.py",
        inp=Sample(body="zqnamedonly wrote", extra_item={"user": {"name": "zqnamedonly"}}),
        outcome=Written(text="zqnamedonly wrote"),
        residual=True,
    ),
    Row(
        id="v18_login_upper_key_NG4_1",
        source="verify018/probes.py",
        inp=Sample(body="cc zqupkey", extra_item={"LOGIN": "zqupkey"}),
        outcome=Written(text="cc zqupkey"),
        residual=True,
    ),
    Row(
        id="v20_followed_private_ipv4_curl_metrics",
        source="verify019/probes.py",
        inp=Sample(body="curl 10.0.0.12:8080/metrics"),
        outcome=Written(text="curl 10.0.0.12:8080/metrics"),
    ),
    Row(
        id="v20_followed_private_ipv4_api_path",
        source="verify019/probes.py",
        inp=Sample(body="GET 10.0.0.12:8080/api/v1/health"),
        outcome=Written(text="GET 10.0.0.12:8080/api/v1/health"),
    ),
    Row(
        id="v20_followed_private_ipv4_docker_pull",
        source="verify019/probes.py",
        inp=Sample(body="docker pull 10.0.0.12:5000/library/python:3.12"),
        outcome=Written(text="docker pull 10.0.0.12:5000/library/python:3.12"),
    ),
    Row(
        id="v20_followed_private_ipv4_stderr_dial",
        source="fable020/sweep.py",
        inp=Sample(stderr="dial tcp 10.96.0.1:443/api: i/o timeout"),
        outcome=Written(text="dial tcp 10.96.0.1:443/api: i/o timeout"),
    ),
    Row(
        id="v20_led_private_ipv4_reads_namespace",
        source="fable020/sweep.py",
        inp=Sample(body="see //10.1.2.3/kvnteam/kvnbilling/x"),
        outcome=Written(text="see //forge.example.com/org/project/x"),
        markers=("10.1.2.3", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_ipv6_loopback_url_health",
        source="verify019/probes.py",
        inp=Sample(body="curl http://[::1]:8000/health"),
        outcome=Written(text="curl http://[::1]:8000/health"),
    ),
    Row(
        id="v20_ipv6_doc_url_docs",
        source="verify019/probes.py",
        inp=Sample(body="see http://[2001:db8::1]:8080/docs"),
        outcome=Written(text="see http://[2001:db8::1]:8080/docs"),
    ),
    Row(
        id="v20_localhost_api_path_not_read",
        source="fable020/sweep.py",
        inp=Sample(body="GET http://localhost:3000/api/users returns the api users"),
        outcome=Written(text="GET http://localhost:3000/api/users returns the api users"),
    ),
    Row(
        id="v20_ipv6_doc_upper_padded_path_NG4_12",
        source="verify018/probes.py",
        inp=Sample(body="http://[2001:db8:0:0::1]/kvnteam/kvnbilling"),
        outcome=Written(text="http://[2001:db8:0:0::1]/kvnteam/kvnbilling"),
        residual=True,
    ),
    Row(
        id="v20_led_private_ipv6_reads_namespace",
        source="fable020/sweep.py",
        inp=Sample(body="see //[fd00::1]/kvnteam/kvnbilling/x"),
        outcome=Written(text="see //forge.example.com/org/project/x"),
        markers=("fd00::1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_lone_segment_private_host_url_docs_word_kept",
        source="fable020/sweep.py",
        inp=Sample(body="open https://code.kvncloud.io/docs and read the docs"),
        outcome=Written(text="open https://forge.example.com/org and read the docs"),
        markers=("code.kvncloud.io",),
    ),
    Row(
        id="v20_lone_segment_private_ipv4_url_health_word_kept",
        source="fable020/sweep.py",
        inp=Sample(body="curl http://192.168.1.10:8080/health shows health"),
        outcome=Written(text="curl http://forge.example.com/org shows health"),
        markers=("192.168.1.10",),
    ),
    Row(
        id="v20_lone_segment_proto_rel_NG4_11",
        source="fable020/sweep.py",
        inp=Sample(body="see //code.kvncloud.io/kvnteam now"),
        outcome=Written(text="see //forge.example.com/kvnteam now"),
        markers=("code.kvncloud.io",),
    ),
    Row(
        id="v20_two_segments_proto_rel_reads_namespace",
        source="fable020/sweep.py",
        inp=Sample(body="see //code.kvncloud.io/kvnteam/kvnbilling now"),
        outcome=Written(text="see //forge.example.com/org/project now"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_api_root_users_reads_owner",
        source="fable020/sweep.py",
        inp=Sample(body="GET https://api.kvncloud.io/users/kvnperson for kvnperson"),
        outcome=Written(text="GET https://forge.example.com/users/org for org"),
        markers=("api.kvncloud.io", "kvnperson"),
    ),
    Row(
        id="v20_version_segment_maven_four_part",
        source="verify019/probes.py",
        inp=Sample(
            body="[INFO] Downloading from central: https://repo.maven.apache.org/maven2/com/oracle/database/jdbc/ojdbc8/19.3.0.0/ojdbc8-19.3.0.0.pom"
        ),
        outcome=Written(
            text="[INFO] Downloading from central: https://repo.maven.apache.org/maven2/com/oracle/database/jdbc/ojdbc8/19.3.0.0/ojdbc8-19.3.0.0.pom"
        ),
    ),
    Row(
        id="v20_version_segment_pypi_four_part",
        source="verify019/probes.py",
        inp=Sample(body="see https://pypi.org/project/x/1.2.3.4/files"),
        outcome=Written(text="see https://pypi.org/project/x/1.2.3.4/files"),
    ),
    Row(
        id="v20_version_segment_pythonhosted_four_part",
        source="verify019/probes.py",
        inp=Sample(body="https://files.pythonhosted.org/packages/ab/cd/12.6.1.1/x.whl"),
        outcome=Written(text="https://files.pythonhosted.org/packages/ab/cd/12.6.1.1/x.whl"),
    ),
    Row(
        id="v20_public_ipv4_as_path_segment_NG4_9",
        source="fable020/sweep.py",
        inp=Sample(body="see https://files.pythonhosted.org/104.16.0.1/json"),
        outcome=Written(text="see https://files.pythonhosted.org/104.16.0.1/json"),
        residual=True,
    ),
    Row(
        id="v20_uvicorn_running_on_unspecified_address",
        source="verify019/probes.py",
        inp=Sample(
            raw_text_stdout="INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)\n"
        ),
        outcome=Written(
            text="INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)\n"
        ),
    ),
    Row(
        id="v20_unspecified_address_led_docs",
        source="verify019/probes.py",
        inp=Sample(body="open http://0.0.0.0:8000/docs"),
        outcome=Written(text="open http://0.0.0.0:8000/docs"),
    ),
    Row(
        id="v20_broadcast_address_led",
        source="verify019/probes.py",
        inp=Sample(body="http://255.255.255.255/x"),
        outcome=Written(text="http://255.255.255.255/x"),
    ),
    Row(
        id="v20_ipv6_unspecified_led",
        source="fable020/sweep.py",
        inp=Sample(body="bind http://[::]:8080/metrics"),
        outcome=Written(text="bind http://[::]:8080/metrics"),
    ),
    Row(
        id="v20_ipv6_mapped_leading_zero_octet_led",
        source="verify019/probes.py",
        inp=Sample(body="http://[::ffff:01.2.3.4]/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("01.2.3.4", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_node_prefix_with_a_long_digit_run",
        source="fable020/crash_probe.py",
        inp=Sample(body="id U_" + "9" * 5000 + " here"),
        outcome=Written(text="id U_" + "9" * 5000 + " here"),
    ),
    Row(
        id="v20_escaped_slash_public_ipv4_url",
        source="verify019/probes.py",
        inp=Sample(body="http:\\/\\/104.16.0.1\\/kvnteam\\/kvnbilling"),
        outcome=Written(text="http:\\/\\/forge.example.com\\/org\\/project"),
        markers=("104.16.0.1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_escaped_slash_private_host_url",
        source="verify018/probes.py",
        inp=Sample(body='{"web_url":"https:\\/\\/code.kvncloud.io\\/kvnteam\\/kvnbilling"}'),
        outcome=Written(text='{"web_url":"https:\\/\\/forge.example.com\\/org\\/project"}'),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_hex_escaped_private_host_url_refused",
        source="verify018/probes.py",
        inp=Sample(body="see https:\\x2f\\x2fcode.kvncloud.io\\x2fkvnteam\\x2fkvnbilling"),
        outcome=Refused("residual host"),
    ),
    Row(
        id="v20_escaped_slash_followed_public_ipv4_refused",
        source="fable020/sweep.py",
        inp=Sample(body="see 104.16.0.1\\/kvnteam now"),
        outcome=Refused("public address at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v20_escapes_in_ordinary_code",
        source="fable020/sweep.py",
        inp=Sample(body="const re = /\\/api\\/v1\\x2f(\\d+)/; see \\/ and \\x2f alone"),
        outcome=Written(text="const re = /\\/api\\/v1\\x2f(\\d+)/; see \\/ and \\x2f alone"),
    ),
    Row(
        id="v20_ipv6_global_url_port_path_no_port_kept",
        source="fable020/sweep.py",
        inp=Sample(body="see http://[2a00:1450::1]:8443/kvnteam/kvnbilling/x now"),
        outcome=Written(text="see http://forge.example.com/org/project now"),
        markers=("2a00:1450::1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_ipv6_ula_ssh_url_port_dropped",
        source="fable020/sweep.py",
        inp=Sample(body="ssh://git@[fd00::1]:2222/kvnteam/kvnbilling.git"),
        outcome=Written(text="ssh://git@forge.example.com/org/project.git"),
        markers=("fd00::1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_ipv6_zone_url_port_path",
        source="verify019/probes.py",
        inp=Sample(body="http://[2a00:1450::1%25eth0]:443/kvnteam/kvnbilling"),
        outcome=Written(text="http://forge.example.com/org/project"),
        markers=("2a00:1450::1", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v20_ipv6_doc_upper_url_lowercased_kept",
        source="fable020/sweep.py",
        inp=Sample(body="see http://[2001:DB8::1]:8080/docs"),
        outcome=Written(text="see http://[2001:db8::1]:8080/docs"),
    ),
    Row(
        id="v20_mailbox_on_loopback_address_written",
        source="fable020/sweep.py",
        inp=Sample(body="ssh root@127.0.0.1 and scp x deploy@0.0.0.0:/tmp"),
        outcome=Written(text="ssh root@127.0.0.1 and scp x deploy@0.0.0.0:/tmp"),
    ),
    Row(
        id="v20_mailbox_on_rfc5737_address_written",
        source="fable020/sweep.py",
        inp=Sample(stderr="Permission denied for admin@203.0.113.9 (publickey)."),
        outcome=Written(text="Permission denied for admin@203.0.113.9 (publickey)."),
    ),
    Row(
        id="v20_mailbox_on_private_address_refused",
        source="fable020/sweep.py",
        inp=Sample(body="ssh deploy@10.1.2.3 now"),
        outcome=Refused("unknown mailbox at exchanges[0].stdout[0].body"),
    ),
    Row(
        id="v20_doc_tags_in_prose_are_no_mention",
        source="fable020/sweep.py",
        inp=Sample(
            body="/** @param x the value @returns the sum @throws Error @see other @var int */"
        ),
        outcome=Written(
            text="/** @param x the value @returns the sum @throws Error @see other @var int */"
        ),
    ),
    Row(
        id="v20_language_annotations_in_prose_are_no_mention",
        source="fable020/sweep.py",
        inp=Sample(
            body="add @Override and @SuppressWarnings; in Kotlin @JvmStatic, in Swift @objc @escaping"
        ),
        outcome=Written(
            text="add @Override and @SuppressWarnings; in Kotlin @JvmStatic, in Swift @objc @escaping"
        ),
    ),
    Row(
        id="v20_css_at_rules_completed_are_no_mention",
        source="fable020/sweep.py",
        inp=Sample(
            body="@color-profile and @custom-media { @config x; @font-palette-values --p {} }"
        ),
        outcome=Written(
            text="@color-profile and @custom-media { @config x; @font-palette-values --p {} }"
        ),
    ),
    Row(
        id="v20_credential_before_at_is_no_mention",
        source="fable020/sweep.py",
        inp=Sample(body="DATABASE_URL=postgres://app:${DB_PASSWORD}@localhost:5432/db"),
        outcome=Written(text="DATABASE_URL=postgres://app:${DB_PASSWORD}@localhost:5432/db"),
    ),
    Row(
        id="v20_framework_annotation_is_a_mention_residual",
        source="fable020/sweep.py",
        inp=Sample(body="the @Autowired field is null, cc @zqxjdoe7"),
        outcome=Written(text="the @author-2 field is null, cc @author"),
        markers=("zqxjdoe7",),
    ),
    Row(
        id="v20_short_handle_mention_replaced_in_at_form",
        source="fable020/sweep.py",
        inp=Sample(body="cc @bob and @zqreviewer please"),
        outcome=Written(text="cc @author-2 and @author-3 please"),
        markers=("zqreviewer",),
    ),
    Row(
        id="v20_short_handle_at_form_replaced_bare_word_kept",
        source="fable020/sweep.py",
        inp=Sample(body="cc @bob, as bob said"),
        outcome=Written(text="cc @author-2, as bob said"),
        markers=("@bob",),
    ),
    Row(
        id="v20_file_line_diagnostics_on_extension_codes",
        source="fable020/sweep.py",
        inp=Sample(
            raw_text_stdout="kernel/foo.bar.cu:12:3: error: undefined\ntop/core.alu.sv:33: syntax error\nsrc/settings.local.py:10:5: E501\n"
        ),
        outcome=Written(
            text="kernel/foo.bar.cu:12:3: error: undefined\ntop/core.alu.sv:33: syntax error\nsrc/settings.local.py:10:5: E501\n"
        ),
    ),
    Row(
        id="v20_nuget_flatcontainer_url_kept",
        source="verify019/probes.py",
        inp=Sample(body="https://api.nuget.org/v3-flatcontainer/x/1.2.3.4/x.1.2.3.4.nupkg"),
        outcome=Written(text="https://api.nuget.org/v3-flatcontainer/x/1.2.3.4/x.1.2.3.4.nupkg"),
    ),
    Row(
        id="v20_encoded_lone_segment_in_title_NG4_11",
        source="verify018/probes.py",
        inp=Sample(title="https%3A%2F%2Fcode.kvncloud.io%2Fkvnteam", body="x"),
        outcome=Written(text="x", title="https%3A%2F%2Fforge.example.com%2Fkvnteam"),
        residual=True,
    ),
    Row(
        id="v20_version_label_with_separators",
        source="fable020/sweep.py",
        inp=Sample(body="Deployed Version: 1.8.0.252/linux and version = 1.8.0.252:8443 today"),
        outcome=Written(
            text="Deployed Version: 1.8.0.252/linux and version = 1.8.0.252:8443 today"
        ),
    ),
    Row(
        id="v21_gitlab_group_route_namespace_replaced",
        source="verify020/repro_blocking.py",
        inp=Sample(body="see https://code.kvncloud.io/kvnteam/-/issues?label_name=bug now"),
        outcome=Written(text="see https://forge.example.com/org/-/issues now"),
        markers=("code.kvncloud.io", "kvnteam"),
    ),
    Row(
        id="v21_gitlab_group_route_on_the_instance",
        source="verify020/repro_blocking.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"see https://{FAKE_HOST}/kvnteam/-/epics/12 now",
        ),
        outcome=Written(text="see https://forge.example.com/org/-/epics/12 now"),
        markers=(FAKE_HOST, "kvnteam"),
    ),
    Row(
        id="v21_gitlab_group_route_proto_rel",
        source="verify020/repro_blocking.py",
        inp=Sample(body="see //code.kvncloud.io/kvnteam/-/issues now"),
        outcome=Written(text="see //forge.example.com/org/-/issues now"),
        markers=("code.kvncloud.io", "kvnteam"),
    ),
    Row(
        id="v21_gitlab_groups_route_nested_namespace",
        source="fable021/probe1.py",
        inp=Sample(body="see https://gitlab.com/groups/kvnteam/kvnsub/-/epics now"),
        outcome=Written(text="see https://forge.example.com/groups/org/-/epics now"),
        markers=("kvnteam", "kvnsub"),
    ),
    Row(
        id="v21_gitlab_user_route_snippets",
        source="fable021/probe1.py",
        inp=Sample(body="see https://code.kvncloud.io/users/kvnperson/-/snippets now"),
        outcome=Written(text="see https://forge.example.com/users/org/-/snippets now"),
        markers=("code.kvncloud.io", "kvnperson"),
    ),
    Row(
        id="v21_gitlab_group_route_word_replaced_elsewhere",
        source="fable021/probe2.py",
        inp=Sample(body="see https://code.kvncloud.io/kvnteam/-/issues and ask kvnteam"),
        outcome=Written(text="see https://forge.example.com/org/-/issues and ask org"),
        markers=("code.kvncloud.io", "kvnteam"),
    ),
    Row(
        id="v21_gitlab_jobs_route_names_no_one",
        source="fable021/probe2.py",
        inp=Sample(body="see https://code.kvncloud.io/-/jobs/5 and the jobs page"),
        outcome=Written(text="see https://forge.example.com/-/jobs/5 and the jobs page"),
        markers=("code.kvncloud.io",),
    ),
    Row(
        id="v21_language_word_mention_only_NG4_13",
        source="verify020/repro_blocking.py",
        inp=Sample(body="cc @test please review"),
        outcome=Written(text="cc @test please review"),
        residual=True,
    ),
    Row(
        id="v21_language_word_mention_only_in_title_NG4_13",
        source="verify020/repro_blocking.py",
        inp=Sample(title="fix: @link reported this", body="x"),
        outcome=Written(text="x", title="fix: @link reported this"),
        residual=True,
    ),
    Row(
        id="v21_language_word_participant_login_replaced",
        source="fable021/probe2.py",
        inp=Sample(login="test", body="cc @test please review, test"),
        outcome=Written(text="cc @author please review, author"),
        markers=("@test",),
    ),
    Row(
        id="v21_language_word_participant_login_replaced_doc_tag",
        source="fable021/probe2.py",
        inp=Sample(login="link", body="fix: @link reported this"),
        outcome=Written(text="fix: @author reported this"),
        markers=("@link",),
    ),
    Row(
        id="v21_nesting_unterminated_brackets_in_body_written",
        source="verify020/repro_blocking.py",
        inp=Sample(body="[" * 20000),
        outcome=Written(text="[" * 20000),
    ),
    Row(
        id="v21_nesting_past_the_decoder_in_stdout_written",
        source="fable021/probe2.py",
        inp=Sample(raw_text_stdout="[" * 20000 + "]" * 20000),
        outcome=Written(text="[" * 20000 + "]" * 20000),
    ),
    Row(
        id="v21_nesting_decodable_not_walkable_in_body_bounded",
        source="fable021/probe2.py",
        inp=Sample(body="[" * 990 + "]" * 990),
        outcome=Bounded(prefix="nesting too deep"),
    ),
    Row(
        id="v21_nesting_decodable_not_walkable_in_stdout_bounded",
        source="fable021/probe2.py",
        inp=Sample(raw_text_stdout='{"a":' * 990 + "1" + "}" * 990),
        outcome=Bounded(prefix="nesting too deep"),
    ),
    Row(
        id="v22_gitlab_web_ide_route_identity_replaced",
        source="verify021/cells.py",
        inp=Sample(
            body="open https://code.kvncloud.io/-/ide/project/kvnteam/kvnbilling/edit/main/-/src/app.py in the IDE"
        ),
        outcome=Written(
            text="open https://forge.example.com/-/ide/project/org/project/edit/main/-/src/app.py in the IDE"
        ),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v22_gitlab_web_ide_route_nested_on_the_instance",
        source="verify021/cells.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"open https://{FAKE_HOST}/-/ide/project/kvnteam/kvnsub/kvnbilling/blob/main/-/x.py now",
        ),
        outcome=Written(
            text="open https://forge.example.com/-/ide/project/org/project/blob/main/-/x.py now"
        ),
        markers=(FAKE_HOST, "kvnteam", "kvnsub", "kvnbilling"),
    ),
    Row(
        id="v22_gitlab_web_ide_third_party_project_on_the_instance",
        source="verify021/cells.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"open https://{FAKE_HOST}/-/ide/project/zqother/kvnbilling/edit/main/- now",
        ),
        outcome=Written(
            text="open https://forge.example.com/-/ide/project/org/project/edit/main/- now"
        ),
        markers=(FAKE_HOST, "zqother", "kvnbilling"),
    ),
    Row(
        id="v22_gitlab_reserved_word_uploads_is_written",
        source="verify021/cells.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"see https://{FAKE_HOST}/uploads/-/system/user/avatar/42/avatar.png and the uploads folder",
        ),
        outcome=Written(
            text="see https://forge.example.com/uploads/-/system/user/avatar/42/avatar.png and the uploads folder"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v22_gitea_reserved_word_repo_is_written",
        source="verify021/cells.py",
        inp=Sample(body="see https://gitea.kvncorp.net/repo/create and the repo page"),
        outcome=Written(text="see https://forge.example.com/repo/create and the repo page"),
        markers=("gitea.kvncorp.net",),
    ),
    Row(
        id="v22_github_reserved_word_marketplace_is_written",
        source="verify021/cells.py",
        inp=Sample(body="see https://github.com/marketplace/actions/checkout and the marketplace"),
        outcome=Written(
            text="see https://forge.example.com/marketplace/actions/checkout and the marketplace"
        ),
    ),
    Row(
        id="v22_bitbucket_reserved_word_account_is_written",
        source="verify021/cells.py",
        inp=Sample(body="see https://bitbucket.org/account/signin/ and the account page"),
        outcome=Written(text="see https://forge.example.com/account/signin/ and the account page"),
    ),
    Row(
        id="v22_bitbucket_server_personal_repository_replaced",
        source="verify021/cells.py",
        inp=Sample(body="see https://bb.kvncorp.net/users/kvnperson/repos/kvnbilling/browse now"),
        outcome=Written(text="see https://forge.example.com/users/org/repos/project/browse now"),
        markers=("bb.kvncorp.net", "kvnperson", "kvnbilling"),
    ),
    Row(
        id="v22_bitbucket_server_project_repository_replaced",
        source="verify021/cells.py",
        inp=Sample(
            body="see https://bb.kvncorp.net/projects/KVNKEY/repos/kvnbilling/pull-requests/5/overview now"
        ),
        outcome=Written(
            text="see https://forge.example.com/projects/org/repos/project/pull-requests/5/overview now"
        ),
        markers=("bb.kvncorp.net", "KVNKEY", "kvnbilling"),
    ),
    Row(
        id="v22_github_team_slug_replaced_elsewhere",
        source="verify021/cells.py",
        inp=Sample(body="see https://ghe.kvncorp.net/orgs/kvnteam/teams/kvnsub and ping kvnsub"),
        outcome=Written(text="see https://forge.example.com/orgs/org/teams/org and ping org"),
        markers=("ghe.kvncorp.net", "kvnteam", "kvnsub"),
    ),
    Row(
        id="v22_npm_tarball_on_the_yarn_registry_is_ordinary",
        source="verify021/cells.py",
        inp=Sample(
            body='resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#679591c564c3bffaae8454cf0b3df370c3d6911c" and lodash is fine'
        ),
        outcome=Written(
            text='resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#679591c564c3bffaae8454cf0b3df370c3d6911c" and lodash is fine'
        ),
    ),
    Row(
        id="v22_scoped_npm_tarball_on_npmmirror_is_ordinary",
        source="verify021/cells.py",
        inp=Sample(
            body="resolved https://registry.npmmirror.com/@babel/core/-/core-7.24.0.tgz now"
        ),
        outcome=Written(
            text="resolved https://registry.npmmirror.com/@babel/core/-/core-7.24.0.tgz now"
        ),
    ),
    Row(
        id="v22_npm_tarball_on_a_private_registry_names_no_one",
        source="verify021/cells.py",
        inp=Sample(
            body="resolved https://npm.kvncorp.net/lodash/-/lodash-4.17.21.tgz and lodash is fine"
        ),
        outcome=Written(
            text="resolved https://forge.example.com/lodash/-/lodash-4.17.21.tgz and lodash is fine"
        ),
        markers=("npm.kvncorp.net",),
    ),
    Row(
        id="v22_nexus_tarball_in_a_yarn_lock_diff_names_no_one",
        source="verify021/cells.py",
        inp=Sample(
            raw_text_stdout="diff --git a/yarn.lock b/yarn.lock\n--- a/yarn.lock\n+++ b/yarn.lock\n@@ -1 +1 @@\n"
            '+  resolved "https://nexus.kvncorp.net/repository/npm-all/lodash/-/lodash-4.17.21.tgz#679591c564c3bffaae8454cf0b3df370c3d6911c"\n'
            "+  lodash: ^4.17.21\n"
        ),
        outcome=Written(
            text="diff --git a/yarn.lock b/yarn.lock\n--- a/yarn.lock\n+++ b/yarn.lock\n@@ -1 +1 @@\n"
            '+  resolved "https://forge.example.com/repository/npm-all/lodash/-/lodash-4.17.21.tgz"\n'
            "+  lodash: ^4.17.21\n"
        ),
        markers=("nexus.kvncorp.net",),
    ),
    Row(
        id="v22_gitlab_s_redirect_identity_after_a_reserved_word_NG4_14",
        source="verify022/cells22.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"see https://{FAKE_HOST}/s/kvnperson and the s page",
        ),
        outcome=Written(text="see https://forge.example.com/s/kvnperson and the s page"),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_remote_namespace_reserved_word_github_assets_replaced",
        source="verify022/cells22.py",
        inp=Sample(
            remote="git@github.com:assets/zqmainrepo.git",
            body="see https://github.com/assets/zqmainrepo/-/x and zqmainrepo in assets",
        ),
        outcome=Written(text="see https://forge.example.com/org/project/-/x and project in org"),
        markers=("zqmainrepo",),
    ),
    Row(
        id="v23_remote_namespace_reserved_word_github_jwt_is_a_short_needle",
        source="verify022/cells22.py",
        inp=Sample(
            remote="git@github.com:jwt/zqmainrepo.git",
            body="see https://github.com/jwt/zqmainrepo/pull/5 and zqmainrepo again",
        ),
        outcome=Refused(prefix="needle segment 1: too short"),
    ),
    Row(
        id="v23_remote_namespace_reserved_word_gitlab_snippets_replaced",
        source="verify022/cells22.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:snippets/{FAKE_REPO}.git",
            body=f"see https://{FAKE_HOST}/snippets/{FAKE_REPO}/-/merge_requests/1 and {FAKE_REPO}",
        ),
        outcome=Written(
            text="see https://forge.example.com/org/project/-/merge_requests/1 and project"
        ),
        markers=(FAKE_HOST, FAKE_REPO),
    ),
    Row(
        id="v23_remote_namespace_reserved_word_gitlab_https_help_replaced",
        source="fable023/probe1.py",
        inp=Sample(
            forge="gitlab",
            remote=f"https://{FAKE_HOST}/help/{FAKE_REPO}.git",
            body=f"see https://{FAKE_HOST}/help/{FAKE_REPO}/-/issues/1 and the help page",
        ),
        outcome=Written(
            text="see https://forge.example.com/org/project/-/issues/1 and the org page"
        ),
        markers=(FAKE_HOST, FAKE_REPO),
    ),
    Row(
        id="v23_remote_namespace_reserved_word_is_replaced_in_a_route_too",
        source="fable023/probe1.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:uploads/{FAKE_REPO}.git",
            body=f"img https://{FAKE_HOST}/uploads/-/system/user/avatar/42/a.png and {FAKE_REPO}",
        ),
        outcome=Written(
            text="img https://forge.example.com/org/-/system/user/avatar/42/a.png and project"
        ),
        markers=(FAKE_HOST, FAKE_REPO),
    ),
    Row(
        id="v23_gitlab_npm_scope_on_the_instance_endpoint_is_ordinary_text_NG4_15",
        source="verify022/cells22.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_gitlab_npm_scope_in_the_project_endpoint_file_name_is_ordinary_text_NG4_15",
        source="fable023/probe1.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/projects/123/packages/npm/@kvnteam/kvnpkg/-/@kvnteam/kvnpkg-1.0.0.tgz now",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/projects/123/packages/npm/@kvnteam/kvnpkg/-/@kvnteam/kvnpkg-1.0.0.tgz now"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_gitlab_npm_scope_on_the_group_endpoint_is_ordinary_text_NG4_15",
        source="verify022/cells22.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/groups/123/-/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz now",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/groups/org/-/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz now"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_gitlab_npm_scope_in_the_encoded_metadata_path_is_ordinary_text_NG4_15",
        source="fable023/probe1.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"GET https://{FAKE_HOST}/api/v4/packages/npm/@kvnteam%2Fkvnpkg now",
        ),
        outcome=Written(
            text="GET https://forge.example.com/api/v4/packages/npm/@kvnteam%2Fkvnpkg now"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_gitlab_npm_scope_on_a_third_party_instance_is_ordinary_text_NG4_15",
        source="fable023/probe1.py",
        inp=Sample(
            body="resolved https://code.kvncloud.io/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz now"
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/packages/npm/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz now"
        ),
        markers=("code.kvncloud.io",),
    ),
    Row(
        id="v23_gitlab_npm_unscoped_package_names_no_one",
        source="fable023/probe1.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/projects/123/packages/npm/lodash/-/lodash-4.17.21.tgz and lodash",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/projects/123/packages/npm/lodash/-/lodash-4.17.21.tgz and lodash"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v23_scoped_tarball_on_a_private_registry_keeps_its_scope_NG4_15",
        source="verify022/cells22.py",
        inp=Sample(
            body="resolved https://npm.kvncorp.net/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg"
        ),
        outcome=Written(
            text="resolved https://forge.example.com/@kvnteam/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvnteam/kvnpkg"
        ),
        markers=("npm.kvncorp.net",),
    ),
    Row(
        id="v23_scoped_tarball_on_a_nexus_proxy_keeps_its_scope",
        source="verify022/cells22.py",
        inp=Sample(
            body="resolved https://nexus.kvncorp.net/repository/npm-all/@babel/core/-/core-7.24.0.tgz now"
        ),
        outcome=Written(
            text="resolved https://forge.example.com/repository/npm-all/@babel/core/-/core-7.24.0.tgz now"
        ),
        markers=("nexus.kvncorp.net",),
    ),
    Row(
        id="v23_api_resource_root_in_upper_case_reads_its_identity",
        source="fable023/probe1.py",
        inp=Sample(
            body="see https://code.kvncloud.io/API/V4/Repos/kvnteam/kvnbilling and kvnbilling"
        ),
        outcome=Written(text="see https://forge.example.com/API/V4/Repos/org/project and project"),
        markers=("code.kvncloud.io", "kvnteam", "kvnbilling"),
    ),
    Row(
        id="v24_nested_remote_under_reserved_root_marketplace_is_one_namespace",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:marketplace/kvnsub/{FAKE_REPO}.git",
            ns="marketplace/kvnsub",
            body=f"see https://{FAKE_HOST}/marketplace/kvnsub/{FAKE_REPO}/-/merge_requests/1 and kvnsub",
        ),
        outcome=Written(
            text="see https://forge.example.com/org/project/-/merge_requests/1 and org"
        ),
        markers=(FAKE_HOST, "kvnsub", FAKE_REPO),
    ),
    Row(
        id="v24_nested_remote_under_reserved_root_uploads_project_and_group_pages",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:uploads/kvnsub/{FAKE_REPO}.git",
            ns="uploads/kvnsub",
            body=f"see https://{FAKE_HOST}/uploads/kvnsub/{FAKE_REPO} and https://{FAKE_HOST}/uploads/kvnsub now",
        ),
        outcome=Written(
            text="see https://forge.example.com/org/project and https://forge.example.com/org now"
        ),
        markers=(FAKE_HOST, "kvnsub", FAKE_REPO),
    ),
    Row(
        id="v24_nested_remote_under_reserved_root_settings_clone_address",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:settings/kvnsub/{FAKE_REPO}.git",
            ns="settings/kvnsub",
            body=f"clone git@{FAKE_HOST}:settings/kvnsub/{FAKE_REPO}.git now",
        ),
        outcome=Written(text="clone git@forge.example.com:org/project.git now"),
        markers=(FAKE_HOST, "kvnsub", FAKE_REPO),
    ),
    Row(
        id="v24_argv_github_repos_path_with_a_reserved_owner_is_read",
        source="verify023/cells23.py",
        inp=Sample(body="ok", api_path="repos/assets/kvnbilling/pulls/5/comments"),
        outcome=Written(text='["api", "repos/org/project/pulls/5/comments"]'),
        markers=("kvnbilling", "repos/assets"),
    ),
    Row(
        id="v24_argv_gitlab_encoded_project_with_a_reserved_namespace_is_read",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body="ok",
            api_path="projects/settings%2Fkvnbilling/merge_requests/1/notes",
        ),
        outcome=Written(text='["api", "projects/org%2Fproject/merge_requests/1/notes"]'),
        markers=("kvnbilling", "settings%2f"),
    ),
    Row(
        id="v24_forwarded_types_scope_next_to_a_gitlab_npm_url_stays_in_code",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            raw_text_stdout="diff --git a/x.log b/x.log\n--- a/x.log\n+++ b/x.log\n@@ -1 +1,2 @@\n"
            f"+npm http fetch GET 200 https://{FAKE_HOST}/api/v4/packages/npm/@types%2fnode\n"
            '+import type { X } from "@types/node"\n',
        ),
        outcome=Written(
            text="diff --git a/x.log b/x.log\n--- a/x.log\n+++ b/x.log\n@@ -1 +1,2 @@\n"
            "+npm http fetch GET 200 https://forge.example.com/api/v4/packages/npm/@types%2fnode\n"
            '+import type { X } from "@types/node"\n'
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v24_forwarded_babel_scope_on_a_gitlab_project_endpoint_stays_in_code",
        source="verify023/cells23.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/projects/123/packages/npm/@babel/core/-/core-7.24.0.tgz and import x from '@babel/core'",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/projects/123/packages/npm/@babel/core/-/core-7.24.0.tgz and import x from '@babel/core'"
        ),
        markers=(FAKE_HOST,),
    ),
    Row(
        id="v24_company_scope_on_its_own_registry_is_ordinary_text_NG4_15",
        source="verify023/cells23.py",
        inp=Sample(
            body="resolved https://npm.kvncorp.net/@kvncorp/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvncorp/kvnpkg"
        ),
        outcome=Written(
            text="resolved https://forge.example.com/@kvncorp/kvnpkg/-/kvnpkg-1.0.0.tgz and @kvncorp/kvnpkg"
        ),
        markers=("npm.kvncorp.net",),
    ),
    Row(
        id="v24_gitlab_npm_scope_equal_to_the_remote_namespace_is_replaced",
        source="fix024/probe_cell.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body=f"resolved https://{FAKE_HOST}/api/v4/packages/npm/@{FAKE_NS}/kvnpkg/-/kvnpkg-1.0.0.tgz and @{FAKE_NS}/kvnpkg",
        ),
        outcome=Written(
            text="resolved https://forge.example.com/api/v4/packages/npm/@org/kvnpkg/-/kvnpkg-1.0.0.tgz and @org/kvnpkg"
        ),
        markers=(FAKE_HOST, FAKE_NS),
    ),
    Row(
        id="v25_review_path_field_led_by_repos_and_a_reserved_word_is_ordinary",
        source="verify024/cells24.py",
        inp=Sample(body="move the assets folder", extra_item={"path": "repos/assets/logo.png"}),
        outcome=Written(text="move the assets folder"),
    ),
    Row(
        id="v25_review_path_field_led_by_repos_public_is_ordinary",
        source="verify024/probe_one.py",
        inp=Sample(body="make the page public", extra_item={"path": "repos/public/index.html"}),
        outcome=Written(text="make the page public"),
    ),
    Row(
        id="v25_body_led_by_repos_and_a_reserved_word_is_ordinary",
        source="verify024/cells24.py",
        inp=Sample(body="repos/settings/defaults.yaml holds the settings"),
        outcome=Written(text="repos/settings/defaults.yaml holds the settings"),
    ),
    Row(
        id="v25_body_led_by_an_encoded_project_and_a_reserved_word_is_ordinary",
        source="verify024/cells24.py",
        inp=Sample(body="projects/help%2Fwiki is where help lives"),
        outcome=Written(text="projects/help%2Fwiki is where help lives"),
    ),
    Row(
        id="v25_body_led_by_repos_and_a_plain_directory_is_ordinary",
        source="verify024/cells24.py",
        inp=Sample(body="repos/frontend/main.py should stay frontend"),
        outcome=Written(text="repos/frontend/main.py should stay frontend"),
    ),
    Row(
        id="v25_review_path_field_led_by_repos_and_a_plain_directory_is_ordinary",
        source="verify024/probe_one.py",
        inp=Sample(body="the frontend build", extra_item={"path": "repos/frontend/index.html"}),
        outcome=Written(text="the frontend build"),
    ),
    Row(
        id="v25_argv_repos_path_with_a_reserved_owner_is_still_read_by_construction",
        source="verify024/probe_one.py",
        inp=Sample(body="ok", api_path="repos/settings/kvnbilling/pulls/5/comments"),
        outcome=Written(text='["api", "repos/org/project/pulls/5/comments"]'),
        markers=("kvnbilling", "repos/settings"),
    ),
    Row(
        id="v25_gitlab_url_field_nested_group_learns_no_joined_namespace",
        source="verify024/probe_v09.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            url=f"https://{FAKE_HOST}/kvnteam/kvnsub/kvnbilling/-/merge_requests/42#note_1",
            body=f"see https://{FAKE_HOST}/kvnteam/kvnsub/kvnbilling/-/merge_requests/42 "
            "and kvnteam/kvnsub-old now",
        ),
        outcome=Written(
            text="see https://forge.example.com/org/project/-/merge_requests/42 and org/org-old now"
        ),
        markers=(FAKE_HOST, "kvnteam", "kvnsub", "kvnbilling"),
    ),
    Row(
        id="v27_body_led_by_a_github_api_path_is_prose_NG4_16",
        source="verify025/cells25.py",
        inp=Sample(body="repos/kvnteam/kvnbilling/pulls/5/comments returns 404 since monday"),
        outcome=Written(text="repos/kvnteam/kvnbilling/pulls/5/comments returns 404 since monday"),
        residual=True,
    ),
    Row(
        id="v26_method_kwargs_github_api_path_with_a_rest_route_is_replaced",
        source="verify025/cells25.py",
        inp=Sample(top_kwargs={"endpoint": "repos/zqother/kvnbilling/issues/3"}),
        outcome=Written(text='{"endpoint": "repos/org/project/issues/3"}'),
        markers=("zqother", "kvnbilling"),
    ),
    Row(
        id="v26_stdin_ghe_api_v3_path_with_a_rest_route_is_replaced",
        source="verify025/cells25.py",
        inp=Sample(stdin="api/v3/repos/kvnteam/kvnbilling/hooks"),
        outcome=Written(text="api/v3/repos/org/project/hooks"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v27_json_string_in_a_body_is_prose_NG4_16",
        source="verify025/cells25.py",
        inp=Sample(body='{"endpoint": "/repos/kvnteam/kvnbilling/branches/main/protection"}'),
        outcome=Written(text='{"endpoint": "/repos/kvnteam/kvnbilling/branches/main/protection"}'),
        residual=True,
    ),
    Row(
        id="v27_review_path_field_api_path_is_prose_NG4_16",
        source="verify025/cells25.py",
        inp=Sample(
            body="see the file",
            extra_item={"path": "repos/kvnteam/kvnbilling/pulls/5/comments"},
        ),
        outcome=Written(
            text="see the file",
            fields=(("path", "repos/kvnteam/kvnbilling/pulls/5/comments"),),
        ),
        residual=True,
    ),
    Row(
        id="v27_reserved_owner_api_path_in_a_body_is_prose_NG4_16",
        source="verify025/cells25.py",
        inp=Sample(body="repos/settings/kvnbilling/pulls/5 returns 404 on the settings page"),
        outcome=Written(text="repos/settings/kvnbilling/pulls/5 returns 404 on the settings page"),
        residual=True,
    ),
    Row(
        id="v27_stderr_api_path_with_a_reserved_owner_learns_the_repository_only",
        source="verify025/cells25.py",
        inp=Sample(stderr="repos/settings/kvnbilling/pulls/5 returns 404 on the settings page"),
        outcome=Written(text="repos/settings/project/pulls/5 returns 404 on the settings page"),
        markers=("kvnbilling",),
    ),
    Row(
        id="v27_code_block_api_path_is_prose_NG4_16",
        source="verify026/cells26.py",
        inp=Sample(body="```\n$ gh api repos/kvnteam/kvnbilling/pulls/5/comments\n```"),
        outcome=Written(text="```\n$ gh api repos/kvnteam/kvnbilling/pulls/5/comments\n```"),
        residual=True,
    ),
    Row(
        id="v27_gitlab_body_api_path_is_prose_NG4_16",
        source="verify026/cells26.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body="mirror repos/kvnteam/kvnbilling/pulls/5 broke",
        ),
        outcome=Written(text="mirror repos/kvnteam/kvnbilling/pulls/5 broke"),
        residual=True,
    ),
    Row(
        id="v27_raw_stdout_led_by_an_api_path_is_prose_NG4_16",
        source="verify025/cells25.py",
        inp=Sample(raw_text_stdout="repos/kvnteam/kvnbilling/pulls/5/comments\n+++ b/x\n"),
        outcome=Written(text="repos/kvnteam/kvnbilling/pulls/5/comments\n+++ b/x\n"),
        residual=True,
    ),
    Row(
        id="v27_stderr_api_path_in_an_error_line_is_replaced",
        source="verify026/cells26.py",
        inp=Sample(stderr="HTTP 404: Not Found (repos/kvnteam/kvnbilling/pulls/5/comments)"),
        outcome=Written(text="HTTP 404: Not Found (repos/org/project/pulls/5/comments)"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v27_method_positional_arg_api_path_is_replaced",
        source="verify025/cells25.py",
        inp=Sample(top_args=(42, {"api": ["repos/kvnteam/kvnbilling/pulls/5/comments"]})),
        outcome=Written(text='[42, {"api": ["repos/org/project/pulls/5/comments"]}]'),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v27_stdin_api_path_to_a_file_with_a_fragment_is_replaced",
        source="verify026/cells26.py",
        inp=Sample(stdin="GET repos/kvnteam/kvnbilling/contents/README.md#L3"),
        outcome=Written(text="GET repos/org/project/contents/README.md#L3"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v28_undeclared_stdin_sentence_is_a_structured_source",
        source="verify026/cells26.py",
        inp=Sample(stdin="repos/payments/ledger/releases/v1.md documents the ledger release"),
        outcome=Written(text="repos/org/project/releases/v1.md documents the project release"),
        markers=("payments", "ledger"),
    ),
    Row(
        id="v28_stderr_file_then_words_is_read",
        source="verify027/cells27.py",
        inp=Sample(stderr="failed to get repos/kvnteam/kvnbilling/contents/README.md: HTTP 404"),
        outcome=Written(text="failed to get repos/org/project/contents/README.md: HTTP 404"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v28_stdin_declared_by_body_file_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            argv_extra=("--body-file", "-"),
            stdin="repos/payments/ledger/releases/v1.md documents the ledger release",
        ),
        outcome=Written(text="repos/payments/ledger/releases/v1.md documents the ledger release"),
    ),
    Row(
        id="v28_stdin_declared_by_a_body_field_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            argv_extra=("-F", "body=@-"),
            stdin="repos/payments/ledger/releases/v1.md documents the ledger release",
        ),
        outcome=Written(text="repos/payments/ledger/releases/v1.md documents the ledger release"),
    ),
    Row(
        id="v28_stdin_with_an_undeclared_field_is_read",
        source="verify027/cells27.py",
        inp=Sample(argv_extra=("-F", "line=@-"), stdin="GET repos/kvnteam/kvnbilling/pulls/5"),
        outcome=Written(text="GET repos/org/project/pulls/5"),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v28_argv_body_field_is_declared_prose",
        source="verify027/repro_post.py",
        inp=Sample(
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=(
                "-f",
                "body=repos/payments/ledger/releases/v1.md documents the ledger for payments",
            ),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_argv_body_field_value_holding_equals_is_declared_prose",
        source="verify028/demo_v06.py",
        inp=Sample(
            body="set retries=3 in repos/payments/ledger/releases/v1.md for payments",
            argv_extra=(
                "-f",
                "body=set retries=3 in repos/payments/ledger/releases/v1.md for payments",
            ),
        ),
        outcome=Written(text="set retries=3 in repos/payments/ledger/releases/v1.md for payments"),
    ),
    Row(
        id="v28_argv_joined_raw_field_title_is_declared_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=(
                "--raw-field=title=repos/payments/ledger/releases/v1.md documents the ledger",
            ),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_argv_path_field_is_declared_prose",
        source="verify027/repro_post.py",
        inp=Sample(
            body="see repos/payments/ledger/releases/notes.md",
            argv_extra=("-F", "path=repos/payments/ledger/releases/notes.md", "-fline=3"),
        ),
        outcome=Written(text="see repos/payments/ledger/releases/notes.md"),
    ),
    Row(
        id="v28_argv_undeclared_field_is_read",
        source="verify027/cells27.py",
        inp=Sample(argv_extra=("-f", "context=repos/kvnteam/kvnbilling/pulls/5")),
        outcome=Written(text="Looks good to me."),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v28_argv_after_body_flag_is_declared_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=(
                "--body",
                "repos/payments/ledger/releases/v1.md documents the ledger for payments",
            ),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_argv_after_title_flag_is_declared_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=("--title", "repos/payments/ledger/releases/v1.md documents the ledger"),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_argv_after_message_flag_is_declared_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=("--message=repos/payments/ledger/releases/v1.md documents the ledger",),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_gitlab_argv_after_description_flag_is_declared_prose",
        source="verify027/cells27.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body="repos/payments/ledger/releases/v1.md documents the ledger for payments",
            argv_extra=(
                "--description",
                "repos/payments/ledger/releases/v1.md documents the ledger for payments",
            ),
        ),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v28_argv_after_a_prose_flag_only_the_next_argument_is_prose",
        source="verify027/cells27.py",
        inp=Sample(argv_extra=("--body", "ok", "repos/kvnteam/kvnbilling/pulls/5")),
        outcome=Written(text="Looks good to me."),
        markers=("kvnteam", "kvnbilling"),
    ),
    Row(
        id="v28_title_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="the ledger for payments",
            title="repos/payments/ledger/releases/v1.md documents the ledger for payments",
        ),
        outcome=Written(
            text="the ledger for payments",
            title="repos/payments/ledger/releases/v1.md documents the ledger for payments",
        ),
    ),
    Row(
        id="v28_gitlab_title_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body="the ledger for payments",
            title="repos/payments/ledger/releases/v1.md documents the ledger for payments",
        ),
        outcome=Written(
            text="the ledger for payments",
            title="repos/payments/ledger/releases/v1.md documents the ledger for payments",
        ),
    ),
    Row(
        id="v28_commit_message_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="the ledger for payments",
            extra_item={"message": "fix repos/payments/ledger/releases/v1.md for payments"},
        ),
        outcome=Written(text="the ledger for payments"),
    ),
    Row(
        id="v28_commit_message_headline_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="the ledger for payments",
            extra_item={"messageHeadline": "fix repos/payments/ledger/releases/v1.md"},
        ),
        outcome=Written(text="the ledger for payments"),
    ),
    Row(
        id="v28_commit_message_body_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(
            body="the ledger for payments",
            extra_item={"messageBody": "moved repos/payments/ledger/releases/v1.md"},
        ),
        outcome=Written(text="the ledger for payments"),
    ),
    Row(
        id="v28_gitlab_change_diff_key_is_prose",
        source="fix028/keys_audit.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            body="the ledger for payments",
            extra_item={"diff": "@@ -1 +1 @@\n+repos/payments/ledger/releases/v1.md\n"},
        ),
        outcome=Written(text="the ledger for payments"),
    ),
    Row(
        id="v28_kwargs_message_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(top_kwargs={"message": "repos/payments/ledger/releases/v1.md for payments"}),
        outcome=Written(text='{"message": "repos/payments/ledger/releases/v1.md for payments"}'),
    ),
    Row(
        id="v28_stdin_json_title_key_is_prose",
        source="verify027/cells27.py",
        inp=Sample(stdin='{"title": "repos/payments/ledger/releases/v1.md for payments"}'),
        outcome=Written(text='{"title": "repos/payments/ledger/releases/v1.md for payments"}'),
    ),
    Row(
        id="v27_body_directory_path_under_repos_is_ordinary",
        source="verify026/cells26.py",
        inp=Sample(body="repos/payments/ledger/releases/v1.md documents the ledger for payments"),
        outcome=Written(
            text="repos/payments/ledger/releases/v1.md documents the ledger for payments"
        ),
    ),
    Row(
        id="v27_review_path_directory_under_repos_is_ordinary",
        source="verify026/cells26.py",
        inp=Sample(
            body="the payments team owns ledger",
            extra_item={"path": "repos/payments/ledger/releases/v1.md"},
        ),
        outcome=Written(
            text="the payments team owns ledger",
            fields=(("path", "repos/payments/ledger/releases/v1.md"),),
        ),
    ),
    Row(
        id="v27_code_block_directory_path_under_repos_is_ordinary",
        source="verify026/cells26.py",
        inp=Sample(body="```\n$ cat repos/frontend/src/tags/index.ts\n```\nthe src file"),
        outcome=Written(text="```\n$ cat repos/frontend/src/tags/index.ts\n```\nthe src file"),
    ),
    Row(
        id="v27_monorepo_layout_under_repos_is_ordinary",
        source="verify026/cells26.py",
        inp=Sample(
            body="monorepo: repos/alpha/beta/releases/notes.md and repos/gamma/delta/tags/list.md"
        ),
        outcome=Written(
            text="monorepo: repos/alpha/beta/releases/notes.md and repos/gamma/delta/tags/list.md"
        ),
    ),
    Row(
        id="v26_text_api_path_with_a_file_after_the_repository_is_ordinary",
        source="fix026/probe_rows.py",
        inp=Sample(body="repos/kvnwork/kvnfiles/main.py stays where kvnwork keeps it"),
        outcome=Written(text="repos/kvnwork/kvnfiles/main.py stays where kvnwork keeps it"),
    ),
    Row(
        id="v26_bare_text_api_path_is_ordinary",
        source="fix026/probe_rows.py",
        inp=Sample(body="repos/kvnwork/kvnfiles is where kvnwork keeps it"),
        outcome=Written(text="repos/kvnwork/kvnfiles is where kvnwork keeps it"),
    ),
    Row(
        id="v26_second_exchange_argv_api_path_is_read_by_construction",
        source="verify025/demo_two_calls.py",
        inp=Sample(body="ok", second_api_path="repos/settings/kvnbilling/pulls/5/comments"),
        outcome=Written(text='["api", "repos/org/project/pulls/5/comments"]'),
        markers=("kvnbilling", "repos/settings"),
    ),
    Row(
        id="v26_url_field_namespace_keeps_its_class_before_an_argv_repository",
        source="verify025/demo_b19.py",
        inp=Sample(
            forge="gitlab",
            remote=f"git@{FAKE_HOST}:{FAKE_NS}/{FAKE_REPO}.git",
            url=f"https://{FAKE_HOST}/kvnbilling/kvnsub/-/merge_requests/1",
            api_path="projects/kvnteam%2Fkvnbilling/merge_requests/1/notes",
            body=f"see https://{FAKE_HOST}/kvnbilling/kvnsub/-/merge_requests/1 for the kvnbilling service",
        ),
        outcome=Written(text='["api", "projects/org%2Forg/merge_requests/1/notes"]'),
        markers=(FAKE_HOST, "kvnteam", "kvnsub", "kvnbilling"),
        url_field_class=True,
    ),
)


def _position(inp: Sample) -> str:
    """The position the row's ``Written.text`` describes."""
    if inp.second_api_path is not None:
        return "argv2"
    if inp.raw_text_stdout is not None:
        return "stdout"
    if inp.stderr:
        return "stderr"
    if inp.stdin:
        return "stdin"
    if tuple(inp.top_args) != (42,):
        return "args"
    if inp.top_kwargs:
        return "kwargs"
    if inp.call_args is not None or inp.api_path is not None:
        return "argv"
    return "body"


def _planted_text(inp: Sample) -> str:
    """The raw text a residual row plants at its position: its raw stdout, or its body (0.27)."""
    return inp.raw_text_stdout if inp.raw_text_stdout is not None else inp.body


def _items(inp: Sample) -> list[dict[str, Any]]:
    """The raw JSON answer of the recorded call (``None`` for a raw text answer)."""
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
    return [item, *(copy.deepcopy(x) for x in inp.extra_items)]


def _stdout(inp: Sample) -> str:
    """The raw stdout the fake forge answers."""
    if inp.raw_text_stdout is not None:
        return inp.raw_text_stdout
    return json.dumps(_items(inp))


def _argv(inp: Sample) -> list[str]:
    """The argv the fake forge's method issues (the recorder records it verbatim)."""
    if inp.raw_text_stdout is not None:
        return ["pr" if inp.forge == "github" else "mr", "diff", "42", *inp.argv_extra]
    if inp.call_args is not None:
        return list(inp.call_args)
    default_api = (
        "repos/:owner/:repo/issues/42/comments"
        if inp.forge == "github"
        else "projects/:id/merge_requests/42/notes"
    )
    return ["api", inp.api_path or default_api, *inp.argv_extra]


def _method(inp: Sample) -> str:
    return "diff" if inp.raw_text_stdout is not None else "conversation_comments"


def _top_kwargs(inp: Sample) -> dict[str, Any]:
    """The method's keyword arguments, the row's only: the stdin goes to the call, as a real method sends it (0.28)."""
    return dict(inp.top_kwargs)


def _top_args(inp: Sample) -> list[Any]:
    """The method's positional arguments: the row's, then a planted argv the fake method issues."""
    args = list(inp.top_args)
    if inp.call_args is not None or inp.api_path is not None:
        args += _argv(inp)
    return args


def _fake_forge(inp: Sample, tmp_path: Path, bot: str | None = None) -> Forge:
    """A forge whose protocol method issues the row's argv and whose binary answers the row's stdout.

    ``bot`` is the ``bot_username`` the recorder's replay asks :func:`select_forge` for.
    """
    binary = tmp_path / ("gh" if inp.forge == "github" else "glab")
    binary.write_text("")
    fixed_argv = _argv(inp)
    planted_argv = inp.call_args is not None or inp.api_path is not None
    stdout = _stdout(inp)

    def _issued(extra: tuple[Any, ...]) -> list[str]:
        """The argv the method issues: the (classified) extra arguments when the row plants argv."""
        return [str(a) for a in extra] if planted_argv else fixed_argv

    def _spawn(argv_: list[str], *, input_data: str | None = None) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr=inp.stderr, argv=list(argv_))

    if inp.forge == "github":

        class _FakeGitHub(GitHubForge):
            def conversation_comments(self, number: int, *extra: Any, **_k: Any) -> Any:
                res = self._spawn([str(binary), *_issued(extra)], input_data=inp.stdin)
                if inp.second_api_path is not None:
                    self._spawn([str(binary), "api", inp.second_api_path])
                return [
                    comment_from_github(c) for c in json.loads(res.stdout) if isinstance(c, dict)
                ]

            def diff(self, number: int, *extra: Any, **_k: Any) -> Diff:
                res = self._spawn([str(binary), *_issued(extra)], input_data=inp.stdin)
                return Diff(text=res.stdout, files=diff_files(res.stdout))

        forge: Any = _FakeGitHub(cwd=tmp_path, gh_path=str(binary), bot_login=bot or inp.bot)
    else:

        class _FakeGitLab(GitLabForge):
            def conversation_comments(self, number: int, *extra: Any, **_k: Any) -> Any:
                res = self._spawn([str(binary), *_issued(extra)], input_data=inp.stdin)
                if inp.second_api_path is not None:
                    self._spawn([str(binary), "api", inp.second_api_path])
                return [note_to_comment(n) for n in json.loads(res.stdout) if isinstance(n, dict)]

            def diff(self, number: int, *extra: Any, **_k: Any) -> Diff:
                res = self._spawn([str(binary), *_issued(extra)], input_data=inp.stdin)
                return Diff(text=res.stdout, files=diff_files(res.stdout))

        forge = _FakeGitLab(cwd=tmp_path, glab_path=str(binary), bot_username=bot or inp.bot)
    forge._spawn = _spawn
    return forge


def _record_live(
    row: Row, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[str, str, dict[str, Any] | None]:
    """Run the row through ``record()``: the public live path, every helper real but the forge."""
    inp = row.inp
    monkeypatch.setenv(deny_list.DENYLIST_ENV, "\n".join(inp.deny))
    monkeypatch.setattr(
        REC, "select_forge", lambda **kw: _fake_forge(inp, tmp_path, kw.get("bot_username"))
    )
    monkeypatch.setattr(REC, "origin_url", lambda _cwd: inp.remote)
    monkeypatch.setattr(REC, "_author_of", lambda *_a: inp.author)
    out_root = tmp_path / "out" / row.id
    try:
        path = REC.record(
            forge_name=inp.forge,
            cwd=tmp_path,
            case=inp.case,
            method=_method(inp),
            args=_top_args(inp),
            kwargs=_top_kwargs(inp),
            trim=None,
            out_root=out_root,
        )
    except (
        REC.UnclassifiedValueError,
        REC.DenylistError,
        rec_mod.SyntheticIdsExhaustedError,
        rec_mod.NeedleError,
        SystemExit,
    ) as exc:
        assert not out_root.exists(), f"{row.id}: a refusal wrote a fixture"
        return "REFUSED", str(exc), None
    except rec_mod.ReplayMismatchError as exc:
        return "REPLAY_MISMATCH", str(exc), None
    return "WRITTEN", "", json.loads(path.read_text())


def _written_parts(row: Row, fixture: dict[str, Any]) -> tuple[str, str | None, dict[str, str]]:
    """The text at the row's position, the title and the named item fields of the written fixture."""
    inp = row.inp
    call = fixture["calls"][0]
    pos = _position(inp)
    title: str | None = None
    fields: dict[str, str] = {}
    if inp.raw_text_stdout is None:
        item = json.loads(call["stdout"])[0]
        if inp.title is not None:
            title = str(item["title"])
        for key in ("headRefName", "source_branch", "baseRefName", "target_branch", "path"):
            if key in item:
                fields[key] = str(item[key])
    if pos == "stdout":
        return str(call["stdout"]), title, fields
    if pos == "stderr":
        return str(call["stderr"]), title, fields
    if pos == "stdin":
        return str(call["stdin"]), title, fields
    if pos == "args":
        return json.dumps(fixture["args"], ensure_ascii=False), title, fields
    if pos == "kwargs":
        return json.dumps(fixture["kwargs"], ensure_ascii=False), title, fields
    if pos == "argv":
        return json.dumps(call["args"], ensure_ascii=False), title, fields
    if pos == "argv2":
        return json.dumps(fixture["calls"][1]["args"], ensure_ascii=False), title, fields
    return str(json.loads(call["stdout"])[0]["body"]), title, fields


def _judge_live(row: Row, status: str, msg: str, fixture: dict[str, Any] | None) -> list[str]:
    """Every way the live outcome differs from the row's one expected outcome."""
    problems: list[str] = []
    outcome = row.outcome
    if isinstance(outcome, Bounded):
        if status == "REFUSED":
            return [] if msg.startswith(outcome.prefix) else [f"expected prefix {outcome.prefix!r}"]
        outcome = Written(text=_planted_text(row.inp))
    if isinstance(outcome, Refused):
        if status != "REFUSED":
            return [f"expected REFUSED {outcome.prefix!r}, got {status}"]
        if not msg.startswith(outcome.prefix):
            problems.append(f"expected prefix {outcome.prefix!r}, got {msg!r}")
        return problems
    if status != "WRITTEN" or fixture is None:
        return [f"expected WRITTEN, got {status} ({msg!r})"]
    blob = json.dumps(fixture, ensure_ascii=False).casefold()
    survivors = [m for m in row.markers if m.casefold() in blob]
    if survivors:
        problems.append(f"survived={survivors}")
    text, title, fields = _written_parts(row, fixture)
    if text != outcome.text:
        problems.append(f"text {text!r} != {outcome.text!r}")
    if outcome.title is not None and title != outcome.title:
        problems.append(f"title {title!r} != {outcome.title!r}")
    for key, expected in outcome.fields:
        if fields.get(key) != expected:
            problems.append(f"{key} {fields.get(key)!r} != {expected!r}")
    return problems


_RAW_TEXT_KEYS = ("body", "title", "headRefName", "source_branch", "baseRefName", "target_branch")


def _node_legacy(number: int) -> str:
    """Encode ``number`` as a legacy GitHub ``04:User<n>`` base64 node id."""
    return base64.b64encode(f"04:User{number}".encode()).decode()


def _node_new(number: int) -> str:
    """Encode ``number`` as a current-format ``U_`` MessagePack node id."""
    raw = b"\x92\x00\xce" + number.to_bytes(4, "big")
    return "U_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


_MAILBOX_KEYS = ("email", "public_email", "commit_email", "author_email", "committer_email")


def _live_only_values(inp: Sample) -> list[str]:
    """Values only the live recorder learns from the recording.

    Every login under a ``login`` or ``username`` key and its ``[bot]``
    stem, each word of a ``name``, a mailbox, its local part and its
    host, and every ``id`` with its node forms, wherever the object
    stands: the offline transform sees a committed fixture, where the
    keys that held them are already dropped.
    """
    values: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, list):
            for member in node:
                _walk(member)
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in ("login", "username") and isinstance(value, str):
                values.append(value)
                if value.casefold().endswith("[bot]"):
                    values.append(value[:-5])
            elif key in ("name", "author_name", "committer_name") and isinstance(value, str):
                values.extend([value, *re.findall(r"[^\W_]+", value)])
            elif key in _MAILBOX_KEYS and isinstance(value, str):
                local, _at, host = value.rpartition("@")
                values.extend([value, local, host])
            elif key == "id" and isinstance(value, int):
                values.extend([str(value), _node_legacy(value), _node_new(value)])
            else:
                _walk(value)

    for item in _items(inp):
        _walk(item)
    if inp.author:
        values.append(inp.author)
    folded = {v.casefold() for v in values if v}
    return sorted(folded - {"author", "bot", "maintainer", "service"})


def _raw_strings(inp: Sample) -> list[str]:
    """Every raw text position of the row's input."""
    out = [
        inp.body,
        inp.title or "",
        inp.head_ref or "",
        inp.base_ref or "",
        inp.stderr,
        inp.stdin or "",
    ]
    out += [inp.raw_text_stdout or "", json.dumps(list(inp.top_args)), json.dumps(inp.top_kwargs)]
    out += [json.dumps(list(inp.call_args or ())), inp.api_path or "", inp.second_api_path or ""]
    for item in [inp.extra_item, *inp.extra_items]:
        out += [str(v) for k, v in item.items() if isinstance(v, str)]
    return out


def _offline_parity_possible(inp: Sample) -> bool:
    """True when no raw text position holds a value only the live recorder can learn."""
    text = "\n".join(_raw_strings(inp)).casefold()
    return not any(v in text for v in _live_only_values(inp))


def _restore_raw_text(row: Row, live: dict[str, Any]) -> dict[str, Any]:
    """The live fixture with every raw text position put back, structured fields kept in class form."""
    inp = row.inp
    out = copy.deepcopy(live)
    for n, (call, argv) in enumerate(zip(out["calls"], _argvs(inp), strict=True)):
        if inp.raw_text_stdout is not None:
            call["stdout"] = inp.raw_text_stdout
        else:
            written = json.loads(call["stdout"])
            raw = _items(inp)
            for w_item, r_item in zip(written, raw, strict=True):
                for key in _RAW_TEXT_KEYS:
                    if key in r_item:
                        w_item[key] = r_item[key]
            call["stdout"] = json.dumps(written, ensure_ascii=False)
        call["stderr"] = inp.stderr
        call["stdin"] = inp.stdin if n == 0 else None
        call["args"] = argv
    out["args"] = _top_args(inp)
    out["kwargs"] = _top_kwargs(inp)
    return out


def _argvs(inp: Sample) -> list[list[str]]:
    """The argv of each call the method issues: the row's, then its second API path when it has one."""
    second = [] if inp.second_api_path is None else [["api", inp.second_api_path]]
    return [_argv(inp), *second]


def _judge_offline(row: Row, live: dict[str, Any]) -> list[str]:
    """The offline transform agrees with the live recorder: parity on the raw text, idempotence on the written text."""
    problems: list[str] = []
    forge = row.inp.forge
    deny = deny_list.Denylist(tuple(row.inp.deny))
    again = OFF.redact_fixture(
        copy.deepcopy(live), forge=forge, remote=row.inp.remote, denylist=deny
    )
    if again != live:
        problems.append("offline: the written fixture is not a fixed point")
    if _offline_parity_possible(row.inp) and not row.url_field_class:
        restored = _restore_raw_text(row, live)
        offline = OFF.redact_fixture(restored, forge=forge, remote=row.inp.remote, denylist=deny)
        if offline != live:
            problems.append("offline: the raw text does not transform to the live text")
    return problems


def _judge_offline_refusal(row: Row, live_msg: str) -> list[str]:
    """An offline transform of the raw recording refuses with the same reason prefix."""
    assert isinstance(row.outcome, (Refused, Bounded))
    inp = row.inp
    raw_fixture: dict[str, Any] = {
        "forge": inp.forge,
        "case": inp.case,
        "method": _method(inp),
        "args": _top_args(inp),
        "kwargs": _top_kwargs(inp),
        "bot_username": inp.bot,
        "source": rec_mod.recorded_source("2026-11-22", inp.forge),
        "trimmed": None,
        "calls": [
            {
                "program": "gh" if inp.forge == "github" else "glab",
                "args": argv,
                "stdin": inp.stdin if n == 0 else None,
                "returncode": 0,
                "stdout": _stdout(inp),
                "stderr": inp.stderr,
            }
            for n, argv in enumerate(_argvs(inp))
        ],
        "result": None,
        "raises": None,
    }
    if not _offline_parity_possible(inp) or live_msg.startswith(("needle ", "refusing to write")):
        return []
    try:
        OFF.redact_fixture(
            raw_fixture,
            forge=inp.forge,
            remote=inp.remote,
            denylist=deny_list.Denylist(tuple(inp.deny)),
        )
    except (
        OFF.ResidualValueError,
        REC.UnclassifiedValueError,
        REC.DenylistError,
        rec_mod.NeedleError,
    ) as exc:
        return _offline_prefix_problems(row.outcome.prefix, str(exc))
    return ["offline: the raw recording was not refused"]


def _offline_prefix_problems(prefix: str, message: str) -> list[str]:
    """The one problem an offline refusal can have: a reason that does not start with the row's prefix."""
    if not message.startswith(prefix):
        return [f"offline: expected prefix {prefix!r}, got {message[:120]!r}"]
    return []


def _judge_offline_bounded(row: Row, msg: str, fixture: dict[str, Any] | None) -> list[str]:
    """A bounded row offline: either outcome of the interpreter's bound, a refusal with the row's prefix."""
    assert isinstance(row.outcome, Bounded)
    try:
        if fixture is not None:
            return _judge_offline(row, fixture)
        problems = _judge_offline_refusal(row, msg)
    except OFF.ResidualValueError as exc:
        return _offline_prefix_problems(row.outcome.prefix, str(exc))
    return [p for p in problems if p != "offline: the raw recording was not refused"]


FAILURES: list[tuple[str, list[str]]] = []


def _is_planted(row: Row) -> bool:
    """A planted row: a marker, a refusal or a documented residual."""
    return bool(row.markers) or row.residual or not isinstance(row.outcome, Written)


def test_the_planted_and_ordinary_split_is_computed_from_the_rows() -> None:
    """AC1 (0.19, 0.20): the counts of the spec come from the file, and a residual is a written, unmarked, planted row."""
    planted = [row for row in ROWS if _is_planted(row)]
    ordinary = [row for row in ROWS if not _is_planted(row)]
    assert len(planted) + len(ordinary) == len(ROWS)
    residual = [row for row in ROWS if row.residual]
    assert residual
    assert all(_is_planted(row) for row in residual)
    refused = [row for row in ROWS if isinstance(row.outcome, Refused)]
    bounded = [row for row in ROWS if isinstance(row.outcome, Bounded)]
    assert (len(ROWS), len(planted), len(ordinary), len(refused), len(bounded)) == (
        905,
        614,
        291,
        73,
        2,
    )
    assert len(residual) == 25
    assert all(not row.residual for row in refused)
    prose = [row for row in ROWS if "NG4_16" in row.id]
    assert len(prose) == 7 and all(row.residual for row in prose)
    assert {_position(row.inp) for row in prose} == {"body", "stdout"}
    assert sum("path" in row.inp.extra_item for row in prose) == 1
    assert [row.id for row in ROWS if row.url_field_class] == [
        "v26_url_field_namespace_keeps_its_class_before_an_argv_repository"
    ]
    for row in residual:
        assert isinstance(row.outcome, Written) and not row.markers, row.id
        assert row.outcome.text == _planted_text(row.inp), row.id
    for row in ordinary:
        assert isinstance(row.outcome, Written) and not row.markers, row.id
    unmarked = [
        row
        for row in ROWS
        if isinstance(row.outcome, Written) and not row.markers and "NG4" in row.id
    ]
    assert all(row.residual for row in unmarked), [row.id for row in unmarked if not row.residual]


def test_acceptance_corpus_live_and_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every row yields exactly its one expected outcome, live through ``record`` and offline."""
    ids = [row.id for row in ROWS]
    assert len(ids) == len(set(ids))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    FAILURES.clear()
    for row in ROWS:
        status, msg, fixture = _record_live(row, monkeypatch, tmp_path)
        problems = _judge_live(row, status, msg, fixture)
        if not problems:
            try:
                if isinstance(row.outcome, Bounded):
                    problems += _judge_offline_bounded(row, msg, fixture)
                elif fixture is not None:
                    problems += _judge_offline(row, fixture)
                else:
                    problems += _judge_offline_refusal(row, msg)
            except TypeError as exc:
                problems.append(f"offline: {exc}")
        if problems:
            FAILURES.append((row.id, problems))
    assert FAILURES == [], f"{len(FAILURES)} of {len(ROWS)} rows differ from their expected outcome"
