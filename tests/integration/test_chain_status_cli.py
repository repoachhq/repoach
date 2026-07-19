"""Integration tests for the ferova chain-status CLI in degraded environments.

Covers the fail-open contract (G4): every data source degrades to an
explicit ``unavailable`` line, exit 0 always, no traceback on stderr.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def test_chain_status_end_to_end_degraded_environment(tmp_path: Path) -> None:
    """Invoke ``ferova chain-status`` against a fresh db and an unbound proxy.

    Asserts exit code 0, the expected degraded digest lines, and no
    traceback on stderr.
    """
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "ferova.db"

    proxy_port = 19999
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    result = subprocess.run(
        [
            "ferova",
            "chain-status",
            "--db-path",
            str(db_path),
            "--proxy-url",
            proxy_url,
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
        env={
            **os.environ,
            "FEROVA_OPENROUTER_API_KEY": "",
        },
    )

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "no probes in window" in result.stdout, (
        f"missing 'no probes in window' in stdout:\n{result.stdout}"
    )
    assert "proxy: unreachable" in result.stdout, (
        f"missing 'proxy: unreachable' in stdout:\n{result.stdout}"
    )

    traceback_marker = "Traceback (most recent call last)"
    assert traceback_marker not in result.stderr, f"traceback on stderr:\n{result.stderr}"
