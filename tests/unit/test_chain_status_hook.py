"""Pin the chain-status SessionStart hook contract: digest must be fail-open.

The tracked .claude/settings.json SessionStart hooks must include a
chain-status command guarded by ``|| true`` so a broken venv can never
block a Claude session (G4 of SP-CHAIN-STATUS-DIGEST). This test
follows the repo-file-assertion pattern of test_dream_check_hook.py.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = _REPO / ".claude" / "settings.json"


def test_session_start_hook_includes_chain_status_command() -> None:
    """The tracked settings.json SessionStart hooks must contain a
    chain-status command guarded by ``|| true`` for fail-open semantics.
    """
    raw = _SETTINGS.read_text(encoding="utf-8")
    doc = json.loads(raw)

    session_start = doc["hooks"]["SessionStart"]
    assert isinstance(session_start, list)
    assert len(session_start) > 0

    all_commands: list[str] = []
    for entry in session_start:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            all_commands.append(cmd)

    chain_cmds = [c for c in all_commands if "chain-status" in c]
    assert len(chain_cmds) > 0, "SessionStart hooks must include a chain-status command"
    for cmd in chain_cmds:
        assert "|| true" in cmd, f"chain-status hook must be fail-open with || true, got: {cmd!r}"
