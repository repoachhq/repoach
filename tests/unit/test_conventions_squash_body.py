"""``python -m repoach.conventions.squash_body`` turns gh's commits JSON into a clean body.

``scripts/safe_merge.sh`` pipes ``gh pr view --json commits`` through it
before ``gh pr merge --squash --body-file -`` (SP-CONVENTIONS-GATE G9).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    """Run the module as safe_merge.sh does, with ``stdin`` piped in."""
    return subprocess.run(
        [sys.executable, "-m", "repoach.conventions.squash_body"],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_SRC)},
        timeout=30,
    )


def test_trailers_of_every_commit_are_dropped() -> None:
    """Each commit becomes a ``* headline`` block with its trailer gone."""
    payload = {
        "commits": [
            {
                "messageHeadline": "feat(a): one",
                "messageBody": "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",
            },
            {
                "messageHeadline": "fix(a): two",
                "messageBody": "Detail.\n\nClaude-Session: https://claude.ai/code/x",
            },
        ]
    }
    res = _run(json.dumps(payload))
    assert res.returncode == 0, res.stderr
    assert res.stdout == "* feat(a): one\n\n* fix(a): two\n\nDetail.\n"


def test_a_payload_without_commits_is_refused() -> None:
    """A payload that is not ``{"commits": [...]}`` exits 1 and prints nothing."""
    res = _run(json.dumps({"title": "x"}))
    assert res.returncode == 1
    assert res.stdout == ""
    assert "commits" in res.stderr
