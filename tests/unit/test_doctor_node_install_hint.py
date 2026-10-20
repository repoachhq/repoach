"""``doctor --gates`` names the install command when ``node_modules`` is absent.

Measured 2026-10-20 on Yoob: with ``frontend/node_modules`` absent,
``doctor --gates`` reported ``tsc`` / ``eslint`` "missing" and said
nothing about installing them. The hint is read from the lockfile.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.gates.typescript import install_command
from repoach.gates.verifiers import node_install_hint


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn.lock", "yarn install --immutable"),
        ("package-lock.json", "npm ci"),
        (None, "npm install"),
    ],
)
def test_install_command_follows_the_lockfile(
    tmp_path: Path, lockfile: str | None, expected: str
) -> None:
    (tmp_path / "package.json").write_text("{}")
    if lockfile is not None:
        (tmp_path / lockfile).write_text("")
    assert install_command(tmp_path) == expected


def test_hint_names_the_subdirectory_and_disappears_once_installed(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}")
    (frontend / "package-lock.json").write_text("{}")
    assert node_install_hint(tmp_path) == "cd frontend && npm ci"
    (frontend / "node_modules").mkdir()
    assert node_install_hint(tmp_path) is None


def test_doctor_gates_prints_one_install_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "frontend").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "develop"], cwd=root, check=True)
    (root / "repoach.toml").write_text('[project]\nlanguages = ["typescript"]\n')
    (root / "frontend" / "package.json").write_text("{}")
    (root / "frontend" / "pnpm-lock.yaml").write_text("")
    monkeypatch.chdir(root)
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    git = shutil.which("git")
    assert git is not None
    (only_git / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(only_git))

    result = CliRunner().invoke(app, ["doctor", "--gates"])

    assert result.exit_code == 0, result.output
    hints = [line for line in result.output.splitlines() if line.startswith("node_install=")]
    assert hints == [
        "node_install=node_modules absent -- run: cd frontend && pnpm install --frozen-lockfile"
    ]
