"""Every child pytest a test spawns owns its basetemp (:mod:`tests.child_pytest`).

The invariant: no test under ``tests/`` names pytest as a command except
through :func:`~tests.child_pytest.child_pytest_argv`. The scan reads each
file as an AST. It refuses a pytest *command token*, where one appears:

* in any string constant passed, directly or through a name assigned in
  the same file, to a call that starts a process or splits a command line:
  ``subprocess.*``, ``os.system``, ``os.popen``, ``os.exec*``,
  ``os.spawn*``, ``os.posix_spawn*``, ``asyncio.create_subprocess_*`` and
  ``shlex.split``, whether reached as ``module.func`` or imported by name;
* in a list or tuple literal anywhere, since an argv is built before it is
  passed;
* or in a call of ``runpytest_subprocess``.

A pytest command token is ``pytest``, ``py.test``, a path ending in either,
or ``-mpytest``. It is read joined (one string, split as a shell would) or
split (list elements), each argument's strings as one command. The one exception is a package name after
``install`` (``pip install pytest``), which runs no pytest. The child
would otherwise open a numbered directory in the shared temporary root
and, at exit, delete what other suites left there. The helper and this
file, which holds the shapes as test data, are exempt.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.child_pytest import child_pytest_argv

_TESTS = Path(__file__).resolve().parents[1]
_HELPER = _TESTS / "child_pytest.py"
_SCAN_EXEMPT = frozenset({_HELPER, Path(__file__).resolve()})
"""The helper, which is the one spawn, and this file, which holds the shapes as test data."""

_PYTEST_TOKEN = re.compile(r"(?:-m)?(?:\S*/)?(?:pytest|py\.test)(?:\.exe)?")
"""A token that names pytest as a command or a ``-m`` module."""

_SPAWN_MODULES = {
    "subprocess": None,
    "os": re.compile(r"system|popen|exec\w*|spawn\w*|posix_spawn\w*"),
    "asyncio": re.compile(r"create_subprocess_\w+"),
    "shlex": re.compile(r"split"),
}
"""Module to the functions of it that start a process or split a command line (None: all)."""


def _tokens(text: str) -> list[str]:
    """Split ``text`` as a shell would, or on whitespace when its quotes do not balance.

    Args:
        text: A string constant.

    Returns:
        Its tokens.
    """
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _names_pytest(tokens: list[str]) -> bool:
    """Return whether ``tokens`` hold a pytest command token not installed by name.

    Args:
        tokens: The words of one command, joined or split.

    Returns:
        True when a token matches :data:`_PYTEST_TOKEN` with no ``install`` before it.
    """
    for index, token in enumerate(tokens):
        if _PYTEST_TOKEN.fullmatch(token) and "install" not in tokens[:index]:
            return True
    return False


def _strings(node: ast.AST, assigned: dict[str, list[ast.AST]], seen: set[str]) -> Iterator[str]:
    """Yield every string constant under ``node``, following names assigned in the file.

    Args:
        node: An argument of a call.
        assigned: Name to the values the file assigns it.
        seen: Names already followed, so a cycle ends.

    Yields:
        The string constants, in walk order.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value
        elif isinstance(child, ast.Name) and child.id in assigned and child.id not in seen:
            seen.add(child.id)
            for value in assigned[child.id]:
                yield from _strings(value, assigned, seen)


def _spawn_targets(tree: ast.AST) -> dict[str, re.Pattern[str] | None]:
    """Return the bare names a file imports from the spawning modules.

    Args:
        tree: A parsed module.

    Returns:
        Local name to the pattern its module's spawning functions match.
    """
    targets: dict[str, re.Pattern[str] | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SPAWN_MODULES:
            pattern = _SPAWN_MODULES[node.module]
            for alias in node.names:
                if pattern is None or pattern.fullmatch(alias.name):
                    targets[alias.asname or alias.name] = pattern
    return targets


def _is_spawn(func: ast.expr, targets: dict[str, re.Pattern[str] | None]) -> bool:
    """Return whether a call's function starts a process or splits a command line.

    Args:
        func: The call's ``func``.
        targets: :func:`_spawn_targets` of the file.

    Returns:
        True for ``module.func`` of :data:`_SPAWN_MODULES` and for a name the file imported from one.
    """
    if isinstance(func, ast.Name):
        return func.id in targets
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id not in _SPAWN_MODULES:
            return False
        pattern = _SPAWN_MODULES[func.value.id]
        return pattern is None or bool(pattern.fullmatch(func.attr))
    return False


def _spawns(tree: ast.AST) -> list[int]:
    """Return the line of every place ``tree`` names pytest as a command outside the helper.

    Args:
        tree: A parsed module.

    Returns:
        The line numbers, sorted and unique.
    """
    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
    spawning = _spawn_targets(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            elements = [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if _names_pytest(elements):
                lines.add(node.lineno)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "runpytest_subprocess":
                lines.add(node.lineno)
            elif _is_spawn(node.func, spawning):
                arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                if any(
                    _names_pytest(
                        [
                            token
                            for text in _strings(argument, assigned, set())
                            for token in _tokens(text)
                        ]
                    )
                    for argument in arguments
                ):
                    lines.add(node.lineno)
    return sorted(lines)


def test_no_test_spawns_a_child_pytest_without_the_helper() -> None:
    found = [
        f"{path.relative_to(_TESTS.parent)}:{line}"
        for path in sorted(_TESTS.rglob("*.py"))
        if path not in _SCAN_EXEMPT
        for line in _spawns(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    ]
    assert found == [], (
        f"spawn the child through tests.child_pytest.child_pytest_argv, so it owns its "
        f"--basetemp: {found}"
    )


def test_the_helper_itself_is_the_one_spawn() -> None:
    assert _spawns(ast.parse(_HELPER.read_text(encoding="utf-8"))) != []


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run([sys.executable, "-m", "pytest", "-q", "x"])',
        'subprocess.run((python, "-m", "pytest"))',
        'subprocess.run(["pytest", "tests"])',
        'subprocess.run([sys.executable, "-mpytest", "-q"])',
        'subprocess.run(["/venv/bin/pytest", "-q"])',
        'os.system("python -m pytest -q tests")',
        'os.popen(f"{sys.executable} -m pytest -q")',
        'subprocess.run("cd x && pytest -q", shell=True)',
        'subprocess.check_output(shlex.split("python -m pytest -q"))',
        'shlex.split("py.test -q")',
        'from subprocess import run\nrun(cmd)\ncmd = "pytest -q"',
        'import os\ncmd = "python3 -m pytest"\nos.system(cmd)',
        'asyncio.create_subprocess_shell("pytest -q")',
        'os.execv(sys.executable, [sys.executable] + args)\nargs = "-m pytest".split()',
        "pytester.runpytest_subprocess('-q')",
    ],
)
def test_the_scan_refuses_each_spawn_shape(source: str) -> None:
    assert _spawns(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["git", "-m", "msg"])',
        'subprocess.run([sys.executable, "-m", "pip", "install", "pytest"])',
        'os.system("pip install -U pytest pytest-xdist")',
        'cmd = "pytest -q"',
        'subprocess.run(["ls", "/tmp/pytest-of-me"])',
        'subprocess.run(child_pytest_argv("-q", "tests/x.py", basetemp=p))',
        'subprocess.run(["git", "log"], check=False)',
        'assert "pytest" in reason',
    ],
)
def test_the_scan_admits_what_spawns_no_pytest(source: str) -> None:
    assert _spawns(ast.parse(source)) == []


def test_child_pytest_argv_appends_an_absolute_basetemp(tmp_path: Path) -> None:
    argv = child_pytest_argv("-q", "tests/x.py", basetemp=tmp_path / "bt")
    assert argv[1:5] == ["-m", "pytest", "-q", "tests/x.py"]
    assert argv[-1] == f"--basetemp={(tmp_path / 'bt').resolve()}"


@pytest.mark.parametrize("arg", ["--basetemp", "--basetemp=/tmp/x"])
def test_child_pytest_argv_refuses_a_second_basetemp(tmp_path: Path, arg: str) -> None:
    with pytest.raises(ValueError, match="pass the basetemp as basetemp="):
        child_pytest_argv("-q", arg, basetemp=tmp_path)
