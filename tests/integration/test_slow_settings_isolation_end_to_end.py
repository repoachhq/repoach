"""Integration test for SP-SLOW-DEFAULTS-TEST-ISOLATION.

Drives the real ``pytest`` executable, from a scratch working directory
holding a deployed ``.env`` that arms the slow-strike breaker, against
the real ``test_slow_settings_defaults`` selector -- reproducing exactly
the invocation shape ``ci_local.sh`` and the merge gate run from the
repo root.

The child gets its own ``--basetemp`` under ``tmp_path``. Without it, the
child's autouse ``tmp_path_factory`` fixtures open a numbered directory in
the shared ``<tmp>/pytest-of-<user>`` root, and at exit pytest deletes
every older numbered directory there that no live session locks: the
leftover basetemps of other suites, 25 000 to 48 000 entries each on the
self-hosted runner. That deletion, not the one test, is what ran past 60 s
on #293 (run 43314128941, attempt 1, both legs).
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

STALE_SESSIONS = 5
"""Numbered sessions planted in the child's temporary root: more than pytest's default keep of 3."""


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

    The child's ``TMPDIR`` holds stale numbered sessions of another
    suite. The child must leave them in place: it works under its own
    ``--basetemp``, so its cost never depends on what other suites left
    in the shared root.
    """
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    (workdir / ".env").write_text("REPOACH_BREAKER_SLOW_SHADOW=false\n", encoding="utf-8")
    shared = tmp_path / "shared-tmp"
    root = shared / f"pytest-of-{getpass.getuser()}"
    stale = [root / f"pytest-{number}" for number in range(1, STALE_SESSIONS + 1)]
    for session in stale:
        (session / "popen-gw0" / "test_left_behind0").mkdir(parents=True)
    target = f"{_REPO}/tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp_path / 'child-basetemp'}",
        ],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        env={**os.environ, "TMPDIR": str(shared)},
        timeout=60,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    removed = [session.name for session in stale if not session.exists()]
    assert removed == [], f"the child deleted other sessions' basetemps: {removed}"
