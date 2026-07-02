"""Pipe-level tests for the .claude/hooks Claude Code hooks.

Each hook is exercised exactly the way the harness runs it — a
subprocess fed the hook-input JSON on stdin — pinning the block/allow
contracts the PR #3 review found untested: enforce-branch-policy
(protected-branch mutations), enforce-tmp-scripts (ad-hoc scripts
outside repo-relative tmp/), strip-useless-comments (golden-rule and
generic comment warnings), end-of-session-reminder (trigger-phrase
protocol injection). ``dream_check`` has its own contract test in
``test_dream_check_hook.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HOOKS = _REPO / ".claude" / "hooks"


def _run_hook(
    script: str,
    payload: object,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a hook script with the given stdin payload, harness-style."""
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(_HOOKS / script)],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd or _REPO,
        env={**os.environ, **(env or {})},
        timeout=30,
    )


def _bash_payload(command: str) -> dict[str, dict[str, str]]:
    """Build the PreToolUse hook-input JSON for a Bash command."""
    return {"tool_input": {"command": command}}


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """A throwaway git repository whose current branch the tests control."""
    subprocess.run(
        ["git", "init", "-q", "-b", "develop", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_branch_policy_blocks_commit_on_protected(scratch_repo: Path) -> None:
    """A git commit while standing on develop is refused with exit 2."""
    result = _run_hook(
        "enforce-branch-policy.py", _bash_payload("git commit -m x"), cwd=scratch_repo
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr


def test_branch_policy_blocks_push_refspec_to_protected(scratch_repo: Path) -> None:
    """A push whose refspec targets develop is refused from any branch."""
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=scratch_repo, check=True)
    result = _run_hook(
        "enforce-branch-policy.py",
        _bash_payload("git push origin develop"),
        cwd=scratch_repo,
    )
    assert result.returncode == 2
    assert "protected" in result.stderr


def test_branch_policy_allows_the_feature_flow(scratch_repo: Path) -> None:
    """Commit and feature-branch push both pass on a feature branch."""
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=scratch_repo, check=True)
    for command in ("git commit -m x", "git push -u origin feat/x"):
        result = _run_hook("enforce-branch-policy.py", _bash_payload(command), cwd=scratch_repo)
        assert result.returncode == 0, result.stderr


def test_branch_policy_bypass_env(scratch_repo: Path) -> None:
    """ALLOW_MAIN_COMMIT=1 clears the protected-branch commit block."""
    result = _run_hook(
        "enforce-branch-policy.py",
        _bash_payload("git commit -m hotfix"),
        cwd=scratch_repo,
        env={"ALLOW_MAIN_COMMIT": "1"},
    )
    assert result.returncode == 0


def test_tmp_scripts_allows_safe_tools_and_repo_tmp() -> None:
    """CLI tools and repo-relative tmp/ scripts pass through."""
    for command in ("pytest tests/unit", "bash tmp/probe.sh", "git status"):
        result = _run_hook("enforce-tmp-scripts.py", _bash_payload(command))
        assert result.returncode == 0, result.stderr


def test_tmp_scripts_blocks_absolute_tmp() -> None:
    """Scripts run from absolute /tmp are refused with exit 2."""
    result = _run_hook("enforce-tmp-scripts.py", _bash_payload("python3 /tmp/foo.py"))
    assert result.returncode == 2
    assert "repo-relative tmp/" in result.stderr


def test_tmp_scripts_blocks_heredoc_to_interpreter() -> None:
    """A heredoc feeding an interpreter is refused with exit 2."""
    result = _run_hook("enforce-tmp-scripts.py", _bash_payload("python3 - <<EOF\nprint(1)\nEOF"))
    assert result.returncode == 2
    assert "heredoc" in result.stderr


def test_tmp_scripts_blocks_multiline_dash_c() -> None:
    """A multi-line ``python -c`` payload is refused with exit 2."""
    result = _run_hook("enforce-tmp-scripts.py", _bash_payload("python3 -c 'x=1\nprint(x)'"))
    assert result.returncode == 2


def test_strip_comments_flags_golden_rule_violation(tmp_path: Path) -> None:
    """An inline comment under a golden-rule root is reported on stderr."""
    (tmp_path / "src").symlink_to(_REPO / "src")
    probe_dir = tmp_path / "tests"
    probe_dir.mkdir()
    probe = probe_dir / "probe.py"
    probe.write_text('"""Probe."""\n\nx = 1  # inline\n', encoding="utf-8")
    result = _run_hook(
        "strip-useless-comments.py",
        {"tool_input": {"file_path": str(probe)}},
        env={"CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    assert "golden rule [inline]" in result.stderr


def test_strip_comments_flags_generic_patterns(tmp_path: Path) -> None:
    """Bare TODOs and banner separators are reported for any .py file."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "# TODO fix this\n# ──────────── section ────────────\nx = 1\n",
        encoding="utf-8",
    )
    result = _run_hook("strip-useless-comments.py", {"tool_input": {"file_path": str(probe)}})
    assert result.returncode == 0
    assert "bare TODO" in result.stderr
    assert "banner section separator" in result.stderr


def test_strip_comments_stays_silent_on_clean_files() -> None:
    """A clean repo source file produces no output and exit 0."""
    clean = _REPO / "src" / "ferova" / "health" / "model_health.py"
    result = _run_hook(
        "strip-useless-comments.py",
        {"tool_input": {"file_path": str(clean)}},
        env={"CLAUDE_PROJECT_DIR": str(_REPO)},
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_end_of_session_injects_protocol_on_trigger() -> None:
    """A stop phrase yields the additionalContext protocol JSON."""
    result = _run_hook("end-of-session-reminder.py", {"prompt": "ok on s'arrête là pour ce soir"})
    assert result.returncode == 0
    out = json.loads(result.stdout)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "END-OF-SESSION PROTOCOL" in context


def test_end_of_session_passes_normal_prompts() -> None:
    """A regular prompt passes through silently."""
    result = _run_hook("end-of-session-reminder.py", {"prompt": "peux-tu lire ce fichier ?"})
    assert result.returncode == 0
    assert result.stdout == ""


def test_end_of_session_survives_garbage_stdin() -> None:
    """Malformed stdin never breaks the hook (exit 0, no output)."""
    result = _run_hook("end-of-session-reminder.py", "not json at all")
    assert result.returncode == 0
    assert result.stdout == ""
