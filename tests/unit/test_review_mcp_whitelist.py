"""Tests for the per-role MCP tool whitelist (SP-MCP-EXT-A/B).

Module-level only — no imports from ``Reviewer`` / ``Coder`` /
``Developer``.  Their wiring is the next spec (SP-MCP-EXT-B);
keeping this suite import-light avoids cascading failures from
unrelated state when the whitelist module is loaded.
"""

from __future__ import annotations

import pytest

from ferova.review.mcp_whitelist import (
    MCP_TOOL_WHITELIST_BY_ROLE,
    allowed_tools_for,
)
from ferova.review.reviewer import BotRole

#: Read-only MCP tools available to inspect the working tree / persisted
#: state without touching the outside world.  Reviewers + Coder + Developer
#: get a curated subset of these; mutating tools (``proxy_restart``,
#: ``run_ferova_command``…) are explicitly excluded.
_READ_ONLY_TOOLS = frozenset(
    {
        "git_status",
        "git_log",
        "proxy_status",
    }
)

#: Tools whose execution has visible side effects (sends, mutations,
#: process restarts).  Forbidden for any review-bot role.
_MUTATING_TOOLS = (
    "send_whatsapp",
    "mcp__ferova__send_whatsapp",
    "proxy_restart",
    "git_open_pr",
    "run_ferova_command",
)

#: Bot roles whose threat model is "process a diff" — they must never
#: hold a mutating MCP tool.
_REVIEW_BOT_ROLES = (
    BotRole.ARCHITECT,
    BotRole.SENTINEL,
    BotRole.TESTER,
    BotRole.SCRIBE,
    BotRole.CODER,
    BotRole.DEVELOPER,
    BotRole.PLANNER,
)


def test_planner_holds_no_mcp_tools() -> None:
    """SP-PLANNER-AGENT: the Planner's exploration tools are local
    repo-jailed ToolDefs, not MCP tools — its MCP row stays empty by
    deliberate decision (fail-closed)."""
    assert allowed_tools_for(BotRole.PLANNER) == ()


def test_allowed_tools_for_known_role_returns_tuple() -> None:
    """``allowed_tools_for(BotRole.CODER)`` returns a tuple, not ``None``."""
    out = allowed_tools_for(BotRole.CODER)
    assert isinstance(out, tuple)


def test_allowed_tools_for_unknown_value_returns_empty() -> None:
    """Non-BotRole values fall through to ``()`` for safety."""
    out = allowed_tools_for("not-a-role")
    assert out == ()


def test_whitelist_uses_botrole_enum_keys() -> None:
    """All keys are BotRole instances — typing strict, no string drift."""
    for key in MCP_TOOL_WHITELIST_BY_ROLE:
        assert isinstance(key, BotRole), f"non-BotRole key snuck in: {key!r}"


@pytest.mark.parametrize("hostile", _MUTATING_TOOLS)
def test_review_bot_roles_do_not_grant_mutating_mcp_tools(hostile: str) -> None:
    """No review-bot role grants any mutating MCP tool name (regression-guard).

    A future contributor MUST NOT inadvertently grant any mutating
    tool to a review bot — the threat model explicitly excludes that
    surface.
    """
    for role in _REVIEW_BOT_ROLES:
        tools = allowed_tools_for(role)
        assert hostile not in tools, f"role {role.value!r} grants forbidden tool {hostile!r}"


def test_whitelist_covers_every_botrole_explicitly() -> None:
    """Every BotRole is listed in the whitelist (no implicit defaults).

    Defence in depth: if a new BotRole is added without updating the
    whitelist, this test fails — forcing the contributor to make a
    deliberate decision about the new role's MCP tool exposure.
    """
    declared = set(MCP_TOOL_WHITELIST_BY_ROLE.keys())
    enum_members = set(BotRole)
    missing = enum_members - declared
    assert not missing, f"BotRole(s) missing from whitelist: {missing}"


def test_review_bot_roles_only_grant_read_only_tools() -> None:
    """Architect/Sentinel/Tester/Scribe/Coder/Developer hold only read-only tools.

    Any tool a review bot can invoke must be in the read-only catalogue.
    Adding a new tool to a review-bot row REQUIRES adding it to
    ``_READ_ONLY_TOOLS`` first (deliberate, reviewable opt-in).
    """
    for role in _REVIEW_BOT_ROLES:
        tools = set(allowed_tools_for(role))
        leaked = tools - _READ_ONLY_TOOLS
        assert not leaked, (
            f"role {role.value!r} grants non-read-only tool(s): {leaked} — "
            "either add them to _READ_ONLY_TOOLS or remove them from the row"
        )


# ---------------------------------------------------------------------------
# SP-MCP-EXT-B: instance-level _allowed_tools attribute on each bot
# ---------------------------------------------------------------------------


def test_reviewer_exposes_allowed_tools_attribute() -> None:
    """Architect's __init__ wires self._allowed_tools from the whitelist."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Architect

    architect = Architect(loop=MagicMock())
    assert architect._allowed_tools == allowed_tools_for(BotRole.ARCHITECT)


def test_coder_exposes_allowed_tools_attribute() -> None:
    """Coder's __init__ wires self._allowed_tools from the whitelist."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Coder

    coder = Coder(loop=MagicMock())
    assert coder._allowed_tools == allowed_tools_for(BotRole.CODER)


def test_developer_exposes_allowed_tools_attribute() -> None:
    """Developer's __init__ wires self._allowed_tools from the whitelist."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Developer

    developer = Developer(loop=MagicMock())
    assert developer._allowed_tools == allowed_tools_for(BotRole.DEVELOPER)
