"""A command's stdout is exactly one JSON document; logs go to stderr.

SP-CLI-STDOUT-ONE-JSON. Measured 2026-10-20 on Yoob: ``repoach review
pr <N> --dry-run`` printed a structlog line on stdout before the JSON,
so ``jq`` failed. Two causes: ``configure_logging`` bound the root
handler to stdout, and structlog's unconfigured default prints to
stdout. Each check runs a fresh interpreter and validates the captured
stdout with ``python -m json.tool``, the operator's own probe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from repoach.cli.review_cmds import review_app
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from tests.unit.test_review_team import _StubGitHubForge
from tests.unit.test_review_transport_error_verdict import _install_unreachable_reviewers

_SRC = Path(__file__).resolve().parents[2] / "src"


def _run(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    # Isolate the child's config/data lookup (HOME, XDG_*) and guarantee it
    # resolves ``repoach`` the way this interpreter does (PYTHONPATH), but
    # start from the parent's own environment rather than replacing it: a
    # CI-provisioned interpreter (actions/setup-python) can need
    # LD_LIBRARY_PATH to even start, and a bare ``{"PATH": ..., "HOME": ...}``
    # dict drops that, so the child never execs (exit 127, no stderr to read).
    env = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PYTHONPATH": str(_SRC),
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert completed.returncode == 0, (
        f"child exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


def _json_tool(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "json.tool"], input=stdout, capture_output=True, text=True
    )


_EMIT = (
    "import json, structlog\n"
    "structlog.get_logger('probe').warning('probe_event', n=1)\n"
    "print(json.dumps({'final_verdict': 'ERROR'}))\n"
)


def test_unconfigured_cli_logger_writes_stderr(tmp_path: Path) -> None:
    completed = _run("import repoach.cli\n" + _EMIT, tmp_path)
    assert _json_tool(completed.stdout).returncode == 0, completed.stdout
    assert "probe_event" in completed.stderr


def test_configured_logging_writes_stderr(tmp_path: Path) -> None:
    completed = _run(
        "import repoach.cli\n"
        "from repoach.core.logging import configure_logging\n"
        "configure_logging()\n" + _EMIT,
        tmp_path,
    )
    assert _json_tool(completed.stdout).returncode == 0, completed.stdout
    assert "probe_event" in completed.stderr


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the reviewer's real retry backoff waits."""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)


def test_review_pr_dry_run_stdout_is_one_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_unreachable_reviewers(monkeypatch)
    forge = _StubGitHubForge()

    def review(pr_number: int, **kwargs: Any) -> TeamOutcome:
        return ReviewTeamOrchestrator(
            forge=forge,
            db_path=tmp_path / "review.db",
            post_to_github=kwargs["post"],
            repo_root=tmp_path,
        ).review_pr(pr_number)

    with patch("repoach.cli.review_cmds.run_review", review):
        result = CliRunner().invoke(review_app, ["pr", "42", "--dry-run"])

    assert result.exit_code == 3, result.output
    assert _json_tool(result.stdout).returncode == 0, result.stdout
    assert json.loads(result.stdout)["final_verdict"] == "ERROR"
