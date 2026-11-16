"""The tree judges and ``[gate].pytest`` (SP-AFFECTED-JOBS-TABLE D5, T8).

A tree judge is a test module that reads a repository path from the
root: a ``/`` chain starting at ``Path(__file__).resolve().parents[2]``
(or a name bound to one, in its scope), or ``Path("<root file>")``. The
walk reads test sources with :mod:`ast`, never with a regular expression.

The invariant judged: a change to a path a judge reads must run that
judge. The judge runs in the job whose ``pytest`` step names its test
directory; when a tracked file under one of its paths selects no such
job, only the ``gate`` job sees the change, so the judge must be in
``[gate].pytest``; and every ``[gate].pytest`` entry must be such a
judge. Every path a judge reads must be classified by the table.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from repoach.affected.select import DEFAULT_WORKFLOW_PATH, select, workflow_jobs
from repoach.affected.table import DEFAULT_TABLE_PATH, UNKNOWN_ROW, AffectedTable, load_table
from repoach.review.merge_wait import load_workflow

_ROOT = Path(__file__).resolve().parents[2]
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _is_root(node: ast.AST) -> bool:
    """Return whether ``node`` is ``Path(__file__)[.resolve()].parents[2]``.

    Args:
        node: An expression.

    Returns:
        True for the repository-root expression of a module two levels
        under the root.
    """
    if not (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 2
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
    ):
        return False
    base = node.value.value
    if (
        isinstance(base, ast.Call)
        and isinstance(base.func, ast.Attribute)
        and base.func.attr == "resolve"
    ):
        base = base.func.value
    return (
        isinstance(base, ast.Call)
        and isinstance(base.func, ast.Name)
        and base.func.id == "Path"
        and len(base.args) == 1
        and isinstance(base.args[0], ast.Name)
        and base.args[0].id == "__file__"
    )


def _chain(node: ast.AST, bindings: Mapping[str, str], root_files: frozenset[str]) -> str | None:
    """Return the repository path ``node`` denotes, or None when it denotes none.

    Args:
        node: An expression.
        bindings: Names of the scope bound to a repository path (``""``
            is the root itself).
        root_files: Tracked files at the root, the only ``Path("…")``
            literals that count.

    Returns:
        ``""`` for the root, ``a/b`` for ``root / "a" / "b"``; a chain
        stops at the first operand that is not a string literal and
        denotes what it reached; None for anything else.
    """
    if _is_root(node):
        return ""
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Path"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in root_files
    ):
        return str(node.args[0].value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _chain(node.left, bindings, root_files)
        if left is None:
            return None
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return str(PurePosixPath(left, right.value)) if left else right.value
        return left
    return None


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield the nodes of a scope, nested function scopes excluded.

    Args:
        body: The statements of the scope.

    Yields:
        Every node reachable without entering a nested function or
        lambda; the nested scope nodes themselves are yielded.
    """
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        pending.extend(ast.iter_child_nodes(node))


def _scope_bindings(
    nodes: list[ast.AST], inherited: Mapping[str, str], root_files: frozenset[str]
) -> dict[str, str]:
    """Return the names of one scope bound to a repository path.

    A name assigned in the scope to anything else shadows an inherited
    binding. Bindings are resolved to a fixpoint, so their order in the
    source does not matter.

    Args:
        nodes: The scope's own nodes (:func:`_own_nodes`).
        inherited: The enclosing scope's bindings.
        root_files: Tracked files at the root.

    Returns:
        Name to repository path.
    """
    assigned: dict[str, list[ast.expr]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assigned.setdefault(node.target.id, []).append(node.value)
    bindings = {name: path for name, path in inherited.items() if name not in assigned}
    changed = True
    while changed:
        changed = False
        for name, values in assigned.items():
            if name in bindings:
                continue
            for value in values:
                path = _chain(value, bindings, root_files)
                if path is not None:
                    bindings[name] = path
                    changed = True
                    break
    return bindings


def _scope_reads(
    body: list[ast.stmt], inherited: Mapping[str, str], root_files: frozenset[str]
) -> set[str]:
    """Return the repository paths one scope and its nested scopes read.

    Args:
        body: The statements of the scope.
        inherited: The enclosing scope's bindings.
        root_files: Tracked files at the root.

    Returns:
        Every non-empty path denoted by a chain that is not the left
        operand of a longer chain.
    """
    nodes = list(_own_nodes(body))
    bindings = _scope_bindings(nodes, inherited, root_files)
    inner = {id(node.left) for node in nodes if isinstance(node, ast.BinOp)}
    reads: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            reads |= _scope_reads(node.body, bindings, root_files)
        elif isinstance(node, ast.Lambda):
            reads |= _scope_reads([ast.Expr(node.body)], bindings, root_files)
        elif isinstance(node, ast.BinOp | ast.Call) and id(node) not in inner:
            path = _chain(node, bindings, root_files)
            if path:
                reads.add(path)
    return reads


def tree_reads(source: str, root_files: frozenset[str]) -> set[str]:
    """Return the repository paths a test module reads from the root.

    Args:
        source: The module's source.
        root_files: Tracked files at the root.

    Returns:
        The paths, relative to the root; empty for a module that is not
        a tree judge.

    Raises:
        SyntaxError: ``source`` is not Python.
    """
    return _scope_reads(ast.parse(source).body, {}, root_files)


def _tracked() -> list[str]:
    """Return ``git ls-files`` of the checkout."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [path for path in listing.split("\0") if path]


def _table() -> tuple[AffectedTable, dict[str, frozenset[str]]]:
    """Return the committed table and the workflow's needs."""
    workflow = _ROOT / DEFAULT_WORKFLOW_PATH
    needs = workflow_jobs(workflow)
    return load_table(_ROOT / DEFAULT_TABLE_PATH, needs, workflow=workflow.name), needs


def _suite_jobs(test_dir: str) -> frozenset[str]:
    """Return the jobs whose ``pytest`` step names ``test_dir``.

    Args:
        test_dir: A test directory (``tests/unit``).

    Returns:
        The ids of the jobs that run the modules of ``test_dir``.
    """
    jobs = load_workflow(_ROOT / DEFAULT_WORKFLOW_PATH)["jobs"]
    return frozenset(
        job_id
        for job_id, job in jobs.items()
        for step in job.get("steps", [])
        if "pytest" in str(step.get("run", "")).split()
        and test_dir in str(step.get("run", "")).split()
    )


def _judges() -> dict[str, set[str]]:
    """Return every tree judge of the checkout with the paths it reads."""
    tracked = _tracked()
    root_files = frozenset(path for path in tracked if "/" not in path)
    judges: dict[str, set[str]] = {}
    for path in tracked:
        if (
            path.startswith("tests/")
            and PurePosixPath(path).name.startswith("test_")
            and path.endswith(".py")
        ):
            reads = tree_reads((_ROOT / path).read_text(encoding="utf-8"), root_files)
            if reads:
                judges[path] = reads
    return judges


def _files_under(read: str, tracked: list[str]) -> list[str]:
    """Return the tracked files ``read`` names: itself, or every file under it.

    Args:
        read: A path a judge reads.
        tracked: ``git ls-files``.

    Returns:
        The tracked files equal to ``read`` or under ``read/``.
    """
    return [path for path in tracked if path == read or path.startswith(f"{read}/")]


def _gate_only_reads() -> dict[str, list[str]]:
    """Return each judge with the paths it reads whose change skips its suite job."""
    table, needs = _table()
    tracked = _tracked()
    found: dict[str, list[str]] = {}
    for judge, reads in _judges().items():
        suite = _suite_jobs(str(PurePosixPath(judge).parent))
        for read in sorted(reads):
            for path in _files_under(read, tracked):
                selection = select([path], table, needs, mode="narrowed", reason="")
                if not selection.jobs & suite:
                    found.setdefault(judge, []).append(read)
                    break
    return found


def test_walk_finds_both_templates_of_pr_template_measured() -> None:
    root_files = frozenset(path for path in _tracked() if "/" not in path)
    source = (_ROOT / "tests/unit/test_pr_template_measured.py").read_text(encoding="utf-8")
    assert tree_reads(source, root_files) == {
        "docs/PR_TEMPLATE.md",
        ".github/pull_request_template.md",
    }


def test_walk_finds_a_root_file_literal() -> None:
    root_files = frozenset(path for path in _tracked() if "/" not in path)
    source = (_ROOT / "tests/unit/test_claude_md_integration_flag.py").read_text(encoding="utf-8")
    assert tree_reads(source, root_files) == {"CLAUDE.md"}


def test_walk_follows_bindings_and_scopes() -> None:
    source = "\n".join(
        [
            "from pathlib import Path",
            "ROOT = Path(__file__).resolve().parents[2]",
            "SPECS = ROOT / 'docs' / 'specs'",
            "def a():",
            "    return SPECS / 'x.md'",
            "def b(tmp_path):",
            "    ROOT = tmp_path",
            "    return ROOT / 'docs'",
            "def c(name):",
            "    return Path(__file__).parents[2] / 'scripts' / name",
            "def d():",
            "    return Path('README.md'), Path('docs/fake.md')",
        ]
    )
    assert tree_reads(source, frozenset({"README.md"})) == {
        "docs/specs",
        "docs/specs/x.md",
        "scripts",
        "README.md",
    }


def test_walk_ignores_a_module_reading_nothing_from_the_root() -> None:
    source = "from pathlib import Path\ndef t(tmp_path):\n    return tmp_path / 'docs' / 'a.md'\n"
    assert tree_reads(source, frozenset({"README.md"})) == set()


def test_gate_pytest_is_exactly_the_judges_their_suite_job_would_skip() -> None:
    table, _ = _table()
    gate_only = _gate_only_reads()
    assert sorted(gate_only) == sorted(table.gate_pytest), (
        f"judges whose reads skip their suite job: {gate_only}; "
        f"[gate].pytest: {list(table.gate_pytest)}"
    )


def test_every_path_a_tree_judge_reads_is_classified() -> None:
    table, needs = _table()
    tracked = _tracked()
    unclassified: list[str] = []
    for judge, reads in sorted(_judges().items()):
        for read in sorted(reads):
            files = _files_under(read, tracked)
            if not files:
                unclassified.append(f"{judge}: {read} (no tracked file)")
                continue
            for path in files:
                selection = select([path], table, needs, mode="narrowed", reason="")
                if UNKNOWN_ROW in selection.rows_hit:
                    unclassified.append(f"{judge}: {path}")
    assert unclassified == []
