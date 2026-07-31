"""Unit tests for ``repoach init`` (SP-REPOACH-INIT-SCAFFOLD).

Covers `_repo_root`-independent building blocks (`scaffold_env`,
`configure_hooks`) plus the CLI registration check, all against
`tmp_path` fixtures — never the real checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from repoach.cli.init_cmds import configure_hooks, scaffold_env
from repoach.cli.main import app


def test_init_command_is_registered() -> None:
    """`repoach --help` lists the `init` command."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout


def test_scaffold_env_creates_file_from_example(tmp_path: Path) -> None:
    """`.env` is created from `.env.example` with the fixture's exact content."""
    example_content = "REPOACH_ENV=dev\nREPOACH_ANTHROPIC_AUTH_TOKEN=\n"
    (tmp_path / ".env.example").write_text(example_content, encoding="utf-8")

    created = scaffold_env(tmp_path)

    assert created is True
    assert (tmp_path / ".env").read_text(encoding="utf-8") == example_content


def test_scaffold_env_does_not_overwrite_existing_env(tmp_path: Path) -> None:
    """Re-running against an existing `.env` leaves its mutated content untouched."""
    (tmp_path / ".env.example").write_text("REPOACH_ENV=dev\n", encoding="utf-8")
    mutated_content = "REPOACH_ENV=custom-operator-value\n"
    (tmp_path / ".env").write_text(mutated_content, encoding="utf-8")

    created = scaffold_env(tmp_path)

    assert created is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == mutated_content


def test_configure_hooks_sets_core_hooks_path(tmp_path: Path) -> None:
    """`configure_hooks` sets `core.hooksPath` to `.githooks` in `tmp_path`."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)

    configure_hooks(tmp_path)

    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ".githooks"
