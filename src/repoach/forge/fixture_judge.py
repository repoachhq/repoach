"""The last judge of a forge fixture before it is written (SP-RECORDER-REDACTS-BY-CLASS D3).

The live recorder (``scripts/record_forge_fixture.py``) and the offline
transform (``scripts/redact_forge_fixtures.py``) both refuse a fixture
here: a login left unclassified, a home path, a deny-list entry or
anything the closure oracle (:func:`repoach.forge.closure.judge`)
refuses. A refusal names a reason, a kind, an ordinal and a key path,
never the value.
"""

from __future__ import annotations

import re
from typing import Any

from repoach.forge import closure
from repoach.forge.denylist import Denylist, read_denylist
from repoach.forge.recording import (
    CONSTANT_FIELDS,
    RECORDED_SOURCE_HEAD_RE,
    decode_json_values,
    is_classified_login,
    login_values,
    scanned_strings,
)

HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_$.-])"
    r"(?:(?:/var)?/(?:home|Users)/|[A-Za-z]:\\{1,2}[Uu]sers\\{1,2})"
    r"[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])"
)
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``home_path`` (that lint lands later; not imported).

A POSIX home (``/home/<x>``, ``/Users/<x>``, ``/var/home/<x>``) or a Windows profile
(``C:\\Users\\<x>``, its backslashes single or JSON-escaped), with a user segment.
"""


class UnclassifiedValueError(Exception):
    """A recorded value is a login with no class word, or matches a generic personal class.

    The message carries the key path, never the value.
    """


def generic_class(text: str) -> str | None:
    r"""``home_path`` when ``text`` holds a home directory path, else ``None``.

    Judged in every decoded form of ``text`` (:func:`repoach.forge.closure.decoded_forms`), as the
    deny-list and the closure oracle judge it, so ``%2Fhome%2F<x>`` and ``\/home\/<x>`` count.

    Args:
        text: One string of the fixture.

    Returns:
        The rule's name, or ``None``.
    """
    forms = closure.decoded_forms(text)
    return "home_path" if any(HOME_PATH_RE.search(form) for form in forms) else None


def decoded_answers(calls: list[dict[str, Any]]) -> list[tuple[str, object]]:
    """``(path, decoded stdout value)`` of every JSON answer, in recording order."""
    out: list[tuple[str, object]] = []
    for i, call in enumerate(calls):
        values = decode_json_values(call["stdout"]) or []
        for k, value in enumerate(values):
            out.append((f"exchanges[{i}].stdout" + (f"[{k}]" if len(values) > 1 else ""), value))
    return out


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
    for path, value in decoded_answers(calls):
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
