"""Read-only, repo-jailed exploration tools for the Planner agent.

SP-PLANNER-AGENT: the Planner explores the repository before writing
the action plan — these three :class:`ToolDef` factories are its only
hands. Every tool is jailed to the repository root (lexical check via
:func:`repoach.review.plan.require_repo_relative`, then a resolved
``relative_to`` containment check against symlink escapes) and capped
so a single result never floods the context window.

Tools never raise on bad input: they return an error STRING the model
can read and correct on the next turn — a crashed loop teaches the
model nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..agent_engine.agent_loop import ToolDef
from ..core.logging import get_logger
from .plan import require_repo_relative

_log = get_logger(__name__)

_READ_FILE_CAP_CHARS: int = 24_000
_LIST_DIR_CAP_ENTRIES: int = 200
_GREP_CAP_MATCHES: int = 80
_GREP_LINE_CAP_CHARS: int = 200
_GREP_SEARCH_WINDOW_CHARS: int = 500

_NOISE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        ".venv",
    }
)


def _coerce_int(value: object, name: str) -> int | str:
    """Accept int or digit-string for a paging parameter; error string otherwise.

    Proxy-served models routinely pass tool arguments as JSON strings
    ("120" for 120); rejecting those would waste a Developer turn on a
    formality the tool can absorb.
    """
    if isinstance(value, bool):
        return f"error: {name!r} must be an integer, got {value!r}"
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        candidate = stripped.removeprefix("-")
        if candidate.isdigit():
            return int(stripped)
    return f"error: {name!r} must be an integer, got {value!r}"


def _jail(repo_root: Path, path: str) -> Path | str:
    """Resolve *path* inside *repo_root* or describe why it is refused.

    Args:
        repo_root: Resolved repository root.
        path: Candidate repo-relative path from the model.

    Returns:
        The resolved :class:`Path` on success, or an error string the
        model can read.
    """
    try:
        require_repo_relative(path)
    except ValueError as exc:
        return f"error: {exc}"
    target = (repo_root / path).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError:
        return f"error: path escapes the repository: {path!r}"
    return target


def _is_noise(target: Path) -> bool:
    """Return whether any path segment is a pruned noise directory."""
    return any(part in _NOISE_DIR_NAMES for part in target.parts)


def make_planner_tools(repo_root: Path | None = None) -> list[ToolDef]:
    """Build the Planner's read-only exploration toolbox.

    Args:
        repo_root: Repository root every tool is jailed to (defaults
            to ``Path.cwd()``).

    Returns:
        ``list_dir``, ``read_file`` and ``grep_repo`` as
        :class:`ToolDef` entries ready for :meth:`AgentLoop.run`.
    """
    root = (repo_root or Path.cwd()).resolve()

    def list_dir(path: str = ".") -> str:
        """List one directory, dirs suffixed ``/``, noise pruned."""
        resolved = _jail(root, path) if path != "." else root
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_dir():
            return f"error: not a directory: {path!r}"
        entries: list[str] = []
        for child in sorted(resolved.iterdir(), key=lambda p: p.name):
            if child.name in _NOISE_DIR_NAMES:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
        clipped = entries[:_LIST_DIR_CAP_ENTRIES]
        if len(entries) > _LIST_DIR_CAP_ENTRIES:
            clipped.append(f"... [{len(entries) - _LIST_DIR_CAP_ENTRIES} more entries clipped]")
        _log.info("planner_tools.list_dir", path=path, n_entries=len(entries))
        return "\n".join(clipped) if clipped else "(empty directory)"

    def read_file(path: str, start_line: int = 1, max_lines: int = 0) -> str:
        """Read one repo file with line-based paging under the 24 000-char cap.

        A file larger than the cap cannot be served whole, and a blind
        truncation strands the model mid-module: two Developer sessions
        exhausted their full turn budget re-reading ``dev_runner.py``
        without ever reaching its second half. Every clipped response
        therefore ends with the exact ``start_line`` call that serves
        the next window, so the refusal doubles as the paging manual.
        """
        start = _coerce_int(start_line, "start_line")
        if isinstance(start, str):
            return start
        window = _coerce_int(max_lines, "max_lines")
        if isinstance(window, str):
            return window
        if start < 1:
            return f"error: 'start_line' must be >= 1, got {start}"
        resolved = _jail(root, path)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"error: no such file: {path!r}"
        try:
            contents = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning("planner_tools.read_failed", path=path, error=str(exc)[:120])
            return f"error: unreadable file {path!r}: {type(exc).__name__}"
        lines = contents.splitlines()
        total = len(lines)
        _log.info(
            "planner_tools.read_file",
            path=path,
            chars=len(contents),
            start_line=start,
            max_lines=window,
        )
        if start == 1 and window <= 0 and len(contents) <= _READ_FILE_CAP_CHARS:
            return contents
        if start > total:
            return f"error: 'start_line' {start} is beyond the end of {path!r} ({total} lines)"
        requested = lines[start - 1 :] if window <= 0 else lines[start - 1 : start - 1 + window]
        served: list[str] = []
        served_chars = 0
        for line in requested:
            if not served and len(line) > _READ_FILE_CAP_CHARS:
                return (
                    line[:_READ_FILE_CAP_CHARS]
                    + f"\n... [truncated: line {start} alone exceeds the"
                    + f" {_READ_FILE_CAP_CHARS}-char cap; file is {len(contents)} chars total]"
                )
            if served and served_chars + len(line) + 1 > _READ_FILE_CAP_CHARS:
                break
            served.append(line)
            served_chars += len(line) + 1
        last = start + len(served) - 1
        body = "\n".join(served)
        if last >= total:
            return f"{body}\n[end of file: lines {start}-{total} of {total}]"
        return (
            f"{body}\n... [truncated at line {last} of {total}: "
            f"call read_file({path!r}, start_line={last + 1}) to continue]"
        )

    def grep_repo(pattern: str, glob: str = "*.py") -> str:
        """Regex-search the repo, ``path:line: text`` matches, capped at 80.

        The search window per line is bounded at
        :data:`_GREP_SEARCH_WINDOW_CHARS` so a model-supplied pattern
        with catastrophic backtracking cannot hang the session on a
        pathological line.
        """
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"error: invalid regex {pattern!r}: {exc}"
        matches: list[str] = []
        total = 0
        for candidate in sorted(root.rglob(glob)):
            if _is_noise(candidate.relative_to(root)) or not candidate.is_file():
                continue
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                _log.warning(
                    "planner_tools.grep_skip_symlink_escape",
                    path=str(candidate.relative_to(root)),
                )
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.debug(
                    "planner_tools.grep_skip_unreadable",
                    path=str(candidate),
                    error=type(exc).__name__,
                )
                continue
            rel = candidate.relative_to(root)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line[:_GREP_SEARCH_WINDOW_CHARS]):
                    total += 1
                    if len(matches) < _GREP_CAP_MATCHES:
                        matches.append(f"{rel}:{lineno}: {line.strip()[:_GREP_LINE_CAP_CHARS]}")
        _log.info("planner_tools.grep_repo", pattern=pattern, glob=glob, n_matches=total)
        if not matches:
            return f"no match for {pattern!r} in {glob!r}"
        if total > len(matches):
            matches.append(f"... [{total - len(matches)} more matches clipped]")
        return "\n".join(matches)

    return [
        ToolDef(
            name="list_dir",
            description=(
                "List the entries of one repository directory (directories get a "
                "trailing slash). Use '.' for the repo root."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative directory path"}
                },
                "required": ["path"],
            },
            callable_fn=list_dir,
        ),
        ToolDef(
            name="read_file",
            description=(
                "Read one repo-relative file (capped at 24000 chars). For files"
                " larger than the cap, page with start_line/max_lines — a"
                " truncated response names the exact start_line to continue from."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"},
                    "start_line": {
                        "type": "integer",
                        "description": "1-based line to start reading from (default 1)",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "max lines to return; 0 means until cap or EOF",
                    },
                },
                "required": ["path"],
            },
            callable_fn=read_file,
        ),
        ToolDef(
            name="grep_repo",
            description=(
                "Regex-search files matching a glob across the repository; returns "
                "path:line: text matches (capped at 80)."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex to search for"},
                    "glob": {
                        "type": "string",
                        "description": "filename glob, default *.py (e.g. *.md, *.toml)",
                    },
                },
                "required": ["pattern"],
            },
            callable_fn=grep_repo,
        ),
    ]
