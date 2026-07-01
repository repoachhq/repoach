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


class Settings(BaseSettings):
    """Global runtime settings.

    Values are read from environment variables with the ``FEROVA_`` prefix
    or from a ``.env`` file at the project root.  Provider API keys live
    in the llm_proxy's own settings module, not here.
    """

    model_config = SettingsConfigDict(
        env_file=("chains.env", ".env"),
        env_prefix="FEROVA_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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
        default=Path("./data/ferova.db"),
        description="Path to the main SQLite database file (review-team persistence).",
    )

    claude_code_routine_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FEROVA_CLAUDE_CODE_ROUTINE_ID", "CLAUDE_CODE_ROUTINE_ID"),
    )
    claude_code_routine_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "FEROVA_CLAUDE_CODE_ROUTINE_TOKEN", "CLAUDE_CODE_ROUTINE_TOKEN"
        ),
    )

    llm_proxy_base_url: str = "http://localhost:8082"
    llm_proxy_auth_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FEROVA_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
        description=(
            "Shared secret with the local llm_proxy sidecar.  Used by ferova "
            "(as bearer) and the proxy (to authenticate inbound calls).  When "
            "``env=prod`` we refuse to boot if this is unset, otherwise the proxy "
            "might be running open."
        ),
    )

    agentmemory_url: str = Field(
        default="http://localhost:3111",
        validation_alias=AliasChoices("FEROVA_AGENTMEMORY_URL", "AGENTMEMORY_URL"),
        description="Base URL of the local agentmemory service (the builder memory layer).",
    )
    builder_memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("FEROVA_BUILDER_MEMORY_ENABLED", "BUILDER_MEMORY_ENABLED"),
        description=(
            "When false, the builder's recall-before / remember-after agentmemory "
            "loop is a no-op (the hard kill-switch)."
        ),
    )
    review_memory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("FEROVA_REVIEW_MEMORY_ENABLED", "REVIEW_MEMORY_ENABLED"),
        description=(
            "When false, the review bench's recall-before-review agentmemory loop "
            "is a no-op (the hard kill-switch for the review scope)."
        ),
    )
    review_lessons_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("FEROVA_REVIEW_LESSONS_ENABLED", "REVIEW_LESSONS_ENABLED"),
        description=(
            "When false, the review cycle does NOT distill verified findings into "
            "builder-scoped lessons at cycle end (SP-REVIEW-LESSONS kill-switch)."
        ),
    )

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
                "env=prod requires FEROVA_ANTHROPIC_AUTH_TOKEN — refusing "
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
