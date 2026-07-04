"""Integration test for the ``chains-audit`` CLI command (SP-CHAINS-THINKING-CLASS).

Invokes the CLI with a temporary ``chains.env`` whose ``MODEL_SONNET`` slot
leads with a reasoner-classified model (``minimax-m3``) and asserts the
finding is printed and the exit code is 0.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ferova.cli.main import app


def test_chains_audit_reports_reasoner_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reasoner head in ``chains.env`` is reported; exit code is 0."""
    chains_env = tmp_path / "chains.env"
    chains_env.write_text(
        "MODEL_OPUS=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/opus\n"
        "MODEL_SONNET=nvidia_nim/minimaxai/minimax-m3,claude_code/sonnet\n"
        "MODEL_HAIKU=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/haiku\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["chains-audit"])

    assert result.exit_code == 0
    assert "sonnet" in result.stdout
    assert "minimax-m3" in result.stdout
    assert "reasoner" in result.stdout
