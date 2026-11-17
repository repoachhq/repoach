"""Every child pytest a test spawns owns its basetemp (:mod:`tests.child_pytest`).

The scan reads every ``.py`` file under ``tests/`` as an AST and refuses
three spawn shapes outside :mod:`tests.child_pytest` and this file:

* a list or tuple literal holding ``"-m", "pytest"`` side by side;
* a list or tuple literal whose first element is ``"pytest"``, the console script;
* a call of ``runpytest_subprocess``.

In each case the child would open a numbered directory in the shared
temporary root and, at exit, delete what other suites left there.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from tests.child_pytest import child_pytest_argv

_TESTS = Path(__file__).resolve().parents[1]
_HELPER = _TESTS / "child_pytest.py"
_SCAN_EXEMPT = frozenset({_HELPER, Path(__file__).resolve()})
"""The helper, which is the one spawn, and this file, which holds the shapes as test data."""


def _spawns(tree: ast.AST) -> list[int]:
    """Return the line of every child-pytest spawn shape in ``tree``.

    Args:
        tree: A parsed module.

    Returns:
        The line numbers, in walk order.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [item.value if isinstance(item, ast.Constant) else None for item in node.elts]
            if values[:1] == ["pytest"] or any(
                pair == ("-m", "pytest") for pair in itertools.pairwise(values)
            ):
                lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "runpytest_subprocess"
        ):
            lines.append(node.lineno)
    return lines


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
        "pytester.runpytest_subprocess('-q')",
    ],
)
def test_the_scan_refuses_each_spawn_shape(source: str) -> None:
    assert _spawns(ast.parse(source)) == [1]


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["git", "-m", "msg"])',
        'subprocess.run([sys.executable, "-m", "pip", "install", "pytest"])',
        'cmd = "pytest -q"',
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
