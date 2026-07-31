"""Global configuration loaded from the environment / ``.env`` file.

Pydantic-settings reads ``chains.env`` first (so its canonical
``MODEL_<CAPABILITY>`` definitions become defaults), then ``.env`` on
top so per-machine secrets and overrides win. The local proxy and the
GitHub Actions workflow share the same definition through this layered
load.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INLINE_COMMENT_RE = re.compile(r"\s+#\s.*$")
"""Regex used by :meth:`Settings._strip_inline_comments` to scrub trailing
``\\s+# comment`` segments from string env values.  Defends against
``EnvironmentFile=`` / shell-sourced ``.env`` paths that don't honour
comment delimiters: a value like ``'dev   # dev | prod'`` reaching a
``Literal`` validator unscrubbed crashes the process at import time
(systemd then restart-loops it).  python-dotenv strips inline comments
when reading the file itself; env-vars already in ``os.environ`` bypass
that path."""


def _repo_root() -> Path:
    """Locate the repository root by walking up from this module's file.

    Returns the nearest ancestor directory containing ``pyproject.toml``
    (the same anchor the lint CLIs walk to), so ``.env``/``chains.env``
    resolution never depends on the process's current working
    directory (SP-CONFIG-ENV-ANCHOR).  A systemd unit, a cron timer or
    any foreign CWD must load the same files a repo-root invocation
    would.  Falls back to the installed package's own top-level
    directory when no ``pyproject.toml`` is found above it (e.g. a
    wheel install with no source checkout alongside it) -- kept as a
    plain module-level function, rather than a constant baked at
    import time, so tests can monkeypatch it to point at an isolated
    fixture tree.

    Returns:
        The absolute anchor directory for env-file resolution.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[1]


def _anchored_env_files(root: Path | None = None) -> tuple[Path, Path]:
    """Return the anchored ``(chains.env, .env)`` pair, in load order.

    ``chains.env`` first so its canonical ``MODEL_<CAPABILITY>``
    definitions become the baseline that ``.env`` layers per-machine
    secrets/overrides on top of (module docstring rationale).

    Args:
        root: Anchor directory; defaults to :func:`_repo_root`.  Exposed
            as a parameter (rather than inlining ``_repo_root()``) so
            tests can point resolution at an isolated fixture tree
            without touching the real deployed ``.env``.

    Returns:
        Absolute paths to ``chains.env`` and ``.env`` under ``root``.
    """
    base = root if root is not None else _repo_root()
    return (base / "chains.env", base / ".env")


class Settings(BaseSettings):
    """Global runtime settings.

    Values are read from environment variables with the ``REPOACH_`` prefix
    or from a ``.env`` file at the project root.  Fields carrying an
    explicit ``AliasChoices`` also accept the bare legacy name (CI
    secrets use it), ``REPOACH_`` winning precedence. Provider API keys
    live in the llm_proxy's own settings module, not here.
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
        time, so it cannot itself be recomputed against a monkeypatched
        :func:`_repo_root` inside a test.  The anchor is therefore
        applied here instead, through pydantic-settings' per-instance
        ``_env_file`` override, at the moment a :class:`Settings` is
        actually built.

        Args:
            **data: Forwarded to
                :class:`~pydantic_settings.BaseSettings`.  A caller
                that already passes an explicit ``_env_file`` (tests
                disabling file loading altogether) is honoured as-is.
        """
        if "_env_file" not in data:
            data["_env_file"] = _anchored_env_files()
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
        default=Path("./data/repoach.db"),
        description="Path to the main SQLite database file (review-team persistence).",
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
    def _anchor_relative_db_path(self) -> Settings:
        """Anchor a relative ``db_path`` against :func:`_repo_root`.

        Mirrors ``_anchored_env_files``: a bare relative value (the
        built-in default or an operator-supplied relative
        ``REPOACH_DB_PATH`` override) must resolve identically
        regardless of the process's current working directory. An
        already-absolute value (the CI shape) passes through
        untouched.

        Returns:
            The same :class:`Settings` instance, with ``db_path``
            replaced by its anchored, resolved, absolute form when it
            started out relative.
        """
        if not self.db_path.is_absolute():
            self.db_path = (_repo_root() / self.db_path).resolve()
        return self

    @model_validator(mode="after")
    def require_llm_proxy_base_url(self) -> Settings:
        """Fail loud instead of silently defaulting the proxy base URL.

        Returns:
            The same :class:`Settings` instance.

        Raises:
            ValueError: When neither an anchored env file nor an
                explicit ``REPOACH_LLM_PROXY_BASE_URL`` resolved a
                value.  Booting anyway would POST the
                ``llm_proxy_auth_token`` bearer secret at an
                unconfigured, possibly wrong, host (SP-CONFIG-ENV-ANCHOR).
        """
        if not self.llm_proxy_base_url:
            raise ValueError(
                "llm_proxy_base_url unresolved -- no anchored env file and "
                "no REPOACH_LLM_PROXY_BASE_URL; refusing to default to a "
                "wrong proxy"
            )
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

    def ensure_dirs(self) -> None:
        """Create the parent directory of :attr:`db_path`.

        Idempotent and safe to call repeatedly.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance.

    The settings object is built lazily on the first call ; persistent
    directories are created at that point.

    Returns:
        The cached :class:`Settings` singleton.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
