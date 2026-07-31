"""``repoach init`` — scaffold ``.env`` and git hooks for a fresh clone.

Performs the two mechanical, idempotent onboarding steps a third-party
clone otherwise requires by hand (copy ``.env.example`` to ``.env``,
wire the guardrail git hooks) and prints the remaining manual steps
(provider secrets, install) that cannot be scaffolded.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer


def _repo_root() -> Path:
    """Return the repository root (`.env`/`chains.env` anchor).

    Extracted as a module-level function, mirroring
    `main.py::_probe_client`, so tests can monkeypatch it to a
    temporary directory instead of mutating the real checkout.
    """
    return Path(__file__).resolve().parents[3]


def scaffold_env(repo_root: Path) -> bool:
    """Copy `.env.example` to `.env` under `repo_root` if `.env` is absent.

    Args:
        repo_root: Directory containing `.env.example` and (maybe) `.env`.

    Returns:
        True if `.env` was created by this call; False if it already
        existed and was left untouched.

    Raises:
        FileNotFoundError: `.env.example` is missing from `repo_root`.
    """
    env_path = repo_root / ".env"
    if env_path.exists():
        return False
    example_path = repo_root / ".env.example"
    if not example_path.exists():
        raise FileNotFoundError(f".env.example not found at {example_path}")
    shutil.copyfile(example_path, env_path)
    return True


def configure_hooks(repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Run `git config core.hooksPath .githooks` in `repo_root`.

    Raises:
        subprocess.CalledProcessError: `git config` failed (e.g.
            `repo_root` is not inside a git working tree) — surfaced,
            never swallowed.
    """
    return subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )


def init() -> None:
    """Typer callback for `repoach init` — scaffold + print next steps."""
    repo_root = _repo_root()

    created = scaffold_env(repo_root)
    if created:
        typer.echo("init: created .env from .env.example")
    else:
        typer.echo("init: .env already exists, left untouched")

    configure_hooks(repo_root)
    typer.echo("init: git config core.hooksPath .githooks")

    typer.echo("")
    typer.echo("Remaining manual steps:")
    typer.echo("  1. Set REPOACH_ANTHROPIC_AUTH_TOKEN and one provider key in .env")
    typer.echo('  2. pip install -e ".[dev]"')
    typer.echo("  3. repoach version   # verify the install")
