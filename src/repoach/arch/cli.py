"""The ``repoach arch`` command group.

``repoach arch graph`` derives the architecture dependency graph from
spec frontmatter and renders it; with ``--check`` it validates the corpus
(disjoint ownership, no cycles, well-formed frontmatter) and exits non-zero
on a violation. Frontier (un-owned) specs and dangling edges are reported
proportionately — one aggregated line — and never fail the run.
"""

from __future__ import annotations

from pathlib import Path

import typer

from repoach.arch.registry import MalformedFrontmatterError, load_registry
from repoach.arch.render import render

arch_app = typer.Typer(
    help="Architecture graph derived from spec frontmatter.",
    no_args_is_help=True,
)


@arch_app.command("graph")
def graph_command(
    output_format: str = typer.Option(
        "mermaid", "--format", help="Render as mermaid, json or dot."
    ),
    check: bool = typer.Option(
        False, "--check", help="Validate the corpus; exit 2 on a cycle or conflict."
    ),
    specs_dir: Path = typer.Option(
        Path("docs/specs"), "--specs-dir", help="Directory of spec markdown files."
    ),
) -> None:
    """Derive and render (or, with ``--check``, validate) the graph."""
    try:
        registry = load_registry(specs_dir)
    except MalformedFrontmatterError as exc:
        typer.echo(f"malformed spec frontmatter: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    conflicts = registry.disjointness_violations()
    cycles = registry.graph().cycles()

    frontier = registry.frontier_ids()
    if frontier:
        typer.echo(f"frontier: {len(frontier)} un-owned spec(s): {list(frontier)}", err=True)
    dangling = registry.dangling_edges()
    if dangling:
        pairs = [f"{source}->{target}" for source, target in dangling]
        typer.echo(f"dangling depends_on: {len(dangling)}: {pairs}", err=True)

    if check:
        for conflict in conflicts:
            typer.echo(
                f"ownership conflict: {conflict.artifact} owned by {list(conflict.spec_ids)}",
                err=True,
            )
        for cycle in cycles:
            typer.echo(f"dependency cycle: {' -> '.join(cycle)}", err=True)
        if conflicts or cycles:
            raise typer.Exit(code=2)

    typer.echo(render(registry, output_format))
