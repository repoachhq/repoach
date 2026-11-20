"""Record and replay the subprocess exchanges of a forge (conformance fixtures).

The conformance suite (``tests/unit/test_forge_conformance.py``) runs
every protocol method of both forges against fixtures under
``tests/fixtures/forge/<forge>/<case>.json``. A fixture is the ordered
list of subprocess exchanges one protocol call made — program
(``gh``/``glab``/``git``), argv after the program, stdin, exit code,
stdout, stderr — plus the normalised result (or the error class) the
call produced. ``scripts/record_forge_fixture.py`` writes them against a
real PR / MR; :class:`ReplaySpawn` plays them back and fails on the
first argv that differs from the recording, which is how the suite
pins the exact calls each forge makes.
"""

from __future__ import annotations

import base64
import html
import json
import re
import shlex
import string
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

from pydantic import BaseModel

from ..core.logging import get_logger
from ..core.redaction import SECRET_VALUE_PATTERNS
from . import protocol
from .protocol import InlineCommentDraft, MergeMethod, ReviewDraft
from .runner import CommandResult

_log = get_logger(__name__)

RECORDED_FIELDS: frozenset[str] = frozenset(
    {
        "a_mode",
        "access_level",
        "allow_force_push",
        "allow_force_pushes",
        "approved_by",
        "author",
        "b_mode",
        "base_sha",
        "baseRefName",
        "body",
        "bucket",
        "checks",
        "comments",
        "commits",
        "conclusion",
        "context",
        "contexts",
        "created_at",
        "data",
        "databaseId",
        "deleted_file",
        "description",
        "detailed_merge_status",
        "diff",
        "diff_refs",
        "draft",
        "enabled",
        "endCursor",
        "expires_at",
        "hasNextPage",
        "head_pipeline",
        "head_sha",
        "headRefName",
        "headRefOid",
        "id",
        "iid",
        "in_reply_to_id",
        "isDraft",
        "isResolved",
        "line",
        "link",
        "login",
        "merge_commit_sha",
        "membership_state",
        "merge_method",
        "mergeable",
        "mergeCommit",
        "mergeStateStatus",
        "message",
        "messageBody",
        "messageHeadline",
        "name",
        "new_file",
        "new_line",
        "new_path",
        "nodes",
        "notes",
        "number",
        "oid",
        "old_line",
        "old_path",
        "only_allow_merge_if_pipeline_succeeds",
        "pageInfo",
        "path",
        "permission",
        "position",
        "pullRequest",
        "renamed_file",
        "repository",
        "required_status_checks",
        "resolved",
        "resolved_at",
        "resolved_by",
        "resolvedBy",
        "reviewThreads",
        "role_name",
        "sha",
        "source_branch",
        "squash_commit_sha",
        "stage",
        "start_sha",
        "started_at",
        "startedAt",
        "state",
        "status",
        "statusCheckRollup",
        "system",
        "target_branch",
        "title",
        "type",
        "updated_at",
        "url",
        "user",
        "username",
        "web_url",
        "work_in_progress",
        "workflow",
    }
)
"""Every JSON key the forges read from a ``gh`` / ``glab`` answer.

A recorded answer keeps only these keys (:func:`redact_stdout`): tokens
(``runners_token``…), emails, ``node_id``\\ s and every other field the
conformance suite never reads are dropped at record time, so a fixture
cannot carry a secret the protocol does not need.
"""

SECRET_KEY_RE = re.compile(r"(token|secret|password|private_key|api_key|node_id|email)", re.I)
"""Keys whose presence in a recorded answer means redaction failed."""


PERSONAL_KEY_RE = re.compile(r"^(full_name|avatar_url|email|public_email|node_id)$", re.I)
"""Keys that name a person; :func:`redact_payload` drops them wherever they appear."""

URL_KEYS: frozenset[str] = frozenset({"url", "web_url", "html_url", "link"})
"""Keys whose string value is a forge URL, rewritten by :func:`redact_url`."""

REPOSITORY_NAME_KEYS: frozenset[str] = frozenset({"name", "path"})
"""Keys of a repository object written :data:`PLACEHOLDER_PROJECT` by key, whatever their value (I4, 0.10)."""

REPOSITORY_DROPPED_KEYS: frozenset[str] = frozenset({"description"})
"""Keys dropped from a repository object (I4, 0.10): prose about a possibly private project, read by no forge.

``gitlab.py`` reads ``description`` from a merge request (its body),
never from a project.
"""

USER_KEYS: frozenset[str] = frozenset({"login", "username"})
"""Keys that make an object a user object (its ``name`` and URLs are dropped)."""

LOGIN_KEYS: frozenset[str] = frozenset({"login", "username", "author", "resolved_by", "user"})
"""Keys whose string value is a login, replaced by its class word (:func:`classify_logins`)."""

CLASS_WORD_RE = re.compile(r"^(maintainer|author|bot|agent|service)(-[1-9][0-9]*)?$")
"""A recorded login once classified: a class word, optionally ``-<n>``."""

GITLAB_PROJECT_BOT_RE = re.compile(r"^project_[0-9]+_bot_[0-9a-f]+$", re.I)
"""A GitLab project access token user: a service account named after a real project id."""

NON_PERSON_LOGINS: frozenset[str] = frozenset({"github-actions[bot]", "repoach-bot"})
"""Logins that name no person, kept as they are by the recorder and the offline transform.

``github-actions[bot]`` is the platform's service account and the
default trusted bot (``repoach.project.resolve.GITHUB_DEFAULT_BOT``);
``repoach-bot`` is this project's own bot. Every other login, a
placeholder such as ``alice`` or ``owner`` included, is a class word
(the real-account rule, SP-RECORDER-REDACTS-BY-CLASS D2).
"""

PROSE_KEYS: frozenset[str] = frozenset(
    {"body", "description", "message", "messageBody", "messageHeadline", "title"}
)
"""Recorded keys whose string value is free text a person wrote, where ``@<handle>`` mentions one."""

_ASCII_ALNUM = string.ascii_letters + string.digits
"""ASCII letters and digits used to compose regex character classes safely (0.16)."""


def _char_class(chars: str, *, negated: bool = False, extra_escapes: tuple[str, ...] = ()) -> str:
    r"""Build a regex character class from an explicit character set via :func:`re.escape` (0.16).

    Every literal character is deduplicated, sorted and escaped so characters
    such as ``-``, ``]``, ``^`` and ``\`` can never form accidental ranges
    when interpolated into a pattern.
    """
    body = "".join(re.escape(ch) for ch in sorted(set(chars)))
    body += "".join(extra_escapes)
    prefix = "^" if negated else ""
    return f"[{prefix}{body}]"


TOKEN_CHARS = "A-Za-z0-9"
"""The characters inside a token, as a regex class body (SP-RECORDER-REDACTS-BY-CLASS I1, 0.11).

The one boundary of every whole-word pattern: the login replacer, the
``@<login>`` replacer, the deny-list and the closure oracle all use
:func:`token_pattern`. Letters and digits only: ``-``, ``_``, ``.``,
``:``, ``@``, ``/``, quotes and every other character delimit a token,
so ``<login>-patch-1`` (GitHub's web-edit branch), ``x-<v>``, ``_<v>``
and ``<v>_x`` hold the value as a token. The oracle's doubt resolves
toward refusing; the replacer, sharing the boundary, replaces wherever
the oracle would look (0.11, R1).
"""

_TOKEN_CLASS = _char_class(_ASCII_ALNUM)
"""Compiled character class for :data:`TOKEN_CHARS` (ASCII letters and digits, 0.16)."""

_CLI_PLACEHOLDER_AFTER_CLASS = _char_class(_ASCII_ALNUM + "-")
"""Characters that may not follow a :data:`CLI_PLACEHOLDER_RE` literal (0.16)."""


def token_pattern(word: str) -> re.Pattern[str]:
    """``word`` as a whole token (:data:`TOKEN_CHARS`), case-insensitive: the only whole-word pattern factory."""
    return re.compile(rf"(?<!{_TOKEN_CLASS}){re.escape(word)}(?!{_TOKEN_CLASS})", re.I)


_SPEC_FILE_ID_RE = re.compile(r"_(SP-[A-Z0-9]+(?:-[A-Z0-9]+)*)_")
"""Spec identifier segment of a ``docs/specs/<date>_<SP-ID>_<slug>.md`` file name (0.12, D3)."""


def _repo_spec_ids() -> frozenset[str]:
    """Every ``SP-<ID>`` of a specification under ``docs/specs/`` in this checkout."""
    specs = Path(__file__).resolve().parents[3] / "docs" / "specs"
    if not specs.is_dir():
        return frozenset()
    found: set[str] = set()
    for path in specs.glob("*.md"):
        match = _SPEC_FILE_ID_RE.search(path.name)
        if match:
            found.add(match.group(1))
    return frozenset(found)


SPEC_IDS: frozenset[str] = _repo_spec_ids()
"""The project's actual specification identifiers from ``docs/specs/`` (0.12, D3)."""

_LITERAL_PARTS: tuple[str, ...] = (
    r":(?:owner|repo|id)",
    r"/dev/null",
    r"(?i:docs\.github\.com)",
    r"(?i:docs\.gitlab\.com)",
    *(re.escape(sid) for sid in sorted(SPEC_IDS, key=len, reverse=True)),
    *(
        re.escape(login.removesuffix("[bot]")) + r"(?:\[bot\])?"
        for login in sorted(NON_PERSON_LOGINS, key=len, reverse=True)
    ),
)

CLI_PLACEHOLDER_RE = re.compile(
    rf"(?<!{_TOKEN_CLASS})(?:" + "|".join(_LITERAL_PARTS) + rf")(?!{_CLI_PLACEHOLDER_AFTER_CLASS})"
)
"""The tools' and the project's own literals (0.10, 0.11, 0.12, 0.13, 0.14): ``:owner``, ``:repo``, ``:id``, ``/dev/null``, ``docs.github.com``, ``docs.gitlab.com``, :data:`SPEC_IDS`.

The :data:`NON_PERSON_LOGINS`, with or without their ``[bot]`` suffix,
are literals too (0.11): kept as they are, so a needle that is one of
their words (the ``github`` organisation owning the Actions app) is not
judged inside ``github-actions[bot]``, nor replaced there. Public forge
documentation hosts (``docs.github.com``, ``docs.gitlab.com``, matched
case-insensitively since 0.14) are literals too (0.13), so keeping
documentation links in text does not collide with the ``github.com`` /
``gitlab.com`` host needles regardless of host casing.

``gh api repos/:owner/:repo/…`` and ``glab api projects/:id/…`` are
written by the forge code and expanded by the CLI; ``--- /dev/null`` is
how git names the missing side of an added or deleted file; a spec
identifier in :data:`SPEC_IDS` (``SP-OWNER-SETTLES-A-FINDING``) is an
actual specification file under ``docs/specs/`` (0.12, D3: bounded to
real spec ids so an invented ``SP-<login>`` or ``SP-<id>`` never hides a
real value). None carries a recorded value. The replacer leaves them
alone and the closure oracle does not judge them, except for the
deny-list, which judges the text with literals kept. Case-sensitive
outside the documentation hosts: the tools write the lowercase forms
only, and a spec identifier is uppercase.
"""

_MENTION_BEFORE_BLOCK_CLASS = _char_class(_ASCII_ALNUM + "_.@/+-")
"""Characters that may not immediately precede ``@<handle>`` in :data:`MENTION_RE` (0.16)."""

_MENTION_HANDLE_CLASS = _char_class(_ASCII_ALNUM + "_-")
"""Characters allowed inside a handle matched by :data:`MENTION_RE` (0.16)."""

_MENTION_DOT_AFTER_CLASS = _char_class(_ASCII_ALNUM + "_")
"""Characters after ``.`` that mark a dotted identifier such as ``@pytest.fixture`` (0.16)."""

_MENTION_SLASH_AFTER_CLASS = _char_class(_ASCII_ALNUM + "_.-")
"""Characters after ``/`` that mark a scoped package such as ``@types/node`` (0.16)."""

_MENTION_END = (
    rf"(?!{_MENTION_HANDLE_CLASS}|\.{_MENTION_DOT_AFTER_CLASS}"
    rf"|/{_MENTION_SLASH_AFTER_CLASS}|\()"
)
"""What may not follow a handle: a handle character, a dotted name (``@pytest.fixture``), a scoped package (``@types/node``) or a call."""

MENTION_RE = re.compile(
    rf"(?<!{_MENTION_BEFORE_BLOCK_CLASS})@({_TOKEN_CLASS}(?:{_MENTION_HANDLE_CLASS}*{_TOKEN_CLASS})?){_MENTION_END}",
    re.I,
)
"""The finder of unknown ``@<handle>`` mentions in free text (SP-RECORDER-REDACTS-BY-CLASS D2, 0.9).

A finder, not a boundary (0.10): it decides which unknown words become
a role, and nothing is kept or refused by it. Not an email
(``a@b.example``), not a dotted name (``@pytest.fixture``, and so not a
dotted GitLab username either), not a decorator call (``@lru_cache(…)``);
a sentence-ending ``.`` is not part of the handle. Once a login is a
role it is replaced by :func:`mention_pattern` or :func:`token_pattern`,
whose boundary is :data:`TOKEN_CHARS`.
"""

PYTHON_DECORATORS: frozenset[str] = frozenset(
    {
        "abstractmethod",
        "asynccontextmanager",
        "cache",
        "cached_property",
        "classmethod",
        "contextmanager",
        "dataclass",
        "deprecated",
        "final",
        "lru_cache",
        "no_type_check",
        "overload",
        "override",
        "property",
        "runtime_checkable",
        "singledispatch",
        "singledispatchmethod",
        "staticmethod",
        "total_ordering",
        "unique",
        "wraps",
    }
)
"""Bare decorator names of the standard library (builtins, ``abc``, ``contextlib``, ``dataclasses``,
``enum``, ``functools``, ``typing``, ``warnings``): ``@<name>`` in prose is code, never a handle."""

_CODE_SPAN_RE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
"""A fenced block or an inline code span of Markdown prose, where ``@<name>`` is code."""

MENTION_ONLY_PREFIX = "@"
"""A ``roles`` key starting with it is a login known only from a mention (D2, 0.9).

Such a login is replaced in its ``@<handle>`` form only: the bare word
(``dataclass`` in ``import dataclass``, a branch, a path) is left alone,
since nothing says it stands for that person there. A login is never
written with a leading ``@``, so the key cannot collide with one.
"""


def mention_pattern(login: str) -> re.Pattern[str]:
    """The ``@<login>`` form of ``login``: :func:`token_pattern` of ``@`` and the login (I1, 0.10)."""
    return token_pattern(f"@{login}")


RECORDED_SOURCE_RE = re.compile(r"^recorded ([0-9]{4}-[0-9]{2}-[0-9]{2}) from (?!a live )[^\s;]+")
"""A recorded fixture's ``source`` head naming where it was recorded from (a folder, a repository), not yet the forge."""


def recorded_source(date: str, forge: str) -> str:
    """A recorded fixture's ``source`` head: the date and the forge, never a checkout's folder name."""
    return f"recorded {date} from a live {forge} change request"


def is_classified_login(login: str) -> bool:
    """Whether ``login`` may stand in a fixture: a class word or a :data:`NON_PERSON_LOGINS` entry."""
    return bool(CLASS_WORD_RE.match(login)) or login in NON_PERSON_LOGINS


def mentions(payload: object) -> list[str]:
    """Every unclassified ``@<handle>`` of a :data:`PROSE_KEYS` value, in order of first appearance.

    A diff, a log or any other text outside those keys is the repository's
    or the runner's content, not a person addressing another, and is not
    searched. Inside them, a Markdown code span or fenced block is code,
    a :data:`PYTHON_DECORATORS` name (case-insensitive, 0.13) is a
    decorator or annotation, a scoped package ``@scope/pkg`` is a package
    name, and a :data:`NON_PERSON_LOGINS` stem (``@github-actions``) names
    no person.
    """
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in PROSE_KEYS and isinstance(item, str):
                    prose = _CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), item)
                    for m in MENTION_RE.finditer(prose):
                        handle = m.group(1)
                        if (
                            handle.casefold() in PYTHON_DECORATORS
                            or is_classified_login(handle)
                            or _names_no_one(handle)
                        ):
                            continue
                        if handle not in found:
                            found.append(handle)
                else:
                    _walk(item)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


PLACEHOLDER_HOST = "forge.example.com"
"""The host every recorded URL is given (an RFC 2606 domain)."""

PLACEHOLDER_NAMESPACE = "org"
"""The namespace every recorded URL is given."""

PLACEHOLDER_PROJECT = "project"
"""The repository name every recorded URL is given, a ``.git`` suffix kept (0.9).

A repository's name is its owner's as much as its namespace is, and a
private one names a private project; the readers pass URLs through and
never parse the name back.
"""

_GITHUB_ROUTES: frozenset[str] = frozenset(
    {
        "actions",
        "blob",
        "branches",
        "checks",
        "commit",
        "commits",
        "compare",
        "issues",
        "pull",
        "pulls",
        "releases",
        "runs",
        "tags",
        "tree",
    }
)
"""Third path segments that mark a ``<owner>/<repo>/<route>`` GitHub URL."""

URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "ssh", "git", "ftp", "ftps"})
"""Schemes of a URL redacted by class: web, API, git/ssh remotes and ftp(s) links (0.9, 0.13)."""

_RFC3986_UNRESERVED_CHARS = _ASCII_ALNUM + "-._~"
"""RFC 3986 Section 2.3 unreserved characters: ``ALPHA / DIGIT / "-" / "." / "_" / "~"`` (0.16 R9-D1)."""

_RFC3986_SUB_DELIM_CHARS = "!$&'()*+,;="
"""RFC 3986 Section 2.2 sub-delims characters (0.16 R9-D1)."""

_RFC3986_USERINFO_CHARSET = _RFC3986_UNRESERVED_CHARS + "%" + _RFC3986_SUB_DELIM_CHARS + ":"
"""Exact RFC 3986 Section 3.2.1 userinfo character set (unreserved, pct-encoded ``%``, sub-delims and ``:``; 0.16 R9-D1)."""

_RFC3986_USERINFO_CLASS = _char_class(_RFC3986_USERINFO_CHARSET)
"""Compiled character class for :data:`_RFC3986_USERINFO_CHARSET`, built via :func:`_char_class` (0.16 R9-D1)."""

_URL_USERINFO_PREFIX = rf"(?:(?:{_RFC3986_USERINFO_CLASS}*@)+)?"
"""Optional URL userinfo prefix covering empty passwords, RFC 3986 userinfo characters and embedded ``@`` (0.15 R8-D3, 0.16 R9-D1)."""

_URL_BODY_DELIMITERS = "\"'<>()[]{}\\,`*|"
"""Delimiters excluded from a non-parenthesised URL body character in free text (0.16)."""

_URL_BODY_CHAR = _char_class(_URL_BODY_DELIMITERS, negated=True, extra_escapes=(r"\s",))
"""One non-delimiter character of a URL body in free text (0.15 Pref 2, 0.16)."""

_JSON_VALUE_OPENER_CLASS = _char_class(":[,")
"""JSON token characters that may precede an opening quote of a whole string value (0.16 R9-D3)."""

_QUOTE_CLASS = _char_class("\"'")
"""Single or double quote characters allowed in HTML/CSS attribute leads (0.16 R9-D3)."""

_HSPACE_CLASS = _char_class(" \t")
"""Horizontal whitespace characters (0.16 R9-D3)."""

_HOST_BEFORE_BLOCK_CLASS = _char_class(_ASCII_ALNUM + "_.-")
"""Characters that may not immediately precede a host, API path or ``glab api`` command token (0.16)."""

_URL_IN_TEXT_RE = re.compile(
    rf"(?:https?|ssh|git|ftps?)://{_URL_USERINFO_PREFIX}"
    rf"(?:{_URL_BODY_CHAR}|\((?:{_URL_BODY_CHAR}|\({_URL_BODY_CHAR}*\))*\))+",
    re.I,
)
"""A URL inside free text (a log line, a ``glab`` error, a clone command), any case, with RFC 3986 userinfo and GFM balanced parentheses (I2, 0.10; 0.13; 0.15 R8-D3, Pref 2)."""

_URL_TRIM_TAIL = ".,:;!?)]}>'\"`*|~_"
"""Trailing prose punctuation and boundary Markdown delimiters trimmed from text URLs without being eaten (0.13, 0.14 R7-D2)."""

_URL_TRIM_PUNCT = _URL_TRIM_TAIL.replace("_", "")
"""Trailing prose punctuation and non-underscore Markdown delimiters trimmed before fragment/query handling (0.15 R8-D4)."""


def _strip_balanced_tail(raw: str, chars: str) -> tuple[str, str]:
    """Trim trailing ``chars`` off ``raw`` without cutting a balanced closing parenthesis (0.15 Pref 2)."""
    end = len(raw)
    while end > 0 and raw[end - 1] in chars:
        if raw[end - 1] == ")" and raw[:end].count("(") >= raw[:end].count(")"):
            break
        end -= 1
    return raw[:end], raw[end:]


def _split_url_tail(raw_url: str, *, open_under: int = 0) -> tuple[str, str]:
    """Split trailing prose punctuation and boundary Markdown delimiters off ``raw_url`` without eating them (0.14 R7-D2, 0.15 R8-D4).

    Prose punctuation and non-underscore Markdown delimiters are trimmed first
    (respecting GFM balanced parentheses). Trailing ``_`` inside a non-anchor
    fragment or query (such as ``#__init__``) stays inside the URL unless
    balanced by ``open_under`` leading underscores before the URL, so dropping
    the fragment or query never leaves stray ``__`` behind.
    """
    core, punct_tail = _strip_balanced_tail(raw_url, _URL_TRIM_PUNCT)
    has_query_or_frag = False
    keep_frag_under = False
    try:
        parts = urlsplit(core)
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        parts = None
    if parts is not None and (parts.fragment or parts.query):
        has_query_or_frag = True
        if parts.fragment and FRAGMENT_ANCHOR_RE.match(parts.fragment.rstrip("_")):
            keep_frag_under = True
    if not has_query_or_frag or keep_frag_under:
        return _strip_balanced_tail(raw_url, _URL_TRIM_TAIL)
    if open_under > 0 and core.endswith("_"):
        stripped = min(open_under, len(core) - len(core.rstrip("_")))
        return core[:-stripped], core[-stripped:] + punct_tail
    return core, punct_tail


_DIFF_MARKER_CLASS = _char_class("+-")
"""Unified-diff line-leading ``+`` / ``-`` markers allowed immediately before an SCP remote (0.16 R9-D2)."""

_SCP_BEFORE_BLOCK_CLASS = _char_class(_ASCII_ALNUM + ".+/-")
"""Characters that may not precede an SCP remote username outside a diff line prefix (0.16)."""

_SCP_USER_CLASS = _char_class(_ASCII_ALNUM + "_.+-")
"""Characters allowed after the first character of an SCP remote username (0.16)."""

_SCP_HOST_LABEL_CLASS = _char_class(_ASCII_ALNUM + "-")
"""Characters allowed inside a DNS host label (0.16)."""

_SCP_PATH_CLASS = _char_class(_ASCII_ALNUM + "_.~%-")
"""Characters allowed inside an SCP remote path segment (0.16)."""

_SCP_IN_TEXT_RE = re.compile(
    rf"(?:(?<=\A{_DIFF_MARKER_CLASS})|(?<=\n{_DIFF_MARKER_CLASS})|(?<!{_SCP_BEFORE_BLOCK_CLASS}))"
    rf"(?P<user>{_TOKEN_CLASS}{_SCP_USER_CLASS}*)@"
    rf"(?P<host>{_SCP_HOST_LABEL_CLASS}+(?:\.{_SCP_HOST_LABEL_CLASS}+)+)\.?"
    rf":(?!//)(?P<path>(?:{_SCP_PATH_CLASS}+/)+{_SCP_PATH_CLASS}*)",
    re.I,
)
"""An scp-style remote inside free text, ``<user>@<host>:<namespace>/<repo>`` (0.9), any case, optional DNS trailing dot (0.13).

The path holds at least one ``/``. A mailbox followed by ``:<port>/<path>``
is read as a remote too (0.10): the doubt resolves toward redaction, so
the mailbox goes with the rest (:func:`redact_urls_in_text`).
"""

_PROTO_REL_LEAD_RE = re.compile(
    rf"(?:(?P<json>{_JSON_VALUE_OPENER_CLASS}{_HSPACE_CLASS}*\")"
    rf"|=(?:{_QUOTE_CLASS})?"
    rf"|(?<!{_MENTION_DOT_AFTER_CLASS})url\((?:{_QUOTE_CLASS})?"
    rf"|\]\("
    rf"|<)$",
    re.I,
)
"""Positive URL-context leads immediately before a protocol-relative ``//host/path`` (0.16 R9-D3)."""

_PROTO_REL_BODY = (
    rf"//{_URL_USERINFO_PREFIX}"
    rf"{_SCP_HOST_LABEL_CLASS}+(?:\.{_SCP_HOST_LABEL_CLASS}+)+\.?"
    rf"(?::[0-9]{{1,5}})?/"
    rf"(?:{_URL_BODY_CHAR}|\((?:{_URL_BODY_CHAR}|\({_URL_BODY_CHAR}*\))*\))*"
)
"""Host-and-path body of a protocol-relative URL ``//host/path`` (0.16 R9-D3)."""

_PROTO_REL_IN_TEXT_RE = re.compile(
    rf"(?P<lead>(?P<json>{_JSON_VALUE_OPENER_CLASS}{_HSPACE_CLASS}*\")"
    rf"|=(?:{_QUOTE_CLASS})?"
    rf"|(?<!{_MENTION_DOT_AFTER_CLASS})url\((?:{_QUOTE_CLASS})?"
    rf"|\]\("
    rf"|<)"
    rf"(?P<url>{_PROTO_REL_BODY})",
    re.I,
)
"""Protocol-relative ``//host/path`` in a positive URL context (JSON value, ``=``, ``url(``, ``](``, ``<``; 0.16 R9-D3)."""

_PROTO_REL_WHOLE_RE = re.compile(rf"^(?P<url>{_PROTO_REL_BODY})$", re.I)
"""Protocol-relative ``//host/path`` occupying a whole decoded structured JSON string value (0.16 R9-D3)."""

_TEXT_VALUE_LEAVES: frozenset[str] = PROSE_KEYS | {
    "stdout",
    "stderr",
    "stdin",
    "diff",
    "source",
    "case",
}
"""Leaf key names whose string values hold prose or code rather than standalone structured JSON values (0.16 R9-D3)."""


def is_structured_json_path(path: str) -> bool:
    """Whether a :func:`fixture_strings` ``path`` addresses a structured non-prose JSON value (0.16 R9-D3)."""
    leaf = path.rsplit(".", 1)[-1]
    if not leaf or leaf.startswith("["):
        return False
    return leaf not in _TEXT_VALUE_LEAVES


FRAGMENT_ANCHOR_RE = re.compile(
    r"^(?:issuecomment-|note_|discussion_r|pullrequestreview-|discussion_)[0-9a-f]+$", re.I
)
"""The fragments a forge URL may keep (0.10): a comment's, a note's or a review's anchor, which a reader parses.

Every other fragment, and every query, is dropped by :func:`redact_url`:
either can carry a login (``?author=<login>``, ``#<login>``), and no
reader consumes one.
"""

_EXAMPLE_HOST_RE = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$|^127\.|^localhost$", re.I
)
"""Hosts that name no one (RFC 2606, loopback); text URLs on them are kept, except on :data:`PLACEHOLDER_HOST`."""

PUBLIC_REFERENCE_HOSTS: frozenset[str] = frozenset(
    {
        "crates.io",
        "docker.io",
        "docs.github.com",
        "docs.gitlab.com",
        "docs.rs",
        "ghcr.io",
        "go.dev",
        "golang.org",
        "k8s.io",
        "mozilla.org",
        "npmjs.com",
        "npmjs.org",
        "pypi.org",
        "python.org",
        "readthedocs.io",
        "readthedocs.org",
        "stackoverflow.com",
        "wikipedia.org",
    }
)
"""Curated registrable suffixes of public documentation and package hosts kept in free text (0.13, 0.15 R8-D1).

Matched by registrable suffix (exact host or ``.<suffix>``): for example
``python.org`` covers ``docs.``, ``www.`` and ``peps.``; ``readthedocs.io``
covers ``<project>.readthedocs.io``; ``docs.github.com`` and
``docs.gitlab.com`` stay scoped to their documentation subdomains so
``github.com`` and ``gitlab.com`` are never matched. Any unlisted host is
rewritten (over-redaction is the safe direction). Kept URLs still have
userinfo stripped, scheme and host lowercased, and path/query scanned for
needles and deny-list values.
"""


def _project(segment: str) -> str:
    """:data:`PLACEHOLDER_PROJECT`, with ``segment``'s ``.git`` suffix when it has one."""
    return PLACEHOLDER_PROJECT + (".git" if segment.casefold().endswith(".git") else "")


def _bare(segment: str) -> str:
    """``segment`` without a ``.git`` suffix, in any case."""
    return segment[:-4] if segment.casefold().endswith(".git") else segment


@dataclass(frozen=True)
class PathIdentity:
    """A forge path split into what names someone and what does not.

    Attributes:
        segments: The path with its namespace and repository replaced by their placeholders.
        namespace: The raw namespace segments (an owner, a group and its subgroups).
        repository: The raw repository name, a ``.git`` suffix removed, or ``None``.
    """

    segments: list[str]
    namespace: tuple[str, ...] = ()
    repository: str | None = None


_API_VERSION_RE = re.compile(r"^v[0-9]+$", re.I)
"""An API version path segment such as ``v3`` or ``v4`` (0.15 R8-D2)."""


def path_identity(segments: list[str]) -> PathIdentity:
    """Split a forge path's segments into its placeholder form, its namespace and its repository.

    The one reading of a forge path: :func:`redact_url` writes
    :attr:`PathIdentity.segments`, and :func:`needles` takes the raw
    namespace and repository as segment needles (0.10), so the two cannot
    disagree on which segment named someone. Empty segments from double
    slashes (``org//repo``, ``repos//o/r``, ``users//u``) are collapsed
    so they never shift or leak a real segment (0.12, D4); a trailing
    slash is preserved.
    """
    core = [s for s in segments if s]
    if not core:
        return PathIdentity(segments)
    trailing = [""] if segments[-1] == "" else []
    if (
        len(core) > 3
        and core[0].casefold() == "api"
        and _API_VERSION_RE.match(core[1])
        and core[2].casefold() == "projects"
    ):
        parts = [p for p in unquote(core[3]).split("/") if p]
        if len(parts) > 1:
            placeholder = f"{PLACEHOLDER_NAMESPACE}/{PLACEHOLDER_PROJECT}"
            return PathIdentity(
                [*core[:3], quote(placeholder, safe=""), *core[4:], *trailing],
                tuple(parts[:-1]),
                _bare(parts[-1]),
            )
        return PathIdentity([*core, *trailing])
    if core[0] == "repos" and len(core) >= 3:
        return PathIdentity(
            ["repos", PLACEHOLDER_NAMESPACE, _project(core[2]), *core[3:], *trailing],
            (core[1],),
            _bare(core[2]),
        )
    if core[0] in ("repos", "users", "orgs", "apps") and len(core) >= 2:
        return PathIdentity(
            [core[0], PLACEHOLDER_NAMESPACE, *core[2:], *trailing],
            (core[1],),
        )
    if core[0].casefold() == "projects" and len(core) >= 2 and core[1].isdigit():
        return PathIdentity(
            [core[0], PLACEHOLDER_PROJECT, *core[2:], *trailing],
            (),
            core[1] if len(core[1]) >= NEEDLE_MIN_LENGTH else None,
        )
    if "-" in core:
        dash = core.index("-")
        if dash >= 2:
            return PathIdentity(
                [PLACEHOLDER_NAMESPACE, _project(core[dash - 1]), *core[dash:], *trailing],
                tuple(core[: dash - 1]),
                _bare(core[dash - 1]),
            )
        return PathIdentity([*core, *trailing])
    if len(core) < 3 or core[2] in _GITHUB_ROUTES:
        rest = core[1:]
        repository = None
        if rest:
            repository = _bare(rest[0])
            rest = [_project(rest[0]), *rest[1:]]
        return PathIdentity([PLACEHOLDER_NAMESPACE, *rest, *trailing], (core[0],), repository)
    return PathIdentity(
        [PLACEHOLDER_NAMESPACE, _project(core[-1]), *trailing],
        tuple(core[:-1]),
        _bare(core[-1]),
    )


def _namespaced(segments: list[str]) -> list[str]:
    """Replace the namespace and the repository of a forge path by their placeholders."""
    return path_identity(segments).segments


def _repository_root(path: str) -> str | None:
    """The repository name of a path that names a repository and nothing under it, else ``None``."""
    core = [s for s in path.strip("/").split("/") if s]
    if len(core) < 2 or "-" in core:
        return None
    if core[0] in ("api", "repos", "users", "orgs", "apps"):
        return None
    if len(core) > 2 and core[2] in _GITHUB_ROUTES:
        return None
    return _bare(core[-1])


def redact_url(value: str) -> str:
    """Rewrite a forge URL by class: placeholder host, namespace and repository, path shape kept.

    ``https://<forge>/<owner>/<repo>/pull/12`` becomes
    ``https://forge.example.com/org/project/pull/12``; on GitLab every
    segment before the repository (the one preceding ``/-/``, or the
    last) is the namespace; ``/-/jobs/5`` with no namespace keeps its
    path. ``api/v4/projects/<group>%2F<repo>`` and ``repos/<owner>/<repo>``
    API paths get the same placeholders. A userinfo is never kept (0.10):
    an ``ssh://`` or ``git://`` remote that had one is written ``git@``,
    any other drops it. The scheme and host are written lowercase. The
    query is dropped, and the fragment too unless it is a
    :data:`FRAGMENT_ANCHOR_RE` anchor (0.10).

    Returns:
        The redacted URL, or ``""`` when ``value`` is not a URL of
        :data:`URL_SCHEMES` with a host (the caller drops the key).
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        return ""
    if parts.scheme not in URL_SCHEMES or not parts.hostname:
        return ""
    path = parts.path
    if path.startswith("/") and len(path) > 1:
        path = "/" + "/".join(_namespaced(path[1:].split("/")))
    host = PLACEHOLDER_HOST
    if parts.scheme in ("ssh", "git") and "@" in parts.netloc:
        host = f"git@{PLACEHOLDER_HOST}"
    fragment = parts.fragment if FRAGMENT_ANCHOR_RE.match(parts.fragment) else ""
    return urlunsplit((parts.scheme, host, path, "", fragment))


def _is_public_reference_host(host: str) -> bool:
    """Whether ``host`` matches a curated suffix in :data:`PUBLIC_REFERENCE_HOSTS` (0.15 R8-D1)."""
    folded = host.rstrip(".").casefold()
    return any(
        folded == suffix or folded.endswith(f".{suffix}") for suffix in PUBLIC_REFERENCE_HOSTS
    )


def _is_allowed_host(host: str) -> bool:
    """Whether ``host`` is allowed in free text: an RFC 2606, loopback or :data:`PUBLIC_REFERENCE_HOSTS` host."""
    return bool(_EXAMPLE_HOST_RE.search(host)) or _is_public_reference_host(host)


def _kept_host(host: str) -> bool:
    """Whether a text URL on ``host`` is kept as it is: an allowed host other than the placeholder."""
    return _is_allowed_host(host) and host.casefold() != PLACEHOLDER_HOST


def _allowed_userinfo(scheme: str, netloc: str, *, allow_example_git: bool = False) -> bool:
    """Whether ``netloc`` carries no forbidden userinfo (0.15 R8-D3).

    Userinfo is never allowed on non-``ssh``/``git`` schemes. On ``ssh`` and
    ``git`` remotes only ``git@forge.example.com`` (plus ``git@<example>``
    for SCP on RFC 2606 example hosts) is allowed; every other userinfo —
    including empty or symbol passwords and SCP users on RFC 2606 hosts — is
    forbidden.
    """
    if "@" not in netloc:
        return True
    if scheme.casefold() not in ("ssh", "git"):
        return False
    userinfo, _, hostport = netloc.rpartition("@")
    if userinfo.casefold() != "git":
        return False
    host = hostport.partition(":")[0].rstrip(".").casefold()
    if allow_example_git and _example_host(host):
        return True
    return netloc.casefold() == f"git@{PLACEHOLDER_HOST}"


def _example_host(host: str) -> bool:
    """Whether ``host`` is an RFC 2606 or loopback host other than :data:`PLACEHOLDER_HOST`."""
    return bool(_EXAMPLE_HOST_RE.search(host)) and host.casefold() != PLACEHOLDER_HOST


_NOREPLY_LOCAL_CLASS = _char_class(_ASCII_ALNUM + "._%+[]-")
"""Characters allowed in the local part of a noreply mailbox (0.16)."""

_NOREPLY_MAILBOX_RE = re.compile(
    rf"(?<!{_NOREPLY_LOCAL_CLASS})(?P<local>{_NOREPLY_LOCAL_CLASS}+)@"
    rf"(?P<host>(?:users\.)?noreply\.(?:{_SCP_HOST_LABEL_CLASS}+\.)+[A-Za-z]{{2,}})\b",
    re.I,
)
"""A forge noreply mailbox (GitHub/GitLab ``@users.noreply.<host>`` or ``@noreply.<host>``, 0.12 D6)."""

_SYSTEM_NOREPLY_RE = re.compile(
    rf"(?<!{_NOREPLY_LOCAL_CLASS})(?:(?P<local>no-?reply)@"
    rf"(?P<host>(?:{_SCP_HOST_LABEL_CLASS}+\.)+[A-Za-z]{{2,}})|(?P<actions>actions)@github\.com)\b",
    re.I,
)
"""A system noreply mailbox (``noreply@github.com``, ``no-reply@<host>``, ``actions@github.com``, 0.13 R6-D5, 0.14)."""


def _normalize_kept_url(parts: SplitResult) -> str:
    """Return a kept-host URL with scheme and host lowercased on the original netloc (0.14 R7-D1)."""
    return urlunsplit(
        (parts.scheme, parts.netloc.casefold(), parts.path, parts.query, parts.fragment)
    )


def _redact_proto_rel(raw_url: str) -> str:
    """Redact one protocol-relative URL ``//host/path``, preserving kept public hosts without userinfo (0.16 R9-D3)."""
    url, tail = _split_url_tail(raw_url)
    try:
        parts = urlsplit(f"https:{url}")
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        return raw_url
    host = parts.hostname or ""
    if not host:
        return raw_url
    if "@" not in parts.netloc and _kept_host(host):
        return _normalize_kept_url(parts).removeprefix("https:") + tail
    redacted = redact_url(f"https:{url}")
    return (redacted.removeprefix("https:") if redacted else url) + tail


def _unredacted_proto_rel(raw_url: str) -> bool:
    """Whether a protocol-relative URL carries an unallowed host or forbidden userinfo (0.16 R9-D3)."""
    url, _tail = _split_url_tail(raw_url)
    try:
        parts = urlsplit(f"https:{url}")
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if not _is_allowed_host(host):
        return True
    return not _allowed_userinfo("https", parts.netloc)


def redact_urls_in_text(text: str, *, whole_value: bool = False) -> str:
    """Redact every URL, protocol-relative URL in a positive URL context, scp-style remote and forge noreply mailbox of ``text`` not on a kept host.

    A URL goes through :func:`redact_url`, with trailing prose punctuation
    (``.,:;!?)]}>'"``) and boundary Markdown delimiters (`` ` ``, ``*``,
    ``|``, ``~``, ``_``) preserved outside the URL without being eaten
    (0.13 R6-D1, 0.14 R7-D2). A protocol-relative ``//host/path`` is
    redacted only in a positive URL context — a whole JSON or decoded
    structured string value (``whole_value=True``), after ``="`` / ``='`` /
    ``=``, inside ``url(`` or ``](``, or after ``<`` — so single-line
    ``//`` code comments stay byte-identical (0.16 R9-D3). An scp remote
    ``<user>@<host>:<ns>/<repo>`` is normalised through it too (0.10): read
    as ``ssh://<user>@<host>/<ns>/<repo>``, redacted, and written back as
    ``git@forge.example.com:org/project``, its ``.git`` suffix kept and its
    user never. A URL on :data:`PLACEHOLDER_HOST` is redacted again, so a
    hand-written one gets the placeholder path too. A URL carrying userinfo
    (``user[:pass]@``) is always redacted through :func:`redact_url`,
    whatever the host — including :data:`PUBLIC_REFERENCE_HOSTS` and RFC
    2606 hosts (0.14 R7-D1). Text URLs on :data:`PUBLIC_REFERENCE_HOSTS`
    without userinfo are kept with their scheme and host lowercased (0.13,
    0.14 R7-D1). A forge noreply mailbox
    (``<id>+<login>@users.noreply.github.com``,
    ``<id>-<login>@users.noreply.gitlab.com``, 0.12 D6) has its local part
    and host redacted by class to
    ``<class>@users.noreply.forge.example.com``; a system noreply mailbox
    (``noreply@github.com``, ``no-reply@<host>``, ``actions@github.com``,
    0.13 R6-D5, 0.14) becomes ``noreply@forge.example.com`` (or
    ``no-reply@forge.example.com``).
    """
    if whole_value:
        whole = _PROTO_REL_WHOLE_RE.match(text)
        if whole is not None:
            return _redact_proto_rel(whole.group("url"))

    def _one(match: re.Match[str]) -> str:
        start = match.start()
        open_under = 0
        while start - open_under > 0 and text[start - open_under - 1] == "_":
            open_under += 1
        url, tail = _split_url_tail(match.group(0), open_under=open_under)
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            return match.group(0)
        host = parts.hostname or ""
        if not host:
            return match.group(0)
        if "@" in parts.netloc:
            return (redact_url(url) or url) + tail
        if _kept_host(host):
            return _normalize_kept_url(parts) + tail
        return (redact_url(url) or url) + tail

    def _proto_rel(match: re.Match[str]) -> str:
        if match.group("json") is not None and match.string[match.end() : match.end() + 1] != '"':
            return match.group(0)
        return match.group("lead") + _redact_proto_rel(match.group("url"))

    def _scp(match: re.Match[str]) -> str:
        host = match.group("host").casefold()
        user = match.group("user")
        if _example_host(host) and _allowed_userinfo(
            "ssh", f"{user}@{host}", allow_example_git=True
        ):
            return match.group(0)
        path, tail = _split_url_tail(match.group("path"))
        url = redact_url(f"ssh://{user}@{match.group('host')}/{path}")
        return url.removeprefix("ssh://").replace("/", ":", 1) + tail

    def _noreply(match: re.Match[str]) -> str:
        host = match.group("host").casefold()
        if _kept_host(host) and not host.endswith(f".{PLACEHOLDER_HOST}"):
            return match.group(0)
        local = match.group("local").casefold()
        role = "bot" if local == "bot" or local.endswith("[bot]") else "author"
        return f"{role}@users.noreply.{PLACEHOLDER_HOST}"

    def _system_noreply(match: re.Match[str]) -> str:
        if match.group("actions") is not None:
            return f"noreply@{PLACEHOLDER_HOST}"
        host = match.group("host").casefold()
        if _example_host(host):
            return match.group(0)
        return f"{match.group('local').casefold()}@{PLACEHOLDER_HOST}"

    with_urls = _PROTO_REL_IN_TEXT_RE.sub(_proto_rel, _URL_IN_TEXT_RE.sub(_one, text))
    step = _NOREPLY_MAILBOX_RE.sub(_noreply, _SCP_IN_TEXT_RE.sub(_scp, with_urls))
    return _SYSTEM_NOREPLY_RE.sub(_system_noreply, step)


def unredacted_host(text: str, *, whole_value: bool = False) -> bool:
    """Whether ``text`` holds a URL, positive-context protocol-relative URL or scp-style remote on a host :func:`redact_urls_in_text` rewrites, or an unredacted URL userinfo.

    After redaction no such host or userinfo is left, so one found in a fixture about
    to be written means a value escaped the redaction (D3, 0.9, 0.14 R7-D1, 0.16 R9-D3).
    """
    if whole_value:
        whole = _PROTO_REL_WHOLE_RE.match(text)
        if whole is not None and _unredacted_proto_rel(whole.group("url")):
            return True
    for match in _URL_IN_TEXT_RE.finditer(text):
        url, _tail = _split_url_tail(match.group(0))
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            continue
        host = parts.hostname or ""
        if not host:
            continue
        if not _is_allowed_host(host):
            return True
        if not _allowed_userinfo(parts.scheme, parts.netloc):
            return True
    for match in _PROTO_REL_IN_TEXT_RE.finditer(text):
        if match.group("json") is not None and text[match.end() : match.end() + 1] != '"':
            continue
        if _unredacted_proto_rel(match.group("url")):
            return True
    return any(
        not _EXAMPLE_HOST_RE.search(m.group("host"))
        or not _allowed_userinfo(
            "ssh", f"{m.group('user')}@{m.group('host')}", allow_example_git=True
        )
        for m in _SCP_IN_TEXT_RE.finditer(text)
    )


def redact_urls_in_value(value: object, *, whole_value: bool = False) -> object:
    """Redact URLs in a decoded value whose keys are kept (a fixture's ``result``).

    A string under a :data:`URL_KEYS` key goes through :func:`redact_url`
    (kept as is when it is not a URL); every other string through
    :func:`redact_urls_in_text`, treating whole non-prose structured JSON
    values as valid protocol-relative URL contexts (0.16 R9-D3).
    """
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key in URL_KEYS and isinstance(item, str) and item:
                out[key] = redact_url(item) or redact_urls_in_text(item, whole_value=True)
            else:
                out[key] = redact_urls_in_value(item, whole_value=key not in _TEXT_VALUE_LEAVES)
        return out
    if isinstance(value, list):
        return [redact_urls_in_value(v, whole_value=whole_value) for v in value]
    if isinstance(value, str):
        return redact_urls_in_text(value, whole_value=whole_value)
    return value


def _repository_of(value: dict[str, object]) -> str | None:
    """The repository an object's URL key names when it names nothing under it, else ``None``."""
    for key, item in value.items():
        if key not in URL_KEYS or not isinstance(item, str):
            continue
        try:
            parts = urlsplit(item.strip())
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            continue
        if parts.scheme in URL_SCHEMES and parts.hostname:
            name = _repository_root(parts.path)
            if name is not None:
                return name
    return None


def redact_payload(value: object) -> object:
    """Keep only :data:`RECORDED_FIELDS` keys, recursively, and redact by class.

    :data:`PERSONAL_KEY_RE` keys are dropped everywhere; in a user object
    (one holding ``login`` or ``username``) ``name`` and the URL keys are
    dropped too, while elsewhere ``name`` is a check-run or job name the
    forges read. ``id`` is kept in every object. A string under a
    :data:`URL_KEYS` key is rewritten by :func:`redact_url` and dropped
    when it is not a URL; every other string has its URLs redacted. In
    a repository object (one whose URL names a repository and nothing
    under it), ``name`` and ``path`` become :data:`PLACEHOLDER_PROJECT`
    by key, whatever their value, and ``description`` is dropped (I4,
    0.10): the context decides, as a user object's ``name`` is dropped
    because a ``login`` stands beside it.
    """
    if isinstance(value, dict):
        user = any(k in value for k in USER_KEYS)
        repository = _repository_of(value) is not None
        out: dict[str, object] = {}
        for key, item in value.items():
            if key not in RECORDED_FIELDS or PERSONAL_KEY_RE.match(key):
                continue
            if user and (key == "name" or key in URL_KEYS):
                continue
            if repository and key in REPOSITORY_NAME_KEYS:
                out[key] = PLACEHOLDER_PROJECT
                continue
            if repository and key in REPOSITORY_DROPPED_KEYS:
                continue
            if key in URL_KEYS and isinstance(item, str):
                redacted = redact_url(item)
                if redacted:
                    out[key] = redacted
                continue
            out[key] = redact_payload(item)
        return out
    if isinstance(value, list):
        return [redact_payload(v) for v in value]
    if isinstance(value, str):
        return redact_urls_in_text(value)
    return value


def bot_mention_stem(login: str) -> str | None:
    """The handle stem of a ``<name>[bot]`` login for ``@<stem>`` mentions, whatever its length, unless a class word or non-person bot (0.14 R7-D4)."""
    if not login.casefold().endswith("[bot]"):
        return None
    stem = login[:-5]
    if not stem or CLASS_WORD_RE.match(stem.casefold()) or _names_no_one(stem):
        return None
    return stem


def bot_stem(login: str) -> str | None:
    """The bare prose stem of a ``<name>[bot]`` login when long enough and not a class word, placeholder or non-person bot (0.13 R6-D2, 0.14 R7-D4)."""
    stem = bot_mention_stem(login)
    if stem is None:
        return None
    folded = stem.casefold()
    if len(stem) < NEEDLE_MIN_LENGTH or folded in _PLACEHOLDERS:
        return None
    return stem


def _login_pattern(login: str) -> re.Pattern[str]:
    """``login`` as a whole token: :func:`token_pattern`, wherever it stands (I1, 0.10)."""
    return token_pattern(login)


def classify_text(text: str, roles: dict[str, str]) -> str:
    """Replace every login of ``roles`` appearing as a token in ``text`` by its class word.

    A :data:`MENTION_ONLY_PREFIX` key is replaced in its ``@<handle>``
    form only (D2, 0.9). A ``<name>[bot]`` key also replaces its
    ``@<name>`` mention whatever the stem length (0.14 R7-D4), and its
    bare prose stem when long enough and outside class words and
    placeholders (0.13 R6-D2), so bot login, mention and prose name stay
    one consistent identity. A :data:`CLI_PLACEHOLDER_RE` template is
    left as it is (0.10); every other occurrence of the token is
    replaced, after a ``:``, a ``.`` or a ``/`` alike.
    """
    pieces = CLI_PLACEHOLDER_RE.split(text)
    templates = CLI_PLACEHOLDER_RE.findall(text)
    expanded = dict(roles)
    present = {k.removeprefix(MENTION_ONLY_PREFIX).casefold() for k in roles}
    for login, word in roles.items():
        if login.startswith(MENTION_ONLY_PREFIX):
            continue
        bare = bot_stem(login)
        if bare is not None:
            if bare.casefold() not in present:
                expanded[bare] = word
                present.add(bare.casefold())
            continue
        mention_stem = bot_mention_stem(login)
        if mention_stem is not None and mention_stem.casefold() not in present:
            expanded[MENTION_ONLY_PREFIX + mention_stem] = word
            present.add(mention_stem.casefold())
    for login in sorted(expanded, key=len, reverse=True):
        if login.startswith(MENTION_ONLY_PREFIX):
            pattern = mention_pattern(login.removeprefix(MENTION_ONLY_PREFIX))
            word = f"@{expanded[login]}"
        else:
            pattern = _login_pattern(login)
            word = expanded[login]
        pieces = [pattern.sub(word, piece) for piece in pieces]
    out = pieces[0]
    for template, piece in zip(templates, pieces[1:], strict=True):
        out += template + piece
    return out


def classify_logins(payload: object, roles: dict[str, str]) -> object:
    """Replace every login in ``payload`` by its class word, recursively.

    A string under a :data:`LOGIN_KEYS` key is looked up in ``roles``
    (casefolded, as forges compare logins) and replaced when found; it
    is left as is otherwise, for the recorder's unclassified check. Any
    other string has the logins of ``roles`` replaced where they appear
    as a token (argv such as ``collaborators/<login>/permission``).

    Args:
        payload: A decoded JSON value, or an argv list.
        roles: Login → class word, built once per recording.

    Returns:
        The classified copy.
    """
    folded = {k.casefold(): v for k, v in roles.items()}
    return _classify(payload, folded, roles)


def _classify(payload: object, folded: dict[str, str], roles: dict[str, str]) -> object:
    """:func:`classify_logins` with the casefolded mapping built once."""
    if isinstance(payload, dict):
        out: dict[str, object] = {}
        for key, item in payload.items():
            if key in LOGIN_KEYS and isinstance(item, str):
                out[key] = folded.get(item.casefold(), item)
            else:
                out[key] = _classify(item, folded, roles)
        return out
    if isinstance(payload, list):
        return [_classify(v, folded, roles) for v in payload]
    if isinstance(payload, str):
        return classify_text(payload, roles)
    return payload


SYNTHETIC_ID_START = 1001
"""The first synthetic user id a recording hands out."""

SYNTHETIC_ID_END = 1999
"""The last synthetic user id (0.9): a declaration outside the range is not trusted."""

SYNTHETIC_IDS_KEY = "synthetic_user_ids"
"""The fixture key declaring the synthetic user ids the fixture holds (D1c, 0.8, 0.9).

The recorder and the offline transform write the numbers they handed
out. A user id is synthetic because its fixture declares it AND it lies
in ``SYNTHETIC_ID_START..SYNTHETIC_ID_END`` (:func:`trusted_declaration`):
a declared number outside the range — a real id typed into a fixture —
is remapped like any undeclared one.
"""


SYNTHETIC_PROJECT_ID_START = 2001
"""The first synthetic repository id (Q2, 0.10): a project id names a project as a user id names an account."""

SYNTHETIC_PROJECT_ID_END = 2999
"""The last synthetic repository id (0.10)."""

SYNTHETIC_PROJECT_IDS_KEY = "synthetic_project_ids"
"""The fixture key declaring the synthetic repository ids the fixture holds (0.10), as :data:`SYNTHETIC_IDS_KEY` does for users."""


class SyntheticIdsExhaustedError(Exception):
    """A map would hand out a number past the end of its synthetic range."""


def is_synthetic_number(
    value: object, low: int = SYNTHETIC_ID_START, high: int = SYNTHETIC_ID_END
) -> bool:
    """Whether ``value`` is an ``int`` in ``low..high`` (by default the user range)."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def trusted_declaration(
    raw: object, low: int = SYNTHETIC_ID_START, high: int = SYNTHETIC_ID_END
) -> frozenset[int]:
    """The numbers of a declaration that may be kept: the ones in ``low..high`` only."""
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(n for n in raw if is_synthetic_number(n, low, high))


RESULT_USER_ID_METHODS: frozenset[tuple[str, str]] = frozenset({("gitlab", "reviews")})
"""``(forge, method)`` pairs whose result items carry a user id as ``id``.

GitLab builds ``Review(id=<the approver's user id>)`` (``gitlab.py:728``);
GitHub's ``Review.id`` is the review's own id, and no other result holds
a user id, so only these positions follow an account's synthetic id.
"""

_SYNTHETIC_NODE_RE = re.compile(r"^U_([0-9]+)$")
"""A synthetic GitHub user node id, ``U_<n>``."""

_NEW_USER_NODE_RE = re.compile(r"^U_([A-Za-z0-9_-]+)$")
"""A GitHub user node id of the current format: ``U_`` and base64url of MessagePack ``[0, <id>]``."""

_LEGACY_USER_NODE_RE = re.compile(r"^04:User([0-9]+)$")
"""A decoded legacy GitHub user node id; its number is the account's numeric id."""

_MSGPACK_UINT_WIDTH = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xCF: 8}
"""MessagePack unsigned-integer markers and their byte widths."""


_BASE64_RE = re.compile(r"(?:[A-Za-z0-9+/]{4})+(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
"""A standard base64 string that decodes without error (a legacy node id's shape)."""


def _new_node_number(suffix: str) -> int | None:
    """The account number of a current-format node id's suffix, or ``None``.

    ``suffix`` matched ``[A-Za-z0-9_-]+``; a length of ``1 mod 4`` is not
    base64url, so the decode below cannot fail.
    """
    if len(suffix) % 4 == 1:
        return None
    raw = base64.urlsafe_b64decode(suffix + "=" * (-len(suffix) % 4))
    if raw[:2] != b"\x92\x00" or len(raw) < 3:
        return None
    marker, body = raw[2], raw[3:]
    if marker < 0x80 and not body:
        return marker
    width = _MSGPACK_UINT_WIDTH.get(marker)
    if width is None or len(body) != width:
        return None
    return int.from_bytes(body, "big")


def user_id_key(value: object) -> object:
    """The account a user id names: its number for every numeric form, else the string.

    The REST id ``<n>``, the digit string ``"<n>"``, a legacy GitHub node
    id (base64 of ``04:User<n>``), a current-format node id (``U_`` and
    base64url of MessagePack ``[0, <n>]``) and the synthetic ``U_<n>``
    all name account ``<n>``, so they map to one synthetic number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, int):
        return value
    if value.isdigit():
        return int(value)
    synthetic = _SYNTHETIC_NODE_RE.match(value)
    if synthetic:
        return int(synthetic.group(1))
    current = _NEW_USER_NODE_RE.match(value)
    if current:
        number = _new_node_number(current.group(1))
        return value if number is None else number
    if not _BASE64_RE.fullmatch(value):
        return value
    decoded = base64.b64decode(value).decode("ascii", errors="replace")
    m = _LEGACY_USER_NODE_RE.match(decoded)
    return int(m.group(1)) if m else value


def user_id_values(node: object) -> list[object]:
    """The ``id`` of every user object of a decoded value, JSON inside strings included."""
    if isinstance(node, str):
        if node.lstrip()[:1] not in ("{", "["):
            return []
        return [i for v in decode_json_values(node) or [] for i in user_id_values(v)]
    if isinstance(node, dict):
        found = [i for v in node.values() for i in user_id_values(v)]
        if any(k in node for k in USER_KEYS) and "id" in node:
            found.append(node["id"])
        return found
    if isinstance(node, list):
        return [i for v in node for i in user_id_values(v)]
    return []


def declared_user_ids(node: object) -> list[int]:
    """The sorted account numbers of every user id ``node`` holds, the value of :data:`SYNTHETIC_IDS_KEY`."""
    numbers = {user_id_key(i) for i in user_id_values(node)}
    return sorted(n for n in numbers if isinstance(n, int))


_API_PROJECT_ID_RE = re.compile(
    rf"(?P<prefix>(?<!{_HOST_BEFORE_BLOCK_CLASS})api/v[0-9]+/+projects/+)"
    rf"(?P<id>[0-9]+)(?!{_MENTION_HANDLE_CLASS})",
    re.I,
)
"""A numeric GitLab project id inside an ``api/v<N>/projects/<id>`` path (0.13 R6-D5, 0.14 R7-D3, 0.15 R8-D2, 0.16 R9-D4)."""

_GLAB_API_ARG_PROJECT_ID_RE = re.compile(
    rf"^(?P<prefix>/?projects/+)(?P<id>[0-9]+)(?!{_MENTION_HANDLE_CLASS})",
    re.I,
)
"""A numeric GitLab project id as the positional endpoint of a ``glab api`` invocation (0.15 R8-D2, 0.16 R9-D4)."""

_GLAB_API_CMD_RE = re.compile(
    rf"(?<!{_HOST_BEFORE_BLOCK_CLASS})glab{_HSPACE_CLASS}+api(?={_HSPACE_CLASS})",
    re.I,
)
"""Start of a ``glab api`` command invocation inside free text (0.16 R9-D4)."""

_GLAB_API_FALLBACK_ID_RE = re.compile(
    rf"(?<!{_HOST_BEFORE_BLOCK_CLASS})projects/+(?P<id>[0-9]+)(?!{_MENTION_HANDLE_CLASS})",
    re.I,
)
"""``projects/<digits>`` inside a ``glab api`` command: the exact id span in an endpoint token's raw source slice, and the safe fallback scan when ``shlex`` sees an unbalanced quote (0.16 R9-D4)."""

_GLAB_API_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "-F",
        "--field",
        "-H",
        "--header",
        "--hostname",
        "--input",
        "-X",
        "--method",
        "-f",
        "--raw-field",
    }
)
"""Documented ``glab api`` CLI flags that consume the following token as their value (0.16 R9-D4)."""


def _is_glab_api_argv(items: Sequence[object]) -> bool:
    """Whether ``items`` is a ``glab api`` argv list (``["api", ...]`` or ``["glab", "api", ...]``, 0.15 R8-D2)."""
    if not items:
        return False
    if items[0] == "api":
        return True
    return len(items) >= 2 and items[0] == "glab" and items[1] == "api"


def _glab_api_endpoint(tokens: Sequence[object]) -> tuple[int, str] | None:
    """Index and value of the first positional endpoint in a ``glab api`` token sequence (0.16 R9-D4).

    Shared by both ``shlex``-tokenised command lines and argv lists. Skips
    flags in both ``-H value`` (via :data:`_GLAB_API_VALUE_FLAGS`) and
    ``--header=value`` forms and boolean flags (``--`` and ``-`` count as
    boolean flags).
    """
    folded = [t.casefold() if isinstance(t, str) else "" for t in tokens]
    if not _is_glab_api_argv(folded):
        return None
    idx = 1 if folded[0] == "api" else 2
    while idx < len(tokens):
        tok = tokens[idx]
        if not isinstance(tok, str):
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 2 if tok in _GLAB_API_VALUE_FLAGS else 1
            continue
        return idx, tok
    return None


def _glab_api_text_id_spans(text: str) -> list[tuple[int, int, int]]:
    """``(start, end, project_id)`` spans of numeric project ids in ``glab api`` commands inside ``text`` (0.16 R9-D4)."""
    spans: list[tuple[int, int, int]] = []
    for cmd_match in _GLAB_API_CMD_RE.finditer(text):
        cmd_start = cmd_match.start()
        line_end = text.find("\n", cmd_start)
        if line_end == -1:
            line_end = len(text)
        cmd_end = line_end
        if cmd_start > 0 and text[cmd_start - 1] == "`":
            closing_tick = text.find("`", cmd_start, line_end)
            if closing_tick != -1:
                cmd_end = closing_tick
        segment = text[cmd_start:cmd_end]
        lexer = shlex.shlex(segment, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens: list[str] = []
        raw_spans: list[tuple[int, int]] = []
        endpoint: tuple[int, str] | None = None
        try:
            while True:
                pos0 = lexer.instream.tell()
                tok = lexer.get_token()
                if tok is None:
                    break
                pos1 = lexer.instream.tell()
                tokens.append(tok)
                raw_spans.append((pos0, pos1))
                endpoint = _glab_api_endpoint(tokens)
                if endpoint is not None:
                    break
        except ValueError as exc:
            _log.debug("forge.recording.glab_shlex_fallback", error=type(exc).__name__)
            for fb in _GLAB_API_FALLBACK_ID_RE.finditer(segment):
                if len(fb.group("id")) >= NEEDLE_MIN_LENGTH:
                    spans.append(
                        (
                            cmd_start + fb.start("id"),
                            cmd_start + fb.end("id"),
                            int(fb.group("id")),
                        )
                    )
            continue
        if endpoint is None:
            continue
        ep_idx, ep_tok = endpoint
        ep_match = _GLAB_API_ARG_PROJECT_ID_RE.match(ep_tok)
        if ep_match is None or len(ep_match.group("id")) < NEEDLE_MIN_LENGTH:
            continue
        pos0, pos1 = raw_spans[ep_idx]
        raw_slice = segment[pos0:pos1]
        raw_id = _GLAB_API_FALLBACK_ID_RE.search(raw_slice)
        if raw_id is not None:
            spans.append(
                (
                    cmd_start + pos0 + raw_id.start("id"),
                    cmd_start + pos0 + raw_id.end("id"),
                    int(ep_match.group("id")),
                )
            )
    return spans


def _api_project_id_spans(text: str) -> list[tuple[int, int, int]]:
    """Sorted non-overlapping ``(start, end, project_id)`` spans of real API and ``glab api`` project ids in ``text`` (0.16 R9-D4)."""
    kept = _kept_url_spans(text)
    found: list[tuple[int, int, int]] = []
    for match in _API_PROJECT_ID_RE.finditer(text):
        if _is_real_api_project_id(match, kept):
            found.append((match.start("id"), match.end("id"), int(match.group("id"))))
    for start, end, pid in _glab_api_text_id_spans(text):
        if any(s <= start < e or start <= s < end for s, e, _ in found):
            continue
        found.append((start, end, pid))
    found.sort(key=lambda item: item[0])
    return found


def _kept_url_spans(text: str) -> list[tuple[int, int]]:
    """Spans of kept-host text URLs without userinfo (documentation/example URLs, 0.14 R7-D3)."""
    spans: list[tuple[int, int]] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url, _tail = _split_url_tail(match.group(0))
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            continue
        host = parts.hostname or ""
        if host and "@" not in parts.netloc and _kept_host(host):
            spans.append((match.start(), match.start() + len(url)))
    return spans


def _is_real_api_project_id(match: re.Match[str], kept_spans: list[tuple[int, int]]) -> bool:
    """Whether an :data:`_API_PROJECT_ID_RE` match is a real project id rather than a doc example (0.14 R7-D3)."""
    if len(match.group("id")) < NEEDLE_MIN_LENGTH:
        return False
    return not any(start <= match.start() < end for start, end in kept_spans)


def is_repository_object(node: object) -> bool:
    """Whether ``node`` is a repository object: a dict whose URL names a repository and nothing under it."""
    return isinstance(node, dict) and _repository_of(node) is not None


def project_id_values(node: object) -> list[object]:
    """Every real repository id of a decoded value: repository objects and non-example ``api/v<N>/projects/<id>`` / ``glab api`` paths, JSON inside strings included (0.10, 0.13, 0.14, 0.16)."""
    if isinstance(node, str):
        if node.lstrip()[:1] in ("{", "["):
            decoded = decode_json_values(node)
            if decoded is not None:
                return [i for v in decoded for i in project_id_values(v)]
        return [pid for _start, _end, pid in _api_project_id_spans(node)]
    if isinstance(node, dict):
        found = [i for v in node.values() for i in project_id_values(v)]
        if is_repository_object(node) and "id" in node:
            found.append(node["id"])
        return found
    if isinstance(node, list):
        found = [i for v in node for i in project_id_values(v)]
        endpoint = _glab_api_endpoint(node)
        if endpoint is not None:
            _idx, ep_tok = endpoint
            match = _GLAB_API_ARG_PROJECT_ID_RE.match(ep_tok)
            if match is not None and _is_real_api_project_id(match, []):
                found.append(int(match.group("id")))
        return found
    return []


def declared_project_ids(node: object) -> list[int]:
    """The sorted repository ids ``node`` holds, the value of :data:`SYNTHETIC_PROJECT_IDS_KEY` (0.10)."""
    numbers = {user_id_key(i) for i in project_id_values(node)}
    return sorted(n for n in numbers if isinstance(n, int))


@dataclass
class UserIdMap:
    """Real user id → synthetic id, a sequence in order of first appearance (D1c).

    An account is keyed by ``(forge, number)``: the same number on GitHub
    and on GitLab names two accounts and gets two synthetic ids. The first
    account seen is :attr:`start`, the next ``start + 1``…; a node id
    becomes ``U_<n>`` with the same ``n`` as the account's numeric id.
    The mapping is never written anywhere, and a sequence carries no
    information about the id it replaces, so a fixture cannot be mapped
    back by enumerating small ids (as a hash of the id could).

    The same class maps repository ids (Q2, 0.10) with ``low``/``high``
    set to :data:`SYNTHETIC_PROJECT_ID_START`..:data:`SYNTHETIC_PROJECT_ID_END`
    (:meth:`for_projects`, :meth:`apply_projects`).

    Attributes:
        start: The first number handed out.
        ids: ``(forge, account)`` → synthetic number.
        low: The first number of the synthetic range a declaration may keep.
        high: The last number of the range; the map never hands out more.
    """

    start: int = SYNTHETIC_ID_START
    ids: dict[tuple[str, object], int] = field(default_factory=dict)
    low: int = SYNTHETIC_ID_START
    high: int = SYNTHETIC_ID_END

    @classmethod
    def for_projects(cls, start: int = SYNTHETIC_PROJECT_ID_START) -> UserIdMap:
        """A map of repository ids, in ``2001..2999`` (0.10)."""
        return cls(start=start, low=SYNTHETIC_PROJECT_ID_START, high=SYNTHETIC_PROJECT_ID_END)

    def synthetic(self, value: object, *, forge: str, keep: frozenset[int] = frozenset()) -> object:
        """The synthetic id for ``value`` on ``forge``, allocated on first sight.

        Args:
            value: A user id as the forge printed it.
            forge: ``github`` or ``gitlab``.
            keep: The synthetic numbers the fixture declares; such an id is
                left as it is when it is in range (:func:`is_synthetic_number`).

        Returns:
            The synthetic id, an ``int`` for an ``int`` and ``U_<n>`` for a string.

        Raises:
            SyntheticIdsExhaustedError: The next number would pass :attr:`high`.
        """
        key = user_id_key(value)
        if key is None:
            return value
        if is_synthetic_number(key, self.low, self.high) and key in keep:
            return value
        if (forge, key) not in self.ids:
            n = self.start + len(self.ids)
            if n > self.high:
                kind = "user" if self.high == SYNTHETIC_ID_END else "project"
                raise SyntheticIdsExhaustedError(f"synthetic {kind} id {n} past {self.high}")
            self.ids[(forge, key)] = n
        n = self.ids[(forge, key)]
        return f"U_{n}" if isinstance(value, str) else n

    def apply(self, payload: object, *, forge: str, keep: frozenset[int] = frozenset()) -> object:
        """Remap the ``id`` of every user object (one holding ``login`` or ``username``)."""
        if isinstance(payload, dict):
            user = any(k in payload for k in USER_KEYS)
            return {
                k: self.synthetic(v, forge=forge, keep=keep)
                if user and k == "id"
                else self.apply(v, forge=forge, keep=keep)
                for k, v in payload.items()
            }
        if isinstance(payload, list):
            return [self.apply(v, forge=forge, keep=keep) for v in payload]
        return payload

    def apply_projects(
        self, payload: object, *, forge: str, keep: frozenset[int] = frozenset()
    ) -> object:
        """Remap the ``id`` of every repository object and ``api/v4/projects/<id>`` URL (0.10, 0.13 R6-D5)."""
        if isinstance(payload, dict):
            repository = is_repository_object(payload)
            return {
                k: self.synthetic(v, forge=forge, keep=keep)
                if repository and k == "id"
                else self.apply_projects(v, forge=forge, keep=keep)
                for k, v in payload.items()
            }
        if isinstance(payload, list):
            endpoint = _glab_api_endpoint(payload)
            ep_idx = endpoint[0] if endpoint is not None else -1
            out_list: list[object] = []
            for idx, item in enumerate(payload):
                if idx == ep_idx and isinstance(item, str):
                    match = _GLAB_API_ARG_PROJECT_ID_RE.match(item)
                    if match is not None and _is_real_api_project_id(match, []):
                        item = _GLAB_API_ARG_PROJECT_ID_RE.sub(
                            lambda m: (
                                f"{m.group('prefix')}"
                                f"{self.synthetic(int(m.group('id')), forge=forge, keep=keep)}"
                            ),
                            item,
                        )
                out_list.append(self.apply_projects(item, forge=forge, keep=keep))
            return out_list
        if isinstance(payload, str):
            spans = _api_project_id_spans(payload)
            if not spans:
                return payload
            parts: list[str] = []
            cursor = 0
            for start, end, pid in spans:
                parts.append(payload[cursor:start])
                parts.append(str(self.synthetic(pid, forge=forge, keep=keep)))
                cursor = end
            parts.append(payload[cursor:])
            return "".join(parts)
        return payload

    def apply_result(
        self, result: object, *, forge: str, method: str, keep: frozenset[int] = frozenset()
    ) -> object:
        """Remap the user ids of a recorded result, at :data:`RESULT_USER_ID_METHODS` positions only.

        Every other ``id`` of a result (a review, a comment, a thread, a
        check run) is the object's own and is left as it is, even when its
        number equals a user id seen elsewhere.
        """
        if (forge, method) not in RESULT_USER_ID_METHODS or not isinstance(result, list):
            return result
        return [
            {**item, "id": self.synthetic(item["id"], forge=forge, keep=keep)}
            if isinstance(item, dict) and "id" in item
            else item
            for item in result
        ]


def login_values(payload: object, path: str = "") -> list[tuple[str, str]]:
    """Every ``(key path, value)`` of a string under a :data:`LOGIN_KEYS` key, in order."""
    if isinstance(payload, dict):
        found: list[tuple[str, str]] = []
        for key, item in payload.items():
            here = f"{path}.{key}" if path else key
            if key in LOGIN_KEYS and isinstance(item, str):
                found.append((here, item))
            else:
                found += login_values(item, here)
        return found
    if isinstance(payload, list):
        return [hit for i, v in enumerate(payload) for hit in login_values(v, f"{path}[{i}]")]
    return []


def decode_json_values(text: str) -> list[object] | None:
    """Decode every top-level JSON value of ``text``; ``None`` when it is not JSON (a diff, a log)."""
    decoder = json.JSONDecoder()
    stripped = text.strip()
    values: list[object] = []
    pos = 0
    try:
        while pos < len(stripped):
            value, pos = decoder.raw_decode(stripped, pos)
            values.append(value)
            while pos < len(stripped) and stripped[pos].isspace():
                pos += 1
    except json.JSONDecodeError as exc:
        _log.debug("forge.recording.not_json", error=str(exc)[:120])
        return None
    return values or None


def redact_stdout(text: str) -> str:
    """Redact a recorded stdout: JSON answers keep only :data:`RECORDED_FIELDS`.

    One JSON value is re-serialised compactly; several (``--paginate``
    pages, ``--jq`` lines) one per line, which both decoders accept.
    Non-JSON output (diffs, logs, ``git`` output) is kept verbatim and
    left to :func:`find_secrets`.
    """
    values = decode_json_values(text)
    if values is None:
        return text
    return "\n".join(json.dumps(redact_payload(v), ensure_ascii=False) for v in values)


def find_secrets(text: str) -> list[str]:
    """Return every secret-shaped value or key found in ``text`` (and its JSON)."""
    found = [m.group(0) for pattern in SECRET_VALUE_PATTERNS for m in pattern.finditer(text)]
    for value in decode_json_values(text) or []:
        found += _secret_keys(value)
    return found


def _secret_keys(value: object) -> list[str]:
    """Keys matching :data:`SECRET_KEY_RE` anywhere in a decoded payload."""
    if isinstance(value, dict):
        hits = [k for k in value if SECRET_KEY_RE.search(k)]
        for v in value.values():
            hits += _secret_keys(v)
        return hits
    if isinstance(value, list):
        return [k for v in value for k in _secret_keys(v)]
    return []


FIXTURE_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "args",
        "bot_username",
        "calls",
        "case",
        "forge",
        "kwargs",
        "method",
        "program",
        "raises",
        "result",
        "returncode",
        "source",
        "stderr",
        "stdin",
        "stdout",
        "trimmed",
        SYNTHETIC_IDS_KEY,
        SYNTHETIC_PROJECT_IDS_KEY,
    }
)
"""The keys a fixture and its exchanges are written with: the recorder's own words, never recorded data."""


def _model_fields() -> frozenset[str]:
    """Every field name of the protocol's models, the keys of a serialised ``result``."""
    names: set[str] = set()
    for value in vars(protocol).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            names |= set(value.model_fields)
    return frozenset(names)


CODE_KEYS: frozenset[str] = RECORDED_FIELDS | FIXTURE_SCHEMA_KEYS | _model_fields()
"""Keys written by the forges' or this package's code (0.11, R5): named in a key path, never judged as a value.

Every other key is recorded data: :func:`fixture_strings` yields it as a
string to judge and names it in a path by its ordinal only, since the
key may be the very value a refusal must not print.
"""


def fixture_strings(value: object, path: str) -> list[tuple[str, str]]:
    """Every ``(key path, string)`` of a decoded value, keys included; a JSON stdout is walked inside.

    A number is judged as its decimal string too (0.10): a user id left
    as a JSON number is as much a value as one left in text. A key is a
    string of the fixture as much as a value is (D3, 0.11): every key
    outside :data:`CODE_KEYS` is yielded at ``<path>.{key <i>}`` and its
    value walked under that same ordinal path, so no path prints a key
    that may name someone.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [(path, str(value))]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for i, (key, item) in enumerate(value.items()):
            if key in CODE_KEYS:
                found += fixture_strings(item, f"{path}.{key}")
                continue
            here = f"{path}.{{key {i}}}"
            found.append((here, str(key)))
            found += fixture_strings(item, here)
        return found
    if isinstance(value, list):
        return [hit for i, v in enumerate(value) for hit in fixture_strings(v, f"{path}[{i}]")]
    if isinstance(value, str):
        decoded = decode_json_values(value) if value.lstrip()[:1] in ("{", "[") else None
        if decoded is None:
            return [(path, value)]
        if len(decoded) == 1:
            return fixture_strings(decoded[0], path)
        return [hit for i, v in enumerate(decoded) for hit in fixture_strings(v, f"{path}[{i}]")]
    return []


def scanned_strings(fixture: dict[str, Any]) -> list[tuple[str, str]]:
    """Every string of a fixture about to be written: each exchange first, as ``exchanges[<i>]``, then every other key."""
    calls: list[object] = list(fixture.get("calls") or [])
    found = [hit for i, c in enumerate(calls) for hit in fixture_strings(c, f"exchanges[{i}]")]
    rest = {key: value for key, value in fixture.items() if key != "calls"}
    found += [(path.lstrip("."), text) for path, text in fixture_strings(rest, "")]
    return found


_NEW_NODE_IN_TEXT_RE = re.compile(r"(?=(U_[A-Za-z0-9_-]{2,}))")
"""A ``U_`` user node id candidate anywhere in text, scanned independently of base64 runs (0.12, D2)."""

_LEGACY_NODE_IN_TEXT_RE = re.compile(r"(?=(MDQ6VXNl[A-Za-z0-9+/]*={0,2}))")
"""A legacy ``04:User<n>`` base64 node id prefix anywhere in text or a URL/branch run (0.12, D2)."""

_LEGACY_USER_NODE_PREFIX_RE = re.compile(r"^04:User([0-9]+)")
"""The ``04:User<n>`` prefix of a decoded legacy node id run, stopping before any trailing suffix."""

_NODE_SUFFIX_MAX = 16
"""The longest current-format node id suffix tried: MessagePack ``[0, <uint64>]`` is 11 bytes, 15 characters."""

_LEGACY_NODE_MAX = 40
"""The longest legacy node id base64 prefix decoded: ``04:User`` plus 20 uint64 digits is 36 characters."""


def _legacy_node_number(run: str) -> int | None:
    """The account number of a legacy ``MDQ6VXNl…`` run (possibly suffixed), or ``None``."""
    body = run[:_LEGACY_NODE_MAX].rstrip("=")
    if len(body) < 8:
        return None
    if len(body) % 4 == 1:
        padded = body[: len(body) - len(body) % 4]
    else:
        padded = body + "=" * (-len(body) % 4)
    decoded = base64.b64decode(padded).decode("ascii", errors="replace")
    match = _LEGACY_USER_NODE_PREFIX_RE.match(decoded)
    return int(match.group(1)) if match else None


def embedded_id_numbers(text: str) -> list[str]:
    """The account number of every user node id ``text`` holds, wherever it stands (0.11 R4, 0.12 D2).

    Scans current-format ``U_…`` runs independently of base64 runs (so a
    preceding URL path or alpha prefix never eats the ``U``) and finds
    the legacy ``MDQ6VXNl`` prefix anywhere in a run (URL paths, branch
    names, prefixed or suffixed strings), on the text's own case, since
    base64 is case-sensitive. A run of digits is the number itself and is
    not repeated.
    """
    numbers: list[str] = []
    for m in _NEW_NODE_IN_TEXT_RE.finditer(text):
        run = m.group(1)
        suffix = run[2:]
        candidates = (
            [run]
            if suffix.isdigit()
            else [f"U_{suffix[:k]}" for k in range(2, min(len(suffix), _NODE_SUFFIX_MAX) + 1)]
        )
        for cand in candidates:
            number = user_id_key(cand)
            if isinstance(number, int) and str(number) not in numbers:
                numbers.append(str(number))
    for m in _LEGACY_NODE_IN_TEXT_RE.finditer(text):
        number = _legacy_node_number(m.group(1))
        if isinstance(number, int) and str(number) not in numbers:
            numbers.append(str(number))
    return numbers


_JSON_U_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})")
"""A JSON ``\\uXXXX`` escape written as text inside a string."""

_DECODE_ROUNDS = 4
"""How many times the oracle peels percent-, entity- and ``\\u``-encodings off a string (``%2525`` is two)."""

CONFUSABLES = str.maketrans(
    {
        "а": "a",
        "в": "b",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "һ": "h",
        "ӏ": "l",
        "ԛ": "q",
        "ԝ": "w",
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
    }
)
"""Lowercase Cyrillic and Greek letters drawn like a Latin one: best-effort defence-in-depth (0.11; general confusables are NG4 since 0.12)."""


def _plain(text: str) -> str:
    """``text`` with full-width forms, invisible characters and lookalike letters folded.

    NFKC folds compatibility forms (full-width letters), format characters
    (zero-width space, soft hyphen) are removed and :data:`CONFUSABLES` mapped.
    """
    if text.isascii():
        return text
    folded = unicodedata.normalize("NFKC", text)
    visible = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return visible.translate(CONFUSABLES)


def _decoded_once(text: str) -> str:
    r"""``text`` with one layer of ``\uXXXX``, HTML-entity and percent encoding removed."""
    unescaped = _JSON_U_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return unquote(html.unescape(unescaped))


def oracle_forms(text: str, *, literals: bool = True) -> list[str]:
    r"""Every form the closure oracle judges ``text`` in, casefolded (I2, I3, 0.11).

    The text as written; each layer of percent, HTML-entity and ``\u``
    encoding peeled off in turn; each of those with compatibility forms,
    invisible characters and lookalike letters folded (:func:`_plain`);
    and the account number of every node id any of them holds
    (:func:`embedded_id_numbers`). No form is built with a redaction
    regex, so a value the redactor did not recognise is still a substring
    of one of them. A :data:`CLI_PLACEHOLDER_RE` literal is blanked
    first, being no recorded value, unless ``literals`` is false: the
    deny-list judges the text whole, a literal included, since an entry is
    the maintainer's own declaration and is refused wherever it stands.
    """
    current = CLI_PLACEHOLDER_RE.sub(" ", text) if literals else text
    stages = [current]
    for _ in range(_DECODE_ROUNDS):
        peeled = _decoded_once(current)
        if peeled == current:
            break
        stages.append(peeled)
        current = peeled
    stages += [plain for plain in map(_plain, stages) if plain not in stages]
    forms: list[str] = []
    for stage in stages:
        for form in (stage.casefold(), *embedded_id_numbers(stage)):
            if form not in forms:
                forms.append(form)
    return forms


NEEDLE_MIN_LENGTH = 4
"""A needle shorter than this would match everywhere; it is a configuration error (exit 2), never a skip."""

FORGE_HOSTS: frozenset[str] = frozenset({"github.com", "api.github.com", "gitlab.com"})
"""The public forges' own hosts, needles of every recording (I3, 0.10)."""

PUBLIC_FORGE_DOMAINS: tuple[str, ...] = ("github.com", "gitlab.com", "githubusercontent.com")
"""A host under one of these is a public forge's (0.11): a project there is the recorded one or a third party's."""

NEEDLE_KINDS: tuple[str, ...] = ("login", "host", "segment", "id", "deny-list")
"""The sources of the closure oracle's needles, in the order they are judged (I3, 0.10)."""


class NeedleError(Exception):
    """A needle the oracle cannot judge (too short, or a class word or placeholder); the message names its kind and ordinal."""


@dataclass(frozen=True, repr=False)
class Needles:
    """The values a built fixture must not hold, casefolded, by kind (I3, 0.10).

    Built from the raw exchanges and from facts, never from the
    redactor's output. Opaque: its ``repr`` counts, so no value reaches a
    log, a traceback or ``pytest -l``.

    Attributes:
        entries: ``(kind, casefolded value)`` in order.
        replacements: ``(raw value, placeholder)`` for the hosts and the
            path segments the pipeline replaces as tokens.
    """

    entries: tuple[tuple[str, str], ...]
    replacements: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        """A count, never a value."""
        return f"<needles: {len(self.entries)}>"

    def __len__(self) -> int:
        """How many needles."""
        return len(self.entries)

    def mapping(self) -> dict[str, str]:
        """Raw host or segment → its placeholder, for :func:`classify_logins` beside the roles."""
        return dict(self.replacements)


_HOST_TOKEN_RE = re.compile(
    rf"(?<!{_HOST_BEFORE_BLOCK_CLASS})"
    rf"((?:{_TOKEN_CLASS}(?:{_SCP_HOST_LABEL_CLASS}*{_TOKEN_CLASS})?\.)+[A-Za-z]{_SCP_HOST_LABEL_CLASS}*{_TOKEN_CLASS})"
    rf"(?!{_SCP_HOST_LABEL_CLASS})",
    re.I,
)
"""A dotted name ending in a letter-led label: a host candidate, judged by its context (:func:`locations`)."""

_AFTER_HOST_DELIMITERS = "\"'<>()[]{}\\,?#|`*"
"""Delimiters excluded from a path segment after a host candidate in :data:`_AFTER_HOST_RE` (0.16)."""

_AFTER_HOST_CHAR = _char_class(_AFTER_HOST_DELIMITERS, negated=True, extra_escapes=(r"\s",))
_AFTER_HOST_NOSLASH = _char_class(
    _AFTER_HOST_DELIMITERS + "/", negated=True, extra_escapes=(r"\s",)
)
_AFTER_HOST_SEG = rf"(?:{_AFTER_HOST_CHAR}|\((?:{_AFTER_HOST_CHAR}|\({_AFTER_HOST_CHAR}*\))*\))*"
_AFTER_HOST_RE = re.compile(
    rf"\.?(?:(?P<port>:[0-9]{{1,5}})?/(?P<slash>{_AFTER_HOST_SEG})"
    rf"|:(?P<scp>{_AFTER_HOST_NOSLASH}*/{_AFTER_HOST_SEG}))",
    re.I,
)
"""What may follow a host: an optional DNS dot, then optional port + ``/`` or scp ``:<path-with-slash>`` with GFM balanced parentheses (0.13, 0.15 Pref 2)."""

_URL_USERINFO_BEFORE_RE = re.compile(rf"://(?:{_RFC3986_USERINFO_CLASS}*@)+$", re.I)
"""Text ending in ``://<userinfo>@`` with any RFC 3986 userinfo character, empty password or embedded ``@`` (0.15 R8-D3, 0.16 R9-D1)."""

_USERINFO_AHEAD_RE = re.compile(rf"(?:{_RFC3986_USERINFO_CLASS}*@)+", re.I)
"""Remaining userinfo ending in ``@`` immediately after a dotted username inside ``scheme://user.name:pw@host`` (0.15 R8-D3, 0.16 R9-D1)."""

_USER_BEFORE_RE = re.compile(rf"{_SCP_USER_CLASS}@$", re.I)
"""Text ending in ``<user>@``: the host after it is a remote's or a mailbox's (``@pytest.fixture`` is neither)."""


def _is_slash_led(
    before: str, *, text: str = "", url_end: int = 0, whole_value: bool = False
) -> bool:
    """Whether ``before`` ends in a URL ``://`` or positive-context protocol-relative ``//`` lead (0.16 R9-D3)."""
    if not before.endswith("//"):
        return False
    if before.endswith("://"):
        return True
    if whole_value and before == "//" and not any(ch.isspace() for ch in text):
        return True
    lead_match = _PROTO_REL_LEAD_RE.search(before[:-2])
    if lead_match is None:
        return False
    if lead_match.group("json") is not None:
        rest = text[url_end:]
        close_quote = rest.find('"')
        return close_quote != -1 and not any(ch.isspace() for ch in rest[:close_quote])
    return True


def _has_user_before(before: str) -> bool:
    """Whether ``before`` ends with URL or SCP/mailbox userinfo ``@`` (0.15 R8-D3)."""
    return bool(_URL_USERINFO_BEFORE_RE.search(before) or _USER_BEFORE_RE.search(before))


_IPV4_AFTER_BLOCK_CLASS = _char_class(_ASCII_ALNUM + ".-")
"""Characters that may not immediately follow an IPv4 host literal in :data:`_IPV4_LED_HOST_RE` (0.16)."""

_IPV4_LED_HOST_RE = re.compile(
    rf"(?<!{_HOST_BEFORE_BLOCK_CLASS})([0-9]{{1,3}}(?:\.[0-9]{{1,3}}){{3}})(?!{_IPV4_AFTER_BLOCK_CLASS})",
    re.I,
)
"""An IPv4 host literal, accepted by :func:`locations` only when scheme- or user-led (``//`` or ``<user>@``, 0.13)."""


def _is_ipv4_host(host: str) -> bool:
    """Whether ``host`` is a valid dotted-quad IPv4 address (four octets 0..255)."""
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


@dataclass(frozen=True)
class Location:
    """A host and the path segments after it, as :func:`locations` reads them from raw text.

    Attributes:
        host: The host, lowercased.
        segments: The path's segments (empty when the host stands alone).
        authenticated: Whether the host was led by URL or SCP userinfo (``user[:pass]@``).
    """

    host: str
    segments: tuple[str, ...] = ()
    authenticated: bool = field(default=False, compare=False)


def locations(
    text: str, *, known_hosts: frozenset[str] = frozenset(), whole_value: bool = False
) -> list[Location]:
    """Every host of ``text`` with the path after it, read without the redactor's regexes (I3, 0.11, 0.12, 0.13, 0.14, 0.16).

    A dotted name is a host when its context says so: ``://``, a
    positive-context protocol-relative ``//`` (whole JSON or decoded
    structured string value with ``whole_value=True``, ``="`` / ``='`` /
    ``=``, ``url(``, ``](`` or ``<``; 0.16 R9-D3), or ``<user>@`` before it
    (``https://h``, ``ssh://u@h``, ``u@h:g/r``, including scheme- or
    user-led IPv4 hosts such as ``https://10.20.30.40/g/r``, 0.13), or an
    un-led path after a host in ``known_hosts`` (``h/g/r``, ``h:g/r``,
    ``h:8080/x``, 0.14 Pref 4). A name right after a single ``/`` is a file
    path's (``src/setup.py``), one with nothing around it (``setup.py``) is
    not a host, and an un-led candidate is accepted only when it is in
    ``known_hosts`` (a forge host, the remote/inferred host, or a host
    already seen led by ``://``, positive ``//`` or ``@`` in the same
    fixture) and its non-port path has at least two non-empty non-all-digit
    segments — so macOS bundle paths (``Xcode.app/Contents/…``), code
    attribute divisions (``config.dev/env/prod``, ``ssl.ca/certs/root.pem``,
    ``stream.io/read/write``, ``torch.net/layers/conv``) and single-line
    ``//`` code comments are never read as hosts. Trailing prose punctuation
    and boundary Markdown delimiters are trimmed from the path (0.13 R6-D1,
    0.14 R7-D2).
    """
    candidates = sorted(
        (
            *((m, False) for m in _HOST_TOKEN_RE.finditer(text)),
            *((m, True) for m in _IPV4_LED_HOST_RE.finditer(text)),
        ),
        key=lambda item: item[0].start(),
    )
    found: list[Location] = []
    for m, is_ipv4 in candidates:
        before = text[: m.start()]
        if before.endswith("/") and not before.endswith("//"):
            continue
        if before.endswith("://") and _USERINFO_AHEAD_RE.match(text, m.end()):
            continue
        after = _AFTER_HOST_RE.match(text, m.end())
        raw_path = (after.group("slash") or after.group("scp") or "") if after else ""
        path, _tail = _split_url_tail(raw_path)
        url_end = after.end() if after else m.end()
        user_led = _has_user_before(before)
        led = _is_slash_led(before, text=text, url_end=url_end, whole_value=whole_value) or user_led
        host = m.group(1).casefold()
        if is_ipv4 and (not led or not _is_ipv4_host(host)):
            continue
        segments = tuple(s for s in path.split("/")) if path else ()
        if not led:
            if not after or host not in known_hosts:
                continue
            non_empty = [s for s in segments if s]
            if not after.group("port") and (
                len(non_empty) < 2 or all(s.isdigit() for s in non_empty)
            ):
                continue
        found.append(Location(host, segments, authenticated=user_led))
    return found


def remote_location(remote: str) -> tuple[str | None, tuple[str, ...]]:
    """The host and path segments of a remote: a URL, an scp remote or an ssh alias (``alias:g/r``).

    Returns:
        ``(host, segments)``; the host is ``None`` for an ssh alias (a
        name with no dot, resolved by the maintainer's ssh configuration)
        or no remote, and the segments are still those of the path.
    """
    text = remote.strip()
    if not text:
        return None, ()
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            return None, ()
        segments = tuple(s for s in parts.path.split("/") if s)
        return (parts.hostname or None), segments
    head, sep, path = text.partition(":")
    if not sep:
        return None, ()
    host = head.rsplit("@", 1)[-1].casefold()
    segments = tuple(s for s in path.split("/") if s)
    return (host if "." in host else None), segments


def is_public_forge_host(host: str) -> bool:
    """Whether ``host`` is a public forge's (:data:`PUBLIC_FORGE_DOMAINS`), a subdomain included."""
    folded = host.casefold()
    return any(folded == d or folded.endswith(f".{d}") for d in PUBLIC_FORGE_DOMAINS)


_API_PROJECT_RE = re.compile(
    r"^/*(?:api/v[0-9]+/+)?(repos|projects)/+([^/?#\s]+)(?:/+([^/?#\s]+))?"
)
"""An API path naming a project: ``repos/<owner>/<repo>/…`` or ``projects/<group>%2F<repo>/…`` (argv)."""


def _api_project(text: str) -> tuple[str, ...]:
    """The namespace and repository segments an argv API path names, its CLI templates aside."""
    m = _API_PROJECT_RE.match(text)
    if m is None:
        return ()
    if m.group(1) == "repos":
        parts = [m.group(2), m.group(3) or ""]
    else:
        parts = [p for p in unquote(m.group(2)).split("/") if p]
    named = tuple(p for p in parts if p and not CLI_PLACEHOLDER_RE.fullmatch(p) and not p.isdigit())
    return named if len(named) == len([p for p in parts if p]) and len(named) >= 2 else ()


def _raw_url_host(raw: object) -> str | None:
    """The first non-example host of a :data:`URL_KEYS` value in ``raw``, JSON inside strings included."""
    stack: list[object] = [raw]
    while stack:
        node = stack.pop(0)
        if isinstance(node, str):
            if node.lstrip()[:1] in ("{", "["):
                stack[:0] = decode_json_values(node) or []
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in URL_KEYS and isinstance(item, str):
                    try:
                        parts = urlsplit(item.strip())
                    except ValueError as exc:
                        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
                        continue
                    if parts.scheme in URL_SCHEMES and parts.hostname:
                        host = parts.hostname.casefold()
                        if not _is_allowed_host(host):
                            return host
                else:
                    stack.append(item)
        elif isinstance(node, list):
            stack[:0] = list(node)
    return None


def raw_logins(raw: object) -> list[str]:
    """Every login of the raw answers, under any key the redaction may drop (0.11, R2).

    ``assignees[].login`` and ``mergedBy.login`` are not recorded fields,
    so a login known only there never reaches ``roles``; read from the
    raw decoded stdout, it is a needle and a role all the same. A class
    word or a :data:`NON_PERSON_LOGINS` entry is not one.
    """
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            if node.lstrip()[:1] in ("{", "["):
                for value in decode_json_values(node) or []:
                    _walk(value)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key not in LOGIN_KEYS or not isinstance(item, str):
                    _walk(item)
                elif item and not is_classified_login(item) and item not in found:
                    found.append(item)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(raw)
    return found


def _id_forms(value: object) -> list[str]:
    """A raw id as the strings it may stand as: its digits, and a node id's own string."""
    key = user_id_key(value)
    forms = [str(key)] if isinstance(key, int) else []
    if isinstance(value, str) and value not in forms:
        forms.append(value)
    return forms


def _names_no_one(value: str) -> bool:
    """Whether a login or path segment is a :data:`NON_PERSON_LOGINS` entry, ``[bot]`` suffix or not (D2)."""
    folded = value.casefold()
    return any(
        folded in (n.casefold(), n.casefold().removesuffix("[bot]")) for n in NON_PERSON_LOGINS
    )


def needles(
    raw: object,
    *,
    roles: dict[str, str],
    remote: str = "",
    denylist: tuple[str, ...] = (),
    min_length: int = NEEDLE_MIN_LENGTH,
) -> Needles:
    """The closure oracle's needles of one recording (I3, 0.11, 0.12), from the raw exchanges and facts.

    - login: every key of ``roles`` (a mention-only key without its
      ``@``) and every login of the raw answers (:func:`raw_logins`);
    - host: :data:`FORGE_HOSTS`, the remote's host and every host of
      ``raw`` (:func:`locations`), but an RFC 2606 or loopback one;
    - segment: the recorded project's namespace and repository — the
      remote's path, the argv's API paths — and every namespace and
      repository of a URL on the remote's host when it is not a public
      forge, or on the host inferred from :data:`URL_KEYS` when the
      remote is an ssh alias (so third-party links like
      ``docs.python.org`` never become segment needles under an ssh
      alias, 0.12 D6), or on any private host when no remote exists at
      all. A third party's project on a public forge
      (``github.com/python/cpython``) names no one of the recording and
      is not a needle;
    - id: every raw user id and repository id, in each of its forms;
    - deny-list: the maintainer's entries.

    Args:
        raw: The exchanges as the forge printed them, the method's
            arguments and the remote's URL; never a redacted value.
        roles: The recording's login → class word.
        remote: The checkout's ``origin`` remote, ``""`` when it has none.
        denylist: The deny-list entries.
        min_length: The shortest needle that can be judged; the offline
            transform passes 1, since its corpus is committed and reviewed.

    Returns:
        The needles, casefolded, each once, with the replacements of the
        hosts (:data:`PLACEHOLDER_HOST`) and segments (:data:`PLACEHOLDER_NAMESPACE`,
        :data:`PLACEHOLDER_PROJECT`).

    Raises:
        NeedleError: A needle shorter than :data:`NEEDLE_MIN_LENGTH`, or a
            class word or placeholder, by kind and ordinal.
    """
    located_strings = fixture_strings(raw, "")
    instance, remote_path = remote_location(remote)
    inferred = _raw_url_host(raw) if instance is None else None
    if instance is None and inferred is not None:
        instance = inferred
    seen_led_hosts = {
        place.host
        for path, text in located_strings
        for place in locations(text, whole_value=is_structured_json_path(path))
        if not _is_ipv4_host(place.host) and not _is_allowed_host(place.host)
    }
    known_hosts = frozenset(
        FORGE_HOSTS
        | ({instance} if instance else set())
        | ({inferred} if inferred else set())
        | seen_led_hosts
    )
    found: dict[str, list[str]] = {kind: [] for kind in NEEDLE_KINDS}
    replacements: dict[str, str] = {}

    def _segments(path: tuple[str, ...] | list[str]) -> None:
        identity = path_identity(list(path))
        for segment in identity.namespace:
            found["segment"].append(segment)
            replacements.setdefault(segment, PLACEHOLDER_NAMESPACE)
        if identity.repository:
            found["segment"].append(identity.repository)
            replacements.setdefault(identity.repository, PLACEHOLDER_PROJECT)

    raw_login_list = [k.removeprefix(MENTION_ONLY_PREFIX) for k in roles] + raw_logins(raw)
    bot_stems: list[str] = []
    for login in raw_login_list:
        bare = bot_stem(login)
        if bare is not None:
            bot_stems.append(bare)
            continue
        mention_stem = bot_mention_stem(login)
        if mention_stem is not None:
            at_stem = f"{MENTION_ONLY_PREFIX}{mention_stem}"
            if len(at_stem) >= min_length:
                bot_stems.append(at_stem)
    found["login"] = raw_login_list + bot_stems
    found["host"] = sorted(FORGE_HOSTS | ({instance} if instance else set()))
    if remote_path:
        _segments(remote_path)
    for path, text in located_strings:
        _segments(_api_project(text))
        for place in locations(
            text, known_hosts=known_hosts, whole_value=is_structured_json_path(path)
        ):
            if _is_allowed_host(place.host):
                continue
            found["host"].append(place.host)
            private = not is_public_forge_host(place.host)
            matches_instance = place.host == instance if instance is not None else not remote_path
            if (
                private
                and (matches_instance or _is_ipv4_host(place.host) or place.authenticated)
                and place.segments
            ):
                _segments(place.segments)
    for host in found["host"]:
        replacements.setdefault(host, PLACEHOLDER_HOST)
    found["id"] = [f for v in user_id_values(raw) + project_id_values(raw) for f in _id_forms(v)]
    found["deny-list"] = list(denylist)
    entries: list[tuple[str, str]] = []
    for kind in NEEDLE_KINDS:
        seen: list[str] = []
        named = (
            v for v in found[kind] if v and not (kind in ("login", "segment") and _names_no_one(v))
        )
        for value in (v.casefold() for v in named):
            if value not in seen:
                seen.append(value)
        for n, value in enumerate(seen, 1):
            if len(value) < min_length:
                raise NeedleError(f"needle {kind} {n}: too short")
            if CLASS_WORD_RE.match(value) or value in _PLACEHOLDERS:
                raise NeedleError(f"needle {kind} {n}: a class word or placeholder")
            entries.append((kind, value))
    kept = {v for _k, v in entries}
    return Needles(
        tuple(entries),
        tuple(
            (raw_value, word)
            for raw_value, word in replacements.items()
            if raw_value.casefold() in kept
        ),
    )


_PLACEHOLDERS: frozenset[str] = frozenset(
    {PLACEHOLDER_HOST, PLACEHOLDER_NAMESPACE, PLACEHOLDER_PROJECT}
)
"""The placeholders the redaction writes; a needle equal to one could never be told from it."""

CONSTANT_FIELDS: frozenset[str] = frozenset({"forge", "method"})
"""Top-level fields the recorder writes from closed enumerations (0.11 R6, 0.12 D3), judged by the deny-list only.

``forge`` is ``github`` or ``gitlab`` and ``method`` is a protocol
method's name; a segment needle that happens to spell one (a project
named ``github``) is no leak there. ``case`` is free-form user input and
is judged by every needle (0.12, D3).
"""

RECORDED_SOURCE_HEAD_RE = re.compile(
    r"^recorded [0-9]{4}-[0-9]{2}-[0-9]{2} from a live (?:github|gitlab) change request"
)
"""The template head :func:`recorded_source` writes; the oracle judges only what follows it (0.11, R6)."""


def residual(fixture: dict[str, Any], found: Needles) -> tuple[str, int, str] | None:
    """The first needle a built fixture still holds, as ``(kind, ordinal, key path)``, or ``None`` (I3, 0.11).

    Each string of the fixture, keys included (:func:`scanned_strings`),
    is judged in every :func:`oracle_forms` form against each needle with
    :func:`token_pattern`, the one boundary. Those forms are built by
    decoding, never by a redaction regex; a deny-list entry is judged on
    the forms that keep the literals, so no literal hides one. The recorder's
    :data:`CONSTANT_FIELDS` are judged by the deny-list only, and
    ``source`` without its :data:`RECORDED_SOURCE_HEAD_RE` head. The
    ordinal counts within the needle's kind.
    """
    ordinals: list[tuple[str, int, re.Pattern[str]]] = []
    counts: dict[str, int] = {}
    for kind, value in found.entries:
        counts[kind] = counts.get(kind, 0) + 1
        ordinals.append((kind, counts[kind], token_pattern(value)))
    for path, text in scanned_strings(fixture):
        judged = ordinals
        if path in CONSTANT_FIELDS:
            judged = [o for o in ordinals if o[0] == "deny-list"]
        elif path == "source":
            text = RECORDED_SOURCE_HEAD_RE.sub("", text)
        forms = oracle_forms(text)
        whole = oracle_forms(text, literals=False)
        for kind, n, pattern in judged:
            if any(pattern.search(form) for form in (whole if kind == "deny-list" else forms)):
                return kind, n, path
    return None


@dataclass
class Exchange:
    """One recorded subprocess call.

    Attributes:
        program: ``gh``, ``glab`` or ``git`` (the binary's basename).
        args: Argv after the program.
        stdin: The stdin payload, when any.
        returncode: Exit code.
        stdout: Captured stdout.
        stderr: Captured stderr.
    """

    program: str
    args: list[str]
    stdin: str | None
    returncode: int
    stdout: str
    stderr: str

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in a fixture."""
        return {
            "program": self.program,
            "args": self.args,
            "stdin": self.stdin,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Exchange:
        """Rebuild an exchange from its fixture object."""
        return cls(
            program=str(data["program"]),
            args=[str(a) for a in data["args"]],
            stdin=data.get("stdin"),
            returncode=int(data["returncode"]),
            stdout=str(data.get("stdout") or ""),
            stderr=str(data.get("stderr") or ""),
        )


def _program(argv0: str) -> str:
    """The basename of a spawned binary."""
    return Path(argv0).name


@dataclass
class RecordingSpawn:
    """A ``_spawn`` replacement that forwards to the real one and records.

    Attributes:
        inner: The forge's original ``_spawn``.
        exchanges: Every call made so far, in order.
    """

    inner: Any
    exchanges: list[Exchange] = field(default_factory=list)

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Run ``argv`` for real and record the exchange."""
        res: CommandResult = self.inner(argv, input_data=input_data)
        self.exchanges.append(
            Exchange(
                program=_program(argv[0]),
                args=list(argv[1:]),
                stdin=input_data,
                returncode=res.returncode,
                stdout=res.stdout,
                stderr=res.stderr,
            )
        )
        return res


class ReplayMismatchError(AssertionError):
    """The code under test made a call the fixture did not record."""


@dataclass
class ReplaySpawn:
    """A ``_spawn`` replacement that answers from recorded exchanges, in order.

    Attributes:
        exchanges: The recorded exchanges still to be consumed.
    """

    exchanges: list[Exchange]

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Return the next recorded result after checking the argv matches."""
        if not self.exchanges:
            raise ReplayMismatchError(f"unrecorded call: {argv[1:]}")
        expected = self.exchanges.pop(0)
        actual = (_program(argv[0]), list(argv[1:]))
        if actual != (expected.program, expected.args):
            raise ReplayMismatchError(f"expected {expected.program} {expected.args}, got {actual}")
        if (input_data or None) != (expected.stdin or None):
            raise ReplayMismatchError(f"stdin differs for {expected.args}")
        return CommandResult(
            returncode=expected.returncode,
            stdout=expected.stdout,
            stderr=expected.stderr,
            argv=list(argv),
        )


def coerce_call(
    method: str, args: list[Any], kwargs: dict[str, Any]
) -> tuple[list[Any], dict[str, Any]]:
    """Turn a fixture's JSON arguments into the protocol's typed arguments.

    ``post_review`` takes a :class:`ReviewDraft`; ``merge`` a
    :class:`MergeMethod`. Every other method takes JSON scalars.
    """
    typed_args = list(args)
    typed_kwargs = dict(kwargs)
    if method == "post_review" and len(typed_args) == 2 and isinstance(typed_args[1], dict):
        draft = dict(typed_args[1])
        draft["comments"] = [InlineCommentDraft(**c) for c in draft.get("comments", [])]
        typed_args[1] = ReviewDraft(**draft)
    if method == "merge" and "method" in typed_kwargs:
        typed_kwargs["method"] = MergeMethod(typed_kwargs["method"])
    return typed_args, typed_kwargs


def serialize_result(value: object) -> object:
    """The JSON form of a protocol method's return value."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, CommandResult):
        return {"returncode": value.returncode, "stdout": value.stdout, "stderr": value.stderr}
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, list):
        return [serialize_result(v) for v in value]
    return value


def load_fixture(path: Path) -> dict[str, Any]:
    """Read one fixture file."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def dump_fixture(path: Path, data: dict[str, Any]) -> None:
    """Write one fixture file (sorted keys, two-space indent, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
