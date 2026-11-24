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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from repoach.forge import closure
from repoach.forge.denylist import Denylist
from repoach.forge.fixture_judge import refuse_unclassified
from repoach.forge.recording import (
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
    UserIdMap,
    classify_logins,
    declared_project_ids,
    declared_user_ids,
    decode_json_values,
    known_values,
    redact_urls_in_text,
    redact_urls_in_value,
    replace_known,
    replace_known_in_stdout,
    replace_known_in_value,
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
