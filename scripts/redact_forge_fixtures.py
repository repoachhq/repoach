#!/usr/bin/env python3
"""Re-apply the recorder's class redaction to every recorded forge fixture (offline).

SP-RECORDER-REDACTS-BY-CLASS: ``scripts/record_forge_fixture.py`` writes
URLs with the placeholder host and namespace, drops personal keys and a
user object's ``name`` and URLs, classifies logins and synthesizes user
ids at record time. The fixtures recorded or written before it are
transformed by the same functions, deterministically and without any
network call::

    scripts/redact_forge_fixtures.py            # rewrite in place
    scripts/redact_forge_fixtures.py --check    # exit 1 when a file would change

In a fixture, every exchange (an object holding ``stdout`` and
``returncode``) has its stdout redacted by
:func:`repoach.forge.recording.redact_payload` when it is JSON and its
URLs redacted when it is text, its stderr's and stdin's URLs redacted; the
top-level ``args`` and ``result`` have their URLs redacted.

Logins follow the real-account rule the recorder applies: every login
that is not a class word or one of
:data:`repoach.forge.recording.NON_PERSON_LOGINS` — a hand-written
placeholder such as ``alice`` or ``owner`` included, since each is a real
account — becomes the next free ``author-<n>`` (a ``[bot]`` login
``bot-<n>``, a GitLab project bot ``service``), under login keys, as whole
arguments, as path segments and as ``@<handle>`` mentions in free text.

Every user id the fixture does not declare under
:data:`repoach.forge.recording.SYNTHETIC_IDS_KEY` becomes a synthetic one,
from one sequence shared by the whole corpus and keyed by
``(forge, id)`` (:class:`repoach.forge.recording.UserIdMap`), so one
account keeps one number across files and two forges' accounts never
share one; the sequence starts after the highest number any fixture
declares. A recorded fixture's ``source`` names the forge, never the
checkout's folder. A second run changes nothing, which
``tests/unit/test_forge_recording_redaction.py`` pins.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import cast

from repoach.forge.recording import (
    GITLAB_PROJECT_BOT_RE,
    LOGIN_KEYS,
    MENTION_ONLY_PREFIX,
    RECORDED_SOURCE_RE,
    SYNTHETIC_ID_START,
    SYNTHETIC_IDS_KEY,
    UserIdMap,
    declared_user_ids,
    decode_json_values,
    is_classified_login,
    login_values,
    mention_pattern,
    mentions,
    recorded_source,
    redact_payload,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"

FORGE_OF_DIRECTORY: dict[str, str] = {"github": "github", "gitlab": "gitlab", "gate": "github"}
"""The forge of a fixture that carries no ``forge`` key, by its directory.

``gate/`` holds read-only ``gh`` captures of this project's own pull
requests (``pr202_capture.json``: ``captured … read-only gh/git``).
"""


class UnknownForgeError(Exception):
    """A fixture names no forge and its directory is not in :data:`FORGE_OF_DIRECTORY`."""


def fixture_forge(path: Path, data: object) -> str:
    """The forge a fixture was recorded on: its ``forge`` key, else its directory's.

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
    """Redact one recorded stdout; an unchanged JSON answer keeps its bytes."""
    values = decode_json_values(stdout)
    if values is None:
        return redact_urls_in_text(stdout)
    if [redact_payload(v) for v in values] == values:
        return stdout
    return redact_stdout(stdout)


def _exchanges(node: object) -> object:
    """Redact every exchange-shaped object of ``node``, recursively."""
    if isinstance(node, dict):
        out = {k: _exchanges(v) for k, v in node.items()}
        if "returncode" in node and isinstance(node.get("stdout"), str):
            out["stdout"] = redact_output(node["stdout"])
            for key in ("stderr", "stdin"):
                if isinstance(node.get(key), str):
                    out[key] = redact_urls_in_text(node[key])
        return out
    if isinstance(node, list):
        return [_exchanges(v) for v in node]
    return node


def _unclassified_logins(data: dict[str, object]) -> dict[str, bool]:
    """Every login of ``data`` that is not a class word nor a non-person login → whether it is mention-only.

    In order of first appearance: the bot login first, then the logins
    under login keys and the ``@<handle>`` mentions in free text, JSON
    stdout walked inside. A login also seen under a login key or as the
    bot is not mention-only.
    """
    found: dict[str, bool] = {}

    def _add(login: str, *, mention: bool) -> None:
        if not login or is_classified_login(login):
            return
        found[login] = found.get(login, True) and mention

    bot = data.get("bot_username")
    if isinstance(bot, str):
        _add(bot, mention=False)
    stack: list[object] = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
            stack[:0] = decode_json_values(node) or []
            continue
        for _path, login in login_values(node):
            _add(login, mention=False)
        for login in mentions(node):
            _add(login, mention=True)
        if isinstance(node, dict):
            stack[:0] = [v for k, v in node.items() if not isinstance(v, str) or k == "stdout"]
        elif isinstance(node, list):
            stack[:0] = list(node)
    return found


def legacy_roles(data: dict[str, object]) -> dict[str, str]:
    """Each unclassified login of ``data`` → the next class word the fixture lacks.

    The bot login becomes ``bot``, another ``[bot]`` login ``bot-<n>``, a
    GitLab project bot ``service`` (then ``service-<n>``), any other login
    ``author-<n>`` (from 2). A mention-only login is keyed ``@<login>``
    (:data:`repoach.forge.recording.MENTION_ONLY_PREFIX`): only its
    ``@<handle>`` form is replaced.
    """
    text = json.dumps(data)
    roles: dict[str, str] = {}
    following = {"author": 2, "service": 1, "bot": 1}
    bot = data.get("bot_username")
    for login, mention_only in _unclassified_logins(data).items():
        if login == bot or login.endswith("[bot]"):
            kind = "bot"
        elif GITLAB_PROJECT_BOT_RE.match(login):
            kind = "service"
        else:
            kind = "author"
        while True:
            n = following[kind]
            following[kind] = n + 1
            word = kind if n == 1 else f"{kind}-{n}"
            if not re.search(rf'"{re.escape(word)}\\*"', text):
                break
        roles[MENTION_ONLY_PREFIX + login if mention_only else login] = word
    return roles


def _mention_text(text: str, roles: dict[str, str]) -> str:
    """Replace every ``@<login>`` of ``roles`` in free text by ``@<class word>``."""
    for login, word in roles.items():
        handle = login.removeprefix(MENTION_ONLY_PREFIX)
        text = mention_pattern(handle).sub(f"@{word}", text)
    return text


def _rename(node: object, roles: dict[str, str]) -> object:
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
                out[key] = [_segment(a, roles) if isinstance(a, str) else a for a in item]
            elif isinstance(item, str):
                out[key] = _mention_text(item, roles)
            else:
                out[key] = _rename(item, roles)
        return out
    if isinstance(node, list):
        return [_rename(v, roles) for v in node]
    return node


def _segment(arg: str, roles: dict[str, str]) -> str:
    """Replace a login that is a whole argument, a path segment or a ``=`` value; never a mention-only one."""
    actors = {k: v for k, v in roles.items() if not k.startswith(MENTION_ONLY_PREFIX)}
    if arg in actors:
        return actors[arg]
    for login, word in actors.items():
        arg = re.sub(rf"(?<=[/=]){re.escape(login)}(?=[/&]|$)", word, arg)
    return arg


def _dumps(values: list[object]) -> str:
    """Serialise decoded stdout values as :func:`repoach.forge.recording.redact_stdout` does."""
    return "\n".join(json.dumps(v, ensure_ascii=False) for v in values)


def _remap_ids(node: object, ids: UserIdMap, forge: str, keep: frozenset[int]) -> object:
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


def _source(data: dict[str, object], forge: str) -> dict[str, object]:
    """``data`` with a recorded ``source`` head naming the forge, not the checkout's folder."""
    source = data.get("source")
    if not isinstance(source, str):
        return data
    m = RECORDED_SOURCE_RE.match(source)
    if m is None:
        return data
    rest = source[m.end() :]
    if rest.startswith(" "):
        rest = "," + rest
    return {**data, "source": recorded_source(m.group(1), forge) + rest}


def redact_fixture(
    data: dict[str, object], ids: UserIdMap | None = None, *, forge: str
) -> dict[str, object]:
    """The class-redacted copy of one fixture; ``ids`` is shared across the corpus."""
    ids = ids if ids is not None else UserIdMap()
    raw_keep = data.get(SYNTHETIC_IDS_KEY)
    keep = (
        frozenset(n for n in raw_keep if isinstance(n, int))
        if isinstance(raw_keep, list)
        else frozenset()
    )
    out = cast(dict[str, object], _exchanges(data))
    for key in ("args", "result"):
        if key in out:
            out[key] = redact_urls_in_value(out[key])
    roles = legacy_roles(out)
    if roles:
        out = cast(dict[str, object], _rename(out, roles))
    out = cast(dict[str, object], _remap_ids(out, ids, forge, keep))
    method = out.get("method")
    if "result" in out and isinstance(method, str):
        out["result"] = ids.apply_result(out["result"], forge=forge, method=method, keep=keep)
    out = _source(out, forge)
    out.pop(SYNTHETIC_IDS_KEY, None)
    declared = declared_user_ids(out)
    if declared:
        out[SYNTHETIC_IDS_KEY] = declared
    if list(data) == sorted(data):
        out = dict(sorted(out.items()))
    return out


def render(data: object, *, ascii_only: bool = False) -> str:
    """A fixture's text: two-space indent, key order kept, trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n"


def corpus_start(fixtures: list[dict[str, object]]) -> int:
    """The first number the corpus may hand out: after every number a fixture declares."""
    declared = [
        n
        for data in fixtures
        for n in cast(list[object], data.get(SYNTHETIC_IDS_KEY) or [])
        if isinstance(n, int)
    ]
    return max([SYNTHETIC_ID_START - 1, *declared]) + 1


def run(root: Path, *, check: bool) -> list[Path]:
    """Redact every ``*.json`` under ``root``; return the files that changed (or would).

    Raises:
        UnknownForgeError: A fixture whose forge cannot be told.
    """
    changed: list[Path] = []
    paths = sorted(root.rglob("*.json"))
    loaded = [(p, p.read_text(encoding="utf-8")) for p in paths]
    fixtures = [cast(dict[str, object], json.loads(text)) for _p, text in loaded]
    ids = UserIdMap(start=corpus_start(fixtures))
    for (path, text), data in zip(loaded, fixtures, strict=True):
        new = redact_fixture(data, ids, forge=fixture_forge(path, data))
        if new == data:
            continue
        changed.append(path)
        if not check:
            path.write_text(render(new, ascii_only=text.isascii()), encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=Path, default=FIXTURE_ROOT)
    parser.add_argument("--check", action="store_true")
    ns = parser.parse_args(argv)
    try:
        changed = run(ns.root, check=ns.check)
    except UnknownForgeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    verb = "would change" if ns.check else "changed"
    sys.stdout.write(f"forge fixtures: {len(changed)} {verb}\n")
    return 1 if ns.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
