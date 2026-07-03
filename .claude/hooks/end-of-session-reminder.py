#!/usr/bin/env python3
"""UserPromptSubmit hook: inject end-of-session protocol reminder.

When the user signals we are stopping ("on s'arrête", "fin de session", "that's
it for today", etc.), this hook adds a system reminder restating the 4-step
end-of-session protocol from CLAUDE.md so Claude runs it BEFORE summarizing.

The memory directory is computed at runtime from $CLAUDE_PROJECT_DIR — same
slug logic as dream_check.py.

Non-matching prompts pass through untouched (exit 0, no output).

Exit codes:
  0 = pass through (with optional additionalContext in JSON stdout)
"""

import json
import os
import re
import sys
from pathlib import Path

TRIGGERS = [
    r"on s[''`]arr[eê]te(?:\s+l[aà])?",
    r"on arr[eê]te(?:\s+l[aà])?",
    r"stop(?:pe)?\s+l[aà]\b",
    r"on stoppe(?:\s+l[aà])?",
    r"fin de (?:la )?session",
    r"\bthat'?s it for (?:today|now)\b",
    r"\bwe(?:'re| are) stopping\b",
    r"\bend of session\b",
    r"\blet'?s stop\b",
    r"\bwrapping up\b",
    r"\bwe are done\b",
    r"\bwe're done\b",
]

TRIGGER_RE = re.compile("|".join(TRIGGERS), re.IGNORECASE)


def _resolve_memory_dir() -> str:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return "memory/"
    slug = project_dir.replace("/", "-").replace("_", "-")
    return str(Path.home() / ".claude" / "projects" / slug / "memory")


def _build_reminder() -> str:
    memory_dir = _resolve_memory_dir()
    return f"""END-OF-SESSION PROTOCOL (from CLAUDE.md) — run BEFORE your summary:

1. Update memory at {memory_dir}/
   - Record non-obvious decisions, blockers, or load-bearing context from this session
     as user/feedback/project/reference memories per the rules in system prompt
   - Update MEMORY.md index entries for anything new

2. Update documentation if necessary
   - If code/architecture was changed, sweep the relevant parts of CLAUDE.md and
     other docs only when warranted
   - Do NOT create new *.md files unless explicitly asked

3. Leave tasks clean: mark in-progress TaskList items completed or move them to
   follow-up memories

4. THEN give the end-of-turn summary (one or two sentences)

Do not skip these steps."""


def main() -> int:
    """Inject the end-of-session protocol when the prompt signals a stop."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt", "") or ""
    if not TRIGGER_RE.search(prompt):
        return 0

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _build_reminder(),
        }
    }
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
