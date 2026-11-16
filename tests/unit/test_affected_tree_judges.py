"""The tree judges and ``[gate].pytest`` (SP-AFFECTED-JOBS-TABLE D5, T8).

A tree judge is a module under ``tests/`` that reads a repository path.
The walk evaluates, with :mod:`ast`, every expression built from
``__file__`` (``Path(__file__)``, ``.resolve()``, ``.parent``,
``.parents[n]``, ``/``, ``.joinpath``, ``.with_name``, ``os.path.dirname``,
``os.path.join``, ``str``) and every ``Path("<literal>")`` whose first
segment is a tracked top-level entry, to a path relative to the root.
A part it cannot evaluate (a variable, a call) ends the path where it
stands: the module is taken to read the whole tree under what was
resolved, the root included. A path above the root is refused by name.

The invariant judged: a change to a path a judge reads must run that
judge. A ``test_*`` module runs in the job whose ``pytest`` step names
its directory; when a file under one of its reads selects no such job,
only ``gate`` sees the change, so the module must be in
``[gate].pytest``, and every ``[gate].pytest`` entry must be such a
module. A helper module (not ``test_*``) cannot be named there, so it
must read nothing of that class. Every path read must be classified.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from repoach.affected.select import DEFAULT_WORKFLOW_PATH, select, workflow_jobs
from repoach.affected.table import (
    DEFAULT_TABLE_PATH,
    UNKNOWN_ROW,
    AffectedTable,
    load_table,
)
from repoach.review.merge_wait import load_workflow

_ROOT = Path(__file__).resolve().parents[2]
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_PATH_CONSTRUCTORS = frozenset({"Path", "PurePath", "PurePosixPath"})
_IDENTITY_METHODS = frozenset({"resolve", "absolute", "expanduser"})
_OS_PATH_IDENTITY = frozenset({"abspath", "realpath", "normpath"})

Parts = tuple[str, ...]


class WalkerError(ValueError):
    """A test module builds a path the walk refuses (one above the root)."""


@dataclass(frozen=True)
class _Module:
    """What the walk knows of one module.

    Attributes:
        name: The module's repository path, for errors.
        parts: Its path split into segments (``__file__``).
        top_entries: The first segments of every tracked path.
        constants: Module-level names bound to a string literal.
        resource_names: Names the module imports from ``importlib.resources``
            (or ``resources`` itself, from ``importlib``).
    """

    name: str
    parts: Parts
    top_entries: frozenset[str]
    constants: Mapping[str, str] = field(default_factory=dict)
    resource_names: frozenset[str] = frozenset()


def _join(module: _Module, node: ast.AST, base: Parts, text: str) -> Parts:
    """Join a literal relative path onto ``base``.

    Args:
        module: The module, for errors.
        node: The expression, for the line in errors.
        base: The parts joined onto.
        text: The literal, ``/``-separated, ``..`` allowed.

    Returns:
        The joined parts.

    Raises:
        WalkerError: The result climbs above the repository root.
    """
    parts = list(base)
    for segment in text.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            parts = list(_parent(module, node, tuple(parts)))
        else:
            parts.append(segment)
    return tuple(parts)


def _parent(module: _Module, node: ast.AST, parts: Parts) -> Parts:
    """Return the parent of ``parts``.

    Raises:
        WalkerError: ``parts`` is the root.
    """
    if not parts:
        raise WalkerError(
            f"{module.name}:{getattr(node, 'lineno', '?')}: a path climbs above the repository root"
        )
    return parts[:-1]


def _literal(node: ast.AST, module: _Module) -> str | None:
    """Return the string ``node`` denotes: a literal or a module string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in module.constants:
        return module.constants[node.id]
    return None


def _os_path(func: ast.AST) -> str | None:
    """Return ``x`` for a call target ``os.path.x``, else None."""
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "path"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
    ):
        return func.attr
    return None


def _join_all(module: _Module, node: ast.AST, base: Parts, args: list[ast.expr]) -> Parts:
    """Join every literal argument onto ``base``, stopping at the first that is not one."""
    for arg in args:
        text = _literal(arg, module)
        if text is None:
            return base
        base = _join(module, node, base, text)
    return base


def _evaluate(node: ast.AST, env: Mapping[str, Parts], module: _Module) -> Parts | None:
    """Return the repository path ``node`` denotes, or None when it denotes none.

    Args:
        node: An expression.
        env: Names of the scope bound to a repository path.
        module: The module.

    Returns:
        The path as parts (``()`` is the root). An operand that is not
        a literal ends the path where it stands.

    Raises:
        WalkerError: The path climbs above the root.
    """
    if isinstance(node, ast.Call | ast.Attribute | ast.BinOp | ast.Subscript) and _from_resources(
        node, module
    ):
        return ()
    if isinstance(node, ast.Name):
        return module.parts if node.id == "__file__" else env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _evaluate(node.value, env, module)
        return None if base is None else _parent(module, node, base)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        base = _evaluate(node.value.value, env, module)
        if base is None:
            return None
        for _ in range(node.slice.value + 1):
            base = _parent(module, node, base)
        return base
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _evaluate(node.left, env, module)
        return None if base is None else _join_all(module, node, base, [node.right])
    if isinstance(node, ast.Call):
        return _evaluate_call(node, env, module)
    return None


def _is_cwd(func: ast.AST) -> bool:
    """Return whether a call target is ``Path.cwd`` or ``os.getcwd``: the root, where pytest runs."""
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "cwd":
        return (isinstance(func.value, ast.Name) and func.value.id in _PATH_CONSTRUCTORS) or (
            isinstance(func.value, ast.Attribute) and func.value.attr in _PATH_CONSTRUCTORS
        )
    return func.attr == "getcwd" and isinstance(func.value, ast.Name) and func.value.id == "os"


def _is_resources(func: ast.AST, module: _Module) -> bool:
    """Return whether a call goes through ``importlib.resources``, read as the whole tree."""
    if isinstance(func, ast.Name):
        return func.id in module.resource_names
    while isinstance(func, ast.Attribute):
        if func.attr == "resources" or (
            isinstance(func.value, ast.Name) and func.value.id in module.resource_names
        ):
            return True
        func = func.value
    return False


def _from_resources(node: ast.AST, module: _Module) -> bool:
    """Return whether ``node`` is, or is built on, an ``importlib.resources`` call.

    A resource path is relative to a package, not to the root, so
    everything built on it stays the whole tree.
    """
    while True:
        if isinstance(node, ast.Call):
            if _is_resources(node.func, module):
                return True
            node = node.func
        elif isinstance(node, ast.Attribute | ast.Subscript):
            node = node.value
        elif isinstance(node, ast.BinOp):
            node = node.left
        else:
            return False


def _builds_a_string(node: ast.AST) -> bool:
    """Return whether ``node`` computes a string the walk cannot read (f-string, ``+``, ``%``, ``format``, ``join``)."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "join"}
        and _os_path(node.func) is None
    )


def _evaluate_call(node: ast.Call, env: Mapping[str, Parts], module: _Module) -> Parts | None:
    """Return the repository path a call denotes, or None (see :func:`_evaluate`)."""
    func = node.func
    if _is_cwd(func):
        return ()
    opener = isinstance(func, ast.Name) and func.id in _PATH_CONSTRUCTORS | {"open"}
    if opener and node.args:
        arg = node.args[0]
        text = _literal(arg, module)
        if text is not None:
            first = text.split("/")[0]
            if not text.startswith("/") and first in module.top_entries:
                return _join(module, node, (), text)
            return None
        if _builds_a_string(arg):
            return ()
        return _evaluate(arg, env, module)
    if isinstance(func, ast.Name) and func.id == "str" and len(node.args) == 1:
        return _evaluate(node.args[0], env, module)
    os_function = _os_path(func)
    if os_function is not None and node.args:
        base = _evaluate(node.args[0], env, module)
        if base is None:
            return None
        if os_function in _OS_PATH_IDENTITY:
            return base
        if os_function == "dirname":
            return _parent(module, node, base)
        if os_function == "join":
            return _join_all(module, node, base, list(node.args[1:]))
        return None
    if isinstance(func, ast.Attribute):
        base = _evaluate(func.value, env, module)
        if base is None:
            return None
        if func.attr in _IDENTITY_METHODS:
            return base
        if func.attr == "joinpath":
            return _join_all(module, node, base, list(node.args))
        if func.attr == "with_name" and node.args:
            return _join_all(module, node, _parent(module, node, base), [node.args[0]])
    return None


def _consumed(node: ast.AST) -> list[ast.AST]:
    """Return the children of a path expression that are steps of it, not reads."""
    if isinstance(node, ast.Attribute):
        return [node.value]
    if isinstance(node, ast.Subscript):
        return [node.value, node.value.value] if isinstance(node.value, ast.Attribute) else []
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if isinstance(node, ast.Call):
        children: list[ast.AST] = [node.func, *node.args]
        if isinstance(node.func, ast.Attribute):
            children.append(node.func.value)
        return children
    return []


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield the nodes of a scope, nested function scopes excluded (they are yielded, not entered)."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        pending.extend(ast.iter_child_nodes(node))


def _assigned(nodes: list[ast.AST]) -> dict[str, list[ast.expr]]:
    """Return each name assigned in the scope with the values bound to it."""
    assigned: dict[str, list[ast.expr]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assigned.setdefault(node.target.id, []).append(node.value)
    return assigned


def _bindings(
    assigned: Mapping[str, list[ast.expr]], inherited: Mapping[str, Parts], module: _Module
) -> dict[str, Parts]:
    """Return the names of one scope bound to a repository path, to a fixpoint.

    A name assigned in the scope to anything else shadows an inherited binding.
    """
    env = {name: parts for name, parts in inherited.items() if name not in assigned}
    changed = True
    while changed:
        changed = False
        for name, values in assigned.items():
            if name in env:
                continue
            for value in values:
                parts = _evaluate(value, env, module)
                if parts is not None:
                    env[name] = parts
                    changed = True
                    break
    return env


def _scope_reads(body: list[ast.stmt], inherited: Mapping[str, Parts], module: _Module) -> set[str]:
    """Return the repository paths one scope and its nested scopes read.

    Raises:
        WalkerError: A path climbs above the root.
    """
    nodes = list(_own_nodes(body))
    assigned = _assigned(nodes)
    env = _bindings(assigned, inherited, module)
    bound_values = {id(value) for values in assigned.values() for value in values}
    evaluated: dict[int, Parts] = {}
    consumed: set[int] = set()
    reads: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            reads |= _scope_reads(node.body, env, module)
        elif isinstance(node, ast.Lambda):
            reads |= _scope_reads([ast.Expr(node.body)], env, module)
        elif isinstance(node, ast.expr) and not isinstance(getattr(node, "ctx", None), ast.Store):
            parts = _evaluate(node, env, module)
            if parts is not None:
                evaluated[id(node)] = parts
                consumed.update(id(child) for child in _consumed(node))
    for node_id, parts in evaluated.items():
        if node_id not in consumed and node_id not in bound_values:
            reads.add("/".join(parts))
    return reads


def tree_reads(source: str, module_path: str, top_entries: frozenset[str]) -> set[str]:
    """Return the repository paths a module under ``tests/`` reads.

    Args:
        source: The module's source.
        module_path: Its repository path (``tests/unit/test_x.py``).
        top_entries: The first segments of every tracked path.

    Returns:
        The paths, relative to the root (``""`` is the whole tree); the
        module's own file excluded; empty for a module reading nothing.

    Raises:
        SyntaxError: ``source`` is not Python.
        WalkerError: A path climbs above the repository root.
    """
    tree = ast.parse(source)
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    resource_names = frozenset(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module == "importlib.resources" or node.module == "importlib")
        for alias in node.names
        if node.module == "importlib.resources" or alias.name == "resources"
    )
    module = _Module(
        module_path, tuple(module_path.split("/")), top_entries, constants, resource_names
    )
    return _scope_reads(tree.body, {}, module) - {module_path}


def _tracked() -> list[str]:
    """Return ``git ls-files`` of the checkout."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [path for path in listing.split("\0") if path]


def _top_entries(tracked: list[str]) -> frozenset[str]:
    """Return the first segment of every tracked path."""
    return frozenset(path.split("/")[0] for path in tracked)


def _table() -> tuple[AffectedTable, dict[str, frozenset[str]]]:
    """Return the committed table and the workflow's needs."""
    workflow = _ROOT / DEFAULT_WORKFLOW_PATH
    needs = workflow_jobs(workflow)
    return load_table(_ROOT / DEFAULT_TABLE_PATH, needs, workflow=workflow.name), needs


def _suite_jobs(test_dir: str | None) -> frozenset[str]:
    """Return the jobs whose ``pytest`` step names ``test_dir``, or any test directory.

    Args:
        test_dir: A test directory (``tests/unit``); None for a helper
            module, run by every job that runs a test directory.

    Returns:
        The ids of the jobs that run the module.
    """
    jobs = load_workflow(_ROOT / DEFAULT_WORKFLOW_PATH)["jobs"]
    found: set[str] = set()
    for job_id, job in jobs.items():
        for step in job.get("steps", []):
            words = str(step.get("run", "")).split()
            if "pytest" not in words:
                continue
            if test_dir in words or (
                test_dir is None and any(w.startswith("tests/") for w in words)
            ):
                found.add(job_id)
    return frozenset(found)


def _judges() -> dict[str, set[str]]:
    """Return every module under ``tests/`` that reads the tree, with its reads."""
    tracked = _tracked()
    top_entries = _top_entries(tracked)
    judges: dict[str, set[str]] = {}
    for path in tracked:
        if path.startswith("tests/") and path.endswith(".py"):
            reads = tree_reads((_ROOT / path).read_text(encoding="utf-8"), path, top_entries)
            if reads:
                judges[path] = reads
    return judges


def _is_test_module(path: str) -> bool:
    """Return whether pytest collects ``path`` (``test_*.py``)."""
    return PurePosixPath(path).name.startswith("test_")


def _files_under(read: str, tracked: list[str]) -> list[str]:
    """Return the paths a read covers: the tracked files equal to or under it, else itself.

    Args:
        read: A path a judge reads (``""`` is the whole tree).
        tracked: ``git ls-files``.

    Returns:
        The tracked files under ``read``; ``[read]`` when none is, so an
        untracked path is still classified.
    """
    if not read:
        return tracked
    covered = [path for path in tracked if path == read or path.startswith(f"{read}/")]
    return covered or [read]


def _gate_only_reads() -> dict[str, list[str]]:
    """Return each judge with the reads whose change skips the jobs that run it."""
    table, needs = _table()
    tracked = _tracked()
    verdicts: dict[str, frozenset[str]] = {}
    found: dict[str, list[str]] = {}
    for judge, reads in _judges().items():
        test_dir = str(PurePosixPath(judge).parent) if _is_test_module(judge) else None
        suite = _suite_jobs(test_dir)
        for read in sorted(reads):
            for path in _files_under(read, tracked):
                if path not in verdicts:
                    verdicts[path] = select([path], table, needs, mode="narrowed", reason="").jobs
                if not verdicts[path] & suite:
                    found.setdefault(judge, []).append(read)
                    break
    return found


def _reads_of(relative: str) -> set[str]:
    tracked = _tracked()
    source = (_ROOT / relative).read_text(encoding="utf-8")
    return tree_reads(source, relative, _top_entries(tracked))


def _synthetic(lines: list[str], module: str = "tests/unit/test_x.py") -> set[str]:
    return tree_reads(
        "\n".join(lines), module, frozenset({"README.md", "docs", "scripts", "src", "tests"})
    )


def test_walk_finds_both_templates_of_pr_template_measured() -> None:
    assert _reads_of("tests/unit/test_pr_template_measured.py") == {
        "docs/PR_TEMPLATE.md",
        ".github/pull_request_template.md",
    }


def test_walk_finds_a_root_file_literal() -> None:
    assert _reads_of("tests/unit/test_claude_md_integration_flag.py") == {"CLAUDE.md"}


def test_walk_follows_parent_to_the_comparison_pages() -> None:
    assert _reads_of("tests/unit/test_gen_comparison.py") == {
        "docs/comparison.md",
        "docs/comparison.yaml",
        "scripts",
        "scripts/gen_comparison.py",
    }


def test_walk_finds_license_read_by_packaging_metadata() -> None:
    assert _reads_of("tests/unit/test_packaging_metadata.py") == {"LICENSE", "pyproject.toml"}


def test_comparison_and_packaging_readers_are_in_the_gate() -> None:
    table, _ = _table()
    assert "tests/unit/test_gen_comparison.py" in table.gate_pytest
    assert "tests/unit/test_packaging_metadata.py" in table.gate_pytest


def test_walk_follows_bindings_scopes_and_idioms() -> None:
    assert _synthetic(
        [
            "import os",
            "from pathlib import Path",
            "ROOT = Path(__file__).resolve().parents[2]",
            "SPECS = ROOT / 'docs' / 'specs'",
            "NAME = 'x.md'",
            "def a():",
            "    return (SPECS / NAME).read_text()",
            "def b(tmp_path):",
            "    ROOT = tmp_path",
            "    return ROOT / 'docs'",
            "def c(name):",
            "    return Path(__file__).parent.parent.parent / 'scripts' / name",
            "def d():",
            "    return Path('README.md'), Path('docs/fake.md'), Path('/abs/x'), Path('elsewhere/x')",
            "def e():",
            "    here = os.path.dirname(os.path.abspath(__file__))",
            "    return os.path.join(here, '..', '..', 'src', 'a.py')",
            "def f():",
            "    return Path(__file__).with_name('fixture.json'), ROOT.joinpath('docs', 'x.md')",
        ]
    ) == {
        "docs/specs/x.md",
        "scripts",
        "README.md",
        "docs/fake.md",
        "src/a.py",
        "tests/unit/fixture.json",
        "docs/x.md",
    }


def test_walk_takes_an_unresolved_root_use_as_the_whole_tree() -> None:
    assert _synthetic(
        [
            "import subprocess",
            "from pathlib import Path",
            "ROOT = Path(__file__).resolve().parents[2]",
            "def t():",
            "    subprocess.run(['git', 'ls-files'], cwd=ROOT)",
        ]
    ) == {""}


def test_walk_refuses_a_path_above_the_root() -> None:
    with pytest.raises(WalkerError, match=r"tests/unit/test_x\.py:2: a path climbs above"):
        _synthetic(["from pathlib import Path", "X = Path(__file__).parents[3]", "print(X)"])


def test_walk_ignores_a_module_reading_nothing_from_the_root() -> None:
    assert _synthetic(["def t(tmp_path):", "    return tmp_path / 'docs' / 'a.md'"]) == set()


def test_gate_pytest_is_exactly_the_judges_their_suite_job_would_skip() -> None:
    table, _ = _table()
    gate_only = _gate_only_reads()
    helpers = sorted(judge for judge in gate_only if not _is_test_module(judge))
    assert helpers == [], f"helper modules reading what only gate sees: {helpers}"
    assert sorted(gate_only) == sorted(table.gate_pytest), (
        f"judges whose reads skip their suite job: {sorted(gate_only)}; "
        f"[gate].pytest: {list(table.gate_pytest)}"
    )


def test_every_path_a_tree_judge_reads_is_classified() -> None:
    table, needs = _table()
    tracked = _tracked()
    unclassified: list[str] = []
    for judge, reads in sorted(_judges().items()):
        for read in sorted(reads):
            for path in _files_under(read, tracked):
                selection = select([path], table, needs, mode="narrowed", reason="")
                if UNKNOWN_ROW in selection.rows_hit:
                    unclassified.append(f"{judge}: {path}")
    assert unclassified == []


def test_walk_reads_a_literal_passed_to_open() -> None:
    assert _synthetic(["def t():", "    return open('docs/x.md').read()"]) == {"docs/x.md"}


def test_walk_resolves_a_module_constant_passed_to_path() -> None:
    assert _synthetic(
        [
            "from pathlib import Path",
            "SPEC = 'docs/specs/a.md'",
            "def t():",
            "    return Path(SPEC).read_text()",
        ]
    ) == {"docs/specs/a.md"}


def test_walk_takes_the_working_directory_as_the_root() -> None:
    assert _synthetic(
        [
            "import os",
            "from pathlib import Path",
            "def t():",
            "    return Path.cwd() / 'docs', os.getcwd()",
        ]
    ) == {"docs", ""}


def test_walk_takes_importlib_resources_as_the_whole_tree() -> None:
    assert _synthetic(
        [
            "from importlib.resources import files",
            "def t():",
            "    return files('repoach').joinpath('x').read_text()",
        ]
    ) == {""}
    assert _synthetic(
        [
            "import importlib.resources",
            "def t():",
            "    return importlib.resources.files('repoach')",
        ]
    ) == {""}


def test_walk_takes_a_computed_string_path_as_the_whole_tree() -> None:
    assert _synthetic(
        [
            "from pathlib import Path",
            "def t(name):",
            "    return Path(f'docs/{name}'), open('docs/' + name)",
        ]
    ) == {""}
