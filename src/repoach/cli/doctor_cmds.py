"""``repoach doctor`` and the project-root guard shared by project-scoped commands.

``doctor`` prints, one ``key=value`` line each, the anchors a run
resolves (SP-UNIVERSAL-INSTALL G6): project root, user config and data
directories, the env files read, every ``repoach.toml`` key with its
source (SP-UNIVERSAL-PROJECT-CONFIG), the ledger path, the selected forge
and provider, and whether each external binary is on ``PATH``.  It
builds :class:`~repoach.core.config.Settings` directly (no directory is
created) and never contacts a forge or a provider.

``doctor --gates`` prints the gate configuration instead
(SP-UNIVERSAL-GATES): the declared languages and commands, then one
row per supported language with each verifier's availability here
(``builtin``, ``found: <path>``, ``missing: <binary>``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer
from pydantic import ValidationError

from .. import __version__
from ..ci_entry.cli import ci_doctor_lines
from ..core.config import Settings, _anchored_env_files, configured_env_value
from ..core.paths import (
    NotARepositoryError,
    require_project_root,
    user_config_dir,
    user_data_dir,
)
from ..gates.config import CONFIG_FILE_NAME, GatesConfig, load_gates_config
from ..gates.verifiers import availability_table, node_install_hint
from ..project.config import ConfigError, config_path
from ..project.resolve import (
    UNDECLARED_COMMAND_NOTE,
    ResolvedProjectConfig,
    effective_rows,
    resolve_project_config,
)
from ..project.toml_write import toml_value

DOCTOR_BINARIES: tuple[str, ...] = ("git", "gh", "glab", "claude")
"""External executables whose presence ``doctor`` reports."""

_ROUTED_PROVIDER_KINDS: frozenset[str] = frozenset({"", "gateway"})
"""``[provider].kind`` values a review can reach today.

Every model call goes through the gateway (``REPOACH_LLM_PROXY_BASE_URL``);
the direct providers are parsed but not routed
(SP-PROVIDER-DIRECT-ANTHROPIC, open), so ``doctor`` says so instead of
implying the key alone is enough."""

ANTHROPIC_CREDENTIAL_NAMES: tuple[str, ...] = ("REPOACH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
"""Environment names the direct ``anthropic`` provider reads, first found wins."""


def _anthropic_credential(root: Path) -> str:
    """Name the Anthropic credential the environment layer holds, never its value.

    Args:
        root: The project root (its env layers are consulted).

    Returns:
        ``<NAME> (present)`` for the first name set, in
        :data:`ANTHROPIC_CREDENTIAL_NAMES` order, else ``none (reads …)``.
    """
    for name in ANTHROPIC_CREDENTIAL_NAMES:
        if configured_env_value((name,), root) is not None:
            return f"{name} (present)"
    return f"none (reads {', then '.join(ANTHROPIC_CREDENTIAL_NAMES)})"


EXIT_UNUSABLE = 2
"""Exit code when the project, forge or provider is not usable."""


def project_root_or_exit(start: Path | None = None) -> Path:
    """Return the git toplevel of ``start`` or end the command with rc 2.

    Args:
        start: Directory to resolve from; defaults to the current
            working directory.

    Returns:
        The absolute git toplevel.

    Raises:
        typer.Exit: With code 2 after naming the directory on stderr,
            when it is not inside a git working tree or ``git`` is absent.
    """
    try:
        return require_project_root(start)
    except NotARepositoryError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc


def _binary_line(name: str) -> str:
    """Return the ``doctor`` line for one executable.

    Args:
        name: Executable name looked up on ``PATH``.

    Returns:
        ``<name>=<absolute path>`` or ``<name>=missing``.
    """
    found = shutil.which(name)
    return f"{name}={found if found is not None else 'missing'}"


_FORGE_BINARIES: dict[str, str] = {"github": "gh", "gitlab": "glab"}
"""CLI each forge is driven through."""


def _forge_status(resolved: ResolvedProjectConfig) -> tuple[str, str | None]:
    """Return the selected forge and whether it is usable here.

    Args:
        resolved: The project's resolved configuration.

    Returns:
        The ``forge=`` value (name, source, binary state) and, when the
        forge's CLI is missing, the problem to report.
    """
    name = resolved.forge
    source = resolved.source("repoach.forge")
    binary = _FORGE_BINARIES[name]
    if shutil.which(binary) is None:
        return (
            f"{name} ({source}; {binary} not on PATH)",
            f"{binary} not on PATH -- install it to use the {name} forge",
        )
    return f"{name} ({source}; {binary} found)", None


def _command_line(name: str, value: str | None, builtin: str) -> str:
    """Return the ``commands.<name>=`` line of ``doctor --gates``."""
    if value is None:
        return f"commands.{name}=({builtin})"
    return f"commands.{name}={value or '(none: skipped)'}"


def print_gates(root: Path) -> None:
    """Print the gate configuration and the per-language verifier table.

    Args:
        root: The project root.

    Raises:
        typer.Exit: With code 2 when ``repoach.toml`` is malformed.
    """
    try:
        config: GatesConfig = load_gates_config(root)
    except ConfigError as exc:
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    source = root / CONFIG_FILE_NAME
    typer.echo(f"gates_config={source if not config.builtin else 'builtin (no repoach.toml)'}")
    typer.echo(f"languages={','.join(sorted(lang.value for lang in config.languages))}")
    typer.echo(f"src_roots={','.join(config.src_roots) or '(anywhere)'}")
    typer.echo(f"test_roots={','.join(config.test_roots) or '(anywhere)'}")
    typer.echo(_command_line("lint", config.lint_cmd, "built-in ruff check + format"))
    typer.echo(_command_line("test", config.test_cmd, "built-in pytest -q tests/unit"))
    typer.echo(_command_line("gate", config.gate_cmd, "none"))
    typer.echo(f"commands.timeout_s={config.timeout_s}")
    typer.echo(f"arch.require_frontmatter={str(config.require_frontmatter).lower()}")
    header = f"{'language':<11} {'declared':<9} {'extensions':<24} {'selector':<8} verifiers"
    typer.echo(header)
    for row in availability_table(root, config):
        verifiers = "; ".join(f"{name}={state}" for name, state in row.verifiers) or "(none)"
        typer.echo(
            f"{row.language.value:<11} {'yes' if row.declared else 'no':<9} "
            f"{','.join(row.extensions):<24} {row.selector_kind or '-':<8} {verifiers}"
        )
    hint = node_install_hint(root)
    if hint is not None:
        typer.echo(f"node_install=node_modules absent -- run: {hint}")


def doctor(
    gates: bool = typer.Option(
        False, "--gates", help="Print the gate configuration and per-language verifier table."
    ),
    ci: bool = typer.Option(
        False,
        "--ci",
        help="Print the CI variables 'repoach ci review' would read and which are present.",
    ),
) -> None:
    """Print the resolved anchors, configuration, forge, provider and binaries.

    Every ``repoach.toml`` key is printed as ``config.<section>.<key>=<value>
    (<source>)``.  Exits 0 when the selected forge and provider are
    usable, 2 otherwise (including outside a git working tree or with an
    invalid ``repoach.toml``). With ``--gates`` prints the gate
    configuration and verifier table instead, exiting 0 unless
    ``repoach.toml`` is invalid. With ``--ci`` prints, by name only, the
    variables ``repoach ci review`` reads and whether each is present,
    then the CI, PR source, posting mode and provider mode it would use;
    it exits 0 and needs no git working tree (SP-UNIVERSAL-CI-ENTRY).

    Args:
        gates: Print the gate table instead of the anchors.
        ci: Print the CI variable table instead of the anchors.

    Raises:
        typer.Exit: With code 2 when anything required is unusable.
    """
    if ci:
        for line in ci_doctor_lines(os.environ):
            typer.echo(line)
        return
    root = project_root_or_exit()
    if gates:
        print_gates(root)
        return
    typer.echo(f"repoach={__version__}")
    typer.echo(f"project_root={root}")
    typer.echo(f"user_config_dir={user_config_dir()}")
    typer.echo(f"user_data_dir={user_data_dir()}")
    for env_file in _anchored_env_files(root):
        state = "present" if env_file.is_file() else "absent"
        typer.echo(f"env_file={env_file} ({state})")
    config_file = config_path(root)
    typer.echo(f"config_file={config_file} ({'present' if config_file.is_file() else 'absent'})")

    try:
        resolved = resolve_project_config(root)
    except ConfigError as exc:
        typer.echo("config=invalid")
        typer.echo(f"repoach: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    for dotted, value, source in effective_rows(resolved):
        shown = f"<{UNDECLARED_COMMAND_NOTE}>" if value is None else toml_value(value)
        typer.echo(f"config.{dotted}={shown} ({source})")

    problems: list[str] = []
    try:
        settings = Settings()
    except ValidationError as exc:
        typer.echo(f"settings=invalid ({exc.error_count()} error(s))")
        typer.echo(f"repoach: settings invalid: {exc}", err=True)
        raise typer.Exit(code=EXIT_UNUSABLE) from exc
    typer.echo(f"db_path={settings.db_path}")

    forge, forge_problem = _forge_status(resolved)
    typer.echo(f"forge={forge}")
    if forge_problem is not None:
        problems.append(forge_problem)

    kind = resolved.config.provider.kind
    if kind in _ROUTED_PROVIDER_KINDS:
        if settings.llm_proxy_base_url:
            typer.echo(f"provider=gateway ({settings.llm_proxy_base_url})")
        else:
            typer.echo("provider=none")
            problems.append(
                "no provider configured -- set REPOACH_LLM_PROXY_BASE_URL (gateway) in the "
                "environment or ~/.config/repoach/env (never in a project file)"
            )
    else:
        unrouted = f"provider {kind}: not routed, set REPOACH_LLM_PROXY_BASE_URL"
        typer.echo(f"provider={kind} ({resolved.source('provider.kind')}; not routed)")
        if kind == "anthropic":
            typer.echo(f"provider_credential={_anthropic_credential(root)}")
        if settings.llm_proxy_base_url:
            typer.echo(f"provider_route=gateway ({settings.llm_proxy_base_url})")
            typer.echo(f"repoach: {unrouted}; reviews go through the gateway", err=True)
        else:
            typer.echo("provider_route=none")
            problems.append(unrouted)

    for name in DOCTOR_BINARIES:
        typer.echo(_binary_line(name))

    for problem in problems:
        typer.echo(f"repoach: {problem}", err=True)
    if problems:
        raise typer.Exit(code=EXIT_UNUSABLE)
