"""Choose and build the :class:`~repoach.forge.protocol.Forge` for a repository.

:func:`select_forge` is the only constructor callers use. Resolution
order for the forge name (SP-UNIVERSAL-FORGE G4, umbrella resolution
rule): the ``--forge`` override > ``REPOACH_FORGE`` > ``[repoach].forge``
in ``repoach.toml`` > the host of ``git remote get-url origin`` >
``github`` (repoach's own default when nothing else says otherwise —
an unknown host is never guessed to be GitLab).

Everything but the flag comes from the project configuration
(:func:`repoach.project.resolve.resolve_project_config`,
SP-UNIVERSAL-PROJECT-CONFIG), which also supplies the merge method,
``remove_source_branch`` and the bot identity.  The bot identity is
``REPOACH_REVIEW_BOT_LOGIN`` > ``[review].bot_username`` > the forge's
default: ``github-actions[bot]`` on GitHub, empty on GitLab — a GitLab
run must name its bot, or trust no marker.
"""

from __future__ import annotations

from pathlib import Path

from ..core.logging import get_logger
from ..core.paths import project_root
from ..project.config import ForgeName
from ..project.resolve import (
    ResolvedProjectConfig,
    effective_bot_username,
    infer_forge_from_url,
    resolve_project_config,
)
from .github import GitHubForge
from .gitlab import GitLabForge
from .protocol import Forge, ForgeError, ForgeUnavailableError, MergeMethod

_log = get_logger(__name__)

FORGE_NAMES: tuple[ForgeName, ...] = ("github", "gitlab")

__all__ = [
    "FORGE_NAMES",
    "infer_forge_from_url",
    "merge_preferences",
    "resolve_forge_name",
    "select_forge",
]


def _resolved(cwd: Path | None) -> ResolvedProjectConfig:
    """Resolve the project configuration of the repository holding ``cwd``."""
    return resolve_project_config(project_root((cwd or Path.cwd()).resolve()))


def resolve_forge_name(
    resolved: ResolvedProjectConfig,
    *,
    override: str | None = None,
) -> tuple[ForgeName, str]:
    """Resolve which forge to use and say why.

    Args:
        resolved: The project configuration.
        override: The ``--forge`` flag value, highest precedence.

    Returns:
        ``(name, source)`` where ``source`` is ``flag``, ``env``,
        ``file``, ``origin`` or ``default``.

    Raises:
        ForgeError: The flag names an unknown forge.
    """
    if override:
        name = override.strip().lower()
        if name == "github" or name == "gitlab":
            return name, "flag"
        raise ForgeError(f"unknown forge {override!r} (from flag); use github or gitlab")
    return resolved.forge, resolved.source("repoach.forge")


def merge_preferences(cwd: Path | None = None) -> tuple[MergeMethod, bool]:
    """Return the configured merge method and ``remove_source_branch``.

    Args:
        cwd: Any directory inside the repository (default: the process cwd).

    Returns:
        ``(merge_method, remove_source_branch)``.

    Raises:
        ConfigError: ``repoach.toml`` or an environment value is invalid.
    """
    resolved = _resolved(cwd)
    section = resolved.config.repoach
    return MergeMethod(section.merge_method), section.remove_source_branch


def select_forge(
    resolved: ResolvedProjectConfig | None = None,
    *,
    override: str | None = None,
    cwd: Path | None = None,
) -> Forge:
    """Build the forge for the repository at ``cwd``.

    Args:
        resolved: Pre-resolved configuration; resolved for ``cwd`` when ``None``.
        override: The ``--forge`` flag value, highest precedence.
        cwd: The clone the forge operates in (default: the process cwd).

    Returns:
        A :class:`~repoach.forge.github.GitHubForge` or
        :class:`~repoach.forge.gitlab.GitLabForge`.

    Raises:
        ForgeUnavailableError: The selected forge's binary is not installed.
        ForgeError: An unknown forge name was given.
        ConfigError: ``repoach.toml`` or an environment value is invalid.
    """
    resolved_cwd = (cwd or Path.cwd()).resolve()
    config = resolved if resolved is not None else _resolved(resolved_cwd)
    name, source = resolve_forge_name(config, override=override)
    bot = effective_bot_username(config, name)
    forge: Forge
    if name == "gitlab":
        forge = GitLabForge(cwd=resolved_cwd, bot_username=bot)
        binary = "glab"
    else:
        forge = GitHubForge(cwd=resolved_cwd, bot_login=bot)
        binary = "gh"
    if not forge.available():
        raise ForgeUnavailableError(
            f"forge {name!r} (from {source}) needs the {binary!r} CLI on PATH; "
            f"install it or authenticate with {_TOKEN_VARS[name]}",
            binary=binary,
        )
    _log.debug("forge.selected", forge=name, source=source, bot_username=bot)
    return forge


_TOKEN_VARS: dict[ForgeName, str] = {
    "github": "GH_TOKEN / GITHUB_TOKEN",
    "gitlab": "GITLAB_TOKEN",
}
