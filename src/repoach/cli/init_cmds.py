"""``repoach init`` — onboard the git repository of the working directory.

The command anchors to the git toplevel of the current working directory
(SP-UNIVERSAL-INSTALL), never to the checkout the package was installed
from, and refuses with rc 2 outside a git working tree.  On a foreign
repository it touches nothing and prints where repoach reads its
configuration; the project file it will scaffold (``repoach.toml``)
lands with SP-UNIVERSAL-PROJECT-CONFIG.

``--repoach-dev`` keeps the contributor onboarding of a repoach
checkout (SP-REPOACH-INIT-SCAFFOLD): copy ``.env.example`` to ``.env``
for the gateway and wire the guardrail git hooks.  Those steps rewrite
``core.hooksPath``, so they never run on a repository that did not ask
for them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from ..core.paths import PROJECT_ENV_FILE_NAME, USER_ENV_FILE_NAME, user_config_dir
from .doctor_cmds import project_root_or_exit


def _repo_root() -> Path:
    """Return the git toplevel of the working directory, or exit with rc 2.

    Extracted as a module-level function, mirroring
    `main.py::_probe_client`, so tests can monkeypatch it to a
    temporary directory instead of mutating the real checkout.
    """
    return project_root_or_exit()


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


def _init_repoach_dev(repo_root: Path) -> None:
    """Run the contributor onboarding of a repoach checkout.

    Args:
        repo_root: The repoach checkout's git toplevel.
    """
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
    typer.echo(
        f"  2. Put REPOACH_LLM_PROXY_BASE_URL and REPOACH_ANTHROPIC_AUTH_TOKEN in "
        f"{PROJECT_ENV_FILE_NAME} (the CLI does not read .env)"
    )
    typer.echo('  3. pip install -e ".[dev]"')
    typer.echo("  4. repoach version   # verify the install")


def init(
    repoach_dev: Annotated[
        bool,
        typer.Option(
            "--repoach-dev",
            help="Contributor onboarding of a repoach checkout: scaffold .env and wire .githooks.",
        ),
    ] = False,
) -> None:
    """Typer callback for `repoach init`."""
    repo_root = _repo_root()
    if repoach_dev:
        _init_repoach_dev(repo_root)
        return

    typer.echo(f"init: project root {repo_root}")
    typer.echo("init: nothing written (repoach.toml lands with SP-UNIVERSAL-PROJECT-CONFIG)")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(
        f"  1. Configure a provider in {user_config_dir() / USER_ENV_FILE_NAME} or "
        f"{repo_root / PROJECT_ENV_FILE_NAME} (keep it out of git)"
    )
    typer.echo("  2. repoach doctor   # check forge, provider and paths")
