"""The Developer's agentic author→test→iterate loop (SP-DEVAGENT-LOOP).

Slice 2 of the real-coding-agent arc (see ``docs/devagent_architecture.md``).
Where slice 1 (:mod:`review.devagent_tools`) gave the Developer the hands that
change and check the tree, this module is the spine that *uses* them: it assembles
the read-only exploration toolbox (:func:`review.planner_tools.make_planner_tools`)
with the author/verify toolbox (:func:`review.devagent_tools.make_developer_tools`,
jailed to the step's file contract) and drives :meth:`AgentLoop.run` for a genuine
multi-turn loop — read → write/edit → run_tests/run_ruff → fix forward until green.

This mirrors the proven Planner pattern (:meth:`Planner._plan_via_proxy` over
read-only tools): a brain/infra outage (every chain candidate empty-completing, a
transport error, the chain exhausting) is caught and returned as a loud, inert
:class:`DevLoopResult` with ``error`` set — never propagated as a crash — so the
orchestrator treats it as a failed step rather than dying.

The loop is the *authoring* engine only. Whether the result actually satisfies the
spec (mechanical AC-selector presence + an LLM judge) is SP-DEVAGENT-SELFVERIFY;
the authoritative mechanical re-run of the gates after the loop lives in
:func:`review.dev_runner.execute_plan_step`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_engine.adapters import GatewayError
from ..agent_engine.agent_loop import AgentLoop
from ..core.logging import get_logger
from .devagent_tools import make_developer_tools
from .planner_tools import make_planner_tools

_log = get_logger(__name__)


@dataclass
class DevLoopResult:
    """Outcome of one agentic step run.

    Attributes:
        text: The model's final plain-text summary of what it changed.
        turns: Number of model round-trips the loop took.
        tokens_used: Approximate prompt + completion tokens across the loop.
        elapsed_s: Wall-clock duration of the loop.
        model_used: The upstream model the proxy chain landed on.
        tool_calls_made: Names of every tool the model invoked, in order.
        error: ``None`` on a completed loop; a short error string when the brain
            or proxy failed (the loop never ran to a final answer). The on-disk
            tree reflects whatever the model wrote before the failure.
    """

    text: str = ""
    turns: int = 0
    tokens_used: int = 0
    elapsed_s: float = 0.0
    model_used: str = ""
    tool_calls_made: list[str] = field(default_factory=list)
    error: str | None = None


def run_agentic_step(
    loop: AgentLoop,
    *,
    system: str,
    brief: str,
    repo_root: Path,
    allowed_paths: Iterable[str],
    repo_tree: str = "",
) -> DevLoopResult:
    """Drive one step of work through the AgentLoop ↔ author/verify tools loop.

    Assembles the toolbox the Developer needs — the read-only exploration tools
    (unrestricted, so the agent can read any file for context) composed with the
    author/verify tools jailed to ``allowed_paths`` (the step's file contract, so
    a write outside it is refused in-loop and the model self-corrects) — and runs
    :meth:`AgentLoop.run` over it.

    Args:
        loop: The Developer's pre-built :class:`AgentLoop` (SONNET tier).
        system: The rendered agentic persona, used as the system prompt.
        brief: The per-step brief (the first user message).
        repo_root: Repository root every tool is jailed to.
        allowed_paths: The step's writable file contract.
        repo_tree: Optional pre-rendered tree appended to the brief for
            orientation (the agent can also explore with ``list_dir``).

    Returns:
        A :class:`DevLoopResult`. On a proxy/chain failure the result has
        ``error`` set and the other fields left at their defaults.
    """
    allowed_paths = tuple(allowed_paths)
    tools = make_planner_tools(repo_root) + make_developer_tools(
        repo_root, allowed_paths=allowed_paths
    )
    user_prompt = brief
    if repo_tree:
        user_prompt = f"{brief}\n\n## Repository tree (orientation)\n```\n{repo_tree}\n```"
    _log.info(
        "devagent_loop.run_started",
        n_tools=len(tools),
        allowed_paths=sorted(set(allowed_paths)),
    )
    try:
        output = loop.run(user_prompt, system=system, tools=tools)
    except GatewayError as exc:
        _log.warning("devagent_loop.proxy_failed", error=str(exc)[:200])
        return DevLoopResult(error=f"proxy agentic loop failed: {str(exc)[:200]}")
    _log.info(
        "devagent_loop.run_done",
        turns=output.turns,
        tokens_used=output.tokens_used,
        model_used=output.model_used,
        n_tool_calls=len(output.tool_calls_made),
    )
    return DevLoopResult(
        text=output.text or "",
        turns=output.turns,
        tokens_used=output.tokens_used,
        elapsed_s=output.elapsed_s,
        model_used=output.model_used,
        tool_calls_made=list(output.tool_calls_made),
    )


__all__ = ["DevLoopResult", "run_agentic_step"]
