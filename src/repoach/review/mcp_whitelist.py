"""Per-role MCP tool whitelist for review bots (SP-MCP-EXT-A).

The :data:`MCP_TOOL_WHITELIST_BY_ROLE` mapping is the single source of
truth for "which MCP tools is each review bot allowed to call".  The
lookup is **fail-closed by default**: :func:`allowed_tools_for` returns
an empty tuple for any role absent from the mapping.  Within the
mapping, Architect / Sentinel / Tester / Scribe are each granted
``git_status``; Coder and Developer are additionally granted
``git_log``; Planner is granted no tools (``()``).

This module ships only the lookup mechanism — wiring into the bot
``__init__`` methods is SP-MCP-EXT-B; a prompt-injection regression
guard test is SP-MCP-EXT-C.

Threat model rationale: exposing any mutating MCP tool to an agent
that processes a diff is a direct prompt-injection vector — a hostile
diff could ask the bot to call the tool.  Keeping the whitelist as a
Python constant (not a function of any prompt input) makes it immune
to that class of attack.

Module-level constants :
    :data:`MCP_TOOL_WHITELIST_BY_ROLE` maps each :class:`BotRole` to a
    tuple of allowed MCP tool names.  An empty tuple means the role
    has access to no MCP tools — the safe default.  Extending a row
    is a deliberate, reviewable opt-in for a specific bot + tool pair.
"""

from __future__ import annotations

from collections.abc import Mapping

from .reviewer import BotRole

MCP_TOOL_WHITELIST_BY_ROLE: Mapping[BotRole, tuple[str, ...]] = {
    BotRole.ARCHITECT: ("git_status",),
    BotRole.SENTINEL: ("git_status",),
    BotRole.TESTER: ("git_status",),
    BotRole.SCRIBE: ("git_status",),
    BotRole.CODER: (
        "git_status",
        "git_log",
    ),
    BotRole.DEVELOPER: (
        "git_status",
        "git_log",
    ),
    BotRole.PLANNER: (),
}


def allowed_tools_for(role: BotRole) -> tuple[str, ...]:
    """Return the MCP tool whitelist for *role*.

    Args:
        role: The bot role to look up.  Accepts :class:`BotRole` only;
            non-enum values fall through to ``()`` for safety.

    Returns:
        A tuple of allowed MCP tool names.  Empty tuple means the bot
        has access to no MCP tools — the secure default.
    """
    return MCP_TOOL_WHITELIST_BY_ROLE.get(role, ())
