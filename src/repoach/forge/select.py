"""Choose and build the :class:`~repoach.forge.protocol.Forge` for a repository.

:func:`select_forge` is the only constructor callers use. Resolution
order for the forge name (SP-UNIVERSAL-FORGE G4, umbrella resolution
rule): the ``--forge`` override > ``REPOACH_FORGE`` > ``[repoach].forge``
in ``repoach.toml`` > the host of ``git remote get-url origin`` >
``github`` (repoach's own default when nothing else says otherwise —
an unknown host is never guessed to be GitLab).

The project configuration child (SP-UNIVERSAL-PROJECT-CONFIG) has not
landed, so :class:`ForgeConfig` reads the forge keys of
``repoach.toml`` itself; it is the seam that child replaces.

Keys read from ``repoach.toml`` at the repository root::

    [repoach]
    forge = "gitlab"            # or "github"
    merge_method = "merge"      # squash | merge | rebase
    remove_source_branch = false

    [review]
    bot_username = "project_67827362_bot"

The bot username defaults to ``REPOACH_REVIEW_BOT_LOGIN``
(``github-actions[bot]``) on GitHub and to empty on GitLab unless that
variable is set explicitly — a GitLab run must name its bot, or trust
no marker.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from ..core.config import get_settings
from ..core.logging import get_logger
from .github import GitHubForge
from .gitlab import GitLabForge
from .protocol import Forge, ForgeError, ForgeUnavailableError, MergeMethod

_log = get_logger(__name__)

ForgeName = Literal["github", "gitlab"]
FORGE_NAMES: tuple[ForgeName, ...] = ("github", "gitlab")
CONFIG_FILENAME = "repoach.toml"


class ForgeConfig(BaseModel):
    """The forge keys of ``repoach.toml``.

    Attributes:
        forge: ``github`` / ``gitlab``, or ``None`` when not configured.
        bot_username: ``[review].bot_username``, or ``None`` when absent.
        merge_method: How changes are merged (default ``squash``).
        remove_source_branch: Delete the head branch on merge.
        root: The directory the file was read from, when found.
    """

    model_config = ConfigDict(frozen=True)

    forge: ForgeName | None = None
    bot_username: str | None = None
    merge_method: MergeMethod = MergeMethod.SQUASH
    remove_source_branch: bool = True
    root: Path | None = None


def _git_toplevel(cwd: Path) -> Path | None:
    """Return ``git rev-parse --show-toplevel`` for ``cwd``, or ``None``."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("forge.select.toplevel_failed", error=str(exc)[:120])
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def load_forge_config(cwd: Path | None = None) -> ForgeConfig:
    """Read the forge keys from ``repoach.toml`` at the repository root.

    Args:
        cwd: Any directory inside the repository (default: the process cwd).

    Returns:
        The parsed :class:`ForgeConfig`; defaults when no file exists.

    Raises:
        ValueError: The file exists but is not valid TOML, or names an
            unknown forge / merge method.
    """
    base = (cwd or Path.cwd()).resolve()
    root = _git_toplevel(base) or base
    path = root / CONFIG_FILENAME
    if not path.is_file():
        return ForgeConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    raw_section = data.get("repoach")
    raw_review = data.get("review")
    section: dict[str, object] = raw_section if isinstance(raw_section, dict) else {}
    review: dict[str, object] = raw_review if isinstance(raw_review, dict) else {}
    raw: dict[str, object] = {"root": root}
    for key in ("forge", "merge_method", "remove_source_branch"):
        value = section.get(key, data.get(key))
        if value is not None:
            raw[key] = value
    if review.get("bot_username") is not None:
        raw["bot_username"] = review["bot_username"]
    return ForgeConfig.model_validate(raw)


def infer_forge_from_url(url: str) -> ForgeName | None:
    """Infer the forge from a remote URL's host, never guessing an unknown host.

    Args:
        url: ``https://host/…``, ``ssh://git@host/…`` or scp-like ``git@host:…``.

    Returns:
        ``github`` for ``github.com``, ``gitlab`` for a host named
        ``gitlab.*`` / ``*.gitlab.*``, else ``None``.
    """
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        host = urlparse(text).hostname or ""
    else:
        host = text.split("@", 1)[-1].split(":", 1)[0]
    host = host.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    labels = host.split(".")
    if "gitlab" in labels:
        return "gitlab"
    return None


def _origin_url(cwd: Path) -> str:
    """Return ``git remote get-url origin`` for ``cwd`` (``""`` when absent)."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("forge.select.origin_failed", error=str(exc)[:120])
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_forge_name(
    config: ForgeConfig,
    *,
    override: str | None = None,
    cwd: Path | None = None,
) -> tuple[ForgeName, str]:
    """Resolve which forge to use and say why.

    Returns:
        ``(name, source)`` where ``source`` is ``flag``, ``env``,
        ``repoach.toml``, ``origin`` or ``default``.

    Raises:
        ForgeError: The flag or the environment names an unknown forge.
    """
    for value, source in ((override, "flag"), (get_settings().forge, "env")):
        if value:
            name = value.strip().lower()
            if name not in FORGE_NAMES:
                raise ForgeError(f"unknown forge {value!r} (from {source}); use github or gitlab")
            return name, source
    if config.forge is not None:
        return config.forge, CONFIG_FILENAME
    inferred = infer_forge_from_url(_origin_url((cwd or Path.cwd()).resolve()))
    if inferred is not None:
        return inferred, "origin"
    return "github", "default"


def resolve_bot_username(forge: ForgeName, config: ForgeConfig) -> str:
    """The trusted bot identity for ``forge`` (G4 defaults per forge)."""
    if config.bot_username is not None:
        return config.bot_username
    settings = get_settings()
    if forge == "github" or "review_bot_login" in settings.model_fields_set:
        return settings.review_bot_login
    return ""


def select_forge(
    config: ForgeConfig | None = None,
    *,
    override: str | None = None,
    cwd: Path | None = None,
) -> Forge:
    """Build the forge for the repository at ``cwd``.

    Args:
        config: Pre-loaded configuration; read from ``repoach.toml`` when ``None``.
        override: The ``--forge`` flag value, highest precedence.
        cwd: The clone the forge operates in (default: the process cwd).

    Returns:
        A :class:`~repoach.forge.github.GitHubForge` or
        :class:`~repoach.forge.gitlab.GitLabForge`.

    Raises:
        ForgeUnavailableError: The selected forge's binary is not installed.
        ForgeError: An unknown forge name was given.
    """
    resolved_cwd = (cwd or Path.cwd()).resolve()
    cfg = config if config is not None else load_forge_config(resolved_cwd)
    name, source = resolve_forge_name(cfg, override=override, cwd=resolved_cwd)
    bot = resolve_bot_username(name, cfg)
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
