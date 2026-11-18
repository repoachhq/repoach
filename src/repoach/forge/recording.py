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
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import BaseModel

from ..core.logging import get_logger
from ..core.redaction import SECRET_VALUE_PATTERNS
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

TOKEN_CHARS = "A-Za-z0-9_-"
"""The characters inside a token, as a regex class body (SP-RECORDER-REDACTS-BY-CLASS I1, 0.10).

The one boundary of every whole-word pattern: the login replacer, the
``@<login>`` replacer, the deny-list and the closure oracle all use
:func:`token_pattern`. ``.``, ``:``, ``@``, ``/``, ``[``, ``]``, quotes
and every other character delimit a token; ``-`` and ``_`` are inside one.
"""


def token_pattern(word: str) -> re.Pattern[str]:
    """``word`` as a whole token (:data:`TOKEN_CHARS`), case-insensitive: the only whole-word pattern factory."""
    return re.compile(rf"(?<![{TOKEN_CHARS}]){re.escape(word)}(?![{TOKEN_CHARS}])", re.I)


CLI_PLACEHOLDER_RE = re.compile(
    rf"(?<![{TOKEN_CHARS}])(?::(?:owner|repo|id)|/dev/null)(?![{TOKEN_CHARS}])"
)
"""The tools' own literals: the argv templates ``:owner``, ``:repo``, ``:id`` and git's ``/dev/null`` (0.10).

``gh api repos/:owner/:repo/…`` and ``glab api projects/:id/…`` are
written by the forge code and expanded by the CLI; ``--- /dev/null`` is
how git names the missing side of an added or deleted file. None carries
a recorded value. The replacer leaves them alone and the closure oracle
does not judge them, so a login ``owner`` cannot turn the forge's own
argv into one the replay does not make, nor a login ``dev`` a diff
header. Case-sensitive: the tools write the lowercase forms only.
"""

_MENTION_END = r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_]|\()"
"""What may not follow a handle: a handle character, a dotted name (``@pytest.fixture``) or a call."""

MENTION_RE = re.compile(
    rf"(?<![A-Za-z0-9_.@/+-])@([A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?){_MENTION_END}", re.I
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
    and a :data:`PYTHON_DECORATORS` name is a decorator, not a handle.
    """
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in PROSE_KEYS and isinstance(item, str):
                    prose = _CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), item)
                    for m in MENTION_RE.finditer(prose):
                        handle = m.group(1)
                        if handle in PYTHON_DECORATORS or is_classified_login(handle):
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

URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "ssh", "git"})
"""Schemes of a forge URL: the web and API ones, and a remote's ``ssh://`` and ``git://`` (0.9)."""

_URL_IN_TEXT_RE = re.compile(r"(?:https?|ssh|git)://[^\s\"'<>()\[\]{}\\,]+", re.I)
"""A URL inside free text (a log line, a ``glab`` error, a clone command), any case (I2, 0.10)."""

_SCP_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_.+/-])(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
    r":(?!//)(?P<path>(?:[A-Za-z0-9_.~%-]+/)+[A-Za-z0-9_.~%-]*)",
    re.I,
)
"""An scp-style remote inside free text, ``<user>@<host>:<namespace>/<repo>`` (0.9), any case.

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

_EXAMPLE_HOST_RE = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$|^127\.|^localhost$", re.I
)
"""Hosts that name no one (RFC 2606, loopback); text URLs on them are kept, except on :data:`PLACEHOLDER_HOST`."""


def _project(segment: str) -> str:
    """:data:`PLACEHOLDER_PROJECT`, with ``segment``'s ``.git`` suffix when it has one."""
    return PLACEHOLDER_PROJECT + (".git" if segment.casefold().endswith(".git") else "")


def _namespaced(segments: list[str]) -> list[str]:
    """Replace the namespace and the repository of a forge path by their placeholders."""
    core = [s for s in segments if s]
    if not core:
        return segments
    if segments[:3] == ["api", "v4", "projects"] and len(segments) > 3:
        if "/" in unquote(segments[3]):
            placeholder = f"{PLACEHOLDER_NAMESPACE}/{PLACEHOLDER_PROJECT}"
            return [*segments[:3], quote(placeholder, safe=""), *segments[4:]]
        return segments
    if segments[0] == "repos" and len(core) >= 3:
        return ["repos", PLACEHOLDER_NAMESPACE, _project(segments[2]), *segments[3:]]
    if segments[0] in ("repos", "users", "orgs") and len(core) >= 2:
        return [segments[0], PLACEHOLDER_NAMESPACE, *segments[2:]]
    if "-" in segments:
        dash = segments.index("-")
        if dash >= 2:
            return [PLACEHOLDER_NAMESPACE, _project(segments[dash - 1]), *segments[dash:]]
        return segments
    if len(core) < 3 or segments[2] in _GITHUB_ROUTES:
        rest = segments[1:]
        if rest and rest[0]:
            rest = [_project(rest[0]), *rest[1:]]
        return [PLACEHOLDER_NAMESPACE, *rest]
    trailing = [""] if segments[-1] == "" else []
    return [PLACEHOLDER_NAMESPACE, _project(core[-1]), *trailing]


def _repository_root(path: str) -> str | None:
    """The repository name of a path that names a repository and nothing under it, else ``None``."""
    segments = path.strip("/").split("/")
    core = [s for s in segments if s]
    if len(core) < 2 or len(core) != len(segments) or "-" in segments:
        return None
    if segments[0] in ("api", "repos", "users", "orgs"):
        return None
    if len(segments) > 2 and segments[2] in _GITHUB_ROUTES:
        return None
    return core[-1].removesuffix(".git")


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


def _kept_host(host: str) -> bool:
    """Whether a text URL on ``host`` is kept as it is: an RFC 2606 or loopback host, not the placeholder."""
    return bool(_EXAMPLE_HOST_RE.search(host)) and host.casefold() != PLACEHOLDER_HOST


def redact_urls_in_text(text: str) -> str:
    """Redact every URL and scp-style remote of ``text`` not on a kept host (:func:`_kept_host`).

    A URL goes through :func:`redact_url`. An scp remote
    ``<user>@<host>:<ns>/<repo>`` is normalised through it too (0.10): read
    as ``ssh://<user>@<host>/<ns>/<repo>``, redacted, and written back as
    ``git@forge.example.com:org/project``, its ``.git`` suffix kept and its
    user never. A URL on :data:`PLACEHOLDER_HOST` is redacted again, so a
    hand-written one gets the placeholder path too.
    """

    def _one(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".:;")
        tail = match.group(0)[len(url) :]
        host = urlsplit(url).hostname or ""
        if not host or _kept_host(host):
            return match.group(0)
        return (redact_url(url) or url) + tail

    def _scp(match: re.Match[str]) -> str:
        if _kept_host(match.group("host").casefold()):
            return match.group(0)
        url = redact_url(f"ssh://{match.group('user')}@{match.group('host')}/{match.group('path')}")
        return url.removeprefix("ssh://").replace("/", ":", 1)

    return _SCP_IN_TEXT_RE.sub(_scp, _URL_IN_TEXT_RE.sub(_one, text))


def unredacted_host(text: str) -> bool:
    """Whether ``text`` holds a URL or an scp-style remote on a host :func:`redact_urls_in_text` rewrites.

    After redaction no such host is left, so one found in a fixture about
    to be written means a value escaped the redaction (D3, 0.9).
    """
    for match in _URL_IN_TEXT_RE.finditer(text):
        host = urlsplit(match.group(0).rstrip(".:;")).hostname or ""
        if host and not _EXAMPLE_HOST_RE.search(host):
            return True
    return any(not _EXAMPLE_HOST_RE.search(m.group("host")) for m in _SCP_IN_TEXT_RE.finditer(text))


def redact_urls_in_value(value: object) -> object:
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
    """Keep only :data:`RECORDED_FIELDS` keys, recursively, and redact by class.

    :data:`PERSONAL_KEY_RE` keys are dropped everywhere; in a user object
    (one holding ``login`` or ``username``) ``name`` and the URL keys are
    dropped too, while elsewhere ``name`` is a check-run or job name the
    forges read. ``id`` is kept in every object. A string under a
    :data:`URL_KEYS` key is rewritten by :func:`redact_url` and dropped
    when it is not a URL; every other string has its URLs redacted. In
    a repository object (one whose URL names a repository and nothing
    under it), a ``name`` or ``path`` equal to that repository's name
    becomes :data:`PLACEHOLDER_PROJECT` (0.9).
    """
    if isinstance(value, dict):
        user = any(k in value for k in USER_KEYS)
        repository = _repository_of(value)
        out: dict[str, object] = {}
        for key, item in value.items():
            if key not in RECORDED_FIELDS or PERSONAL_KEY_RE.match(key):
                continue
            if user and (key == "name" or key in URL_KEYS):
                continue
            if repository is not None and key in ("name", "path") and item == repository:
                out[key] = PLACEHOLDER_PROJECT
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


def _login_pattern(login: str) -> re.Pattern[str]:
    """``login`` as a whole token: :func:`token_pattern`, wherever it stands (I1, 0.10)."""
    return token_pattern(login)


def classify_text(text: str, roles: dict[str, str]) -> str:
    """Replace every login of ``roles`` appearing as a token in ``text`` by its class word.

    A :data:`MENTION_ONLY_PREFIX` key is replaced in its ``@<handle>``
    form only (D2, 0.9). A :data:`CLI_PLACEHOLDER_RE` template is left
    as it is (0.10); every other occurrence of the token is replaced,
    after a ``:``, a ``.`` or a ``/`` alike.
    """
    pieces = CLI_PLACEHOLDER_RE.split(text)
    templates = CLI_PLACEHOLDER_RE.findall(text)
    for login in sorted(roles, key=len, reverse=True):
        if login.startswith(MENTION_ONLY_PREFIX):
            pattern = mention_pattern(login.removeprefix(MENTION_ONLY_PREFIX))
            word = f"@{roles[login]}"
        else:
            pattern = _login_pattern(login)
            word = roles[login]
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


class SyntheticIdsExhaustedError(Exception):
    """A map would hand out a number past :data:`SYNTHETIC_ID_END`."""


def is_synthetic_number(value: object) -> bool:
    """Whether ``value`` is an ``int`` in ``SYNTHETIC_ID_START..SYNTHETIC_ID_END``."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and SYNTHETIC_ID_START <= value <= SYNTHETIC_ID_END
    )


def trusted_declaration(raw: object) -> frozenset[int]:
    """The numbers of a ``synthetic_user_ids`` value that may be kept: the in-range ones only."""
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(n for n in raw if is_synthetic_number(n))


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

    Attributes:
        start: The first number handed out.
        ids: ``(forge, account)`` → synthetic number.
    """

    start: int = SYNTHETIC_ID_START
    ids: dict[tuple[str, object], int] = field(default_factory=dict)

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
            SyntheticIdsExhaustedError: The next number would pass :data:`SYNTHETIC_ID_END`.
        """
        key = user_id_key(value)
        if key is None:
            return value
        if is_synthetic_number(key) and key in keep:
            return value
        if (forge, key) not in self.ids:
            n = self.start + len(self.ids)
            if n > SYNTHETIC_ID_END:
                raise SyntheticIdsExhaustedError(f"synthetic user id {n} past {SYNTHETIC_ID_END}")
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
