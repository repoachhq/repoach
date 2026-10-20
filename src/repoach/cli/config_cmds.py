"""``repoach config show`` — the effective project configuration and its sources.

Prints the merged configuration of the current repository as TOML, each
key followed by ``# source: default|file|env|origin`` (SP-UNIVERSAL-PROJECT-CONFIG
Outputs).  ``[state]`` shows the effective ledger and dump paths.  It
contacts neither a forge nor a provider and needs no gateway.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..project.config import ConfigError, config_path
from ..project.resolve import (
    UNDECLARED_COMMAND_NOTE,
    ResolvedProjectConfig,
    effective_rows,
    resolve_project_config,
)
from ..project.toml_write import toml_value
from .doctor_cmds import EXIT_UNUSABLE, project_root_or_exit

config_app = typer.Typer(help="Inspect the project configuration (repoach.toml).")


def render_show(resolved: ResolvedProjectConfig) -> str:
    """Render the effective configuration as annotated TOML.

    Args:
        resolved: The resolved configuration.

    Returns:
        The text ``config show`` prints.
    """
    lines = [f"# {config_path(resolved.root)}"]
    current = ""
    for dotted, value, source in effective_rows(resolved):
        section, _, key = dotted.partition(".")
        if section != current:
            lines.extend(["", f"[{section}]"])
            current = section
        if value is None:
            lines.append(f"# {key}: {UNDECLARED_COMMAND_NOTE}")
            continue
        lines.append(f"{key} = {toml_value(value)}  # source: {source}")
    return "\n".join(lines)


def load_resolved_or_exit(root: Path) -> ResolvedProjectConfig:
    """Resolve the configuration of ``root`` or end the command with rc 2.

    Args:
        root: The project root.

    Returns:
        The resolved configuration.

    Raises:
        typer.Exit: With code 2 when ``repoach.toml`` or an environment
            value is invalid; the message names the file, key and line.
    """
    try:
        return resolve_project_config(root)
    except ConfigError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc


@config_app.command("show")
def config_show() -> None:
    """Print the effective configuration with the source of every key."""
    root = project_root_or_exit()
    typer.echo(render_show(load_resolved_or_exit(root)))
