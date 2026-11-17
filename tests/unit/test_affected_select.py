"""The selection of the jobs a diff affects (SP-AFFECTED-JOBS-TABLE T3-T7, D3, D4, D6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from repoach.affected.select import (
    BASE_REMOTE,
    DEFAULT_WORKFLOW_PATH,
    GATE_JOB,
    GitError,
    Selection,
    WorkflowError,
    changed_paths,
    check_name_jobs,
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
from repoach.affected.table import DEFAULT_TABLE_PATH, AffectedTable, load_table, matches
from repoach.review.merge_wait import expand_job_names, load_workflow

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / DEFAULT_WORKFLOW_PATH


def _committed() -> tuple[AffectedTable, dict[str, frozenset[str]]]:
    needs = workflow_jobs(_WORKFLOW)
    return load_table(_ROOT / DEFAULT_TABLE_PATH, needs, workflow=_WORKFLOW.name), needs


def _narrowed(paths: list[str]) -> Selection:
    table, needs = _committed()
    return select(paths, table, needs, mode="narrowed", reason="narrowed: pull_request on develop")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def test_every_tracked_path_is_classified() -> None:
    tracked = [path for path in _git(_ROOT, "ls-files", "-z").split("\0") if path]
    table, _ = _committed()
    first = next(
        (
            path
            for path in tracked
            if not matches(table.full, path) and not any(matches(row, path) for row in table.rows)
        ),
        None,
    )
    assert first is None, f"{first} matches no row of {DEFAULT_TABLE_PATH} and is not in [full]"


def test_unknown_path_selects_every_job() -> None:
    selection = _narrowed(["Makefile"])
    assert selection.jobs == {GATE_JOB, *workflow_jobs(_WORKFLOW)}
    assert selection.reason == "unknown path: Makefile"
    assert selection.rows_hit == {"unknown": ["Makefile"]}
    assert selection.mode == "narrowed"


def test_full_class_path_selects_every_job() -> None:
    selection = _narrowed(["docs/a.md", ".github/workflows/ci.yml"])
    assert selection.jobs == {GATE_JOB, *workflow_jobs(_WORKFLOW)}
    assert selection.reason == "full class: .github/"


def test_empty_diff_selects_gate_alone() -> None:
    selection = _narrowed([])
    assert selection.jobs == {GATE_JOB}
    assert selection.paths == 0


def test_docs_and_src_select_test_and_the_wheel() -> None:
    selection = _narrowed(["docs/specs/x.md", "src/repoach/review/a.py"])
    assert selection.jobs == {GATE_JOB, "test", "wheel-build", "wheel-smoke"}
    assert selection.rows_hit == {
        "docs": ["docs/specs/x.md"],
        "python": ["src/repoach/review/a.py"],
        "wheel": ["src/repoach/review/a.py"],
    }
    assert selection.reason == "narrowed: pull_request on develop"


def test_docs_only_selects_gate_alone() -> None:
    selection = _narrowed(["docs/getting_started.md"])
    assert selection.jobs == {GATE_JOB}
    assert selection.rows_hit == {"docs": ["docs/getting_started.md"]}


def test_full_mode_selects_every_job_and_keeps_its_reason() -> None:
    table, needs = _committed()
    selection = select(["docs/a.md"], table, needs, mode="full", reason="full: push on main")
    assert selection.jobs == {GATE_JOB, *needs}
    assert selection.reason == "full: push on main"
    assert selection.rows_hit == {"docs": ["docs/a.md"]}


def test_rows_hit_keeps_three_paths_per_row() -> None:
    selection = _narrowed([f"docs/{n}.md" for n in range(5)])
    assert selection.rows_hit == {"docs": ["docs/0.md", "docs/1.md", "docs/2.md"]}
    assert selection.paths == 5


def test_selection_is_closed_under_the_real_needs() -> None:
    table, needs = _committed()
    assert "wheel-build" in needs["wheel-smoke"]
    narrow = table.model_copy(
        update={
            "rows": tuple(
                row.model_copy(update={"jobs": frozenset({"wheel-smoke"})})
                if row.name == "wheel"
                else row
                for row in table.rows
            )
        }
    )
    selection = select(["prompts/review/x.md"], narrow, needs, mode="narrowed", reason="r")
    assert {"wheel-smoke", "wheel-build"} <= selection.jobs


def test_github_output_lines_one_per_job_then_mode() -> None:
    selection = _narrowed(["src/a.py"])
    assert github_output_lines(selection, ["test", "wheel-build", "wheel-smoke"]) == [
        "job_test=true",
        "job_wheel_build=true",
        "job_wheel_smoke=true",
        "mode=narrowed",
        "reason=narrowed: pull_request on develop",
    ]
    docs = _narrowed(["docs/a.md"])
    assert github_output_lines(docs, ["test", "wheel-build"]) == [
        "job_test=false",
        "job_wheel_build=false",
        "mode=narrowed",
        "reason=narrowed: pull_request on develop",
    ]


def test_github_output_reason_keeps_an_authored_line_break_on_its_line() -> None:
    selection = _narrowed(["odd\npath\r\njob_test=false"])
    lines = github_output_lines(selection, ["test"])
    assert lines[-1] == "reason=unknown path: odd\\npath\\r\\njob_test=false"
    assert "\n".join(lines).count("\n") == len(lines) - 1
    assert [line for line in lines if line.startswith("job_test=")] == ["job_test=true"]


def test_summary_markdown_names_mode_rows_and_jobs() -> None:
    text = summary_markdown(_narrowed(["docs/a.md", "src/b.py"]))
    assert "Mode `narrowed`: `narrowed: pull_request on develop`. Paths judged: 2." in text
    assert "| `docs` | `docs/a.md` |" in text
    assert "| `python` | `src/b.py` |" in text
    assert "Jobs selected: `gate`, `test`, `wheel-build`, `wheel-smoke`" in text


@pytest.mark.parametrize(
    ("event", "base_ref", "expected"),
    [
        ("pull_request", "develop", ("narrowed", "narrowed: pull_request on develop")),
        ("pull_request", "main", ("full", "full: pull_request on main")),
        ("push", "", ("full", "full: push on main")),
        ("workflow_dispatch", "", ("full", "full: workflow_dispatch on main")),
    ],
)
def test_decide_mode(event: str, base_ref: str, expected: tuple[str, str]) -> None:
    assert (
        decide_mode(event=event, base_ref=base_ref, ref="main", integration_branch="develop")
        == expected
    )


_MATRIX_WORKFLOW = """\
jobs:
  test:
    name: Test suite (Python ${{ matrix.python-version }})
    strategy:
      matrix:
        python-version: ["3.11", "3.13"]
  build:
    name: build
  smoke:
    needs: build
    strategy:
      matrix:
        python-version: ["3.11"]
  report:
    needs: [test, smoke]
"""


def test_workflow_jobs_reads_string_and_list_needs(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(_MATRIX_WORKFLOW, encoding="utf-8")
    assert workflow_jobs(path) == {
        "test": frozenset(),
        "build": frozenset(),
        "smoke": frozenset({"build"}),
        "report": frozenset({"test", "smoke"}),
    }


def test_the_real_wheel_smoke_needs_wheel_build() -> None:
    assert "wheel-build" in workflow_jobs(_WORKFLOW)["wheel-smoke"]


def test_check_name_jobs_expands_the_matrix(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(_MATRIX_WORKFLOW, encoding="utf-8")
    assert check_name_jobs(path) == {
        "Test suite (Python 3.11)": "test",
        "Test suite (Python 3.13)": "test",
        "build": "build",
        "smoke (3.11)": "smoke",
        "report": "report",
    }


def test_closure_is_transitive(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(_MATRIX_WORKFLOW, encoding="utf-8")
    needs = workflow_jobs(path)
    table, _ = _committed()
    report_only = table.model_copy(
        update={
            "rows": tuple(
                row.model_copy(update={"jobs": frozenset({"report"})})
                if row.name == "python"
                else row
                for row in table.rows
            )
        }
    )
    selection = select(["tests/x.py"], report_only, needs, mode="narrowed", reason="r")
    assert selection.jobs == {GATE_JOB, "report", "test", "smoke", "build"}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("name: CI\n", "ci.yml: jobs has no jobs mapping"),
        ("jobs:\n  a: 1\n", "ci.yml: jobs.a must be a mapping"),
        (
            "jobs:\n  a:\n    needs: {x: 1}\n",
            "ci.yml: jobs.a.needs must be a job id or a list of job ids",
        ),
        ("jobs:\n  a:\n    needs: [b]\n", "ci.yml: jobs.a.needs names 'b', not a job"),
        ("jobs: [\n", "ci.yml: is not YAML"),
    ],
)
def test_workflow_jobs_names_the_defect(tmp_path: Path, document: str, message: str) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(WorkflowError) as caught:
        workflow_jobs(path)
    assert str(caught.value).startswith(message)


def test_workflow_jobs_unreadable(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError, match=r"ci\.yml: cannot be read"):
        workflow_jobs(tmp_path / "ci.yml")


def test_load_workflow_is_safe_load_and_raises(tmp_path: Path) -> None:
    path = tmp_path / "w.yml"
    path.write_text("jobs:\n  a: {needs: b}\n", encoding="utf-8")
    assert load_workflow(path) == {"jobs": {"a": {"needs": "b"}}}
    path.write_text("jobs: [\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_workflow(path)
    with pytest.raises(OSError):
        load_workflow(tmp_path / "absent.yml")


def test_expand_job_names() -> None:
    matrix = {"strategy": {"matrix": {"python-version": ["3.11", "3.13"]}}}
    assert expand_job_names("t", {"name": "T (${{ matrix.python-version }})", **matrix}) == [
        "T (3.11)",
        "T (3.13)",
    ]
    assert expand_job_names("w", {"name": "w", **matrix}) == ["w (3.11)", "w (3.13)"]
    assert expand_job_names("b", {}) == ["b"]


@pytest.fixture
def two_commit_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    (repo / "gone.txt").write_text("g\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "topic")
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    _git(repo, "rm", "-q", "gone.txt")
    _git(repo, "commit", "-q", "-m", "rename and delete")
    return repo


def test_changed_paths_lists_both_names_of_a_rename_and_deletions(two_commit_repo: Path) -> None:
    assert sorted(changed_paths(two_commit_repo, base="main")) == [
        "docs/a.md",
        "docs/b.md",
        "gone.txt",
    ]
    assert changed_paths(two_commit_repo, base="topic") == []


def test_changed_paths_names_the_failing_command(two_commit_repo: Path) -> None:
    with pytest.raises(GitError) as caught:
        changed_paths(two_commit_repo, base="no-such-ref")
    assert caught.value.command == "git merge-base no-such-ref HEAD"
    assert caught.value.detail


def test_changed_paths_never_passes_an_option_like_revision(
    two_commit_repo: Path, tmp_path: Path
) -> None:
    written = tmp_path / "written"
    for base, head in ((f"--output={written}", "HEAD"), ("main", f"--output={written}")):
        with pytest.raises(GitError) as caught:
            changed_paths(two_commit_repo, base=base, head=head)
        assert "starts with '-'" in caught.value.detail
    assert not written.exists()


def test_summary_markdown_keeps_a_hostile_path_inside_its_cell() -> None:
    text = summary_markdown(_narrowed(["docs/a`|[x](http://e)`.md"]))
    row = next(line for line in text.splitlines() if line.startswith("| `docs`"))
    assert row == "| `docs` | `` docs/a`\\|[x](http://e)`.md `` |"
    assert row.count(" | ") == 1


def test_summary_markdown_keeps_a_hostile_reason_on_its_line() -> None:
    selection = _narrowed(["docs/a.md"]).model_copy(
        update={"reason": "unknown path: x\n::stop-commands::t|[l](http://e)"}
    )
    text = summary_markdown(selection)
    mode_line = next(line for line in text.splitlines() if line.startswith("Mode "))
    assert mode_line == (
        "Mode `narrowed`: `unknown path: x\\n::stop-commands::t\\|[l](http://e)`. Paths judged: 1."
    )
    assert not any(line.startswith("::") for line in text.splitlines())


def test_one_line_writes_out_line_breaks() -> None:
    assert one_line("a\r\nb") == "a\\r\\nb"


def test_default_base_is_the_remote_tracking_integration_branch() -> None:
    assert default_base("develop") == f"{BASE_REMOTE}/develop"


def test_matrix_values_reads_one_axis(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(_MATRIX_WORKFLOW, encoding="utf-8")
    assert matrix_values(path, "test", "python-version") == ["3.11", "3.13"]
    with pytest.raises(WorkflowError, match=r"jobs\.build\.strategy\.matrix\.python-version"):
        matrix_values(path, "build", "python-version")
    with pytest.raises(WorkflowError, match=r"jobs\.absent is not a job"):
        matrix_values(path, "absent", "python-version")


def test_job_running_names_the_one_job_running_a_command(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "jobs:\n"
        "  a:\n    steps:\n      - run: pytest -q tests/unit -n auto\n"
        "  b:\n    steps:\n      - uses: actions/checkout\n      - run: python -m build\n"
        "  c:\n    steps:\n      - run: python -m build --sdist\n",
        encoding="utf-8",
    )
    assert job_running(path, "pytest -q tests/unit") == "a"
    with pytest.raises(WorkflowError, match=r"jobs that do: b, c"):
        job_running(path, "python -m build")
    with pytest.raises(WorkflowError, match=r"jobs that do: none"):
        job_running(path, "ruff")
