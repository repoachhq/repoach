"""AST import-resolution gate for Developer step outputs (SP-DEV-STEP-CONTEXT).

Third occurrence of the hallucinated-import class killed a dispatch:
the model invents a ``ferova.*`` module, pytest answers
``ModuleNotFoundError``, and the retry creates the missing module
instead of fixing the import. This gate runs before pytest and turns
the failure into directive feedback — which module is missing, what
the package actually contains, and where the wanted names really
live — so even an improvised import converges on retry. Only
``ferova`` imports are checked; stdlib and third-party resolution
stays pytest's job.
"""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

from ..core.logging import get_logger

_log = get_logger(__name__)

_MAX_NAME_HOMES = 3
"""Cap on the "found in" suggestions per missing name."""


def _module_file(src_root: Path, dotted: str) -> Path | None:
    """Return the file backing a dotted module, or ``None`` when absent."""
    rel = Path(*dotted.split("."))
    as_module = (src_root / rel).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = src_root / rel / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


def _package_modules(src_root: Path, dotted: str) -> tuple[str, list[str]]:
    """Return the nearest existing parent package and its module names."""
    parts = dotted.split(".")
    while parts:
        candidate = src_root / Path(*parts)
        if candidate.is_dir():
            modules = sorted(p.stem for p in candidate.glob("*.py") if p.stem != "__init__")
            return ".".join(parts), modules
        parts.pop()
    return "", []


def _top_level_names(module_file: Path) -> set[str]:
    """Collect every top-level defined or imported name of a module.

    Imported aliases count as defined: a package façade (an
    ``__init__.py`` re-exporting submodule names, usually with
    ``__all__``) makes those names part of its public surface. The
    gate once refused ``from ferova.arch import load_registry`` —
    a re-export sanctioned by ``ferova.arch.__all__`` that pytest
    imported fine — and killed a green Developer step over an import
    the model had not even written (SP-DEV-STEP-PREFLIGHT, 2026-07-04).
    """
    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        _log.debug(
            "import_gate.target_unparseable",
            path=str(module_file),
            error=str(exc)[:120],
        )
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _find_name_homes(src_root: Path, name: str) -> list[str]:
    """Locate modules whose top level defines *name* (def/class/assign)."""
    pattern = re.compile(rf"^(def|class) {re.escape(name)}\b|^{re.escape(name)}(:| =)", re.M)
    homes: list[str] = []
    for module_file in sorted((src_root / "ferova").rglob("*.py")):
        try:
            if pattern.search(module_file.read_text(encoding="utf-8")):
                dotted = ".".join(module_file.relative_to(src_root).with_suffix("").parts)
                homes.append(dotted.removesuffix(".__init__"))
        except (OSError, UnicodeDecodeError) as exc:
            _log.debug(
                "import_gate.home_scan_skipped",
                path=str(module_file),
                error=str(exc)[:120],
            )
        if len(homes) >= _MAX_NAME_HOMES:
            break
    return homes


def _resolve_relative(path_str: str, level: int, module: str | None) -> str | None:
    """Translate a relative import inside ``src/`` to a dotted module path."""
    parts = Path(path_str).with_suffix("").parts
    if not parts or parts[0] != "src":
        return None
    package_parts = list(parts[1:-1])
    if level > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - (level - 1)]
    if module:
        base += module.split(".")
    return ".".join(base)


def _module_violation(src_root: Path, path_str: str, dotted: str) -> str:
    parent, modules = _package_modules(src_root, dotted)
    close = difflib.get_close_matches(dotted.rsplit(".", 1)[-1], modules, n=3)
    return (
        f"{path_str}: module '{dotted}' does not exist; "
        f"package '{parent}' contains: {', '.join(modules) or '(nothing)'}; "
        f"close matches: {', '.join(close) or '(none)'}"
    )


def check_imports(repo_root: Path, paths: list[str]) -> tuple[bool, str]:
    """Resolve every ``ferova`` import of the given files against ``src/``.

    Args:
        repo_root: Repository root.
        paths: Repo-relative paths (non-Python and absent files are
            skipped — the syntax gate runs before this one).

    Returns:
        ``(True, "")`` when every import resolves, else ``(False,
        report)`` with one directive line per violation: missing
        modules list their parent package's real contents and close
        name matches; missing names say which modules actually define
        them.
    """
    src_root = repo_root / "src"
    violations: list[str] = []
    for path_str in paths:
        target = repo_root / path_str
        if target.suffix != ".py" or not target.is_file():
            continue
        try:
            tree = ast.parse(target.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            _log.debug("import_gate.parse_skipped", path=path_str, error=str(exc)[:120])
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "ferova" and not _module_file(
                        src_root, alias.name
                    ):
                        violations.append(_module_violation(src_root, path_str, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    dotted = _resolve_relative(path_str, node.level, node.module)
                    if dotted is None or dotted.split(".")[0] != "ferova":
                        continue
                elif (node.module or "").split(".")[0] == "ferova":
                    dotted = node.module or ""
                else:
                    continue
                module_file = _module_file(src_root, dotted)
                if module_file is None:
                    violations.append(_module_violation(src_root, path_str, dotted))
                    continue
                defined = _top_level_names(module_file)
                for alias in node.names:
                    if alias.name == "*" or alias.name in defined:
                        continue
                    if _module_file(src_root, f"{dotted}.{alias.name}") is not None:
                        continue
                    homes = _find_name_homes(src_root, alias.name)
                    violations.append(
                        f"{path_str}: '{alias.name}' is not defined in '{dotted}'; "
                        f"found in: {', '.join(homes) or '(nowhere under ferova)'}"
                    )
    if violations:
        return False, "\n".join(violations)
    return True, ""
