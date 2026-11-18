#!/usr/bin/env python3
"""Re-apply the recorder's class redaction to every recorded forge fixture (offline).

SP-RECORDER-REDACTS-BY-CLASS: ``scripts/record_forge_fixture.py`` writes
URLs with the placeholder host and namespace, drops personal keys and a
user object's ``name`` and URLs at record time. The fixtures recorded
before it are transformed by the same functions, deterministically and
without any network call::

    scripts/redact_forge_fixtures.py            # rewrite in place
    scripts/redact_forge_fixtures.py --check    # exit 1 when a file would change

In a fixture, every exchange (an object holding ``stdout`` and
``returncode``) has its stdout redacted by
:func:`repoach.forge.recording.redact_payload` when it is JSON and its
URLs redacted when it is text, its stderr's and stdin's URLs redacted; the
top-level ``args`` and ``result`` have their URLs redacted. Logins already
hold class words, except the hand-written placeholders of
:data:`LEGACY_LOGINS`, which become ``author-<n>`` under login keys, as
whole arguments and as path segments. A second run
changes nothing, which ``tests/unit/test_forge_recording_redaction.py``
pins.
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
    decode_json_values,
    login_values,
    redact_payload,
    redact_stdout,
    redact_urls_in_text,
    redact_urls_in_value,
)

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "forge"

LEGACY_LOGINS: frozenset[str] = frozenset({"alice", "bob", "dev", "devops", "owner"})
"""Hand-written placeholder logins that are also real forge accounts; each becomes ``author-<n>``.

``repoach-bot`` (this project's bot) and ``github-actions[bot]`` (the
platform's service account) name no person and are kept.
"""


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


def _legacy_logins(data: object) -> list[str]:
    """The :data:`LEGACY_LOGINS` a fixture holds as a login value, in order of first appearance."""
    found: list[str] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, str) and node.lstrip()[:1] in ("{", "["):
            stack[:0] = decode_json_values(node) or []
            continue
        for _path, login in login_values(node):
            legacy = login in LEGACY_LOGINS or GITLAB_PROJECT_BOT_RE.match(login)
            if legacy and login not in found:
                found.append(login)
        if isinstance(node, dict):
            stack[:0] = [v for k, v in node.items() if not isinstance(v, str) or k == "stdout"]
        elif isinstance(node, list):
            stack[:0] = list(node)
    return found


def legacy_roles(data: dict[str, object]) -> dict[str, str]:
    """Each legacy login of ``data`` → the next class word the fixture lacks.

    A placeholder of :data:`LEGACY_LOGINS` becomes ``author-<n>`` (from 2),
    a GitLab project bot ``service`` (then ``service-<n>``).
    """
    text = json.dumps(data)
    roles: dict[str, str] = {}
    following = {"author": 2, "service": 1}
    for login in _legacy_logins(data):
        kind = "service" if GITLAB_PROJECT_BOT_RE.match(login) else "author"
        while True:
            n = following[kind]
            following[kind] = n + 1
            word = kind if n == 1 else f"{kind}-{n}"
            if not re.search(rf'"{re.escape(word)}\\*"', text):
                break
        roles[login] = word
    return roles


def _rename(node: object, roles: dict[str, str]) -> object:
    """Replace ``roles`` logins under login keys only; JSON stdout is walked inside."""
    if isinstance(node, dict):
        out: dict[str, object] = {}
        for key, item in node.items():
            if key in LOGIN_KEYS and isinstance(item, str):
                out[key] = roles.get(item, item)
            elif key == "stdout" and isinstance(item, str):
                values = decode_json_values(item)
                renamed = [_rename(v, roles) for v in (values or [])]
                out[key] = item if values is None or renamed == values else _dumps(renamed)
            elif key == "args" and isinstance(item, list):
                out[key] = [_segment(a, roles) if isinstance(a, str) else a for a in item]
            else:
                out[key] = _rename(item, roles)
        return out
    if isinstance(node, list):
        return [_rename(v, roles) for v in node]
    return node


def _segment(arg: str, roles: dict[str, str]) -> str:
    """Replace a login that is a whole argument, a path segment or a ``=`` value."""
    if arg in roles:
        return roles[arg]
    for login, word in roles.items():
        arg = re.sub(rf"(?<=[/=]){re.escape(login)}(?=[/&]|$)", word, arg)
    return arg


def _dumps(values: list[object]) -> str:
    """Serialise decoded stdout values as :func:`repoach.forge.recording.redact_stdout` does."""
    return "\n".join(json.dumps(v, ensure_ascii=False) for v in values)


def redact_fixture(data: dict[str, object]) -> dict[str, object]:
    """The class-redacted copy of one fixture."""
    out = cast(dict[str, object], _exchanges(data))
    for key in ("args", "result"):
        if key in out:
            out[key] = redact_urls_in_value(out[key])
    roles = legacy_roles(out)
    if roles:
        out = cast(dict[str, object], _rename(out, roles))
    return out


def render(data: object, *, ascii_only: bool = False) -> str:
    """A fixture's text: two-space indent, key order kept, trailing newline."""
    return json.dumps(data, indent=2, ensure_ascii=ascii_only) + "\n"


def run(root: Path, *, check: bool) -> list[Path]:
    """Redact every ``*.json`` under ``root``; return the files that changed (or would)."""
    changed: list[Path] = []
    for path in sorted(root.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        new = redact_fixture(data)
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
    changed = run(ns.root, check=ns.check)
    verb = "would change" if ns.check else "changed"
    sys.stdout.write(f"forge fixtures: {len(changed)} {verb}\n")
    return 1 if ns.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
