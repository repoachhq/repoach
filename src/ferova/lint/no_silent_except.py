"""Scan for silent ``except`` handlers (SP-LINT-LOG-CATCH-ALL).

The scanner walks ``.py`` files under the configured roots, parses
each one with the standard library :mod:`ast` module, and reports
every ``ExceptHandler`` whose body **swallows** the exception
without emitting any log call. The pattern is the root cause behind
several silent failures filed during the 2026-05-28 logging audit.

A handler is considered **silent** when *all* of the following hold:

1. Its body contains no function call whose attribute path looks like
   logging (``_log.<level>``, ``log.<level>``, ``logger.<level>``,
   ``LOG.<level>``, ``logging.<level>``, or any callable whose final
   attribute is ``warning`` / ``error`` / ``exception`` / ``critical``
   / ``info`` / ``debug``).
2. Its body contains no ``raise`` statement (a re-raise or new raise
   is loud by construction).
3. Its final statement is one of: ``pass`` /
   ``return None|False|0|""|[]|{}`` / ``continue`` / a bare ``...``
   expression / an assignment of a bare silent constant or empty
   container literal (``result = None``, ``items: list = []``).
   Anything else (a ``break``, a ``sys.exit``, a complex branching
   block, an assignment of a *computed* value such as
   ``result = fallback()``, ...) is considered non-silent.

Known limit (by design): ``return`` of a non-silent constant such as
``return -1`` is not flagged — too many legitimate sentinel returns.

The ``# allow-silent-except: <reason>`` escape hatch (placed on the
``except`` line) suppresses the violation. Use sparingly — every
suppression should carry a one-line reason.
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_ROOTS: tuple[str, ...] = ("src", "tests", "scripts", "agents")

MAX_SILENT_EXCEPT: int = 0
"""Ratchet baseline — the count measured at gate-introduction time.

The pre-commit hook, the CI step, and the pytest gate all enforce
``total_violations <= MAX_SILENT_EXCEPT``. The baseline can only
ratchet **down**: each follow-up logging spec
(SP-COLLECTORS-LOGGING,
SP-CYCLE-OBSERVABILITY, ...) lowers the constant by the size of its
own clean-up. When it reaches 0 the gate becomes zero-tolerance.
"""

_ALLOW_RE = re.compile(r"#\s*allow-silent-except\b", re.IGNORECASE)

_LOG_OBJECT_NAMES: frozenset[str] = frozenset(
    {"_log", "log", "logger", "LOG", "logging", "_logger", "structlog"}
)

_LOG_METHOD_NAMES: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

_SILENT_TRAILING_CONSTANTS: tuple[object, ...] = (None, False, 0, "", b"")


@dataclass(frozen=True)
class SilentExceptViolation:
    """A single silent-except handler reported by the scanner.

    Attributes:
        path: Source file containing the handler, relative to the
            scan root.
        line: 1-based line number of the ``except`` keyword.
        col: 1-based column of the ``except`` keyword.
        kind: One of ``"pass"``, ``"return"``, ``"continue"``,
            ``"ellipsis"``, ``"assign"``.
        snippet: A short rendering of the handler trailer for the
            log message (e.g. ``"return None"``).
    """

    path: Path
    line: int
    col: int
    kind: str
    snippet: str

    def format(self) -> str:
        """Return a single ``path:line:col [kind] snippet`` line."""
        return f"{self.path}:{self.line}:{self.col} [{self.kind}] {self.snippet}"


def _attr_chain_names(node: ast.AST) -> tuple[str, ...]:
    """Flatten an ``a.b.c`` attribute expression into ``("a", "b", "c")``.

    Returns an empty tuple for anything that does not look like a
    plain attribute chain (subscripts, calls, etc.).
    """
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        parts.reverse()
        return tuple(parts)
    return ()


def _call_looks_like_log(call: ast.Call) -> bool:
    """Return ``True`` when *call* looks like a logging emit.

    Two recognition paths:

    * The attribute root is one of :data:`_LOG_OBJECT_NAMES` (covers
      ``_log.warning(...)``, ``logger.error(...)``, ``LOG.info(...)``).
    * The terminal attribute is one of :data:`_LOG_METHOD_NAMES`
      regardless of the root (catches custom helpers like
      ``self._log.warning(...)`` or ``ctx.logger.error(...)``).
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        chain = _attr_chain_names(func)
        if chain and chain[0] in _LOG_OBJECT_NAMES:
            return True
        if func.attr in _LOG_METHOD_NAMES:
            return True
    return isinstance(func, ast.Name) and func.id in _LOG_METHOD_NAMES


def _body_contains_log_call(body: Iterable[ast.stmt]) -> bool:
    """Walk *body* and return ``True`` when any log call is found."""
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and _call_looks_like_log(sub):
                return True
    return False


def _body_contains_raise(body: Iterable[ast.stmt]) -> bool:
    """Walk *body* and return ``True`` when any ``raise`` statement appears."""
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Raise):
                return True
    return False


def _silent_value_snippet(value: ast.expr) -> str | None:
    """Render *value* when it is a bare silent constant or empty literal.

    Shared by the ``return`` and assignment trailer cases: a bare
    silent constant (:data:`_SILENT_TRAILING_CONSTANTS`) or an empty
    ``List`` / ``Tuple`` / ``Dict`` / ``Set`` literal hides the
    failure; a *computed* value (call, name, attribute read) is a
    legitimate recovery and returns ``None`` here.
    """
    if isinstance(value, ast.Constant) and value.value in _SILENT_TRAILING_CONSTANTS:
        return repr(value.value)
    if isinstance(value, ast.List) and not value.elts:
        return "[]"
    if isinstance(value, ast.Tuple) and not value.elts:
        return "()"
    if isinstance(value, ast.Dict) and not value.keys:
        return "{}"
    if isinstance(value, ast.Set) and not value.elts:
        return "set()"
    return None


def _classify_silent_trailer(stmt: ast.stmt) -> tuple[str, str] | None:
    """Return ``(kind, snippet)`` when *stmt* is a silent trailer, else ``None``.

    The trailer is the last statement of an ``ExceptHandler`` body.
    SP-LINT-SILENT-TRAILERS closed the two empirically confirmed
    bypasses: ``...`` as a one-character ``pass`` replacement, and
    assignments of bare silent sentinels (``result = None``).
    """
    if isinstance(stmt, ast.Pass):
        return ("pass", "pass")
    if isinstance(stmt, ast.Continue):
        return ("continue", "continue")
    if isinstance(stmt, ast.Return):
        value = stmt.value
        if value is None:
            return ("return", "return")
        rendered = _silent_value_snippet(value)
        if rendered is not None:
            return ("return", f"return {rendered}")
        return None
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return ("ellipsis", "...")
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        value = stmt.value
        if value is None:
            return None
        rendered = _silent_value_snippet(value)
        if rendered is None:
            return None
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        rendered_targets = " = ".join(ast.unparse(target) for target in targets)
        return ("assign", f"{rendered_targets} = {rendered}")
    return None


def _allow_suppressed(except_line_source: str) -> bool:
    """Return ``True`` when the ``except`` line carries the allow directive."""
    return bool(_ALLOW_RE.search(except_line_source))


def scan_file(path: Path) -> list[SilentExceptViolation]:
    """Return every silent-except handler found in *path*.

    Files that fail to parse (syntax error, encoding error, ...) are
    silently skipped — any downstream tool would also crash on them,
    so this scanner does not raise.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("silent_except_scan_read_failed", extra={"path": str(path), "error": str(exc)})
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        _log.debug(
            "silent_except_scan_parse_failed",
            extra={"path": str(path), "error": str(exc)},
        )
        return []

    source_lines = source.splitlines()
    out: list[SilentExceptViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        if not body:
            continue
        if _body_contains_log_call(body):
            continue
        if _body_contains_raise(body):
            continue
        trailer = _classify_silent_trailer(body[-1])
        if trailer is None:
            continue
        line_index = node.lineno - 1
        if 0 <= line_index < len(source_lines) and _allow_suppressed(source_lines[line_index]):
            continue
        kind, snippet = trailer
        out.append(
            SilentExceptViolation(
                path=path,
                line=node.lineno,
                col=node.col_offset + 1,
                kind=kind,
                snippet=snippet,
            )
        )
    return out


def iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``*.py`` file under the configured roots.

    Non-existent roots are skipped silently. Files inside any
    ``__pycache__`` directory are excluded.
    """
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def scan(roots: Iterable[Path]) -> list[SilentExceptViolation]:
    """Scan every Python file under *roots* and aggregate violations."""
    out: list[SilentExceptViolation] = []
    for f in iter_python_files(roots):
        out.extend(scan_file(f))
    return out


def summarise(violations: Iterable[SilentExceptViolation]) -> dict[str, int]:
    """Bucket violations by trailer kind plus total + file count.

    Returns:
        Dict with keys ``"pass"``, ``"return"``, ``"continue"``,
        ``"ellipsis"``, ``"assign"``, ``"total"``, and ``"files"``
        (number of distinct files with at least one violation).
    """
    counts = {"pass": 0, "return": 0, "continue": 0, "ellipsis": 0, "assign": 0}
    total = 0
    files: set[Path] = set()
    for v in violations:
        total += 1
        files.add(v.path)
        if v.kind in counts:
            counts[v.kind] += 1
    return {**counts, "total": total, "files": len(files)}
