"""Unit tests for SP-PIN-TOOLCHAIN G2/G3 — gates spawn through the running interpreter."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoach.gates import commands
from repoach.gates.commands import (
    CommandResult,
    missing_module_tail,
    pytest_argv,
    ruff_argv,
    run_project_gate,
)
from repoach.gates.config import GatesConfig
from repoach.gates.selectors import parse_selector, run_selector
from repoach.review import coder_loop, devagent_tools
from repoach.review.coder_loop import (
    run_pytest,
    run_pytest_matrix,
    run_ruff_gate,
)
from repoach.review.devagent_tools import make_developer_tools

_PYTEST_MODULE = "pytest"


def _record(
    monkeypatch: pytest.MonkeyPatch,
    rc: int = 0,
    out: str = "",
    err: str = "",
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd, timeout_s=300, env=None):
        calls.append(list(argv))
        return rc, out, err

    monkeypatch.setattr(coder_loop, "_run", fake_run)
    return calls


def _make_repoach_checkout(
    root: Path, *, lint_cmd: str | None = None, test_cmd: str | None = None
) -> None:
    (root / "pyproject.toml").write_text('[project]\nname = "repoach"\n', encoding="utf-8")
    lint_val = (
        lint_cmd
        if lint_cmd is not None
        else (
            "ruff check src tests && ruff format --check src tests && "
            "python scripts/lint_no_inline_comments.py --summary && "
            "python scripts/lint_no_silent_except.py --summary"
        )
    )
    test_val = test_cmd if test_cmd is not None else "pytest -q tests/unit"
    (root / "repoach.toml").write_text(
        f'[commands]\nlint = "{lint_val}"\ntest = "{test_val}"\n'
        'gate = "python scripts/check.py"\n'
        'custom = "ruff check src"\n',
        encoding="utf-8",
    )


def _record_gate_argv(
    monkeypatch: pytest.MonkeyPatch,
    *,
    passed: bool = True,
    returncode: int = 0,
    tail: str = "",
) -> list[tuple[list[str], str | None]]:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run_argv(argv, *, cwd, timeout_s, label=None, missing_module=None):
        calls.append((list(argv), missing_module))
        return CommandResult(passed, returncode, tail, "")

    monkeypatch.setattr(commands, "run_argv", fake_run_argv)
    return calls


def test_argv_helpers_use_sys_executable() -> None:
    assert ruff_argv("check", "src") == [sys.executable, "-m", "ruff", "check", "src"]
    assert pytest_argv("-q") == [sys.executable, "-m", _PYTEST_MODULE, "-q"]
    assert pytest_argv("-q", python="python3.13") == ["python3.13", "-m", _PYTEST_MODULE, "-q"]


def test_ruff_gate_spawns_four_argv_through_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    calls = _record(monkeypatch)
    ok, _ = run_ruff_gate(tmp_path)
    assert ok is True
    assert calls == [
        [sys.executable, "-m", "ruff", "check", "--fix", "src", "tests"],
        [sys.executable, "-m", "ruff", "format", "src", "tests"],
        [sys.executable, "-m", "ruff", "check", "src", "tests"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "tests"],
    ]


def test_pytest_gate_spawns_through_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    calls = _record(monkeypatch)
    ok, _ = run_pytest(tmp_path)
    assert ok is True
    assert calls == [[sys.executable, "-m", _PYTEST_MODULE, "-q", "tests/unit"]]


def test_pytest_gate_keeps_an_explicit_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record(monkeypatch)
    run_pytest(tmp_path, python="python3.13")
    assert calls == [["python3.13", "-m", _PYTEST_MODULE, "-q", "tests/unit"]]


def test_missing_ruff_is_named_not_command_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(monkeypatch, rc=1, err="/x/python: No module named ruff")
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is False
    assert tail == f"ruff is not installed in {sys.executable}"


def test_missing_ruff_on_format_check_step_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = 0

    def fake_run(argv, *, cwd, timeout_s=300, env=None):
        nonlocal step
        step += 1
        if step == 4:
            return 1, "", "ModuleNotFoundError: No module named 'ruff'"
        return 0, "", ""

    monkeypatch.setattr(coder_loop, "_run", fake_run)
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is False
    assert tail == f"ruff is not installed in {sys.executable}"


def test_missing_pytest_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, rc=1, err="/x/python: No module named pytest")
    ok, tail = run_pytest(tmp_path)
    assert ok is False
    assert tail == f"pytest is not installed in {sys.executable}"


def test_missing_pytest_names_explicit_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(monkeypatch, rc=1, err="/custom/python3.13: No module named pytest")
    ok, tail = run_pytest(tmp_path, python="/custom/python3.13")
    assert ok is False
    assert tail == "pytest is not installed in /custom/python3.13"


def test_missing_module_tail_ignores_rc_zero_even_with_matching_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        missing_module_tail(0, "pytest", sys.executable, "/x/python: No module named pytest")
        is None
    )
    _record(
        monkeypatch,
        rc=0,
        out="1 passed (tested 'No module named pytest' handling)",
        err="/x/python: No module named pytest",
    )
    ok, tail = run_pytest(tmp_path)
    assert ok is True
    assert "not installed" not in tail
    _record(
        monkeypatch,
        rc=0,
        out="All checks passed! No module named ruff",
        err="/x/python: No module named ruff",
    )
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is True
    assert tail == "ruff clean"


def test_pytest_traceback_mentioning_missing_module_is_not_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traceback_out = (
        "FAILED tests/unit/test_toolchain_spawn.py::test_missing_pytest_is_named\n"
        "E   ModuleNotFoundError: No module named 'pytest'\n"
        "AssertionError: assert False\n"
    )
    _record(monkeypatch, rc=1, out=traceback_out, err="")
    ok, tail = run_pytest(tmp_path)
    assert ok is False
    assert "AssertionError: assert False" in tail
    assert "not installed" not in tail


def test_ruff_lint_output_mentioning_missing_module_is_not_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint_out = "src/bad.py:1:30: F821 Undefined name `x`\n/x/python: No module named ruff\n"
    _record(monkeypatch, rc=1, out=lint_out, err="")
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is False
    assert tail.startswith("ruff check failed:\n")
    assert "F821" in tail
    assert "not installed" not in tail


def test_missing_module_tail_ignores_unrelated_stderr_lines() -> None:
    assert (
        missing_module_tail(
            1, "pytest", sys.executable, "AssertionError: '/x/python: No module named pytest'\n"
        )
        is None
    )
    assert (
        missing_module_tail(
            1, "pytest", sys.executable, "ModuleNotFoundError: No module named 'pytest_extra'\n"
        )
        is None
    )


def test_developer_run_tests_spawns_through_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    seen: list[list[str]] = []

    def fake_subprocess_run(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(devagent_tools.subprocess, "run", fake_subprocess_run)
    tools = {tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path)}
    tools["run_tests"]("tests/unit")
    assert seen == [[sys.executable, "-m", _PYTEST_MODULE, "-q", "--", "tests/unit"]]


def test_developer_run_tests_names_missing_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)

    def fake_subprocess_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr=f"{sys.executable}: No module named pytest\n"
        )

    monkeypatch.setattr(devagent_tools.subprocess, "run", fake_subprocess_run)
    tools = {tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path)}
    out = tools["run_tests"]("tests/unit")
    assert out == f"FAIL (pytest tests/unit)\npytest is not installed in {sys.executable}"


def test_repoach_configured_gates_resolve_toolchain_to_sys_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    _make_repoach_checkout(tmp_path)
    calls = _record_gate_argv(monkeypatch)

    ok_lint, _ = run_ruff_gate(tmp_path)
    assert ok_lint is True
    assert calls == [
        ([sys.executable, "-m", "ruff", "check", "src", "tests"], "ruff"),
        ([sys.executable, "-m", "ruff", "format", "--check", "src", "tests"], "ruff"),
        ([sys.executable, "scripts/lint_no_inline_comments.py", "--summary"], None),
        ([sys.executable, "scripts/lint_no_silent_except.py", "--summary"], None),
    ]

    calls.clear()
    ok_test, _ = run_pytest_matrix(tmp_path)
    assert ok_test is True
    assert calls == [([sys.executable, "-m", _PYTEST_MODULE, "-q", "tests/unit"], _PYTEST_MODULE)]

    calls.clear()
    tools = {tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path)}
    assert tools["run_ruff"]().startswith("PASS")
    assert tools["run_tests"]("tests/unit").startswith("PASS")
    assert [argv for argv, _ in calls] == [
        [sys.executable, "-m", "ruff", "check", "src", "tests"],
        [sys.executable, "-m", "ruff", "format", "--check", "src", "tests"],
        [sys.executable, "scripts/lint_no_inline_comments.py", "--summary"],
        [sys.executable, "scripts/lint_no_silent_except.py", "--summary"],
        [sys.executable, "-m", _PYTEST_MODULE, "-q", "tests/unit"],
    ]

    calls.clear()
    cfg = GatesConfig.from_mapping(
        {
            "commands": {
                "gate": "python scripts/check.py",
                "custom": "ruff check src",
            }
        }
    )
    assert run_project_gate(tmp_path, cfg)[0] is True
    selector = parse_selector("cmd:custom")
    assert selector is not None
    assert run_selector(tmp_path, selector, cfg)[0] is True
    assert calls == [
        ([sys.executable, "scripts/check.py"], None),
        ([sys.executable, "-m", "ruff", "check", "src"], "ruff"),
    ]


def test_repoach_configured_missing_ruff_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repoach_checkout(tmp_path)

    class _FakePopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            self.argv = argv

        def communicate(self, timeout=None) -> tuple[str, str]:
            return "", f"{self.argv[0]}: No module named ruff\n"

    monkeypatch.setattr(commands.subprocess, "Popen", _FakePopen)
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is False
    assert tail == f"ruff is not installed in {sys.executable}"


def test_repoach_configured_matrix_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repoach_checkout(tmp_path, test_cmd="{python} -m pytest -q tests/unit")
    calls = _record_gate_argv(monkeypatch)
    monkeypatch.setattr(coder_loop, "_pytest_pythons", lambda: [None, "python3.13"])
    ok, _ = run_pytest_matrix(tmp_path)
    assert ok is True
    assert calls == [
        ([sys.executable, "-m", _PYTEST_MODULE, "-q", "tests/unit"], _PYTEST_MODULE),
        (["python3.13", "-m", _PYTEST_MODULE, "-q", "tests/unit"], _PYTEST_MODULE),
    ]


def test_repoach_configured_missing_pytest_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repoach_checkout(tmp_path)

    class _FakePopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            self.argv = argv

        def communicate(self, timeout=None) -> tuple[str, str]:
            return "", f"{self.argv[0]}: No module named pytest\n"

    monkeypatch.setattr(commands.subprocess, "Popen", _FakePopen)
    ok, tail = run_pytest_matrix(tmp_path)
    assert ok is False
    assert tail == f"pytest is not installed in {sys.executable}"


def test_foreign_repo_configured_commands_stay_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "other-project"\n', encoding="utf-8"
    )
    (tmp_path / "repoach.toml").write_text(
        '[commands]\nlint = "ruff check src"\ntest = "pytest -q"\n',
        encoding="utf-8",
    )
    calls = _record_gate_argv(monkeypatch)
    assert run_ruff_gate(tmp_path)[0] is True
    assert run_pytest_matrix(tmp_path)[0] is True
    assert calls == [
        (["ruff", "check", "src"], None),
        ([_PYTEST_MODULE, "-q"], None),
    ]


def test_configured_gate_ignores_missing_module_line_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repoach_checkout(tmp_path)

    class _FakePopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            self.argv = argv

        def communicate(self, timeout=None) -> tuple[str, str]:
            return "E   ModuleNotFoundError: No module named 'pytest'\n", ""

    monkeypatch.setattr(commands.subprocess, "Popen", _FakePopen)
    ok, tail = run_pytest_matrix(tmp_path)
    assert ok is False
    assert "not installed" not in tail
    assert "No module named 'pytest'" in tail


def test_developer_run_tests_ignores_missing_module_line_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)

    def fake_subprocess_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="E   ModuleNotFoundError: No module named 'pytest'\n",
            stderr="",
        )

    monkeypatch.setattr(devagent_tools.subprocess, "run", fake_subprocess_run)
    tools = {tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path)}
    out = tools["run_tests"]("tests/unit")
    assert out.startswith("FAIL (pytest tests/unit)")
    assert "not installed" not in out


def test_run_argv_sets_the_typed_missing_module_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakePopen:
        returncode = 1

        def __init__(self, argv, **kwargs) -> None:
            self.argv = argv

        def communicate(self, timeout=None) -> tuple[str, str]:
            return "", f"{self.argv[0]}: No module named ruff\n"

    monkeypatch.setattr(commands.subprocess, "Popen", _FakePopen)
    named = commands.run_argv(
        [sys.executable, "-m", "ruff"], cwd=tmp_path, timeout_s=5, missing_module="ruff"
    )
    other = commands.run_argv(
        [sys.executable, "-m", "ruff"], cwd=tmp_path, timeout_s=5, missing_module="pytest"
    )
    assert named.missing_module is True
    assert other.missing_module is False


def test_run_command_stops_on_the_typed_flag_not_on_tail_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_repoach_checkout(tmp_path)
    results = iter([CommandResult(False, 1, "reworded tail", "", True)])
    calls: list[list[str]] = []

    def fake_run_argv(argv, **kwargs):
        calls.append(list(argv))
        return next(results)

    monkeypatch.setattr(commands, "run_argv", fake_run_argv)
    result = commands.run_command("ruff check && ruff format", root=tmp_path, timeout_s=5)
    assert result.missing_module is True
    assert result.tail == "reworded tail"
    assert len(calls) == 1
