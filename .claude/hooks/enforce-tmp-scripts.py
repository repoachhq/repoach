#!/usr/bin/env python3
"""PreToolUse hook: enforce that ad-hoc scripts run from repo-relative tmp/.

Blocks Bash commands that:
  1. Run scripts from /tmp/ (absolute) — must use repo-relative tmp/
  2. Run .sh/.py scripts from repo root or other dirs (not tmp/, ci/, scripts/, deploy/)
  3. Run python <file>.py directly instead of from tmp/
  4. Use `python -c` / `bash -c` / `sh -c` with a multi-line payload
     or with `$(cat ...)` indirect script loading — put it in tmp/ instead.
  5. Feed an interpreter via heredoc (`python3 << EOF ... EOF`, `cat <<EOF | bash`).
     Heredocs that feed non-interpreter tools (git commit -m, gh pr create) pass through.

Safe commands (git, npm, pytest, pip, docker, system utils, single-line python -c/-m)
pass through.

Exit codes:
  0 = allow
  2 = block (prints reason to stderr)
"""

import json
import os
import re
import shlex
import sys

INTERPRETER_RE = re.compile(r"^(python[23]?|bash|sh)$")
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "").rstrip("/")


def _strip_env_vars(cmd: str) -> str:
    """Remove leading FOO=bar env var assignments."""
    while re.match(r"^[A-Z_]+=\S+\s+", cmd):
        cmd = re.sub(r"^[A-Z_]+=\S+\s+", "", cmd, count=1)
    return cmd


def _is_repo_relative_tmp(cmd: str) -> bool:
    """Check if command targets repo-relative tmp/.

    Accepts ``./tmp/``, ``tmp/``, or an absolute path inside
    ``$CLAUDE_PROJECT_DIR/tmp/``.
    """
    if re.match(r"^(?:bash\s+|sh\s+|python[3]?\s+)?(?:\./)?tmp/", cmd):
        return True
    if PROJECT_DIR:
        abs_tmp = f"{PROJECT_DIR}/tmp/"
        pattern = rf"^(?:bash\s+|sh\s+|python[3]?\s+)?{re.escape(abs_tmp)}"
        if re.match(pattern, cmd):
            return True
    return False


def _is_absolute_tmp(cmd: str) -> bool:
    """Check if command targets /tmp/ (absolute path — blocked)."""
    return bool(re.match(r"^(?:bash\s+|sh\s+|python[3]?\s+)?/tmp/", cmd)) or cmd.startswith("/tmp/")


def _is_script_file(cmd: str) -> bool:
    """Check if command runs a .sh or .py script file (not a CLI tool)."""
    if re.match(r"^python[3]?\s+-[cm]\s+", cmd):
        return False
    if re.match(r"^python[3]?\s+\S+\.py", cmd):
        return True
    if re.match(r"^(?:bash\s+|sh\s+)?\S+\.sh", cmd):
        return True
    if re.match(r"^\S+\.sh(?:\s|$)", cmd):
        return True
    return bool(re.match(r"^\S+\.py(?:\s|$)", cmd))


def _is_allowed_script_location(cmd: str) -> bool:
    """Check if script is in an allowed location (ci/, scripts/, deploy/, .claude/hooks/, setup.sh)."""
    allowed = (
        r"(?:\./)?ci/",
        r"(?:\./)?scripts/",
        r"(?:\./)?deploy/",
        r"(?:\./)?\.claude/hooks/",
        r"(?:\./)?setup\.sh",
    )
    script_path = re.sub(r"^(?:bash\s+|sh\s+|python[3]?\s+)", "", cmd).strip()
    return any(re.match(pat, script_path) for pat in allowed)


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _check_dash_c(cmd: str) -> str | None:
    """Vet ``python/bash/sh -c '<payload>'`` invocations.

    Blocks payloads that are multi-line or load a script indirectly
    via ``$(cat ...)``.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None

    for i, tok in enumerate(tokens):
        if tok != "-c" or i == 0 or i + 1 >= len(tokens):
            continue
        if not INTERPRETER_RE.match(_basename(tokens[i - 1])):
            continue
        payload = tokens[i + 1]
        if "\n" in payload:
            return "multi-line `-c` payload — write the script to tmp/ instead"
        if "$(cat " in payload or "$(<" in payload:
            return "indirect script load via $(cat ...) in `-c` — put the script in tmp/"
    return None


def _check_heredoc_to_interpreter(cmd: str) -> str | None:
    """Block heredocs that feed an interpreter, directly or through a pipe.

    Allowed: `git commit -m "$(cat <<'EOF' ... EOF)"` — heredoc content is a
    message argument, not interpreted as code.
    Blocked: `python3 << EOF`, `bash <<- EOF`, `cat << EOF | python`.
    """
    if re.search(r"(?:^|[\s;&|])(?:python[23]?|bash|sh)\b[^\n|<]*<<-?\s*['\"]?\w+", cmd):
        return "heredoc feeding an interpreter — write the script to tmp/ instead"
    if re.search(
        r"<<-?\s*['\"]?\w+['\"]?[\s\S]*?\|\s*(?:python[23]?|bash|sh)(?:\s|$)",
        cmd,
    ):
        return "heredoc piped to an interpreter — write the script to tmp/ instead"
    return None


# Common safe CLI tools across stacks. Add to the pattern as your project grows.
SAFE_TOOL_PATTERNS = re.compile(
    r"^(?:"
    r"git|npm|npx|node|yarn|pnpm|"
    r"pytest|pip3?|conda|uv|poetry|"
    r"ruff|black|isort|mypy|"
    r"docker|sudo docker|docker[ -]compose|"
    r"curl|wget|"
    r"ls|tree|du|wc|find|"
    r"cat|head|tail|grep|sort|xargs|"
    r"mkdir|cp|mv|rm|ln|touch|"
    r"chmod|chown|sudo\s+(?:chown|chmod|mkdir|rm|bash|dpkg)|"
    r"make|gcc|g\+\+|"
    r"alembic|"
    r"psql|createdb|pg_isready|"
    r"xdg-open|open|"
    r"lsblk|lspci|mount|findmnt|"
    r"pgrep|pkill|kill|ps|"
    r"jobs|ss|lsof|"
    r"dpkg|apt|"
    r"snap|systemctl|"
    r"glab|gh|claude|"
    r"source|export|"
    r"env|echo|printf|"
    r"true|test|"
    r"command\s+-v|"
    r"timeout|time|"
    r"bash\s+-c|"
    r"sed|awk|"
    r"for|while|if|"
    r"sleep"
    r")(?:\s|$|[;\|&])"
)


def _block(command: str, reason: str) -> None:
    print(
        f"BLOCKED: {reason}\n"
        f"  Write the script to tmp/<name>.{{sh,py}} then run ./tmp/<name>.sh\n"
        f"  Command: {command[:200]}",
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    """Read the PreToolUse payload and block off-policy script runs."""
    hook_input = json.loads(sys.stdin.read())
    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "").strip()

    if not command:
        return

    check = _strip_env_vars(command)

    if _is_repo_relative_tmp(check):
        return

    reason = _check_heredoc_to_interpreter(command)
    if reason:
        _block(command, reason)

    reason = _check_dash_c(command)
    if reason:
        _block(command, reason)

    if _is_absolute_tmp(check):
        _block(command, "absolute /tmp/ is not the repo-relative tmp/")

    if _is_script_file(check):
        if _is_allowed_script_location(check):
            return
        _block(command, "ad-hoc script outside tmp/, ci/, scripts/, deploy/")

    if SAFE_TOOL_PATTERNS.match(check):
        return

    if re.match(r"^python[3]?\s+-[cm]\s+", check):
        return


if __name__ == "__main__":
    main()
