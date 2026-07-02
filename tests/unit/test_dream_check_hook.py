"""Pin the Dream Mode hook contract: REMIND, never auto-trigger.

The SessionStart hook used to inject the full consolidation prompt,
which an in-project ``claude -p`` session would EXECUTE — abandoning
its task and rewriting the operator's durable memory (brain-swap
experiment, 2026-06-09). The hook now only emits a short reminder; the
consolidation lives in the deliberate ``/dream`` command. These tests
load the hook module by path and lock that contract so the
auto-trigger cannot quietly return.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_HOOK = _REPO / ".claude" / "hooks" / "dream_check.py"
_COMMAND = _REPO / ".claude" / "commands" / "dream.md"


def _load_hook():
    spec = importlib.util.spec_from_file_location("dream_check_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_emits_a_reminder_not_the_full_prompt() -> None:
    hook = _load_hook()
    assert hasattr(hook, "DREAM_REMINDER")
    assert not hasattr(hook, "DREAM_PROMPT")
    rendered = hook.DREAM_REMINDER.format(new_sessions=9, last_dream="X", memory_files=42)
    assert "/dream" in rendered
    assert "reminder only" in rendered
    assert "<dream-mode>" not in rendered
    assert "INSTRUCTIONS" not in rendered


def test_hook_keeps_its_trigger_thresholds() -> None:
    hook = _load_hook()
    assert hook.HOURS_BETWEEN_DREAMS == 24
    assert hook.MIN_SESSIONS_BETWEEN_DREAMS == 5


def test_dream_command_exists_with_consolidation_steps() -> None:
    text = _COMMAND.read_text(encoding="utf-8")
    assert "Dream Mode" in text
    assert "never automatically" in text
    for token in ("MEMORY.md", ".dream_state.json", "feedback_", "CLAUDE.md"):
        assert token in text
