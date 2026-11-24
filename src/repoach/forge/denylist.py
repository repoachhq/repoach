"""The maintainer's deny-list, read outside the tree and held opaque (SP-NO-PERSONAL-DATA-GATE D3).

SP-RECORDER-REDACTS-BY-CLASS (K8, 0.10): the recorder and the offline
transform read the same deny-list through this one reader; an entry is
never printed, logged or bound to a name a traceback can show. The gate
of SP-NO-PERSONAL-DATA-GATE, when it lands, reads it from here too.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from repoach.core.paths import user_config_dir
from repoach.forge import closure

DENYLIST_ENV = "REPOACH_PERSONAL_DENYLIST"
"""SP-NO-PERSONAL-DATA-GATE D3: the deny-list in CI, newline- or comma-separated."""

DENYLIST_FILE_NAME = "personal-denylist"
"""SP-NO-PERSONAL-DATA-GATE D3: the deny-list file under ``user_config_dir()``, one entry per line."""

DENYLIST_MIN_LENGTH = 4
"""SP-NO-PERSONAL-DATA-GATE D3: a shorter entry would match everywhere and is refused."""


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

    def short_entry(self) -> int | None:
        """The ordinal of the first entry shorter than :data:`DENYLIST_MIN_LENGTH`, or ``None``."""
        for n, entry in enumerate(self._entries, 1):
            if len(entry) < DENYLIST_MIN_LENGTH:
                return n
        return None

    def needle_values(self) -> tuple[str, ...]:
        """The entries, for :func:`repoach.forge.recording.known_values` only; never printed."""
        return self._entries


def read_denylist(config_dir: Path | None = None) -> Denylist:
    """The maintainer's deny-list, from the file and the environment merged (SP-NO-PERSONAL-DATA-GATE D3).

    A copy of that gate's reader (it lands later; not imported): the file
    :func:`denylist_file`, one entry per line with ``#`` comments, and
    :data:`DENYLIST_ENV`, newline- or comma-separated. Both live outside the tree;
    no entry is ever printed or written, and the frame that raises binds
    none: the entries are read in :func:`_read_entries` and judged by the
    opaque :class:`Denylist`.

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
    denylist = _read_entries(config_dir)
    short = denylist.short_entry()
    if short is not None:
        raise DenylistError(f"deny-list entry {short}: too short")
    return denylist


def _read_entries(config_dir: Path | None) -> Denylist:
    """The merged entries, opaque; never raises on content."""
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
