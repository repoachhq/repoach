"""``repoach migrate-config`` — move pre-SP-UNIVERSAL-INSTALL state to its new home.

Two legacy locations are still honoured with a deprecation notice: a
repoach checkout's own ``.env`` (read by the CLI in place of
``.repoach.env``) and a regular ``data/repoach.db`` ledger at the
project root.  This command retires both:

* the CLI keys of ``.env`` are split by the project allow-list
  (:data:`~repoach.core.config.PROJECT_ENV_ALLOWED`): layout and
  convention keys go to an untracked ``.repoach.env``, every secret,
  endpoint, path or command (the gateway URL and token first) to the
  user-level ``~/.config/repoach/env``, created ``0600``.  ``.env``
  itself is left untouched for the gateway.  Only in a repoach
  checkout: a foreign project's ``.env`` is never handed to repoach.
* the ledger is copied to :func:`~repoach.core.paths.default_db_path`
  with SQLite's backup API while a write lock is held, verified
  (``PRAGMA integrity_check`` and per-table row counts), and only then
  is the source removed and replaced by a symlink.  **Stop the gateway
  first**: any process holding the ledger open is named and the
  migration refuses, because a writer that kept the old file open
  would commit into a file nobody reads any more.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values
from pydantic import AliasChoices

from ..core.config import PROJECT_ENV_ALLOWED, Settings
from ..core.logging import get_logger
from ..core.paths import (
    PROJECT_ENV_FILE_NAME,
    USER_ENV_FILE_NAME,
    default_db_path,
    legacy_db_path,
    legacy_env_file,
    user_config_dir,
)
from .doctor_cmds import EXIT_UNUSABLE, project_root_or_exit

_log = get_logger(__name__)

LEDGER_SIBLING_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
"""SQLite side files that belong to the ledger."""

LOCK_TIMEOUT_S = 2.0
"""How long to wait for the ledger's write lock before refusing."""

PRIVATE_FILE_MODE = 0o600
"""Permissions of every env file this command writes."""


class MigrationRefusedError(RuntimeError):
    """A migration step would lose or overwrite state."""


def _core_env_names() -> set[str]:
    """Return every env name the core :class:`Settings` reads (upper-case)."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(f"REPOACH_{field_name}".upper())
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(choice.upper() for choice in alias.choices if isinstance(choice, str))
    return names


def _dotenv_line(key: str, value: str) -> str:
    """Render one double-quoted dotenv assignment."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"\n'


def _write_private(path: Path, lines: list[str], *, append: bool) -> None:
    """Write (or append) ``lines`` to ``path`` with mode ``0600``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    path.chmod(PRIVATE_FILE_MODE)


def migrate_env(root: Path, *, dry_run: bool) -> str:
    """Split a repoach checkout's legacy ``.env`` into project and user files.

    Args:
        root: The project root.
        dry_run: Report the step without touching the filesystem.

    Returns:
        One human-readable line describing what was (or would be) done.

    Raises:
        MigrationRefusedError: The user file already sets one of the
            keys to a different value.
    """
    legacy = legacy_env_file(root)
    if legacy is None:
        return "migrate: env -- nothing to do"
    core_names = _core_env_names()
    project_lines: list[str] = []
    user_values: dict[str, str] = {}
    for key, value in dotenv_values(legacy, interpolate=False).items():
        if value is None or key.upper() not in core_names:
            continue
        if key.upper() in PROJECT_ENV_ALLOWED:
            project_lines.append(_dotenv_line(key, value))
        else:
            user_values[key] = value
    user_env = user_config_dir() / USER_ENV_FILE_NAME
    existing = dotenv_values(user_env, interpolate=False) if user_env.is_file() else {}
    conflicts = sorted(k for k, v in user_values.items() if k in existing and existing[k] != v)
    if conflicts:
        raise MigrationRefusedError(
            f"{user_env} already sets {', '.join(conflicts)} to other values; reconcile by hand"
        )
    user_lines = [_dotenv_line(k, v) for k, v in user_values.items() if k not in existing]
    project_env = root / PROJECT_ENV_FILE_NAME
    if not dry_run:
        if user_lines:
            _write_private(user_env, user_lines, append=True)
        _write_private(project_env, project_lines, append=False)
    return (
        f"migrate: env -- {len(user_lines)} key(s) to {user_env}, "
        f"{len(project_lines)} to {project_env}; {legacy} left for the gateway"
    )


def _open_holders(paths_to_check: list[Path]) -> list[str]:
    """Return ``pid (command)`` for every other process holding one of the files open.

    Linux only (``/proc``); elsewhere the SQLite lock is the only guard.
    Processes whose descriptors cannot be read (other users' processes)
    are counted and reported in a single debug line, never one line per
    process, so a refusal is not buried under permission noise.

    Args:
        paths_to_check: The ledger and its side files.

    Returns:
        One entry per holding process.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    wanted = {str(path.resolve()) for path in paths_to_check if path.exists()}
    holders: list[str] = []
    unreadable: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            targets = {os.readlink(fd) for fd in (entry / "fd").iterdir()}
            command = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError as exc:
            unreadable.append(type(exc).__name__)
        else:
            if targets & wanted:
                holders.append(f"{entry.name} ({command})")
    if unreadable:
        _log.debug("migrate.proc_unreadable", count=len(unreadable))
    return holders


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Return ``{table: row count}`` for every user table."""
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def _copy_verified(source: Path, destination: Path) -> dict[str, int]:
    """Back up ``source`` into ``destination`` and verify the copy.

    Args:
        source: The live ledger; the caller holds its write lock.
        destination: A path that does not exist yet.

    Returns:
        The verified per-table row counts.

    Raises:
        MigrationRefusedError: The copy failed its integrity check or
            its row counts differ from the source.
    """
    reader = sqlite3.connect(source, timeout=LOCK_TIMEOUT_S)
    copy = sqlite3.connect(destination)
    try:
        reader.backup(copy)
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        expected = _table_counts(reader)
        actual = _table_counts(copy)
    finally:
        copy.close()
        reader.close()
    if integrity != "ok" or expected != actual:
        destination.unlink()
        raise MigrationRefusedError(
            f"copy of {source} failed verification (integrity={integrity}, "
            f"rows {expected} != {actual}); source left in place"
        )
    return actual


def migrate_ledger(root: Path, *, dry_run: bool) -> str:
    """Move a legacy ``data/repoach.db`` to the XDG ledger and link it back.

    Args:
        root: The project root.
        dry_run: Report the step without touching the filesystem.

    Returns:
        One human-readable line describing what was (or would be) done.

    Raises:
        MigrationRefusedError: A ledger already exists at the
            destination, another process holds the ledger open, its
            write lock cannot be taken within :data:`LOCK_TIMEOUT_S`, or
            the copy fails verification.
    """
    legacy = legacy_db_path(root)
    if legacy is None:
        return "migrate: ledger -- nothing to do"
    destination = default_db_path(root)
    if destination.exists():
        raise MigrationRefusedError(
            f"ledger already exists at {destination}; merge or remove it, then rerun"
        )
    siblings = [legacy.with_name(legacy.name + suffix) for suffix in LEDGER_SIBLING_SUFFIXES]
    holders = _open_holders([legacy, *siblings])
    if holders:
        raise MigrationRefusedError(
            f"{legacy} is open by {', '.join(holders)}; stop the gateway and any repoach run first"
        )
    if dry_run:
        return f"migrate: ledger -- {legacy} would move to {destination}"
    lock = sqlite3.connect(legacy, timeout=LOCK_TIMEOUT_S, isolation_level=None)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationRefusedError(
                f"cannot lock {legacy} within {LOCK_TIMEOUT_S}s ({exc}); another writer "
                "holds it -- stop the gateway first"
            ) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(destination.name + ".migrating")
        counts = _copy_verified(legacy, staging)
        staging.rename(destination)
        legacy.unlink()
        for sibling in siblings:
            sibling.unlink(missing_ok=True)
        legacy.symlink_to(destination)
        lock.execute("ROLLBACK")
    finally:
        lock.close()
    return (
        f"migrate: ledger -- {legacy} copied to {destination} and verified "
        f"({sum(counts.values())} rows in {len(counts)} tables); symlink left behind"
    )


def migrate_config(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the steps without changing anything."),
    ] = False,
) -> None:
    """Move legacy ``.env`` / ``data/repoach.db`` state to the new locations.

    Stop the gateway (and any running repoach command) first: the ledger
    is not moved while another process holds it open.

    Raises:
        typer.Exit: With code 2 outside a git working tree or when a
            step would lose or overwrite state.
    """
    root = project_root_or_exit()
    try:
        typer.echo(migrate_env(root, dry_run=dry_run))
        typer.echo(migrate_ledger(root, dry_run=dry_run))
    except MigrationRefusedError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
