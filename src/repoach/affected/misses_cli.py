"""``repoach ci affected harvest | replay | record`` (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES).

The three commands keep the corpus and the misses table. Each prints one
JSON object on stdout (SP-CLI-STDOUT-ONE-JSON), the ``MISS`` lines and
``misses: <n>/<failed jobs>`` on stderr, and exits:

* 0;
* 1 on a miss (``harvest``, ``replay``) or a duplicate row (``record``);
* 2 when the forge or a file refuses, naming it; nothing is written;
* 3 outside a git working tree.

:func:`register` adds them to ``repoach ci affected``
(:data:`~.cli.affected_app`).
"""

from __future__ import annotations

import json
from datetime import date

import typer

from . import gh as gh_module
from . import harvest as harvest_module
from .cli import (
    EXIT_UNREADABLE,
    fail_with,
    load_inputs,
    project_root,
    read_integration_branch,
)
from .harvest import HARVEST_REFUSALS, HARVEST_WINDOW, corpus_source, write_corpus
from .misses import (
    DEFAULT_CORPUS_PATH,
    DEFAULT_MISSES_PATH,
    MISS,
    PATHS_SEPARATOR,
    VERDICTS,
    CorpusError,
    DuplicateRowError,
    MissesError,
    MissRow,
    UnknownConclusionError,
    UnknownJobError,
    load_corpus,
    load_misses,
    record_row,
    replay,
    report_lines,
)
from .select import DEFAULT_WORKFLOW_PATH, WorkflowError, check_name_jobs
from .table import DEFAULT_TABLE_PATH

EXIT_MISS = 1
"""``harvest`` or ``replay`` found a miss; ``record`` met a duplicate row."""


def _echo_report(lines: list[str], payload: dict[str, object]) -> None:
    """Print a report's lines on stderr and its JSON object on stdout.

    Args:
        lines: The stderr lines, in order.
        payload: The one JSON object (SP-CLI-STDOUT-ONE-JSON).
    """
    for line in lines:
        typer.echo(line, err=True)
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


def harvest_cmd(
    merged: int = typer.Option(
        HARVEST_WINDOW, "--merged", min=1, help="How many merged pull requests to read."
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="owner/name; default the repository gh resolves for this tree."
    ),
    out: str = typer.Option(DEFAULT_CORPUS_PATH, "--out", help="The corpus to rewrite."),
    table: str = typer.Option(DEFAULT_TABLE_PATH, "--table", help="The path-to-job table."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
    misses: str = typer.Option(DEFAULT_MISSES_PATH, "--misses", help="The misses table."),
) -> None:
    """Rewrite the corpus from the forge and name every new miss.

    Raises:
        typer.Exit: 1 when a failed job is unselected and no row covers
            it; 2 when an input or the forge refuses (the corpus is
            untouched); 3 outside a git working tree.
    """
    root = project_root()
    base = read_integration_branch(root)
    try:
        result = harvest_module.harvest_and_replay(
            root,
            repo=repo,
            merged=merged,
            base=base,
            table=table,
            workflow=workflow,
            misses=misses,
            gh=gh_module.run_gh,
        )
        name = harvest_module.workflow_name(root / workflow)
    except HARVEST_REFUSALS as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    try:
        write_corpus(
            result.harvest.runs,
            root / out,
            repo=result.harvest.repo,
            harvested=date.today(),
            source=corpus_source(base, name),
        )
    except OSError as exc:
        raise fail_with(
            EXIT_UNREADABLE, f"{out}: cannot be written: {exc.strerror or exc}"
        ) from exc
    report = result.report
    lines = report_lines(report, count=report.new_misses)
    lines.insert(-1, f"not admitted: {result.harvest.not_admitted}")
    _echo_report(
        lines,
        {
            "repo": result.harvest.repo,
            "merged": merged,
            "out": out,
            "runs": len(result.harvest.runs),
            "not_admitted": result.harvest.not_admitted,
            "failed_jobs": report.failed_jobs,
            "new_misses": [line.model_dump(mode="json") for line in report.new_misses],
        },
    )
    if report.new_misses:
        raise typer.Exit(code=EXIT_MISS)


def replay_cmd(
    corpus: str = typer.Option(DEFAULT_CORPUS_PATH, "--corpus", help="The corpus."),
    misses: str = typer.Option(DEFAULT_MISSES_PATH, "--misses", help="The misses table."),
    table: str = typer.Option(DEFAULT_TABLE_PATH, "--table", help="The path-to-job table."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
) -> None:
    """Replay the corpus and the misses table against the table.

    Raises:
        typer.Exit: 1 when a failed job or a ``miss`` row is unselected;
            2 when an input cannot be read or names an unknown job or job conclusion; 3
            outside a git working tree.
    """
    root = project_root()
    loaded, needs = load_inputs(root, table, workflow)
    try:
        names = check_name_jobs(root / workflow)
        report = replay(
            load_corpus(root / corpus), load_misses(root / misses), loaded, needs, names
        )
    except (
        WorkflowError,
        CorpusError,
        MissesError,
        UnknownJobError,
        UnknownConclusionError,
    ) as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    _echo_report(
        report_lines(report, count=report.misses),
        {
            "failed_jobs": report.failed_jobs,
            "misses": [line.model_dump(mode="json") for line in report.misses],
            "recorded": [line.model_dump(mode="json") for line in report.recorded],
            "truncated": [line.model_dump(mode="json") for line in report.truncated],
        },
    )
    if report.misses:
        raise typer.Exit(code=EXIT_MISS)


def record_cmd(
    pr: int = typer.Option(..., "--pr", min=0, help="The pull request the red is charged to."),
    run: int = typer.Option(..., "--run", min=0, help="The run that went red."),
    job: str = typer.Option(..., "--job", help="The check name that failed."),
    verdict: str = typer.Option(..., "--verdict", help="miss, conflict or external."),
    note: str = typer.Option(..., "--note", help="Why the verdict, in one line."),
    paths: str | None = typer.Option(
        None, "--paths", help="The ;-joined paths to replay; default the run's head in the corpus."
    ),
    head_sha: str | None = typer.Option(
        None, "--head-sha", help="The commit the run judged; default the run's head in the corpus."
    ),
    corpus: str = typer.Option(DEFAULT_CORPUS_PATH, "--corpus", help="The corpus."),
    misses: str = typer.Option(DEFAULT_MISSES_PATH, "--misses", help="The misses table."),
    workflow: str = typer.Option(DEFAULT_WORKFLOW_PATH, "--workflow", help="The CI workflow."),
) -> None:
    """Add one row to the misses table, sorted; never change another.

    Raises:
        typer.Exit: 1 when ``(run, job)`` is already a row; 2 when the
            verdict is unknown, the job is no check of the workflow, a
            ``miss`` has no paths, or a file cannot be read or written;
            3 outside a git working tree.
    """
    root = project_root()
    if verdict not in VERDICTS:
        raise fail_with(EXIT_UNREADABLE, f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    try:
        if job not in check_name_jobs(root / workflow):
            raise UnknownJobError(job, "--job")
        head = None
        if paths is None or head_sha is None:
            head = next(
                (item for item in load_corpus(root / corpus) if (item.pr, item.run) == (pr, run)),
                None,
            )
    except (WorkflowError, CorpusError, UnknownJobError) as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    if paths is not None:
        row_paths = tuple(item for item in paths.split(PATHS_SEPARATOR) if item)
    else:
        row_paths = head.paths if head is not None else ()
    if verdict == MISS and not row_paths:
        raise fail_with(
            EXIT_UNREADABLE,
            f"a miss needs paths: run {run} of #{pr} is not in {corpus}; pass --paths",
        )
    sha = head_sha if head_sha is not None else (head.head_sha if head is not None else "")
    row = MissRow(
        pr=pr, run=run, head_sha=sha, job=job, verdict=verdict, paths=row_paths, note=note
    )
    try:
        record_row(root / misses, row)
    except DuplicateRowError as exc:
        raise fail_with(EXIT_MISS, str(exc)) from exc
    except MissesError as exc:
        raise fail_with(EXIT_UNREADABLE, str(exc)) from exc
    except OSError as exc:
        raise fail_with(
            EXIT_UNREADABLE, f"{misses}: cannot be written: {exc.strerror or exc}"
        ) from exc
    _echo_report([f"recorded {verdict} #{pr} run {run} {job}"], row.model_dump(mode="json"))


def register(app: typer.Typer) -> None:
    """Add ``harvest``, ``replay`` and ``record`` to ``app``.

    Args:
        app: ``repoach ci affected`` (:data:`~.cli.affected_app`).
    """
    app.command("harvest")(harvest_cmd)
    app.command("replay")(replay_cmd)
    app.command("record")(record_cmd)
