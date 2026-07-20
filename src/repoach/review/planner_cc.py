"""Delegated-exploration Planner backend — one ``claude -p`` session.

SP-PLANNER-CC-EXPLORE (option 3 of the builder work-stream): instead of
driving the AgentLoop ↔ local-tools loop over the NIM/coder proxy
chain, run a single ``claude -p`` session in the repository with the
CLI's NATIVE read-only tools (Read/Glob/Grep/LS) on the operator's
Max subscription. The Planner reasons at full Claude quality with
first-class exploration and the CLI's own session cache.

This is an EXPLICIT Planner mode, never a transparent chain link: the
execution model (the CLI drives its own exploration) and the audit
trail (tool calls happen inside the CLI, not the AgentLoop) differ
from the proxy path, so it is opt-in per invocation.

Isolation (SP-PLANNER-CC-ISOLATE): the session runs from a fresh,
empty scratch ``cwd`` — NEVER the project root — with the repo reached
only through a single read-only ``--add-dir``. The brain-swap
experiment (2026-06-09) showed that running ``claude -p`` with
``cwd=repo`` makes it a FULL in-project Claude Code agent: it loaded
this project's ``CLAUDE.md`` + auto-memory + the "Dream Mode" memory
routine, abandoned the planning task to consolidate memory, and
REWROTE the operator's durable memory files — the read-only tool
allowlist does not gate the memory subsystem. A neutral scratch cwd
loads no project context (verified: ``loaded_project_memory: false``,
memory mtimes unchanged) while ``--add-dir`` keeps repo reads working.
The model is told the repo's absolute path so it explores there
despite the empty cwd.

Security: the CLI is handed a hard read-only tool allowlist
(:data:`_CC_READ_ONLY_TOOLS`) and the repo as a single ``--add-dir``.
It can never write, edit, or run shell — only read what is already in
the working tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..core.logging import get_logger

_log = get_logger(__name__)

_CC_READ_ONLY_TOOLS: str = "Read,Glob,Grep,LS"
_CC_TIMEOUT_S: int = 600


def _scrubbed_env() -> dict[str, str]:
    """Return the parent environment with this app's ``FEROVA_*`` stripped.

    The ``claude`` CLI authenticates via its own Max session and never
    needs Repoach's config or provider API keys; passing them to the
    child would needlessly widen the secret surface. Everything else
    (``HOME``, ``PATH``, the CLI's own auth) is preserved so the CLI
    still runs.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("FEROVA_")}


@dataclass
class CcExploreResult:
    """Outcome of one :func:`run_cc_exploration` call.

    Attributes:
        text: The CLI's final assistant message (the envelope's
            ``result`` field) — carries the JSON plan on success.
        num_turns: Tool-call round-trips the CLI made.
        duration_ms: Wall-clock the CLI reported.
        is_error: Whether the call failed (transport, timeout,
            non-JSON envelope, or the CLI's own ``is_error``).
        error: Short failure description when ``is_error``; ``None``
            on success.
    """

    text: str
    num_turns: int
    duration_ms: int
    is_error: bool
    error: str | None = None


def _fail(message: str) -> CcExploreResult:
    """Build a failed :class:`CcExploreResult`, logging the cause.

    Every failure path funnels through here so a delegated exploration
    that dies (timeout, spawn error, non-JSON envelope, the CLI's own
    ``is_error``) leaves a WARNING in the log rather than a silent
    structured return.
    """
    _log.warning("planner_cc.failed", error=message)
    return CcExploreResult(text="", num_turns=0, duration_ms=0, is_error=True, error=message)


def run_cc_exploration(
    *,
    prompt: str,
    repo_root: Path,
    model: str,
    allow_tools: bool = True,
    cli_path: str | None = None,
    timeout_s: int = _CC_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> CcExploreResult:
    """Run one read-only ``claude -p`` session and return its final text.

    Args:
        prompt: The full prompt handed to ``claude -p``.
        repo_root: Repository root reached through the read-only
            ``--add-dir``; the subprocess cwd is an isolated scratch
            dir, never this path, so no project context loads.
        model: CLI model alias (``"sonnet"`` / ``"opus"`` / ``"haiku"``).
        allow_tools: When ``True`` (exploration), pass the read-only
            tool allowlist and ``--add-dir``, and orient the model to
            the repo's absolute path. When ``False`` (refinement),
            pass neither — the model only reshapes text it already
            produced, no repo access needed.
        cli_path: Override the ``claude`` executable (tests).
        timeout_s: Hard cap on the subprocess.
        env: Optional environment for the subprocess. ``None`` (the
            default) hands the CLI a :func:`_scrubbed_env` — the parent
            environment minus this app's ``FEROVA_*`` secrets, which the
            CLI never needs. Pass an explicit dict to override.

    Returns:
        A :class:`CcExploreResult`. Never raises — every failure mode
        is reported via ``is_error`` so the caller's parse/retry loop
        stays in control.
    """
    claude = cli_path or shutil.which("claude") or "claude"
    child_env = env if env is not None else _scrubbed_env()
    full_prompt = prompt
    cmd = [claude, "-p"]
    if allow_tools:
        full_prompt = (
            f"The repository you must explore is rooted at {repo_root}. Use your "
            f"Read/Glob/Grep/LS tools on paths under {repo_root} to inspect it. Your "
            "current working directory is an empty scratch directory — ignore it and "
            "do not look for project files there.\n\n" + prompt
        )
        cmd += [full_prompt, "--allowedTools", _CC_READ_ONLY_TOOLS, "--add-dir", str(repo_root)]
    else:
        cmd += [full_prompt]
    cmd += ["--permission-mode", "default", "--output-format", "json", "--model", model]

    _log.info(
        "planner_cc.spawn",
        model=model,
        allow_tools=allow_tools,
        prompt_chars=len(full_prompt),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="repoach_planner_cc_") as workdir:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env=child_env,
            )
    except subprocess.TimeoutExpired:
        return _fail(f"claude -p timed out after {timeout_s}s")
    except OSError as exc:
        return _fail(f"claude -p could not be spawned: {exc}")

    if proc.returncode != 0:
        return _fail(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:200]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _fail(f"claude emitted a non-JSON envelope: {proc.stdout[:200]}")
    if not isinstance(envelope, dict):
        return _fail(f"claude envelope is not an object: {str(envelope)[:160]}")
    if envelope.get("is_error"):
        return _fail(f"claude reported is_error: {str(envelope.get('result'))[:200]}")

    result = CcExploreResult(
        text=str(envelope.get("result") or ""),
        num_turns=int(envelope.get("num_turns") or 0),
        duration_ms=int(envelope.get("duration_ms") or 0),
        is_error=False,
    )
    _log.info(
        "planner_cc.done",
        model=model,
        num_turns=result.num_turns,
        duration_ms=result.duration_ms,
        text_chars=len(result.text),
    )
    return result
