"""Global configuration loaded from the environment and repoach's own env files.

The CLI and the agent side read, lowest precedence first,
``$XDG_CONFIG_HOME/repoach/env``, then ``<project>/.repoach.env``, then
the process environment (SP-UNIVERSAL-INSTALL G5).  The project file may
only set the keys of :data:`PROJECT_ENV_ALLOWED` (layout and
conventions) and is refused when git tracks it: endpoints, tokens,
paths and commands are user-level only, so a cloned repository cannot
redirect the user's token.  The project is the
git toplevel of the current working directory
(:func:`repoach.core.paths.project_root`), never an ancestor of the
installed package: on a tool install that ancestor is ``site-packages``
or a foreign project hosting the virtualenv, whose ``.env`` must never
be taken as repoach's.  The host repository's ``.env`` and ``chains.env``
are not read here; the llm_proxy gateway keeps its own anchors in
``repoach.llm_proxy.config.settings``.

Building :class:`Settings` never requires a gateway: the proxy base URL
is checked by :meth:`Settings.require_llm_proxy_base_url` at the point
a gateway client is actually built, so ``repoach --version`` and
``repoach doctor`` work on a machine that runs none.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import paths
from .paths import env_files, project_root

_INLINE_COMMENT_RE = re.compile(r"\s+#\s.*$")
"""Regex used by :meth:`Settings._strip_inline_comments` to scrub trailing
``\\s+# comment`` segments from string env values.  Defends against
``EnvironmentFile=`` / shell-sourced ``.env`` paths that don't honour
comment delimiters: a value like ``'dev   # dev | prod'`` reaching a
``Literal`` validator unscrubbed crashes the process at import time
(systemd then restart-loops it).  python-dotenv strips inline comments
when reading the file itself; env-vars already in ``os.environ`` bypass
that path."""


def _anchored_env_files(root: Path | None = None) -> tuple[Path, ...]:
    """Return the env files :class:`Settings` reads, lowest precedence first.

    In a repoach checkout that has not run ``repoach migrate-config``
    yet, its legacy ``.env`` takes the place of ``.repoach.env``, with a
    one-line deprecation notice; a foreign project's ``.env`` never does.

    Args:
        root: Project root; defaults to :func:`project_root` of the
            current working directory.  Exposed as a parameter so tests
            can point resolution at an isolated fixture tree.

    Returns:
        ``(user_config_dir()/env, root/.repoach.env)`` as absolute
        paths, the second replaced by ``root/.env`` in the legacy case.
    """
    base = root if root is not None else project_root()
    user_env, project_env = env_files(base)
    legacy = paths.legacy_env_file(base)
    if legacy is None:
        return (user_env, project_env)
    paths.notify_deprecated(
        f"reading legacy {legacy}; move the CLI settings to {project_env} or {user_env} "
        "(run 'repoach migrate-config')"
    )
    return (user_env, legacy)


PROJECT_ENV_ALLOWED: dict[str, str] = {
    "REPOACH_LOG_LEVEL": "log_level",
    "REPOACH_INTEGRATION_BRANCH": "REPOACH_INTEGRATION_BRANCH",
    "INTEGRATION_BRANCH": "REPOACH_INTEGRATION_BRANCH",
    "REPOACH_RELEASE_BRANCH": "REPOACH_RELEASE_BRANCH",
    "RELEASE_BRANCH": "REPOACH_RELEASE_BRANCH",
    "REPOACH_BUILDER_MEMORY_ENABLED": "REPOACH_BUILDER_MEMORY_ENABLED",
    "BUILDER_MEMORY_ENABLED": "REPOACH_BUILDER_MEMORY_ENABLED",
    "REPOACH_REVIEW_MEMORY_ENABLED": "REPOACH_REVIEW_MEMORY_ENABLED",
    "REVIEW_MEMORY_ENABLED": "REPOACH_REVIEW_MEMORY_ENABLED",
    "REPOACH_REVIEW_LESSONS_ENABLED": "REPOACH_REVIEW_LESSONS_ENABLED",
    "REVIEW_LESSONS_ENABLED": "REPOACH_REVIEW_LESSONS_ENABLED",
    "REPOACH_PLANNER_PARSE_ATTEMPTS": "planner_parse_attempts",
    "REPOACH_AUTOMERGE_CI_WAIT_SECONDS": "automerge_ci_wait_seconds",
    "REPOACH_AUTOMERGE_CI_POLL_INTERVAL": "automerge_ci_poll_interval",
}
"""Keys a project-level ``.repoach.env`` may set, mapped to their init key.

Only layout and convention knobs (branch names, kill-switches, budgets).
Anything that is a secret, an endpoint, a filesystem location, an
executable or a trust identity -- the gateway URL and token, the
agentmemory URL, routine credentials, ``db_path``/``logs_dir``,
``coder_pythons``, ``review_bot_login`` -- is user-level only
(``~/.config/repoach/env`` or the process environment): a cloned
repository must never be able to redirect the user's token or code to
a host or binary of its choosing (SP-UNIVERSAL-INSTALL)."""


def _env_names_by_target() -> dict[str, set[str]]:
    """Group the allow-listed env names by the init key they feed."""
    grouped: dict[str, set[str]] = {}
    for name, target in PROJECT_ENV_ALLOWED.items():
        grouped.setdefault(target, set()).add(name)
    return grouped


def _project_env_overrides(root: Path) -> dict[str, str]:
    """Return the allow-listed values of ``root/.repoach.env``.

    The file is refused outright when git tracks it (it came with the
    clone), and every key outside :data:`PROJECT_ENV_ALLOWED` is dropped
    with one notice naming the file.  A key the process environment
    already sets (under any of its names) is left to the environment.

    Args:
        root: The project root.

    Returns:
        Init keyword arguments for :class:`Settings`.
    """
    project_env = env_files(root)[1]
    if not project_env.is_file():
        return {}
    if paths.is_tracked(root, project_env):
        paths.notify(
            f"refusing {project_env}: it is tracked by git, so it came with the "
            "repository; keep it untracked and gitignored"
        )
        return {}
    environment = {name.upper() for name in os.environ}
    names_by_target = _env_names_by_target()
    overrides: dict[str, str] = {}
    rejected: list[str] = []
    for raw_key, value in dotenv_values(project_env).items():
        key = raw_key.upper()
        target = PROJECT_ENV_ALLOWED.get(key)
        if target is None:
            rejected.append(raw_key)
            continue
        if value is None or names_by_target[target] & environment:
            continue
        overrides[target] = value
    if rejected:
        paths.notify(
            f"ignoring {', '.join(sorted(rejected))} in {project_env}: provider URLs, "
            "tokens, keys, paths and commands are user-level only "
            f"({env_files(root)[0]} or the environment)"
        )
    return overrides


def _unrestricted_env_files(root: Path) -> tuple[Path, ...]:
    """Return the env files read in full: the user file, plus a legacy ``.env``.

    A legacy ``.env`` that git tracks is refused like a tracked
    ``.repoach.env`` (a fork of repoach could otherwise ship one).

    Args:
        root: The project root.

    Returns:
        The files :class:`Settings` hands to pydantic-settings.
    """
    files = _anchored_env_files(root)
    user_env, second = files
    if second.name != paths.LEGACY_ENV_FILE_NAME:
        return (user_env,)
    if paths.is_tracked(root, second):
        paths.notify(f"refusing {second}: it is tracked by git, so it came with the repository")
        return (user_env,)
    return files


def _default_db_path() -> Path:
    """Return the default ledger path for the current project.

    Returns:
        The ledger shared with the gateway
        (see :func:`repoach.core.paths.resolve_db_path`).
    """
    return paths.resolve_db_path(project_root())


def _default_logs_dir() -> Path:
    """Return the default diagnostic-dump directory for the current project.

    Returns:
        ``user_data_dir()/<project-slug>/logs``
        (see :func:`repoach.core.paths.default_logs_dir`).
    """
    return paths.default_logs_dir(project_root())


class Settings(BaseSettings):
    """Global runtime settings.

    Values are read from environment variables with the ``REPOACH_``
    prefix or from repoach's own env files (module docstring).  Fields
    carrying an explicit ``AliasChoices`` also accept the bare legacy
    name (CI secrets use it), ``REPOACH_`` winning precedence. Provider
    API keys live in the llm_proxy's own settings module, not here.
    """

    model_config = SettingsConfigDict(
        env_file=(),
        env_prefix="REPOACH_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __init__(self, **data: Any) -> None:
        """Construct :class:`Settings`, anchoring env-file discovery.

        ``model_config.env_file`` is baked in once, at class-definition
        time, so it cannot follow the working directory a command runs
        from.  The anchor is therefore applied here instead, through
        pydantic-settings' per-instance ``_env_file`` override, at the
        moment a :class:`Settings` is actually built.  The user file
        (and a repoach checkout's untracked legacy ``.env``) is read in
        full; the project's ``.repoach.env`` contributes only its
        allow-listed keys (:data:`PROJECT_ENV_ALLOWED`), passed as init
        values below the process environment.

        Args:
            **data: Forwarded to
                :class:`~pydantic_settings.BaseSettings`.  A caller
                that already passes an explicit ``_env_file`` (tests
                disabling file loading altogether) is honoured as-is.
        """
        if "_env_file" not in data:
            root = project_root()
            data["_env_file"] = _unrestricted_env_files(root)
            for key, value in _project_env_overrides(root).items():
                data.setdefault(key, value)
        super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _strip_inline_comments(cls, data: Any) -> Any:
        r"""Strip trailing ``\s+# comment`` from string inputs.

        URL fragments (``...#frag``) are preserved because they have no
        whitespace before the ``#``.  See :data:`_INLINE_COMMENT_RE`
        for the rationale.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: (_INLINE_COMMENT_RE.sub("", value).rstrip() if isinstance(value, str) else value)
            for key, value in data.items()
        }

    env: Literal["dev", "prod"] = Field(
        default="dev",
        description="Runtime environment.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Minimum log level emitted by the application.",
    )

    db_path: Path = Field(
        default_factory=_default_db_path,
        description=(
            "Path to the main SQLite database file (review-team persistence). "
            "Defaults to $XDG_DATA_HOME/repoach/<project-slug>/repoach.db, "
            "one ledger per project, never inside the checkout or the "
            "installed package (SP-UNIVERSAL-INSTALL G4). A relative "
            "REPOACH_DB_PATH resolves against the project root."
        ),
    )
    logs_dir: Path = Field(
        default_factory=_default_logs_dir,
        description=(
            "Directory for diagnostic dumps (parse failures, rejected Coder "
            "fixes). Defaults to $XDG_DATA_HOME/repoach/<project-slug>/logs, "
            "never under the installed package; CI points it at the "
            "workspace's logs/ so the artifact upload finds the files. A "
            "relative value resolves against the project root."
        ),
    )

    claude_code_routine_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "REPOACH_CLAUDE_CODE_ROUTINE_ID",
            "CLAUDE_CODE_ROUTINE_ID",
        ),
    )
    claude_code_routine_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "REPOACH_CLAUDE_CODE_ROUTINE_TOKEN",
            "CLAUDE_CODE_ROUTINE_TOKEN",
        ),
    )

    llm_proxy_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the local llm_proxy sidecar -- the bearer target "
            "for llm_proxy_auth_token.  Resolved from the anchored "
            "chains.env/.env pair or an explicit REPOACH_LLM_PROXY_BASE_URL "
            "environment variable; carries no baked-in host default, "
            "because a wrong guess would silently ship the bearer secret "
            "to an unconfigured, possibly wrong, proxy "
            "(SP-CONFIG-ENV-ANCHOR)."
        ),
    )
    llm_proxy_auth_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "REPOACH_ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
        ),
        description=(
            "Shared secret with the local llm_proxy sidecar.  Used by repoach "
            "(as bearer) and the proxy (to authenticate inbound calls).  When "
            "``env=prod`` we refuse to boot if this is unset, otherwise the proxy "
            "might be running open."
        ),
    )

    agentmemory_url: str = Field(
        default="http://localhost:3111",
        validation_alias=AliasChoices("REPOACH_AGENTMEMORY_URL", "AGENTMEMORY_URL"),
        description="Base URL of the local agentmemory service (the builder memory layer).",
    )
    builder_memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "REPOACH_BUILDER_MEMORY_ENABLED",
            "BUILDER_MEMORY_ENABLED",
        ),
        description=(
            "When false, the builder's recall-before / remember-after agentmemory "
            "loop is a no-op (the hard kill-switch)."
        ),
    )
    review_memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "REPOACH_REVIEW_MEMORY_ENABLED",
            "REVIEW_MEMORY_ENABLED",
        ),
        description=(
            "When false, the review bench's recall-before-review agentmemory loop "
            "is a no-op (the hard kill-switch for the review scope)."
        ),
    )
    review_lessons_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "REPOACH_REVIEW_LESSONS_ENABLED",
            "REVIEW_LESSONS_ENABLED",
        ),
        description=(
            "When false, the review cycle does NOT distill verified findings into "
            "builder-scoped lessons at cycle end (SP-REVIEW-LESSONS kill-switch)."
        ),
    )

    review_bot_login: str = Field(
        default="github-actions[bot]",
        validation_alias=AliasChoices(
            "REPOACH_REVIEW_BOT_LOGIN",
            "REVIEW_BOT_LOGIN",
        ),
        description=(
            "GitHub login the review-bot writers authenticate as when posting "
            "comments and replies. Trust-bearing markers (the evidence-reply "
            "sentinel, the review-archive HTML marker) are honoured only on "
            "comments whose ``user.login`` matches this value "
            "(SP-EVIDENCE-SENTINEL-AUTHOR) — any other author is untrusted."
        ),
    )

    planner_parse_attempts: str = Field(
        default="",
        description=(
            "Raw override for the Planner's per-session parse-attempt budget "
            "(REPOACH_PLANNER_PARSE_ATTEMPTS). Kept as a raw string -- an "
            "empty/unset value or a non-integer falls back to the built-in "
            "default of 5, and any parsed value is clamped to >=1 -- both at "
            "the read site in review/planner.py, exactly as before this moved "
            "off a raw os.environ read (SP-CONSISTENCY-SWEEP)."
        ),
    )
    coder_pythons: str = Field(
        default="",
        description=(
            "CSV of pytest interpreter names/paths for the Coder's local gate "
            "matrix (REPOACH_CODER_PYTHONS), e.g. 'python3.11,python3.13'. "
            "Empty/unset resolves to [None] (bare pytest on PATH); each listed "
            "entry is validated against shutil.which at the read site in "
            "review/coder_loop.py, exactly as before this moved off a raw "
            "os.environ read (SP-CONSISTENCY-SWEEP)."
        ),
    )

    integration_branch: str = Field(
        default="develop",
        validation_alias=AliasChoices(
            "REPOACH_INTEGRATION_BRANCH",
            "INTEGRATION_BRANCH",
        ),
        description=(
            "Name of the integration branch PRs target and the review-bot "
            "team auto-merges into (default 'develop'). Overriding this lets "
            "a repo that doesn't use the develop/main two-branch model "
            "configure its own integration branch name instead of hitting "
            "the hardcoded 'develop' literal (SP-BRANCH-CONFIG)."
        ),
    )
    forge: str = Field(
        default="",
        validation_alias=AliasChoices("REPOACH_FORGE"),
        description=(
            "Which forge the repository lives on: 'github' or 'gitlab'. Empty "
            "(the default) defers to repoach.toml, then to the origin remote's "
            "host, then to GitHub (SP-UNIVERSAL-FORGE G4)."
        ),
    )
    release_branch: str = Field(
        default="main",
        validation_alias=AliasChoices(
            "REPOACH_RELEASE_BRANCH",
            "RELEASE_BRANCH",
        ),
        description=(
            "Name of the release branch the operator manually merges "
            "integration_branch into (default 'main'). Read by the "
            "release gate's git ref commands and by release-PR helpers "
            "(SP-BRANCH-CONFIG)."
        ),
    )

    automerge_ci_wait_seconds: int = Field(
        default=720,
        ge=0,
        description="Total CI-gate wait budget in seconds; 0 = single evaluation, fail fast.",
    )
    automerge_ci_poll_interval: int = Field(
        default=30,
        ge=1,
        description="Seconds between CI rollup polls when the budget allows waiting.",
    )

    @model_validator(mode="after")
    def _anchor_relative_paths(self) -> Settings:
        """Anchor a relative ``db_path`` / ``logs_dir`` against the project root.

        A relative ``REPOACH_DB_PATH`` / ``REPOACH_LOGS_DIR`` resolves against
        :func:`repoach.core.paths.project_root` (the git toplevel of
        the working directory), never against the installed package.
        An already-absolute value (the CI shape) and the built-in
        per-project default pass through untouched.

        Returns:
            The same :class:`Settings` instance, with each path
            replaced by its anchored, resolved, absolute form when it
            started out relative.
        """
        root = project_root()
        if not self.db_path.is_absolute():
            self.db_path = (root / self.db_path).resolve()
        if not self.logs_dir.is_absolute():
            self.logs_dir = (root / self.logs_dir).resolve()
        return self

    @model_validator(mode="after")
    def require_proxy_token_in_prod(self) -> Settings:
        """Enforce the boot guard documented on ``llm_proxy_auth_token``.

        Returns:
            The same :class:`Settings` instance.

        Raises:
            ValueError: When ``env == "prod"`` and
                ``llm_proxy_auth_token`` is unset or empty — the proxy
                might be running open.
        """
        if self.env == "prod" and (
            self.llm_proxy_auth_token is None or not self.llm_proxy_auth_token.get_secret_value()
        ):
            raise ValueError(
                "env=prod requires REPOACH_ANTHROPIC_AUTH_TOKEN — refusing "
                "to boot without the llm_proxy shared secret."
            )
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    The settings object is built lazily on the first call.  Building it
    creates no directory: each store creates its database's parent on
    first write, so a command that only reads configuration leaves
    nothing behind in ``$HOME`` (SP-UNIVERSAL-INSTALL).

    Returns:
        The cached :class:`Settings` singleton.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def require_llm_proxy_base_url(settings: Settings) -> str:
    """Return the gateway base URL, failing loud when none is configured.

    Called by the code that builds a gateway client, not at
    :class:`Settings` construction: a command that never talks to the
    gateway (``repoach --version``, ``repoach doctor``) must not need
    one (SP-UNIVERSAL-INSTALL G2).  Reads the attribute only, so test
    doubles standing in for :class:`Settings` keep working.

    Args:
        settings: The resolved settings (or a stand-in exposing
            ``llm_proxy_base_url``).

    Returns:
        The configured ``llm_proxy_base_url``.

    Raises:
        ValueError: When no env file and no explicit
            ``REPOACH_LLM_PROXY_BASE_URL`` resolved a value.  Proceeding
            anyway would POST the ``llm_proxy_auth_token`` bearer secret
            at an unconfigured, possibly wrong, host (SP-CONFIG-ENV-ANCHOR).
    """
    base_url = settings.llm_proxy_base_url
    if not base_url:
        raise ValueError(
            "llm_proxy_base_url unresolved -- set REPOACH_LLM_PROXY_BASE_URL "
            "in the environment or ~/.config/repoach/env (a project file may not set it); "
            "refusing to default to a wrong proxy"
        )
    return base_url
