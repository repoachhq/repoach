"""The corpus, the misses table and their replay (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES D1, D3).

The corpus (``tests/fixtures/affected_jobs/pr_runs.json``) holds, per
merged pull request, every head that ran ``CI`` with the paths of that
head and the outcome of each job. The misses table
(``tests/fixtures/affected_jobs/misses.tsv``, ``format:affected-jobs-misses``)
holds one row per ``(run, job)`` a red outside the corpus was charged
to, with its verdict. :func:`replay` judges both against the path-to-job
table: a failed job the table would not have selected is a miss, unless
a ``conflict`` or ``external`` row says the table was not at fault.

This module reads files only; the forge is read by :mod:`.harvest`, so
the replay imports no ``gh`` caller.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict

from .select import select
from .table import AffectedTable

DEFAULT_CORPUS_PATH = "tests/fixtures/affected_jobs/pr_runs.json"
"""The corpus, relative to the project root."""

DEFAULT_MISSES_PATH = "tests/fixtures/affected_jobs/misses.tsv"
"""The misses table, relative to the project root."""

FAILURE = "failure"
"""The plain failed conclusion of a job or a step."""

FAILED_CONCLUSIONS = frozenset({FAILURE, "timed_out", "startup_failure"})
"""Job conclusions that are a judgement against the head: the job ran, or tried to, and went red (D1)."""

NOT_JUDGED_CONCLUSIONS = frozenset(
    {"success", "cancelled", "skipped", "neutral", "action_required", "stale"}
)
"""Job conclusions that are no failure. ``cancelled`` is a cancelled run and ``skipped`` a job
that never ran (D1); ``action_required`` waits for an approval and ran nothing; ``stale`` is
a check run the forge superseded; ``neutral`` is green by definition."""

JOB_CONCLUSIONS = FAILED_CONCLUSIONS | NOT_JUDGED_CONCLUSIONS
"""Every job conclusion the corpus admits; any other is refused by name."""

Verdict = Literal["miss", "conflict", "external"]
VERDICTS: tuple[str, ...] = get_args(Verdict)
"""The verdicts a row may carry; only ``miss`` is judged by the replay."""

MISS: Verdict = "miss"
"""The verdict the replay judges."""

TRUNCATED = "truncated"
"""The label of a head whose paths were cut by the forge, replayed as the full class."""

MISSES_COLUMNS: tuple[str, ...] = ("pr", "run", "head_sha", "job", "verdict", "paths", "note")
"""The header of the misses table, in order."""

PATHS_SEPARATOR = ";"
"""Joins the ``paths`` column of the misses table."""

PATHS_SAMPLE = 3
"""Paths printed per replay line."""

_CORPUS_TOP_KEYS = frozenset({"repo", "harvested", "source", "pull_requests"})
_CORPUS_PR_KEYS = frozenset({"number", "heads"})
_CORPUS_HEAD_KEYS = frozenset({"sha", "paths", "paths_truncated", "runs"})
_CORPUS_RUN_KEYS = frozenset({"run", "conclusion", "jobs"})
_CORPUS_JOB_KEYS = frozenset({"name", "conclusion", "failed_steps"})


class CorpusError(ValueError):
    """The corpus cannot be read or breaks its format.

    Attributes:
        path: The corpus file.
        reason: The defect, naming the pull request, head or run it is about.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path.name}: {reason}")


class MissesError(ValueError):
    """The misses table cannot be read, breaks its format, or refuses a row.

    Attributes:
        path: The misses file.
        line: The 1-based line the defect is on, None for a whole-file defect.
        reason: The defect, without the location prefix.
    """

    def __init__(self, path: Path, reason: str, *, line: int | None = None) -> None:
        self.path = path
        self.line = line
        self.reason = reason
        where = f":{line}" if line is not None else ""
        super().__init__(f"{path.name}{where}: {reason}")


class DuplicateRowError(MissesError):
    """A row with the same ``(run, job)`` key is already in the table (D3)."""


class UnknownJobError(ValueError):
    """A job name is not a check the workflow produces.

    Attributes:
        job: The name that was not found.
        where: What named it (``misses.tsv:3`` or ``#275 run 1``).
    """

    def __init__(self, job: str, where: str) -> None:
        self.job = job
        self.where = where
        super().__init__(f"{where}: {job!r} is no check of the workflow")


class UnknownConclusionError(ValueError):
    """A job conclusion is neither in :data:`FAILED_CONCLUSIONS` nor in :data:`NOT_JUDGED_CONCLUSIONS`.

    Attributes:
        conclusion: The conclusion that was not recognised.
    """

    def __init__(self, conclusion: object) -> None:
        self.conclusion = conclusion
        super().__init__(
            f"job conclusion {conclusion!r} is not one of {', '.join(sorted(JOB_CONCLUSIONS))}"
        )


def job_failed(conclusion: str) -> bool:
    """Return whether a job conclusion is a failure (D1).

    Args:
        conclusion: The job's conclusion as the forge names it.

    Returns:
        True for :data:`FAILED_CONCLUSIONS`, False for :data:`NOT_JUDGED_CONCLUSIONS`.

    Raises:
        UnknownConclusionError: ``conclusion`` is in neither set.
    """
    if conclusion in FAILED_CONCLUSIONS:
        return True
    if conclusion in NOT_JUDGED_CONCLUSIONS:
        return False
    raise UnknownConclusionError(conclusion)


class JobOutcome(BaseModel):
    """How one job of one run ended.

    Attributes:
        name: The check name, as the forge shows it (``Test suite (Python 3.11)``).
        conclusion: One of :data:`JOB_CONCLUSIONS`.
        failed_steps: The names of the steps that concluded ``failure``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    conclusion: str
    failed_steps: tuple[str, ...] = ()


class HeadRun(BaseModel):
    """One completed ``CI`` run of one pull request, with the paths of its head.

    Attributes:
        pr: The pull request number.
        run: The run id.
        head_sha: The commit the run judged.
        paths: The paths the head changes against its base (F2).
        paths_truncated: The forge cut the listing at its cap; the
            replay then selects every job.
        conclusion: The run's conclusion.
        jobs: The outcome of each job.
    """

    model_config = ConfigDict(frozen=True)

    pr: int
    run: int
    head_sha: str
    paths: tuple[str, ...]
    paths_truncated: bool = False
    conclusion: str
    jobs: tuple[JobOutcome, ...]


class MissRow(BaseModel):
    """One row of the misses table (D3).

    Attributes:
        pr: The pull request the red is charged to.
        run: The run that went red.
        head_sha: The commit it judged; empty when the recorder did not know it.
        job: The check name that failed.
        verdict: ``miss``, ``conflict`` or ``external``.
        paths: The paths the replay selects on.
        note: Why the verdict, in one line.
    """

    model_config = ConfigDict(frozen=True)

    pr: int
    run: int
    head_sha: str
    job: str
    verdict: Verdict
    paths: tuple[str, ...]
    note: str

    @property
    def key(self) -> tuple[int, str]:
        """Return the row's unique key, ``(run, job)``."""
        return self.run, self.job


class ReplayLine(BaseModel):
    """One printed line of a replay.

    Attributes:
        label: ``miss``, ``conflict``, ``external`` or ``truncated``.
        pr: The pull request.
        run: The run.
        head_sha: The head, possibly empty for a row.
        job: The check name; empty on a ``truncated`` line.
        paths: The first :data:`PATHS_SAMPLE` paths.
        source: ``corpus`` or ``misses``: which file the line comes from.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    pr: int
    run: int
    head_sha: str
    job: str
    paths: tuple[str, ...]
    source: Literal["corpus", "misses"]


class ReplayReport(BaseModel):
    """What a replay found.

    Attributes:
        failed_jobs: The failed jobs of the corpus.
        misses: The failed jobs the table does not select: corpus
            failures no row covers, and ``miss`` rows still unselected.
        new_misses: The corpus failures among :attr:`misses` that no row covers.
        recorded: The ``conflict`` and ``external`` rows, never a failure.
        truncated: The corpus heads replayed as the full class.
    """

    model_config = ConfigDict(frozen=True)

    failed_jobs: int
    misses: tuple[ReplayLine, ...]
    new_misses: tuple[ReplayLine, ...]
    recorded: tuple[ReplayLine, ...]
    truncated: tuple[ReplayLine, ...]


def _require(path: Path, mapping: Any, keys: frozenset[str], where: str) -> Mapping[str, Any]:
    """Return ``mapping`` when it is a mapping with exactly ``keys``.

    Args:
        path: The corpus file, for the error.
        mapping: The parsed JSON value.
        keys: The keys the format defines at this level.
        where: What the value is (``#275 head 7a7a048``), for the error.

    Returns:
        ``mapping``.

    Raises:
        CorpusError: ``mapping`` is not an object, or its keys differ from ``keys``.
    """
    if not isinstance(mapping, Mapping):
        raise CorpusError(path, f"{where} is not an object")
    if set(mapping) != keys:
        raise CorpusError(
            path, f"{where} has keys {sorted(mapping)}, the format defines {sorted(keys)}"
        )
    return mapping


def _str_list(path: Path, value: Any, where: str) -> tuple[str, ...]:
    """Return ``value`` as a tuple of strings.

    Args:
        path: The corpus file, for the error.
        value: The parsed JSON value.
        where: What the value is, for the error.

    Returns:
        The strings, in order.

    Raises:
        CorpusError: ``value`` is not a list of strings.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorpusError(path, f"{where} is not a list of strings")
    return tuple(value)


def _typed(path: Path, value: Any, kind: type, where: str) -> Any:
    """Return ``value`` when it is exactly of type ``kind``.

    Args:
        path: The corpus file, for the error.
        value: The parsed JSON value.
        kind: ``int``, ``str`` or ``bool``.
        where: What the value is, for the error.

    Returns:
        ``value``.

    Raises:
        CorpusError: ``value`` is of another type (a bool is no int).
    """
    if type(value) is not kind:
        raise CorpusError(path, f"{where} is {value!r}, not {kind.__name__}")
    return value


def load_corpus(path: Path) -> list[HeadRun]:
    """Read the corpus as one :class:`HeadRun` per ``(pull request, run)``.

    Args:
        path: The corpus file (:data:`DEFAULT_CORPUS_PATH` under the root).

    Returns:
        The runs, in file order.

    Raises:
        CorpusError: The file cannot be read, is not JSON, or a level
            carries a key the format does not define, lacks one, or holds
            a value of the wrong type, or a job conclusion outside
            :data:`JOB_CONCLUSIONS`. The message names where.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CorpusError(path, f"cannot be read: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusError(path, f"is not JSON: {exc}") from exc
    top = _require(path, data, _CORPUS_TOP_KEYS, "the corpus")
    if not isinstance(top["pull_requests"], list):
        raise CorpusError(path, "pull_requests is not a list")
    runs: list[HeadRun] = []
    for pr_body in top["pull_requests"]:
        pr = _require(path, pr_body, _CORPUS_PR_KEYS, "a pull request")
        number = _typed(path, pr["number"], int, "a pull request number")
        if not isinstance(pr["heads"], list):
            raise CorpusError(path, f"#{number} heads is not a list")
        for head_body in pr["heads"]:
            head = _require(path, head_body, _CORPUS_HEAD_KEYS, f"#{number} a head")
            sha = _typed(path, head["sha"], str, f"#{number} a head sha")
            where = f"#{number} head {sha[:7]}"
            paths = _str_list(path, head["paths"], f"{where} paths")
            truncated = _typed(path, head["paths_truncated"], bool, f"{where} paths_truncated")
            if not isinstance(head["runs"], list):
                raise CorpusError(path, f"{where} runs is not a list")
            for run_body in head["runs"]:
                run = _require(path, run_body, _CORPUS_RUN_KEYS, f"{where} a run")
                run_id = _typed(path, run["run"], int, f"{where} a run id")
                if not isinstance(run["jobs"], list):
                    raise CorpusError(path, f"{where} run {run_id} jobs is not a list")
                jobs = []
                for job_body in run["jobs"]:
                    job = _require(path, job_body, _CORPUS_JOB_KEYS, f"{where} run {run_id} a job")
                    try:
                        job_failed(job["conclusion"])
                    except UnknownConclusionError as exc:
                        raise CorpusError(path, f"{where} run {run_id}: {exc}") from exc
                    jobs.append(
                        JobOutcome(
                            name=_typed(path, job["name"], str, f"run {run_id} a job name"),
                            conclusion=_typed(
                                path, job["conclusion"], str, f"run {run_id} a conclusion"
                            ),
                            failed_steps=_str_list(
                                path, job["failed_steps"], f"run {run_id} failed_steps"
                            ),
                        )
                    )
                runs.append(
                    HeadRun(
                        pr=number,
                        run=run_id,
                        head_sha=sha,
                        paths=paths,
                        paths_truncated=truncated,
                        conclusion=_typed(path, run["conclusion"], str, f"run {run_id} conclusion"),
                        jobs=tuple(jobs),
                    )
                )
    return runs


def _int_cell(path: Path, line: int, column: str, value: str) -> int:
    """Return a non-negative integer cell of the misses table.

    Args:
        path: The misses file, for the error.
        line: The line, for the error.
        column: The column name, for the error.
        value: The cell.

    Returns:
        The integer.

    Raises:
        MissesError: The cell is not a run of decimal digits.
    """
    if not value.isdigit() or not value.isascii():
        raise MissesError(path, f"{column} is {value!r}, not a number", line=line)
    return int(value)


def _sort_key(row: MissRow) -> tuple[int, int, str]:
    """Return the order of the misses table: ``pr``, ``run``, ``job`` (D3)."""
    return row.pr, row.run, row.job


def load_misses(path: Path) -> list[MissRow]:
    """Read the misses table.

    Args:
        path: The misses file (:data:`DEFAULT_MISSES_PATH` under the root).

    Returns:
        The rows, in file order.

    Raises:
        MissesError: The file cannot be read, its header is not
            :data:`MISSES_COLUMNS`, a line has another number of cells, a
            number cell is not a number, a verdict is not in
            :data:`VERDICTS`, a ``miss`` row has no paths, a key repeats
            (:class:`DuplicateRowError`) or the rows are not sorted by
            ``pr``, ``run``, ``job``. The message names the line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissesError(path, f"cannot be read: {exc.strerror or exc}") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or tuple(lines[0].split("\t")) != MISSES_COLUMNS:
        raise MissesError(path, f"header must be {chr(9).join(MISSES_COLUMNS)!r}", line=1)
    rows: list[MissRow] = []
    seen: set[tuple[int, str]] = set()
    for number, line in enumerate(lines[1:], start=2):
        cells = line.split("\t")
        if len(cells) != len(MISSES_COLUMNS):
            raise MissesError(
                path, f"has {len(cells)} cells, the header {len(MISSES_COLUMNS)}", line=number
            )
        values = dict(zip(MISSES_COLUMNS, cells, strict=True))
        if values["verdict"] not in VERDICTS:
            raise MissesError(
                path,
                f"verdict {values['verdict']!r} is not one of {', '.join(VERDICTS)}",
                line=number,
            )
        paths = tuple(p for p in values["paths"].split(PATHS_SEPARATOR) if p)
        if values["verdict"] == MISS and not paths:
            raise MissesError(path, "a miss row has no paths to replay", line=number)
        row = MissRow(
            pr=_int_cell(path, number, "pr", values["pr"]),
            run=_int_cell(path, number, "run", values["run"]),
            head_sha=values["head_sha"],
            job=values["job"],
            verdict=values["verdict"],
            paths=paths,
            note=values["note"],
        )
        if row.key in seen:
            raise DuplicateRowError(
                path, f"run {row.run} job {row.job!r} is already a row", line=number
            )
        if rows and _sort_key(row) < _sort_key(rows[-1]):
            raise MissesError(path, "rows are not sorted by pr, run, job", line=number)
        seen.add(row.key)
        rows.append(row)
    return rows


def _cell(path: Path, column: str, value: str) -> str:
    """Return ``value`` when it fits in one cell of the misses table.

    Args:
        path: The misses file, for the error.
        column: The column name, for the error.
        value: The cell's text.

    Returns:
        ``value``.

    Raises:
        MissesError: ``value`` holds a tab or a line break.
    """
    if any(character in value for character in "\t\r\n"):
        raise MissesError(path, f"{column} {value!r} holds a tab or a line break")
    return value


def _row_line(path: Path, row: MissRow) -> str:
    """Return ``row`` as one line of the misses table.

    Args:
        path: The misses file, for the error.
        row: The row.

    Returns:
        The tab-separated line, without its line break.

    Raises:
        MissesError: A cell holds a tab or a line break, or a path holds
            :data:`PATHS_SEPARATOR`.
    """
    for item in row.paths:
        if PATHS_SEPARATOR in item:
            raise MissesError(path, f"path {item!r} holds {PATHS_SEPARATOR!r}")
    cells = [
        str(row.pr),
        str(row.run),
        _cell(path, "head_sha", row.head_sha),
        _cell(path, "job", row.job),
        row.verdict,
        _cell(path, "paths", PATHS_SEPARATOR.join(row.paths)),
        _cell(path, "note", row.note),
    ]
    return "\t".join(cells)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file and a rename.

    A reader sees the old file or the new one, never a truncated one.

    Args:
        path: The file to replace.
        text: Its new content.

    Raises:
        OSError: The temporary file cannot be written or renamed; it is
            removed and ``path`` is left as it was.
    """
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def record_row(path: Path, row: MissRow) -> list[MissRow]:
    """Add ``row`` to the misses table, keeping every other row and the order (D3).

    Args:
        path: The misses file.
        row: The row to add.

    Returns:
        The rows after the addition, sorted.

    Raises:
        DuplicateRowError: A row with ``row.key`` is already there; the
            file is untouched.
        MissesError: The table cannot be read, or ``row`` cannot be
            written as one line; the file is untouched.
        OSError: The file cannot be replaced; it is left as it was.
    """
    rows = load_misses(path)
    if any(existing.key == row.key for existing in rows):
        raise DuplicateRowError(path, f"run {row.run} job {row.job!r} is already a row")
    rows = sorted([*rows, row], key=_sort_key)
    lines = ["\t".join(MISSES_COLUMNS), *(_row_line(path, item) for item in rows)]
    atomic_write_text(path, "\n".join(lines) + "\n")
    return rows


def _job_id(check_names: Mapping[str, str], job: str, where: str) -> str:
    """Return the workflow job id of a check name.

    Args:
        check_names: Check name to job id (:func:`~.select.check_name_jobs`).
        job: The check name.
        where: What named it, for the error.

    Returns:
        The job id.

    Raises:
        UnknownJobError: ``job`` is not a check of the workflow.
    """
    if job not in check_names:
        raise UnknownJobError(job, where)
    return check_names[job]


def replay(
    corpus: Sequence[HeadRun],
    misses: Sequence[MissRow],
    table: AffectedTable,
    needs: Mapping[str, frozenset[str]],
    check_names: Mapping[str, str],
) -> ReplayReport:
    """Judge every failed job of the corpus and every ``miss`` row against the table (D1, D3).

    A failed job (a conclusion of :data:`FAILED_CONCLUSIONS`) is a miss when the
    narrowed selection of its head's paths does not hold its job id,
    unless a ``conflict`` or ``external`` row has its ``(run, job)``;
    a ``miss`` row is a miss while the selection of its paths does not
    hold its job. A head with ``paths_truncated`` is replayed as the
    full class, so it is never a miss.

    Args:
        corpus: :func:`load_corpus`.
        misses: :func:`load_misses`.
        table: The path-to-job table.
        needs: :func:`~.select.workflow_jobs` of the workflow.
        check_names: :func:`~.select.check_name_jobs` of the workflow.

    Returns:
        The report; :attr:`ReplayReport.misses` is empty when the table
        selects every failure it is charged with.

    Raises:
        UnknownJobError: A failed job of the corpus or a row of any
            verdict names a check the workflow does not produce.
        UnknownConclusionError: A job of the corpus has a conclusion
            outside :data:`JOB_CONCLUSIONS`.
    """
    rows_by_key = {row.key: row for row in misses}
    for row in misses:
        _job_id(check_names, row.job, f"misses #{row.pr} run {row.run}")
    found: list[ReplayLine] = []
    new: list[ReplayLine] = []
    truncated: list[ReplayLine] = []
    failed = 0
    for head in corpus:
        mode = "full" if head.paths_truncated else "narrowed"
        selection = select(head.paths, table, needs, mode=mode, reason="replay")
        sample = head.paths[:PATHS_SAMPLE]
        if head.paths_truncated:
            truncated.append(
                ReplayLine(
                    label=TRUNCATED,
                    pr=head.pr,
                    run=head.run,
                    head_sha=head.head_sha,
                    job="",
                    paths=sample,
                    source="corpus",
                )
            )
        for job in head.jobs:
            if not job_failed(job.conclusion):
                continue
            failed += 1
            job_id = _job_id(check_names, job.name, f"corpus #{head.pr} run {head.run}")
            if job_id in selection.jobs or (head.run, job.name) in rows_by_key:
                continue
            line = ReplayLine(
                label=MISS,
                pr=head.pr,
                run=head.run,
                head_sha=head.head_sha,
                job=job.name,
                paths=sample,
                source="corpus",
            )
            found.append(line)
            new.append(line)
    recorded: list[ReplayLine] = []
    for row in misses:
        line = ReplayLine(
            label=row.verdict,
            pr=row.pr,
            run=row.run,
            head_sha=row.head_sha,
            job=row.job,
            paths=row.paths[:PATHS_SAMPLE],
            source="misses",
        )
        if row.verdict != MISS:
            recorded.append(line)
            continue
        selection = select(row.paths, table, needs, mode="narrowed", reason="replay")
        if check_names[row.job] not in selection.jobs:
            found.append(line)
    return ReplayReport(
        failed_jobs=failed,
        misses=tuple(found),
        new_misses=tuple(new),
        recorded=tuple(recorded),
        truncated=tuple(truncated),
    )


def describe(line: ReplayLine) -> str:
    """Return the stderr line of one replay line.

    Args:
        line: The line.

    Returns:
        ``MISS #<pr> run <id> <job> paths <first three>…`` for a miss,
        ``CONFLICT``/``EXTERNAL`` the same way, ``TRUNCATED #<pr> run
        <id>: paths cut by the forge, replayed as the full class``.
    """
    if line.label == TRUNCATED:
        return (
            f"TRUNCATED #{line.pr} run {line.run}: paths cut by the forge, "
            "replayed as the full class"
        )
    return (
        f"{line.label.upper()} #{line.pr} run {line.run} {line.job} paths {', '.join(line.paths)}…"
    )


def report_lines(report: ReplayReport, *, count: Sequence[ReplayLine]) -> list[str]:
    """Return the stderr account of a replay, ending in ``misses: <n>/<failed jobs>``.

    Args:
        report: The replay.
        count: The lines the closing count counts and prints as misses
            (:attr:`ReplayReport.misses` for ``replay``,
            :attr:`ReplayReport.new_misses` for ``harvest``).

    Returns:
        One line per miss counted, per recorded row, per truncated head,
        then the count.
    """
    lines = [describe(line) for line in count]
    lines.extend(describe(line) for line in report.recorded)
    lines.extend(describe(line) for line in report.truncated)
    lines.append(f"misses: {len(count)}/{report.failed_jobs}")
    return lines
