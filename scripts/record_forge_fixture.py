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
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repoach.core.logging import get_logger, route_default_logs_to_stderr
from repoach.core.paths import user_config_dir
from repoach.forge import closure
from repoach.forge.protocol import Forge, ForgeError
from repoach.forge.recording import (
    CONSTANT_FIELDS,
    GITLAB_PROJECT_BOT_RE,
    MENTION_ONLY_PREFIX,
    NON_PERSON_LOGINS,
    RECORDED_SOURCE_HEAD_RE,
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_IDS_KEY,
    Exchange,
    NeedleError,
    RecordingSpawn,
    ReplaySpawn,
    SyntheticIdsExhaustedError,
    UserIdMap,
    bot_stem,
    classify_logins,
    coerce_call,
    declared_project_ids,
    declared_user_ids,
    decode_json_values,
    dump_fixture,
    find_secret_tokens,
    is_classified_login,
    known_values,
    login_values,
    mentions,
    raw_logins,
    recorded_source,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    replace_known,
    replace_known_in_stdout,
    replace_known_in_value,
    scanned_strings,
    serialize_result,
)
from repoach.forge.select import select_forge

_log = get_logger(__name__)

HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_$.-])/(home|Users)/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])")
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``home_path`` (that lint lands later; not imported)."""

DENYLIST_ENV = "REPOACH_PERSONAL_DENYLIST"
"""SP-NO-PERSONAL-DATA-GATE D3: the deny-list in CI, newline- or comma-separated."""

DENYLIST_FILE_NAME = "personal-denylist"
"""SP-NO-PERSONAL-DATA-GATE D3: the deny-list file under ``user_config_dir()``, one entry per line."""

DENYLIST_MIN_LENGTH = 4
"""SP-NO-PERSONAL-DATA-GATE D3: a shorter entry would match everywhere and is refused."""


class UnclassifiedValueError(Exception):
    """A recorded value is a login with no class word, or matches a generic personal class.

    The message carries the key path, never the value.
    """


class DenylistError(Exception):
    """The deny-list holds an entry too short to judge with; the message names its ordinal only."""


def denylist_file(config_dir: Path | None = None) -> Path:
    """The deny-list file: ``personal-denylist`` under ``config_dir``, by default ``user_config_dir()``."""
    return (config_dir if config_dir is not None else user_config_dir()) / DENYLIST_FILE_NAME


class Denylist:
    """The maintainer's deny-list entries, opaque (K8, 0.10).

    Its ``repr`` is a count, so neither a log, a traceback nor ``pytest -l``
    can show an entry; a caller asks :meth:`hit` for an ordinal and never
    binds an entry or a pattern to a name of its own.
    """

    __slots__ = ("_entries", "_patterns")

    def __init__(self, entries: tuple[str, ...] = ()) -> None:
        self._entries = entries
        self._patterns = denylist_patterns(list(entries))

    def __repr__(self) -> str:
        """A count, never an entry."""
        return f"<denylist: {len(self._entries)} entries>"

    def __len__(self) -> int:
        """How many entries."""
        return len(self._entries)

    def hit(self, text: str) -> int | None:
        """The ordinal of the first entry ``text`` holds (:func:`denylist_hit`), or ``None``."""
        return denylist_hit(text, self._patterns)

    def needle_values(self) -> tuple[str, ...]:
        """The entries, for :func:`repoach.forge.recording.known_values` only; never printed."""
        return self._entries


def read_denylist(config_dir: Path | None = None) -> Denylist:
    """The maintainer's deny-list, from the file and the environment merged (SP-NO-PERSONAL-DATA-GATE D3).

    A copy of that gate's reader (it lands later; not imported): the file
    :func:`denylist_file`, one entry per line with ``#`` comments, and
    :data:`DENYLIST_ENV`, newline- or comma-separated. Both live outside
    the tree; no entry is ever printed or written.

    Args:
        config_dir: The configuration directory holding the file; ``None``
            reads ``user_config_dir()``. The test suite passes the real
            directory, captured before it points ``XDG_CONFIG_HOME`` at a
            scratch one.

    Returns:
        The entries in order, duplicates removed, as an opaque
        :class:`Denylist`; empty when neither source exists.

    Raises:
        DenylistError: An entry shorter than :data:`DENYLIST_MIN_LENGTH`, by ordinal.
    """
    raw: list[str] = []
    path = denylist_file(config_dir)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            raw.append(line.split("#", 1)[0])
    raw += re.split(r"[\n,]", os.environ.get(DENYLIST_ENV, ""))
    entries: list[str] = []
    for item in (r.strip() for r in raw):
        if item and item not in entries:
            entries.append(item)
    for n, entry in enumerate(entries, 1):
        if len(entry) < DENYLIST_MIN_LENGTH:
            raise DenylistError(f"deny-list entry {n}: too short")
    return Denylist(tuple(entries))


def denylist_patterns(entries: list[str]) -> list[re.Pattern[str]]:
    """SP-NO-PERSONAL-DATA-GATE D1 ``denylist``: each entry a whole token (:func:`repoach.forge.closure.token_pattern`)."""
    return [closure.token_pattern(e) for e in entries]


def denylist_hit(text: str, patterns: list[re.Pattern[str]]) -> int | None:
    """The ordinal of the first deny-list entry ``text`` holds, or ``None``.

    ``text`` is judged in every form the closure oracle judges it in
    (:func:`repoach.forge.closure.forms_of`), literals kept: decoded, casefolded
    and with the account number of every node id it holds, wherever the
    node id stands, so an entry holding a numeric user id catches its node ids.
    """
    forms = closure.forms_of(text)
    for n, pattern in enumerate(patterns, 1):
        if any(pattern.search(form) for form in forms):
            return n
    return None


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


def generic_class(text: str) -> str | None:
    """``home_path`` when ``text`` holds a home directory path, else ``None``."""
    return "home_path" if HOME_PATH_RE.search(text) else None


def _decoded(calls: list[dict[str, Any]]) -> list[tuple[str, object]]:
    """``(path, decoded stdout value)`` of every JSON answer, in recording order."""
    out: list[tuple[str, object]] = []
    for i, call in enumerate(calls):
        values = decode_json_values(call["stdout"]) or []
        for k, value in enumerate(values):
            out.append((f"exchanges[{i}].stdout" + (f"[{k}]" if len(values) > 1 else ""), value))
    return out


def build_roles(
    calls: list[dict[str, Any]],
    *,
    bot: str,
    maintainer: str | None,
    author: str | None,
    also: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    """Login → class word for one recording, stable across argv and stdout.

    Precedence: the configured bot (``bot``), the ``is_maintainer``
    login (``maintainer``), the change request's author (``author``);
    then every further login of the recorded answers, and every
    ``@<handle>`` mentioned in their free text
    (:func:`repoach.forge.recording.mentions`), in order of first
    appearance: the next ``bot-<n>`` for a ``[bot]`` login, the next
    ``service`` for a GitLab project bot and the next ``author-<n>``
    otherwise. A login of :data:`repoach.forge.recording.NON_PERSON_LOGINS`
    or already a class word is kept. A login is compared casefolded, as
    forges compare logins. A login known only from a mention is keyed
    ``@<login>`` (:data:`repoach.forge.recording.MENTION_ONLY_PREFIX`), so
    only its ``@<handle>`` form is replaced; seen later as a login, it is
    re-keyed with the same word.
    """
    roles: dict[str, str] = {}
    following = {"bot": 1, "author": 1, "service": 1}

    def _next(kind: str) -> str:
        n = following[kind]
        following[kind] = n + 1
        return kind if n == 1 else f"{kind}-{n}"

    def _key_of(login: str) -> str | None:
        folded = login.casefold()
        for key in roles:
            if key.removeprefix(MENTION_ONLY_PREFIX).casefold() == folded:
                return key
        return None

    def _add(login: str, *, mention: bool) -> None:
        if not login or is_classified_login(login):
            return
        bot_key = _key_of(f"{login}[bot]") if mention else None
        if bot_key is not None:
            if bot_stem(bot_key) is not None and _key_of(login) is None:
                roles[login] = roles[bot_key]
            return
        key = _key_of(login)
        if key is not None:
            if not mention and key.startswith(MENTION_ONLY_PREFIX):
                roles[login] = roles.pop(key)
            return
        promoted = None
        if not mention and login.casefold().endswith("[bot]"):
            stem_key = _key_of(login[:-5])
            if stem_key is not None and stem_key.startswith(MENTION_ONLY_PREFIX):
                promoted = roles.pop(stem_key)
        if GITLAB_PROJECT_BOT_RE.match(login):
            word = _next("service")
        else:
            word = _next("bot" if login.casefold().endswith("[bot]") else "author")
        roles[MENTION_ONLY_PREFIX + login if mention else login] = word
        stem = bot_stem(login) if promoted is not None else None
        if stem is not None:
            roles[stem] = word

    if bot and bot not in NON_PERSON_LOGINS:
        roles[bot] = _next("bot")
    if maintainer and _key_of(maintainer) is None and not is_classified_login(maintainer):
        roles[maintainer] = "maintainer"
    if author:
        word = _next("author")
        if _key_of(author) is None and not is_classified_login(author):
            roles[author] = word
    for _path, value in _decoded(calls):
        for _key, login in login_values(value):
            _add(login, mention=False)
        for login in mentions(value):
            _add(login, mention=True)
    for login in also:
        _add(login, mention=False)
    return roles


def refuse_unclassified(
    fixture: dict[str, Any],
    denylist: Denylist | None = None,
    known: closure.Known | None = None,
) -> None:
    """Raise :class:`UnclassifiedValueError` on any value of the fixture that may name someone (D3, 0.17).

    Every login under a login key must be a class word or a
    :data:`repoach.forge.recording.NON_PERSON_LOGINS` entry. Then each
    string of the fixture, in this order, may hold no home path, no
    deny-list entry and nothing the closure oracle
    (:func:`repoach.forge.closure.judge`) refuses: a known value, a host
    that is neither a placeholder nor public, a public address or a mailbox
    that is neither a placeholder nor a documentation literal.

    Args:
        fixture: The fixture about to be written; ``calls`` are reported
            as ``exchanges[<i>]``.
        denylist: The deny-list (:func:`read_denylist` when ``None``).
        known: The recording's known values; none when ``None``.

    Raises:
        UnclassifiedValueError: Naming a reason, a kind, an ordinal and the key path, never the value.
    """
    calls: list[dict[str, Any]] = list(fixture.get("calls") or [])
    for path, value in _decoded(calls):
        for key_path, login in login_values(value):
            if login and not is_classified_login(login):
                raise UnclassifiedValueError(f"unclassified: {path}.{key_path}")
    deny = read_denylist() if denylist is None else denylist
    judged = known if known is not None else closure.Known(())
    for path, text in scanned_strings(fixture):
        rule = generic_class(text)
        if rule is not None:
            raise UnclassifiedValueError(f"generic class {rule} at {path}")
        entry = deny.hit(text)
        if entry is not None:
            raise UnclassifiedValueError(f"deny-list entry {entry} at {path}")
        if path in CONSTANT_FIELDS:
            continue
        if path == "source":
            text = RECORDED_SOURCE_HEAD_RE.sub("", text)
        refusal = closure.judge(text, judged)
        if refusal is not None:
            where = f" {refusal.kind} {refusal.n}" if refusal.reason == "residual" else ""
            raise UnclassifiedValueError(f"{refusal.reason}{where} at {path}")


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


def origin_url(cwd: Path) -> str:
    """The ``origin`` remote of the checkout at ``cwd``, ``""`` when there is none; a needle source, never written."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.info("recorder.no_origin", error=type(exc).__name__)
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _set_bot(forge: Forge, login: str) -> None:
    """Give a forge the recording's classified bot login."""
    if hasattr(forge, "_bot_login"):
        forge._bot_login = login
    else:
        forge._bot_username = login


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
    forge = select_forge(override=forge_name, cwd=cwd)
    if bot is not None:
        _set_bot(forge, bot)
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
    """Run one protocol method for real and write its fixture.

    Returns:
        The fixture path written.
    """
    if method in WRITE_METHODS:
        raise SystemExit(f"refusing to record write method {method!r}: fixtures never post")
    forge = select_forge(override=forge_name, cwd=cwd)
    author = _author_of(forge, method, args)
    recorder = RecordingSpawn(inner=forge._spawn)
    forge._spawn = recorder
    typed_args, typed_kwargs = coerce_call(method, args, kwargs)
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
        denylist=denylist.needle_values(),
        ids=ids,
        projects=projects,
        forge=forge_name,
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

    result, raises = _replay(
        forge_name, cwd, method, typed_args, typed_kwargs, calls, bot, _normalize
    )
    if raises != live_raises:
        raise SystemExit(
            "refusing to write a fixture whose redacted replay disagrees with the live call"
        )
    leaked = [hit for call in calls for hit in find_secret_tokens(call["stdout"] + call["stderr"])]
    if leaked:
        raise SystemExit(
            f"refusing to write a fixture carrying {len(leaked)} secret-shaped value(s)"
        )
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
    except (UnclassifiedValueError, DenylistError, SyntheticIdsExhaustedError, NeedleError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
