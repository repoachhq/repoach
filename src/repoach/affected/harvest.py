"""The harvest of the corpus from the forge (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES D2, D5).

:func:`harvest` reads, for the last merged pull requests into the
integration branch, every completed ``pull_request`` run of the CI
workflow, the outcome of each job over every attempt, and the paths of
the head each run judged, read through the forge's ``compare`` of the
pull request's base and that head (F2). :func:`write_corpus` writes it
in the corpus format :func:`~.misses.load_corpus` reads. Every forge
read goes through one ``gh`` callable (:func:`~.gh.run_gh` in use),
so a test answers from a recorded fixture and a failure names the
command.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from ..review.merge_wait import load_workflow
from .gh import GhRunner, HarvestError
from .misses import (
    FAILURE,
    HeadRun,
    JobOutcome,
    MissesError,
    ReplayReport,
    UnknownJobError,
    atomic_write_text,
    load_misses,
    replay,
)
from .select import NARROWED_EVENT, WorkflowError, check_name_jobs, workflow_jobs
from .table import TableError, load_table

HARVEST_WINDOW = 30
"""Merged pull requests a harvest reads by default (constraint ``harvest_window_pull_requests``)."""

RUN_LIST_LIMIT = 200
"""Runs asked per head branch; a listing that reaches it is refused, never cut."""

COMPARE_FILE_CAP = 300
"""Files the forge's ``compare`` lists at most; a head at the cap is ``paths_truncated``."""

COMPARED_STATUSES = frozenset({"ahead", "diverged", "identical"})
"""``compare`` statuses whose file list is the head's diff from its merge base with the base."""

SKIPPED = "skipped"
"""The conclusion of a job the actor allowlist kept from running."""

COMPLETED = "completed"
"""The status of a run that has ended."""

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMPARE_JQ = "{status: .status, files: [(.files // [])[] | {filename, previous_filename}]}"


class Harvest(BaseModel):
    """What a harvest read.

    Attributes:
        repo: ``owner/name`` of the repository.
        runs: One :class:`~.misses.HeadRun` per admitted ``(pull request, run)``.
        not_admitted: Runs whose every job was skipped by the actor
            allowlist: counted, never recorded.
    """

    model_config = ConfigDict(frozen=True)

    repo: str
    runs: tuple[HeadRun, ...]
    not_admitted: int


def _json(gh: GhRunner, *args: str) -> Any:
    """Run ``gh`` and parse its stdout as JSON.

    Args:
        gh: The ``gh`` runner.
        *args: The arguments after ``gh``.

    Returns:
        The parsed value.

    Raises:
        HarvestError: ``gh`` fails, or its stdout is not JSON.
    """
    out = gh(*args)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise HarvestError(["gh", *args], f"answer is not JSON: {exc}") from exc


def resolve_repo(gh: GhRunner) -> str:
    """Return the ``owner/name`` that ``gh`` resolves for the working tree.

    Args:
        gh: The ``gh`` runner.

    Returns:
        The repository, as ``gh repo view`` names it.

    Raises:
        HarvestError: ``gh`` fails, or answers something that is not ``owner/name``.
    """
    args = ("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    repo = gh(*args).strip()
    if not _REPO.fullmatch(repo):
        raise HarvestError(["gh", *args], f"answered {repo!r}, not owner/name")
    return repo


def workflow_name(ci_yml: Path) -> str:
    """Return the ``name`` of the workflow, the one ``gh run list --workflow`` reads.

    Args:
        ci_yml: The workflow file.

    Returns:
        Its top-level ``name``.

    Raises:
        WorkflowError: The file cannot be read or parsed, or has no
            non-empty string ``name``.
    """
    try:
        data = load_workflow(ci_yml)
    except OSError as exc:
        raise WorkflowError(ci_yml, f"cannot be read: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise WorkflowError(ci_yml, f"is not YAML: {str(exc).splitlines()[0]}") from exc
    name = data.get("name") if isinstance(data, Mapping) else None
    if not isinstance(name, str) or not name.strip():
        raise WorkflowError(ci_yml, "must be a non-empty string", key="name")
    return name


def _field(command: Sequence[str], body: Any, key: str, kind: type) -> Any:
    """Return ``body[key]`` when ``body`` is an object holding a value of type ``kind``.

    Args:
        command: The ``gh`` command that answered ``body``, for the error.
        body: A parsed JSON value.
        key: The key to read.
        kind: The type the value must have (a bool is no int).

    Returns:
        The value.

    Raises:
        HarvestError: ``body`` is not an object, or the value is missing
            or of another type.
    """
    value = body.get(key) if isinstance(body, Mapping) else None
    if type(value) is not kind:
        raise HarvestError(command, f"{key} is {value!r}, not {kind.__name__}")
    return value


def _list(command: Sequence[str], body: Any) -> list[Any]:
    """Return ``body`` when it is a JSON list.

    Args:
        command: The ``gh`` command that answered ``body``, for the error.
        body: A parsed JSON value.

    Returns:
        ``body``.

    Raises:
        HarvestError: ``body`` is not a list.
    """
    if not isinstance(body, list):
        raise HarvestError(command, "answer is not a list")
    return body


def _head_paths(gh: GhRunner, repo: str, base_sha: str, head_sha: str) -> tuple[list[str], bool]:
    """Return the paths ``head_sha`` changes from its merge base with ``base_sha`` (F2).

    Args:
        gh: The ``gh`` runner.
        repo: ``owner/name``.
        base_sha: The pull request's base commit.
        head_sha: The head a run judged.

    Returns:
        Every ``filename`` and ``previous_filename``, sorted and unique,
        and whether the listing reached :data:`COMPARE_FILE_CAP`.

    Raises:
        HarvestError: ``gh`` fails, or ``compare`` answers a status
            outside :data:`COMPARED_STATUSES` (a head already in its base
            has no diff to read) or files that are not named.
    """
    args = ("api", f"repos/{repo}/compare/{base_sha}...{head_sha}", "--jq", _COMPARE_JQ)
    command = ["gh", *args]
    body = _json(gh, *args)
    status = _field(command, body, "status", str)
    if status not in COMPARED_STATUSES:
        raise HarvestError(command, f"status {status!r}: the head's diff cannot be read")
    files = _list(command, body.get("files"))
    paths: set[str] = set()
    for entry in files:
        paths.add(_field(command, entry, "filename", str))
        previous = entry.get("previous_filename")
        if previous is not None:
            paths.add(_field(command, entry, "previous_filename", str))
    return sorted(paths), len(files) >= COMPARE_FILE_CAP


def _jobs(gh: GhRunner, repo: str, run_id: int, attempts: int) -> tuple[JobOutcome, ...]:
    """Return the outcome of each job of a run over every attempt.

    A job that failed on any attempt is recorded as failed with that
    attempt's failed steps, even when a re-run passed: a job the table
    would have skipped is still a job it would have skipped. Any other
    job carries the conclusion of the last attempt that ran it.

    Args:
        gh: The ``gh`` runner.
        repo: ``owner/name``.
        run_id: The run.
        attempts: How many attempts the run had.

    Returns:
        One outcome per job name, sorted by name.

    Raises:
        HarvestError: ``gh`` fails or a job is not named or concluded.
    """
    outcomes: dict[str, JobOutcome] = {}
    for attempt in range(1, attempts + 1):
        args = (
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--attempt",
            str(attempt),
            "--json",
            "jobs",
        )
        command = ["gh", *args]
        body = _json(gh, *args)
        for job in _list(command, _field(command, body, "jobs", list)):
            name = _field(command, job, "name", str)
            conclusion = _field(command, job, "conclusion", str)
            failed_steps = tuple(
                _field(command, step, "name", str)
                for step in _list(command, job.get("steps", []))
                if isinstance(step, Mapping) and step.get("conclusion") == FAILURE
            )
            previous = outcomes.get(name)
            if previous is not None and previous.conclusion == FAILURE:
                continue
            outcomes[name] = JobOutcome(name=name, conclusion=conclusion, failed_steps=failed_steps)
    return tuple(outcomes[name] for name in sorted(outcomes))


def harvest(repo: str, *, merged: int, base: str, workflow: str, gh: GhRunner) -> Harvest:
    """Read the corpus of the last ``merged`` pull requests merged into ``base`` (D2).

    ``gh pr list --state merged`` lists the pull requests newest first
    by creation. For each, the runs of ``workflow`` on its head branch
    with the ``pull_request`` event, completed and created between its
    opening and its merge, are read with every attempt's jobs; the head
    of each run is compared with the pull request's base commit.

    Args:
        repo: ``owner/name``.
        merged: How many merged pull requests to read.
        base: The integration branch the pull requests merged into.
        workflow: The workflow's ``name`` (:func:`workflow_name`).
        gh: The ``gh`` runner (:func:`run_gh`).

    Returns:
        The admitted runs and the count of runs not admitted.

    Raises:
        HarvestError: Any forge read fails or answers something the
            harvest cannot use, or a run listing reaches
            :data:`RUN_LIST_LIMIT`. Nothing is returned in part.
    """
    list_args = (
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--base",
        base,
        "--limit",
        str(merged),
        "--json",
        "number,headRefName,createdAt,mergedAt",
    )
    list_command = ["gh", *list_args]
    pulls = _list(list_command, _json(gh, *list_args))
    runs: list[HeadRun] = []
    not_admitted = 0
    paths_cache: dict[tuple[str, str], tuple[list[str], bool]] = {}
    for pull in pulls:
        number = _field(list_command, pull, "number", int)
        branch = _field(list_command, pull, "headRefName", str)
        opened = _field(list_command, pull, "createdAt", str)
        closed = _field(list_command, pull, "mergedAt", str)
        base_args = ("api", f"repos/{repo}/pulls/{number}", "--jq", ".base.sha")
        base_sha = gh(*base_args).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise HarvestError(["gh", *base_args], f"answered {base_sha!r}, not a commit sha")
        run_args = (
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            branch,
            "--event",
            NARROWED_EVENT,
            "--workflow",
            workflow,
            "--limit",
            str(RUN_LIST_LIMIT),
            "--json",
            "databaseId,status,conclusion,headSha,createdAt,attempt",
        )
        run_command = ["gh", *run_args]
        listed = _list(run_command, _json(gh, *run_args))
        if len(listed) >= RUN_LIST_LIMIT:
            raise HarvestError(
                run_command, f"listed {len(listed)} runs, the limit: some may be cut"
            )
        for item in sorted(listed, key=lambda entry: str(entry.get("createdAt", ""))):
            if _field(run_command, item, "status", str) != COMPLETED:
                continue
            created = _field(run_command, item, "createdAt", str)
            if not opened <= created <= closed:
                continue
            run_id = _field(run_command, item, "databaseId", int)
            head_sha = _field(run_command, item, "headSha", str)
            jobs = _jobs(gh, repo, run_id, _field(run_command, item, "attempt", int))
            if jobs and all(job.conclusion == SKIPPED for job in jobs):
                not_admitted += 1
                continue
            key = (base_sha, head_sha)
            if key not in paths_cache:
                paths_cache[key] = _head_paths(gh, repo, base_sha, head_sha)
            paths, truncated = paths_cache[key]
            runs.append(
                HeadRun(
                    pr=number,
                    run=run_id,
                    head_sha=head_sha,
                    paths=tuple(paths),
                    paths_truncated=truncated,
                    conclusion=_field(run_command, item, "conclusion", str),
                    jobs=jobs,
                )
            )
    return Harvest(repo=repo, runs=tuple(runs), not_admitted=not_admitted)


def corpus_source(base: str, workflow: str) -> str:
    """Return the ``source`` line of a harvested corpus: the reads that made it.

    Args:
        base: The integration branch.
        workflow: The workflow's ``name``.

    Returns:
        One sentence naming each ``gh`` read, with no URL.
    """
    return (
        f"repoach ci affected harvest: gh pr list --state merged --base {base}; per pull "
        f"request gh run list --branch <head> --event {NARROWED_EVENT} --workflow {workflow}, "
        "every completed run created between its opening and its merge; gh run view "
        "--attempt <n> --json jobs over every attempt; each head's paths from gh api "
        "repos/<repo>/compare/<pull request base sha>...<head> (filename and previous_filename)"
    )


def corpus_document(
    runs: Sequence[HeadRun], *, repo: str, harvested: date, source: str
) -> dict[str, Any]:
    """Return the corpus as the JSON document :func:`~.misses.load_corpus` reads.

    Args:
        runs: The runs to write.
        repo: ``owner/name``.
        harvested: The day of the harvest.
        source: :func:`corpus_source`.

    Returns:
        ``repo``, ``harvested``, ``source`` and ``pull_requests``: sorted
        by number, each head once in the order of its first run, its
        runs by id.
    """
    pulls: dict[int, dict[str, dict[str, Any]]] = {}
    for run in sorted(runs, key=lambda item: (item.pr, item.run)):
        heads = pulls.setdefault(run.pr, {})
        head = heads.setdefault(
            run.head_sha,
            {
                "sha": run.head_sha,
                "paths": list(run.paths),
                "paths_truncated": run.paths_truncated,
                "runs": [],
            },
        )
        head["runs"].append(
            {
                "run": run.run,
                "conclusion": run.conclusion,
                "jobs": [
                    {
                        "name": job.name,
                        "conclusion": job.conclusion,
                        "failed_steps": list(job.failed_steps),
                    }
                    for job in run.jobs
                ],
            }
        )
    return {
        "repo": repo,
        "harvested": harvested.isoformat(),
        "source": source,
        "pull_requests": [
            {"number": number, "heads": list(heads.values())}
            for number, heads in sorted(pulls.items())
        ],
    }


def write_corpus(
    runs: Sequence[HeadRun], path: Path, *, repo: str, harvested: date, source: str
) -> None:
    """Write the corpus atomically: a temporary file, then a rename.

    Args:
        runs: The runs to write.
        path: The corpus file.
        repo: ``owner/name``.
        harvested: The day of the harvest.
        source: :func:`corpus_source`.

    Raises:
        OSError: The file cannot be written; the previous corpus stays
            byte-identical.
    """
    document = corpus_document(runs, repo=repo, harvested=harvested, source=source)
    atomic_write_text(path, json.dumps(document, indent=1, ensure_ascii=False) + "\n")


class Harvested(BaseModel):
    """A harvest and its replay against the table and the misses table.

    Attributes:
        harvest: What the forge answered.
        report: :func:`~.misses.replay` of its runs.
    """

    model_config = ConfigDict(frozen=True)

    harvest: Harvest
    report: ReplayReport


HARVEST_REFUSALS: tuple[type[Exception], ...] = (
    HarvestError,
    TableError,
    WorkflowError,
    MissesError,
    UnknownJobError,
)
"""What :func:`harvest_and_replay` raises when it cannot say; each names its cause."""


def harvest_and_replay(
    root: Path,
    *,
    repo: str | None,
    merged: int,
    base: str,
    table: str,
    workflow: str,
    misses: str,
    gh: GhRunner,
) -> Harvested:
    """Read the inputs, harvest the forge and replay the harvest (D2, D5).

    The table, the workflow and the misses table are read first, so an
    unreadable input refuses before any forge read.

    Args:
        root: The project root.
        repo: ``owner/name``; None resolves it with :func:`resolve_repo`.
        merged: How many merged pull requests to read.
        base: The integration branch.
        table: The path-to-job table, relative to ``root`` unless absolute.
        workflow: The workflow, relative to ``root`` unless absolute.
        misses: The misses table, relative to ``root`` unless absolute.
        gh: The ``gh`` runner.

    Returns:
        The harvest and its replay.

    Raises:
        TableError: The table cannot be read.
        WorkflowError: The workflow cannot be read or has no ``name``.
        MissesError: The misses table cannot be read.
        HarvestError: A forge read fails.
        UnknownJobError: A failed job or a row names no check of the workflow.
    """
    ci_yml = root / workflow
    needs = workflow_jobs(ci_yml)
    loaded = load_table(root / table, needs, workflow=ci_yml.name)
    names = check_name_jobs(ci_yml)
    name = workflow_name(ci_yml)
    rows = load_misses(root / misses)
    resolved = repo if repo is not None else resolve_repo(gh)
    harvested = harvest(resolved, merged=merged, base=base, workflow=name, gh=gh)
    report = replay(harvested.runs, rows, loaded, needs, names)
    return Harvested(harvest=harvested, report=report)
