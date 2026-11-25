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
import functools
import inspect
import ipaddress
import itertools
import json
import re
import string
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

from pydantic import BaseModel

from ..core import text_forms
from ..core.logging import get_logger
from ..core.redaction import SECRET_VALUE_PATTERNS
from . import closure, protocol
from .denylist import Denylist, read_denylist
from .fixture_reading import (
    CLASS_WORD_RE,
    LOGIN_KEYS,
    NON_PERSON_LOGINS,
    RECORDED_FIELDS,
    RECORDED_SOURCE_HEAD_RE,
    decode_json_values,
    fixture_strings,
    is_classified_login,
    login_values,
    secret_keys,
)
from .protocol import InlineCommentDraft, MergeMethod, ReviewDraft
from .recording_tables import (
    GITHUB_REPO_API_ROUTES,
    LANGUAGE_AT_WORDS,
    RESERVED_ROUTE_WORDS,
)
from .runner import CommandResult

_log = get_logger(__name__)


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


GITLAB_PROJECT_BOT_RE = re.compile(r"^project_[0-9]+_bot_[0-9a-f]+$", re.I)
"""A GitLab project access token user: a service account named after a real project id."""


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
    """``word`` as a whole token (:data:`TOKEN_CHARS`), case-insensitive: the only whole-word pattern factory.

    A ``%2F`` or ``%2f`` right before the word is a delimiter as well:
    ``projects/<ns>%2F<repo>`` is the one URL-encoded shape the forges' own
    argv carries (D7).
    """
    return re.compile(
        rf"(?:(?<!{_TOKEN_CLASS})|(?<=%2[Ff])){re.escape(word)}(?!{_TOKEN_CLASS})", re.I
    )


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

The :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS`, with or without their ``[bot]`` suffix,
are literals too (0.11): kept as they are, so a needle that is one of
their words (the ``github`` organisation owning the Actions app) is not
judged inside ``github-actions[bot]``, nor replaced there. Public forge
documentation hosts (``docs.github.com``, ``docs.gitlab.com``, matched
case-insensitively since 0.14) are literals too (0.13), so keeping
documentation links in text does not collide with the ``github.com`` /
``gitlab.com`` host values regardless of host casing.

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

_MENTION_BEFORE_BLOCK_CLASS = _char_class(_ASCII_ALNUM + "_.@/+-:${}")
"""Characters that may not immediately precede ``@<handle>`` in :data:`MENTION_RE` (0.16; 0.20: the userinfo characters ``:${}``, so ``${TOKEN}@host`` is a credential, not a mention)."""

_MENTION_HANDLE_CLASS = _char_class(_ASCII_ALNUM + "_-")
"""Characters allowed inside a handle matched by :data:`MENTION_RE` (0.16)."""

_MENTION_DOT_AFTER_CLASS = _char_class(_ASCII_ALNUM + "_")
"""Characters after ``.`` that mark a dotted identifier such as ``@pytest.fixture`` (0.16)."""

_MENTION_SLASH_AFTER_CLASS = _char_class(_ASCII_ALNUM + "_.-")
"""Characters after ``/`` that mark a scoped package such as ``@types/node`` (0.16)."""

_LETTER_OR_DIGIT = r"[^\W_]"
"""A letter or a digit of any script (PR #319 round 7): a handle is read on Unicode letters, never on its ASCII head."""

_MENTION_END = (
    rf"(?!{_MENTION_HANDLE_CLASS}|{_LETTER_OR_DIGIT}|\.(?:{_MENTION_DOT_AFTER_CLASS}|{_LETTER_OR_DIGIT})"
    rf"|/(?:{_MENTION_SLASH_AFTER_CLASS}|{_LETTER_OR_DIGIT})|\()"
)
"""What may not follow a handle: a handle character or any letter or digit, a dotted name (``@pytest.fixture``), a scoped package (``@types/node``) or a call."""

MENTION_RE = re.compile(
    rf"(?<!{_MENTION_BEFORE_BLOCK_CLASS})(?<!{_LETTER_OR_DIGIT})"
    rf"@({_LETTER_OR_DIGIT}(?:(?:{_MENTION_HANDLE_CLASS}|{_LETTER_OR_DIGIT})*{_LETTER_OR_DIGIT})?)"
    rf"{_MENTION_END}",
    re.I,
)
"""The finder of unknown ``@<handle>`` mentions in free text (SP-RECORDER-REDACTS-BY-CLASS D2, 0.9).

A finder, not a boundary (0.10): it decides which unknown words become
a role, and nothing is kept or refused by it. Not an email
(``a@b.example``), not a dotted name (``@pytest.fixture``, and so not a
dotted GitLab username either), not a decorator call (``@lru_cache(…)``);
a sentence-ending ``.`` is not part of the handle. The handle is read on
the letters and digits of every script (PR #319 round 7): ``@müller`` is
the handle ``müller``, never ``m`` followed by text, so it becomes one
role and is replaced whole. Once a login is a role it is replaced by
:func:`mention_pattern` or :func:`token_pattern`, whose boundary is
:data:`TOKEN_CHARS`.
"""


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
    """The ``@<login>`` form of ``login``: :func:`token_pattern` of ``@`` and the login (I1, 0.10).

    The handle ends where :data:`MENTION_RE` ends it (PR #319 round 7): never before a letter or
    a digit of any script, so ``@m`` is not replaced inside ``@müller``, which is another handle.
    """
    bounded = token_pattern(f"@{login}")
    return re.compile(f"{bounded.pattern}(?!{_LETTER_OR_DIGIT})", bounded.flags)


RECORDED_SOURCE_RE = re.compile(r"^recorded ([0-9]{4}-[0-9]{2}-[0-9]{2}) from (?!a live )[^\s;]+")
"""A recorded fixture's ``source`` head naming where it was recorded from (a folder, a repository), not yet the forge."""


def recorded_source(date: str, forge: str) -> str:
    """A recorded fixture's ``source`` head: the date and the forge, never a checkout's folder name.

    Args:
        date: The recording day, ``YYYY-MM-DD``.
        forge: ``github`` or ``gitlab``.

    Returns:
        ``recorded <date> from a live <forge> change request``.
    """
    return f"recorded {date} from a live {forge} change request"


def mentions(payload: object) -> list[str]:
    """Every unclassified ``@<handle>`` of a :data:`PROSE_KEYS` value, in order of first appearance.

    A diff, a log or any other text outside those keys is the repository's
    or the runner's content, not a person addressing another, and is not
    searched. Inside them, a Markdown code span or fenced block is code,
    a :data:`LANGUAGE_AT_WORDS` word (case-insensitive: a Python
    decorator, a CSS at-rule, a documentation tag, a Java, Kotlin, Swift
    or Dart annotation, 0.13, 0.18, 0.20) is code, a scoped package
    ``@scope/pkg`` is a package
    name, and a :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` stem (``@github-actions``) names
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
                            handle.casefold() in LANGUAGE_AT_WORDS
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

_RFC3986_USERINFO_CHARSET = _RFC3986_UNRESERVED_CHARS + "%" + _RFC3986_SUB_DELIM_CHARS + ":{}"
"""Exact RFC 3986 Section 3.2.1 userinfo character set (unreserved, pct-encoded ``%``, sub-delims and ``:``; 0.16 R9-D1)."""

_RFC3986_USERINFO_CLASS = _char_class(_RFC3986_USERINFO_CHARSET)
"""Compiled character class for :data:`_RFC3986_USERINFO_CHARSET`, built via :func:`_char_class` (0.16 R9-D1)."""

_URL_USERINFO_PREFIX = rf"(?:(?:{_RFC3986_USERINFO_CLASS}*@)+)?"
"""Optional URL userinfo prefix covering empty passwords, RFC 3986 userinfo characters and embedded ``@`` (0.15 R8-D3, 0.16 R9-D1)."""

_URL_BODY_DELIMITERS = "\"'<>()[]{}\\,`*|"
"""Delimiters excluded from a non-parenthesised URL body character in free text (0.16)."""

_URL_BODY_CHAR = _char_class(_URL_BODY_DELIMITERS, negated=True, extra_escapes=(r"\s",))
"""One non-delimiter character of a URL body in free text (0.15 Pref 2, 0.16)."""


_URL_IPV6_HOST = r"\[[0-9A-Za-z:.%_~-]+\]"
"""A bracketed IPv6 host, zone id included, right after the userinfo: the one place ``[`` belongs in a URL (0.20)."""

_URL_BODY = rf"(?:{_URL_BODY_CHAR}|\((?:{_URL_BODY_CHAR}|\({_URL_BODY_CHAR}*\))*\))"
"""One body character of a text URL, or a GFM balanced parenthesis group."""

_URL_IN_TEXT_RE = re.compile(
    rf"(?:https?|ssh|git|ftps?)://{_URL_USERINFO_PREFIX}"
    rf"(?:{_URL_IPV6_HOST}{_URL_BODY}*|{_URL_BODY}+)",
    re.I,
)
"""A URL inside free text (a log line, a ``glab`` error, a clone command), any case, with RFC 3986 userinfo, a bracketed IPv6 host and GFM balanced parentheses (I2, 0.10; 0.13; 0.15 R8-D3, Pref 2; 0.20).

Without the bracketed host an IPv6 URL was never a text URL (``[`` is a
body delimiter), so only the token replacement rewrote it, keeping its
port and the rest of its path, and the offline transform then rewrote
that again: the fixture was not a fixed point. One rule now: a
rewritten URL carries the placeholder host and no port; a kept one is
byte-identical but for its scheme and host case.
"""

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


FRAGMENT_ANCHOR_RE = re.compile(
    r"^(?:issuecomment-|note_|discussion_r|pullrequestreview-|discussion_)[0-9a-f]+$", re.I
)
"""The fragments a forge URL may keep (0.10): a comment's, a note's or a review's anchor, which a reader parses.

Every other fragment, and every query, is dropped by :func:`redact_url`:
either can carry a login (``?author=<login>``, ``#<login>``), and no
reader consumes one.
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

_API_RESOURCES: tuple[str, ...] = ("repos", "users", "orgs", "apps", "groups")
"""First path segments of a forge route that name an owner next: ``repos/<o>/<r>``, ``users/<u>``, GitLab's ``groups/<g>`` (0.21)."""

_IDENTITY_SLOTS: dict[str, tuple[str, str]] = {
    "users": ("repos", PLACEHOLDER_PROJECT),
    "projects": ("repos", PLACEHOLDER_PROJECT),
    "orgs": ("teams", PLACEHOLDER_NAMESPACE),
}
"""After an owner root, the route word that opens the grammar's next identity slot and the placeholder written there (0.22).

Bitbucket Server's ``users/<u>/repos/<r>`` and ``projects/<KEY>/repos/<r>``
name a repository; GitHub's ``orgs/<o>/teams/<t>`` names a team, a
namespace of people.
"""


_IDE_ROUTES: frozenset[str] = frozenset({"edit", "blob", "tree", "merge_requests"})
"""The words that end the project path of a GitLab Web IDE route, ``-/ide/project/<path>/<word>/…`` (0.22)."""

_TARBALL_VERSION_RE = re.compile(r"^[0-9][0-9a-z.+-]*$", re.I)
"""The version of an npm tarball name, ``<pkg>-<version>.tgz``: a digit first, then digits, letters, dots, ``+`` and ``-`` (0.22)."""


def _npm_registry(core: list[str]) -> int | None:
    """The index of the first segment after ``packages/npm`` in a GitLab API path, else ``None`` (0.23).

    GitLab's npm registry is ``api/v<N>/packages/npm/…`` on the instance,
    ``api/v<N>/projects/<id>/packages/npm/…`` and
    ``api/v<N>/groups/<id>/-/packages/npm/…``. What follows names a
    package and no one (0.24): an ``@<scope>`` there is as often a
    forwarded public one (``@types``, ``@babel``) as a group, so it is
    ordinary text (NG4-15) and never learnt.
    """
    if len(core) < 4 or core[0].casefold() != "api" or not _API_VERSION_RE.match(core[1]):
        return None
    for i in range(2, len(core) - 1):
        if core[i].casefold() == "packages" and core[i + 1].casefold() == "npm":
            return i + 2
    return None


def _ide_route(core: list[str]) -> bool:
    """Whether ``core`` is a GitLab Web IDE route, ``-/ide/project/<path>/…`` (0.22)."""
    return (
        len(core) > 3
        and core[0] == "-"
        and core[1].casefold() == "ide"
        and core[2].casefold() == "project"
    )


def _project_root(core: list[str]) -> bool:
    """Whether ``core`` is a Bitbucket Server project route, ``projects/<KEY>/repos/<r>/…`` (0.22)."""
    return core[0].casefold() == "projects" and len(core) > 3 and core[2].casefold() == "repos"


def _package_tarball(core: list[str], dash: int) -> bool:
    """Whether the ``-`` at ``dash`` separates an npm package from its tarball, ``<pkg>/-/<pkg>-<version>.tgz`` (0.22).

    The grammar of every npm-compatible registry — npmjs, yarnpkg,
    npmmirror, a private ``npm.<corp>``, a Nexus ``repository/<repo>/``
    prefix — scoped (``@scope/<pkg>/-/<pkg>-<version>.tgz``) or not: the
    package names no one, so it is neither a namespace nor a repository.
    """
    if dash < 1 or dash + 1 >= len(core):
        return False
    package, tarball = core[dash - 1], core[dash + 1]
    if not tarball.startswith(package + "-") or not tarball.endswith(".tgz"):
        return False
    return _TARBALL_VERSION_RE.match(tarball[len(package) + 1 : -4]) is not None


def path_identity(segments: list[str], *, remote: bool = False) -> PathIdentity:
    """Split a forge path's segments into its placeholder form, its namespace and its repository.

    The one reading of a forge path: :func:`redact_url` writes
    :attr:`PathIdentity.segments`, and :func:`known_values` takes the raw
    namespace and repository as segment values (0.10), so the two cannot
    disagree on which segment named someone. Empty segments from double
    slashes (``org//repo``, ``repos//o/r``, ``users//u``) are collapsed
    so they never shift or leak a real segment (0.12, D4); a trailing
    slash is preserved. Everything before a GitLab ``/-/`` separator is
    an identity (0.21, B1): one segment is a namespace (``<group>/-/issues``,
    ``<user>/-/snippets``), two or more are the namespace and the
    repository, and after an owner root (``groups/<g>/<sub>/-/epics``)
    every segment up to the separator is the namespace. The grammars are
    a table (0.22): a reserved route word (:data:`RESERVED_ROUTE_WORDS`)
    is never an identity, a Web IDE route reads its project path
    (``-/ide/project/<ns>/<repo>/edit/…``), an owner root reads the
    identity slots of its grammar (:data:`_IDENTITY_SLOTS`:
    ``users/<u>/repos/<r>``, ``orgs/<o>/teams/<t>``), a package
    tarball before a ``-`` (``<pkg>/-/<pkg>-<version>.tgz``) names no one,
    and what follows ``packages/npm/`` of GitLab's npm registry
    (``api/v4/…/packages/npm/``, :func:`_npm_registry`) names no one, its
    ``@<scope>`` included (0.24: never learnt, NG4-15). The route grammars are
    those of a URL path; a ``remote`` path names a repository by
    construction (``<ns>/…/<repo>``, 0.23) and reads that way whatever its
    first word, so a recorded remote whose namespace is a reserved word
    (``jwt``, ``assets``, real GitHub accounts) still yields its identity.

    Args:
        segments: The path's segments, split on ``/``.
        remote: The path is a remote's or an argv API path's: the namespace is every segment but
            the last, whatever the words.

    Returns:
        The placeholder segments, the namespace and the repository the path names.
    """
    core = [s for s in segments if s]
    if not core:
        return PathIdentity(segments)
    trailing = [""] if segments[-1] == "" else []
    if remote:
        return _repository_path(core, trailing)
    if _ide_route(core):
        stop = next(
            (i for i in range(4, len(core)) if core[i].casefold() in _IDE_ROUTES), len(core)
        )
        project = core[3:stop]
        if len(project) == 1:
            return PathIdentity(
                [*core[:3], PLACEHOLDER_NAMESPACE, *core[stop:], *trailing], (project[0],)
            )
        return PathIdentity(
            [*core[:3], PLACEHOLDER_NAMESPACE, _project(project[-1]), *core[stop:], *trailing],
            tuple(project[:-1]),
            _bare(project[-1]),
        )
    npm = _npm_registry(core)
    if npm is not None:
        head = core[: npm - 2]
        prefix = (
            path_identity(head)
            if head[2:3] and head[2].casefold() in ("projects", "groups")
            else PathIdentity(head)
        )
        return PathIdentity(
            [*prefix.segments, *core[npm - 2 :], *trailing],
            prefix.namespace,
            prefix.repository,
        )
    if core[0].casefold() in RESERVED_ROUTE_WORDS:
        return PathIdentity([*core, *trailing])
    if (
        len(core) > 3
        and core[0].casefold() == "api"
        and _API_VERSION_RE.match(core[1])
        and core[2].casefold() in _API_RESOURCES
    ):
        inner = path_identity([*core[2:], *trailing])
        return PathIdentity([*core[:2], *inner.segments], inner.namespace, inner.repository)
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
    if core[0].casefold() == "repos" and len(core) >= 3:
        return PathIdentity(
            [core[0], PLACEHOLDER_NAMESPACE, _project(core[2]), *core[3:], *trailing],
            (core[1],),
            _bare(core[2]),
        )
    if (core[0].casefold() in _API_RESOURCES and len(core) >= 2) or _project_root(core):
        stop = core.index("-") if "-" in core else 2
        owner = core[1:stop]
        rest = core[stop:]
        namespace, repository = tuple(owner), None
        slot = _IDENTITY_SLOTS.get(core[0].casefold())
        if slot is not None and len(rest) > 1 and rest[0].casefold() == slot[0]:
            if slot[1] == PLACEHOLDER_PROJECT:
                repository = _bare(rest[1])
                rest = [rest[0], _project(rest[1]), *rest[2:]]
            else:
                namespace = (*namespace, rest[1])
                rest = [rest[0], slot[1], *rest[2:]]
        return PathIdentity(
            [core[0], *([PLACEHOLDER_NAMESPACE] if owner else []), *rest, *trailing],
            namespace,
            repository,
        )
    if core[0].casefold() == "projects" and len(core) >= 2 and core[1].isdigit():
        return PathIdentity([*core, *trailing])
    if "-" in core:
        dash = core.index("-")
        if _package_tarball(core, dash):
            return PathIdentity([*core, *trailing])
        if dash == 1:
            return PathIdentity([PLACEHOLDER_NAMESPACE, *core[1:], *trailing], (core[0],))
        return PathIdentity(
            [PLACEHOLDER_NAMESPACE, _project(core[dash - 1]), *core[dash:], *trailing],
            tuple(core[: dash - 1]),
            _bare(core[dash - 1]),
        )
    if len(core) < 3 or core[2] in _GITHUB_ROUTES:
        rest = core[1:]
        repository = None
        if rest:
            repository = _bare(rest[0])
            rest = [_project(rest[0]), *rest[1:]]
        return PathIdentity([PLACEHOLDER_NAMESPACE, *rest, *trailing], (core[0],), repository)
    return _repository_path(core, trailing)


def _repository_path(core: list[str], trailing: list[str]) -> PathIdentity:
    """The generic reading of a repository path: every segment but the last is the namespace, the last the repository (0.23).

    A lone segment is a namespace. The route grammars of
    :func:`path_identity` are not applied: this is the reading of a remote,
    and of a URL path no grammar claimed.
    """
    if len(core) == 1:
        return PathIdentity([PLACEHOLDER_NAMESPACE, *trailing], (core[0],))
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


_URL_HOST_NAME_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:xn--[a-z0-9-]+|[a-z][a-z0-9-]*[a-z0-9])\.?",
    re.I,
)
"""A dotted host name by DNS label grammar (RFC 1123 2.1): its last label an A-label or led by a letter."""


def _names_host_and_port(authority: str) -> bool:
    """Whether ``authority`` reads as ``<host>[:<port>]`` (RFC 3986 3.2.2, 3.2.3), the port ASCII digits.

    The host is a dotted name (:data:`_URL_HOST_NAME_RE`), an IPv4 address, a bracketed IPv6
    literal (zone id included) or a placeholder-class host such as ``localhost``. A one-label
    name other than a placeholder is not: followed by a ``/`` and an ``@`` it reads as a user and
    a password as well.
    """
    if authority.startswith("["):
        literal, bracket, tail = authority[1:].partition("]")
        try:
            ipaddress.IPv6Address(literal.partition("%")[0])
        except ValueError as exc:
            _log.debug("forge.recording.not_an_ipv6_host", error=type(exc).__name__)
            return False
        if not bracket or (tail and not tail.startswith(":")):
            return False
        port = tail[1:]
    else:
        name, _, port = authority.partition(":")
        if not name:
            return False
        try:
            ipaddress.IPv4Address(name)
        except ValueError as exc:
            _log.debug("forge.recording.not_an_ipv4_host", error=type(exc).__name__)
            if not (_URL_HOST_NAME_RE.fullmatch(name) or closure.is_placeholder_host(name)):
                return False
    return all(c in "0123456789" for c in port)


def userinfo_crosses_path(url: str) -> bool:
    """Whether a ``/``, ``?`` or ``#`` stands inside the userinfo of ``url`` (PR #319 round 8, 4688674758).

    The authority runs from ``<scheme>://`` to its first ``/``, ``?`` or ``#`` (RFC 3986 3.2).
    When what follows its last ``@`` (or the whole of it, when it holds none) is no
    ``<host>[:<port>]`` (:func:`_names_host_and_port`) and an ``@`` stands later in ``url``, the
    authority was cut inside a userinfo: ``redis://:Ab/cd@<host>`` names the host ``<host>`` with
    the password ``Ab/cd``, not an empty host with the port ``Ab``. Such a URL is never
    rewritten; it is left for the closure oracle, which refuses it. A path holding an ``@`` after
    a host (``https://www.npmjs.com/package/@scope/name``) is a path, and an empty authority
    (``file:///<path>``) holds no userinfo.

    Args:
        url: One URL, its scheme included.

    Returns:
        ``True`` when the userinfo crosses the path, query or fragment delimiter.
    """
    _, sep, rest = url.partition("://")
    end = next((i for i, c in enumerate(rest) if c in "/?#"), len(rest))
    if not sep or end == 0 or "@" not in rest[end:]:
        return False
    return not _names_host_and_port(rest[:end].rpartition("@")[2])


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
    :data:`FRAGMENT_ANCHOR_RE` anchor (0.10). A URL whose userinfo
    crosses a ``/``, ``?`` or ``#`` (:func:`userinfo_crosses_path`) is
    returned as it stands, never rewritten: the closure oracle refuses
    the recording (PR #319 round 8, 4688674758).

    Returns:
        The redacted URL, ``value`` itself when its userinfo crosses its
        path, or ``""`` when ``value`` is not a URL of
        :data:`URL_SCHEMES` with a host (the caller drops the key).
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        return ""
    if parts.scheme not in URL_SCHEMES or not parts.hostname:
        return ""
    if userinfo_crosses_path(value.strip()):
        return value
    path = parts.path
    if path.startswith("/") and len(path) > 1:
        path = "/" + "/".join(_namespaced(path[1:].split("/")))
    host = PLACEHOLDER_HOST
    if parts.scheme in ("ssh", "git") and "@" in parts.netloc:
        host = f"git@{PLACEHOLDER_HOST}"
    fragment = parts.fragment if FRAGMENT_ANCHOR_RE.match(parts.fragment) else ""
    return urlunsplit((parts.scheme, host, path, "", fragment))


def _is_allowed_host(host: str) -> bool:
    """Whether ``host`` is allowed in free text: a placeholder-class or public host (:mod:`repoach.forge.closure`)."""
    return closure.is_placeholder_host(host) or closure.is_public_host(host)


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
    return closure.is_placeholder_host(host) and host.casefold() != PLACEHOLDER_HOST


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


def _identity_survives(host: str, path: str, rewritten: str) -> bool:
    """Whether an identity a tenant route names on the public ``host`` is still in ``rewritten``.

    The forge route grammar places a repository's owner, not an npm registry's CouchDB user
    (``-/user/org.couchdb.user:<login>``): such a URL is left as it stands, and the closure
    oracle refuses it as a ``tenant path`` (PR #319 round 7, 4687852992), never written.
    """
    folded = rewritten.casefold()
    return any(
        identity.casefold() in folded
        for form in text_forms.decoded_forms(path)
        for identity in closure.tenant_identities(f"{host.casefold()}/{form.lstrip('/')}")
    )


def redact_urls_in_text(text: str) -> str:
    """Redact every URL, scp-style remote and forge noreply mailbox of ``text`` not on a kept host.

    A URL goes through :func:`redact_url`, with trailing prose punctuation
    (``.,:;!?)]}>'"``) and boundary Markdown delimiters (`` ` ``, ``*``,
    ``|``, ``~``, ``_``) preserved outside the URL without being eaten
    (0.13 R6-D1, 0.14 R7-D2). A protocol-relative ``//host/path`` is not
    rewritten by shape: its host, namespace and repository are known values
    (D7) and its path is kept (0.17). An scp remote
    ``<user>@<host>:<ns>/<repo>`` is normalised through it too (0.10): read
    as ``ssh://<user>@<host>/<ns>/<repo>``, redacted, and written back as
    ``git@forge.example.com:org/project``, its ``.git`` suffix kept and its
    user never. A URL on :data:`PLACEHOLDER_HOST` is redacted again, so a
    hand-written one gets the placeholder path too. A URL carrying userinfo
    (``user[:pass]@``) is always redacted through :func:`redact_url`,
    whatever the host — including :func:`repoach.forge.closure.is_public_host` hosts and RFC
    2606 hosts (0.14 R7-D1). Text URLs on public hosts
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
        if userinfo_crosses_path(url):
            return match.group(0)
        if "@" in parts.netloc:
            return (redact_url(url) or url) + tail
        target = f"{parts.path}?{parts.query}" if parts.query else parts.path
        if _kept_host(host) and not closure.is_tenant_path(host, target):
            return _normalize_kept_url(parts) + tail
        rewritten = redact_url(url) or url
        if _kept_host(host) and _identity_survives(host, target, rewritten):
            return match.group(0)
        return rewritten + tail

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

    with_urls = _URL_IN_TEXT_RE.sub(_one, text)
    step = _NOREPLY_MAILBOX_RE.sub(_noreply, _SCP_IN_TEXT_RE.sub(_scp, with_urls))
    return _SYSTEM_NOREPLY_RE.sub(_system_noreply, step)


def redact_urls_in_value(value: Any) -> Any:
    """Redact URLs in a decoded value whose keys are kept (a fixture's ``result``).

    A string under a :data:`URL_KEYS` key goes through :func:`redact_url`
    (kept as is when it is not a URL); every other string through
    :func:`redact_urls_in_text`.
    """
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key in URL_KEYS and isinstance(item, str) and item:
                out[key] = redact_url(item) or redact_urls_in_text(item)
            else:
                out[key] = redact_urls_in_value(item)
        return out
    if isinstance(value, list):
        return [redact_urls_in_value(v) for v in value]
    if isinstance(value, str):
        return redact_urls_in_text(value)
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
    """Keep only :data:`repoach.forge.fixture_reading.RECORDED_FIELDS` keys, recursively, and redact by class.

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


def outside_literals(text: str, rewrite: Callable[[str], str]) -> str:
    """``text`` with ``rewrite`` applied to every stretch between :data:`CLI_PLACEHOLDER_RE` literals, which stay as they are.

    Args:
        text: The text rewritten.
        rewrite: The rewrite applied to each stretch.

    Returns:
        ``text`` rewritten, its literals unchanged.
    """
    out: list[str] = []
    last = 0
    for match in CLI_PLACEHOLDER_RE.finditer(text):
        out += [rewrite(text[last : match.start()]), match.group(0)]
        last = match.end()
    out.append(rewrite(text[last:]))
    return "".join(out)


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

    Args:
        text: Any string of a recording.
        roles: Login -> class word for the recording.

    Returns:
        ``text`` with each login replaced.
    """
    expanded = dict(roles)
    present = {k.removeprefix(MENTION_ONLY_PREFIX).casefold() for k in roles}
    for login, word in roles.items():
        mention_stem = bot_mention_stem(login)
        if mention_stem is not None and mention_stem.casefold() not in present:
            expanded[MENTION_ONLY_PREFIX + mention_stem] = word
    rules: list[tuple[re.Pattern[str], str]] = []
    for login in sorted(expanded, key=len, reverse=True):
        if login.startswith(MENTION_ONLY_PREFIX):
            rules.append(
                (mention_pattern(login.removeprefix(MENTION_ONLY_PREFIX)), f"@{expanded[login]}")
            )
        else:
            rules.append((_login_pattern(login), expanded[login]))

    def rewrite(piece: str) -> str:
        for pattern, word in rules:
            piece = pattern.sub(word, piece)
        return piece

    return outside_literals(text, rewrite)


def classify_logins(payload: Any, roles: dict[str, str]) -> Any:
    """Replace every login in ``payload`` by its class word, recursively.

    A string under a :data:`repoach.forge.fixture_reading.LOGIN_KEYS` key is looked up in ``roles``
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


SYNTHETIC_PROJECT_ID_START = 2001
"""The first synthetic repository id (Q2, 0.10): a project id names a project as a user id names an account."""

SYNTHETIC_PROJECT_ID_END = 2999
"""The last synthetic repository id (0.10)."""


class SyntheticIdsExhaustedError(Exception):
    """A map would hand out a number past the end of its synthetic range."""


def is_synthetic_number(
    value: object, low: int = SYNTHETIC_ID_START, high: int = SYNTHETIC_ID_END
) -> bool:
    """Whether ``value`` is an ``int`` in ``low..high`` (by default the user range).

    Args:
        value: A decoded JSON value.
        low: The first number of the range.
        high: The last number of the range.

    Returns:
        ``True`` for an ``int`` (never a ``bool``) inside the range.
    """
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def trusted_declaration(
    raw: object, low: int = SYNTHETIC_ID_START, high: int = SYNTHETIC_ID_END
) -> frozenset[int]:
    """The numbers of a declaration that may be kept: the ones in ``low..high`` only.

    Args:
        raw: A fixture's declaration, as committed.
        low: The first number of the range.
        high: The last number of the range.

    Returns:
        The declared numbers inside the range, empty when ``raw`` is not a list.
    """
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
    """The sorted account numbers of every user id ``node`` holds, the value of :data:`repoach.forge.fixture_reading.SYNTHETIC_IDS_KEY`."""
    numbers = {user_id_key(i) for i in user_id_values(node)}
    return sorted(n for n in numbers if isinstance(n, int))


def is_repository_object(node: object) -> bool:
    """Whether ``node`` is a repository object: a dict whose URL names a repository and nothing under it."""
    return isinstance(node, dict) and _repository_of(node) is not None


def project_id_values(node: object) -> list[object]:
    """Every repository id of a decoded value: repository objects and ``projects/<n>`` tokens of five digits or more (D9, 0.17), JSON inside strings included."""
    if isinstance(node, str):
        if node.lstrip()[:1] in ("{", "["):
            decoded = decode_json_values(node)
            if decoded is not None:
                return [i for v in decoded for i in project_id_values(v)]
        return [int(m.group("id")) for m in _PROJECT_ID_RE.finditer(node)]
    if isinstance(node, dict):
        found = [i for v in node.values() for i in project_id_values(v)]
        if is_repository_object(node) and "id" in node:
            found.append(node["id"])
        return found
    if isinstance(node, list):
        return [i for v in node for i in project_id_values(v)]
    return []


def declared_project_ids(node: object) -> list[int]:
    """The sorted repository ids ``node`` holds, the value of :data:`repoach.forge.fixture_reading.SYNTHETIC_PROJECT_IDS_KEY` (0.10)."""
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
        """Remap the ``id`` of every user object (one holding ``login`` or ``username``).

        Args:
            payload: A decoded JSON value.
            forge: ``github`` or ``gitlab``.
            keep: Declared synthetic numbers, kept as they are.

        Returns:
            A copy with each user object's ``id`` synthetic.
        """
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
        self, payload: Any, *, forge: str, keep: frozenset[int] = frozenset()
    ) -> Any:
        """Remap the ``id`` of every repository object and of every ``api/v4/projects/<id>`` path.

        Since 0.10 and 0.13 (R6-D5).

        Args:
            payload: A decoded JSON value, argv or a text.
            forge: ``github`` or ``gitlab``.
            keep: Declared synthetic numbers, kept as they are.

        Returns:
            A copy with each repository id synthetic.
        """
        if isinstance(payload, dict):
            repository = is_repository_object(payload)
            return {
                k: self.synthetic(v, forge=forge, keep=keep)
                if repository and k == "id"
                else self.apply_projects(v, forge=forge, keep=keep)
                for k, v in payload.items()
            }
        if isinstance(payload, list):
            return [self.apply_projects(v, forge=forge, keep=keep) for v in payload]
        if isinstance(payload, str):
            return _PROJECT_ID_RE.sub(
                lambda m: (
                    m.group(0)[: m.start("id") - m.start()]
                    + str(self.synthetic(int(m.group("id")), forge=forge, keep=keep))
                ),
                payload,
            )
        return payload

    def apply_result(
        self, result: object, *, forge: str, method: str, keep: frozenset[int] = frozenset()
    ) -> object:
        """Remap the user ids of a recorded result, at :data:`RESULT_USER_ID_METHODS` positions only.

        Every other ``id`` of a result (a review, a comment, a thread, a
        check run) is the object's own and is left as it is, even when its
        number equals a user id seen elsewhere.

        Args:
            result: The recorded result.
            forge: ``github`` or ``gitlab``.
            method: The recorded protocol method.
            keep: Declared synthetic numbers, kept as they are.

        Returns:
            The result, its user ids synthetic where the method's result holds one.
        """
        if (forge, method) not in RESULT_USER_ID_METHODS or not isinstance(result, list):
            return result
        return [
            {**item, "id": self.synthetic(item["id"], forge=forge, keep=keep)}
            if isinstance(item, dict) and "id" in item
            else item
            for item in result
        ]


def login_sightings(values: Iterable[object]) -> list[tuple[str, bool]]:
    """Every login ``values`` name, in order: ``(login, False)`` under a login key, ``(login, True)`` as a mention.

    Args:
        values: Decoded JSON values (recorded answers, a fixture's nodes), read in order; each
            gives its :func:`repoach.forge.fixture_reading.login_values`, then its :func:`mentions`.

    Returns:
        The sightings, repeats kept; :func:`assign_roles` reads the first appearance of each.
    """
    found: list[tuple[str, bool]] = []
    for value in values:
        found += [(login, False) for _path, login in login_values(value)]
        found += [(login, True) for login in mentions(value)]
    return found


def _class_word(kind: str, n: int) -> str:
    """The class word of the ``n``-th account of ``kind``: ``author``, then ``author-2``."""
    return kind if n == 1 else f"{kind}-{n}"


def _nothing_held(_word: str) -> bool:
    """No class word is taken yet: a live recording starts from a blank page."""
    return False


def assign_roles(
    sightings: Iterable[tuple[str, bool]],
    *,
    bot: str = "",
    maintainer: str | None = None,
    author: str | None = None,
    held: Callable[[str], bool] = _nothing_held,
) -> dict[str, str]:
    """Login → class word for one recording: the one role builder, live and offline (D2, D4).

    Precedence: the configured ``bot`` (``bot``), the ``is_maintainer`` login (``maintainer``),
    the change request's ``author`` (``author``); then every further login of ``sightings`` in
    order of first appearance, compared casefolded as forges compare logins: the next
    ``bot-<n>`` for a ``[bot]`` login, the next ``service`` for a GitLab project bot and the next
    ``author-<n>`` otherwise. A login of :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` or already a class word is
    kept. A login seen only as a mention is keyed ``@<login>`` (:data:`MENTION_ONLY_PREFIX`), so
    only its ``@<handle>`` form is replaced, unless ``<login>[bot]`` is a login of the recording:
    then the mention is that bot's stem and shares its word when :func:`bot_stem` reads one,
    and names no one otherwise (``@ci`` beside ``ci[bot]``). Words are handed out only once
    every sighting is read, so a stem met before its bot takes no word of its own.

    Args:
        sightings: ``(login, is_mention)`` in recording order (:func:`login_sightings`).
        bot: The configured bot login, ``""`` when none.
        maintainer: The login an ``is_maintainer`` call asks about, if any.
        author: The change request's author, if known; ``author`` is spent on it either way.
        held: Whether a class word is already taken: the offline transform passes the words its
            fixture holds (and ``author``, its change request's), so a new login never merges
            with one.

    Returns:
        Each login (a mention-only one keyed ``@<login>``) mapped to its class word.
    """
    roles: dict[str, str] = {}
    following = {"bot": 1, "author": 1, "service": 1}

    def _next(kind: str) -> str:
        n = next(n for n in itertools.count(following[kind]) if not held(_class_word(kind, n)))
        following[kind] = n + 1
        return _class_word(kind, n)

    def _key_of(login: str) -> str | None:
        folded = login.casefold()
        return next(
            (k for k in roles if k.removeprefix(MENTION_ONLY_PREFIX).casefold() == folded), None
        )

    if bot and not is_classified_login(bot):
        roles[bot] = _next("bot")
    if maintainer and _key_of(maintainer) is None and not is_classified_login(maintainer):
        roles[maintainer] = "maintainer"
    if author:
        word = _next("author")
        if _key_of(author) is None and not is_classified_login(author):
            roles[author] = word
    spelled: dict[str, str] = {}
    mention_only: dict[str, bool] = {}
    for login, mention in sightings:
        if not login or is_classified_login(login):
            continue
        first = spelled.setdefault(login.casefold(), login)
        mention_only[first] = mention_only.get(first, True) and mention
    bots = {
        login.casefold()[:-5]: login
        for login in [*roles, *mention_only]
        if login.casefold().endswith("[bot]")
    }
    stems: dict[str, str] = {}
    for login in [k for k, only in mention_only.items() if only and k.casefold() in bots]:
        del mention_only[login]
        if bot_stem(bots[login.casefold()]) is not None:
            stems[login] = bots[login.casefold()]
    for login, only in mention_only.items():
        if _key_of(login) is not None:
            continue
        if GITLAB_PROJECT_BOT_RE.match(login):
            kind = "service"
        else:
            kind = "bot" if login.casefold().endswith("[bot]") else "author"
        roles[MENTION_ONLY_PREFIX + login if only else login] = _next(kind)
    for stem, bot_login in stems.items():
        bot_key = _key_of(bot_login)
        if bot_key is not None and _key_of(stem) is None:
            roles[stem] = roles[bot_key]
    return roles


def redact_stdout(text: str) -> str:
    """Redact a recorded stdout: JSON answers keep only :data:`repoach.forge.fixture_reading.RECORDED_FIELDS`.

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
        found += secret_keys(value)
    return found


NEEDLE_MIN_LENGTH = 4
"""A needle shorter than this would match everywhere; it is a configuration error (exit 2), never a skip."""

FORGE_HOSTS: frozenset[str] = frozenset({"github.com", "api.github.com", "gitlab.com"})
"""The public forges' own hosts, known values of every recording (I3, 0.10)."""

PUBLIC_FORGE_DOMAINS: tuple[str, ...] = ("github.com", "gitlab.com", "githubusercontent.com")
"""A host under one of these is a public forge's (0.11): a project there is the recorded one or a third party's."""


class NeedleError(Exception):
    """A needle the oracle cannot judge (too short, or a class word or placeholder); the message names its kind and ordinal."""


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
    named = tuple(
        p
        for p in parts
        if p
        and not CLI_PLACEHOLDER_RE.fullmatch(p)
        and not p.isdigit()
        and not (p.startswith("{") and p.endswith("}"))
        and p.casefold() not in _PLACEHOLDERS
    )
    return named if len(named) == len([p for p in parts if p]) and len(named) >= 2 else ()


def _instance_host(raw: object) -> str | None:
    """The first host of a :data:`URL_KEYS` value in ``raw`` that is neither a placeholder nor public: the recorded instance."""
    for node in _walk_dicts(raw):
        for key, item in node.items():
            if key in URL_KEYS and isinstance(item, str):
                try:
                    parts = urlsplit(item)
                except ValueError as exc:
                    _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
                    continue
                host = parts.hostname or ""
                if parts.scheme in URL_SCHEMES and host and not _is_allowed_host(host):
                    return host
    return None


def raw_logins(raw: object) -> list[str]:
    """Every login of the raw answers, under any key the redaction may drop (0.11, R2).

    ``assignees[].login`` and ``mergedBy.login`` are not recorded fields,
    so a login known only there never reaches ``roles``; read from the
    raw decoded stdout, it is a needle and a role all the same. A class
    word or a :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` entry is not one.
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
    """Whether a login or path segment is a :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` entry, ``[bot]`` suffix or not (D2)."""
    folded = value.casefold()
    return any(
        folded in (n.casefold(), n.casefold().removesuffix("[bot]")) for n in NON_PERSON_LOGINS
    )


_PLACEHOLDERS: frozenset[str] = frozenset(
    {PLACEHOLDER_HOST, PLACEHOLDER_NAMESPACE, PLACEHOLDER_PROJECT}
)
"""The placeholders the redaction writes; a needle equal to one could never be told from it."""


_PROJECT_ID_RE = re.compile(r"(?<![A-Za-z0-9])projects/+(?P<id>[0-9]{5,})(?![A-Za-z0-9])", re.I)
"""A ``projects/<n>`` token with ``n`` of at least five digits, in any string (D9, 0.17)."""

_NAME_WORD_RE = re.compile(r"[^\W_]+")
"""A word of a person's name: letters and digits."""

_MAILBOX_KEYS: frozenset[str] = frozenset(
    {"email", "public_email", "commit_email", "author_email", "committer_email"}
)
"""User-object keys holding a mailbox, read into the known values before the keys are dropped (D1)."""

_NAME_KEYS: tuple[str, ...] = ("name", "author_name", "committer_name")
"""Keys holding a person's name beside a mailbox: a user object's, a GitLab commit object's (D5)."""

_ENCODED_PROJECT_RE = re.compile(
    r"projects/+(?P<path>[A-Za-z0-9._-]+(?:%2[Ff][A-Za-z0-9._-]+)+)", re.I
)
"""A ``projects/<group>%2F<repo>`` token: the one URL-encoded shape a forge's own argv carries (D7 source 3)."""

_PATH_END = "\"'<>()[]{}\\,`*|?# \t\r\n"
"""Characters that end the path read after a host (D7 source 3)."""

_API_ROOTS: frozenset[str] = frozenset({"api", "repos", "users", "orgs", "apps", "groups"})
"""First path segments whose identity needs more than two segments (0.21: GitLab's ``groups``)."""


def _literals() -> tuple[str, ...]:
    """The tool and project literals the oracle blanks: CLI templates, documentation hosts, spec ids, non-person logins."""
    named = [":owner", ":repo", ":id", "/dev/null", "docs.github.com", "docs.gitlab.com"]
    for login in NON_PERSON_LOGINS:
        named += [login, login.removesuffix("[bot]")]
    return (*named, *sorted(SPEC_IDS))


def _id_encodings(number: int) -> list[str]:
    """Every string a user account number may stand as: digits, legacy and current node ids."""
    legacy = base64.b64encode(f"04:User{number}".encode()).decode()
    forms = [str(number), legacy, legacy.rstrip("=")]
    packed: list[bytes] = []
    if number < 0x80:
        packed.append(bytes([number]))
    for marker, width in sorted(_MSGPACK_UINT_WIDTH.items()):
        if number < 1 << (8 * width):
            packed.append(bytes([marker]) + number.to_bytes(width, "big"))
    for body in packed:
        forms.append("U_" + base64.urlsafe_b64encode(b"\x92\x00" + body).decode().rstrip("="))
    return forms


def _walk_dicts(node: object) -> list[dict[str, object]]:
    """Every dict of a decoded value, JSON inside strings included."""
    if isinstance(node, str):
        if node.lstrip()[:1] not in ("{", "["):
            return []
        return [d for v in decode_json_values(node) or [] for d in _walk_dicts(v)]
    if isinstance(node, dict):
        return [node] + [d for v in node.values() for d in _walk_dicts(v)]
    if isinstance(node, list):
        return [d for v in node for d in _walk_dicts(v)]
    return []


def _argv_strings(node: object) -> list[str]:
    """Every argv string of the recorded calls: the ``args`` of an exchange (``args`` and ``stdout``), never a string's decoded JSON (0.25), never one the call declares prose (0.28, :func:`_declared_prose`)."""
    if isinstance(node, dict):
        found = [d for v in node.values() for d in _argv_strings(v)]
        argv = node.get("args")
        if "stdout" in node and isinstance(argv, list):
            prose, _stdin = _declared_prose(argv)
            found += [a for i, a in enumerate(argv) if isinstance(a, str) and i not in prose]
        return found
    if isinstance(node, list):
        return [d for v in node for d in _argv_strings(v)]
    return []


_API_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.~%/-])/?(?:api/v[0-9]+/+)?repos/+(?P<owner>[A-Za-z0-9_.-]+)/+(?P<repo>[A-Za-z0-9_.-]+)",
    re.I,
)
"""A GitHub-form API path, ``[/][api/v<N>/]repos/<owner>/<repo>``, in any case, not inside a longer path or a URL (0.26).

GitHub Enterprise serves it under ``api/v3/``, Gitea under ``api/v1/``.
"""

_API_ROUTE_END = "/" + _PATH_END
"""Characters that end the route word read after ``repos/<owner>/<repo>/`` (0.26)."""


def _api_path_projects(text: str) -> list[tuple[str, str]]:
    """The ``(owner, repository)`` each GitHub API path of a structured string names (0.26, 0.28).

    ``[/][api/v<N>/]repos/<owner>/<repo>/<word>…`` names a repository only
    when ``<word>`` is a sub-resource of GitHub's REST repository
    endpoints (:data:`GITHUB_REPO_API_ROUTES`); a bare
    ``repos/<owner>/<repo>`` and any other word (``main.py``, ``src``) is
    ordinary text. Whatever follows the path is not read: a structured
    string is never judged prose by its wording (0.28, the owner's
    decision withdrawing the 0.27 sentence rule); prose is the channel a
    call declares (:func:`_declared_prose`, :data:`_PROSE_KEYS`). The
    route grammar applies: an owner that is a reserved route word
    (:data:`RESERVED_ROUTE_WORDS`, in any case), a tool literal, a
    placeholder or a number is not learnt (``""``), its repository still
    is. A path inside a URL is the host reader's.
    """
    found: list[tuple[str, str]] = []
    for match in _API_PATH_RE.finditer(text):
        rest = text[match.end() :].lstrip("/")
        stop = next((i for i, c in enumerate(rest) if c in _API_ROUTE_END), len(rest))
        route = rest[:stop].rstrip(_URL_TRIM_TAIL)
        if route.casefold() not in GITHUB_REPO_API_ROUTES:
            continue
        owner, repository = match.group("owner"), _bare(match.group("repo"))
        if not _names_a_value(repository):
            continue
        reserved = owner.casefold() in RESERVED_ROUTE_WORDS
        found.append((owner if _names_a_value(owner) and not reserved else "", repository))
    return found


_PROSE_KEYS: frozenset[str] = frozenset(
    {
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
)
"""The keys whose value is prose, never a structured source (0.27, 0.28: the owner's structure/prose decision).

Audited from the keys of every recorded answer, stdin and argument under
``tests/fixtures/forge`` (0.28): a comment's or a change request's body
(GitHub ``body``, GitLab's merge request ``description`` and note
``body``), its ``title``, a commit's ``message`` / ``messageHeadline`` /
``messageBody`` (GitLab's ``message`` also carries an error sentence), a
GitLab change's ``diff`` (file content, as a raw diff stdout is) and a
review comment's file path (GitHub ``path``, GitLab
``position.new_path``/``old_path``). The other keys of those answers are
identifiers, states, dates, refs, URLs and labels (``name``,
``workflow``: a check's or a workflow's configured name), not prose. A
key is matched as written: the forges write these keys and no other
spelling of them. A code block is inside a body.
"""

_FIELD_FLAGS: frozenset[str] = frozenset({"-f", "-F", "--field", "--raw-field"})
"""The ``gh api`` / ``glab api`` flags whose argument is one ``key=value`` field (0.28)."""

_PROSE_VALUE_FLAGS: frozenset[str] = frozenset({"--body", "--title", "--message", "--description"})
"""The flags whose argument is prose (0.28): ``gh pr create|comment|review --body``/``--title``, ``glab mr create --title``/``--description``, a ``--message``."""

_PROSE_STDIN_FLAG = "--body-file"
"""The flag that sends a body on the stdin when its argument is ``-`` (0.28): ``gh pr merge --squash --body-file -``."""

_STDIN_FIELD_VALUE = "@-"
"""A field value read from the stdin (``-F body=@-``, 0.28)."""


def _field_argument(argv: Sequence[object], index: int, arg: str) -> tuple[int, str] | None:
    """The ``key=value`` field the argument ``arg`` at ``index`` gives, with the argv index that holds it (0.28).

    ``-f``/``-F``/``--field``/``--raw-field`` give the next argument;
    ``-f<field>``/``-F<field>``, ``-f=<field>`` and
    ``--field=<field>``/``--raw-field=<field>`` give the rest of the same
    argument, as ``gh``'s and ``glab``'s flag parser reads them.
    """
    if arg in _FIELD_FLAGS:
        following = argv[index + 1] if index + 1 < len(argv) else None
        return (index + 1, following) if isinstance(following, str) else None
    flag, _assigned, rest = arg.partition("=")
    if flag in _FIELD_FLAGS:
        return index, rest
    if arg[:2] in ("-f", "-F"):
        return index, arg[2:]
    return None


def _declared_prose(argv: Sequence[object]) -> tuple[frozenset[int], bool]:
    """The argv indices one call declares prose, and whether it declares its stdin prose (0.28).

    Prose is what the call says it is, never what a string looks like:
    the value of a field whose key is a :data:`_PROSE_KEYS` key (``-f
    body=…``, ``-F path=…``, ``--raw-field=title=…``), the argument of a
    :data:`_PROSE_VALUE_FLAGS` flag (``--body <text>``, ``--title=<text>``),
    and the stdin when the call reads its body there (``--body-file -``,
    ``-F body=@-``). ``--input -`` sends a JSON payload whose prose keys
    are :data:`_PROSE_KEYS`, read where it is walked. The flags
    themselves and every other argument are structured.
    """
    prose: set[int] = set()
    stdin = False
    for index, arg in enumerate(argv):
        if not isinstance(arg, str):
            continue
        flag, joined, rest = arg.partition("=")
        following = argv[index + 1] if index + 1 < len(argv) else None
        if flag in _PROSE_VALUE_FLAGS:
            prose.add(index if joined else index + 1)
        if flag == _PROSE_STDIN_FLAG:
            stdin = stdin or (rest if joined else following) == "-"
        field = _field_argument(argv, index, arg)
        if field is None:
            continue
        where, value = field
        key, assigned, content = value.partition("=")
        if assigned and key in _PROSE_KEYS:
            if content == _STDIN_FIELD_VALUE:
                stdin = True
            else:
                prose.add(where)
    return frozenset(prose), stdin


@functools.cache
def _prose_parameters(method: str) -> frozenset[int]:
    """The positional arguments of a recorded protocol method that are prose, from its signature (0.28).

    A parameter is prose when its name is a :data:`_PROSE_KEYS` key in
    :class:`repoach.forge.protocol.Forge`: ``post_comment(number, body)``,
    ``reply(number, comment_id, body)``, ``upsert_marked_comment(number,
    marker, body)``. A keyword argument is a key of the recorded
    ``kwargs`` and is judged as one; an argument the signature does not
    name is structured. An unknown method declares none.
    """
    member = getattr(protocol.Forge, method, None)
    if not inspect.isfunction(member):
        return frozenset()
    positional = [
        p
        for p in inspect.signature(member).parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ][1:]
    return frozenset(i for i, p in enumerate(positional) if p.name in _PROSE_KEYS)


def _without_prose_arguments(raw: object, method: str) -> object:
    """``raw`` (``[calls, args, kwargs]``) with the method's prose positional arguments blanked (0.28)."""
    positions = _prose_parameters(method)
    if not isinstance(raw, list) or len(raw) < 2 or not isinstance(raw[1], list):
        return raw
    args = [None if i in positions else a for i, a in enumerate(raw[1])]
    return [raw[0], args, *raw[2:]]


def _structured_strings(node: object) -> list[str]:
    """Every string of the structured sources of the recorded calls, in recording order (0.27, 0.28).

    The structured sources are an exchange's argv, stdin and stderr, the
    field values of its JSON stdout, and the method's positional and
    keyword arguments; a JSON string among them is walked inside, keys
    included. Prose is not one: the value of a :data:`_PROSE_KEYS` key at
    any depth, an exchange's stdout that is not JSON (a diff, a raw
    answer), and the argv values and the stdin the exchange declares
    prose (:func:`_declared_prose`). An exchange is an object with an
    ``args`` list and a ``stdout``, as :func:`_argv_strings` reads it.
    """
    if isinstance(node, str):
        decoded = decode_json_values(node) if node.lstrip()[:1] in ("{", "[") else None
        if decoded is None:
            return [node]
        return [s for v in decoded for s in _structured_strings(v)]
    if isinstance(node, dict):
        argv = node.get("args")
        exchange = "stdout" in node and isinstance(argv, list)
        prose, prose_stdin = (
            _declared_prose(cast("list[object]", argv)) if exchange else (frozenset(), False)
        )
        found: list[str] = []
        for key, item in node.items():
            if key in _PROSE_KEYS:
                continue
            found.append(str(key))
            if exchange and key == "stdout" and isinstance(item, str):
                found += [s for v in decode_json_values(item) or [] for s in _structured_strings(v)]
                continue
            if key == "stdin" and prose_stdin:
                continue
            if key == "args" and prose:
                item = [a for i, a in enumerate(item) if i not in prose]
            found += _structured_strings(item)
        return found
    if isinstance(node, list):
        return [s for v in node for s in _structured_strings(v)]
    return []


def _names_a_value(segment: str) -> bool:
    """Whether an API path segment may name someone: not empty, a tool literal, a number or a placeholder."""
    return bool(
        segment
        and not CLI_PLACEHOLDER_RE.fullmatch(segment)
        and not segment.isdigit()
        and segment.casefold() not in _PLACEHOLDERS
    )


PROVENANCE: tuple[tuple[str, str], ...] = (
    ("remote", "repository"),
    ("url_field", "route"),
    ("argv", "repository"),
    ("text", "text"),
    ("structured", "api_path"),
)
"""THE rule of which source names an identity, and how (0.26, 0.27): ``(source, reading)``, in learning order.

Every namespace and repository :func:`known_values` learns from a path
comes from one row, and no reading runs outside it:

- ``remote``: the checkout's ``origin`` remote. Reading ``repository``:
  hard, by construction (``path_identity(remote=True)``): the namespace
  is every segment but the last whatever the words, the last is the
  repository, and a namespace of two segments or more is a value as each
  of its leading paths too.
- ``url_field``: a :data:`URL_KEYS` value of a decoded answer, on a host
  that is not public. Reading ``route``: hard, through the route grammar
  of :func:`path_identity` (reserved words apply), and no joined value
  (0.25, V09).
- ``argv``: an ``args`` string of a recorded exchange
  (:func:`_argv_strings`, every exchange) naming an API project
  (:func:`_api_project`), never one the call declares prose
  (:func:`_declared_prose`, 0.28). Reading ``repository``, as the remote.
- ``text``: every string of the fixture (:func:`repoach.forge.fixture_reading.fixture_strings`), the
  three sources above included. Reading ``text``: soft, never refusing
  a recording: the hosts and the paths after them
  (:func:`repoach.forge.closure.hosts`, :func:`_follow_segments`) and a
  GitLab ``projects/<group>%2F<repo>`` token (:data:`_ENCODED_PROJECT_RE`).
  A GitHub API path is not read there (0.25, 0.27).
- ``structured``: a string of the structured sources of a recorded call
  (:func:`_structured_strings`): the argv, the stdin, the stderr, the
  field values of a JSON stdout and the method's arguments, never prose.
  Prose is DECLARED, never guessed from its wording (0.28, the owner's
  decisions of 2026-11-24): the value of a :data:`_PROSE_KEYS` key (a
  body, a title, a commit message, a diff, a review path), a raw stdout,
  the argv value of a prose field (``-f body=``/``path=``/``title=``) or
  the argument of a prose flag (``--body``, ``--title``, ``--message``,
  ``--description``), the stdin a call declares its body
  (``--body-file -``, ``-F body=@-``), and a positional argument the
  method's signature names as one (:func:`_prose_parameters`). Reading
  ``api_path``: soft, a GitHub API path ``repos/<o>/<r>/<route>`` whose
  route word is a REST repository sub-resource
  (:func:`_api_path_projects`), in every decoded form. An API path cited
  in prose names no one (NG4-16).

The order is the learning order, and inside a source the recording's
order (the first exchange, the first item, the first field): a value two
readings name keeps the class of the first, so a value a URL field names
as a namespace stays ``org`` when an argv path names it as a repository.
Logins, names, mailboxes and user and repository ids are structured
fields read by their keys, not paths, and are outside this table.
"""


def _follow_segments(segments: list[str], *, full: bool) -> list[str]:
    """The segments after a host that name its namespace and repository.

    In a URL on the recorded instance every segment counts, since the
    project's pages sit under its repository; after any other lead
    (``//cdn.example.io/<ns>/<repo>/app.js``, a bare host, another
    host's URL) only the first two do, so the file name stays — unless
    the path is a route grammar whose identity sits deeper (an API root,
    a GitLab ``-``, Bitbucket Server's ``projects/<KEY>/repos/<r>``, 0.22).
    """
    named = [s for s in segments if s]
    if not named:
        return []
    if full or "-" in named or named[0].casefold() in _API_ROOTS or _project_root(named):
        return segments
    return named[:2]


def _api_route(path: Sequence[str]) -> bool:
    """Whether ``path`` names an owner through an API root (``users/<u>``, ``repos/<o>/<r>``, ``api/v<N>/…``).

    A text path names someone only with both a namespace and a
    repository, or through such a root, or before a GitLab ``/-/``
    separator (``<group>/-/issues``, 0.21 B1: everything before ``-`` is
    an identity), or through a Web IDE route (``-/ide/project/<ns>``,
    0.22); a lone segment after a host (``/docs``, ``/health``,
    ``/explore``) is a page, not an identity (D7 source 3, NG4-11), so
    the word is not learnt and not replaced wherever else it stands.
    """
    named = [s for s in path if s]
    return bool(named) and (
        named[0].casefold() in _API_ROOTS or "-" in named[1:] or _ide_route(named)
    )


def _path_after(text: str, end: int) -> list[str]:
    """The path segments written right after a host that ends at ``end``: ``/<a>/<b>`` or scp ``:<a>/<b>``."""
    lead = text[end : end + 1]
    if lead == ":":
        rest = text[end + 1 :]
        if rest[:1].isdigit():
            head = re.match(r"[0-9]{1,5}(?=/)", rest)
            if head is None:
                return []
            rest = rest[head.end() :]
    elif lead == "/":
        rest = text[end:]
    else:
        return []
    stop = next((i for i, c in enumerate(rest) if c in _PATH_END), len(rest))
    path = rest[:stop].rstrip(_URL_TRIM_TAIL).strip("/")
    return path.split("/") if path else []


def known_values(
    raw: object,
    *,
    roles: dict[str, str],
    remote: str = "",
    denylist: Denylist | None = None,
    min_length: int = NEEDLE_MIN_LENGTH,
    ids: UserIdMap | None = None,
    projects: UserIdMap | None = None,
    forge: str = "github",
    keep_users: frozenset[int] = frozenset(),
    keep_projects: frozenset[int] = frozenset(),
    method: str = "",
) -> closure.Known:
    """The private values one recording holds, with the class token each becomes (D7).

    Built before any rewrite from the structured fields of ``raw`` (logins,
    user and repository ids, a user's name and mailboxes, the hosts of
    :data:`URL_KEYS` values and of the remote, the recorded project's
    namespace and repository), from the deny-list, and from the hosts
    :func:`repoach.forge.closure.hosts` finds in any string of ``raw``, a
    numeric-label one aside (:func:`repoach.forge.closure.is_numeric_label_host`:
    its path is read and it is left for the oracle to refuse).
    A login known only from a mention and shorter than
    :data:`NEEDLE_MIN_LENGTH` is no value (0.20): the convenience layer
    replaces its ``@`` form and its bare word is too short to be a token.
    The remote and an argv API path (:func:`_api_project`) name a
    repository by construction (``path_identity(remote=True)``, 0.24):
    their namespace is every segment but the last whatever the words, and
    a namespace of two segments or more is a value as each of its leading
    paths too (``<a>/<b>`` -> ``org``), so a text path the route grammar
    keeps as it stands is written as the grammar writes any other. Which
    source names a namespace or a repository, and how, is
    :data:`PROVENANCE` (0.26) and nothing else: an argv API path is an
    argv string of a recorded call (:func:`_argv_strings`), a GitHub API
    path is read through a REST route word in the structured sources of
    a call only (:func:`_api_path_projects`, 0.27), and a body or a
    review comment's ``path`` led by ``repos/`` or ``projects/`` is prose,
    read by the text readers only (NG4-16), wherever it travels: a
    channel the call declares prose (an argv field or flag, a stdin, a
    positional argument of ``method``, 0.28) is no structured source.

    Args:
        raw: ``[calls, args, kwargs]``: the exchanges as the forge printed
            them and the method's arguments; never a redacted value.
        roles: The recording's login -> class word.
        remote: The checkout's ``origin`` remote, ``""`` when it has none.
        denylist: The deny-list, opaque (:class:`repoach.forge.denylist.Denylist`); judged,
            never replaced, its entries handed to the oracle by
            :meth:`repoach.forge.denylist.Denylist.judged_with` and to no caller. ``None`` reads
            the maintainer's (:func:`repoach.forge.denylist.read_denylist`): only an explicit
            empty ``Denylist()`` is empty, and a plain tuple of entries is refused.
        min_length: The shortest value a structured source may hold; the
            offline transform passes 1 for logins and segments.
        ids: The user-id map the structured pass filled; a fresh one when ``None``.
        projects: The repository-id map; a fresh one when ``None``.
        forge: ``github`` or ``gitlab``, the key of the id maps.
        keep_users: Declared synthetic user numbers, never values.
        keep_projects: Declared synthetic repository numbers, never values.
        method: The recorded protocol method, whose signature declares
            its prose positional arguments (:func:`_prose_parameters`).

    Returns:
        The known values, casefolded, each once per kind, with their tokens.

    Raises:
        NeedleError: A structured value shorter than ``min_length`` or equal
            to a class word or placeholder, by kind and ordinal.
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list cannot be read or
            is malformed.
        TypeError: ``denylist`` is not a :class:`repoach.forge.denylist.Denylist`.
        SyntheticIdsExhaustedError: An id needs a synthetic number past its range.
    """
    deny = _denylist_of(denylist)
    ids = ids if ids is not None else UserIdMap()
    projects = projects if projects is not None else UserIdMap.for_projects()
    strict: dict[str, list[tuple[str, str]]] = {kind: [] for kind in closure.KINDS}
    soft: dict[str, list[tuple[str, str]]] = {kind: [] for kind in closure.KINDS}

    by_login = {k.removeprefix(MENTION_ONLY_PREFIX).casefold(): v for k, v in roles.items()}

    def _role(login: str) -> str:
        return by_login.get(login.casefold(), "author")

    def _add(kind: str, value: str, token: str, *, hard: bool) -> None:
        if value and not (kind == "segment" and _names_no_one(value)):
            (strict if hard else soft)[kind].append((value, token))

    def _segments(path: Sequence[str], *, hard: bool, remote: bool = False) -> None:
        identity = path_identity(list(path), remote=remote)
        if not hard and identity.repository is None and not _api_route(path):
            return
        for segment in identity.namespace:
            _add("segment", segment, PLACEHOLDER_NAMESPACE, hard=hard)
        if remote:
            for end in range(2, len(identity.namespace) + 1):
                _add(
                    "segment", "/".join(identity.namespace[:end]), PLACEHOLDER_NAMESPACE, hard=hard
                )
        if identity.repository:
            _add("segment", identity.repository, PLACEHOLDER_PROJECT, hard=hard)

    def _host(value: str) -> None:
        soft["host"].append((value, PLACEHOLDER_HOST))

    def _add_id(number: object, *, project: bool) -> None:
        key = user_id_key(number)
        if isinstance(key, int) and key in (keep_projects if project else keep_users):
            return
        if isinstance(key, int):
            forms = [str(key)] if project else _id_encodings(key)
        else:
            forms = _id_forms(number)
        for form in forms:
            token = _id_token(form, key, ids=ids, projects=projects, forge=forge, project=project)
            if token != form:
                _add("id", form, token, hard=True)

    mention_only = {
        k.removeprefix(MENTION_ONLY_PREFIX) for k in roles if k.startswith(MENTION_ONLY_PREFIX)
    }
    logins = [k.removeprefix(MENTION_ONLY_PREFIX) for k in roles] + raw_logins(raw)
    for login in logins:
        if _names_no_one(login):
            continue
        if login in mention_only and len(login) < NEEDLE_MIN_LENGTH:
            continue
        word = _role(login)
        _add("login", login, word, hard=True)
        mention = bot_mention_stem(login)
        if mention is not None and len(mention) + 1 >= min_length:
            _add("login", f"{MENTION_ONLY_PREFIX}{mention}", word, hard=True)

    remote_host, remote_path = remote_location(remote)
    instance = remote_host if remote_host else _instance_host(raw)
    own = {remote_host} if remote_host and not closure.is_public_host(remote_host) else set()
    for host in sorted(FORGE_HOSTS | own):
        _host(host)

    strings = fixture_strings(raw, "")
    url_fields: list[str] = []
    for node in _walk_dicts(raw):
        if any(key in node for key in USER_KEYS) or (
            any(key in node for key in _NAME_KEYS) and any(key in node for key in _MAILBOX_KEYS)
        ):
            who = next((str(node[k]) for k in USER_KEYS if isinstance(node.get(k), str)), "")
            word = _role(who)
            for name_key in _NAME_KEYS:
                name = node.get(name_key)
                if isinstance(name, str):
                    _add("name", name, "", hard=False)
                    for part in _NAME_WORD_RE.findall(name):
                        _add("name", part, word, hard=False)
            for key in _MAILBOX_KEYS:
                box = node.get(key)
                if isinstance(box, str) and "@" in box:
                    local, _, domain = box.rpartition("@")
                    _add("mailbox", box, f"{word}@{PLACEHOLDER_HOST}", hard=False)
                    if len(local) >= NEEDLE_MIN_LENGTH:
                        _add("mailbox", local, word, hard=False)
                    if not closure.is_placeholder_host(domain) and not closure.is_public_host(
                        domain
                    ):
                        _host(domain)
        url_fields += [v for k, v in node.items() if k in URL_KEYS and isinstance(v, str)]

    def _repository(path: Sequence[str]) -> None:
        _segments(path, hard=True, remote=True)

    def _route(item: str) -> None:
        try:
            parts = urlsplit(item.strip())
        except ValueError as exc:
            _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
            return
        host = parts.hostname or ""
        if parts.scheme in URL_SCHEMES and host and not _is_allowed_host(host):
            _host(host)
            if not is_public_forge_host(host):
                _segments([s for s in parts.path.split("/") if s], hard=True)

    def _text(text: str) -> None:
        for found in _ENCODED_PROJECT_RE.finditer(text):
            _segments(unquote(found.group("path")).split("/"), hard=False)
        for form in text_forms.decoded_forms(text):
            for place in closure.hosts(form):
                allowed = closure.is_placeholder_host(place.host) or closure.is_public_host(
                    place.host
                )
                if allowed:
                    continue
                reads_path = place.led or not closure.is_address(place.host)
                if reads_path and not closure.is_numeric_label_host(place.host):
                    _host(place.host)
                if is_public_forge_host(place.host) or not reads_path:
                    continue
                follow = _follow_segments(
                    _path_after(form, place.end),
                    full=form[: place.start].endswith("://") and place.host.casefold() == instance,
                )
                if follow:
                    _segments(follow, hard=False)

    def _api_path(text: str) -> None:
        for form in text_forms.decoded_forms(text):
            for owner, repository in _api_path_projects(form):
                _add("segment", owner, PLACEHOLDER_NAMESPACE, hard=False)
                _add("segment", repository, PLACEHOLDER_PROJECT, hard=False)

    sources: dict[str, list[Any]] = {
        "remote": [remote_path],
        "url_field": url_fields,
        "argv": [_api_project(t) for t in _argv_strings(raw)],
        "text": [t for _p, t in strings],
        "structured": _structured_strings(_without_prose_arguments(raw, method)),
    }
    readings: dict[str, Callable[[Any], None]] = {
        "repository": _repository,
        "route": _route,
        "text": _text,
        "api_path": _api_path,
    }
    for source, reading in PROVENANCE:
        for item in sources[source]:
            readings[reading](item)

    for number in user_id_values(raw):
        _add_id(number, project=False)
    for number in project_id_values(raw):
        _add_id(number, project=True)
    for text in (t for _p, t in strings):
        for match in _PROJECT_ID_RE.finditer(text):
            _add_id(int(match.group("id")), project=True)

    entries: list[tuple[str, str]] = []
    tokens: dict[str, str] = {}
    for kind in closure.KINDS:
        seen: set[str] = set()
        for n, (value, token) in enumerate(_unique(strict[kind]), 1):
            folded = value.casefold()
            if len(folded) < (NEEDLE_MIN_LENGTH if kind == "id" else min_length):
                if kind == "id" and min_length < NEEDLE_MIN_LENGTH:
                    continue
                raise NeedleError(f"needle {kind} {n}: too short")
            if CLASS_WORD_RE.match(folded) or folded in _PLACEHOLDERS:
                raise NeedleError(f"needle {kind} {n}: a class word or placeholder")
            seen.add(folded)
            entries.append((kind, folded))
            tokens.setdefault(folded, token)
        for value, token in _unique(soft[kind]):
            folded = value.casefold()
            if (
                len(folded) < NEEDLE_MIN_LENGTH
                or CLASS_WORD_RE.match(folded)
                or folded in _PLACEHOLDERS
                or folded in seen
                or _names_no_one(folded)
            ):
                continue
            seen.add(folded)
            entries.append((kind, folded))
            tokens.setdefault(folded, token)
    return deny.judged_with(closure.Known(tuple(entries), _literals(), tuple(tokens.items())))


def _denylist_of(denylist: Denylist | None) -> Denylist:
    """The deny-list :func:`known_values` judges with; fails closed.

    Args:
        denylist: An opaque :class:`repoach.forge.denylist.Denylist`, or ``None``.

    Returns:
        ``denylist``, or the maintainer's (:func:`repoach.forge.denylist.read_denylist`) when
        ``None``: only an explicit empty ``Denylist()`` is empty.

    Raises:
        TypeError: ``denylist`` is neither, a plain tuple of entries included.
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list cannot be read.
    """
    if denylist is None:
        return read_denylist()
    if not isinstance(denylist, Denylist):
        raise TypeError("known_values: denylist must be a Denylist or None")
    return denylist


def _unique(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``pairs`` without a repeated value (casefolded), the first token kept."""
    seen: set[str] = set()
    out = []
    for value, token in pairs:
        if value.casefold() not in seen:
            seen.add(value.casefold())
            out.append((value, token))
    return out


def _id_token(
    form: str, key: object, *, ids: UserIdMap, projects: UserIdMap, forge: str, project: bool
) -> str:
    """The synthetic form of one id string: digits stay digits, a node id becomes ``U_<n>``, an opaque id ``U_<n>``."""
    owner = projects if project else ids
    number = owner.synthetic(key, forge=forge)
    if isinstance(key, int):
        return str(number) if form.isdigit() else f"U_{number}"
    return str(number)


@functools.cache
def _replacer(tokens: tuple[tuple[str, str], ...]) -> tuple[re.Pattern[str], dict[str, str]] | None:
    """The one pattern that finds every known value as a whole token, longest first, and its token table."""
    if not tokens:
        return None
    table = dict(tokens)
    alternation = "|".join(re.escape(v) for v in sorted(table, key=len, reverse=True))
    pattern = re.compile(
        rf"(?:(?<![{_ASCII_ALNUM}])|(?<=%2[Ff]))(?:{alternation})(?![{_ASCII_ALNUM}])", re.I
    )
    return pattern, table


def replace_known(text: str, known: closure.Known, mapping: dict[str, str] | None = None) -> str:
    """Replace every known value in ``text`` by its class token, at every position (D7).

    One pass over the longest value first, so a replaced value is never
    touched twice. A :data:`CLI_PLACEHOLDER_RE` literal is left as it is.
    A login is the convenience layer's (:func:`classify_logins`); a
    ``deny-list`` value is never replaced, the oracle refuses it.

    Args:
        text: Any string of the fixture.
        known: The recording's known values.
        mapping: Casefolded value -> token; the known values' own by default.

    Returns:
        ``text`` with each known value replaced.
    """
    table = dict(mapping) if mapping is not None else known.mapping()
    live = tuple((v, table[v]) for k, v in known.entries if k != "login" and table.get(v))
    built = _replacer(live)
    if built is None:
        return text
    pattern, replacements = built
    return outside_literals(
        text, lambda piece: pattern.sub(lambda m: replacements[m.group(0).casefold()], piece)
    )


def replace_known_in_value(value: Any, known: closure.Known) -> Any:
    """:func:`replace_known` on every string of a decoded value; keys are never rewritten.

    Args:
        value: A decoded JSON value, argv or the method's arguments.
        known: The recording's known values.

    Returns:
        A copy with every string rewritten.
    """
    if isinstance(value, dict):
        return {k: replace_known_in_value(v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_known_in_value(v, known) for v in value]
    if isinstance(value, str):
        return replace_known(value, known)
    return value


def replace_known_in_stdout(text: str, known: closure.Known) -> str:
    """:func:`replace_known` on a recorded stdout: JSON answers are walked, text is replaced whole.

    Args:
        text: One recorded stdout.
        known: The recording's known values.

    Returns:
        The stdout rewritten: each JSON value re-serialised, a text stdout replaced whole.
    """
    values = decode_json_values(text)
    if values is None:
        return replace_known(text, known)
    return "\n".join(
        json.dumps(replace_known_in_value(v, known), ensure_ascii=False) for v in values
    )


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
        normalize: Applied to every issued argv element and stdin before the
            comparison, so a raw value a forge computes itself (a remote, a
            link) is compared in the redacted form it was recorded in; none
            for a committed fixture, whose argv are compared as they are.
    """

    exchanges: list[Exchange]
    normalize: Callable[[str], str] | None = None

    def __call__(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Return the next recorded result after checking the argv matches."""
        if not self.exchanges:
            raise ReplayMismatchError(f"unrecorded call: {argv[1:]}")
        expected = self.exchanges.pop(0)
        issued = list(argv[1:])
        if self.normalize is not None:
            issued = [self.normalize(a) for a in issued]
            input_data = self.normalize(input_data) if input_data else input_data
        actual = (_program(argv[0]), issued)
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

    Args:
        method: The protocol method.
        args: Its positional arguments, as JSON.
        kwargs: Its keyword arguments, as JSON.

    Returns:
        The typed positional and keyword arguments.
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
    """Write one fixture file (sorted keys, two-space indent, trailing newline).

    Args:
        path: The fixture's file; its directory is created.
        data: The fixture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def rename_logins_in_text(text: str, roles: dict[str, str]) -> str:
    """Replace every login of ``roles`` in free text by its class word, a tool literal left as it is.

    The offline transform's one entry into the text rewriting: the ``@<login>`` form of every
    login (and a ``<name>[bot]`` login's ``@<stem>``), and the bare form of every login that is
    not mention-only (a key without :data:`MENTION_ONLY_PREFIX`).

    Args:
        text: One string of a fixture.
        roles: Login (a mention-only one keyed ``@<login>``) -> class word.

    Returns:
        ``text`` with each login replaced, outside the tool literals (:func:`outside_literals`).
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    for login, word in sorted(roles.items(), key=lambda pair: len(pair[0]), reverse=True):
        handle = login.removeprefix(MENTION_ONLY_PREFIX)
        rules.append((mention_pattern(handle), f"@{word}"))
        if handle == login:
            rules.append((token_pattern(login), word))
            mention_stem = bot_mention_stem(login)
            if mention_stem is not None:
                rules.append((mention_pattern(mention_stem), f"@{word}"))

    def rewrite(piece: str) -> str:
        for pattern, replacement in rules:
            piece = pattern.sub(replacement, piece)
        return piece

    return outside_literals(text, rewrite)


def rename_logins_in_argument(arg: str, roles: dict[str, str]) -> str:
    """Replace a login that is a whole argument, a path segment or a ``=`` value; never a mention-only one.

    Args:
        arg: One argv string of a fixture.
        roles: Login (a mention-only one keyed ``@<login>``) -> class word.

    Returns:
        The class word when ``arg`` is a login, else ``arg`` with each login after ``/`` or ``=``
        and before ``/``, ``&`` or its end replaced.
    """
    actors = {k: v for k, v in roles.items() if not k.startswith(MENTION_ONLY_PREFIX)}
    if arg in actors:
        return actors[arg]
    for login, word in actors.items():
        arg = re.sub(rf"(?<=[/=]){re.escape(login)}(?=[/&]|$)", word, arg)
    return arg


def rewrite_recorded_source(source: str, forge: str) -> str:
    """A committed fixture's ``source`` with its recorded head naming the forge, not the checkout's folder.

    Args:
        source: The fixture's ``source``.
        forge: ``github`` or ``gitlab``.

    Returns:
        ``source`` with a ``recorded <date> from <folder>`` head (:data:`RECORDED_SOURCE_RE`)
        written as :func:`recorded_source` writes it; any other ``source`` as it is.
    """
    m = RECORDED_SOURCE_RE.match(source)
    if m is None:
        return source
    rest = source[m.end() :]
    if rest.startswith(" "):
        rest = "," + rest
    return recorded_source(m.group(1), forge) + rest


def replace_known_in_source(source: str, known: closure.Known) -> str:
    """Replace every known value in a fixture's ``source``, its template head (:data:`repoach.forge.fixture_reading.RECORDED_SOURCE_HEAD_RE`) kept.

    Args:
        source: The fixture's ``source``.
        known: The recording's known values.

    Returns:
        The head as it is, then the rest with each known value replaced by its class token.
    """
    head = RECORDED_SOURCE_HEAD_RE.match(source)
    cut = head.end() if head else 0
    return source[:cut] + replace_known(source[cut:], known)
