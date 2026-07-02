#!/usr/bin/env python3
"""SessionStart hook: REMIND when a Dream Mode consolidation is due.

Checks two conditions:
  1. At least 24h since the last dream
  2. At least 5 new sessions since the last dream

If both are met, emits a SHORT reminder to stdout nudging the operator
to run ``/dream`` when convenient. It NEVER triggers the consolidation
itself — the reflective pass lives in the ``/dream`` slash command
(``.claude/commands/dream.md``) and runs only on deliberate invocation.

This separation (reminder vs. auto-trigger) means the dream can never
hijack a non-interactive ``claude -p`` session: a headless subprocess
that receives this reminder sees only a one-line nudge, not a
full-consolidation prompt that would replace its task — the failure
mode the 2026-06-09 brain-swap experiment exposed, where the old
auto-trigger made an in-project ``claude -p`` Planner abandon planning
and rewrite the operator's durable memory.

Always exits 0 — never blocks session start.
"""

import glob
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOURS_BETWEEN_DREAMS = 24
MIN_SESSIONS_BETWEEN_DREAMS = 5


def _resolve_paths():
    """Resolve memory dir and sessions dir from CLAUDE_PROJECT_DIR."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return None, None

    home = Path.home()
    slug = project_dir.replace("/", "-").replace("_", "-")
    sessions_dir = home / ".claude" / "projects" / slug
    memory_dir = sessions_dir / "memory"
    return memory_dir, sessions_dir


def _load_state(state_path):
    """Load dream state, returning defaults if missing or corrupt."""
    default = {"last_dream": None, "last_dream_session_count": 0, "dreams": []}
    if not state_path.exists():
        return default
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _count_new_sessions(sessions_dir, since_timestamp):
    """Count JSONL session files newer than the given Unix timestamp."""
    session_files = glob.glob(str(sessions_dir / "*.jsonl"))
    return sum(1 for f in session_files if os.path.getmtime(f) > since_timestamp)


def main():
    """Emit the ``/dream`` reminder to stdout when a dream is due.

    Resolves the project's memory + sessions dirs, applies the
    24h-and-5-sessions threshold, and prints :data:`DREAM_REMINDER`
    when both hold. Returns silently (no output) otherwise, and never
    raises — a SessionStart hook must not block session start.
    """
    memory_dir, sessions_dir = _resolve_paths()
    if not memory_dir or not sessions_dir:
        return

    state_path = memory_dir / ".dream_state.json"
    state = _load_state(state_path)
    now = datetime.now(UTC)

    last_dream = state.get("last_dream")
    if last_dream:
        last_dt = datetime.fromisoformat(last_dream)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        if (now - last_dt) < timedelta(hours=HOURS_BETWEEN_DREAMS):
            return
        since_ts = last_dt.timestamp()
    else:
        since_ts = 0

    new_sessions = _count_new_sessions(sessions_dir, since_ts)
    if new_sessions < MIN_SESSIONS_BETWEEN_DREAMS:
        return

    memory_file_count = len(glob.glob(str(memory_dir / "*.md")))

    print(
        DREAM_REMINDER.format(
            new_sessions=new_sessions,
            memory_files=memory_file_count,
            last_dream=last_dream or "never",
        ),
        file=sys.stdout,
    )


DREAM_REMINDER = """\
[dream-mode] A memory dream is due — {new_sessions} sessions since the last on \
{last_dream}, {memory_files} memory files. Run `/dream` when convenient for a \
reflective consolidation pass (merge redundant memories, prune stale facts, \
refresh the index). This is a reminder only; nothing runs automatically."""


if __name__ == "__main__":
    main()
