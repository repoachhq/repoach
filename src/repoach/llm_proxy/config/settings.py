"""Centralized configuration using Pydantic Settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from loguru import logger
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .nim import NimSettings
from .provider_ids import SUPPORTED_PROVIDER_IDS


def _env_files() -> tuple[Path, ...]:
    """Return env file paths in priority order (later overrides earlier).

    SP-CHAINS-SINGLE-SOURCE (2026-06-21): ``chains.env`` is read LAST so it
    is AUTHORITATIVE for the capability chains (``MODEL_OPUS`` /
    ``MODEL_SONNET`` / ``MODEL_HAIKU``) — a per-machine
    ``.env`` can no longer silently shadow the canonical tracked file.
    ``chains.env`` defines only those capability keys, so ordering it last
    changes precedence for nothing else; ``.env`` still wins for credentials and
    every other setting. (Supersedes SP-CHAINS-DOTENV-PRECEDENCE, which had
    ``chains.env`` ahead of ``.env`` and let ``.env`` override the chains.)
    An explicit ``os.environ`` value still wins over every file (CI's
    ``source chains.env`` sets the same value).
    """
    files: list[Path] = [
        Path.home() / ".config" / "free-claude-code" / ".env",
        Path(".env"),
    ]
    if explicit := os.environ.get("FCC_ENV_FILE"):
        files.append(Path(explicit))
    files.append(Path("chains.env"))
    return tuple(files)


def _configured_env_files(model_config: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return the currently configured env files for Settings."""
    configured = model_config.get("env_file")
    if configured is None:
        return ()
    if isinstance(configured, (str, Path)):
        return (Path(configured),)
    return tuple(Path(item) for item in configured)


def _env_file_contains_key(path: Path, key: str) -> bool:
    """Check whether a dotenv-style file defines the given key."""
    return _env_file_value(path, key) is not None


def _env_file_value(path: Path, key: str) -> str | None:
    """Return a dotenv value when the file explicitly defines the key."""
    if not path.is_file():
        return None

    try:
        values = dotenv_values(path)
    except OSError as exc:
        logger.debug(
            "llm_proxy_settings.dotenv_read_failed",
            path=str(path),
            error_type=type(exc).__name__,
        )
        return None

    if key not in values:
        return None
    value = values[key]
    return "" if value is None else value


def _env_file_override(model_config: Mapping[str, Any], key: str) -> str | None:
    """Return the last configured dotenv value that explicitly defines a key."""
    configured_value: str | None = None
    for env_file in _configured_env_files(model_config):
        value = _env_file_value(env_file, key)
        if value is not None:
            configured_value = value
    return configured_value


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_AUTH_TOKEN_ALIASES = (
    "REPOACH_ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
)
"""Auth-token env names in precedence order: canonical REPOACH_ first,
bare legacy second — the same order the field's ``AliasChoices``
resolves, so dotenv-precedence probing stays in lockstep with
validation."""
"""Bind addresses reachable from this machine only — the secure default.

A non-loopback bind without an auth token is refused at construction
(SP-PROXY-SECURE-DEFAULTS): a fresh deployment that forgets ``.env``
must not serve an unauthenticated LLM proxy on all interfaces.
"""

_LEGACY_TO_REPOACH_ALIAS: dict[str, str] = {
    "OPENROUTER_API_KEY": "REPOACH_OPENROUTER_API_KEY",
    "NVIDIA_NIM_API_KEY": "REPOACH_NVIDIA_NIM_API_KEY",
    "KIMI_API_KEY": "REPOACH_KIMI_API_KEY",
    "GROQ_API_KEY": "REPOACH_GROQ_API_KEY",
    "CEREBRAS_API_KEY": "REPOACH_CEREBRAS_API_KEY",
    "DEEPSEEK_API_KEY": "REPOACH_DEEPSEEK_API_KEY",
    "ARTIFICIAL_ANALYSIS_API_KEY": "REPOACH_ARTIFICIAL_ANALYSIS_API_KEY",
    "BREAKER_ENABLED": "REPOACH_BREAKER_ENABLED",
    "BREAKER_TTL_S": "REPOACH_BREAKER_TTL_S",
    "BREAKER_TTL_TERMINAL_S": "REPOACH_BREAKER_TTL_TERMINAL_S",
    "BREAKER_PROBE_SEED_ENABLED": "REPOACH_BREAKER_PROBE_SEED_ENABLED",
    "BREAKER_PROBE_SEED_DB": "REPOACH_BREAKER_PROBE_SEED_DB",
    "BREAKER_STATE_PERSIST_ENABLED": "REPOACH_BREAKER_STATE_PERSIST_ENABLED",
    "EFFORT_MAP_SEED_ENABLED": "REPOACH_EFFORT_MAP_SEED_ENABLED",
    "CLAUDE_CODE_CLI_PATH": "REPOACH_CLAUDE_CODE_CLI_PATH",
    "CLAUDE_CODE_DEFAULT_MODEL": "REPOACH_CLAUDE_CODE_DEFAULT_MODEL",
    "CLAUDE_CODE_SUBPROCESS_TIMEOUT": "REPOACH_CLAUDE_CODE_SUBPROCESS_TIMEOUT",
    "MODEL": "REPOACH_PROXY_DEFAULT_MODEL",
    "NVIDIA_NIM_PROXY": "REPOACH_NVIDIA_NIM_PROXY",
    "OPENROUTER_PROXY": "REPOACH_OPENROUTER_PROXY",
    "PROVIDER_RATE_LIMIT": "REPOACH_PROXY_PROVIDER_RATE_LIMIT",
    "PROVIDER_RATE_WINDOW": "REPOACH_PROXY_PROVIDER_RATE_WINDOW",
    "PROVIDER_MAX_CONCURRENCY": "REPOACH_PROXY_PROVIDER_MAX_CONCURRENCY",
    "ENABLE_THINKING": "REPOACH_PROXY_ENABLE_THINKING",
    "BUDGET_RETRY_ENABLED": "REPOACH_PROXY_BUDGET_RETRY_ENABLED",
    "BUDGET_RETRY_FACTOR": "REPOACH_PROXY_BUDGET_RETRY_FACTOR",
    "BUDGET_RETRY_FLOOR": "REPOACH_PROXY_BUDGET_RETRY_FLOOR",
    "BUDGET_RETRY_CAP": "REPOACH_PROXY_BUDGET_RETRY_CAP",
    "HTTP_READ_TIMEOUT": "REPOACH_PROXY_HTTP_READ_TIMEOUT",
    "HTTP_WRITE_TIMEOUT": "REPOACH_PROXY_HTTP_WRITE_TIMEOUT",
    "HTTP_CONNECT_TIMEOUT": "REPOACH_PROXY_HTTP_CONNECT_TIMEOUT",
    "FIRST_BYTE_DEADLINE_S": "REPOACH_PROXY_FIRST_BYTE_DEADLINE_S",
    "HOST": "REPOACH_PROXY_HOST",
    "PORT": "REPOACH_PROXY_PORT",
    "LOG_FILE": "REPOACH_PROXY_LOG_FILE",
    "ANTHROPIC_AUTH_TOKEN": "REPOACH_ANTHROPIC_AUTH_TOKEN",
    "CHAINPILOT_APPLY_ENABLED": "REPOACH_CHAINPILOT_APPLY_ENABLED",
    "CHAINPILOT_MAX_MUTATIONS": "REPOACH_CHAINPILOT_MAX_MUTATIONS",
    "BREAKER_TTL_QUARANTINE_S": "REPOACH_BREAKER_TTL_QUARANTINE_S",
    "BREAKER_QUARANTINE_THRESHOLD": "REPOACH_BREAKER_QUARANTINE_THRESHOLD",
    "BREAKER_SLOW_LATENCY_GATE_S": "REPOACH_BREAKER_SLOW_LATENCY_GATE_S",
    "BREAKER_SLOW_TPS_FLOOR": "REPOACH_BREAKER_SLOW_TPS_FLOOR",
    "BREAKER_SLOW_K": "REPOACH_BREAKER_SLOW_K",
    "BREAKER_SLOW_N": "REPOACH_BREAKER_SLOW_N",
    "BREAKER_SLOW_TTL_S": "REPOACH_BREAKER_SLOW_TTL_S",
    "BREAKER_SLOW_SHADOW": "REPOACH_BREAKER_SLOW_SHADOW",
    "CREDITS_FLOOR_USD": "REPOACH_CREDITS_FLOOR_USD",
    "CREDITS_HEALTH_CACHE_TTL_S": "REPOACH_CREDITS_HEALTH_CACHE_TTL_S",
    "CHAIN_STATUS_WINDOW_H": "REPOACH_CHAIN_STATUS_WINDOW_H",
    "REGEN_MAX_CELL_AGE_H": "REPOACH_REGEN_MAX_CELL_AGE_H",
    "REGEN_SWEEP_PER_PROVIDER_CAP": "REPOACH_REGEN_SWEEP_PER_PROVIDER_CAP",
    "REGEN_SWEEP_PER_PROVIDER_CONCURRENCY": "REPOACH_REGEN_SWEEP_PER_PROVIDER_CONCURRENCY",
    "REGEN_SWEEP_PACING_S": "REPOACH_REGEN_SWEEP_PACING_S",
    "REGEN_SWEEP_RETRY_BACKOFF_S": "REPOACH_REGEN_SWEEP_RETRY_BACKOFF_S",
    "DISPATCH_TOTAL_BUDGET_S": "REPOACH_DISPATCH_TOTAL_BUDGET_S",
}


def _aliases(legacy: str) -> AliasChoices:
    """Return ``AliasChoices(REPOACH_*, legacy)`` for ``legacy``.

    The REPOACH_ alias is listed first so Pydantic v2 prefers it when
    both names happen to be set in the environment; the legacy bare
    alias comes last (kept for pre-prefix deployments and CI secrets
    that use the bare names). :data:`_LEGACY_TO_REPOACH_ALIAS` above is
    the single source of truth: update the dict + the corresponding
    Field declaration in lockstep so a single grep over either name
    surfaces the pairing. Tests in
    ``tests/unit/test_settings_sharp_prefix_aliases.py`` pin the
    mapping so a typo cannot land silently.

    Args:
        legacy: The legacy bare env key (e.g. ``"OPENROUTER_API_KEY"``).
            Must be a key of :data:`_LEGACY_TO_REPOACH_ALIAS`.

    Returns:
        A :class:`pydantic.AliasChoices` ordered ``(REPOACH_*, legacy)``
        so the REPOACH_ name wins precedence in Pydantic v2 settings
        resolution.

    Raises:
        KeyError: When ``legacy`` is not declared in
            :data:`_LEGACY_TO_REPOACH_ALIAS` — keeps the call sites
            honest so a typo at the Field declaration cannot land.
    """
    return AliasChoices(_LEGACY_TO_REPOACH_ALIAS[legacy], legacy)


def _removed_env_var_message(model_config: Mapping[str, Any]) -> str | None:
    """Return a migration error for removed env vars, if present."""
    removed_key = "NIM_ENABLE_THINKING"
    replacement = "ENABLE_THINKING"

    if removed_key in os.environ:
        return f"{removed_key} has been removed in this release. Rename it to {replacement}."

    for env_file in _configured_env_files(model_config):
        if _env_file_contains_key(env_file, removed_key):
            return (
                f"{removed_key} has been removed in this release. "
                f"Rename it to {replacement}. Found in {env_file}."
            )

    return None


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Field naming follows the upstream-provider grouping
    (NVIDIA NIM / OpenRouter / Claude Code) so config keys stay
    greppable.

    ``model`` is the fallback Claude model in
    ``provider_type/model/name`` form; every Claude request maps to
    it unless one of the per-tier overrides
    (``model_opus`` / ``model_sonnet`` / ``model_haiku``) matches.

    ``claude_code_*`` configures the OAuth-authenticated subprocess
    provider (no API key needed).  When ``MODEL_OPUS`` resolves to
    ``claude_code/...`` the proxy spawns the local ``claude`` CLI and
    pipes the request through it.

    ``anthropic_auth_token`` is the Anthropic-style server API key
    used to protect the proxy endpoints.  Set it via the
    ``REPOACH_ANTHROPIC_AUTH_TOKEN`` environment variable; when empty,
    no auth is required — which is why an empty token is only
    accepted together with a loopback ``host``
    (see :meth:`require_token_for_public_bind`).
    """

    open_router_api_key: str = Field(default="", validation_alias=_aliases("OPENROUTER_API_KEY"))

    nvidia_nim_api_key: str = Field(default="", validation_alias=_aliases("NVIDIA_NIM_API_KEY"))

    kimi_api_key: str = Field(default="", validation_alias=_aliases("KIMI_API_KEY"))
    groq_api_key: str = Field(default="", validation_alias=_aliases("GROQ_API_KEY"))
    cerebras_api_key: str = Field(default="", validation_alias=_aliases("CEREBRAS_API_KEY"))
    deepseek_api_key: str = Field(default="", validation_alias=_aliases("DEEPSEEK_API_KEY"))

    artificial_analysis_api_key: str = Field(
        default="", validation_alias=_aliases("ARTIFICIAL_ANALYSIS_API_KEY")
    )

    claude_code_cli_path: str = Field(
        default="claude",
        validation_alias=_aliases("CLAUDE_CODE_CLI_PATH"),
    )
    claude_code_default_model: str = Field(
        default="sonnet",
        validation_alias=_aliases("CLAUDE_CODE_DEFAULT_MODEL"),
    )
    claude_code_subprocess_timeout: float = Field(
        default=600.0,
        validation_alias=_aliases("CLAUDE_CODE_SUBPROCESS_TIMEOUT"),
    )

    model: str = Field(default="nvidia_nim/z-ai/glm4.7", validation_alias=_aliases("MODEL"))

    model_opus: str | None = Field(default=None, validation_alias=AliasChoices("MODEL_OPUS"))
    model_sonnet: str | None = Field(default=None, validation_alias=AliasChoices("MODEL_SONNET"))
    model_haiku: str | None = Field(default=None, validation_alias=AliasChoices("MODEL_HAIKU"))

    nvidia_nim_proxy: str = Field(default="", validation_alias=_aliases("NVIDIA_NIM_PROXY"))
    open_router_proxy: str = Field(default="", validation_alias=_aliases("OPENROUTER_PROXY"))

    provider_rate_limit: int = Field(default=40, validation_alias=_aliases("PROVIDER_RATE_LIMIT"))
    provider_rate_window: int = Field(default=60, validation_alias=_aliases("PROVIDER_RATE_WINDOW"))
    provider_max_concurrency: int = Field(
        default=5, validation_alias=_aliases("PROVIDER_MAX_CONCURRENCY")
    )
    enable_thinking: bool = Field(default=True, validation_alias=_aliases("ENABLE_THINKING"))

    budget_retry_enabled: bool = Field(
        default=True, validation_alias=_aliases("BUDGET_RETRY_ENABLED")
    )
    budget_retry_factor: int = Field(default=8, validation_alias=_aliases("BUDGET_RETRY_FACTOR"))
    budget_retry_floor: int = Field(default=512, validation_alias=_aliases("BUDGET_RETRY_FLOOR"))
    budget_retry_cap: int = Field(default=8192, validation_alias=_aliases("BUDGET_RETRY_CAP"))

    breaker_enabled: bool = Field(default=True, validation_alias=_aliases("BREAKER_ENABLED"))
    breaker_ttl_s: float = Field(default=120.0, validation_alias=_aliases("BREAKER_TTL_S"))
    breaker_ttl_terminal_s: float = Field(
        default=604_800.0, validation_alias=_aliases("BREAKER_TTL_TERMINAL_S")
    )
    breaker_ttl_quarantine_s: float = Field(
        default=21_600.0, validation_alias=_aliases("BREAKER_TTL_QUARANTINE_S")
    )
    breaker_quarantine_threshold: int = Field(
        default=3, ge=1, validation_alias=_aliases("BREAKER_QUARANTINE_THRESHOLD")
    )
    breaker_slow_latency_gate_s: float = Field(
        default=10.0, validation_alias=_aliases("BREAKER_SLOW_LATENCY_GATE_S")
    )
    breaker_slow_tps_floor: float = Field(
        default=1.0, validation_alias=_aliases("BREAKER_SLOW_TPS_FLOOR")
    )
    breaker_slow_k: int = Field(default=3, validation_alias=_aliases("BREAKER_SLOW_K"))
    breaker_slow_n: int = Field(default=5, validation_alias=_aliases("BREAKER_SLOW_N"))
    breaker_slow_ttl_s: float = Field(
        default=300.0, validation_alias=_aliases("BREAKER_SLOW_TTL_S")
    )
    breaker_slow_shadow: bool = Field(
        default=True, validation_alias=_aliases("BREAKER_SLOW_SHADOW")
    )
    credits_floor_usd: float = Field(default=2.0, validation_alias=_aliases("CREDITS_FLOOR_USD"))
    credits_health_cache_ttl_s: float = Field(
        default=3600.0, validation_alias=_aliases("CREDITS_HEALTH_CACHE_TTL_S")
    )
    chain_status_window_h: float = Field(
        default=24.0, validation_alias=_aliases("CHAIN_STATUS_WINDOW_H")
    )

    regen_max_cell_age_h: float = Field(
        default=12.0, validation_alias=_aliases("REGEN_MAX_CELL_AGE_H")
    )
    regen_sweep_per_provider_cap: int = Field(
        default=12, validation_alias=_aliases("REGEN_SWEEP_PER_PROVIDER_CAP")
    )
    regen_sweep_per_provider_concurrency: int = Field(
        default=2, validation_alias=_aliases("REGEN_SWEEP_PER_PROVIDER_CONCURRENCY")
    )
    regen_sweep_pacing_s: float = Field(
        default=0.5, validation_alias=_aliases("REGEN_SWEEP_PACING_S")
    )
    regen_sweep_retry_backoff_s: float = Field(
        default=2.0, validation_alias=_aliases("REGEN_SWEEP_RETRY_BACKOFF_S")
    )

    breaker_probe_seed_enabled: bool = Field(
        default=True, validation_alias=_aliases("BREAKER_PROBE_SEED_ENABLED")
    )
    breaker_probe_seed_db: str = Field(
        default="data/repoach.db", validation_alias=_aliases("BREAKER_PROBE_SEED_DB")
    )
    breaker_state_persist_enabled: bool = Field(
        default=True, validation_alias=_aliases("BREAKER_STATE_PERSIST_ENABLED")
    )
    effort_map_seed_enabled: bool = Field(
        default=True, validation_alias=_aliases("EFFORT_MAP_SEED_ENABLED")
    )
    chainpilot_apply_enabled: bool = Field(
        default=False, validation_alias=_aliases("CHAINPILOT_APPLY_ENABLED")
    )
    chainpilot_max_mutations: int = Field(
        default=2, ge=0, validation_alias=_aliases("CHAINPILOT_MAX_MUTATIONS")
    )

    http_read_timeout: float = Field(default=120.0, validation_alias=_aliases("HTTP_READ_TIMEOUT"))
    http_write_timeout: float = Field(default=10.0, validation_alias=_aliases("HTTP_WRITE_TIMEOUT"))
    http_connect_timeout: float = Field(
        default=2.0, validation_alias=_aliases("HTTP_CONNECT_TIMEOUT")
    )
    first_byte_deadline_s: float = Field(
        default=20.0, ge=0, validation_alias=_aliases("FIRST_BYTE_DEADLINE_S")
    )
    dispatch_total_budget_s: float = Field(
        default=900.0, validation_alias=_aliases("DISPATCH_TOTAL_BUDGET_S")
    )
    """Wall-clock budget for one ``_stream_with_failover`` call
    (SP-CHAIN-REQUEST-BUDGET), shared across the WHOLE chain walk rather
    than replacing any hop's own timeout.  ``900.0`` = 120 + 120 (both
    native hops genuinely hang to their read timeout,
    ``http_read_timeout``) + 600 (``claude_code_subprocess_timeout``
    floor) + 60s headroom for scheduling/SSE-peek overhead.  Do not
    configure below ~660s (600s ``claude_code`` floor + one native
    hop's worth of margin) or a legitimately slow-but-successful
    ``claude_code`` cold start risks truncation on a chain where an
    earlier hop already spent budget."""

    nim: NimSettings = Field(default_factory=NimSettings)

    host: str = Field(default="127.0.0.1", validation_alias=_aliases("HOST"))
    port: int = Field(default=8082, validation_alias=_aliases("PORT"))
    log_file: str = Field(default="server.log", validation_alias=_aliases("LOG_FILE"))
    anthropic_auth_token: str = Field(default="", validation_alias=_aliases("ANTHROPIC_AUTH_TOKEN"))

    @model_validator(mode="before")
    @classmethod
    def reject_removed_env_vars(cls, data: Any) -> Any:
        """Fail fast when removed environment variables are still configured."""
        if message := _removed_env_var_message(cls.model_config):
            raise ValueError(message)
        return data

    @field_validator(
        "model_opus",
        "model_sonnet",
        "model_haiku",
        mode="before",
    )
    @classmethod
    def parse_optional_str(cls, v: Any) -> Any:
        if v == "":
            return None
        return v

    @field_validator("model", "model_opus", "model_sonnet", "model_haiku")
    @classmethod
    def validate_model_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if "/" not in v:
            raise ValueError(
                f"Model must be prefixed with provider type. "
                f"Valid providers: {', '.join(SUPPORTED_PROVIDER_IDS)}. "
                f"Format: provider_type/model/name"
            )
        provider = v.split("/", 1)[0]
        if provider not in SUPPORTED_PROVIDER_IDS:
            supported = ", ".join(f"'{p}'" for p in SUPPORTED_PROVIDER_IDS)
            raise ValueError(f"Invalid provider: '{provider}'. Supported: {supported}")
        return v

    @model_validator(mode="after")
    def prefer_dotenv_anthropic_auth_token(self) -> Settings:
        """Let explicit .env auth config override stale shell/client tokens.

        Probes :data:`_AUTH_TOKEN_ALIASES` in order (REPOACH_, then the
        legacy bare ``ANTHROPIC_AUTH_TOKEN``) so pre-prefix deployments
        keep working.

        Returns:
            The same :class:`Settings` instance, with
            ``self.anthropic_auth_token`` overwritten when any of the
            aliases is defined in a configured dotenv file.
        """
        dotenv_value = None
        for alias in _AUTH_TOKEN_ALIASES:
            dotenv_value = _env_file_override(self.model_config, alias)
            if dotenv_value is not None:
                break
        if dotenv_value is not None:
            self.anthropic_auth_token = dotenv_value
        return self

    @model_validator(mode="after")
    def require_token_for_public_bind(self) -> Settings:
        """Refuse a non-loopback bind without an auth token.

        ``require_api_key`` is a documented no-op when the token is
        empty, so an empty token on a public interface would serve an
        unauthenticated proxy (quota theft + free rides on the
        claude_code backend).  Declared after
        :meth:`prefer_dotenv_anthropic_auth_token` so the dotenv
        override is applied before the check.

        Returns:
            The same :class:`Settings` instance.

        Raises:
            ValueError: When ``host`` is not in
                :data:`_LOOPBACK_HOSTS` and ``anthropic_auth_token``
                is empty.
        """
        if self.host not in _LOOPBACK_HOSTS and not self.anthropic_auth_token:
            raise ValueError(
                f"host={self.host!r} is not loopback: a non-loopback bind "
                "requires REPOACH_ANTHROPIC_AUTH_TOKEN, otherwise the proxy "
                "would serve unauthenticated on a public interface."
            )
        return self

    def uses_process_anthropic_auth_token(self) -> bool:
        """Return whether proxy auth came from process env, not dotenv config.

        Checks the REPOACH_ and legacy aliases in lockstep with
        :meth:`prefer_dotenv_anthropic_auth_token`.

        Returns:
            ``True`` when no alias is defined in any configured
            dotenv file and at least one is present in
            :data:`os.environ` — i.e. auth was injected by a shell
            session rather than a deployment-time config file.
            ``False`` otherwise.
        """
        for key in _AUTH_TOKEN_ALIASES:
            if _env_file_override(self.model_config, key) is not None:
                return False
        return any(os.environ.get(key) for key in _AUTH_TOKEN_ALIASES)

    @property
    def provider_type(self) -> str:
        """Extract provider type from the default model string."""
        return self.model.split("/", 1)[0]

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
