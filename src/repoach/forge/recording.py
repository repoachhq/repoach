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

GITLAB_PROJECT_BOT_RE = re.compile(r"^project_[0-9]+_bot_[0-9a-f]+$")
"""A GitLab project access token user: a service account named after a real project id."""

PLACEHOLDER_HOST = "forge.example.com"
"""The host every recorded URL is given (an RFC 2606 domain)."""

PLACEHOLDER_NAMESPACE = "org"
"""The namespace every recorded URL is given."""

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

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}\\,]+")
"""An ``http(s)`` URL inside free text (a log line, a ``glab`` error)."""

_EXAMPLE_HOST_RE = re.compile(
    r"(^|\.)(example\.(com|org|net)|example|test|invalid|localhost)$|^127\.|^localhost$", re.I
)
"""Hosts that name no one (RFC 2606, loopback); text URLs on them are kept."""


def _namespaced(segments: list[str]) -> list[str]:
    """Replace the namespace of a forge path by :data:`PLACEHOLDER_NAMESPACE`."""
    core = [s for s in segments if s]
    if not core:
        return segments
    if segments[:3] == ["api", "v4", "projects"] and len(segments) > 3:
        project = unquote(segments[3])
        if "/" in project:
            repo = project.rsplit("/", 1)[1]
            return [*segments[:3], quote(f"{PLACEHOLDER_NAMESPACE}/{repo}", safe=""), *segments[4:]]
        return segments
    if segments[0] in ("repos", "users", "orgs") and len(core) >= 2:
        return [segments[0], PLACEHOLDER_NAMESPACE, *segments[2:]]
    if "-" in segments:
        dash = segments.index("-")
        if dash >= 2:
            return [PLACEHOLDER_NAMESPACE, *segments[dash - 1 :]]
        return segments
    if len(core) < 3 or segments[2] in _GITHUB_ROUTES:
        return [PLACEHOLDER_NAMESPACE, *segments[1:]]
    trailing = [""] if segments[-1] == "" else []
    return [PLACEHOLDER_NAMESPACE, core[-1], *trailing]


def redact_url(value: str) -> str:
    """Rewrite a forge URL by class: placeholder host and namespace, path shape kept.

    ``https://<forge>/<owner>/<repo>/pull/12`` becomes
    ``https://forge.example.com/org/<repo>/pull/12``; on GitLab every
    segment before the repository (the one preceding ``/-/``, or the
    last) is the namespace; ``/-/jobs/5`` with no namespace keeps its
    path. ``api/v4/projects/<group>%2F<repo>`` and ``repos/<owner>/<repo>``
    API paths get the same namespace.

    Returns:
        The redacted URL, or ``""`` when ``value`` is not an ``http(s)``
        URL with a host (the caller drops the key).
    """
    try:
        parts = urlsplit(value.strip())
    except ValueError as exc:
        _log.debug("forge.recording.not_a_url", error=type(exc).__name__)
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    path = parts.path
    if path.startswith("/") and len(path) > 1:
        path = "/" + "/".join(_namespaced(path[1:].split("/")))
    return urlunsplit((parts.scheme, PLACEHOLDER_HOST, path, parts.query, parts.fragment))


def redact_urls_in_text(text: str) -> str:
    """Apply :func:`redact_url` to every URL of ``text`` not on a placeholder host."""

    def _one(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".:;")
        tail = match.group(0)[len(url) :]
        host = urlsplit(url).hostname or ""
        if not host or _EXAMPLE_HOST_RE.search(host):
            return match.group(0)
        return (redact_url(url) or url) + tail

    return _URL_IN_TEXT_RE.sub(_one, text)


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


def redact_payload(value: object) -> object:
    """Keep only :data:`RECORDED_FIELDS` keys, recursively, and redact by class.

    :data:`PERSONAL_KEY_RE` keys are dropped everywhere; in a user object
    (one holding ``login`` or ``username``) ``name`` and the URL keys are
    dropped too, while elsewhere ``name`` is a check-run or job name the
    forges read. ``id`` is kept in every object. A string under a
    :data:`URL_KEYS` key is rewritten by :func:`redact_url` and dropped
    when it is not a URL; every other string has its URLs redacted.
    """
    if isinstance(value, dict):
        user = any(k in value for k in USER_KEYS)
        out: dict[str, object] = {}
        for key, item in value.items():
            if key not in RECORDED_FIELDS or PERSONAL_KEY_RE.match(key):
                continue
            if user and (key == "name" or key in URL_KEYS):
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
    """``login`` as a whole token (``/``, ``=``, ``?`` and quotes delimit it; ``:owner`` is a placeholder, not a login)."""
    return re.compile(rf"(?<![A-Za-z0-9_.:\[\]-]){re.escape(login)}(?![A-Za-z0-9_\[\]-])", re.I)


def classify_text(text: str, roles: dict[str, str]) -> str:
    """Replace every login of ``roles`` appearing as a token in ``text`` by its class word."""
    for login in sorted(roles, key=len, reverse=True):
        text = _login_pattern(login).sub(roles[login], text)
    return text


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

SYNTHETIC_ID_LIMIT = 10000
"""Numeric user ids below this are synthetic; a recorded one never is (D1, 0.7)."""

_SYNTHETIC_NODE_RE = re.compile(r"^U_[0-9]+$")
"""A synthetic GitHub user node id, ``U_<n>``."""

_LEGACY_USER_NODE_RE = re.compile(r"^04:User([0-9]+)$")
"""A decoded legacy GitHub user node id; its number is the account's numeric id."""


def is_synthetic_user_id(value: object) -> bool:
    """Whether ``value`` is a synthetic user id: an integer below the limit, or ``U_<n>``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value < SYNTHETIC_ID_LIMIT
    return isinstance(value, str) and bool(_SYNTHETIC_NODE_RE.match(value))


def user_id_key(value: object) -> object:
    """The account a user id names: a number for numeric and legacy node ids, else the string.

    A legacy GitHub node id (base64 of ``04:User<n>``) and the REST id
    ``<n>`` of the same account share one key, so both map to one
    synthetic number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, int):
        return value
    if value.isdigit():
        return int(value)
    try:
        decoded = base64.b64decode(value, validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        _log.debug("forge.recording.opaque_node_id", error=type(exc).__name__)
        return value
    m = _LEGACY_USER_NODE_RE.match(decoded)
    return int(m.group(1)) if m else value


@dataclass
class UserIdMap:
    """Real user id → synthetic id, a sequence in order of first appearance (D1, 0.7).

    The first account seen is ``1001``, the next ``1002``…; a node id
    becomes ``U_<n>`` with the same ``n`` as the account's numeric id.
    The mapping is never written anywhere, and a sequence carries no
    information about the id it replaces, so a fixture cannot be mapped
    back by enumerating small ids (as a hash of the id could).

    Attributes:
        keep_synthetic: Leave ids that are already synthetic as they are
            (the offline transform of hand-written fixtures); the
            recorder remaps every id.
        start: The first number handed out.
        ids: Account key → synthetic number.
    """

    keep_synthetic: bool = False
    start: int = SYNTHETIC_ID_START
    ids: dict[object, int] = field(default_factory=dict)

    def synthetic(self, value: object) -> object:
        """The synthetic id for ``value`` (allocated on first sight)."""
        if self.keep_synthetic and is_synthetic_user_id(value):
            return value
        key = user_id_key(value)
        if key is None:
            return value
        n = self.ids.setdefault(key, self.start + len(self.ids))
        return f"U_{n}" if isinstance(value, str) else n

    def apply(self, payload: object) -> object:
        """Remap the ``id`` of every user object (one holding ``login`` or ``username``)."""
        if isinstance(payload, dict):
            user = any(k in payload for k in USER_KEYS)
            return {
                k: self.synthetic(v) if user and k == "id" else self.apply(v)
                for k, v in payload.items()
            }
        if isinstance(payload, list):
            return [self.apply(v) for v in payload]
        return payload

    def apply_known(self, payload: object) -> object:
        """Remap every ``id`` whose account is already mapped (a result: ``Review.id``)."""
        if isinstance(payload, dict):
            return {
                k: self._known(v) if k == "id" else self.apply_known(v) for k, v in payload.items()
            }
        if isinstance(payload, list):
            return [self.apply_known(v) for v in payload]
        return payload

    def _known(self, value: object) -> object:
        """``value`` remapped when its account is in :attr:`ids`, else unchanged."""
        if self.keep_synthetic and is_synthetic_user_id(value):
            return value
        key = user_id_key(value)
        if key is None or key not in self.ids:
            return value
        n = self.ids[key]
        return f"U_{n}" if isinstance(value, str) else n


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
