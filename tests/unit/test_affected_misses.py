"""The harvest, the misses table and the replay (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES T1-T7)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from repoach.affected.cli import EXIT_UNREADABLE
from repoach.affected.gh import HarvestError, run_gh
from repoach.affected.harvest import (
    COMPARE_FILE_CAP,
    HARVEST_WINDOW,
    RUN_LIST_LIMIT,
    corpus_document,
    corpus_source,
    harvest,
    resolve_repo,
    workflow_name,
    write_corpus,
)
from repoach.affected.misses import (
    DEFAULT_CORPUS_PATH,
    DEFAULT_MISSES_PATH,
    FAILED_CONCLUSIONS,
    MISSES_COLUMNS,
    NOT_JUDGED_CONCLUSIONS,
    VERDICTS,
    CorpusError,
    DuplicateRowError,
    HeadRun,
    JobOutcome,
    MissesError,
    MissRow,
    UnknownConclusionError,
    UnknownJobError,
    atomic_write_text,
    describe,
    job_failed,
    load_corpus,
    load_misses,
    record_row,
    replay,
    report_lines,
)
from repoach.affected.misses_cli import EXIT_MISS, register
from repoach.affected.select import (
    DEFAULT_WORKFLOW_PATH,
    WorkflowError,
    check_name_jobs,
    workflow_jobs,
)
from repoach.affected.table import DEFAULT_TABLE_PATH, AffectedTable, load_table
from repoach.cli import release_cmds

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / DEFAULT_WORKFLOW_PATH
_ANSWERS = _ROOT / "tests" / "fixtures" / "affected_jobs" / "harvest" / "gh_answers.json"
_HEADER = "\t".join(MISSES_COLUMNS) + "\n"
_TEST_311 = "Test suite (Python 3.11)"
_APP = typer.Typer()
register(_APP)


def _committed() -> tuple[AffectedTable, dict[str, frozenset[str]], dict[str, str]]:
    needs = workflow_jobs(_WORKFLOW)
    table = load_table(_ROOT / DEFAULT_TABLE_PATH, needs, workflow=_WORKFLOW.name)
    return table, needs, check_name_jobs(_WORKFLOW)


class RecordedGh:
    """A ``gh`` runner answering from :data:`_ANSWERS`; an unrecorded call is refused."""

    def __init__(self, path: Path = _ANSWERS, *, fail_on: str | None = None) -> None:
        self.answers = {
            tuple(entry["args"]): entry
            for entry in json.loads(path.read_text(encoding="utf-8"))["answers"]
        }
        self.calls: list[tuple[str, ...]] = []
        self.fail_on = fail_on

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if self.fail_on is not None and self.fail_on in args:
            raise HarvestError(["gh", *args], "HTTP 403: API rate limit exceeded")
        if args not in self.answers:
            raise HarvestError(["gh", *args], "no recorded answer")
        entry = self.answers[args]
        return entry["text"] if "text" in entry else json.dumps(entry["json"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository holding copies of the table, the workflow, the corpus and the misses table."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    for relative in (
        DEFAULT_TABLE_PATH,
        DEFAULT_WORKFLOW_PATH,
        DEFAULT_CORPUS_PATH,
        DEFAULT_MISSES_PATH,
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((_ROOT / relative).read_bytes())
    monkeypatch.chdir(root)
    return root


def _head(**overrides: object) -> HeadRun:
    values: dict[str, object] = {
        "pr": 1,
        "run": 10,
        "head_sha": "a" * 40,
        "paths": ("src/repoach/a.py",),
        "conclusion": "failure",
        "jobs": (JobOutcome(name=_TEST_311, conclusion="failure", failed_steps=("Pytest",)),),
    }
    values.update(overrides)
    return HeadRun.model_validate(values)


def _row(**overrides: object) -> MissRow:
    values: dict[str, object] = {
        "pr": 2,
        "run": 20,
        "head_sha": "",
        "job": _TEST_311,
        "verdict": "miss",
        "paths": ("docs/x.md",),
        "note": "probe",
    }
    values.update(overrides)
    return MissRow.model_validate(values)


def _python_row_selects_nothing(table: AffectedTable) -> AffectedTable:
    return table.model_copy(
        update={
            "rows": tuple(
                row.model_copy(update={"jobs": frozenset()}) if row.name == "python" else row
                for row in table.rows
            )
        }
    )


def test_t1_replay_of_the_committed_corpus_and_table_finds_no_miss(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(_ROOT)
    result = CliRunner().invoke(_APP, ["replay"])
    assert result.exit_code == 0, result.stderr
    failed = json.loads(result.stdout)["failed_jobs"]
    assert failed > 0
    assert result.stderr.splitlines()[-1] == f"misses: 0/{failed}"
    with capsys.disabled():
        print(f"\n{result.stderr.splitlines()[-1]}")


def test_t1_replay_goes_red_when_the_python_row_selects_nothing() -> None:
    table, needs, names = _committed()
    corpus = load_corpus(_ROOT / DEFAULT_CORPUS_PATH)
    report = replay(corpus, [], _python_row_selects_nothing(table), needs, names)
    assert report.misses
    assert {names[line.job] for line in report.misses} == {"test"}
    assert report.new_misses == report.misses


def test_t2_harvest_writes_the_admitted_heads_and_names_the_miss(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = RecordedGh()
    monkeypatch.setattr("repoach.affected.gh.run_gh", gh)
    result = CliRunner().invoke(_APP, ["harvest", "--merged", "2", "--repo", "repoachhq/repoach"])
    assert result.exit_code == EXIT_MISS, result.stderr
    miss_lines = [line for line in result.stderr.splitlines() if line.startswith("MISS ")]
    assert miss_lines == [f"MISS #11 run 101 {_TEST_311} paths docs/a.md…"]
    assert result.stderr.splitlines()[-2:] == ["not admitted: 1", "misses: 1/1"]
    payload = json.loads(result.stdout)
    assert (payload["runs"], payload["not_admitted"], payload["failed_jobs"]) == (2, 1, 1)
    runs = load_corpus(repo / DEFAULT_CORPUS_PATH)
    assert [(run.pr, run.run, run.head_sha[:4]) for run in runs] == [
        (11, 101, "aaaa"),
        (11, 102, "bbbb"),
    ]
    assert runs[0].jobs[0] == JobOutcome(
        name=_TEST_311, conclusion="failure", failed_steps=("Pytest",)
    )
    assert runs[1].paths == ("docs/a.md", "src/repoach/new.py", "src/repoach/old.py")
    assert not any("cccc" in call[-1] for call in gh.calls if call[0] == "api")


def test_t2_a_cancelled_job_is_never_a_failure() -> None:
    table, needs, names = _committed()
    cancelled = _head(
        paths=("docs/a.md",),
        jobs=(JobOutcome(name=_TEST_311, conclusion="cancelled"),),
    )
    report = replay([cancelled], [], table, needs, names)
    assert (report.failed_jobs, report.misses) == (0, ())


def test_t3_a_failing_forge_leaves_the_corpus_byte_identical(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (repo / DEFAULT_CORPUS_PATH).read_bytes()
    monkeypatch.setattr("repoach.affected.gh.run_gh", RecordedGh(fail_on="102"))
    result = CliRunner().invoke(_APP, ["harvest", "--merged", "2", "--repo", "repoachhq/repoach"])
    assert result.exit_code == EXIT_UNREADABLE
    assert "HTTP 403: API rate limit exceeded" in result.stderr
    assert result.stdout == ""
    assert (repo / DEFAULT_CORPUS_PATH).read_bytes() == before


def test_t3_write_corpus_keeps_the_previous_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = tmp_path / "pr_runs.json"
    corpus.write_text("previous\n", encoding="utf-8")

    def _refuse(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("repoach.affected.misses.os.replace", _refuse)
    with pytest.raises(OSError, match="No space left"):
        write_corpus([_head()], corpus, repo="o/r", harvested=date(2026, 11, 17), source="s")
    assert corpus.read_text(encoding="utf-8") == "previous\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["pr_runs.json"]


def test_t4_the_misses_table_is_well_formed() -> None:
    path = _ROOT / DEFAULT_MISSES_PATH
    assert path.read_text(encoding="utf-8").splitlines()[0] == _HEADER.rstrip("\n")
    rows = load_misses(path)
    names = check_name_jobs(_WORKFLOW)
    unknown = [row.job for row in rows if row.job not in names]
    assert unknown == [], f"misses.tsv names jobs ci.yml does not define: {unknown}"
    assert all(row.verdict in VERDICTS for row in rows)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("pr\trun\n", "header must be"),
        (
            f"{_HEADER}2\t20\t\t{_TEST_311}\tmiss\tdocs/x.md\tn\n1\t10\t\t{_TEST_311}\tmiss\tdocs/x.md\tn\n",
            "not sorted",
        ),
        (f"{_HEADER}1\t10\t\t{_TEST_311}\tguess\tdocs/x.md\tn\n", "verdict 'guess' is not one of"),
        (
            f"{_HEADER}1\t10\t\t{_TEST_311}\tmiss\tdocs/x.md\tn\n1\t10\t\t{_TEST_311}\tconflict\t\tn\n",
            "is already a row",
        ),
        (f"{_HEADER}1\t10\t\t{_TEST_311}\tmiss\tdocs/x.md\n", "has 6 cells"),
        (f"{_HEADER}x\t10\t\t{_TEST_311}\tmiss\tdocs/x.md\tn\n", "pr is 'x', not a number"),
        (f"{_HEADER}1\t10\t\t{_TEST_311}\tmiss\t\tn\n", "a miss row has no paths"),
    ],
)
def test_t4_load_misses_refuses_a_malformed_table(tmp_path: Path, body: str, reason: str) -> None:
    path = tmp_path / "misses.tsv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(MissesError, match=reason) as caught:
        load_misses(path)
    assert caught.value.line is not None


def test_t4_load_misses_refuses_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(MissesError, match="cannot be read"):
        load_misses(tmp_path / "absent.tsv")


def test_t5_conflict_and_external_rows_never_fail_a_miss_row_does() -> None:
    table, needs, names = _committed()
    rows = [
        _row(run=20, verdict="conflict", paths=()),
        _row(run=21, verdict="external", paths=("docs/x.md",)),
    ]
    report = replay([], rows, table, needs, names)
    assert report.misses == ()
    assert [line.label for line in report.recorded] == ["conflict", "external"]
    assert [
        line for line in report_lines(report, count=report.misses) if line.startswith("EXTERNAL")
    ]
    red = replay([], [*rows, _row(run=22)], table, needs, names)
    assert [(line.label, line.run) for line in red.misses] == [("miss", 22)]
    assert red.new_misses == ()


def test_t5_a_recorded_verdict_covers_its_corpus_failure() -> None:
    table, needs, names = _committed()
    head = _head(paths=("docs/a.md",))
    assert len(replay([head], [], table, needs, names).new_misses) == 1
    external = _row(pr=1, run=10, verdict="external")
    covered = replay([head], [external], table, needs, names)
    assert (covered.misses, covered.new_misses, covered.failed_jobs) == ((), (), 1)


def test_t5_a_miss_row_the_table_now_selects_is_proved() -> None:
    table, needs, names = _committed()
    report = replay([], [_row(paths=("src/repoach/a.py",))], table, needs, names)
    assert report.misses == ()


def test_t6_record_refuses_a_duplicate_and_appends_sorted(repo: Path) -> None:
    def record(run: str, pr: str = "5") -> int:
        return (
            CliRunner()
            .invoke(
                _APP,
                [
                    "record",
                    "--pr",
                    pr,
                    "--run",
                    run,
                    "--job",
                    _TEST_311,
                    "--verdict",
                    "external",
                    "--paths",
                    "docs/x.md;src/a.py",
                    "--note",
                    "feed outage",
                ],
            )
            .exit_code
        )

    table = repo / DEFAULT_MISSES_PATH
    committed = load_misses(table)
    assert record("30") == 0
    assert record("20", pr="3") == 0
    before = table.read_bytes()
    assert record("30") == EXIT_MISS
    assert table.read_bytes() == before
    rows = load_misses(table)
    assert [(row.pr, row.run) for row in rows] == sorted(
        [(3, 20), (5, 30), *((row.pr, row.run) for row in committed)]
    )
    assert [row for row in rows if row in committed] == committed
    added = next(row for row in rows if (row.pr, row.run) == (3, 20))
    assert added.paths == ("docs/x.md", "src/a.py")


def test_t6_record_row_refuses_a_duplicate_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "misses.tsv"
    path.write_text(_HEADER, encoding="utf-8")
    record_row(path, _row())
    before = path.read_bytes()
    with pytest.raises(DuplicateRowError):
        record_row(path, _row(verdict="external"))
    assert path.read_bytes() == before


def test_record_takes_paths_and_head_from_the_corpus(repo: Path) -> None:
    head = load_corpus(repo / DEFAULT_CORPUS_PATH)[0]
    job = head.jobs[0].name
    result = CliRunner().invoke(
        _APP,
        [
            "record",
            "--pr",
            str(head.pr),
            "--run",
            str(head.run),
            "--job",
            job,
            "--verdict",
            "conflict",
            "--note",
            "two green pull requests",
        ],
    )
    assert result.exit_code == 0, result.stderr
    row = load_misses(repo / DEFAULT_MISSES_PATH)[0]
    assert (row.head_sha, row.paths) == (head.head_sha, head.paths)


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (
            ["--job", "No such job", "--verdict", "miss", "--paths", "a"],
            "'No such job' is no check",
        ),
        (
            ["--job", _TEST_311, "--verdict", "maybe", "--paths", "a"],
            "verdict 'maybe' is not one of",
        ),
        (["--job", _TEST_311, "--verdict", "miss"], "a miss needs paths: run 0 of #0"),
        (
            ["--job", _TEST_311, "--verdict", "miss", "--paths", "a", "--note", "x\ty"],
            "holds a tab",
        ),
    ],
)
def test_record_refuses_by_name_and_writes_nothing(
    repo: Path, args: list[str], reason: str
) -> None:
    before = (repo / DEFAULT_MISSES_PATH).read_bytes()
    command = ["record", "--pr", "0", "--run", "0", *args]
    if "--note" not in args:
        command += ["--note", "probe"]
    result = CliRunner().invoke(_APP, command)
    assert result.exit_code == EXIT_UNREADABLE
    assert reason in result.stderr
    assert (repo / DEFAULT_MISSES_PATH).read_bytes() == before


def test_c2_a_recorded_probe_miss_turns_the_replay_red(repo: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(_APP, ["replay"]).exit_code == 0
    recorded = runner.invoke(
        _APP,
        [
            "record",
            "--pr",
            "0",
            "--run",
            "0",
            "--job",
            _TEST_311,
            "--verdict",
            "miss",
            "--paths",
            "docs/x.md",
            "--note",
            "probe",
        ],
    )
    assert recorded.exit_code == 0, recorded.stderr
    result = runner.invoke(_APP, ["replay"])
    assert result.exit_code == EXIT_MISS
    assert f"MISS #0 run 0 {_TEST_311} paths docs/x.md…" in result.stderr


def test_t7_a_truncated_head_is_replayed_as_the_full_class() -> None:
    table, needs, names = _committed()
    head = _head(paths=("docs/a.md",), paths_truncated=True)
    report = replay([head], [], table, needs, names)
    assert report.misses == ()
    assert [line.run for line in report.truncated] == [10]
    assert describe(report.truncated[0]) == (
        "TRUNCATED #1 run 10: paths cut by the forge, replayed as the full class"
    )
    listed = replay([head.model_copy(update={"paths_truncated": False})], [], table, needs, names)
    assert len(listed.misses) == 1


def test_replay_refuses_a_job_the_workflow_does_not_produce() -> None:
    table, needs, names = _committed()
    head = _head(jobs=(JobOutcome(name="Lint (gone)", conclusion="failure"),))
    with pytest.raises(UnknownJobError, match="'Lint \\(gone\\)' is no check"):
        replay([head], [], table, needs, names)
    with pytest.raises(UnknownJobError, match="misses #2 run 20"):
        replay([], [_row(job="Lint (gone)", verdict="external")], table, needs, names)


def test_replay_cli_refuses_an_unreadable_corpus(repo: Path) -> None:
    (repo / DEFAULT_CORPUS_PATH).write_text("{", encoding="utf-8")
    result = CliRunner().invoke(_APP, ["replay"])
    assert result.exit_code == EXIT_UNREADABLE
    assert "pr_runs.json: is not JSON" in result.stderr


def test_the_committed_corpus_round_trips_through_write_corpus(tmp_path: Path) -> None:
    path = _ROOT / DEFAULT_CORPUS_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    runs = load_corpus(path)
    copy = tmp_path / "pr_runs.json"
    write_corpus(
        runs,
        copy,
        repo=document["repo"],
        harvested=date.fromisoformat(document["harvested"]),
        source=document["source"],
    )
    assert copy.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d.update(extra=1), "the format defines"),
        (lambda d: d["pull_requests"][0]["heads"][0].pop("paths_truncated"), "#1 a head has keys"),
        (lambda d: d["pull_requests"][0].update(number="1"), "not int"),
        (lambda d: d["pull_requests"][0]["heads"][0].update(paths_truncated=1), "not bool"),
        (
            lambda d: d["pull_requests"][0]["heads"][0]["runs"][0]["jobs"][0].update(
                failed_steps="x"
            ),
            "failed_steps is not a list",
        ),
    ],
)
def test_load_corpus_refuses_what_the_format_does_not_define(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], object], reason: str
) -> None:
    document = corpus_document([_head()], repo="o/r", harvested=date(2026, 11, 17), source="s")
    mutate(document)
    path = tmp_path / "pr_runs.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CorpusError, match=reason):
        load_corpus(path)


def test_load_corpus_refuses_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="cannot be read"):
        load_corpus(tmp_path / "absent.json")


def test_corpus_document_groups_runs_by_pull_request_and_head() -> None:
    runs = [_head(pr=2, run=30), _head(pr=1, run=11, head_sha="b" * 40), _head(pr=1, run=10)]
    document = corpus_document(runs, repo="o/r", harvested=date(2026, 11, 17), source="s")
    assert [pr["number"] for pr in document["pull_requests"]] == [1, 2]
    assert [head["sha"][0] for head in document["pull_requests"][0]["heads"]] == ["a", "b"]
    assert document["harvested"] == "2026-11-17"


def test_corpus_source_names_the_reads_and_no_url() -> None:
    source = corpus_source("develop", "CI")
    assert "--base develop" in source and "--workflow CI" in source
    assert "http" not in source


def test_atomic_write_text_replaces_the_file(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("old", encoding="utf-8")
    atomic_write_text(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n"
    assert [item.name for item in tmp_path.iterdir()] == ["f.txt"]


_RUN_LIST_A = (
    "run",
    "list",
    "--repo",
    "repoachhq/repoach",
    "--branch",
    "feat/a",
    "--event",
    "pull_request",
    "--workflow",
    "CI",
    "--limit",
    str(RUN_LIST_LIMIT),
    "--json",
    "databaseId,status,conclusion,headSha,createdAt,attempt",
)
_COMPARE_A = (
    "api",
    "repos/repoachhq/repoach/compare/" + "1" * 40 + "..." + "a" * 40,
    "--jq",
    "{status: .status, files: [(.files // [])[] | {filename, previous_filename}]}",
)


def _harvest(gh: RecordedGh):
    return harvest("repoachhq/repoach", merged=2, base="develop", workflow="CI", gh=gh)


def test_harvest_counts_a_failure_of_an_earlier_attempt() -> None:
    gh = RecordedGh()
    listing = json.loads(json.dumps(gh.answers[_RUN_LIST_A]["json"]))
    listing[-1]["attempt"] = 2
    gh.answers[_RUN_LIST_A] = {"json": listing}
    first = gh.answers[
        ("run", "view", "101", "--repo", "repoachhq/repoach", "--attempt", "1", "--json", "jobs")
    ]
    passed = json.loads(json.dumps(first["json"]))
    for job in passed["jobs"]:
        job["conclusion"], job["steps"] = "success", []
    gh.answers[
        ("run", "view", "101", "--repo", "repoachhq/repoach", "--attempt", "2", "--json", "jobs")
    ] = {"json": passed}
    run = next(item for item in _harvest(gh).runs if item.run == 101)
    assert run.jobs[0] == JobOutcome(name=_TEST_311, conclusion="failure", failed_steps=("Pytest",))
    assert run.jobs[1].conclusion == "success"


def test_harvest_refuses_a_run_listing_at_its_limit() -> None:
    gh = RecordedGh()
    one = gh.answers[_RUN_LIST_A]["json"][-1]
    gh.answers[_RUN_LIST_A] = {"json": [one] * RUN_LIST_LIMIT}
    with pytest.raises(HarvestError, match="the limit: some may be cut"):
        _harvest(gh)


def test_harvest_refuses_a_head_already_in_its_base() -> None:
    gh = RecordedGh()
    gh.answers[_COMPARE_A] = {"json": {"status": "behind", "files": []}}
    with pytest.raises(HarvestError, match="status 'behind'"):
        _harvest(gh)


def test_harvest_marks_a_head_at_the_compare_cap_truncated() -> None:
    gh = RecordedGh()
    files = [
        {"filename": f"docs/{n}.md", "previous_filename": None} for n in range(COMPARE_FILE_CAP)
    ]
    gh.answers[_COMPARE_A] = {"json": {"status": "ahead", "files": files}}
    run = next(item for item in _harvest(gh).runs if item.run == 101)
    assert run.paths_truncated
    table, needs, names = _committed()
    assert replay([run], [], table, needs, names).misses == ()


def test_harvest_refuses_a_base_that_is_not_a_sha() -> None:
    gh = RecordedGh()
    gh.answers[("api", "repos/repoachhq/repoach/pulls/12", "--jq", ".base.sha")] = {
        "text": "null\n"
    }
    with pytest.raises(HarvestError, match="'null', not a commit sha"):
        _harvest(gh)


def test_harvest_refuses_an_answer_that_is_not_json() -> None:
    gh = RecordedGh()
    gh.answers[_RUN_LIST_A] = {"text": "<html>"}
    with pytest.raises(HarvestError, match="answer is not JSON"):
        _harvest(gh)


def test_resolve_repo_reads_gh_and_refuses_a_non_slug() -> None:
    assert resolve_repo(lambda *_a: "repoachhq/repoach\n") == "repoachhq/repoach"
    with pytest.raises(HarvestError, match="not owner/name"):
        resolve_repo(lambda *_a: "https://github.com/x\n")


def test_workflow_name_reads_the_top_level_name(tmp_path: Path) -> None:
    assert workflow_name(_WORKFLOW) == "CI"
    nameless = tmp_path / "ci.yml"
    nameless.write_text("jobs: {}\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="name must be a non-empty string"):
        workflow_name(nameless)
    with pytest.raises(WorkflowError, match="cannot be read"):
        workflow_name(tmp_path / "absent.yml")


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    binary.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")


def test_run_gh_returns_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(tmp_path, monkeypatch, 'echo "args: $*"')
    assert run_gh("pr", "list") == "args: pr list\n"


def test_run_gh_names_the_command_and_the_first_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(tmp_path, monkeypatch, 'echo "gh auth login required" >&2; echo more >&2; exit 4')
    with pytest.raises(HarvestError) as caught:
        run_gh("pr", "list")
    assert str(caught.value) == "gh pr list: gh auth login required"


def test_run_gh_refuses_a_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(HarvestError, match="gh executable not found"):
        run_gh("pr", "list")


def test_the_suite_reads_no_forge() -> None:
    from repoach.affected import gh

    with pytest.raises(HarvestError, match="the test suite reads no forge"):
        gh.run_gh("pr", "list")


def test_release_gate_reports_the_misses_and_never_refuses_on_them(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = RecordedGh()
    monkeypatch.setattr("repoach.affected.harvest.HARVEST_WINDOW", 2)
    monkeypatch.setattr(
        "repoach.affected.gh.run_gh", lambda *a: gh(*a) if a[0] != "repo" else "repoachhq/repoach\n"
    )
    before = (repo / DEFAULT_CORPUS_PATH).read_bytes()
    report = release_cmds.affected_misses_report(repo)
    assert report["line"] == "affected-jobs misses: 1/1 over the last 2 merged pull requests"
    assert (repo / DEFAULT_CORPUS_PATH).read_bytes() == before


def test_release_gate_says_cannot_say_when_the_forge_refuses(repo: Path) -> None:
    report = release_cmds.affected_misses_report(repo)
    assert str(report["line"]).startswith("affected-jobs misses: cannot say: gh repo view")
    assert "the test suite reads no forge" in str(report["error"])


def test_the_default_window_is_the_spec_constraint() -> None:
    spec = next((_ROOT / "docs" / "specs").glob("*_SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES_*.md"))
    assert f"harvest_window_pull_requests: {HARVEST_WINDOW}" in spec.read_text(encoding="utf-8")


def test_the_commands_are_registered_under_repoach_ci_affected() -> None:
    from repoach.cli.main import app

    for command in ("harvest", "replay", "record"):
        result = CliRunner().invoke(app, ["ci", "affected", command, "--help"])
        assert result.exit_code == 0, result.output


def test_the_corpus_is_scrubbed_to_numbers_shas_paths_and_job_names() -> None:
    path = _ROOT / DEFAULT_CORPUS_PATH
    text = path.read_text(encoding="utf-8")
    corpus = json.loads(text)
    assert corpus["repo"] == "repoachhq/repoach"
    assert "http" not in text
    assert "ghp_" not in text and "github_pat_" not in text
    assert load_corpus(path)


@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure"])
def test_a_timed_out_or_startup_failure_job_counts_as_failed(conclusion: str) -> None:
    table, needs, names = _committed()
    head = _head(paths=("docs/a.md",), jobs=(JobOutcome(name=_TEST_311, conclusion=conclusion),))
    report = replay([head], [], table, needs, names)
    assert report.failed_jobs == 1
    assert [(line.run, line.job) for line in report.misses] == [(10, _TEST_311)]


@pytest.mark.parametrize("conclusion", ["timed_out", "startup_failure"])
def test_harvest_keeps_a_timed_out_or_startup_failure_attempt(conclusion: str) -> None:
    gh = RecordedGh()
    view = ("run", "view", "101", "--repo", "repoachhq/repoach", "--attempt", "1", "--json", "jobs")
    answer = json.loads(json.dumps(gh.answers[view]["json"]))
    answer["jobs"][0]["conclusion"] = conclusion
    answer["jobs"][0]["steps"][1]["conclusion"] = conclusion
    gh.answers[view] = {"json": answer}
    run = next(item for item in _harvest(gh).runs if item.run == 101)
    assert run.jobs[0] == JobOutcome(
        name=_TEST_311, conclusion=conclusion, failed_steps=("Pytest",)
    )


@pytest.mark.parametrize("conclusion", ["action_required", "stale", "neutral", "skipped"])
def test_a_job_that_ran_nothing_or_was_superseded_is_no_failure(conclusion: str) -> None:
    assert job_failed(conclusion) is False
    table, needs, names = _committed()
    head = _head(paths=("docs/a.md",), jobs=(JobOutcome(name=_TEST_311, conclusion=conclusion),))
    assert replay([head], [], table, needs, names).failed_jobs == 0


def test_every_conclusion_is_either_failed_or_not_judged() -> None:
    assert FAILED_CONCLUSIONS.isdisjoint(NOT_JUDGED_CONCLUSIONS)
    assert all(job_failed(item) for item in FAILED_CONCLUSIONS)
    assert not any(job_failed(item) for item in NOT_JUDGED_CONCLUSIONS)


def test_an_unknown_conclusion_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(UnknownConclusionError, match="'exploded' is not one of"):
        job_failed("exploded")
    table, needs, names = _committed()
    head = _head(jobs=(JobOutcome(name=_TEST_311, conclusion="exploded"),))
    with pytest.raises(UnknownConclusionError, match="'exploded'"):
        replay([head], [], table, needs, names)
    document = corpus_document([head], repo="o/r", harvested=date(2026, 11, 17), source="s")
    path = tmp_path / "pr_runs.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CorpusError, match="#1 head aaaaaaa run 10: job conclusion 'exploded'"):
        load_corpus(path)
    gh = RecordedGh()
    view = ("run", "view", "102", "--repo", "repoachhq/repoach", "--attempt", "1", "--json", "jobs")
    answer = json.loads(json.dumps(gh.answers[view]["json"]))
    answer["jobs"][1]["conclusion"] = "exploded"
    gh.answers[view] = {"json": answer}
    with pytest.raises(
        HarvestError, match=r"Test suite \(Python 3\.13\): job conclusion 'exploded'"
    ):
        _harvest(gh)
