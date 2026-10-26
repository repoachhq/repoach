"""The effective project configuration and where each value came from.

Resolution order for every key (umbrella ``SP-UNIVERSAL-REPO``): a CLI
flag (applied by the command itself) > a ``REPOACH_*`` environment
layer (:func:`repoach.core.config.configured_env_value`) >
``repoach.toml`` > a fact read from the clone (the ``origin`` remote, for
the forge only) > the built-in default.  :func:`resolve_project_config`
returns the merged :class:`~repoach.project.config.ProjectConfig` with a
source tag per key, which ``repoach config show`` and ``repoach doctor``
print and the forge selection consumes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError

from ..core.config import configured_env_value
from ..core.paths import default_logs_dir, is_repoach_checkout, origin_url, resolve_db_path
from .config import (
    ConfigError,
    ForgeName,
    ProjectConfig,
    config_path,
    load_project_config,
)
from .toml_write import TomlScalar

ConfigSource = Literal["default", "file", "env", "origin"]
"""Where an effective value came from (``flag`` is the caller's own layer)."""

DEFAULT_FORGE: ForgeName = "github"
"""Forge used when neither the environment, the file nor the origin names one."""

GITHUB_DEFAULT_BOT = "github-actions[bot]"
"""Trusted bot login on GitHub when nothing names one (SP-EVIDENCE-SENTINEL-AUTHOR)."""

ENV_NAMES: dict[str, tuple[str, ...]] = {
    "repoach.forge": ("REPOACH_FORGE",),
    "repoach.integration_branch": ("REPOACH_INTEGRATION_BRANCH", "INTEGRATION_BRANCH"),
    "repoach.release_branch": ("REPOACH_RELEASE_BRANCH", "RELEASE_BRANCH"),
    "repoach.merge_method": ("REPOACH_MERGE_METHOD",),
    "repoach.remove_source_branch": ("REPOACH_REMOVE_SOURCE_BRANCH",),
    "review.bot_username": ("REPOACH_REVIEW_BOT_LOGIN", "REVIEW_BOT_LOGIN"),
    "provider.kind": ("REPOACH_PROVIDER",),
    "provider.opus": ("REPOACH_MODEL_OPUS",),
    "provider.sonnet": ("REPOACH_MODEL_SONNET",),
    "provider.haiku": ("REPOACH_MODEL_HAIKU",),
    "provider.cli_path": ("REPOACH_CLAUDE_CODE_CLI_PATH",),
    "provider.base_url": ("REPOACH_OPENAI_BASE_URL",),
    "gates.refuter_max_judged": ("REPOACH_REFUTER_MAX_JUDGED",),
    "gates.refuter_context_tokens": ("REPOACH_REFUTER_CONTEXT_TOKENS",),
    "state.db_path": ("REPOACH_DB_PATH",),
    "state.logs_dir": ("REPOACH_LOGS_DIR",),
}
"""Environment names that outrank a ``repoach.toml`` key, preferred first."""


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


class ResolvedProjectConfig(BaseModel):
    """The effective configuration of one project.

    Attributes:
        root: The project root the file was read from.
        config: The merged configuration; ``repoach.forge`` is always
            set (``github`` when nothing names a forge).
        sources: Source tag per dotted key, for every key of the model.
    """

    model_config = ConfigDict(frozen=True)

    root: Path
    config: ProjectConfig
    sources: dict[str, ConfigSource]

    @property
    def forge(self) -> ForgeName:
        """The effective forge name."""
        return self.config.repoach.forge or DEFAULT_FORGE

    def source(self, dotted: str) -> ConfigSource:
        """Return where ``dotted`` came from (``default`` for an unknown key)."""
        return self.sources.get(dotted, "default")


def _all_keys(config: ProjectConfig) -> list[str]:
    """Return every ``section.key`` of the model, in declaration order."""
    keys: list[str] = []
    for section_name, section_field in type(config).model_fields.items():
        section_type = section_field.annotation
        if not isinstance(section_type, type) or not issubclass(section_type, BaseModel):
            continue
        for key, key_field in section_type.model_fields.items():
            keys.append(f"{section_name}.{key_field.alias or key}")
    return keys


def resolve_project_config(root: Path) -> ResolvedProjectConfig:
    """Merge environment, ``repoach.toml``, origin and defaults for ``root``.

    Args:
        root: The project root (git toplevel).

    Returns:
        The effective configuration with a source per key.

    Raises:
        ConfigError: The file is invalid, or an environment value has
            the wrong type for its key (the message names the variable).
    """
    file_config = load_project_config(root)
    data = file_config.model_dump(by_alias=True)
    sources: dict[str, ConfigSource] = {
        key: ("file" if file_config.is_set(key) else "default") for key in _all_keys(file_config)
    }
    env_origin: dict[str, str] = {}
    env_state: dict[str, str] = {}
    for dotted, names in ENV_NAMES.items():
        value = configured_env_value(names, root)
        if value is None:
            continue
        section, _, key = dotted.partition(".")
        sources[dotted] = "env"
        env_origin[dotted] = names[0]
        if section == "state":
            env_state[key] = value
        else:
            data[section][key] = value
    try:
        config = ProjectConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"] if isinstance(part, str))
        variable = env_origin.get(loc, "the environment")
        raise ConfigError(
            config_path(root), f"{variable}: {first['msg']}", key=loc.replace("schema_", "schema")
        ) from exc
    if env_state:
        config = config.model_copy(update={"state": config.state.model_copy(update=env_state)})
    for name in file_config.commands.named():
        sources[f"commands.{name}"] = "file"
    config = config.with_host_defaults(repoach_host=is_repoach_checkout(root))
    if config.repoach.forge is None:
        url = origin_url(root)
        inferred = infer_forge_from_url(url) if url is not None else None
        forge: ForgeName = inferred if inferred is not None else DEFAULT_FORGE
        sources["repoach.forge"] = "origin" if inferred is not None else "default"
        config = config.model_copy(
            update={"repoach": config.repoach.model_copy(update={"forge": forge})}
        )
    return ResolvedProjectConfig(root=root, config=config, sources=sources)


def default_bot_username(forge: ForgeName) -> str:
    """Return the trusted bot identity when nothing names one.

    Args:
        forge: The effective forge.

    Returns:
        ``github-actions[bot]`` on GitHub; empty (trust no marker) on GitLab.
    """
    return GITHUB_DEFAULT_BOT if forge == "github" else ""


def effective_bot_username(resolved: ResolvedProjectConfig, forge: ForgeName) -> str:
    """Return the bot identity for ``forge``: environment > file > forge default.

    Args:
        resolved: The resolved configuration.
        forge: The forge actually selected (a ``--forge`` flag may differ
            from ``resolved.forge``).

    Returns:
        The login trust markers are honoured from.
    """
    if resolved.source("review.bot_username") in ("env", "file"):
        return resolved.config.review.bot_username
    return default_bot_username(forge)


def effective_state_paths(resolved: ResolvedProjectConfig) -> tuple[Path, Path]:
    """Return the effective ledger path and diagnostic-dump directory.

    Args:
        resolved: The resolved configuration.

    Returns:
        ``(db_path, logs_dir)``, absolute: the environment or file value
        anchored to the project root, else the per-user default.
    """
    root = resolved.root
    state = resolved.config.state
    db_path = (root / state.db_path).resolve() if state.db_path else resolve_db_path(root)
    logs_dir = (root / state.logs_dir).resolve() if state.logs_dir else default_logs_dir(root)
    return db_path, logs_dir


UNDECLARED_COMMAND_NOTE = "not declared in repoach.toml; skipped on this repository"
"""Why an undeclared ``[commands]`` entry does not run on a foreign repository."""


def effective_rows(
    resolved: ResolvedProjectConfig,
) -> list[tuple[str, TomlScalar | None, str]]:
    """Return ``(dotted key, effective value, source)`` for every key.

    The forge, the bot identity and the ``[state]`` paths are shown as
    they take effect (forge default applied, paths made absolute).

    Args:
        resolved: The resolved configuration.

    Returns:
        One row per key, in the file's section order; the value is None
        for an undeclared command that will be skipped.
    """
    db_path, logs_dir = effective_state_paths(resolved)
    special: dict[str, TomlScalar] = {
        "repoach.forge": resolved.forge,
        "review.bot_username": effective_bot_username(resolved, resolved.forge),
        "state.db_path": str(db_path),
        "state.logs_dir": str(logs_dir),
    }
    rows: list[tuple[str, TomlScalar | None, str]] = []
    for section, values in resolved.config.model_dump(by_alias=True).items():
        for key, value in values.items():
            dotted = f"{section}.{key}"
            rows.append((dotted, special.get(dotted, value), resolved.source(dotted)))
    return rows
