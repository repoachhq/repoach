"""Tests for SP-UNIVERSAL-GATES — project-declared lint / test / gate commands.

AC2: a fake ``lint`` script exiting 1 makes :func:`run_lint_gate` red
with the script's last line in the tail; an empty ``lint`` is a printed
skip; a missing binary is red with ``command not found``.  Also pinned:
the TypeScript-only fake repository whose ``lint`` exits 1 / 0 through
the Coder's own gate (:func:`run_ruff_gate`), the ``{python}`` /
``{target}`` expansion of the test command, ``NAME=value`` prefixes,
timeouts, the Developer's ``run_tests`` / ``run_ruff`` tools reading
the configuration, and the built-in path (no ``repoach.toml``) running
exactly today's ruff argv.
"""

from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest

from repoach.gates.commands import (
    expand_test_command,
    run_lint_gate,
    run_project_gate,
    run_test_gate,
    split_command,
)
from repoach.gates.config import GatesConfig, load_gates_config
from repoach.review import coder_loop
from repoach.review.coder_loop import run_pytest_matrix, run_ruff_gate
from repoach.review.devagent_tools import make_developer_tools


def _script(root: Path, rel: str, body: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _config(**commands: object) -> GatesConfig:
    return GatesConfig.from_mapping(
        {"project": {"languages": ["typescript"]}, "commands": dict(commands)}
    )


def test_lint_exit_1_is_red_with_the_last_line(tmp_path: Path) -> None:
    _script(
        tmp_path, "scripts/lint.sh", 'echo "checking"\necho "src/x.ts:3:1 no-unused-vars"\nexit 1\n'
    )
    passed, tail = run_lint_gate(tmp_path, _config(lint="./scripts/lint.sh"))
    assert passed is False
    assert tail.startswith("$ ./scripts/lint.sh\n")
    assert "src/x.ts:3:1 no-unused-vars" in tail
    assert tail.rstrip().endswith("exit status 1")


def test_lint_exit_0_is_green(tmp_path: Path) -> None:
    _script(tmp_path, "scripts/lint.sh", "echo clean\nexit 0\n")
    passed, tail = run_lint_gate(tmp_path, _config(lint="./scripts/lint.sh"))
    assert passed is True
    assert "clean" in tail


def test_empty_lint_is_a_printed_skip(tmp_path: Path) -> None:
    assert run_lint_gate(tmp_path, _config()) == (True, "skipped: no [commands].lint configured")
    assert run_test_gate(tmp_path, _config()) == (True, "skipped: no [commands].test configured")
    assert run_project_gate(tmp_path, _config()) == (
        True,
        "skipped: no [commands].gate configured",
    )


def test_missing_binary_is_red_with_command_not_found(tmp_path: Path) -> None:
    passed, tail = run_lint_gate(tmp_path, _config(lint="definitely-not-a-binary-xyz --all"))
    assert passed is False
    assert "command not found: definitely-not-a-binary-xyz" in tail
    assert "Traceback" not in tail


def test_gate_command_timeout_is_red(tmp_path: Path) -> None:
    _script(tmp_path, "slow.sh", "echo started\nsleep 5\n")
    passed, tail = run_project_gate(tmp_path, _config(gate="./slow.sh", timeout_s=1))
    assert passed is False
    assert "timeout after 1s" in tail


def _alive(pid: int) -> bool:
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.is_file():
        return False
    fields = stat_file.read_text().rsplit(")", 1)[-1].split()
    return bool(fields) and fields[0] != "Z"


def test_timeout_kills_the_children_of_the_command(tmp_path: Path) -> None:
    _script(tmp_path, "spawn.sh", "sleep 60 &\necho $! > child.pid\nwait\n")
    passed, tail = run_project_gate(tmp_path, _config(gate="./spawn.sh", timeout_s=1))
    assert passed is False and "timeout after 1s" in tail
    child = int((tmp_path / "child.pid").read_text().strip())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)


def test_leading_assignments_become_environment(tmp_path: Path) -> None:
    _script(tmp_path, "ci/run-local-ci.sh", 'echo "wait=$YOOB_CI_WAIT args=$*"\n')
    assert split_command("YOOB_CI_WAIT=1 ./ci/run-local-ci.sh --affected") == (
        {"YOOB_CI_WAIT": "1"},
        ["./ci/run-local-ci.sh", "--affected"],
    )
    passed, tail = run_project_gate(
        tmp_path, _config(gate="YOOB_CI_WAIT=1 ./ci/run-local-ci.sh --affected")
    )
    assert passed is True
    assert "wait=1 args=--affected" in tail


def test_test_command_expansion() -> None:
    assert expand_test_command("pytest -q") == "pytest -q"
    assert expand_test_command("pytest -q", target="tests/a b.py") == "pytest -q 'tests/a b.py'"
    assert expand_test_command("npx vitest run {target}", target="src/x.test.ts") == (
        "npx vitest run src/x.test.ts"
    )
    assert expand_test_command("{python} -m pytest", python="python3.13") == (
        "python3.13 -m pytest"
    )


def test_test_target_starting_with_dash_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not start with '-'"):
        run_test_gate(tmp_path, _config(test="pytest -q"), target="--pdb")


def test_typescript_repo_lint_red_through_the_coder_gate(tmp_path: Path) -> None:
    (tmp_path / "repoach.toml").write_text(
        '[project]\nlanguages = ["typescript"]\n[commands]\nlint = "./lint.sh"\n',
        encoding="utf-8",
    )
    _script(tmp_path, "lint.sh", 'echo "frontend/src/a.ts:1:7 error unused"\nexit 1\n')
    passed, tail = run_ruff_gate(tmp_path)
    assert passed is False
    assert "$ ./lint.sh" in tail and "frontend/src/a.ts:1:7 error unused" in tail


def test_typescript_repo_lint_green_through_the_coder_gate(tmp_path: Path) -> None:
    (tmp_path / "repoach.toml").write_text(
        '[project]\nlanguages = ["typescript"]\n[commands]\nlint = "./lint.sh"\n',
        encoding="utf-8",
    )
    _script(tmp_path, "lint.sh", "exit 0\n")
    assert run_ruff_gate(tmp_path)[0] is True


def test_configured_test_command_replaces_pytest(tmp_path: Path) -> None:
    (tmp_path / "repoach.toml").write_text('[commands]\ntest = "./t.sh"\n', encoding="utf-8")
    _script(tmp_path, "t.sh", "echo 'FAIL src/x.test.ts > adds'\nexit 1\n")
    passed, tail = run_pytest_matrix(tmp_path)
    assert passed is False
    assert "FAIL src/x.test.ts > adds" in tail


def test_python_placeholder_expands_over_the_interpreter_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repoach.toml").write_text(
        '[commands]\ntest = "./t.sh {python}"\n', encoding="utf-8"
    )
    _script(tmp_path, "t.sh", 'echo "ran with $1" >> ran.log\n')
    monkeypatch.setattr(coder_loop, "_pytest_pythons", lambda: ["py-a", "py-b"])
    passed, summary = run_pytest_matrix(tmp_path)
    assert passed is True
    assert summary == "tests green under py-a, py-b"
    assert (tmp_path / "ran.log").read_text(encoding="utf-8").splitlines() == [
        "ran with py-a",
        "ran with py-b",
    ]


def test_builtin_config_runs_todays_ruff_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> tuple[int, str, str]:
        calls.append(argv[1:])
        return 0, "", ""

    monkeypatch.setattr(coder_loop, "_run", fake_run)
    assert load_gates_config(tmp_path).builtin
    assert run_ruff_gate(tmp_path) == (True, "ruff clean")
    assert calls == [
        ["check", "--fix", "src", "tests"],
        ["format", "src", "tests"],
        ["check", "src", "tests"],
        ["format", "--check", "src", "tests"],
    ]


def test_developer_tools_use_the_configured_commands(tmp_path: Path) -> None:
    (tmp_path / "repoach.toml").write_text(
        '[project]\nlanguages = ["typescript"]\n'
        '[commands]\nlint = "./lint.sh"\ntest = "./t.sh {target}"\n',
        encoding="utf-8",
    )
    _script(tmp_path, "lint.sh", "echo lint-ran\nexit 1\n")
    _script(tmp_path, "t.sh", 'echo "target=$1"\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.test.ts").write_text("", encoding="utf-8")
    tools = {tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path)}
    lint_out = tools["run_ruff"]()
    assert lint_out.startswith("FAIL (./lint.sh)") and "lint-ran" in lint_out
    test_out = tools["run_tests"]("src/x.test.ts")
    assert test_out.startswith("PASS (./t.sh {target} src/x.test.ts)")
    assert "target=src/x.test.ts" in test_out
    assert tools["run_tests"]("--pdb").startswith("error:")
