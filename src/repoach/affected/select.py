"""The selection of the jobs a diff affects (SP-AFFECTED-JOBS-TABLE D3, D4).

:func:`select` turns a list of changed paths into the set of ``ci.yml``
jobs they affect: every job when a path is in the ``[full]`` class or
matches no row, else the union of the rows' jobs; then the closure under
the workflow's ``needs`` edges; ``gate`` always. :func:`decide_mode`
says whether the event may be narrowed at all. The workflow is read
through :func:`~repoach.review.merge_wait.load_workflow`, the loader the
required-check names come from, so job ids and check names have one
derivation.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_serializer

from ..review.merge_wait import expand_job_names, load_workflow
from .table import FULL_ROW, UNKNOWN_ROW, AffectedTable, matches, matching_entry

DEFAULT_WORKFLOW_PATH = ".github/workflows/ci.yml"
"""The workflow whose jobs the table selects, relative to the project root."""

GATE_JOB = "gate"
"""The job that computes the selection; it is always selected."""

NARROWED_EVENT = "pull_request"
"""The one event whose selection may be narrowed (D4)."""

BASE_REMOTE = "origin"
"""The remote whose tracking branch of the integration branch is the default base."""

ROWS_HIT_SAMPLE = 3
"""Paths kept per row in :attr:`Selection.rows_hit`."""

Mode = Literal["narrowed", "full"]


class WorkflowError(ValueError):
    """The workflow cannot be read or has no ``jobs`` mapping.

    Attributes:
        path: The workflow file.
        key: Dotted key the defect is about (``jobs.wheel-smoke.needs``),
            empty for a whole-file defect.
        reason: The defect, without the location prefix.
    """

    def __init__(self, path: Path, reason: str, *, key: str = "") -> None:
        self.path = path
        self.key = key
        self.reason = reason
        subject = f" {key}" if key else ""
        super().__init__(f"{path.name}:{subject} {reason}")


class GitError(RuntimeError):
    """A ``git`` command failed.

    Attributes:
        command: The command line, as run.
        detail: The first line of its stderr, or why it could not start.
    """

    def __init__(self, command: Sequence[str], detail: str) -> None:
        self.command = " ".join(command)
        self.detail = detail
        super().__init__(f"{self.command}: {detail}")


class Selection(BaseModel):
    """The jobs one diff affects.

    Attributes:
        mode: ``narrowed`` or ``full`` (:func:`decide_mode`).
        reason: Why the selection is what it is: the mode's reason, or
            the first path that selected every job.
        paths: How many paths were judged.
        jobs: The selected job ids, ``gate`` included.
        rows_hit: Row name to its first :data:`ROWS_HIT_SAMPLE` paths;
            ``full`` and ``unknown`` name the paths that selected every job.
    """

    model_config = ConfigDict(frozen=True)

    mode: Mode
    reason: str
    paths: int
    jobs: frozenset[str]
    rows_hit: dict[str, list[str]]

    @field_serializer("jobs")
    def _sorted_jobs(self, jobs: frozenset[str]) -> list[str]:
        """Serialize the jobs sorted, so the JSON is stable."""
        return sorted(jobs)


def _needs_of(path: Path, job_id: str, job: Mapping[str, Any]) -> frozenset[str]:
    """Return the ``needs`` of one job as a set of job ids.

    Args:
        path: The workflow file, for the error.
        job_id: The job's key.
        job: The job's mapping.

    Returns:
        The ids the job needs; empty without ``needs``.

    Raises:
        WorkflowError: ``needs`` is neither a string nor a list of strings.
    """
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return frozenset({needs})
    if isinstance(needs, list) and all(isinstance(item, str) for item in needs):
        return frozenset(needs)
    raise WorkflowError(path, "must be a job id or a list of job ids", key=f"jobs.{job_id}.needs")


def _jobs(ci_yml: Path) -> Mapping[str, Mapping[str, Any]]:
    """Return the ``jobs`` mapping of the workflow.

    Args:
        ci_yml: The workflow file.

    Returns:
        Job id to its mapping, in file order.

    Raises:
        WorkflowError: The file cannot be read or parsed, has no ``jobs``
            mapping, or a job is not a mapping.
    """
    try:
        data = load_workflow(ci_yml)
    except OSError as exc:
        raise WorkflowError(ci_yml, f"cannot be read: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise WorkflowError(ci_yml, f"is not YAML: {str(exc).splitlines()[0]}") from exc
    jobs = data.get("jobs") if isinstance(data, Mapping) else None
    if not isinstance(jobs, Mapping) or not jobs:
        raise WorkflowError(ci_yml, "has no jobs mapping", key="jobs")
    for job_id, job in jobs.items():
        if not isinstance(job, Mapping):
            raise WorkflowError(ci_yml, "must be a mapping", key=f"jobs.{job_id}")
    return {str(job_id): job for job_id, job in jobs.items()}


def workflow_jobs(ci_yml: Path) -> dict[str, frozenset[str]]:
    """Return every job of the workflow with the jobs it ``needs``.

    Args:
        ci_yml: The workflow file.

    Returns:
        Job id to the ids of its ``needs``, in file order.

    Raises:
        WorkflowError: The workflow is unreadable, has no jobs, or a
            ``needs`` names a job the workflow does not define.
    """
    jobs = _jobs(ci_yml)
    needs = {job_id: _needs_of(ci_yml, job_id, job) for job_id, job in jobs.items()}
    for job_id, required in needs.items():
        for target in sorted(required - set(needs)):
            raise WorkflowError(ci_yml, f"names {target!r}, not a job", key=f"jobs.{job_id}.needs")
    return needs


def check_name_jobs(ci_yml: Path) -> dict[str, str]:
    """Return each check name the workflow produces with the job id producing it.

    Args:
        ci_yml: The workflow file.

    Returns:
        Check name (matrix expanded, as the forge shows it) to job id.

    Raises:
        WorkflowError: The workflow is unreadable or has no jobs.
    """
    return {
        name: job_id
        for job_id, job in _jobs(ci_yml).items()
        for name in expand_job_names(job_id, job)
    }


def default_base(integration_branch: str) -> str:
    """Return the default base revision of a diff.

    Args:
        integration_branch: ``[repoach].integration_branch``.

    Returns:
        ``<BASE_REMOTE>/<integration_branch>`` (``origin/develop``).
    """
    return f"{BASE_REMOTE}/{integration_branch}"


def decide_mode(
    *, event: str, base_ref: str, ref: str, integration_branch: str
) -> tuple[Mode, str]:
    """Decide whether the event's selection may be narrowed (D4).

    Args:
        event: The event name (``pull_request``, ``push``, ``workflow_dispatch``).
        base_ref: The pull request's base branch; empty outside a pull request.
        ref: What the run is about when there is no base branch (the
            ``--base`` revision).
        integration_branch: ``[repoach].integration_branch``.

    Returns:
        ``("narrowed", "narrowed: pull_request on <base>")`` for a pull
        request into the integration branch; ``("full", "full: <event>
        on <base or ref>")`` for anything else.
    """
    target = base_ref or ref
    if event == NARROWED_EVENT and base_ref == integration_branch:
        return "narrowed", f"narrowed: {event} on {target}"
    return "full", f"full: {event} on {target}"


def _closure(jobs: set[str], needs: Mapping[str, frozenset[str]]) -> set[str]:
    """Return ``jobs`` closed under ``needs``.

    Args:
        jobs: The job ids selected so far.
        needs: Job id to the ids it needs.

    Returns:
        ``jobs`` plus every job a selected job needs, transitively.
    """
    closed = set(jobs)
    pending = list(jobs)
    while pending:
        for required in needs.get(pending.pop(), frozenset()):
            if required not in closed:
                closed.add(required)
                pending.append(required)
    return closed


def select(
    paths: Sequence[str],
    table: AffectedTable,
    needs: Mapping[str, frozenset[str]],
    *,
    mode: Mode,
    reason: str,
) -> Selection:
    """Select the jobs ``paths`` affect (D3).

    Args:
        paths: The changed paths.
        table: The table.
        needs: :func:`workflow_jobs` of the workflow.
        mode: The mode of :func:`decide_mode`; ``full`` selects every job.
        reason: The reason of :func:`decide_mode`.

    Returns:
        Every job when ``mode`` is ``full``, a path is in the ``[full]``
        class or a path matches no row (the reason then names the first
        such path); else the union of the matching rows' jobs. Then the
        closure under ``needs``, and :data:`GATE_JOB`.
    """
    rows_hit: dict[str, list[str]] = {}
    selected: set[str] = set()
    everything = mode == "full"
    for path in paths:
        hit = [row for row in table.rows if matches(row, path)]
        full_entry = matching_entry(table.full, path)
        if full_entry is not None or not hit:
            if not everything:
                reason = f"full class: {full_entry}" if full_entry else f"unknown path: {path}"
            everything = True
            _sample(rows_hit, FULL_ROW if full_entry else UNKNOWN_ROW, path)
        for row in hit:
            selected.update(row.jobs)
            _sample(rows_hit, row.name, path)
    if everything:
        selected = set(needs)
    jobs = _closure(selected, needs) | {GATE_JOB}
    return Selection(
        mode=mode, reason=reason, paths=len(paths), jobs=frozenset(jobs), rows_hit=rows_hit
    )


def _sample(rows_hit: dict[str, list[str]], name: str, path: str) -> None:
    """Record ``path`` under ``name`` while the row holds fewer than the sample.

    Args:
        rows_hit: The map being built.
        name: The row name.
        path: The path that hit it.
    """
    kept = rows_hit.setdefault(name, [])
    if len(kept) < ROWS_HIT_SAMPLE:
        kept.append(path)


def _git(repo_root: Path, *args: str) -> str:
    """Run one ``git`` command in ``repo_root`` and return its stdout.

    Args:
        repo_root: The working tree.
        *args: The arguments after ``git``.

    Returns:
        The command's stdout.

    Raises:
        GitError: ``git`` is not installed or exits non-zero; the error
            names the command and the first line of its stderr.
    """
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command, cwd=repo_root, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise GitError(command, "git executable not found") from exc
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        raise GitError(command, lines[0] if lines else f"exit {completed.returncode}")
    return completed.stdout


def changed_paths(repo_root: Path, *, base: str, head: str = "HEAD") -> list[str]:
    """Return the paths ``head`` changes since its merge-base with ``base`` (D4).

    Args:
        repo_root: The working tree.
        base: The base revision (``origin/develop``).
        head: The head revision.

    Returns:
        ``git diff --name-only --no-renames <merge-base> <head>``: both
        names of a rename, deletions included, in ``git``'s order.

    Raises:
        GitError: ``git merge-base`` or ``git diff`` fails.
    """
    merge_base = _git(repo_root, "merge-base", base, head).strip()
    listing = _git(repo_root, "diff", "--name-only", "--no-renames", "-z", merge_base, head)
    return [path for path in listing.split("\0") if path]


def github_output_lines(selection: Selection, job_ids: Sequence[str]) -> list[str]:
    """Return the ``$GITHUB_OUTPUT`` lines of a selection.

    Args:
        selection: The selection.
        job_ids: The workflow's job ids, in the order to write them.

    Returns:
        ``job_<id>=true|false`` per job id, hyphens turned into
        underscores, then ``mode=<mode>``.
    """
    lines = [
        f"job_{job_id.replace('-', '_')}={'true' if job_id in selection.jobs else 'false'}"
        for job_id in job_ids
    ]
    lines.append(f"mode={selection.mode}")
    return lines


def summary_markdown(selection: Selection) -> str:
    """Return the step-summary table of a selection.

    Args:
        selection: The selection.

    Returns:
        A markdown section: the mode and reason, then one table row per
        row hit with its sample paths, then the selected jobs.
    """
    lines = [
        "### Affected jobs",
        "",
        f"Mode `{selection.mode}`: {selection.reason}. Paths judged: {selection.paths}.",
        "",
        "| row | paths (first three) |",
        "|---|---|",
    ]
    lines.extend(
        f"| `{name}` | {', '.join(f'`{path}`' for path in paths)} |"
        for name, paths in selection.rows_hit.items()
    )
    lines.extend(
        ["", f"Jobs selected: {', '.join(f'`{job}`' for job in sorted(selection.jobs))}", ""]
    )
    return "\n".join(lines)
