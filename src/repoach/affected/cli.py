"""``repoach ci affected``: the jobs a diff affects (SP-AFFECTED-JOBS-TABLE).

``select`` prints one JSON object on stdout (SP-CLI-STDOUT-ONE-JSON),
its one-line account on stderr, and exits:

* 0 on a selection;
* 2 when the table, the workflow or ``repoach.toml`` cannot be read,
  naming the file and the key;
* 3 when ``git`` fails, naming the command.

An exit other than 0 prints no selection, so a caller goes red rather
than guessing a narrow or a full set. ``gate-pytest`` prints the
``[gate].pytest`` selectors, one per line.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..core.paths import NotARepositoryError, require_project_root
from ..project.config import ConfigError, load_project_config
from .select import (
    DEFAULT_WORKFLOW_PATH,
    GitError,
    WorkflowError,
    changed_paths,
    decide_mode,
    github_output_lines,
    select,
    summary_markdown,
    workflow_jobs,
)
from .table import DEFAULT_TABLE_PATH, AffectedTable, TableError, load_table

EXIT_SELECTED = 0
"""A selection was printed."""

EXIT_UNREADABLE = 2
"""The table, the workflow or ``repoach.toml`` cannot be read."""

EXIT_GIT = 3
"""``git`` failed."""

affected_app = typer.Typer(
    help="The CI jobs a diff affects, read from .github/affected-jobs.toml.",
    no_args_is_help=True,
)


def _fail(code: int, message: str) -> typer.Exit:
    """Print ``message`` on stderr and return the exit to raise.

    Args:
        code: The exit code.
        message: The one line naming what failed.

    Returns:
        The :class:`typer.Exit` carrying ``code``.
    """
    typer.echo(f"repoach ci affected: {message}", err=True)
    return typer.Exit(code=code)


def _root() -> Path:
    """Return the project root.

    Returns:
        The git toplevel of the working directory.

    Raises:
        typer.Exit: :data:`EXIT_GIT` outside a git working tree.
    """
    try:
        return require_project_root()
    except NotARepositoryError as exc:
        raise _fail(EXIT_GIT, str(exc)) from exc


def _load(root: Path, table: str, workflow: str) -> tuple[AffectedTable, dict[str, frozenset[str]]]:
    """Read the workflow's jobs and the table validated against them.

    Args:
        root: The project root.
        table: The table path, relative to ``root`` unless absolute.
        workflow: The workflow path, relative to ``root`` unless absolute.

    Returns:
        The table and :func:`~repoach.affected.select.workflow_jobs`.

    Raises:
        typer.Exit: :data:`EXIT_UNREADABLE` when either cannot be read.
    """
    workflow_path = root / workflow
    try:
        needs = workflow_jobs(workflow_path)
        loaded = load_table(root / table, needs, workflow=workflow_path.name)
    except (WorkflowError, TableError) as exc:
        raise _fail(EXIT_UNREADABLE, str(exc)) from exc
    return loaded, needs


@affected_app.command("select")
def select_cmd(
    base: str | None = typer.Option(
        None, "--base", help="Base revision; default origin/<[repoach].integration_branch>."
    ),
    head: str = typer.Option("HEAD", "--head", help="Head revision."),
    event: str = typer.Option(
        "pull_request", "--event", help="The CI event: pull_request, push, workflow_dispatch."
    ),
    base_ref: str | None = typer.Option(
        None,
        "--base-ref",
        help="The pull request's base branch; default [repoach].integration_branch, empty on a push.",
    ),
    table: str = typer.Option(DEFAULT_TABLE_PATH, "--table", help="The path-to-job table."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
    github_output: str | None = typer.Option(
        None, "--github-output", help="Append job_<id>=true|false and mode= lines to this file."
    ),
    summary: str | None = typer.Option(
        None, "--summary", help="Append the selection as a markdown table to this file."
    ),
) -> None:
    """Print the jobs the diff from the merge-base with --base to --head affects.

    Raises:
        typer.Exit: 2 on an unreadable table, workflow or ``repoach.toml``;
            3 when ``git`` fails.
    """
    root = _root()
    try:
        branches = load_project_config(root).repoach
    except ConfigError as exc:
        raise _fail(EXIT_UNREADABLE, str(exc)) from exc
    loaded, needs = _load(root, table, workflow)
    base_rev = base if base is not None else f"origin/{branches.integration_branch}"
    pr_base = base_ref if base_ref is not None else branches.integration_branch
    mode, reason = decide_mode(
        event=event, base_ref=pr_base, ref=base_rev, integration_branch=branches.integration_branch
    )
    try:
        paths = changed_paths(root, base=base_rev, head=head)
    except GitError as exc:
        raise _fail(EXIT_GIT, str(exc)) from exc
    selection = select(paths, loaded, needs, mode=mode, reason=reason)
    if github_output is not None:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(
                "".join(f"{line}\n" for line in github_output_lines(selection, list(needs)))
            )
    if summary is not None:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(summary_markdown(selection))
    typer.echo(
        f"repoach ci affected: {selection.mode} ({selection.reason}): "
        f"{', '.join(sorted(selection.jobs))} for {selection.paths} path(s)",
        err=True,
    )
    typer.echo(json.dumps(selection.model_dump(mode="json"), indent=2, ensure_ascii=False))


@affected_app.command("gate-pytest")
def gate_pytest_cmd(
    table: str = typer.Option(DEFAULT_TABLE_PATH, "--table", help="The path-to-job table."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
) -> None:
    """Print the [gate].pytest selectors, one per line.

    Raises:
        typer.Exit: 2 on an unreadable table or workflow; 3 outside a
            git working tree.
    """
    loaded, _ = _load(_root(), table, workflow)
    for selector in loaded.gate_pytest:
        typer.echo(selector)
