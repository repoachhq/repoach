"""``repoach doctor`` and the project-root guard shared by project-scoped commands.

``doctor`` prints, one ``key=value`` line each, the anchors a run
resolves (SP-UNIVERSAL-INSTALL G6): project root, user config and data
directories, the env files read, every ``repoach.toml`` key with its
source (SP-UNIVERSAL-PROJECT-CONFIG), the ledger path, the selected forge
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
    require_project_root,
    user_config_dir,
    user_data_dir,
)
from ..project.config import ConfigError, config_path
from ..project.resolve import ResolvedProjectConfig, effective_rows, resolve_project_config
from ..project.toml_write import toml_value

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


_FORGE_BINARIES: dict[str, str] = {"github": "gh", "gitlab": "glab"}
"""CLI each forge is driven through."""


def _forge_status(resolved: ResolvedProjectConfig) -> tuple[str, str | None]:
    """Return the selected forge and whether it is usable here.

    Args:
        resolved: The project's resolved configuration.

    Returns:
        The ``forge=`` value (name, source, binary state) and, when the
        forge's CLI is missing, the problem to report.
    """
    name = resolved.forge
    source = resolved.source("repoach.forge")
    binary = _FORGE_BINARIES[name]
    if shutil.which(binary) is None:
        return (
            f"{name} ({source}; {binary} not on PATH)",
            f"{binary} not on PATH -- install it to use the {name} forge",
        )
    return f"{name} ({source}; {binary} found)", None


def doctor() -> None:
    """Print the resolved anchors, configuration, forge, provider and binaries.

    Every ``repoach.toml`` key is printed as ``config.<section>.<key>=<value>
    (<source>)``.  Exits 0 when the selected forge and provider are
    usable, 2 otherwise (including outside a git working tree or with an
    invalid ``repoach.toml``).

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
    config_file = config_path(root)
    typer.echo(f"config_file={config_file} ({'present' if config_file.is_file() else 'absent'})")

    try:
        resolved = resolve_project_config(root)
    except ConfigError as exc:
        typer.echo("config=invalid")
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    for dotted, value, source in effective_rows(resolved):
        typer.echo(f"config.{dotted}={toml_value(value)} ({source})")

    problems: list[str] = []
    try:
        settings = Settings()
    except ValidationError as exc:
        typer.echo(f"settings=invalid ({exc.error_count()} error(s))")
        typer.echo(f"repoach: settings invalid: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    typer.echo(f"db_path={settings.db_path}")

    forge, forge_problem = _forge_status(resolved)
    typer.echo(f"forge={forge}")
    if forge_problem is not None:
        problems.append(forge_problem)

    if settings.llm_proxy_base_url:
        typer.echo(f"provider=gateway ({settings.llm_proxy_base_url})")
    else:
        typer.echo("provider=none")
        problems.append(
            "no provider configured -- set REPOACH_LLM_PROXY_BASE_URL (gateway) in the "
            "environment or ~/.config/repoach/env (never in a project file)"
        )

    for name in DOCTOR_BINARIES:
        typer.echo(_binary_line(name))

    for problem in problems:
        typer.echo(f"repoach: {problem}", err=True)
    if problems:
        raise typer.Exit(code=EXIT_UNUSABLE)
