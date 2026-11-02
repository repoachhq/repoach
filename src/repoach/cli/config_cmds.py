"""``repoach config show`` — the effective project configuration and its sources.

Prints the merged configuration of the current repository as TOML, each
key followed by ``# source: default|file|env|origin`` (SP-UNIVERSAL-PROJECT-CONFIG
Outputs).  ``[state]`` shows the effective ledger and dump paths.  It
contacts neither a forge nor a provider and needs no gateway.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TypeAlias

import typer

from ..project.config import ConfigError, config_path
from ..project.resolve import (
    UNDECLARED_COMMAND_NOTE,
    ResolvedProjectConfig,
    effective_rows,
    resolve_project_config,
)
from ..project.toml_write import (
    TomlValue,
    array_of_tables_blocks,
    is_array_of_tables,
    toml_value,
)
from .doctor_cmds import EXIT_UNUSABLE, project_root_or_exit

config_app = typer.Typer(help="Inspect the project configuration (repoach.toml).")


Row: TypeAlias = "tuple[str, TomlValue | None, str]"
"""One ``(section.key, value, source)`` row of :func:`~repoach.project.resolve.effective_rows`."""


def by_section(rows: Iterable[Row]) -> list[tuple[str, list[Row]]]:
    """Group the rows by section, each section's arrays of tables after its plain keys.

    A key written after a ``[[section.key]]`` block belongs to that
    block's last entry, so ``config show`` lists a section's plain keys
    first and its arrays of tables last; the order is otherwise kept.

    Args:
        rows: The effective rows, in file order.

    Returns:
        One ``(section, rows)`` pair per section, in first-seen order.
    """
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row[0].partition(".")[0], []).append(row)
    return [
        (section, sorted(members, key=lambda row: is_array_of_tables(row[1])))
        for section, members in grouped.items()
    ]


def render_show(resolved: ResolvedProjectConfig) -> str:
    """Render the effective configuration as annotated TOML.

    Each section lists its plain keys, then each array of tables
    (``[[review.generated]]``) as one ``[[section.key]]`` block per entry
    (:func:`by_section`), each block after a blank line; every line of a
    block carries the source of the whole array.

    Args:
        resolved: The resolved configuration.

    Returns:
        The text ``config show`` prints.
    """
    lines = [f"# {config_path(resolved.root)}"]
    for section, rows in by_section(effective_rows(resolved)):
        lines.extend(["", f"[{section}]"])
        for dotted, value, source in rows:
            key = dotted.partition(".")[2]
            note = f"  # source: {source}"
            if value is None:
                lines.append(f"# {key}: {UNDECLARED_COMMAND_NOTE}")
            elif is_array_of_tables(value):
                for block in array_of_tables_blocks(section, key, value):
                    lines.append("")
                    lines.extend(line + note for line in block)
            else:
                lines.append(f"{key} = {toml_value(value)}{note}")
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
