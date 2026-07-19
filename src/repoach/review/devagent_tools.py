"""Author + verify tools for the DEVAGENT coding agent (SP-DEVAGENT-TOOLS).

Slice 1 of the real-coding-agent arc (see ``docs/devagent_architecture.md``).
Where the Planner gets read-only hands (``planner_tools.py``), the Developer
gets the hands that *change and check* the tree: ``write_file`` / ``edit_file``
to author code and ``run_tests`` / ``run_ruff`` to verify it — so a later slice
can wire them into ``AgentLoop.run`` for an author→test→iterate loop.

Every tool follows the same two rules as the Planner toolbox:

* **Sandboxed writes.** Mutating tools jail the path to the repository root *and*
  require the **resolved** repo-relative path to clear the write whitelist
  (:func:`coder_loop.is_path_allowed`), so a ``./`` prefix or an in-repo symlink
  cannot slip a forbidden target past the string-matching whitelist — the agent
  cannot create or overwrite ``.github/workflows``, ``.githooks``,
  ``prompts/review/*``, ``.env*`` or escape via traversal.
* **Never raise.** A tool returns an error *string* the model can read and
  correct next turn; a crashed loop teaches the model nothing.

Threat model: ``run_tests`` *executes* code the agent wrote (pytest imports
``conftest.py`` and the target), which is inherent to a coding agent — the
write-jail cannot contain that. Two mitigations: secret-bearing env vars are
scrubbed from the test subprocess (:func:`review.secret_env.scrubbed_env`), and real containment
is operational — the Developer works on a feature branch and nothing reaches
``main``/``develop`` without passing the 4-reviewer gate. This module does not
provide OS-level process isolation.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

from ..agent_engine.agent_loop import ToolDef
from ..core.logging import get_logger
from .coder_loop import is_path_allowed, run_ruff_gate
from .patch_apply import apply_search_replace_edits
from .plan import require_repo_relative
from .secret_env import scrubbed_env

_log = get_logger(__name__)

_WRITE_CAP_CHARS: int = 200_000
_TEST_TAIL_CAP_CHARS: int = 6_000
_TEST_TIMEOUT_S: int = 900
_DEFAULT_TEST_TARGET: str = "tests/unit"


def _write_text_safely(target: Path, text: str) -> str:
    """Write *text* to *target* without risking a half-truncated file.

    Encodes to UTF-8 **before** opening the file, so non-encodable content (e.g. a
    lone surrogate) is rejected with an error string while the existing file is
    left untouched — ``Path.write_text`` truncates first and would lose data if the
    encode then failed mid-write. Returns ``""`` on success or an error string.
    """
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return f"error: content is not valid UTF-8 ({exc})"
    try:
        target.write_bytes(data)
    except OSError as exc:
        return f"error: could not write {target.name!r}: {type(exc).__name__}"
    return ""


def _resolve_in_repo(repo_root: Path, path: str) -> Path | str:
    """Resolve *path* inside *repo_root* or describe why it is refused."""
    if not isinstance(path, str):
        return "error: 'path' must be a string"
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


def normalise_contract(repo_root: Path, allowed_paths: Iterable[str]) -> frozenset[str]:
    """Resolve a file contract to the canonical repo-relative posix paths.

    Each entry is resolved through ``repo_root`` and reduced to its repo-relative
    posix form — the same shape :func:`_resolve_writable` compares against, and the
    same shape ``git status --porcelain`` reports — so a contract written as
    ``./src/x.py`` matches a write to ``src/x.py`` both at the in-loop write jail
    and at the runner's post-loop contract check (SP-DEVAGENT-LOOP). Entries that
    are not strings or escape the repo are dropped silently; they can never
    authorise a write the whitelist would otherwise refuse.
    """
    out: set[str] = set()
    for path in allowed_paths:
        if not isinstance(path, str):
            continue
        resolved = (repo_root / path).resolve()
        if resolved.is_relative_to(repo_root):
            out.add(resolved.relative_to(repo_root).as_posix())
    return frozenset(out)


def _resolve_writable(
    repo_root: Path, path: str, contract: frozenset[str] | None = None
) -> Path | str:
    """Resolve a path the agent may *write*, or an error string.

    Jails to the repo root, then enforces the write whitelist on the **resolved**
    repo-relative path — not the raw string — so neither a ``./`` (or ``//``)
    prefix nor an in-repo symlink can slip a forbidden target past
    :func:`is_path_allowed` (whose matching is plain string prefix/exact). When a
    ``contract`` is supplied (the step's file whitelist, SP-DEVAGENT-LOOP), the
    resolved path must also be one of its members, so the agentic loop cannot write
    outside the step it was handed — the model reads the refusal and self-corrects.
    """
    resolved = _resolve_in_repo(repo_root, path)
    if isinstance(resolved, str):
        return resolved
    relative = resolved.relative_to(repo_root).as_posix()
    if not is_path_allowed(relative):
        return f"error: path is not writable (outside the allow-list): {path!r}"
    if contract is not None and relative not in contract:
        return (
            f"error: path is outside this step's file contract: {path!r} "
            f"(allowed: {sorted(contract)})"
        )
    return resolved


def make_developer_tools(
    repo_root: Path | None = None,
    allowed_paths: Iterable[str] | None = None,
) -> list[ToolDef]:
    """Build the Developer's author + verify toolbox.

    Args:
        repo_root: Repository root every tool is jailed to (defaults to
            ``Path.cwd()``).
        allowed_paths: Optional per-step file contract (SP-DEVAGENT-LOOP). When
            given, ``write_file`` / ``edit_file`` refuse any path outside it with
            an error string and no write, on top of the standard write whitelist.
            ``None`` keeps the slice-1 behaviour (whitelist only).

    Returns:
        ``write_file``, ``edit_file``, ``run_tests`` and ``run_ruff`` as
        :class:`ToolDef` entries ready for :meth:`AgentLoop.run`.
    """
    root = (repo_root or Path.cwd()).resolve()
    contract = None if allowed_paths is None else normalise_contract(root, allowed_paths)

    def write_file(path: str, content: str) -> str:
        """Author a whole file (create or overwrite), creating parent dirs."""
        if not isinstance(content, str):
            return "error: 'content' must be a string"
        if len(content) > _WRITE_CAP_CHARS:
            return f"error: content too large ({len(content)} chars > {_WRITE_CAP_CHARS})"
        resolved = _resolve_writable(root, path, contract)
        if isinstance(resolved, str):
            return resolved
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("devagent_tools.write_failed", path=path, error=str(exc)[:120])
            return f"error: could not create parent of {path!r}: {type(exc).__name__}"
        error = _write_text_safely(resolved, content)
        if error:
            _log.warning("devagent_tools.write_failed", path=path, error=error[:120])
            return error
        _log.info("devagent_tools.write_file", path=path, chars=len(content))
        return f"ok: wrote {len(content)} chars to {path}"

    def edit_file(path: str, edits: list[dict[str, str]]) -> str:
        """Apply ordered anchored ``{search, replace}`` edits to an existing file."""
        if not isinstance(edits, list):
            return "error: 'edits' must be a list of {search, replace} objects"
        resolved = _resolve_writable(root, path, contract)
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"error: no such file to edit: {path!r} (use write_file to create it)"
        try:
            current = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"error: unreadable file {path!r}: {type(exc).__name__}"
        new_content, report = apply_search_replace_edits(current, edits)
        if new_content is None:
            return f"error: {report}"
        error = _write_text_safely(resolved, new_content)
        if error:
            return error
        _log.info("devagent_tools.edit_file", path=path, n_edits=len(edits))
        return f"ok: applied {len(edits)} edit(s) to {path}"

    def run_tests(target: str = _DEFAULT_TEST_TARGET) -> str:
        """Run pytest on a repo-relative target (default the unit suite)."""
        resolved = _resolve_in_repo(root, target)
        if isinstance(resolved, str):
            return resolved
        argv = ["python", "-m", "pytest", "-q", "--", target]
        try:
            proc = subprocess.run(
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_TEST_TIMEOUT_S,
                check=False,
                env=scrubbed_env(),
            )
        except FileNotFoundError as exc:
            return f"error: pytest binary not found: {exc}"
        except subprocess.TimeoutExpired:
            return f"error: tests timed out after {_TEST_TIMEOUT_S}s on {target!r}"
        tail = (proc.stdout + "\n" + proc.stderr)[-_TEST_TAIL_CAP_CHARS:]
        verdict = "PASS" if proc.returncode == 0 else "FAIL"
        _log.info("devagent_tools.run_tests", target=target, verdict=verdict)
        return f"{verdict} (pytest {target})\n{tail}"

    def run_ruff() -> str:
        """Run the repo's ruff gate (lint + format check, as CI does)."""
        passed, tail = run_ruff_gate(root)
        verdict = "PASS" if passed else "FAIL"
        _log.info("devagent_tools.run_ruff", verdict=verdict)
        return f"{verdict} (ruff)\n{tail[-_TEST_TAIL_CAP_CHARS:]}"

    return [
        ToolDef(
            name="write_file",
            description=(
                "Create or overwrite a whole repo file with the given content "
                "(parent dirs are created). Use for authoring new files or full rewrites."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "content": {"type": "string", "description": "Full file contents."},
                },
                "required": ["path", "content"],
            },
            callable_fn=write_file,
        ),
        ToolDef(
            name="edit_file",
            description=(
                "Apply ordered anchored edits to an existing file. Each edit is "
                "{search, replace}; search must occur exactly once (copy it verbatim)."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "search": {"type": "string"},
                                "replace": {"type": "string"},
                            },
                            "required": ["search", "replace"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            callable_fn=edit_file,
        ),
        ToolDef(
            name="run_tests",
            description=(
                "Run pytest on a repo-relative target (a test file/dir, default "
                "'tests/unit'). Returns PASS/FAIL and the captured tail."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Repo-relative pytest target (default tests/unit).",
                    }
                },
            },
            callable_fn=run_tests,
        ),
        ToolDef(
            name="run_ruff",
            description="Run the repo's ruff lint+format gate. Returns PASS/FAIL and the tail.",
            parameters_schema={"type": "object", "properties": {}},
            callable_fn=run_ruff,
        ),
    ]


__all__ = ["make_developer_tools", "normalise_contract"]
