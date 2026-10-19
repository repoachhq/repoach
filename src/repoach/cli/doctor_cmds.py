"""``repoach doctor`` and the project-root guard shared by project-scoped commands.

``doctor`` prints, one ``key=value`` line each, the anchors a run
resolves (SP-UNIVERSAL-INSTALL G6): project root, user config and data
directories, the env files read, the ledger path, the selected forge
and provider, and whether each external binary is on ``PATH``.  It
builds :class:`~repoach.core.config.Settings` directly (no directory is
created) and never contacts a forge or a provider.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from pydantic import ValidationError

from .. import __version__
from ..core.config import Settings, _anchored_env_files
from ..core.paths import (
    NotARepositoryError,
    project_slug,
    require_project_root,
    user_config_dir,
    user_data_dir,
)

DOCTOR_BINARIES: tuple[str, ...] = ("git", "gh", "glab", "claude")
"""External executables whose presence ``doctor`` reports."""

EXIT_UNUSABLE = 2
"""Exit code when the project, forge or provider is not usable."""


def project_root_or_exit(start: Path | None = None) -> Path:
    """Return the git toplevel of ``start`` or end the command with rc 2.

    Args:
        start: Directory to resolve from; defaults to the current
            working directory.

    Returns:
        The absolute git toplevel.

    Raises:
        typer.Exit: With code 2 after naming the directory on stderr,
            when it is not inside a git working tree or ``git`` is absent.
    """
    try:
        return require_project_root(start)
    except NotARepositoryError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc


def _binary_line(name: str) -> str:
    """Return the ``doctor`` line for one executable.

    Args:
        name: Executable name looked up on ``PATH``.

    Returns:
        ``<name>=<absolute path>`` or ``<name>=missing``.
    """
    found = shutil.which(name)
    return f"{name}={found if found is not None else 'missing'}"


def _forge_status(root: Path) -> tuple[str, bool]:
    """Return the selected forge and whether it is usable here.

    GitHub through ``gh`` is the only shipped forge until
    SP-UNIVERSAL-FORGE lands; a GitLab ``origin`` is named but reported
    unusable rather than silently treated as GitHub.

    Args:
        root: The project root.

    Returns:
        The ``forge=`` value and its usability.
    """
    host = project_slug(root).split("/", 1)[0]
    if "gitlab" in host:
        return "gitlab (not supported until SP-UNIVERSAL-FORGE)", False
    if shutil.which("gh") is None:
        return "github (gh missing)", False
    return "github (gh found)", True


def doctor() -> None:
    """Print the resolved anchors, forge, provider and binaries.

    Exits 0 when the selected forge and provider are usable, 2
    otherwise (including outside a git working tree).

    Raises:
        typer.Exit: With code 2 when anything required is unusable.
    """
    root = project_root_or_exit()
    typer.echo(f"repoach={__version__}")
    typer.echo(f"project_root={root}")
    typer.echo(f"user_config_dir={user_config_dir()}")
    typer.echo(f"user_data_dir={user_data_dir()}")
    for env_file in _anchored_env_files(root):
        state = "present" if env_file.is_file() else "absent"
        typer.echo(f"env_file={env_file} ({state})")

    problems: list[str] = []
    try:
        settings = Settings()
    except ValidationError as exc:
        typer.echo(f"settings=invalid ({exc.error_count()} error(s))")
        typer.echo(f"repoach: settings invalid: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    typer.echo(f"db_path={settings.db_path}")

    forge, forge_ok = _forge_status(root)
    typer.echo(f"forge={forge}")
    if not forge_ok:
        problems.append(f"forge unusable: {forge}")

    if settings.llm_proxy_base_url:
        typer.echo(f"provider=gateway ({settings.llm_proxy_base_url})")
    else:
        typer.echo("provider=none")
        problems.append(
            "no provider configured -- set REPOACH_LLM_PROXY_BASE_URL (gateway) in the "
            "environment, ~/.config/repoach/env or <project>/.repoach.env"
        )

    for name in DOCTOR_BINARIES:
        typer.echo(_binary_line(name))

    for problem in problems:
        typer.echo(f"repoach: {problem}", err=True)
    if problems:
        raise typer.Exit(code=EXIT_UNUSABLE)
