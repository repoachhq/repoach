#!/usr/bin/env python3
"""PreToolUse hook: enforce ferova's PR-only protected-branch policy.

Ferova adaptation of the sharp-agent original (operator-approved
2026-07-02). Rules — mirrors .githooks/pre-push, but fires at the
Claude tool layer even when core.hooksPath is not configured:
  1. NO direct commit on main / master / develop. Create a feature
     branch first (`git checkout -b feat/<spec-id>`, `fix/<name>`,
     `chore/<name>`...) and land it via PR against develop.
  2. NO push whose refspec mentions main / master / develop, from any
     branch (`git push origin develop`, `HEAD:main`, ...), and no bare
     `git push` while standing on a protected branch.
  3. NO hard reset to a protected branch ref.

Bypass: set ``ALLOW_MAIN_COMMIT=1`` in the environment (rare hot-fix
cases — same convention as the sharp-agent original; also clears the
false positive of a feature branch whose name contains a protected
word).

Exit codes:
  0 = allow
  2 = block (prints reason to stderr)
"""

import json
import os
import re
import subprocess
import sys

PROTECTED_BRANCHES = {"main", "master", "develop"}
PROTECTED_REF_RE = re.compile(r"\bmain\b|\bmaster\b|\bdevelop\b")


def _current_branch() -> str | None:
    try:
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _block(reason: str, suggestion: str) -> None:
    print(f"BLOCKED: {reason}\n  {suggestion}", file=sys.stderr)
    sys.exit(2)


def _is_commit_command(cmd: str) -> bool:
    return bool(re.search(r"(?:^|[\s;&|])git\s+commit\b", cmd))


def _is_push_command(cmd: str) -> bool:
    return bool(re.search(r"(?:^|[\s;&|])git\s+push\b", cmd))


def _is_hard_reset_to_protected(cmd: str) -> bool:
    return bool(
        re.search(
            r"git\s+reset\s+--hard\s+(?:origin/)?(?:main|master|develop)\b",
            cmd,
        )
    )


def main():
    """Read the PreToolUse payload and block protected-branch mutations."""
    if os.environ.get("ALLOW_MAIN_COMMIT") == "1":
        return

    hook_input = json.loads(sys.stdin.read())
    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "").strip()
    if not command:
        return

    branch = _current_branch()
    on_protected = branch in PROTECTED_BRANCHES

    if _is_commit_command(command) and on_protected:
        _block(
            f"direct commit on protected branch '{branch}'",
            "Create a feature branch first:\n"
            "    git checkout -b feat/<spec-id>   # or fix/<name>, chore/<name>\n"
            "  Then commit and open a PR against develop.\n"
            "  (Set ALLOW_MAIN_COMMIT=1 for a genuine hot-fix — rare.)",
        )

    if _is_push_command(command):
        if on_protected:
            _block(
                f"push while standing on protected branch '{branch}'",
                "Protected branches are PR-only (develop) or human-merge-only\n"
                "  (main). Push a feature branch instead and merge via\n"
                "  scripts/safe_merge.sh.",
            )
        if PROTECTED_REF_RE.search(command):
            _block(
                "push refspec mentions a protected branch (main/develop)",
                "Protected branches are PR-only. Push the feature branch:\n"
                "    git push -u origin <feature-branch>\n"
                "  then merge via PR (scripts/safe_merge.sh for develop).\n"
                "  (Branch name merely containing 'main'/'develop'? Set\n"
                "  ALLOW_MAIN_COMMIT=1 for this one command.)",
            )

    if _is_hard_reset_to_protected(command):
        _block(
            "hard reset to a protected branch ref from here",
            "Destructive. If you truly mean it, set ALLOW_MAIN_COMMIT=1 for this one command.",
        )


if __name__ == "__main__":
    main()
