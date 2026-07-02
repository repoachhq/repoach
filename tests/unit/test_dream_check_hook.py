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
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
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


def _run_main(home: Path, *, sessions: int, state: str | None) -> subprocess.CompletedProcess[str]:
    """Run the hook subprocess against a fabricated HOME + project slug."""
    memory = home / ".claude" / "projects" / "-fake-proj" / "memory"
    memory.mkdir(parents=True)
    if state is not None:
        (memory / ".dream_state.json").write_text(state, encoding="utf-8")
    for i in range(sessions):
        (memory.parent / f"s{i}.jsonl").write_text("{}", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "CLAUDE_PROJECT_DIR": "/fake/proj"},
        timeout=30,
    )


def test_main_emits_reminder_when_due(tmp_path: Path) -> None:
    """Never dreamed + enough sessions -> the stdout reminder fires."""
    result = _run_main(tmp_path, sessions=6, state=None)
    assert result.returncode == 0
    assert "[dream-mode]" in result.stdout
    assert "/dream" in result.stdout


def test_main_stays_silent_after_a_recent_dream(tmp_path: Path) -> None:
    """A dream inside the 24h window suppresses the reminder."""
    recent = json.dumps({"last_dream": datetime.now(UTC).isoformat()})
    result = _run_main(tmp_path, sessions=6, state=recent)
    assert result.returncode == 0
    assert result.stdout == ""


def test_main_stays_silent_below_the_session_threshold(tmp_path: Path) -> None:
    """Fewer than MIN_SESSIONS_BETWEEN_DREAMS sessions -> no reminder."""
    result = _run_main(tmp_path, sessions=2, state=None)
    assert result.returncode == 0
    assert result.stdout == ""


def test_main_survives_a_malformed_last_dream(tmp_path: Path) -> None:
    """A corrupt timestamp degrades to never-dreamed instead of raising."""
    result = _run_main(tmp_path, sessions=6, state='{"last_dream": "not-a-date"}')
    assert result.returncode == 0
    assert "[dream-mode]" in result.stdout
