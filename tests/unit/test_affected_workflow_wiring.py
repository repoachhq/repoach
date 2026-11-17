"""The CI workflow always runs and an unaffected job is a named no-op (SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED).

These guards read ``.github/workflows/ci.yml`` with the loader the
required-check names come from (:func:`~repoach.review.merge_wait.load_workflow`)
and judge the invariants of D5: ``gate`` selects once and every other
job needs it; every step of those jobs but ``Selection`` runs only when
its own ``job_<id>`` output is ``'true'``; ``Selection`` is first and
unconditional and refuses an output it does not know; the tree lints
run in ``gate`` alone; no workflow narrows a run with a ``paths``
filter. The dry run selects on a documentation-only diff and replays
each step's condition against the outputs ``gate`` would publish.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repoach.affected.cli import affected_app
from repoach.affected.select import DEFAULT_WORKFLOW_PATH, GATE_JOB, workflow_jobs
from repoach.affected.table import DEFAULT_TABLE_PATH, load_table
from repoach.review.auto_merge import DEFAULT_REQUIRED_CHECK_NAMES
from repoach.review.merge_wait import expand_job_names, load_workflow

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / DEFAULT_WORKFLOW_PATH
_WORKFLOWS = _ROOT / ".github" / "workflows"
_SELECTION = "Selection"
_TABLE_ENV = "REPOACH_AFFECTED_JOBS_TABLE"
_GATED = re.compile(r"^needs\.gate\.outputs\.(job_[a-z0-9_]+) == 'true'(?: && (?P<rest>.+))?$")
_TREE_LINTS = (
    "scripts/check_contributing.py",
    "scripts/lint_no_ai_attribution.py",
    "repoach arch check",
)
_CODE_LINTS = (
    "ruff check src tests",
    "ruff format --check src tests",
    "scripts/lint_no_inline_comments.py",
    "scripts/lint_no_silent_except.py",
)
_SELECT_ARGS = {
    "--table": f"${_TABLE_ENV}",
    "--base": "origin/$CI_BASE_BRANCH",
    "--event": "$GITHUB_EVENT_NAME",
    "--base-ref": "$GITHUB_BASE_REF",
    "--github-output": "$GITHUB_OUTPUT",
    "--summary": "$GITHUB_STEP_SUMMARY",
}


def _workflow(path: Path = _CI) -> Mapping[str, Any]:
    data = load_workflow(path)
    assert isinstance(data, Mapping), path.name
    return data


def _jobs() -> Mapping[str, Mapping[str, Any]]:
    return _workflow()["jobs"]


def _legs() -> dict[str, Mapping[str, Any]]:
    return {job_id: job for job_id, job in _jobs().items() if job_id != GATE_JOB}


def _needs(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _output_of(job_id: str) -> str:
    return f"job_{job_id.replace('-', '_')}"


def _triggers(data: Mapping[str, Any]) -> Any:
    return data.get("on", data.get(True))


def _step_running(job: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    found = [step for step in job["steps"] if command in str(step.get("run", ""))]
    assert len(found) == 1, f"{command!r}: {len(found)} steps"
    return found[0]


def _gating_output(step: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return the output a step's ``if`` gates on and the condition it adds.

    Args:
        step: A step of a job other than ``gate``.

    Returns:
        ``(job_<id>, rest)``: the output the condition reads first and
        what it ANDs to it, ``None`` when nothing.

    Raises:
        AssertionError: The ``if`` is not ``needs.gate.outputs.job_<id>
            == 'true'`` optionally followed by ``&& <condition>`` with no
            ``||``, so a false output could still run the step.
    """
    condition = str(step.get("if", ""))
    match = _GATED.match(condition)
    assert match is not None and "||" not in condition, (
        f"step {step.get('name')!r} is not gated: if={condition!r}"
    )
    return match.group(1), match.group("rest")


def test_every_job_but_gate_needs_gate_and_gate_needs_nothing() -> None:
    jobs = _jobs()
    assert "needs" not in jobs[GATE_JOB]
    assert next(iter(jobs)) == GATE_JOB
    for job_id, job in _legs().items():
        assert GATE_JOB in _needs(job), f"{job_id} does not need {GATE_JOB}"


def test_every_leg_step_is_gated_on_its_own_job() -> None:
    for job_id, job in _legs().items():
        for step in job["steps"][1:]:
            output, _ = _gating_output(step)
            assert output == _output_of(job_id), f"{job_id}/{step.get('name')} reads {output}"
        assert _output_of(job_id) in _jobs()[GATE_JOB]["outputs"]


def test_gating_refuses_an_or_and_an_ungated_step() -> None:
    with pytest.raises(AssertionError, match="not gated"):
        _gating_output({"name": "Install", "run": "pip install"})
    with pytest.raises(AssertionError, match="not gated"):
        _gating_output({"name": "x", "if": "needs.gate.outputs.job_test == 'true' || always()"})
    assert _gating_output({"if": "needs.gate.outputs.job_test == 'true' && a == 'b'"}) == (
        "job_test",
        "a == 'b'",
    )


def test_selection_is_first_unconditional_and_the_same_in_every_leg() -> None:
    runs = set()
    for job_id, job in _legs().items():
        first = job["steps"][0]
        assert first["name"] == _SELECTION, job_id
        assert "if" not in first, job_id
        assert first["env"]["AFFECTED"] == f"${{{{ needs.gate.outputs.{_output_of(job_id)} }}}}"
        assert "${GITHUB_JOB}" in first["run"] and "gate job summary" in first["run"]
        assert [s["name"] for s in job["steps"]].count(_SELECTION) == 1, job_id
        runs.add(first["run"])
    assert len(runs) == 1, "the Selection steps diverge; keep one text"


def test_gate_outputs_every_select_output_from_the_select_step() -> None:
    gate = _jobs()[GATE_JOB]
    expected = {_output_of(job_id) for job_id in _legs()} | {"mode", "reason"}
    assert set(gate["outputs"]) == expected
    for name, value in gate["outputs"].items():
        assert value == f"${{{{ steps.select.outputs.{name} }}}}", name


def test_select_step_passes_every_flag_and_the_table_from_env() -> None:
    data = _workflow()
    assert data["env"][_TABLE_ENV] == DEFAULT_TABLE_PATH
    gate = _jobs()[GATE_JOB]
    assert gate["env"]["CI_BASE_BRANCH"] == "${{ github.base_ref || github.ref_name }}"
    select = next(s for s in gate["steps"] if s.get("id") == "select")
    tokens = shlex.split(select["run"])
    assert tokens[:4] == ["repoach", "ci", "affected", "select"]
    flags = tokens[4::2]
    assert len(flags) == len(set(flags)) and len(tokens[4:]) == 2 * len(flags)
    assert dict(zip(flags, tokens[5::2], strict=True)) == _SELECT_ARGS
    assert DEFAULT_TABLE_PATH not in _CI.read_text(encoding="utf-8").replace(
        f"{_TABLE_ENV}: {DEFAULT_TABLE_PATH}", ""
    )


def test_gate_pytest_reads_the_table_from_env() -> None:
    step = _step_running(_jobs()[GATE_JOB], "repoach ci affected gate-pytest")
    assert f'--table "${_TABLE_ENV}"' in step["run"]


def test_tree_lints_run_in_gate_only_and_code_lints_in_test() -> None:
    jobs = _jobs()
    for command in _TREE_LINTS:
        _step_running(jobs[GATE_JOB], command)
        for job_id, job in _legs().items():
            assert not any(command in str(s.get("run", "")) for s in job["steps"]), (
                f"{command!r} still runs in {job_id}"
            )
    for command in _CODE_LINTS:
        _step_running(jobs["test"], command)


def test_job_ids_are_gate_and_the_jobs_the_table_names() -> None:
    needs = workflow_jobs(_CI)
    table = load_table(_ROOT / DEFAULT_TABLE_PATH, needs, workflow=_CI.name)
    named = {job for row in table.rows for job in row.jobs}
    assert set(_jobs()) == {GATE_JOB} | named


def test_no_workflow_narrows_a_run_with_a_paths_filter() -> None:
    workflows = sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])
    assert _CI in workflows
    for path in workflows:
        triggers = _triggers(_workflow(path))
        events = triggers if isinstance(triggers, Mapping) else {}
        for event, spec in events.items():
            if isinstance(spec, Mapping):
                assert not {"paths", "paths-ignore"} & set(spec), f"{path.name}: on.{event}"


def test_triggers_and_concurrency_are_today_s_with_develop_pushes() -> None:
    data = _workflow()
    assert _triggers(data) == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
        "workflow_dispatch": None,
    }
    assert data["concurrency"] == {"group": "ci-${{ github.ref }}", "cancel-in-progress": True}


def _run_selection(tmp_path: Path, job_id: str, affected: str, reason: str) -> tuple[int, str, str]:
    """Run a leg's committed ``Selection`` step under bash, as the runner does.

    Args:
        tmp_path: Where the step summary file is written.
        job_id: The leg whose step is run, also its ``GITHUB_JOB``.
        affected: The ``AFFECTED`` value (the leg's ``job_<id>`` output).
        reason: The ``SELECTION_REASON`` value.

    Returns:
        The exit code, stdout, and what the step appended to its summary.
    """
    bash = shutil.which("bash")
    assert bash is not None
    summary = tmp_path / f"summary-{job_id}.md"
    summary.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_JOB": job_id,
        "GITHUB_STEP_SUMMARY": str(summary),
        "AFFECTED": affected,
        "SELECTION_MODE": "narrowed",
        "SELECTION_REASON": reason,
    }
    done = subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", _legs()[job_id]["steps"][0]["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout, summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(("affected", "verdict"), [("true", "affected"), ("false", "not affected")])
def test_selection_prints_one_notice_and_one_summary_line(
    tmp_path: Path, affected: str, verdict: str
) -> None:
    code, stdout, summary = _run_selection(tmp_path, "test", affected, "r")
    line = f"test: {verdict} by this diff (narrowed, r); selection in the gate job summary"
    assert code == 0
    assert stdout == f"::notice::{line}\n"
    assert summary == f"{line}\n"


@pytest.mark.parametrize("affected", ["", "True", "yes"])
def test_selection_refuses_an_unknown_output_by_name(tmp_path: Path, affected: str) -> None:
    code, stdout, summary = _run_selection(tmp_path, "wheel-smoke", affected, "r")
    assert code == 1
    assert stdout == (
        f"::error::wheel-smoke: the gate output for this job is '{affected}', "
        "neither true nor false\n"
    )
    assert summary == ""


def test_selection_keeps_a_reason_naming_a_command_inside_the_notice(tmp_path: Path) -> None:
    reason = "unknown path: ::error::x $(id) `id`"
    code, stdout, _ = _run_selection(tmp_path, "test", "false", reason)
    assert code == 0
    assert stdout.count("\n") == 1
    assert stdout.startswith(
        "::notice::test: not affected by this diff (narrowed, unknown path: ::"
    )
    assert "$(id) `id`" in stdout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _docs_only_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return the ``$GITHUB_OUTPUT`` the gate's select writes for a docs-only pull request.

    Args:
        tmp_path: Where the throwaway repository and output file live.
        monkeypatch: Moves the working directory into that repository.

    Returns:
        Output name to value, from a repository holding the committed
        table and workflow and one commit adding ``docs/x.md``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    for relative in (DEFAULT_TABLE_PATH, DEFAULT_WORKFLOW_PATH):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((_ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "topic")
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")
    monkeypatch.chdir(repo)
    output = tmp_path / "github_output"
    result = CliRunner().invoke(
        affected_app,
        [
            "select",
            "--base",
            "develop",
            "--event",
            "pull_request",
            "--base-ref",
            "develop",
            "--github-output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.stderr
    pairs = (line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    return dict(pairs)


def test_a_docs_only_diff_makes_every_leg_a_success_named_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = _docs_only_outputs(tmp_path, monkeypatch)
    assert outputs["mode"] == "narrowed"
    names: list[str] = [GATE_JOB]
    for job_id, job in _legs().items():
        assert outputs[_output_of(job_id)] == "false", job_id
        for step in job["steps"][1:]:
            output, _ = _gating_output(step)
            assert outputs[output] != "true", f"{job_id}/{step.get('name')} would run"
        code, stdout, _ = _run_selection(
            tmp_path, job_id, outputs[_output_of(job_id)], outputs["reason"]
        )
        assert (code, stdout.split(" by ")[0]) == (0, f"::notice::{job_id}: not affected")
        names.extend(expand_job_names(job_id, job))
    assert names == list(DEFAULT_REQUIRED_CHECK_NAMES)
