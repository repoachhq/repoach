"""The redaction pipeline of a forge recording, shared by the live recorder and the offline transform.

SP-RECORDER-REDACTS-BY-CLASS D4: ``scripts/record_forge_fixture.py`` runs the
stages of :func:`redact_recording` on the exchanges it has just recorded, and
both it and ``scripts/redact_forge_fixtures.py`` finish a fixture through
:func:`seal_fixture`, so the synthetic-id declarations and the last judge
before a write are one code path. The stages, in order: the roles are applied
and text URLs redacted (:func:`classify_calls`), user and repository ids are
made synthetic (:func:`synthesize_user_ids`), the known values are read from
the raw recording (:func:`repoach.forge.recording.known_values`) and replaced
everywhere (:func:`replace_known_in_calls`).

The offline transform of a committed fixture lives here too (:func:`redact_fixture`, and
:func:`redact_corpus` for a whole corpus), so the scripts hold their command line only: the
exchanges' URLs are redacted (:func:`redact_output`), the roles read through the one role
builder (:func:`fixture_roles`) and applied, the ids remapped, the known values read from the
fixture's own structured fields (:func:`fixture_known`) and replaced, and the fixture sealed by
:func:`seal_fixture`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoach.forge import closure
from repoach.forge.denylist import Denylist, read_denylist
from repoach.forge.fixture_judge import UnclassifiedValueError, refuse_unclassified
from repoach.forge.recording import (
    LOGIN_KEYS,
    SYNTHETIC_ID_END,
    SYNTHETIC_ID_START,
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_ID_END,
    SYNTHETIC_PROJECT_ID_START,
    SYNTHETIC_PROJECT_IDS_KEY,
    UserIdMap,
    assign_roles,
    classify_logins,
    declared_project_ids,
    declared_user_ids,
    decode_json_values,
    known_values,
    login_sightings,
    raw_logins,
    redact_payload,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    rename_logins_in_argument,
    rename_logins_in_text,
    replace_known,
    replace_known_in_source,
    replace_known_in_stdout,
    replace_known_in_value,
    rewrite_recorded_source,
    trusted_declaration,
)


def classify_calls(calls: list[dict[str, Any]], roles: dict[str, str]) -> list[dict[str, Any]]:
    """Apply the roles to every exchange's argv, stdin, stdout and stderr, and redact text URLs.

    Args:
        calls: The recorded exchanges, their stdout already redacted by field.
        roles: Login -> class word for the recording.

    Returns:
        New exchanges; a JSON stdout is re-serialised value by value, any other string is
        rewritten as text.
    """
    out: list[dict[str, Any]] = []
    for call in calls:
        new = dict(call)
        new["args"] = classify_logins(redact_urls_in_value(call["args"]), roles)
        values = decode_json_values(call["stdout"])
        if values is None:
            new["stdout"] = str(classify_logins(redact_urls_in_text(call["stdout"]), roles))
        else:
            new["stdout"] = "\n".join(
                json.dumps(classify_logins(v, roles), ensure_ascii=False) for v in values
            )
        for key in ("stdin", "stderr"):
            if isinstance(call.get(key), str):
                new[key] = str(classify_logins(redact_urls_in_text(call[key]), roles))
        out.append(new)
    return out


def synthesize_user_ids(
    calls: list[dict[str, Any]], ids: UserIdMap, *, forge: str, projects: UserIdMap | None = None
) -> list[dict[str, Any]]:
    """Replace every user id and every repository id of the recorded exchanges by a synthetic one (D1c).

    Repository ids include an ``api/v4/projects/<id>`` path.

    Args:
        calls: The recorded exchanges, roles already applied.
        ids: The user-id map; it hands out the synthetic numbers and remembers them.
        forge: ``github`` or ``gitlab``, the key of the maps.
        projects: The repository-id map; a fresh one when ``None``.

    Returns:
        New exchanges: user and repository ids of a JSON stdout remapped, repository ids of
        argv, stdin, stderr and a text stdout remapped.
    """
    projects = projects if projects is not None else UserIdMap.for_projects()
    out: list[dict[str, Any]] = []
    for call in calls:
        new = dict(call)
        values = decode_json_values(call["stdout"])
        if values is not None:
            new["stdout"] = "\n".join(
                json.dumps(
                    projects.apply_projects(ids.apply(v, forge=forge), forge=forge),
                    ensure_ascii=False,
                )
                for v in values
            )
        else:
            new["stdout"] = str(projects.apply_projects(call["stdout"], forge=forge))
        if isinstance(call.get("args"), list):
            new["args"] = projects.apply_projects(call["args"], forge=forge)
        for key in ("stdin", "stderr"):
            if isinstance(call.get(key), str):
                new[key] = str(projects.apply_projects(call[key], forge=forge))
        out.append(new)
    return out


def replace_known_in_calls(
    calls: list[dict[str, Any]], known: closure.Known
) -> list[dict[str, Any]]:
    """Replace every known value (D7) in every exchange's argv, stdin, stdout and stderr.

    Args:
        calls: The recorded exchanges, roles and synthetic ids already applied.
        known: The recording's known values (:func:`repoach.forge.recording.known_values`).

    Returns:
        New exchanges, each value replaced by its class token.
    """
    out: list[dict[str, Any]] = []
    for call in calls:
        new = dict(call)
        if isinstance(call.get("args"), list):
            new["args"] = replace_known_in_value(call["args"], known)
        new["stdout"] = replace_known_in_stdout(call["stdout"], known)
        for key in ("stdin", "stderr"):
            if isinstance(call.get(key), str):
                new[key] = replace_known(call[key], known)
        out.append(new)
    return out


@dataclass(frozen=True)
class RedactedRecording:
    """A recording after every stage of :func:`redact_recording`.

    Opaque in its ``repr``: it holds the known values, which a traceback must not show.

    Attributes:
        calls: The redacted exchanges.
        args: The method's positional arguments, redacted.
        kwargs: The method's keyword arguments, redacted.
        known: The recording's known values.
        ids: The user-id map, holding the synthetic numbers handed out.
        projects: The repository-id map.
    """

    calls: list[dict[str, Any]]
    args: list[Any]
    kwargs: dict[str, Any]
    known: closure.Known
    ids: UserIdMap
    projects: UserIdMap

    def __repr__(self) -> str:
        """A count, never a value."""
        return f"<redacted recording: {len(self.calls)} exchanges>"

    def normalize(self, text: str, roles: dict[str, str], *, forge: str) -> str:
        """One issued string as the fixture records it: every stage applied to it alone.

        Args:
            text: A string the replay issues.
            roles: The recording's login -> class word.
            forge: ``github`` or ``gitlab``.

        Returns:
            ``text`` classified, its URLs redacted, its repository ids remapped and its known
            values replaced.
        """
        value = classify_logins(redact_urls_in_value(text), roles)
        remapped = str(self.projects.apply_projects(value, forge=forge))
        return str(replace_known(remapped, self.known))


def redact_recording(
    calls: list[dict[str, Any]],
    args: list[Any],
    kwargs: dict[str, Any],
    *,
    raw: object,
    roles: dict[str, str],
    remote: str,
    denylist: Denylist,
    forge: str,
    method: str,
) -> RedactedRecording:
    """Run every redaction stage on one recording, in the one order the module docstring states.

    Args:
        calls: The recorded exchanges, their stdout redacted by field (and trimmed).
        args: The method's positional arguments as given.
        kwargs: The method's keyword arguments as given.
        raw: ``[calls, args, kwargs]`` as recorded, before any rewrite.
        roles: The recording's login -> class word.
        remote: The checkout's ``origin`` remote, ``""`` when it has none.
        denylist: The deny-list, opaque.
        forge: ``github`` or ``gitlab``.
        method: The recorded protocol method.

    Returns:
        The redacted exchanges and arguments, with the known values and the id maps.

    Raises:
        NeedleError: A structured value is a class word or a placeholder.
        SyntheticIdsExhaustedError: The recording needs a synthetic id past its range.
    """
    ids = UserIdMap()
    projects = UserIdMap.for_projects()
    calls = synthesize_user_ids(classify_calls(calls, roles), ids, forge=forge, projects=projects)
    args = list(
        projects.apply_projects(classify_logins(redact_urls_in_value(args), roles), forge=forge)
    )
    kwargs = dict(
        projects.apply_projects(classify_logins(redact_urls_in_value(kwargs), roles), forge=forge)
    )
    known = known_values(
        raw,
        roles=roles,
        remote=remote,
        denylist=denylist,
        ids=ids,
        projects=projects,
        forge=forge,
        method=method,
    )
    return RedactedRecording(
        calls=replace_known_in_calls(calls, known),
        args=list(replace_known_in_value(args, known)),
        kwargs=dict(replace_known_in_value(kwargs, known)),
        known=known,
        ids=ids,
        projects=projects,
    )


def seal_fixture(
    fixture: dict[str, Any],
    *,
    denylist: Denylist,
    known: closure.Known,
    users_in: object = None,
    sort_keys: bool = False,
) -> dict[str, Any]:
    """Declare a fixture's synthetic ids and judge it: the last step before any write, live and offline.

    Args:
        fixture: The redacted fixture; its declaration keys are replaced, at the end.
        denylist: The deny-list, opaque.
        known: The recording's known values.
        users_in: Where the user ids are read; the whole fixture when ``None``. The live
            recorder passes its exchanges, which every user id of its result comes from.
        sort_keys: Sort the fixture's top-level keys before judging (a committed fixture that
            was sorted stays so).

    Returns:
        The fixture with :data:`repoach.forge.recording.SYNTHETIC_IDS_KEY` and
        :data:`repoach.forge.recording.SYNTHETIC_PROJECT_IDS_KEY` declared when it holds any.

    Raises:
        UnclassifiedValueError: The judge refuses it (:func:`repoach.forge.fixture_judge.refuse_unclassified`).
    """
    out = dict(fixture)
    for key, declared in (
        (SYNTHETIC_IDS_KEY, declared_user_ids(out if users_in is None else users_in)),
        (SYNTHETIC_PROJECT_IDS_KEY, declared_project_ids(out)),
    ):
        out.pop(key, None)
        if declared:
            out[key] = declared
    if sort_keys:
        out = dict(sorted(out.items()))
    refuse_unclassified(out, denylist, known)
    return out


class ResidualValueError(Exception):
    """The oracle refuses a redacted fixture (D3, D4); the message names a reason, a kind, an ordinal and a path, never a value."""


FORGE_OF_DIRECTORY: dict[str, str] = {"github": "github", "gitlab": "gitlab", "gate": "github"}
"""The forge of a fixture that carries no ``forge`` key, by its directory.

``gate/`` holds read-only ``gh`` captures of this project's own pull
requests (``pr202_capture.json``: ``captured … read-only gh/git``).
"""


class UnknownForgeError(Exception):
    """A fixture names no forge and its directory is not in :data:`FORGE_OF_DIRECTORY`."""


def fixture_forge(path: Path, data: object) -> str:
    """The forge a fixture was recorded on: its ``forge`` key, else its directory's.

    Args:
        path: The fixture's file, whose directory names the forge when ``data`` does not.
        data: The fixture as committed.

    Returns:
        ``github`` or ``gitlab``.

    Raises:
        UnknownForgeError: Neither names a forge.
    """
    named = data.get("forge") if isinstance(data, dict) else None
    if isinstance(named, str) and named:
        return named
    forge = FORGE_OF_DIRECTORY.get(path.parent.name)
    if forge is None:
        raise UnknownForgeError(f"no forge for {path.parent.name}/{path.name}")
    return forge


def redact_output(stdout: str) -> str:
    """Redact one recorded stdout; an unchanged JSON answer keeps its bytes.

    Args:
        stdout: One recorded stdout.

    Returns:
        The stdout with its JSON values redacted by field, or its text URLs rewritten; the very
        same string when nothing changes.
    """
    values = decode_json_values(stdout)
    if values is None:
        return redact_urls_in_text(stdout)
    if [redact_payload(v) for v in values] == values:
        return stdout
    return redact_stdout(stdout)


def _exchanges(node: object) -> Any:
    """Redact every exchange-shaped object of ``node``, recursively."""
    if isinstance(node, dict):
        out = {k: _exchanges(v) for k, v in node.items()}
        if "returncode" in node and isinstance(node.get("stdout"), str):
            out["stdout"] = redact_output(node["stdout"])
            if isinstance(node.get("args"), list):
                out["args"] = redact_urls_in_value(node["args"])
            for key in ("stderr", "stdin"):
                if isinstance(node.get(key), str):
                    out[key] = redact_urls_in_text(node[key])
        return out
    if isinstance(node, list):
        return [_exchanges(v) for v in node]
    return node


def _answer_nodes(data: dict[str, object]) -> list[object]:
    """Every node of a fixture the roles read, in order: the fixture, then each JSON value inside it.

    A string is entered only when it is a JSON text, under a ``stdout`` key or in a list; every
    other node is listed, then its children, depth first.
    """
    nodes: list[object] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, str):
            if node.lstrip()[:1] in ("{", "["):
                stack[:0] = decode_json_values(node) or []
            continue
        nodes.append(node)
        if isinstance(node, dict):
            stack[:0] = [v for k, v in node.items() if not isinstance(v, str) or k == "stdout"]
        elif isinstance(node, list):
            stack[:0] = list(node)
    return nodes


def fixture_roles(data: dict[str, object], also: Sequence[str] = ()) -> dict[str, str]:
    """Login → class word for one committed fixture, through the one role builder.

    The fixture's own logins and mentions (:func:`_answer_nodes`,
    :func:`repoach.forge.recording.login_sightings`), then ``also``, go to
    :func:`repoach.forge.recording.assign_roles` with its ``bot_username`` as the bot. A
    committed fixture already holds class words, so a word it holds is taken, and ``author``
    is its change request's: a new login gets the next word the fixture lacks
    (``author-2`` first).

    Args:
        data: The fixture, its exchanges redacted by the convenience layer.
        also: The logins the raw answers carry under keys the redaction drops
            (:func:`repoach.forge.recording.raw_logins`), as the live recorder reads them.

    Returns:
        Each login (a mention-only one keyed ``@<login>``) mapped to its class word.
    """
    text = json.dumps(data)

    def _held(word: str) -> bool:
        return word == "author" or re.search(rf'"{re.escape(word)}\\*"', text) is not None

    sightings = login_sightings(_answer_nodes(data)) + [(login, False) for login in also]
    bot = data.get("bot_username")
    return assign_roles(sightings, bot=bot if isinstance(bot, str) else "", held=_held)


def _rename(node: object, roles: dict[str, str]) -> Any:
    """Replace ``roles`` logins under login keys and as mentions; JSON stdout is walked inside."""
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, item in node.items():
            if (key in LOGIN_KEYS or key == "bot_username") and isinstance(item, str):
                out[key] = roles.get(item, item)
            elif key == "stdout" and isinstance(item, str):
                values = decode_json_values(item)
                renamed = [_rename(v, roles) for v in (values or [])]
                out[key] = item if values is None or renamed == values else _dumps(renamed)
            elif key == "args" and isinstance(item, list):
                out[key] = [
                    rename_logins_in_argument(a, roles) if isinstance(a, str) else a for a in item
                ]
            elif key == "case" and isinstance(item, str):
                out[key] = item
            elif isinstance(item, str):
                out[key] = rename_logins_in_text(item, roles)
            else:
                out[key] = _rename(item, roles)
        return out
    if isinstance(node, list):
        return [_rename(v, roles) for v in node]
    return node


def _dumps(values: list[object]) -> str:
    """Serialise decoded stdout values as :func:`repoach.forge.recording.redact_stdout` does."""
    return "\n".join(json.dumps(v, ensure_ascii=False) for v in values)


def _remap_ids(node: object, ids: UserIdMap, forge: str, keep: frozenset[int]) -> Any:
    """Remap user ids in every exchange's JSON stdout; an unchanged answer keeps its bytes."""
    if isinstance(node, dict):
        out = {k: _remap_ids(v, ids, forge, keep) for k, v in node.items()}
        stdout = node.get("stdout")
        if isinstance(stdout, str) and "returncode" in node:
            values = decode_json_values(stdout)
            if values is not None:
                remapped = [ids.apply(v, forge=forge, keep=keep) for v in values]
                if remapped != values:
                    out["stdout"] = _dumps(remapped)
        return out
    if isinstance(node, list):
        return [_remap_ids(v, ids, forge, keep) for v in node]
    return node


def _remap_projects(node: object, ids: UserIdMap, forge: str, keep: frozenset[int]) -> Any:
    """Remap repository ids in every exchange and ``api/v4/projects/<id>`` URL (Q2 0.10, 0.13); unchanged JSON keeps its bytes."""
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for k, v in node.items():
            if k == "stdout" and isinstance(v, str) and "returncode" in node:
                values = decode_json_values(v)
                if values is not None:
                    remapped = [ids.apply_projects(val, forge=forge, keep=keep) for val in values]
                    out[k] = _dumps(remapped) if remapped != values else v
                else:
                    out[k] = ids.apply_projects(v, forge=forge, keep=keep)
            else:
                out[k] = _remap_projects(v, ids, forge, keep)
        return out
    if isinstance(node, list):
        if node and all(isinstance(v, str) for v in node):
            return ids.apply_projects(node, forge=forge, keep=keep)
        return [_remap_projects(v, ids, forge, keep) for v in node]
    if isinstance(node, str):
        return ids.apply_projects(node, forge=forge, keep=keep)
    return node


def _source(data: dict[str, object], forge: str) -> dict[str, object]:
    """``data`` with a recorded ``source`` head naming the forge, not the checkout's folder."""
    source = data.get("source")
    if not isinstance(source, str):
        return data
    rewritten = rewrite_recorded_source(source, forge)
    return data if rewritten == source else {**data, "source": rewritten}


def fixture_known(
    data: dict[str, object],
    *,
    roles: dict[str, str] | None = None,
    remote: str = "",
    denylist: Denylist | None = None,
    ids: UserIdMap | None = None,
    projects: UserIdMap | None = None,
    forge: str = "github",
) -> closure.Known:
    """The known values (D7) the offline transform can learn from one fixture's own structured fields.

    The fixture's exchanges, ``args`` and ``kwargs`` are the raw recording,
    and its ``method`` declares which positional arguments are prose;
    ``remote`` and ``denylist`` are the inputs the live recorder has and a
    committed fixture has not. Declared synthetic ids are never values.

    Args:
        data: The fixture as committed.
        roles: The fixture's login -> class word.
        remote: The checkout's ``origin`` remote, when the caller has one.
        denylist: The deny-list; the maintainer's
            (:func:`repoach.forge.denylist.read_denylist`) when ``None``, never an empty one.
        ids: The corpus's user-id map; a fresh one when ``None``.
        projects: The corpus's repository-id map; a fresh one when ``None``.
        forge: ``github`` or ``gitlab``.

    Returns:
        The fixture's known values (:func:`repoach.forge.recording.known_values`).

    Raises:
        NeedleError: A structured value that is a class word or placeholder, by kind and ordinal.
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list is malformed.
        SyntheticIdsExhaustedError: An id needs a synthetic number past its range.
    """
    raw = [data.get("calls"), data.get("args"), data.get("kwargs")]
    return known_values(
        raw,
        roles=roles or {},
        remote=remote,
        denylist=denylist,
        min_length=1,
        ids=ids,
        projects=projects,
        forge=forge,
        keep_users=trusted_declaration(data.get(SYNTHETIC_IDS_KEY)),
        keep_projects=trusted_declaration(
            data.get(SYNTHETIC_PROJECT_IDS_KEY),
            SYNTHETIC_PROJECT_ID_START,
            SYNTHETIC_PROJECT_ID_END,
        ),
        method=str(data.get("method") or ""),
    )


def _replace_known(node: object, known: closure.Known) -> Any:
    """Replace every known value in every string of a fixture; keys, ``forge`` and ``method`` are left."""
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, item in node.items():
            if key in ("forge", "method", "case"):
                out[key] = item
            elif key == "stdout" and isinstance(item, str) and "returncode" in node:
                out[key] = replace_known_in_stdout(item, known)
            elif key == "source" and isinstance(item, str):
                out[key] = replace_known_in_source(item, known)
            else:
                out[key] = _replace_known(item, known)
        return out
    if isinstance(node, list):
        return [_replace_known(v, known) for v in node]
    if isinstance(node, str):
        return replace_known(node, known)
    return node


def redact_fixture(
    data: dict[str, object],
    ids: UserIdMap | None = None,
    *,
    forge: str,
    projects: UserIdMap | None = None,
    remote: str = "",
    denylist: Denylist | None = None,
) -> dict[str, object]:
    """The class-redacted copy of one fixture: the live pipeline minus the forge (D4, 0.17).

    ``ids`` and ``projects`` are shared across the corpus. A fixture whose
    JSON nests deeper than the readers can walk is refused like the live
    recorder refuses it (0.21, B3), never crashed on. The deny-list fails
    closed: omitted, it is the maintainer's, as in :func:`redact_corpus` and
    :func:`repoach.forge.fixture_judge.refuse_unclassified`; a caller that
    means no deny-list passes an empty :class:`repoach.forge.denylist.Denylist`.

    Args:
        data: The fixture as committed.
        ids: The corpus's user-id map; a fresh one when ``None``.
        forge: ``github`` or ``gitlab``.
        projects: The corpus's repository-id map; a fresh one when ``None``.
        remote: The checkout's ``origin`` remote, when the caller has one.
        denylist: The deny-list; :func:`repoach.forge.denylist.read_denylist` when ``None``.

    Returns:
        The redacted fixture, judged by the closure oracle.

    Raises:
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list is malformed.
        SyntheticIdsExhaustedError: An id needs a synthetic number past its range.
        NeedleError: A structured value that is a class word or placeholder.
        ResidualValueError: The redacted fixture is refused by the closure oracle
            (:func:`repoach.forge.closure.judge`, through
            :func:`repoach.forge.fixture_judge.refuse_unclassified`): a known value, an
            unknown host, a public address, an unknown mailbox, a deny-list entry, a home
            path or an unclassified login; or ``nesting too deep``.
    """
    try:
        return _redact_fixture(
            data,
            ids,
            forge=forge,
            projects=projects,
            remote=remote,
            denylist=denylist if denylist is not None else read_denylist(),
        )
    except RecursionError as exc:
        raise ResidualValueError("nesting too deep: the fixture cannot be walked") from exc


def _redact_fixture(
    data: dict[str, object],
    ids: UserIdMap | None,
    *,
    forge: str,
    projects: UserIdMap | None,
    remote: str,
    denylist: Denylist,
) -> dict[str, object]:
    """:func:`redact_fixture` without its nesting guard."""
    ids = ids if ids is not None else UserIdMap()
    projects = projects if projects is not None else UserIdMap.for_projects()
    keep = trusted_declaration(data.get(SYNTHETIC_IDS_KEY))
    keep_projects = trusted_declaration(
        data.get(SYNTHETIC_PROJECT_IDS_KEY), SYNTHETIC_PROJECT_ID_START, SYNTHETIC_PROJECT_ID_END
    )
    out: dict[str, Any] = _exchanges(data)
    for key in ("args", "kwargs", "result"):
        if key in out:
            out[key] = redact_urls_in_value(out[key])
    roles = fixture_roles(out, also=raw_logins(data.get("calls")))
    if roles:
        out = _rename(out, roles)
    out = _remap_ids(out, ids, forge, keep)
    out = _remap_projects(out, projects, forge, keep_projects)
    known = fixture_known(
        data, roles=roles, remote=remote, denylist=denylist, ids=ids, projects=projects, forge=forge
    )
    out = _replace_known(out, known)
    method = out.get("method")
    if "result" in out and isinstance(method, str):
        out["result"] = ids.apply_result(out["result"], forge=forge, method=method, keep=keep)
    out = _source(out, forge)
    try:
        return seal_fixture(
            out, denylist=denylist, known=known, sort_keys=list(data) == sorted(data)
        )
    except UnclassifiedValueError as exc:
        raise ResidualValueError(str(exc)) from exc


def render(data: object, *, ascii_only: bool = False) -> str:
    """A fixture's text: two-space indent, key order kept, trailing newline.

    Args:
        data: The fixture.
        ascii_only: Escape every non-ASCII character, as the committed file did.

    Returns:
        The file's text.
    """
    return json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n"


def corpus_start(
    fixtures: list[dict[str, object]],
    key: str = SYNTHETIC_IDS_KEY,
    low: int = SYNTHETIC_ID_START,
    high: int = SYNTHETIC_ID_END,
) -> int:
    """The first number the corpus may hand out: after every trusted number a fixture declares.

    Args:
        fixtures: Every fixture of the corpus.
        key: The declaration key, ``synthetic_user_ids`` by default.
        low: The first number of the synthetic range.
        high: The last number of the synthetic range.

    Returns:
        One past the largest trusted declared number, ``low`` when none is declared.
    """
    declared = [n for data in fixtures for n in trusted_declaration(data.get(key), low, high)]
    return max([low - 1, *declared]) + 1


def redact_corpus(root: Path, *, check: bool, denylist: Denylist | None = None) -> list[Path]:
    """Redact every ``*.json`` under ``root``; return the files that changed (or would).

    Every file is transformed and judged by the closure oracle
    (:func:`fixture_known`, :func:`repoach.forge.closure.judge`)
    before any is written, so a refusal leaves the corpus as it was.

    Args:
        root: The fixture corpus.
        check: Write nothing; only say which files would change.
        denylist: The deny-list (:func:`repoach.forge.denylist.read_denylist` when ``None``).

    Returns:
        The files that changed, or would change under ``check``.

    Raises:
        UnknownForgeError: A fixture whose forge cannot be told.
        SyntheticIdsExhaustedError: The corpus needs a number past the synthetic range.
        NeedleError: A needle the oracle cannot judge.
        ResidualValueError: A redacted fixture still holds a needle.
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list is malformed.
    """
    deny = read_denylist() if denylist is None else denylist
    paths = sorted(root.rglob("*.json"))
    loaded = [(p, p.read_text(encoding="utf-8")) for p in paths]
    fixtures: list[dict[str, object]] = [json.loads(text) for _p, text in loaded]
    ids = UserIdMap(start=corpus_start(fixtures))
    projects = UserIdMap.for_projects(
        corpus_start(
            fixtures,
            SYNTHETIC_PROJECT_IDS_KEY,
            SYNTHETIC_PROJECT_ID_START,
            SYNTHETIC_PROJECT_ID_END,
        )
    )
    pending: list[tuple[Path, str]] = []
    for index, (path, text) in enumerate(loaded):
        data = fixtures[index]
        rel = path.relative_to(root).as_posix()
        try:
            new = redact_fixture(
                data,
                ids,
                forge=fixture_forge(path, data),
                projects=projects,
                denylist=deny,
            )
        except ResidualValueError as exc:
            reason, _sep, where = str(exc).rpartition(" at ")
            raise ResidualValueError(f"{reason} at {rel}:{where}" if _sep else str(exc)) from exc
        if new != data:
            pending.append((path, render(new, ascii_only=text.isascii())))
    if not check:
        for path, rendered in pending:
            path.write_text(rendered, encoding="utf-8")
    return [path for path, _rendered in pending]
