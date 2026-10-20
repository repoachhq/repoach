"""``repoach init`` — onboard the git repository of the working directory.

The command anchors to the git toplevel of the current working directory
(SP-UNIVERSAL-INSTALL), never to the checkout the package was installed
from, and refuses with rc 2 outside a git working tree.  It writes
``repoach.toml`` from measured defaults and ``<specs_dir>/_TEMPLATE.md``,
appends ``.repoach.env`` to an existing ``.gitignore``, and touches
nothing else (SP-UNIVERSAL-PROJECT-CONFIG G2/G3, see
:mod:`repoach.project.scaffold`).

``--repoach-dev`` keeps the contributor onboarding of a repoach
checkout (SP-REPOACH-INIT-SCAFFOLD): copy ``.env.example`` to ``.env``
for the gateway and wire the guardrail git hooks.  Those steps rewrite
``core.hooksPath``, so they run only when asked and only in a checkout
that contains ``src/repoach/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from ..ci_entry.snippets import (
    DEFAULT_REF,
    InstallTarget,
    apply_ci_init,
    gitlab_include_snippet,
    plan_ci_init,
    render,
)
from ..core.paths import USER_ENV_FILE_NAME, user_config_dir
from ..project.config import ConfigError
from ..project.scaffold import InitRefusedError, apply_init, plan_init
from .doctor_cmds import EXIT_UNUSABLE, project_root_or_exit

REPOACH_SOURCE_MARKER = Path("src") / "repoach"
"""Directory whose presence identifies a repoach checkout for ``--repoach-dev``."""


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
        f"{user_config_dir() / USER_ENV_FILE_NAME} (or run 'repoach migrate-config')"
    )
    typer.echo('  3. pip install -e ".[dev]"')
    typer.echo("  4. repoach version   # verify the install")


def _init_ci(
    kind: str, *, print_only: bool, dry_run: bool, force: bool, ref: str, pypi: str | None
) -> None:
    """Write (or print) the review job for ``kind``.

    Raises:
        typer.Exit: Code 2 for an unknown CI or outside a git working tree.
    """
    name = kind.strip().lower()
    if name != "gitlab" and name != "github":
        typer.echo(f"repoach: unknown CI {kind!r}; use gitlab or github", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE)
    target = InstallTarget(ref=ref, pypi_version=pypi)
    if print_only:
        typer.echo(render(name, target)[1], nl=False)
        return
    repo_root = _repo_root()
    plan = plan_ci_init(repo_root, name, target, force=force)
    if not dry_run:
        apply_ci_init(repo_root, plan)
    typer.echo(plan.describe(dry_run=dry_run))
    if name == "gitlab":
        typer.echo("init: add to .gitlab-ci.yml (repoach never edits it):")
        typer.echo(gitlab_include_snippet(), nl=False)


def init(
    repoach_dev: Annotated[
        bool,
        typer.Option(
            "--repoach-dev",
            help="Contributor onboarding of a repoach checkout: scaffold .env and wire .githooks.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would be written; write nothing.")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-measure and rewrite an existing repoach.toml."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="With --force: discard uncommitted repoach.toml changes."),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Do not ask the forge API (merge method stays squash)."),
    ] = False,
    ci: Annotated[
        str | None,
        typer.Option(
            "--ci",
            help="Write the review job for this CI instead: gitlab or github.",
        ),
    ] = None,
    print_only: Annotated[
        bool,
        typer.Option("--print", help="With --ci: print the job on stdout; write nothing."),
    ] = False,
    ref: Annotated[
        str,
        typer.Option("--ref", help="With --ci: repoach git ref (tag or SHA) the job installs."),
    ] = DEFAULT_REF,
    pypi_version: Annotated[
        str | None,
        typer.Option("--pypi-version", help="With --ci: install repoach==VERSION from PyPI."),
    ] = None,
) -> None:
    """Write repoach.toml and the spec template into the current repository.

    Prints one line per file (``created``, ``unchanged``, ``would
    create``); a second run prints ``unchanged`` for each and changes
    no byte.  Exit codes: ``0`` done, ``2`` not a git repository, an
    invalid existing ``repoach.toml``, or a refused ``--force``.

    With ``--ci gitlab|github`` it writes only the review job
    (SP-UNIVERSAL-CI-ENTRY): ``.repoach/ci/gitlab-review.yml`` for an
    ``include:`` (``.gitlab-ci.yml`` is never edited; the include lines
    are printed) or ``.github/workflows/repoach-review.yml``. A file
    holding other content is left alone unless ``--force``.
    """
    if ci is not None:
        _init_ci(
            ci, print_only=print_only, dry_run=dry_run, force=force, ref=ref, pypi=pypi_version
        )
        return
    repo_root = _repo_root()
    if repoach_dev:
        if not (repo_root / REPOACH_SOURCE_MARKER).is_dir():
            typer.echo(
                f"repoach: --repoach-dev refused: {repo_root} has no {REPOACH_SOURCE_MARKER}/ "
                "(it is not a repoach checkout)",
                err=True,
            )
            raise typer.Exit(code=EXIT_UNUSABLE)
        _init_repoach_dev(repo_root)
        return

    try:
        plan = plan_init(repo_root, force=force, yes=yes, probe=not offline)
    except (InitRefusedError, ConfigError) as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    if not dry_run:
        apply_init(repo_root, plan)
    for action in plan.actions:
        typer.echo(action.describe(dry_run=dry_run))
    for note in plan.notes:
        typer.echo(f"init: note: {note}")
