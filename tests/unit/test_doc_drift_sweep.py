"""Tests for SP-DOC-DRIFT-SWEEP: three corrected docstrings agree with the
shipped code they document.

Doc-only spec — no behavior changed. This suite asserts the false claims
audited 2026-07-13 are gone and the docstrings now state the real policy,
then drives the actual runtime objects to prove the corrected claims hold.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from repoach.agent_engine.agent_loop import NimAgentOutput
from repoach.lint.no_inline_comments import scan_file
from repoach.review import inline_comment_heal, mcp_whitelist
from repoach.review.mcp_whitelist import allowed_tools_for
from repoach.review.reviewer import BotRole


def test_docstrings_match_shipped_policy() -> None:
    """AC1: the three corrected docstrings no longer contain the false
    claims audited 2026-07-13 and each states the truthful replacement.

    - D1 ``mcp_whitelist`` module docstring: drops "every role maps to
      an empty tuple", now names ``git_log`` for Coder/Developer.
    - D2 ``NimAgentOutput.trace`` attribute docstring: drops the dead
      "transport-level stub" fallback.
    - D3 ``inline_comment_heal`` module docstring: drops "left for the
      gate to reject" for standalone comments, now states the gate
      implicitly allows them.
    """
    assert mcp_whitelist.__doc__ is not None
    assert "every role maps to an empty tuple" not in mcp_whitelist.__doc__
    assert "git_log" in mcp_whitelist.__doc__

    assert NimAgentOutput.__doc__ is not None
    assert "transport-level stub" not in NimAgentOutput.__doc__

    assert inline_comment_heal.__doc__ is not None
    assert "left for the gate to reject" not in inline_comment_heal.__doc__
    assert "implicitly allows them" in inline_comment_heal.__doc__


def test_allowed_tools_for_coder_matches_corrected_docstring_claim() -> None:
    """AC2: the real whitelist lookup backs the corrected mcp_whitelist claim."""
    assert "git_log" in allowed_tools_for(BotRole.CODER)
    assert "git_log" in allowed_tools_for(BotRole.DEVELOPER)


def test_gate_allows_standalone_comment_backing_corrected_heal_docstring() -> None:
    """AC2: the real gate scanner allows a lone standalone comment.

    Proves the ``inline_comment_heal`` docstring's corrected claim —
    "the gate itself ... implicitly allows them" — against the live
    scanner, not a paraphrase.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "standalone_comment.py"
        target.write_text("x = 1\n# a lone standalone comment\n")
        violations = scan_file(target)
        assert violations == []
