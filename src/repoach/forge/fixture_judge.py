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

from repoach.core import text_forms
from repoach.core.redaction import SECRET_VALUE_PATTERNS
from repoach.forge import closure
from repoach.forge.denylist import Denylist, read_denylist
from repoach.forge.fixture_reading import (
    RECORDED_SOURCE_HEAD_RE,
    decode_json_values,
    is_classified_login,
    login_values,
    scanned_strings,
    secret_keys,
)

SECRET_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    pattern for pattern in SECRET_VALUE_PATTERNS if "@" not in pattern.pattern
)
"""The forge-token shapes of :data:`repoach.core.redaction.SECRET_VALUE_PATTERNS`; a mailbox is the closure oracle's to judge (D3, 0.17)."""

CONSTANT_FIELDS: frozenset[str] = frozenset({"forge", "method"})
"""Top-level fields the recorder writes from closed enumerations (0.11 R6, 0.12 D3), judged by the deny-list only.

``forge`` is ``github`` or ``gitlab`` and ``method`` is a protocol
method's name; a segment needle that happens to spell one (a project
named ``github``) is no leak there. ``case`` is free-form user input and
is judged by every needle (0.12, D3).
"""

HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_$.-])"
    r"(?:(?:/var)?/(?:home|Users)/"
    r"|(?:/mnt|/cygdrive)?/[A-Za-z]/[Uu]sers/"
    r"|[A-Za-z]:(?:\\{1,2}|/)[Uu]sers(?:\\{1,2}|/))"
    r"[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])"
)
"""Copy of SP-NO-PERSONAL-DATA-GATE D1 ``home_path`` (that lint lands later; not imported).

A POSIX home (``/home/<x>``, ``/Users/<x>``, ``/var/home/<x>``), a Windows profile
(``C:\\Users\\<x>``, its backslashes single or JSON-escaped, or ``C:/Users/<x>``) or that
profile as a POSIX layer mounts it: WSL ``/mnt/<drive>/Users/<x>``, Git-Bash and MSYS2
``/<drive>/Users/<x>``, Cygwin ``/cygdrive/<drive>/Users/<x>``; with a user segment.
"""


class UnclassifiedValueError(Exception):
    """The judge refuses a fixture; the message names the reason and the key path, never the value.

    The reasons, each the head of its message: ``unclassified`` (a login with no class word),
    ``secret-shaped value`` (a token shape, or a secret-shaped key in a JSON stdout),
    ``generic class home_path``, ``deny-list entry <n>``, every refusal of the closure oracle
    (:class:`repoach.forge.closure.Refusal`: ``residual <kind> <n>``, ``tenant path``,
    ``unknown host``, ``public address``, ``unknown mailbox``, ``url userinfo``), and, from the
    live recorder, ``nesting too deep`` (a recording deeper than the readers can walk).
    """


def generic_class(text: str) -> str | None:
    r"""``home_path`` when ``text`` holds a home directory path, else ``None``.

    Judged in every decoded form of ``text`` (:func:`repoach.core.text_forms.decoded_forms`), as the
    deny-list and the closure oracle judge it, so ``%2Fhome%2F<x>`` and ``\/home\/<x>`` count.

    Args:
        text: One string of the fixture.

    Returns:
        The rule's name, or ``None``.
    """
    forms = text_forms.decoded_forms(text)
    return "home_path" if any(HOME_PATH_RE.search(form) for form in forms) else None


def has_secret_token(text: str) -> bool:
    r"""Whether a forge token shape (:data:`SECRET_TOKEN_PATTERNS`) is in ``text``.

    Judged in every decoded form of ``text`` (:func:`repoach.core.text_forms.decoded_forms`, the one
    decoder the home-path rule and the deny-list read), so a token whose prefix or delimiter is
    percent-, entity- or string-escaped (``ghp%5F…``, ``ghp&#95;…``, ``ghp_…``) counts.

    Args:
        text: One string of the fixture.

    Returns:
        ``True`` when a decoded form holds a token shape.
    """
    return any(
        pattern.search(form)
        for form in text_forms.decoded_forms(text)
        for pattern in SECRET_TOKEN_PATTERNS
    )


def find_secret_tokens(text: str) -> list[str]:
    """Every forge token shape and secret-shaped key found in ``text`` (and its JSON), mailboxes aside.

    Args:
        text: A recorded stdout or any other string.

    Returns:
        Each token-shaped match of :data:`SECRET_TOKEN_PATTERNS`, then each secret-shaped key of
        every JSON value ``text`` decodes to.
    """
    found = [m.group(0) for pattern in SECRET_TOKEN_PATTERNS for m in pattern.finditer(text)]
    for value in decode_json_values(text) or []:
        found += secret_keys(value)
    return found


def decoded_answers(calls: list[dict[str, Any]]) -> list[tuple[str, object]]:
    """``(path, decoded stdout value)`` of every JSON answer, in recording order.

    Args:
        calls: The recorded exchanges; a stdout that is not JSON yields nothing.

    Returns:
        ``exchanges[<i>].stdout`` (with ``[<k>]`` when a stdout holds several values) and the value.
    """
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
    :data:`repoach.forge.fixture_reading.NON_PERSON_LOGINS` entry. Then each
    string of the fixture, in this order, may hold no home path, no
    deny-list entry, no token-shaped value
    (:data:`SECRET_TOKEN_PATTERNS`; a JSON stdout
    holds no secret-shaped key either, :func:`find_secret_tokens`)
    and nothing the closure oracle
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
        DenylistError: ``denylist`` is ``None`` and the maintainer's deny-list is malformed.
    """
    calls: list[dict[str, Any]] = list(fixture.get("calls") or [])
    for path, value in decoded_answers(calls):
        for key_path, login in login_values(value):
            if login and not is_classified_login(login):
                raise UnclassifiedValueError(f"unclassified: {path}.{key_path}")
    for i, call in enumerate(calls):
        if find_secret_tokens(str(call.get("stdout") or "")):
            raise UnclassifiedValueError(f"secret-shaped value at exchanges[{i}].stdout")
    deny = read_denylist() if denylist is None else denylist
    judged = known if known is not None else closure.Known(())
    for path, text in scanned_strings(fixture):
        rule = generic_class(text)
        if rule is not None:
            raise UnclassifiedValueError(f"generic class {rule} at {path}")
        entry = deny.hit(text)
        if entry is not None:
            raise UnclassifiedValueError(f"deny-list entry {entry} at {path}")
        if has_secret_token(text):
            raise UnclassifiedValueError(f"secret-shaped value at {path}")
        if path in CONSTANT_FIELDS:
            continue
        if path == "source":
            text = RECORDED_SOURCE_HEAD_RE.sub("", text)
        refusal = closure.judge(text, judged)
        if refusal is not None:
            where = f" {refusal.kind} {refusal.n}" if refusal.reason == "residual" else ""
            raise UnclassifiedValueError(f"{refusal.reason}{where} at {path}")
