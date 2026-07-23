"""Integration test for SP-SLOW-DEFAULTS-TEST-ISOLATION.

Drives the real ``pytest`` executable, from a scratch working directory
holding a deployed ``.env`` that arms the slow-strike breaker, against
the real ``test_slow_settings_defaults`` selector -- reproducing exactly
the invocation shape ``ci_local.sh`` and the merge gate run from the
repo root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_slow_settings_defaults_subprocess_survives_deployed_shadow_arm(
    tmp_path: Path,
) -> None:
    """A deployed .env that arms the slow-strike breaker must not fail
    the real pytest gate for test_slow_settings_defaults.

    Writes REPOACH_BREAKER_SLOW_SHADOW=false into a scratch .env, runs
    pytest for the single real selector with that directory as the
    process cwd (Settings resolves its dotenv path relative to cwd,
    exactly as it does under ci_local.sh at the repo root), and asserts
    the gate still exits 0.
    """
    (tmp_path / ".env").write_text("REPOACH_BREAKER_SLOW_SHADOW=false\n", encoding="utf-8")
    target = f"{_REPO}/tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
