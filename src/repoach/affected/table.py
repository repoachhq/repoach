"""The path-to-job table, ``format:affected-jobs`` (SP-AFFECTED-JOBS-TABLE D1, D2).

A row names path prefixes and suffixes, the ``ci.yml`` jobs a matching
path affects, and the sentence that says why. :func:`matches` is the
only matching rule: a prefix ending in ``/`` matches a path that starts
with it, any other prefix matches the path equal to it, a suffix matches
a path that ends with it. There is no glob and no other dialect; a key
the format does not define is refused, never ignored.
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

TABLE_VERSION = 1
"""The one ``version`` this reader accepts (``table_version``)."""

DEFAULT_TABLE_PATH = ".github/affected-jobs.toml"
"""Where the table lives, relative to the project root (owner decision O1)."""

FULL_ROW = "full"
"""Name of the ``[full]`` class and of its entry in a selection's rows hit."""

UNKNOWN_ROW = "unknown"
"""Name, in a selection's rows hit, of the paths that match no row."""

_ROW_KEYS = frozenset({"prefixes", "suffixes", "jobs", "reason"})
_FULL_KEYS = frozenset({"prefixes", "suffixes", "reason"})
_TOP_KEYS = frozenset({"version", "gate", "rows", "full"})
_GATE_KEYS = frozenset({"pytest"})
_GLOB_CHARACTERS = frozenset("*?[]{}!")
"""Characters of the glob dialects the format refuses rather than reads as literals."""


class TableError(ValueError):
    """The table cannot be read or breaks the format.

    Attributes:
        path: The file that was read.
        key: Dotted key the defect is about (``rows.wheel.jobs``), empty
            for a whole-file defect.
        reason: The defect, without the location prefix.
    """

    def __init__(self, path: Path, reason: str, *, key: str = "") -> None:
        self.path = path
        self.key = key
        self.reason = reason
        subject = f" {key}" if key else ""
        super().__init__(f"{path.name}:{subject} {reason}")


class Row(BaseModel):
    """One row of the table.

    Attributes:
        name: The key under ``rows`` (``docs``), or ``full``.
        prefixes: Path prefixes, matched by :func:`matches`.
        suffixes: Path suffixes, matched by :func:`matches`.
        jobs: The ``ci.yml`` job ids a matching path selects; empty for
            ``[full]``, which selects every job.
        reason: One sentence saying why these paths affect these jobs.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    prefixes: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    jobs: frozenset[str] = frozenset()
    reason: str


class AffectedTable(BaseModel):
    """The whole table.

    Attributes:
        version: Always :data:`TABLE_VERSION`.
        gate_pytest: The ``[gate].pytest`` selectors the ``gate`` job runs.
        rows: The rows under ``rows``, in file order.
        full: The ``[full]`` class.
    """

    model_config = ConfigDict(frozen=True)

    version: int
    gate_pytest: tuple[str, ...]
    rows: tuple[Row, ...]
    full: Row


def matching_entry(row: Row, path: str) -> str | None:
    """Return the prefix or suffix of ``row`` that ``path`` matches (D1, the only matching rule).

    Args:
        row: A row of the table.
        path: A repository-relative POSIX path, as ``git`` prints it.

    Returns:
        The first prefix that matches (one ending in ``/`` starts
        ``path``, any other equals it), else the first suffix that ends
        ``path``, else None.
    """
    for prefix in row.prefixes:
        if path.startswith(prefix) if prefix.endswith("/") else path == prefix:
            return prefix
    for suffix in row.suffixes:
        if path.endswith(suffix):
            return suffix
    return None


def matches(row: Row, path: str) -> bool:
    """Return whether ``path`` belongs to ``row``.

    Args:
        row: A row of the table.
        path: A repository-relative POSIX path, as ``git`` prints it.

    Returns:
        True when :func:`matching_entry` finds an entry.
    """
    return matching_entry(row, path) is not None


def _strings(path: Path, key: str, value: Any) -> tuple[str, ...]:
    """Return ``value`` as a tuple of non-empty strings.

    Args:
        path: The table file, for the error.
        key: The dotted key ``value`` was read from.
        value: The parsed TOML value.

    Returns:
        The strings, in file order.

    Raises:
        TableError: ``value`` is not a list, or holds something other
            than a non-empty string.
    """
    if not isinstance(value, list):
        raise TableError(path, "must be a list of strings", key=key)
    for item in value:
        if not isinstance(item, str) or not item:
            raise TableError(path, f"holds {item!r}, not a non-empty string", key=key)
    return tuple(value)


def _entries(path: Path, key: str, value: Any, *, suffix: bool) -> tuple[str, ...]:
    """Return the prefixes or suffixes of a row, refusing what D1 does not define.

    A path as ``git`` prints it is relative, POSIX, normalized, so an
    entry that ``git`` could never match is a defect, never a literal.

    Args:
        path: The table file, for the error.
        key: The dotted key ``value`` was read from.
        value: The parsed TOML value.
        suffix: Whether the entries are suffixes, which may start with ``/``
            (``/conftest.py`` ends a path at a segment boundary).

    Returns:
        The entries, in file order.

    Raises:
        TableError: An entry is not a non-empty string, holds a glob
            character (``*?[]{}!``), a backslash or whitespace at an end,
            starts with ``/`` (a prefix) or ``./``, or has an empty, ``.``
            or ``..`` segment.
    """
    entries = _strings(path, key, value)
    for entry in entries:
        glob = sorted(set(entry) & _GLOB_CHARACTERS)
        if glob:
            raise TableError(
                path,
                f"holds {entry!r}: {glob[0]!r} is glob syntax; D1 matches prefixes and suffixes only",
                key=key,
            )
        body = entry[1:] if suffix and entry.startswith("/") else entry
        segments = body.rstrip("/").split("/")
        if (
            "\\" in entry
            or entry != entry.strip()
            or body.startswith("/")
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise TableError(
                path,
                f"holds {entry!r}: not a relative, normalized POSIX path as git prints it",
                key=key,
            )
    return entries


def _row(
    path: Path,
    key: str,
    name: str,
    body: Any,
    allowed: frozenset[str],
    job_ids: Collection[str],
    workflow: str,
) -> Row:
    """Build one row from its TOML table.

    Args:
        path: The table file, for the error.
        key: The dotted key of the row (``rows.docs`` or ``full``).
        name: The row's name.
        body: The parsed TOML value of the row.
        allowed: The keys this kind of row may carry.
        job_ids: The job ids the workflow defines.
        workflow: The workflow's file name, for the error.

    Returns:
        The row.

    Raises:
        TableError: The row is not a table, carries an unknown key, has
            neither prefixes nor suffixes, has no ``reason``, or names a
            job the workflow does not define.
    """
    if not isinstance(body, Mapping):
        raise TableError(path, "must be a table", key=key)
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise TableError(path, f"unknown key {unknown[0]!r}", key=key)
    prefixes = _entries(path, f"{key}.prefixes", body.get("prefixes", []), suffix=False)
    suffixes = _entries(path, f"{key}.suffixes", body.get("suffixes", []), suffix=True)
    if not prefixes and not suffixes:
        raise TableError(path, "has neither prefixes nor suffixes", key=key)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise TableError(path, "is missing or empty", key=f"{key}.reason")
    jobs: tuple[str, ...] = ()
    if "jobs" in allowed:
        if "jobs" not in body:
            raise TableError(path, "is missing", key=f"{key}.jobs")
        jobs = _strings(path, f"{key}.jobs", body["jobs"])
    for job in jobs:
        if job not in job_ids:
            raise TableError(path, f"names {job!r}, not a job of {workflow}", key=f"{key}.jobs")
    return Row(name=name, prefixes=prefixes, suffixes=suffixes, jobs=frozenset(jobs), reason=reason)


def load_table(path: Path, job_ids: Collection[str], *, workflow: str) -> AffectedTable:
    """Read and validate the table.

    Args:
        path: The table file (:data:`DEFAULT_TABLE_PATH` under the root).
        job_ids: The job ids the workflow defines; a row naming another
            is refused.
        workflow: The workflow's file name (``ci.yml``), named in errors.

    Returns:
        The validated table.

    Raises:
        TableError: The file cannot be read or parsed, its ``version`` is
            not :data:`TABLE_VERSION`, a key is unknown or missing, a row
            is named :data:`FULL_ROW` or :data:`UNKNOWN_ROW`, or a row
            names a job the workflow does not define. The message names
            the key.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TableError(path, f"cannot be read: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise TableError(path, f"is not TOML: {exc}") from exc
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise TableError(path, "is not a key of the format", key=unknown[0])
    version = data.get("version")
    if version != TABLE_VERSION:
        raise TableError(path, f"is {version!r}, this reader reads {TABLE_VERSION}", key="version")
    gate = data.get("gate", {})
    if not isinstance(gate, Mapping):
        raise TableError(path, "must be a table", key="gate")
    unknown = sorted(set(gate) - _GATE_KEYS)
    if unknown:
        raise TableError(path, "is not a key of the format", key=f"gate.{unknown[0]}")
    gate_pytest = _strings(path, "gate.pytest", gate.get("pytest", []))
    rows_body = data.get("rows")
    if not isinstance(rows_body, Mapping) or not rows_body:
        raise TableError(path, "must be a non-empty table of rows", key="rows")
    rows: list[Row] = []
    for name, body in rows_body.items():
        if name in {FULL_ROW, UNKNOWN_ROW}:
            raise TableError(path, "is a reserved row name", key=f"rows.{name}")
        rows.append(_row(path, f"rows.{name}", name, body, _ROW_KEYS, job_ids, workflow))
    if FULL_ROW not in data:
        raise TableError(path, "is missing", key=FULL_ROW)
    full = _row(path, FULL_ROW, FULL_ROW, data[FULL_ROW], _FULL_KEYS, job_ids, workflow)
    return AffectedTable(
        version=TABLE_VERSION, gate_pytest=gate_pytest, rows=tuple(rows), full=full
    )
