#!/usr/bin/env python3
"""Record one forge conformance fixture against a real PR / MR (read-only).

SP-UNIVERSAL-FORGE A1: when ``gh`` or ``glab`` changes, re-record the
fixtures under ``tests/fixtures/forge/<forge>/`` against a real change
request so the conformance suite keeps pinning what the CLI actually
prints. The script runs ONE protocol method on the forge selected for
``--cwd`` and writes the subprocess exchanges it made plus its
normalised result::

    scripts/record_forge_fixture.py --forge gitlab --cwd ../some-checkout \\
        --case pull_request --method pull_request --args '[283]'

Write methods are refused: a recording must never post, merge or open
anything. Their fixtures are written by hand from the forge's documented
payloads and say so in their ``source`` field.

Every JSON answer is redacted at record time: only the keys the
forges read (:data:`repoach.forge.recording.RECORDED_FIELDS`) are kept,
and a fixture in which a token- or email-shaped value survives is refused.

SP-RECORDER-REDACTS-BY-CLASS: personal keys, a user object's ``name``
and URLs are dropped, every URL and scp-style remote the convenience layer
recognises gets the placeholder host, namespace and repository
(:func:`repoach.forge.recording.redact_url`,
``forge.example.com/org/project``), and every login becomes its class
word in stdout, argv and the method's arguments: the bot ``bot``, the
account an ``is_maintainer`` call asks about ``maintainer``, the change
request's author ``author``, any other ``[bot]`` login ``bot-<n>``, a
GitLab project bot ``service`` and any other login ``author-<n>``; a
login known only from an ``@<handle>`` in free text is ``author-<n>`` in
that form only, and a decorator or a dotted name is no handle; the
:data:`repoach.forge.recording.NON_PERSON_LOGINS` are kept. Every user
id becomes a synthetic one in ``1001..1999``
(:class:`repoach.forge.recording.UserIdMap`), declared under
``synthetic_user_ids``.

Every private value the recording holds in a structured field, and the
hosts the oracle reads in any string, is a known value
(:func:`repoach.forge.recording.known_values`); each is replaced in every
string by its class token (:func:`repoach.forge.recording.replace_known`),
whatever shape it stands in. The result is then recomputed by replaying
the redacted exchanges, so the fixture is what the readers return from it.
The whole fixture, its ``source`` included, is judged last: a login left
unclassified, a home path, an entry of the maintainer's deny-list or
anything the closure oracle (:func:`repoach.forge.closure.judge`)
refuses (a known value, a host that is neither a placeholder nor public,
a public address, a mailbox that is neither a placeholder nor a
documentation literal) exits 2 naming a reason, a kind, an ordinal and
the key path, never the value, and writes nothing. ``source`` names the
forge and the date, never the checkout's folder. Logs go to stderr;
stdout is the fixture's path or nothing.

``--trim N`` keeps only the first ``N`` items of every JSON array in
recorded stdout (and of the recorded result), for listings too large to
commit; the fixture records that it was trimmed.
"""

from __future__ import annotations

import argparse
import copy
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoach.core.logging import get_logger, route_default_logs_to_stderr
from repoach.forge import closure
from repoach.forge.denylist import DenylistError, read_denylist
from repoach.forge.fixture_judge import (
    UnclassifiedValueError,
    decoded_answers,
    refuse_unclassified,
)
from repoach.forge.protocol import Forge, ForgeError
from repoach.forge.recording import (
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
    Exchange,
    NeedleError,
    RecordingSpawn,
    ReplayMismatchError,
    ReplaySpawn,
    SyntheticIdsExhaustedError,
    UserIdMap,
    assign_roles,
    classify_logins,
    coerce_call,
    declared_project_ids,
    declared_user_ids,
    decode_json_values,
    dump_fixture,
    known_values,
    login_sightings,
    raw_logins,
    recorded_source,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    replace_known,
    replace_known_in_stdout,
    replace_known_in_value,
    serialize_result,
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

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"


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


def classify_calls(calls: list[dict[str, Any]], roles: dict[str, str]) -> list[dict[str, Any]]:
    """Apply ``roles`` to every exchange's argv, stdin, stdout and stderr; redact text URLs."""
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
    """Replace every user id, and every repository id (Q2 0.10, ``api/v4/projects/<id>`` 0.13), of the recorded answers by a synthetic one (D1c)."""
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
    """Replace every known value (D7) in every exchange's argv, stdin, stdout and stderr."""
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


def origin_url(cwd: Path) -> str:
    """The ``origin`` remote of the checkout at ``cwd``: a needle source, never written.

    ``git`` runs with :func:`git_env` only, the one subprocess the recorder starts itself; the
    forge's own ``gh`` / ``glab`` calls need its credentials and keep the environment.

    Args:
        cwd: The checkout whose forge is recorded.

    Returns:
        The remote as ``git`` prints it, ``""`` when there is none or ``git`` cannot run.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.info("recorder.no_origin", error=type(exc).__name__)
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


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
    forge = select_forge(override=forge_name, cwd=cwd, bot_username=bot)
    forge._spawn = ReplaySpawn([Exchange.from_json(c) for c in calls], normalize)
    try:
        return serialize_result(getattr(forge, method)(*typed_args, **typed_kwargs)), None
    except ForgeError as exc:
        return None, type(exc).__name__


def record(
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
        UnclassifiedValueError: Anything the judge refuses, ``nesting too deep`` included.
        DenylistError: The maintainer's deny-list is malformed (an entry too short, a comma).
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
    """:func:`record` without its nesting guard."""
    if method in WRITE_METHODS:
        raise SystemExit(f"refusing to record write method {method!r}: fixtures never post")
    if method.startswith("_") or not inspect.isfunction(getattr(Forge, method, None)):
        raise SystemExit(f"refusing to record {method!r}: no protocol method has that name")
    typed_args, typed_kwargs = coerce_call(method, args, kwargs)
    forge = select_forge(override=forge_name, cwd=cwd)
    author = _author_of(forge, method, args)
    recorder = RecordingSpawn(inner=forge._spawn)
    forge._spawn = recorder
    live_raises: str | None = None
    try:
        getattr(forge, method)(*typed_args, **typed_kwargs)
    except ForgeError as exc:
        live_raises = type(exc).__name__
    calls = [e.to_json() for e in recorder.exchanges]
    remote = origin_url(cwd)
    raw: list[object] = [copy.deepcopy(calls), copy.deepcopy(args), copy.deepcopy(kwargs)]
    for call in calls:
        call["stdout"] = redact_stdout(call["stdout"])
    if trim is not None:
        for call in calls:
            call["stdout"] = _trim_stdout(call["stdout"], trim)
    maintainer = str(args[0]) if method == "is_maintainer" and args else None
    roles = build_roles(
        calls,
        bot=forge.bot_username,
        maintainer=maintainer,
        author=author,
        also=raw_logins(raw),
    )
    denylist = read_denylist()
    calls = classify_calls(calls, roles)
    ids = UserIdMap()
    projects = UserIdMap.for_projects()
    calls = synthesize_user_ids(calls, ids, forge=forge_name, projects=projects)
    args = list(
        projects.apply_projects(
            classify_logins(redact_urls_in_value(args), roles), forge=forge_name
        )
    )
    kwargs = dict(
        projects.apply_projects(
            classify_logins(redact_urls_in_value(kwargs), roles), forge=forge_name
        )
    )
    known = known_values(
        raw,
        roles=roles,
        remote=remote,
        denylist=denylist,
        ids=ids,
        projects=projects,
        forge=forge_name,
        method=method,
    )
    calls = replace_known_in_calls(calls, known)
    args = list(replace_known_in_value(args, known))
    kwargs = dict(replace_known_in_value(kwargs, known))
    bot = roles.get(forge.bot_username, forge.bot_username)
    typed_args, typed_kwargs = coerce_call(method, args, kwargs)

    def _normalize(text: str) -> str:
        """The issued text as the fixture records it: the pipeline applied to one string."""
        value = classify_logins(redact_urls_in_value(text), roles)
        return str(replace_known(str(projects.apply_projects(value, forge=forge_name)), known))

    disagrees = "refusing to write a fixture whose redacted replay disagrees with the live call"
    try:
        result, raises = _replay(
            forge_name, cwd, method, typed_args, typed_kwargs, calls, bot, _normalize
        )
    except ReplayMismatchError as exc:
        raise SystemExit(disagrees) from exc
    if raises != live_raises:
        raise SystemExit(disagrees)
    fixture: dict[str, Any] = {
        "forge": forge_name,
        "case": case,
        "method": method,
        "args": args,
        "kwargs": kwargs,
        "bot_username": bot,
        "source": recorded_source(datetime.now(UTC).date().isoformat(), forge_name),
        "trimmed": trim,
        "calls": calls,
        "result": result,
        "raises": raises,
    }
    declared = declared_user_ids(calls)
    if declared:
        fixture[SYNTHETIC_IDS_KEY] = declared
    declared_projects = declared_project_ids(fixture)
    if declared_projects:
        fixture[SYNTHETIC_PROJECT_IDS_KEY] = declared_projects
    refuse_unclassified(fixture, denylist, known)
    path = out_root / forge_name / f"{case}.json"
    dump_fixture(path, fixture)
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one line on stdout, the fixture's path, or nothing and exit 2 on a refusal."""
    route_default_logs_to_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--forge", required=True, choices=["github", "gitlab"])
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--case", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--args", default="[]", help="JSON list of positional arguments")
    parser.add_argument("--kwargs", default="{}", help="JSON object of keyword arguments")
    parser.add_argument("--trim", type=int, default=None)
    parser.add_argument("--out", type=Path, default=FIXTURE_ROOT)
    ns = parser.parse_args(argv)
    try:
        path = record(
            forge_name=ns.forge,
            cwd=ns.cwd,
            case=ns.case,
            method=ns.method,
            args=json.loads(ns.args),
            kwargs=json.loads(ns.kwargs),
            trim=ns.trim,
            out_root=ns.out,
        )
    except (
        UnclassifiedValueError,
        DenylistError,
        SyntheticIdsExhaustedError,
        NeedleError,
        ForgeError,
    ) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
