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
top-level ``args``, ``kwargs`` and ``result`` have their URLs redacted.

Logins follow the real-account rule the recorder applies: every login
that is not a class word or one of
:data:`repoach.forge.recording.NON_PERSON_LOGINS` — a hand-written
placeholder such as ``alice`` or ``owner`` included, since each is a real
account — becomes the next free ``author-<n>`` (a ``[bot]`` login
``bot-<n>``, a GitLab project bot ``service``), under login keys, as whole
arguments, as path segments and as ``@<handle>`` mentions in free text; a
login known only from a mention is replaced in its ``@<handle>`` form only.

Every user id the fixture does not declare under
:data:`repoach.forge.recording.SYNTHETIC_IDS_KEY`, or declares outside
``1001..1999`` (:func:`repoach.forge.recording.trusted_declaration`),
becomes a synthetic one, from one sequence shared by the whole corpus and
keyed by ``(forge, id)`` (:class:`repoach.forge.recording.UserIdMap`), so
one account keeps one number across files and two forges' accounts never
share one; the sequence starts after the highest trusted number any
fixture declares, and a corpus that would need a number past the range is
exit 2 with nothing written. A recorded fixture's ``source`` names the forge, never the
checkout's folder. A repository id becomes a synthetic one in
``2001..2999``, declared under ``synthetic_project_ids``. Every fixture is
judged by the recorder's own judge
(:func:`record_forge_fixture.refuse_unclassified`, the closure oracle
:func:`repoach.forge.closure.judge`) against the known values of its own
structured fields, the public forges' hosts and the deny-list before any
file is written; a refusal is exit 2 and nothing is written. A
second run changes nothing, which
``tests/unit/test_forge_recording_redaction.py`` pins.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from repoach.core.logging import route_default_logs_to_stderr
from repoach.forge import closure
from repoach.forge.recording import (
    GITLAB_PROJECT_BOT_RE,
    LOGIN_KEYS,
    MENTION_ONLY_PREFIX,
    RECORDED_SOURCE_HEAD_RE,
    RECORDED_SOURCE_RE,
    SYNTHETIC_ID_END,
    SYNTHETIC_ID_START,
    SYNTHETIC_IDS_KEY,
    SYNTHETIC_PROJECT_ID_END,
    SYNTHETIC_PROJECT_ID_START,
    SYNTHETIC_PROJECT_IDS_KEY,
    NeedleError,
    SyntheticIdsExhaustedError,
    UserIdMap,
    bot_mention_stem,
    bot_stem,
    declared_project_ids,
    declared_user_ids,
    decode_json_values,
    is_classified_login,
    known_values,
    login_values,
    mention_pattern,
    mentions,
    outside_literals,
    recorded_source,
    redact_payload,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
    replace_known,
    replace_known_in_stdout,
    token_pattern,
    trusted_declaration,
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"

RECORDER = "record_forge_fixture"
"""The recorder script's module name; its deny-list reader is the one this script uses."""


def recorder() -> ModuleType:
    """The recorder script as a module, the one already loaded when there is one.

    The deny-list reader lives there (SP-NO-PERSONAL-DATA-GATE D3's copy);
    reusing a loaded module keeps one :class:`DenylistError` class.
    """
    loaded = sys.modules.get(RECORDER)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(
        RECORDER, Path(__file__).with_name(f"{RECORDER}.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError(RECORDER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[RECORDER] = module
    spec.loader.exec_module(module)
    return module


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
        folded = login.casefold()
        bot_key = (
            next((k for k in found if k.casefold() == f"{folded}[bot]"), None) if mention else None
        )
        if bot_key is not None:
            if bot_stem(bot_key) is not None:
                found[login] = False
            return
        promoted: list[str] = []
        if not mention and folded.endswith("[bot]"):
            stem = folded[:-5]
            promoted = [k for k, m_only in found.items() if m_only and k.casefold() == stem]
            for existing in promoted:
                del found[existing]
        found[login] = found.get(login, True) and mention
        if promoted and bot_stem(login) is not None:
            found[promoted[0]] = False

    bot = data.get("bot_username")
    if isinstance(bot, str):
        _add(bot, mention=False)
    stack: list[object] = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, str):
            if node.lstrip()[:1] in ("{", "["):
                stack[:0] = decode_json_values(node) or []
        else:
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
    unclassified = _unclassified_logins(data)
    siblings = {b.casefold(): b for b in unclassified if b.casefold().endswith("[bot]")}
    stems = {
        k: siblings[f"{k.casefold()}[bot]"]
        for k, mention_only in unclassified.items()
        if not mention_only and f"{k.casefold()}[bot]" in siblings
    }
    for login, mention_only in unclassified.items():
        if login not in stems:
            if login == bot or login.casefold().endswith("[bot]"):
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
    for stem, bot_login in stems.items():
        roles[stem] = roles[bot_login]
    return roles


def _mention_text(text: str, roles: dict[str, str]) -> str:
    """Replace every login of ``roles`` in free text by its class word, a tool literal left as it is.

    The ``@<login>`` form of every login (and a ``<name>[bot]`` login's
    ``@<stem>``) and the bare form of every login that is not mention-only.
    """
    rules: list[tuple[re.Pattern[str], str]] = []
    for login, word in sorted(roles.items(), key=lambda pair: len(pair[0]), reverse=True):
        mention_only = login.startswith(MENTION_ONLY_PREFIX)
        rules.append((mention_pattern(login.removeprefix(MENTION_ONLY_PREFIX)), f"@{word}"))
        if not mention_only:
            rules.append((token_pattern(login), word))
            mention_stem = bot_mention_stem(login)
            if mention_stem is not None:
                rules.append((mention_pattern(mention_stem), f"@{word}"))

    def rewrite(piece: str) -> str:
        for pattern, replacement in rules:
            piece = pattern.sub(replacement, piece)
        return piece

    return outside_literals(text, rewrite)


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
                out[key] = [_segment(a, roles) if isinstance(a, str) else a for a in item]
            elif key == "case" and isinstance(item, str):
                out[key] = item
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
    m = RECORDED_SOURCE_RE.match(source)
    if m is None:
        return data
    rest = source[m.end() :]
    if rest.startswith(" "):
        rest = "," + rest
    return {**data, "source": recorded_source(m.group(1), forge) + rest}


def fixture_known(
    data: dict[str, object],
    *,
    roles: dict[str, str] | None = None,
    remote: str = "",
    denylist: tuple[str, ...] = (),
    ids: UserIdMap | None = None,
    projects: UserIdMap | None = None,
    forge: str = "github",
) -> closure.Known:
    """The known values (D7) the offline transform can learn from one fixture's own structured fields.

    The fixture's exchanges, ``args`` and ``kwargs`` are the raw recording;
    ``remote`` and ``denylist`` are the inputs the live recorder has and a
    committed fixture has not. Declared synthetic ids are never values.

    Raises:
        NeedleError: A structured value that is a class word or placeholder, by kind and ordinal.
    """
    raw = [data.get("calls"), data.get("args"), data.get("kwargs"), remote]
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
                head = RECORDED_SOURCE_HEAD_RE.match(item)
                cut = head.end() if head else 0
                out[key] = item[:cut] + replace_known(item[cut:], known)
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
    denylist: tuple[str, ...] = (),
) -> dict[str, object]:
    """The class-redacted copy of one fixture: the live pipeline minus the forge (D4, 0.17).

    ``ids`` and ``projects`` are shared across the corpus.

    Raises:
        NeedleError: A structured value that is a class word or placeholder.
        ResidualValueError: The redacted fixture is refused by :func:`repoach.forge.recording`'s
            oracle: a known value, an unknown host, a public address, an unknown mailbox, a
            deny-list entry, a home path or an unclassified login.
    """
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
    roles = legacy_roles(out)
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
    for key, declared in (
        (SYNTHETIC_IDS_KEY, declared_user_ids(out)),
        (SYNTHETIC_PROJECT_IDS_KEY, declared_project_ids(out)),
    ):
        out.pop(key, None)
        if declared:
            out[key] = declared
    if list(data) == sorted(data):
        out = dict(sorted(out.items()))
    try:
        recorder().refuse_unclassified(out, recorder().Denylist(denylist), known)
    except recorder().UnclassifiedValueError as exc:
        raise ResidualValueError(str(exc)) from exc
    return out


def render(data: object, *, ascii_only: bool = False) -> str:
    """A fixture's text: two-space indent, key order kept, trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n"


def corpus_start(
    fixtures: list[dict[str, object]],
    key: str = SYNTHETIC_IDS_KEY,
    low: int = SYNTHETIC_ID_START,
    high: int = SYNTHETIC_ID_END,
) -> int:
    """The first number the corpus may hand out: after every trusted number a fixture declares under ``key``."""
    declared = [n for data in fixtures for n in trusted_declaration(data.get(key), low, high)]
    return max([low - 1, *declared]) + 1


class DenylistLike(Protocol):
    """What :func:`run` needs of the recorder's opaque ``Denylist``."""

    def needle_values(self) -> tuple[str, ...]:
        """The entries, for the known values only."""
        ...


def run(root: Path, *, check: bool, denylist: DenylistLike | None = None) -> list[Path]:
    """Redact every ``*.json`` under ``root``; return the files that changed (or would).

    Every file is transformed and judged by the closure oracle
    (:func:`fixture_known`, :func:`repoach.forge.closure.judge`)
    before any is written, so a refusal leaves the corpus as it was.

    Args:
        root: The fixture corpus.
        check: Write nothing; only say which files would change.
        denylist: The deny-list (the recorder's ``read_denylist()`` when ``None``).

    Raises:
        UnknownForgeError: A fixture whose forge cannot be told.
        SyntheticIdsExhaustedError: The corpus needs a number past the synthetic range.
        NeedleError: A needle the oracle cannot judge.
        ResidualValueError: A redacted fixture still holds a needle.
    """
    deny: DenylistLike = recorder().read_denylist() if denylist is None else denylist
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
    entries = deny.needle_values()
    for (path, text), data in zip(loaded, fixtures, strict=True):
        rel = path.relative_to(root).as_posix()
        try:
            new = redact_fixture(
                data,
                ids,
                forge=fixture_forge(path, data),
                projects=projects,
                denylist=entries,
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: one line on stdout, or nothing and exit 2 on a refusal."""
    route_default_logs_to_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", type=Path, default=FIXTURE_ROOT)
    parser.add_argument("--check", action="store_true")
    ns = parser.parse_args(argv)
    try:
        changed = run(ns.root, check=ns.check)
    except (
        UnknownForgeError,
        SyntheticIdsExhaustedError,
        NeedleError,
        ResidualValueError,
        recorder().DenylistError,
    ) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    verb = "would change" if ns.check else "changed"
    sys.stdout.write(f"forge fixtures: {len(changed)} {verb}\n")
    return 1 if ns.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
