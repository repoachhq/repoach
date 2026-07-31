"""Integration test for `repoach init` (SP-REPOACH-INIT-SCAFFOLD).

Drives the command end-to-end via `CliRunner` against a `git init`-ed
`tmp_path` fixture with `_repo_root` monkeypatched, so the real
checkout's `.env` and git config are never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.cli.init_cmds as init_cmds
from repoach.cli.main import app


def test_repoach_init_scaffolds_fresh_clone_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repoach init` scaffolds `.env`, wires hooks, and prints follow-up steps."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    example_content = "REPOACH_ENV=dev\nREPOACH_ANTHROPIC_AUTH_TOKEN=\n"
    (tmp_path / ".env.example").write_text(example_content, encoding="utf-8")
    monkeypatch.setattr(init_cmds, "_repo_root", lambda: tmp_path)

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".env").read_text(encoding="utf-8") == example_content

    hooks_path = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert hooks_path.stdout.strip() == ".githooks"

    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" in result.stdout
    assert "pip install" in result.stdout
    assert "repoach version" in result.stdout
