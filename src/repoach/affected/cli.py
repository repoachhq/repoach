"""``repoach ci affected``: the jobs a diff affects (SP-AFFECTED-JOBS-TABLE).

``select`` prints one JSON object on stdout (SP-CLI-STDOUT-ONE-JSON),
its one-line account on stderr, and exits:

* 0 on a selection;
* 2 when the table, the workflow or ``repoach.toml`` cannot be read,
  naming the file and the key;
* 3 when ``git`` fails, naming the command.

An exit other than 0 prints no selection, so a caller goes red rather
than guessing a narrow or a full set. ``gate-pytest`` prints the
``[gate].pytest`` selectors, one per line; ``base`` prints the default
base revision, the one place a shell caller reads the integration branch from;
``matrix <job> <axis>`` prints one matrix axis of a ``ci.yml`` job;
``job-running <command>`` prints the one job whose step runs a command.
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
    default_base,
    github_output_lines,
    job_running,
    matrix_values,
    one_line,
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


def fail_with(code: int, message: str) -> typer.Exit:
    """Print ``message`` on stderr and return the exit to raise.

    Args:
        code: The exit code.
        message: The one line naming what failed.

    Returns:
        The :class:`typer.Exit` carrying ``code``.
    """
    typer.echo(f"repoach ci affected: {message}", err=True)
    return typer.Exit(code=code)


def project_root() -> Path:
    """Return the project root.

    Returns:
        The git toplevel of the working directory.

    Raises:
        typer.Exit: :data:`EXIT_GIT` outside a git working tree.
    """
    try:
        return require_project_root()
    except NotARepositoryError as exc:
        raise fail_with(EXIT_GIT, str(exc)) from exc


def read_integration_branch(root: Path) -> str:
    """Return ``[repoach].integration_branch`` of the project.

    Args:
        root: The project root.

    Returns:
        The branch name (``develop`` without a ``repoach.toml``).

    Raises:
        typer.Exit: :data:`EXIT_UNREADABLE` when ``repoach.toml`` is invalid.
    """
    try:
        return load_project_config(root).repoach.integration_branch
    except ConfigError as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc


def load_inputs(
    root: Path, table: str, workflow: str
) -> tuple[AffectedTable, dict[str, frozenset[str]]]:
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
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
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
    root = project_root()
    integration_branch = read_integration_branch(root)
    loaded, needs = load_inputs(root, table, workflow)
    base_rev = base if base is not None else default_base(integration_branch)
    pr_base = base_ref if base_ref is not None else integration_branch
    mode, reason = decide_mode(
        event=event, base_ref=pr_base, ref=base_rev, integration_branch=integration_branch
    )
    try:
        paths = changed_paths(root, base=base_rev, head=head)
    except GitError as exc:
        raise fail_with(EXIT_GIT, str(exc)) from exc
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
        f"repoach ci affected: {selection.mode} ({one_line(selection.reason)}): "
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
    loaded, _ = load_inputs(project_root(), table, workflow)
    for selector in loaded.gate_pytest:
        typer.echo(selector)


@affected_app.command("base")
def base_cmd() -> None:
    """Print the default base revision, ``origin/<[repoach].integration_branch>``.

    Raises:
        typer.Exit: 2 on an invalid ``repoach.toml``; 3 outside a git
            working tree.
    """
    typer.echo(default_base(read_integration_branch(project_root())))


@affected_app.command("matrix")
def matrix_cmd(
    job: str = typer.Argument(..., help="The job id (wheel-smoke)."),
    axis: str = typer.Argument(..., help="The matrix key (python-version)."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
) -> None:
    """Print the values of one matrix axis of one workflow job, one per line.

    Raises:
        typer.Exit: 2 when the workflow, the job or the axis cannot be
            read; 3 outside a git working tree.
    """
    try:
        values = matrix_values(project_root() / workflow, job, axis)
    except WorkflowError as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    for value in values:
        typer.echo(value)


@affected_app.command("job-running")
def job_running_cmd(
    command: str = typer.Argument(..., help="A command line as a ci.yml step runs it."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
) -> None:
    """Print the id of the one workflow job with a step that runs COMMAND.

    Raises:
        typer.Exit: 2 when the workflow cannot be read or not exactly one
            job runs the command; 3 outside a git working tree.
    """
    try:
        job_id = job_running(project_root() / workflow, command)
    except WorkflowError as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    typer.echo(job_id)
