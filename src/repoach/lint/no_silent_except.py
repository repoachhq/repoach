"""Scan for silent ``except`` handlers (SP-LINT-LOG-CATCH-ALL).

The scanner walks ``.py`` files under the configured roots, parses
each one with the standard library :mod:`ast` module, and reports
every ``ExceptHandler`` whose body **swallows** the exception
without emitting any log call, plus every ``contextlib.suppress``
block that does the same (SP-LINT-GATE-SOUNDNESS, G2). The pattern
is the root cause behind several silent failures filed during the
2026-05-28 logging audit.

A handler is considered **silent** when *all* of the following hold:

1. Its body contains no log call that actually handles the caught
   exception — the call's receiver must be rooted in a real logger
   object (:data:`_LOG_OBJECT_NAMES`), and when the handler binds the
   exception (``except ... as exc:``) the call's arguments must
   reference that bound name. A call whose receiver is not a logger
   object (``cache.info()``, bare ``q.log(x)``) no longer clears the
   handler merely because its terminal attribute happens to spell a
   logging method name (SP-LINT-GATE-SOUNDNESS, G1).
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

A ``with contextlib.suppress(<Type>, ...):`` block is reported under
the same rule: a body with no qualifying log call is silent,
regardless of what trailing statement it ends on (SP-LINT-GATE-
SOUNDNESS, G2).

The escape hatch is a **standalone** comment line directly above the
``except`` (or the ``with contextlib.suppress`` statement), of the
exact form ``# repoach: allow-silent-except reason="<non-empty>"``.
Because it sits on its own line it satisfies the no-inline-comments
gate; because the reason is enforced, an empty or missing ``reason=``
does not suppress the violation (SP-LINT-GATE-SOUNDNESS, G3).

A file that fails to read or parse is reported as a single
``kind="unparseable"`` violation rather than silently skipped
(SP-LINT-GATE-SOUNDNESS, G4) — an unparseable file under a scanned
root is exactly the kind of blind spot this gate exists to close.
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

_ALLOW_DIRECTIVE_RE = re.compile(
    r'^#\s*repoach:\s*allow-silent-except\s+reason="([^"]*)"\s*$',
    re.IGNORECASE,
)

_LOG_OBJECT_NAMES: frozenset[str] = frozenset(
    {"_log", "log", "logger", "LOG", "logging", "_logger", "structlog"}
)

_SILENT_TRAILING_CONSTANTS: tuple[object, ...] = (None, False, 0, "", b"")


@dataclass(frozen=True)
class SilentExceptViolation:
    """A single silent-except (or silent-suppress) handler reported by the scanner.

    Attributes:
        path: Source file containing the handler, relative to the
            scan root.
        line: 1-based line number of the ``except`` keyword, the
            ``with`` keyword of a silent ``contextlib.suppress``
            block, or ``1`` for an ``"unparseable"`` violation.
        col: 1-based column of the same token.
        kind: One of ``"pass"``, ``"return"``, ``"continue"``,
            ``"ellipsis"``, ``"assign"``, ``"suppress"`` (a silent
            ``contextlib.suppress`` block), or ``"unparseable"`` (the
            file failed to read or parse).
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


def _call_references_name(call: ast.Call, name: str) -> bool:
    """Return ``True`` when *name* appears as a bound identifier in *call*'s arguments."""
    for arg in (*call.args, *(kw.value for kw in call.keywords)):
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name) and sub.id == name:
                return True
    return False


def _call_looks_like_log(call: ast.Call, *, exc_name: str | None) -> bool:
    """Return ``True`` when *call* is a real logging emit that handles the exception.

    The call's receiver must be rooted in a genuine logger object
    (:data:`_LOG_OBJECT_NAMES`) — a bare ``cache.info(...)`` or
    ``q.log(x)`` on a non-logger name no longer qualifies merely
    because its terminal attribute spells a logging method name. When
    the handler binds the caught exception (``except ... as exc:``),
    the call must additionally reference that bound name somewhere in
    its arguments; a handler with no binding is cleared by any
    qualifying logger call.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        chain = _attr_chain_names(func)
        receiver = chain[:-1]
        if not any(part in _LOG_OBJECT_NAMES for part in receiver):
            return False
    elif isinstance(func, ast.Name):
        if func.id not in _LOG_OBJECT_NAMES:
            return False
    else:
        return False
    if exc_name is None:
        return True
    return _call_references_name(call, exc_name)


def _body_contains_log_call(body: Iterable[ast.stmt], *, exc_name: str | None) -> bool:
    """Walk *body* and return ``True`` when any qualifying log call is found."""
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and _call_looks_like_log(sub, exc_name=exc_name):
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


def _with_item_is_contextlib_suppress(item: ast.withitem) -> bool:
    """Return ``True`` when *item*'s context expression is ``contextlib.suppress(...)``.

    Recognises both ``contextlib.suppress(...)`` and the bare
    ``suppress(...)`` spelling produced by ``from contextlib import
    suppress``.
    """
    expr = item.context_expr
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute):
        return _attr_chain_names(func) == ("contextlib", "suppress")
    return isinstance(func, ast.Name) and func.id == "suppress"


def _suppress_is_silent(node: ast.With) -> tuple[str, str] | None:
    """Return ``(kind, snippet)`` when *node* is a silent ``contextlib.suppress`` block.

    Returns ``None`` both when *node* is not a ``contextlib.suppress``
    / ``suppress`` context manager at all, and when its body already
    emits a qualifying log call or re-raises — mirroring the
    ``ExceptHandler`` path in :func:`scan_file`. A ``suppress`` block
    binds no exception name, so any qualifying logger call in its
    body clears it.
    """
    suppress_items = [item for item in node.items if _with_item_is_contextlib_suppress(item)]
    if not suppress_items:
        return None
    if _body_contains_log_call(node.body, exc_name=None):
        return None
    if _body_contains_raise(node.body):
        return None
    rendered = ", ".join(ast.unparse(item.context_expr) for item in suppress_items)
    return ("suppress", f"with {rendered}:")


def _directive_reason(line: str) -> str | None:
    """Return the non-empty reason carried by a standalone allow-directive line.

    Returns ``None`` when *line* is not the directive at all, or when
    it is present but its ``reason=`` is empty — an empty reason is a
    fail-closed no-op (the violation still reports).
    """
    match = _ALLOW_DIRECTIVE_RE.match(line.strip())
    if match is None:
        return None
    reason = match.group(1).strip()
    return reason or None


def _allow_suppressed(source_lines: list[str], construct_line_index: int) -> bool:
    """Return ``True`` when the line above ``construct_line_index`` carries a valid directive.

    Args:
        source_lines: The file's lines (0-based, from ``str.splitlines()``).
        construct_line_index: 0-based index of the ``except``/``with`` line.
    """
    above_index = construct_line_index - 1
    if above_index < 0:
        return False
    return _directive_reason(source_lines[above_index]) is not None


def scan_file(path: Path) -> list[SilentExceptViolation]:
    """Return every silent-except / silent-suppress handler found in *path*.

    Files that fail to read or parse (syntax error, encoding error,
    ...) FAIL the gate: a single ``kind="unparseable"`` violation is
    reported rather than the file being silently skipped
    (SP-LINT-GATE-SOUNDNESS, G4).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("silent_except_scan_read_failed", extra={"path": str(path), "error": str(exc)})
        return [
            SilentExceptViolation(
                path=path, line=1, col=1, kind="unparseable", snippet=f"read failed: {exc}"
            )
        ]
    try:
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        _log.warning(
            "silent_except_scan_parse_failed",
            extra={"path": str(path), "error": str(exc)},
        )
        return [
            SilentExceptViolation(
                path=path, line=1, col=1, kind="unparseable", snippet=f"parse failed: {exc}"
            )
        ]

    source_lines = source.splitlines()
    out: list[SilentExceptViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            suppress_trailer = _suppress_is_silent(node)
            if suppress_trailer is None:
                continue
            if _allow_suppressed(source_lines, node.lineno - 1):
                continue
            kind, snippet = suppress_trailer
            out.append(
                SilentExceptViolation(
                    path=path,
                    line=node.lineno,
                    col=node.col_offset + 1,
                    kind=kind,
                    snippet=snippet,
                )
            )
            continue
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = node.body
        if not body:
            continue
        if _body_contains_log_call(body, exc_name=node.name):
            continue
        if _body_contains_raise(body):
            continue
        trailer = _classify_silent_trailer(body[-1])
        if trailer is None:
            continue
        if _allow_suppressed(source_lines, node.lineno - 1):
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

    Non-existent roots are skipped silently at this layer — the CLI
    wrappers are responsible for failing closed when *zero* roots
    resolve (SP-LINT-GATE-SOUNDNESS, G5). Files inside any
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
        ``"ellipsis"``, ``"assign"``, ``"suppress"``,
        ``"unparseable"``, ``"total"``, and ``"files"`` (number of
        distinct files with at least one violation).
    """
    counts = {
        "pass": 0,
        "return": 0,
        "continue": 0,
        "ellipsis": 0,
        "assign": 0,
        "suppress": 0,
        "unparseable": 0,
    }
    total = 0
    files: set[Path] = set()
    for v in violations:
        total += 1
        files.add(v.path)
        if v.kind in counts:
            counts[v.kind] += 1
    return {**counts, "total": total, "files": len(files)}
