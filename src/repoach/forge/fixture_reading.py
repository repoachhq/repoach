"""How a fixture is read: its strings, its logins and its JSON answers (SP-RECORDER-REDACTS-BY-CLASS D3).

The recorder (:mod:`repoach.forge.recording`) and the judge (:mod:`repoach.forge.fixture_judge`)
read a fixture through this one module, so the judge depends on no internal of the recorder
(PR #319 round 7, 4687852547): the recorded field names, the fixture's own keys, the login keys
and class words, the JSON decoder of a recorded answer, the walk that yields every string of a
fixture with its key path, and the secret-shaped key finder. Nothing here rewrites a value.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from ..core.logging import get_logger
from . import protocol

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

A recorded answer keeps only these keys (:func:`repoach.forge.recording.redact_stdout`): tokens
(``runners_token``…), emails, ``node_id``\\ s and every other field the
conformance suite never reads are dropped at record time, so a fixture
cannot carry a secret the protocol does not need.
"""


SECRET_KEY_RE = re.compile(r"(token|secret|password|private_key|api_key|node_id|email)", re.I)
"""Keys whose presence in a recorded answer means redaction failed."""


LOGIN_KEYS: frozenset[str] = frozenset({"login", "username", "author", "resolved_by", "user"})
"""Keys whose string value is a login, replaced by its class word (:func:`repoach.forge.recording.classify_logins`)."""


CLASS_WORD_RE = re.compile(r"^(maintainer|author|bot|agent|service)(-[1-9][0-9]*)?$")
"""A recorded login once classified: a class word, optionally ``-<n>``."""


NON_PERSON_LOGINS: frozenset[str] = frozenset({"github-actions[bot]", "repoach-bot"})
"""Logins that name no person, kept as they are by the recorder and the offline transform.

``github-actions[bot]`` is the platform's service account and the
default trusted bot (``repoach.project.resolve.GITHUB_DEFAULT_BOT``);
``repoach-bot`` is this project's own bot. Every other login, a
placeholder such as ``alice`` or ``owner`` included, is a class word
(the real-account rule, SP-RECORDER-REDACTS-BY-CLASS D2).
"""


def is_classified_login(login: str) -> bool:
    """Whether ``login`` may stand in a fixture: a class word or a :data:`NON_PERSON_LOGINS` entry.

    Args:
        login: A recorded login.

    Returns:
        ``True`` for a class word or a non-person login.
    """
    return bool(CLASS_WORD_RE.match(login)) or login in NON_PERSON_LOGINS


SYNTHETIC_IDS_KEY = "synthetic_user_ids"
"""The fixture key declaring the synthetic user ids the fixture holds (D1c, 0.8, 0.9).

The recorder and the offline transform write the numbers they handed
out. A user id is synthetic because its fixture declares it AND it lies
in ``SYNTHETIC_ID_START..SYNTHETIC_ID_END`` (:func:`repoach.forge.recording.trusted_declaration`):
a declared number outside the range — a real id typed into a fixture —
is remapped like any undeclared one.
"""


SYNTHETIC_PROJECT_IDS_KEY = "synthetic_project_ids"
"""The fixture key declaring the synthetic repository ids the fixture holds (0.10), as :data:`SYNTHETIC_IDS_KEY` does for users."""


def login_values(payload: object, path: str = "") -> list[tuple[str, str]]:
    """Every ``(key path, value)`` of a string under a :data:`LOGIN_KEYS` key, in order.

    Args:
        payload: A decoded JSON value.
        path: The key path of ``payload`` itself, ``""`` at the top.

    Returns:
        Each login key's path and string value, depth first.
    """
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
    """Decode every top-level JSON value of ``text``; ``None`` when it is not JSON (a diff, a log).

    A text nested deeper than the interpreter can decode (thousands of
    ``[`` in a comment body) is not JSON either: the decoder's
    ``RecursionError`` is caught with its decode error (0.21, B3), so the
    text is read as text and nothing raises.
    """
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
    except (json.JSONDecodeError, RecursionError) as exc:
        _log.debug("forge.recording.not_json", error=type(exc).__name__)
        return None
    return values or None


def secret_keys(value: object) -> list[str]:
    """Keys matching :data:`SECRET_KEY_RE` anywhere in a decoded payload.

    Args:
        value: A decoded JSON value.

    Returns:
        Each matching key, depth first.
    """
    if isinstance(value, dict):
        hits = [k for k in value if SECRET_KEY_RE.search(k)]
        for v in value.values():
            hits += secret_keys(v)
        return hits
    if isinstance(value, list):
        return [k for v in value for k in secret_keys(v)]
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

    Args:
        value: A decoded value of the fixture.
        path: The key path of ``value`` itself.

    Returns:
        Each path and string, in walking order.
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


RECORDED_SOURCE_HEAD_RE = re.compile(
    r"^recorded [0-9]{4}-[0-9]{2}-[0-9]{2} from a live (?:github|gitlab) change request"
)
"""The template head :func:`repoach.forge.recording.recorded_source` writes; the oracle judges only what follows it (0.11, R6)."""
