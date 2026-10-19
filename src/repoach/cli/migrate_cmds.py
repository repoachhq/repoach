"""``repoach migrate-config`` — move pre-SP-UNIVERSAL-INSTALL state to its new home.

Two legacy locations are still honoured with a deprecation notice: a
repoach checkout's own ``.env`` (read by the CLI in place of
``.repoach.env``) and a regular ``data/repoach.db`` ledger at the
project root.  This command retires both:

* ``.repoach.env`` becomes a relative symlink to ``.env``, so the CLI
  and the gateway keep reading one file.  Only in a repoach checkout:
  a foreign project's ``.env`` is never handed to repoach.
* the ledger (with its ``-wal`` / ``-shm`` / ``-journal`` siblings) is
  moved to :func:`~repoach.core.paths.default_db_path`, and a symlink
  is left at ``data/repoach.db`` so a gateway still started with the
  old relative path keeps writing the same file.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer

from ..core.paths import (
    LEGACY_ENV_FILE_NAME,
    PROJECT_ENV_FILE_NAME,
    default_db_path,
    legacy_db_path,
    legacy_env_file,
)
from .doctor_cmds import EXIT_UNUSABLE, project_root_or_exit

LEDGER_SIBLING_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
"""SQLite side files that must travel with the ledger."""


class MigrationRefusedError(RuntimeError):
    """A migration step would overwrite existing state."""


def migrate_env(root: Path, *, dry_run: bool) -> str:
    """Link ``.repoach.env`` to the legacy ``.env`` of a repoach checkout.

    Args:
        root: The project root.
        dry_run: Report the step without touching the filesystem.

    Returns:
        One human-readable line describing what was (or would be) done.
    """
    legacy = legacy_env_file(root)
    if legacy is None:
        return "migrate: env -- nothing to do"
    target = root / PROJECT_ENV_FILE_NAME
    if not dry_run:
        target.symlink_to(LEGACY_ENV_FILE_NAME)
    return f"migrate: env -- {target} -> {LEGACY_ENV_FILE_NAME}"


def migrate_ledger(root: Path, *, dry_run: bool) -> str:
    """Move a legacy ``data/repoach.db`` to the XDG ledger and link it back.

    Args:
        root: The project root.
        dry_run: Report the step without touching the filesystem.

    Returns:
        One human-readable line describing what was (or would be) done.

    Raises:
        MigrationRefusedError: A ledger already exists at the destination.
    """
    legacy = legacy_db_path(root)
    if legacy is None:
        return "migrate: ledger -- nothing to do"
    destination = default_db_path(root)
    if destination.exists():
        raise MigrationRefusedError(
            f"ledger already exists at {destination}; merge or remove it, then rerun"
        )
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in LEDGER_SIBLING_SUFFIXES:
            sibling = legacy.with_name(legacy.name + suffix)
            if sibling.exists():
                shutil.move(sibling, destination.with_name(destination.name + suffix))
        shutil.move(legacy, destination)
        legacy.symlink_to(destination)
    return f"migrate: ledger -- {legacy} moved to {destination} (symlink left behind)"


def migrate_config(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the steps without changing anything."),
    ] = False,
) -> None:
    """Move legacy ``.env`` / ``data/repoach.db`` state to the new locations.

    Raises:
        typer.Exit: With code 2 outside a git working tree or when a
            step would overwrite existing state.
    """
    root = project_root_or_exit()
    typer.echo(migrate_env(root, dry_run=dry_run))
    try:
        typer.echo(migrate_ledger(root, dry_run=dry_run))
    except MigrationRefusedError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
