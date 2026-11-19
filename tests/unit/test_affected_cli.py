"""``repoach ci affected`` and ``scripts/ci_local.sh --affected`` (SP-AFFECTED-JOBS-TABLE T9-T11)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.affected.cli import EXIT_GIT, EXIT_UNREADABLE, affected_app
from repoach.affected.select import DEFAULT_WORKFLOW_PATH, matrix_values, workflow_jobs
from repoach.affected.table import DEFAULT_TABLE_PATH

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "ci_local.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with the real table and workflow: ``develop``, then ``topic`` adding docs and src."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    for relative in (DEFAULT_TABLE_PATH, DEFAULT_WORKFLOW_PATH):
        _write(root / relative, (_ROOT / relative).read_text(encoding="utf-8"))
    _write(root / "README.md", "r\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    _write(root / "docs" / "specs" / "x.md", "x\n")
    _write(root / "src" / "repoach" / "a.py", "A = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "change")
    monkeypatch.chdir(root)
    return root


def test_select_prints_one_json_object(repo: Path) -> None:
    result = CliRunner().invoke(affected_app, ["select", "--base", "develop"])
    assert result.exit_code == 0, result.stderr
    selection = json.loads(result.stdout)
    assert selection == {
        "mode": "narrowed",
        "reason": "narrowed: pull_request on develop",
        "paths": 2,
        "jobs": ["gate", "test", "wheel-build", "wheel-smoke"],
        "rows_hit": {
            "docs": ["docs/specs/x.md"],
            "python": ["src/repoach/a.py"],
            "wheel": ["src/repoach/a.py"],
        },
    }
    assert "repoach ci affected: narrowed" in result.stderr


def test_select_defaults_base_to_the_integration_branch_remote(repo: Path) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/develop", "develop")
    result = CliRunner().invoke(affected_app, ["select"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["paths"] == 2


def test_select_writes_github_output_and_summary(repo: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    summary = tmp_path / "summary.md"
    output.write_text("earlier=1\n", encoding="utf-8")
    summary.write_text("### An earlier step\n", encoding="utf-8")
    result = CliRunner().invoke(
        affected_app,
        ["select", "--base", "develop", "--github-output", str(output), "--summary", str(summary)],
    )
    assert result.exit_code == 0, result.stderr
    job_ids = list(workflow_jobs(repo / DEFAULT_WORKFLOW_PATH))
    assert job_ids
    assert output.read_text(encoding="utf-8").splitlines() == [
        "earlier=1",
        *(f"job_{job_id.replace('-', '_')}=true" for job_id in job_ids),
        "mode=narrowed",
        "reason=narrowed: pull_request on develop",
    ]
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("### An earlier step\n### Affected jobs\n")
    assert "| `docs` | `docs/specs/x.md` |" in text
    assert "Jobs selected: `gate`, `test`, `wheel-build`, `wheel-smoke`" in text


def test_select_unknown_base_exits_3_naming_the_command(repo: Path) -> None:
    result = CliRunner().invoke(affected_app, ["select", "--base", "no-such-ref"])
    assert result.exit_code == EXIT_GIT
    assert result.stdout == ""
    assert "git merge-base no-such-ref HEAD" in result.stderr


def test_select_bad_table_key_exits_2_and_prints_no_selection(repo: Path) -> None:
    table = repo / DEFAULT_TABLE_PATH
    table.write_text(
        table.read_text(encoding="utf-8").replace('"wheel-smoke"]', '"wheel-smok"]'),
        encoding="utf-8",
    )
    result = CliRunner().invoke(affected_app, ["select", "--base", "develop"])
    assert result.exit_code == EXIT_UNREADABLE
    assert result.stdout == ""
    assert (
        "affected-jobs.toml: rows.wheel.jobs names 'wheel-smok', not a job of ci.yml"
        in result.stderr
    )


def test_select_unreadable_workflow_exits_2(repo: Path) -> None:
    (repo / DEFAULT_WORKFLOW_PATH).unlink()
    result = CliRunner().invoke(affected_app, ["select", "--base", "develop"])
    assert result.exit_code == EXIT_UNREADABLE
    assert result.stdout == ""
    assert "ci.yml: cannot be read" in result.stderr


def test_select_outside_a_repository_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    result = CliRunner().invoke(affected_app, ["select", "--base", "develop"])
    assert result.exit_code == EXIT_GIT
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--event", "push", "--base", "develop", "--base-ref", ""], "full: push on develop"),
        (
            ["--event", "pull_request", "--base", "develop", "--base-ref", "main"],
            "full: pull_request on main",
        ),
        (
            ["--event", "workflow_dispatch", "--base", "develop", "--base-ref", ""],
            "full: workflow_dispatch on develop",
        ),
    ],
)
def test_select_full_mode_events(repo: Path, args: list[str], reason: str) -> None:
    result = CliRunner().invoke(affected_app, ["select", *args])
    assert result.exit_code == 0, result.stderr
    selection = json.loads(result.stdout)
    assert selection["mode"] == "full"
    assert selection["reason"] == reason
    assert selection["jobs"] == ["gate", "test", "wheel-build", "wheel-smoke"]
    assert selection["paths"] == 2


def test_gate_pytest_prints_the_selectors(repo: Path) -> None:
    result = CliRunner().invoke(affected_app, ["gate-pytest"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        line.strip().strip('",')
        for line in (repo / DEFAULT_TABLE_PATH).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('"tests/')
    ]


_STUB_REPOACH = """#!/usr/bin/env bash
echo "repoach $*" >> "$STUB_LOG"
case "$1 $2 $3" in
    "ci affected select")
        if [[ "$STUB_SELECT_EXIT" != 0 ]] ; then exit "$STUB_SELECT_EXIT" ; fi
        while [[ $# -gt 0 ]] ; do
            if [[ "$1" == --github-output ]] ; then printf '%b' "$STUB_SELECTION" >> "$2" ; fi
            shift
        done
        echo '{"mode": "narrowed"}'
        ;;
    "ci affected gate-pytest") [[ -n "$STUB_GATE_EMPTY" ]] || echo tests/unit/test_gate_judge.py ;;
    "ci affected base") echo main ;;
    "ci affected matrix" | "ci affected job-running") PYTHONPATH="$REAL_PYTHONPATH" exec "$REAL_PYTHON" -m repoach.cli.main "$@" ;;
esac
"""

_STUB_LOGGER = """#!/usr/bin/env bash
if [[ "$1" == -c ]] ; then [[ " $STUB_MISSING " != *" ${2#import } "* ]] ; exit ; fi
echo "$(basename "$0") $*" >> "$STUB_LOG"
"""


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout holding ``scripts/ci_local.sh`` and stub tools, and the stub directory."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / "ci_local.sh")
    _write(root / "scripts" / "job_proxy.sh", "exit 0\n")
    _write(root / "scripts" / "wheel_smoke.sh", 'echo "wheel_smoke $*" >> "$STUB_LOG"\n')
    _write(
        root / DEFAULT_WORKFLOW_PATH, (_ROOT / DEFAULT_WORKFLOW_PATH).read_text(encoding="utf-8")
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (
        ("repoach", _STUB_REPOACH),
        ("python", _STUB_LOGGER),
        ("ruff", _STUB_LOGGER),
        ("python3.11", _STUB_LOGGER),
        ("python3.13", _STUB_LOGGER),
        ("python3.12", _STUB_LOGGER),
        ("pipx", _STUB_LOGGER),
    ):
        stub = stubs / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return root, stubs


def _run_affected(
    checkout: tuple[Path, Path],
    selection: str,
    select_exit: int = 0,
    *,
    base: tuple[str, ...] = ("main",),
    missing: str = "",
    gate_empty: bool = False,
) -> tuple[int, str, list[str]]:
    root, stubs = checkout
    log = stubs.parent / "calls.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "STUB_SELECTION": selection,
        "STUB_SELECT_EXIT": str(select_exit),
        "STUB_MISSING": missing,
        "STUB_GATE_EMPTY": "1" if gate_empty else "",
        "REAL_PYTHON": sys.executable,
        "REAL_PYTHONPATH": str(_ROOT / "src"),
    }
    completed = subprocess.run(
        ["bash", str(root / "scripts" / "ci_local.sh"), "--affected", *base],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return completed.returncode, completed.stdout, log.read_text(encoding="utf-8").splitlines()


def test_ci_local_affected_docs_selection_runs_the_gate_only(checkout: tuple[Path, Path]) -> None:
    rc, stdout, calls = _run_affected(
        checkout,
        "job_test=false\\njob_wheel_build=false\\njob_wheel_smoke=false\\nmode=narrowed\\n",
    )
    assert rc == 0, stdout
    assert '{"mode": "narrowed"}' in stdout
    assert calls[0].startswith("repoach ci affected select --base main --github-output ")
    assert calls[1:] == [
        "repoach ci affected job-running pytest -q tests/unit",
        "repoach ci affected job-running python -m build",
        "repoach ci affected job-running scripts/wheel_smoke.sh",
        "repoach arch check --base main",
        "repoach ci affected gate-pytest",
        "python -m pytest -q tests/unit/test_gate_judge.py",
        "python scripts/check_contributing.py CONTRIBUTING.md",
        "python scripts/lint_no_ai_attribution.py --range main..HEAD",
    ]


def test_ci_local_affected_skips_gate_pytest_when_table_names_none(
    checkout: tuple[Path, Path],
) -> None:
    rc, stdout, calls = _run_affected(
        checkout,
        "job_test=false\\njob_wheel_build=false\\njob_wheel_smoke=false\\nmode=narrowed\\n",
        gate_empty=True,
    )
    assert rc == 0, stdout
    assert "[gate].pytest names no selector — stage skipped" in stdout
    assert "repoach ci affected gate-pytest" in calls
    assert not any(call.startswith("python -m pytest") for call in calls)


def test_ci_local_affected_test_selection_adds_lint_and_suite(checkout: tuple[Path, Path]) -> None:
    rc, stdout, calls = _run_affected(
        checkout, "job_test=true\\njob_wheel_build=false\\njob_wheel_smoke=false\\nmode=narrowed\\n"
    )
    assert rc == 0, stdout
    assert "python -P -m ruff check src tests" in calls
    assert "python -m pytest -q tests/unit -n auto --dist worksteal" in calls
    assert not any(call.startswith("python -m build") for call in calls)


def test_ci_local_affected_wheel_selection_adds_the_wheel_block(
    checkout: tuple[Path, Path],
) -> None:
    rc, stdout, calls = _run_affected(
        checkout, "job_test=false\\njob_wheel_build=true\\njob_wheel_smoke=true\\nmode=narrowed\\n"
    )
    assert rc == 0, stdout
    assert any(call.startswith("python -m build --sdist --wheel") for call in calls)
    assert not any(call.startswith("python -P -m ruff") for call in calls)
    assert "python -m pytest -q tests/unit -n auto --dist worksteal" not in calls


def test_ci_local_affected_stops_when_select_fails(checkout: tuple[Path, Path]) -> None:
    rc, _, calls = _run_affected(checkout, "", select_exit=2)
    assert rc == 2
    assert len(calls) == 1
    assert calls[0].startswith("repoach ci affected select")


def test_ci_local_affected_without_base_reads_it_from_repoach(checkout: tuple[Path, Path]) -> None:
    rc, stdout, calls = _run_affected(
        checkout,
        "job_test=false\\njob_wheel_build=false\\njob_wheel_smoke=false\\nmode=narrowed\\n",
        base=(),
    )
    assert rc == 0, stdout
    assert calls[0] == "repoach ci affected base"
    assert calls[1].startswith("repoach ci affected select --base main --github-output ")


def test_ci_local_wheel_block_refuses_by_name_when_build_is_missing(
    checkout: tuple[Path, Path],
) -> None:
    rc, stdout, calls = _run_affected(
        checkout,
        "job_test=false\\njob_wheel_build=true\\njob_wheel_smoke=true\\nmode=narrowed\\n",
        missing="build twine",
    )
    assert rc == 1
    assert "refused: missing python module build python module twine" in stdout
    assert not any(call.startswith("python -m build") for call in calls)


def test_base_prints_the_integration_branch_remote(repo: Path) -> None:
    result = CliRunner().invoke(affected_app, ["base"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "origin/develop\n"
    (repo / "repoach.toml").write_text(
        '[repoach]\nintegration_branch = "trunk"\n', encoding="utf-8"
    )
    assert CliRunner().invoke(affected_app, ["base"]).stdout == "origin/trunk\n"


def test_base_refuses_an_invalid_repoach_toml(repo: Path) -> None:
    (repo / "repoach.toml").write_text("[repoach]\nno_such_key = 1\n", encoding="utf-8")
    result = CliRunner().invoke(affected_app, ["base"])
    assert result.exit_code == EXIT_UNREADABLE
    assert result.stdout == ""


_WHEEL_SELECTION = "job_test=false\\njob_wheel_build=true\\njob_wheel_smoke=true\\nmode=narrowed\\n"


def _smoke_pythons(calls: list[str]) -> list[str]:
    return [
        Path(call.split("--python ")[1].split()[0]).name
        for call in calls
        if call.startswith("wheel_smoke ")
    ]


def test_ci_local_smokes_every_python_of_the_ci_wheel_smoke_matrix(
    checkout: tuple[Path, Path],
) -> None:
    rc, stdout, calls = _run_affected(checkout, _WHEEL_SELECTION)
    assert rc == 0, stdout
    expected = [
        f"python{version}"
        for version in matrix_values(_ROOT / DEFAULT_WORKFLOW_PATH, "wheel-smoke", "python-version")
    ]
    assert _smoke_pythons(calls) == expected


def test_changing_the_matrix_changes_the_pythons_ci_local_smokes(
    checkout: tuple[Path, Path],
) -> None:
    root, _ = checkout
    workflow = root / DEFAULT_WORKFLOW_PATH
    text = workflow.read_text(encoding="utf-8")
    smoke = text.index("wheel-smoke:")
    edited = text[:smoke] + text[smoke:].replace(
        'python-version: ["3.11", "3.13"]', 'python-version: ["3.12"]', 1
    )
    assert edited != text
    workflow.write_text(edited, encoding="utf-8")
    rc, stdout, calls = _run_affected(checkout, _WHEEL_SELECTION)
    assert rc == 0, stdout
    assert _smoke_pythons(calls) == ["python3.12"]


def test_matrix_prints_one_axis_of_one_job(repo: Path) -> None:
    result = CliRunner().invoke(affected_app, ["matrix", "wheel-smoke", "python-version"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == matrix_values(
        repo / DEFAULT_WORKFLOW_PATH, "wheel-smoke", "python-version"
    )


def test_matrix_refuses_an_unknown_job_or_axis(repo: Path) -> None:
    for args, key in (
        (["nope", "python-version"], "jobs.nope"),
        (["wheel-smoke", "os"], "jobs.wheel-smoke.strategy.matrix.os"),
    ):
        result = CliRunner().invoke(affected_app, ["matrix", *args])
        assert result.exit_code == EXIT_UNREADABLE
        assert result.stdout == ""
        assert key in result.stderr


def _rename_jobs(root: Path, renames: dict[str, str]) -> None:
    workflow = root / DEFAULT_WORKFLOW_PATH
    text = workflow.read_text(encoding="utf-8")
    for old, new in renames.items():
        assert f"\n  {old}:\n" in text
        text = text.replace(f"\n  {old}:\n", f"\n  {new}:\n").replace(
            f"needs: {old}\n", f"needs: {new}\n"
        )
    workflow.write_text(text, encoding="utf-8")


def test_ci_local_reads_the_job_ids_from_ci_yml(checkout: tuple[Path, Path]) -> None:
    root, _ = checkout
    _rename_jobs(root, {"test": "suite", "wheel-build": "pkg", "wheel-smoke": "pkg-smoke"})
    rc, stdout, calls = _run_affected(
        checkout, "job_suite=true\\njob_pkg=false\\njob_pkg_smoke=false\\nmode=narrowed\\n"
    )
    assert rc == 0, stdout
    assert "python -m pytest -q tests/unit -n auto --dist worksteal" in calls
    assert not any(call.startswith("python -m build") for call in calls)
    rc, stdout, calls = _run_affected(
        checkout, "job_test=true\\njob_pkg=true\\njob_pkg_smoke=false\\nmode=narrowed\\n"
    )
    assert rc == 0, stdout
    assert "python -m pytest -q tests/unit -n auto --dist worksteal" not in calls
    assert any(call.startswith("python -m build --sdist --wheel") for call in calls)


def test_job_running_names_the_one_job_and_refuses_otherwise(repo: Path) -> None:
    assert (
        CliRunner().invoke(affected_app, ["job-running", "pytest -q tests/unit"]).stdout == "test\n"
    )
    for command in ("no-such-command", "actions/checkout"):
        result = CliRunner().invoke(affected_app, ["job-running", command])
        assert result.exit_code == EXIT_UNREADABLE
        assert result.stdout == ""
        assert f"one job must run {command!r}" in result.stderr
