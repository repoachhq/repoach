"""The redaction pipeline of a forge recording, shared by the live recorder and the offline transform.

SP-RECORDER-REDACTS-BY-CLASS D4: the live recorder (:func:`record_live`) runs the
stages of :func:`redact_recording` on the exchanges it has just recorded, and
both it and the offline transform finish a fixture through
:func:`seal_fixture`, so the synthetic-id declarations and the last judge
before a write are one code path. The stages, in order: the roles are applied
and text URLs redacted (:func:`classify_calls`), user and repository ids are
made synthetic (:func:`synthesize_user_ids`), the known values are read from
the raw recording (:func:`repoach.forge.recording.known_values`) and replaced
everywhere (:func:`replace_known_in_calls`).

:func:`record_live` runs one read-only protocol method (PR #319 round 9, 4689482897): it checks
the method, collects the exchanges, the author and the ``origin`` remote (:func:`origin_url`),
redacts each stdout by field, builds the roles (:func:`build_roles`), runs the stages above,
replays the redacted exchanges, seals the fixture and writes it.
``scripts/record_forge_fixture.py`` holds its command line and exit codes only.

The offline transform of a committed fixture lives here too (:func:`redact_fixture`, and
:func:`redact_corpus` for a whole corpus), so ``scripts/redact_forge_fixtures.py`` holds its
command line only as well: the exchanges' URLs are redacted (:func:`redact_output`), the roles read through the one role
builder (:func:`fixture_roles`) and applied, the ids remapped, the known values read from the
fixture's own structured fields (:func:`fixture_known`) and replaced, and the fixture sealed by
:func:`seal_fixture`.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoach.core.denylist import Denylist, read_denylist
from repoach.core.logging import get_logger
from repoach.core.paths import NotARepositoryError
from repoach.forge import closure
from repoach.forge.fixture_judge import (
    UnclassifiedValueError,
    decoded_answers,
    refuse_unclassified,
)
from repoach.forge.fixture_reading import (
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
    decode_json_values,
    scanned_strings,
)
from repoach.forge.protocol import Forge, ForgeError
from repoach.forge.recording import (
    SYNTHETIC_ID_END,
    SYNTHETIC_ID_START,
    SYNTHETIC_PROJECT_ID_END,
    SYNTHETIC_PROJECT_ID_START,
    Exchange,
    RecordingSpawn,
    ReplayMismatchError,
    ReplaySpawn,
    UserIdMap,
    assign_roles,
    classify_logins,
    coerce_call,
    declared_project_ids,
    declared_user_ids,
    dump_fixture,
    holds_cut_url,
    known_values,
    login_sightings,
    raw_logins,
    recorded_source,
    redact_payload,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    replace_known,
    replace_known_in_source,
    replace_known_in_stdout,
    replace_known_in_value,
    rewrite_recorded_source,
    serialize_result,
    trusted_declaration,
)
from repoach.forge.select import select_forge

_log = get_logger(__name__)

WRITE_METHODS = frozenset(
    {
        "post_comment",
        "post_review",
        "reply",
        "upsert_marked_comment",
        "merge",
        "create_pull_request",
        "fetch_head",
        "git",
    }
)
"""The protocol methods that post, merge, fetch or push: never recorded."""


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


def cut_userinfo_path(recording: dict[str, Any]) -> str:
    """Where a recording holds a URL userinfo no URL reader reads whole (PR #319 round 10, 4683464677).

    Asked of every string right after the URL redaction and before a login, an id or a known value
    is replaced, live and offline alike, so no replacement can hide a userinfo from the last judge:
    ``redis://deploy:12/cd@<host>`` names a user until ``deploy`` is learnt as a host and written
    ``forge.example.com``. :func:`seal_fixture` refuses the fixture when it finds one.

    Args:
        recording: A fixture, or its ``calls``, ``args`` and ``kwargs``, the URLs of its JSON
            answers redacted; every other string is redacted here before it is judged.

    Returns:
        The path of the first string that holds one, ``""`` when none does.
    """
    for path, text in scanned_strings(recording):
        if holds_cut_url(text) or closure.has_cut_userinfo(redact_urls_in_text(text)):
            return path
    return ""


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
        cut_userinfo: Where the recording holds a cut userinfo (:func:`cut_userinfo_path`).
    """

    calls: list[dict[str, Any]]
    args: list[Any]
    kwargs: dict[str, Any]
    known: closure.Known
    ids: UserIdMap
    projects: UserIdMap
    cut_userinfo: str

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
    cut_userinfo = cut_userinfo_path({"calls": calls, "args": args, "kwargs": kwargs})
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
        cut_userinfo=cut_userinfo,
    )


def seal_fixture(
    fixture: dict[str, Any],
    *,
    denylist: Denylist,
    known: closure.Known,
    cut_userinfo: str,
    users_in: object = None,
    sort_keys: bool = False,
) -> dict[str, Any]:
    """Declare a fixture's synthetic ids and judge it: the last step before any write, live and offline.

    Args:
        fixture: The redacted fixture; its declaration keys are replaced, at the end.
        denylist: The deny-list, opaque.
        known: The recording's known values.
        cut_userinfo: Where the recording holds a userinfo cut by a URL reader
            (:func:`cut_userinfo_path`), ``""`` when nowhere.
        users_in: Where the user ids are read; the whole fixture when ``None``. The live
            recorder passes its exchanges, which every user id of its result comes from.
        sort_keys: Sort the fixture's top-level keys before judging (a committed fixture that
            was sorted stays so).

    Returns:
        The fixture with :data:`repoach.forge.fixture_reading.SYNTHETIC_IDS_KEY` and
        :data:`repoach.forge.fixture_reading.SYNTHETIC_PROJECT_IDS_KEY` declared when it holds any.

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
    if cut_userinfo:
        raise UnclassifiedValueError(f"url userinfo at {cut_userinfo}")
    return out


def _trim(value: object, limit: int) -> object:
    """Keep the first ``limit`` items of every list, recursively."""
    if isinstance(value, list):
        return [_trim(v, limit) for v in value[:limit]]
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    return value


def _trim_stdout(stdout: str, limit: int) -> str:
    """Trim a JSON stdout (one value, or ``--paginate`` pages merged) to ``limit`` items."""
    decoder = json.JSONDecoder()
    text = stdout.strip()
    values: list[object] = []
    pos = 0
    try:
        while pos < len(text):
            value, pos = decoder.raw_decode(text, pos)
            values.append(value)
            while pos < len(text) and text[pos].isspace():
                pos += 1
    except json.JSONDecodeError:
        return stdout
    if not values:
        return stdout
    merged: object = values[0]
    if len(values) > 1 and all(isinstance(v, list) for v in values):
        merged = [item for v in values for item in v if isinstance(v, list)]
    return json.dumps(_trim(merged, limit))


def build_roles(
    calls: list[dict[str, Any]],
    *,
    bot: str,
    maintainer: str | None,
    author: str | None,
    also: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    """Login → class word for one live recording, through the one role builder.

    The recorded answers are read in order (:func:`repoach.forge.fixture_judge.decoded_answers`,
    :func:`repoach.forge.recording.login_sightings`), then ``also``; the words are handed out by
    :func:`repoach.forge.recording.assign_roles`, which the offline transform shares.

    Args:
        calls: The recorded exchanges, as the forge printed them.
        bot: The configured bot login.
        maintainer: The login an ``is_maintainer`` call asks about, if any.
        author: The change request's author, if known.
        also: Further logins read outside the answers (the method's arguments).

    Returns:
        Each login (a mention-only one keyed ``@<login>``) mapped to its class word.
    """
    sightings = login_sightings(value for _path, value in decoded_answers(calls))
    sightings += [(login, False) for login in also]
    return assign_roles(sightings, bot=bot, maintainer=maintainer, author=author)


GIT_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "SYSTEMROOT",
)
"""The variables the recorder's own ``git`` child keeps: where ``git`` is and where its config lives.

``git remote get-url`` reads no credential, so a forge token, an API key, the maintainer's
deny-list (``REPOACH_PERSONAL_DENYLIST``) and an inline ``GIT_CONFIG_COUNT`` /
``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>`` value (an ``http.extraHeader`` carries a token)
never reach it, in a checkout whose config the recorder does not control. ``SYSTEMROOT`` lets
the child start on Windows.
"""


def git_env() -> dict[str, str]:
    """The minimal environment of the recorder's own ``git`` child.

    Returns:
        The :data:`GIT_ENV_KEYS` the process has, and ``GIT_TERMINAL_PROMPT=0`` so a checkout
        that would ask for a password fails instead of waiting.
    """
    env = {key: os.environ[key] for key in GIT_ENV_KEYS if key in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class FixtureWriteError(Exception):
    """The fixture cannot be written; the message names a reason, never a path.

    ``fixture not written (<OSError class>)``: a file stands where the output directory goes
    (``FileExistsError``), a permission, a full disk or any other ``OSError``.
    """


class OriginRemoteError(Exception):
    """The checkout's ``origin`` remote cannot be read; the message names a reason, never a path, a remote or git's output.

    ``origin remote: unreadable (<reason>)``, the reason being ``git missing``, ``timeout``,
    ``cannot run (<class>)`` (an ``OSError``, or ``NotARepositoryError`` when git or the checkout
    goes while the recording runs), ``not UTF-8``, ``empty answer`` or ``exit <n>`` (a refusal
    of git itself: a directory that is missing or no repository, a dubious owner, a broken
    configuration).
    """


def origin_url(cwd: Path) -> str:
    """The ``origin`` remote of the checkout at ``cwd``: a needle source, never written.

    ``git`` runs with :func:`git_env` only, the one subprocess the recorder starts itself; the
    forge's own ``gh`` / ``glab`` calls need its credentials and keep the environment. Reading
    fails closed (PR #319 round 9, 4689483340): a remote that could not be read would leave its
    host, namespace and repository in the recording. "No such remote" is a fact git reports in
    every locale and version: ``git remote get-url`` fails, and ``git remote`` succeeds and lists
    no ``origin``.

    Args:
        cwd: The checkout whose forge is recorded.

    Returns:
        The remote as ``git`` prints it, ``""`` when git reports that there is none.

    Raises:
        OriginRemoteError: ``git`` cannot run, times out, fails otherwise or prints nothing.
    """
    reason, printed = _git_output(cwd, "remote", "get-url", "origin")
    if not reason and printed.strip():
        return printed.strip()
    if not reason:
        reason = "empty answer"
    elif reason.startswith("exit "):
        listing, names = _git_output(cwd, "remote")
        if not listing and "origin" not in names.split():
            _log.info("recorder.no_origin")
            return ""
    _log.warning("recorder.origin_unreadable", reason=reason)
    raise OriginRemoteError(f"origin remote: unreadable ({reason})")


def _git_output(cwd: Path, *argv: str) -> tuple[str, str]:
    """Why ``git -C <cwd> <argv>`` failed (``""`` when it did not) and what it printed.

    Never raises, and the reason holds neither a path nor a line of git's output.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env=git_env(),
        )
    except FileNotFoundError as exc:
        _log.debug("recorder.git_failed", error=type(exc).__name__)
        return "git missing", ""
    except subprocess.TimeoutExpired as exc:
        _log.debug("recorder.git_failed", error=type(exc).__name__)
        return "timeout", ""
    except UnicodeDecodeError as exc:
        _log.debug("recorder.git_failed", error=type(exc).__name__)
        return "not UTF-8", ""
    except OSError as exc:
        _log.debug("recorder.git_failed", error=type(exc).__name__)
        return f"cannot run ({type(exc).__name__})", ""
    if done.returncode != 0:
        return f"exit {done.returncode}", ""
    return "", done.stdout


def _author_of(forge: Forge, method: str, args: list[Any]) -> str | None:
    """The change request's author, read without recording, when ``args[0]`` is its number."""
    if not args or not isinstance(args[0], int) or isinstance(args[0], bool):
        return None
    if method == "is_maintainer":
        return None
    try:
        return forge.pull_request(args[0]).author or None
    except ForgeError as exc:
        _log.info("recorder.author_unknown", error=type(exc).__name__)
        return None


def _forge_of(forge_name: str, cwd: Path, bot: str | None = None) -> Forge:
    """The forge of ``cwd`` (:func:`repoach.forge.select.select_forge`), its ``git`` failures named.

    Selecting a forge runs ``git`` in ``cwd`` too, and its error prints the checkout's path.

    Raises:
        OriginRemoteError: ``git`` or ``cwd`` went after the remote was read.
        ForgeError: The forge cannot be selected.
        repoach.project.config.ConfigError: ``repoach.toml`` or an environment value is invalid.
    """
    try:
        return select_forge(override=forge_name, cwd=cwd, bot_username=bot)
    except NotARepositoryError as exc:
        reason = f"cannot run ({type(exc).__name__})"
        _log.warning("recorder.origin_unreadable", reason=reason)
    raise OriginRemoteError(f"origin remote: unreadable ({reason})")


def _replay(
    forge_name: str,
    cwd: Path,
    method: str,
    typed_args: list[Any],
    typed_kwargs: dict[str, Any],
    calls: list[dict[str, Any]],
    bot: str | None = None,
    normalize: Callable[[str], str] | None = None,
) -> tuple[object, str | None]:
    """Recompute the result from the redacted exchanges so the fixture stays self-consistent."""
    forge = _forge_of(forge_name, cwd, bot)
    forge._spawn = ReplaySpawn([Exchange.from_json(c) for c in calls], normalize)
    try:
        return serialize_result(getattr(forge, method)(*typed_args, **typed_kwargs)), None
    except ForgeError as exc:
        return None, type(exc).__name__


def record_live(
    *,
    forge_name: str,
    cwd: Path,
    case: str,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
    trim: int | None,
    out_root: Path,
) -> Path:
    """Run one read-only protocol method against the forge, redact its exchanges and write the fixture.

    Every refusal names a reason and a path, never a value, and writes
    nothing; none is ever an uncaught crash (0.21, B3). A recording whose JSON nests deeper than the readers can walk (a
    value nested hundreds of levels, which no forge answers) is refused
    like any other value the recorder cannot classify: the readers'
    ``RecursionError`` becomes a named refusal, nothing is written.

    Args:
        forge_name: ``github`` or ``gitlab``.
        cwd: The checkout whose forge is recorded.
        case: The fixture's file name, without ``.json``.
        method: The protocol method; a write method is refused.
        args: Its positional arguments.
        kwargs: Its keyword arguments.
        trim: Keep only the first ``trim`` items of every recorded JSON array.
        out_root: The fixture root; the file is ``<out_root>/<forge>/<case>.json``.

    Returns:
        The fixture path written.

    Raises:
        SystemExit: A write method, a name that is no protocol method, or a redacted replay
            that disagrees with the live call (it raises otherwise, or issues a call the
            recording does not hold); nothing is written and the message names no value.
        TypeError: ``args`` or ``kwargs`` do not fit the method's signature, raised by the
            live call before anything is recorded.
        ValueError: The method refuses an argument or cannot read an answer (an answer that is
            no UTF-8 or no JSON, a model the answer does not fit); nothing is written.
        OSError: The forge's CLI is installed and cannot be started; nothing is written.
        UnclassifiedValueError: Anything the judge refuses, ``nesting too deep`` included.
        DenylistError: The maintainer's deny-list cannot be judged with: its file exists but
            cannot be read (a dangling symlink, a directory, a permission, an encoding), an
            entry is too short, or the variable holds a comma.
        OriginRemoteError: The checkout's ``origin`` remote cannot be read: ``git`` is missing,
            ``cwd`` is missing or no repository, or git refuses it. Read first, before the forge
            is selected, so nothing runs against the forge.
        FixtureWriteError: The fixture cannot be written under ``out_root``.
        SyntheticIdsExhaustedError: The recording needs a synthetic id past its range.
        NeedleError: A structured value is a class word or a placeholder.
        ForgeError: The forge cannot be selected: ``ForgeUnavailableError`` when its CLI is not
            installed.
        repoach.project.config.ConfigError: The checkout's ``repoach.toml`` or an environment
            value is invalid.
    """
    try:
        return _record(
            forge_name=forge_name,
            cwd=cwd,
            case=case,
            method=method,
            args=args,
            kwargs=kwargs,
            trim=trim,
            out_root=out_root,
        )
    except RecursionError as exc:
        raise UnclassifiedValueError("nesting too deep: the recording cannot be walked") from exc


def _record(
    *,
    forge_name: str,
    cwd: Path,
    case: str,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
    trim: int | None,
    out_root: Path,
) -> Path:
    """:func:`record_live` without its nesting guard: check, collect, redact, replay, seal, write."""
    _refuse_unrecordable(method)
    live = _collect(forge_name=forge_name, cwd=cwd, method=method, args=args, kwargs=kwargs)
    calls = _redact_by_field(live.calls, trim)
    maintainer = str(args[0]) if method == "is_maintainer" and args else None
    roles = build_roles(
        calls,
        bot=live.forge.bot_username,
        maintainer=maintainer,
        author=live.author,
        also=raw_logins(live.raw),
    )
    denylist = read_denylist()
    redacted = redact_recording(
        calls,
        args,
        kwargs,
        raw=live.raw,
        roles=roles,
        remote=live.remote,
        denylist=denylist,
        forge=forge_name,
        method=method,
    )
    bot = roles.get(live.forge.bot_username, live.forge.bot_username)
    result, raises = _replay_redacted(forge_name, cwd, method, redacted, roles, bot, live.raises)
    fixture: dict[str, Any] = {
        "forge": forge_name,
        "case": case,
        "method": method,
        "args": redacted.args,
        "kwargs": redacted.kwargs,
        "bot_username": bot,
        "source": recorded_source(datetime.now(UTC).date().isoformat(), forge_name),
        "trimmed": trim,
        "calls": redacted.calls,
        "result": result,
        "raises": raises,
    }
    fixture = seal_fixture(
        fixture,
        denylist=denylist,
        known=redacted.known,
        cut_userinfo=redacted.cut_userinfo,
        users_in=redacted.calls,
    )
    path = out_root / forge_name / f"{case}.json"
    _write(path, fixture)
    return path


def _write(path: Path, fixture: dict[str, Any]) -> None:
    """Write the sealed fixture.

    Raises:
        FixtureWriteError: Any ``OSError`` of the write, by its class.
    """
    try:
        dump_fixture(path, fixture)
    except OSError as exc:
        reason = type(exc).__name__
        _log.warning("recorder.not_written", error=reason)
    else:
        return
    raise FixtureWriteError(f"fixture not written ({reason})")


def _refuse_unrecordable(method: str) -> None:
    """Refuse a write method and a name that is no protocol method, before anything runs.

    Raises:
        SystemExit: Naming the method, which is the caller's own argument.
    """
    if method in WRITE_METHODS:
        raise SystemExit(f"refusing to record write method {method!r}: fixtures never post")
    if method.startswith("_") or not inspect.isfunction(getattr(Forge, method, None)):
        raise SystemExit(f"refusing to record {method!r}: no protocol method has that name")


@dataclass(frozen=True)
class _Live:
    """What one live call left: the forge, its exchanges, the raw recording and the inputs read beside it."""

    forge: Forge
    calls: list[dict[str, Any]]
    raw: list[object]
    raises: str | None
    remote: str
    author: str | None

    def __repr__(self) -> str:
        """A count, never a value."""
        return f"<live call: {len(self.calls)} exchanges>"


def _collect(
    *, forge_name: str, cwd: Path, method: str, args: list[Any], kwargs: dict[str, Any]
) -> _Live:
    """Read the remote, then run the method once against the forge, recording every exchange; read the author.

    Raises:
        TypeError: ``args`` or ``kwargs`` do not fit the method's signature.
        OriginRemoteError: The ``origin`` remote cannot be read; the forge is never selected.
        ForgeError: The forge cannot be selected.
    """
    typed_args, typed_kwargs = coerce_call(method, args, kwargs)
    remote = origin_url(cwd)
    forge = _forge_of(forge_name, cwd)
    author = _author_of(forge, method, args)
    recorder = RecordingSpawn(inner=forge._spawn)
    forge._spawn = recorder
    raises: str | None = None
    try:
        getattr(forge, method)(*typed_args, **typed_kwargs)
    except ForgeError as exc:
        raises = type(exc).__name__
    calls = [e.to_json() for e in recorder.exchanges]
    raw: list[object] = [copy.deepcopy(calls), copy.deepcopy(args), copy.deepcopy(kwargs)]
    return _Live(forge, calls, raw, raises, remote, author)


def _redact_by_field(calls: list[dict[str, Any]], trim: int | None) -> list[dict[str, Any]]:
    """Each exchange with its stdout redacted by field, then trimmed to ``trim`` items."""
    out = []
    for call in calls:
        stdout = redact_stdout(call["stdout"])
        out.append({**call, "stdout": stdout if trim is None else _trim_stdout(stdout, trim)})
    return out


def _replay_redacted(
    forge_name: str,
    cwd: Path,
    method: str,
    redacted: RedactedRecording,
    roles: dict[str, str],
    bot: str,
    live_raises: str | None,
) -> tuple[object, str | None]:
    """The result replayed from the redacted exchanges; it must agree with the live call.

    Raises:
        SystemExit: The replay issues a call the recording does not hold, or raises otherwise.
    """
    typed_args, typed_kwargs = coerce_call(method, redacted.args, redacted.kwargs)

    def _normalize(text: str) -> str:
        """The issued text as the fixture records it: the pipeline applied to one string."""
        return redacted.normalize(text, roles, forge=forge_name)

    disagrees = "refusing to write a fixture whose redacted replay disagrees with the live call"
    try:
        result, raises = _replay(
            forge_name, cwd, method, typed_args, typed_kwargs, redacted.calls, bot, _normalize
        )
    except ReplayMismatchError as exc:
        raise SystemExit(disagrees) from exc
    if raises != live_raises:
        raise SystemExit(disagrees)
    return result, raises


class ResidualValueError(Exception):
    """The offline transform refuses a fixture (D3, D4); the message names a reason, a kind, an ordinal and a path, never a value.

    The reasons are the judge's (:class:`repoach.forge.fixture_judge.UnclassifiedValueError`:
    ``unclassified``, ``secret-shaped value``, ``generic class home_path``, ``deny-list entry``
    and every refusal of the closure oracle), and ``nesting too deep`` (a fixture deeper than
    the readers can walk); :func:`redact_corpus` adds the fixture's path to the location.
    """


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
    """Apply the roles to a committed fixture: the live rule on every string, the fixture's shape kept.

    :func:`repoach.forge.recording.classify_logins` is the one rule, live and offline (PR #319
    round 10, 4683464072): a login key is looked up casefolded, any other string, an argument
    included, has each login replaced as a token. Only the walk is the offline transform's own,
    since a committed fixture is more than the exchanges :func:`classify_calls` reads: its
    exchanges may stand anywhere (a ``gate/`` capture), it holds a ``result`` and a
    ``bot_username`` to rename, and ``case`` and an ``args`` member that is no string stay as
    they stand (the judge refuses a login there).
    """
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, item in node.items():
            if key == "stdout" and isinstance(item, str):
                values = decode_json_values(item)
                renamed = classify_logins(item if values is None else values, roles)
                out[key] = renamed if values is None else _dumps(renamed)
            elif key == "args" and isinstance(item, list):
                out[key] = [classify_logins(a, roles) if isinstance(a, str) else a for a in item]
            elif key == "case" and isinstance(item, str):
                out[key] = item
            elif key == "bot_username":
                out[key] = classify_logins({"login": item}, roles)["login"]
            elif isinstance(item, str):
                out[key] = classify_logins({key: item}, roles)[key]
            else:
                out[key] = _rename(item, roles)
        return out
    if isinstance(node, list):
        return [_rename(v, roles) for v in node]
    if isinstance(node, str):
        return classify_logins(node, roles)
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
            (:func:`repoach.core.denylist.read_denylist`) when ``None``, never an empty one.
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
    means no deny-list passes an empty :class:`repoach.core.denylist.Denylist`.

    Args:
        data: The fixture as committed.
        ids: The corpus's user-id map; a fresh one when ``None``.
        forge: ``github`` or ``gitlab``.
        projects: The corpus's repository-id map; a fresh one when ``None``.
        remote: The checkout's ``origin`` remote, when the caller has one.
        denylist: The deny-list; :func:`repoach.core.denylist.read_denylist` when ``None``.

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
    cut_userinfo = cut_userinfo_path(out)
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
            out,
            denylist=denylist,
            known=known,
            cut_userinfo=cut_userinfo,
            sort_keys=list(data) == sorted(data),
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
        denylist: The deny-list (:func:`repoach.core.denylist.read_denylist` when ``None``).

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
