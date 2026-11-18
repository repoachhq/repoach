#!/usr/bin/env python3
"""Record one forge conformance fixture against a real PR / MR (read-only).

SP-UNIVERSAL-FORGE A1: when ``gh`` or ``glab`` changes, re-record the
fixtures under ``tests/fixtures/forge/<forge>/`` against a real change
request so the conformance suite keeps pinning what the CLI actually
prints. The script runs ONE protocol method on the forge selected for
``--cwd`` and writes the subprocess exchanges it made plus its
normalised result::

    scripts/record_forge_fixture.py --forge gitlab --cwd ../yoob_2 \\
        --case pull_request --method pull_request --args '[283]'

Write methods are refused: a recording must never post, merge or open
anything. Their fixtures are written by hand from the forge's documented
payloads and say so in their ``source`` field.

Every JSON answer is redacted at record time: only the keys the
forges read (:data:`repoach.forge.recording.RECORDED_FIELDS`) are kept,
and a fixture in which a token- or email-shaped value survives is refused.

SP-RECORDER-REDACTS-BY-CLASS: personal keys, a user object's ``name``
and URLs are dropped, every URL gets the placeholder host and namespace
(:func:`repoach.forge.recording.redact_url`), and every login becomes
its class word in stdout, argv and the method's arguments — the bot
``bot``, the account an ``is_maintainer`` call asks about
``maintainer``, the change request's author ``author``, any other
``[bot]`` login ``bot-<n>`` and any other login ``author-<n>``. The
result is then recomputed by replaying the redacted exchanges, so the
fixture is what the readers return from it. A login left unclassified,
or a string of a generic personal class (a home path, a public IPv4
address, a mailbox outside the documentation domains), exits 2 naming
the key path, never the value, and writes nothing.

``--trim N`` keeps only the first ``N`` items of every JSON array in
recorded stdout (and of the recorded result), for listings too large to
commit; the fixture records that it was trimmed.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from repoach.core.logging import get_logger
from repoach.forge.protocol import Forge, ForgeError
from repoach.forge.recording import (
    CLASS_WORD_RE,
    GITLAB_PROJECT_BOT_RE,
    Exchange,
    RecordingSpawn,
    ReplaySpawn,
    classify_logins,
    coerce_call,
    decode_json_values,
    dump_fixture,
    find_secrets,
    login_values,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    serialize_result,
)
from repoach.forge.select import select_forge

_log = get_logger(__name__)

HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_$.-])/(home|Users)/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])")
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``home_path`` (that lint lands later; not imported)."""

IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d]|\.\d)")
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``public_ipv4`` token."""

IPV4_RESERVED = tuple(
    ipaddress.IPv4Network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
"""The ``public_ipv4`` exclusions of SP-NO-PERSONAL-DATA-GATE D1 (``255.255.255.255`` is in 240/4)."""

IPV4_VERSION_BEFORE_RE = re.compile(r"(v|V|==|>=|<=|~=|!=|version[ :=]?)$")
"""Text ending this way makes a four-component token a version, not an address."""

IPV4_VERSION_AFTER = ("-", "+", ".tar", ".whl", ".zip", ".gz")
"""Text starting this way after the token makes it a version or an archive name."""

MAILBOX_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9]([A-Za-z0-9._%+-]*[A-Za-z0-9])?@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"
)
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``mailbox``."""

MAILBOX_DOC_DOMAIN_RE = re.compile(
    r"(^example\.(com|org|net)$|\.(invalid|test|example|localhost|internal)$|^localhost$)", re.I
)
"""Mailbox domains that name no one (``mailbox`` exclusions)."""

URL_USERINFO_RE = re.compile(r"(?<=://)(?:(?!://)\S)+@")
"""Copy of ``repoach.core.redaction._URL_USERINFO`` (SP-NO-PERSONAL-DATA-GATE F8)."""


class UnclassifiedValueError(Exception):
    """A recorded value is a login with no class word, or matches a generic personal class.

    The message carries the key path, never the value.
    """


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


def _public_ipv4(text: str) -> bool:
    """Whether ``text`` holds a public IPv4 address outside a version context."""
    for m in IPV4_RE.finditer(text):
        octets = [int(g) for g in m.groups()]
        if any(o > 255 for o in octets):
            continue
        if IPV4_VERSION_BEFORE_RE.search(text[: m.start()]):
            continue
        if text[m.end() :].startswith(IPV4_VERSION_AFTER):
            continue
        address = ipaddress.IPv4Address(".".join(map(str, octets)))
        if not any(address in net for net in IPV4_RESERVED):
            return True
    return False


def _personal_mailbox(text: str) -> bool:
    """Whether ``text`` holds a mailbox outside the documentation domains."""
    userinfo = [(m.start(), m.end()) for m in URL_USERINFO_RE.finditer(text)]
    for m in MAILBOX_RE.finditer(text):
        local, _, domain = m.group(0).partition("@")
        if MAILBOX_DOC_DOMAIN_RE.search(domain):
            continue
        if any(w in part.lower() for part in (local, domain) for w in ("noreply", "no-reply")):
            continue
        if any(start <= m.start() < end for start, end in userinfo):
            continue
        after = text[m.end() : m.end() + 2]
        if after[:1] == ":" and after[1:2] not in ("", " ", "/"):
            continue
        return True
    return False


def generic_class(text: str) -> str | None:
    """The first generic personal class ``text`` matches, or ``None``."""
    if HOME_PATH_RE.search(text):
        return "home_path"
    if _public_ipv4(text):
        return "public_ipv4"
    if _personal_mailbox(text):
        return "mailbox"
    return None


def _strings(value: object, path: str) -> list[tuple[str, str]]:
    """Every ``(key path, string)`` of a decoded value; a JSON stdout is walked inside."""
    if isinstance(value, dict):
        return [hit for k, v in value.items() for hit in _strings(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [hit for i, v in enumerate(value) for hit in _strings(v, f"{path}[{i}]")]
    if isinstance(value, str):
        decoded = decode_json_values(value) if value.lstrip()[:1] in ("{", "[") else None
        if decoded is None:
            return [(path, value)]
        if len(decoded) == 1:
            return _strings(decoded[0], path)
        return [hit for i, v in enumerate(decoded) for hit in _strings(v, f"{path}[{i}]")]
    return []


def _decoded(calls: list[dict[str, Any]]) -> list[tuple[str, object]]:
    """``(path, decoded stdout value)`` of every JSON answer, in recording order."""
    out: list[tuple[str, object]] = []
    for i, call in enumerate(calls):
        values = decode_json_values(call["stdout"]) or []
        for k, value in enumerate(values):
            out.append((f"exchanges[{i}].stdout" + (f"[{k}]" if len(values) > 1 else ""), value))
    return out


def build_roles(
    calls: list[dict[str, Any]], *, bot: str, maintainer: str | None, author: str | None
) -> dict[str, str]:
    """Login → class word for one recording, stable across argv and stdout.

    Precedence: the configured bot (``bot``), the ``is_maintainer``
    login (``maintainer``), the change request's author (``author``);
    then every further login of the recorded answers in order of first
    appearance, the next ``bot-<n>`` for a ``[bot]`` login or a GitLab project bot and the next
    ``author-<n>`` otherwise. A login is compared casefolded, as forges
    compare logins.
    """
    roles: dict[str, str] = {}
    following = {"bot": 1, "author": 1}

    def _next(kind: str) -> str:
        n = following[kind]
        following[kind] = n + 1
        return kind if n == 1 else f"{kind}-{n}"

    def _known(login: str) -> bool:
        return login.casefold() in {k.casefold() for k in roles}

    if bot:
        roles[bot] = _next("bot")
    if maintainer and not _known(maintainer):
        roles[maintainer] = "maintainer"
    if author:
        word = _next("author")
        if not _known(author):
            roles[author] = word
    for _path, value in _decoded(calls):
        for _key, login in login_values(value):
            if login and not _known(login) and not CLASS_WORD_RE.match(login):
                bot_like = login.endswith("[bot]") or GITLAB_PROJECT_BOT_RE.match(login)
                roles[login] = _next("bot" if bot_like else "author")
    return roles


def refuse_unclassified(calls: list[dict[str, Any]], result_args: object) -> None:
    """Raise :class:`UnclassifiedValueError` on a login without a class word or a generic class.

    Args:
        calls: The classified, redacted exchanges.
        result_args: The classified method arguments.

    Raises:
        UnclassifiedValueError: Naming the key path, never the value.
    """
    for path, value in _decoded(calls):
        for key_path, login in login_values(value):
            if login and not CLASS_WORD_RE.match(login):
                raise UnclassifiedValueError(f"unclassified: {path}.{key_path}")
    scanned = [("args", result_args)] + [(f"exchanges[{i}]", c) for i, c in enumerate(calls)]
    for root, value in scanned:
        for path, text in _strings(value, root):
            rule = generic_class(text)
            if rule is not None:
                raise UnclassifiedValueError(f"generic class {rule} at {path}")


def classify_calls(calls: list[dict[str, Any]], roles: dict[str, str]) -> list[dict[str, Any]]:
    """Apply ``roles`` to every exchange's argv, stdin, stdout and stderr; redact text URLs."""
    out: list[dict[str, Any]] = []
    for call in calls:
        new = dict(call)
        new["args"] = classify_logins(call["args"], roles)
        values = decode_json_values(call["stdout"])
        if values is None:
            new["stdout"] = redact_urls_in_text(str(classify_logins(call["stdout"], roles)))
        else:
            new["stdout"] = "\n".join(
                json.dumps(classify_logins(v, roles), ensure_ascii=False) for v in values
            )
        for key in ("stdin", "stderr"):
            if isinstance(call.get(key), str):
                new[key] = redact_urls_in_text(str(classify_logins(call[key], roles)))
        out.append(new)
    return out


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
) -> tuple[object, str | None]:
    """Recompute the result from the redacted exchanges so the fixture stays self-consistent."""
    forge = select_forge(override=forge_name, cwd=cwd)
    if bot is not None:
        _set_bot(forge, bot)
    forge._spawn = ReplaySpawn([Exchange.from_json(c) for c in calls])
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
    for call in calls:
        call["stdout"] = redact_stdout(call["stdout"])
    if trim is not None:
        for call in calls:
            call["stdout"] = _trim_stdout(call["stdout"], trim)
    maintainer = str(args[0]) if method == "is_maintainer" and args else None
    roles = build_roles(calls, bot=forge.bot_username, maintainer=maintainer, author=author)
    calls = classify_calls(calls, roles)
    args = list(cast(list[Any], redact_urls_in_value(classify_logins(args, roles))))
    kwargs = dict(cast(dict[str, Any], redact_urls_in_value(classify_logins(kwargs, roles))))
    bot = roles.get(forge.bot_username, forge.bot_username)
    refuse_unclassified(calls, [args, kwargs])
    typed_args, typed_kwargs = coerce_call(method, args, kwargs)
    result, raises = _replay(forge_name, cwd, method, typed_args, typed_kwargs, calls, bot)
    if raises != live_raises:
        raise SystemExit(
            "refusing to write a fixture whose redacted replay disagrees with the live call"
        )
    leaked = [hit for call in calls for hit in find_secrets(call["stdout"] + call["stderr"])]
    if leaked:
        raise SystemExit(
            f"refusing to write a fixture carrying {len(leaked)} secret-shaped value(s)"
        )
    fixture = {
        "forge": forge_name,
        "case": case,
        "method": method,
        "args": args,
        "kwargs": kwargs,
        "bot_username": bot,
        "source": f"recorded {datetime.now(UTC).date().isoformat()} from {cwd.resolve().name}",
        "trimmed": trim,
        "calls": calls,
        "result": result,
        "raises": raises,
    }
    path = out_root / forge_name / f"{case}.json"
    dump_fixture(path, fixture)
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
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
    except UnclassifiedValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
