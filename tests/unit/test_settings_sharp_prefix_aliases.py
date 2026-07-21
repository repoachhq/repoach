"""Regression — every Pydantic Field on llm_proxy Settings reads REPOACH_ + legacy.

Repoach hard cutover (2026-07) — the transitional old-brand middle
alias is gone.  Each proxy ``Settings`` field now resolves through a
two-name :class:`pydantic.AliasChoices`: the canonical ``REPOACH_*``
name first, then the legacy bare name (``MODEL_OPUS``,
``OPENROUTER_API_KEY``, ``HOST`` …) inherited from the vendored
``free-claude-code`` package and kept for pre-prefix deployments.

These tests pin the dual-read so:

1. Every entry in :data:`_LEGACY_TO_REPOACH_ALIAS` corresponds to a
   declared field — a typo in the map would otherwise land silently.
2. Setting the REPOACH_ name produces the same Settings value as
   setting the legacy bare name (read-through equivalence).
3. When both names are set, REPOACH_ wins (precedence pinned for the
   eventual legacy-alias removal).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import (
    _LEGACY_TO_REPOACH_ALIAS,
    Settings,
)

_LEGACY_TO_FIELD: dict[str, str] = {
    "OPENROUTER_API_KEY": "open_router_api_key",
    "NVIDIA_NIM_API_KEY": "nvidia_nim_api_key",
    "KIMI_API_KEY": "kimi_api_key",
    "GROQ_API_KEY": "groq_api_key",
    "CEREBRAS_API_KEY": "cerebras_api_key",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "ARTIFICIAL_ANALYSIS_API_KEY": "artificial_analysis_api_key",
    "BREAKER_ENABLED": "breaker_enabled",
    "BREAKER_TTL_S": "breaker_ttl_s",
    "BREAKER_TTL_TERMINAL_S": "breaker_ttl_terminal_s",
    "BREAKER_TTL_QUARANTINE_S": "breaker_ttl_quarantine_s",
    "BREAKER_QUARANTINE_THRESHOLD": "breaker_quarantine_threshold",
    "BREAKER_PROBE_SEED_ENABLED": "breaker_probe_seed_enabled",
    "BREAKER_PROBE_SEED_DB": "breaker_probe_seed_db",
    "BREAKER_SLOW_LATENCY_GATE_S": "breaker_slow_latency_gate_s",
    "BREAKER_SLOW_TPS_FLOOR": "breaker_slow_tps_floor",
    "BREAKER_SLOW_K": "breaker_slow_k",
    "BREAKER_SLOW_N": "breaker_slow_n",
    "BREAKER_SLOW_TTL_S": "breaker_slow_ttl_s",
    "BREAKER_SLOW_SHADOW": "breaker_slow_shadow",
    "EFFORT_MAP_SEED_ENABLED": "effort_map_seed_enabled",
    "CLAUDE_CODE_CLI_PATH": "claude_code_cli_path",
    "CLAUDE_CODE_DEFAULT_MODEL": "claude_code_default_model",
    "CLAUDE_CODE_SUBPROCESS_TIMEOUT": "claude_code_subprocess_timeout",
    "MODEL": "model",
    "NVIDIA_NIM_PROXY": "nvidia_nim_proxy",
    "OPENROUTER_PROXY": "open_router_proxy",
    "PROVIDER_RATE_LIMIT": "provider_rate_limit",
    "PROVIDER_RATE_WINDOW": "provider_rate_window",
    "PROVIDER_MAX_CONCURRENCY": "provider_max_concurrency",
    "ENABLE_THINKING": "enable_thinking",
    "BUDGET_RETRY_ENABLED": "budget_retry_enabled",
    "BUDGET_RETRY_FACTOR": "budget_retry_factor",
    "BUDGET_RETRY_FLOOR": "budget_retry_floor",
    "BUDGET_RETRY_CAP": "budget_retry_cap",
    "HTTP_READ_TIMEOUT": "http_read_timeout",
    "HTTP_WRITE_TIMEOUT": "http_write_timeout",
    "HTTP_CONNECT_TIMEOUT": "http_connect_timeout",
    "HOST": "host",
    "PORT": "port",
    "LOG_FILE": "log_file",
    "ANTHROPIC_AUTH_TOKEN": "anthropic_auth_token",
    "CHAINPILOT_APPLY_ENABLED": "chainpilot_apply_enabled",
    "CHAINPILOT_MAX_MUTATIONS": "chainpilot_max_mutations",
    "CREDITS_FLOOR_USD": "credits_floor_usd",
    "CREDITS_HEALTH_CACHE_TTL_S": "credits_health_cache_ttl_s",
    "CHAIN_STATUS_WINDOW_H": "chain_status_window_h",
}
"""Legacy env key → Pydantic field name, kept in lockstep with
:data:`_LEGACY_TO_REPOACH_ALIAS` to give the read-through tests a
single source of truth for ``getattr(Settings, field_name)``."""


def _clean_env_for_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every alias name so each test starts clean.

    Tests below set one specific env var ; without this scrub the
    inherited shell could shadow the assertion.  Also disable env-file
    loading so ``chains.env`` / ``.env`` don't leak values in.
    """
    for legacy, repoach in _LEGACY_TO_REPOACH_ALIAS.items():
        monkeypatch.delenv(legacy, raising=False)
        monkeypatch.delenv(repoach, raising=False)
    monkeypatch.setattr(settings_module, "_env_files", lambda: ())
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())


def _build_settings(_pytest_factory_unused: pytest.MonkeyPatch) -> Settings:
    """Return a fresh Settings, bypassing the lru_cache + env file load."""
    return Settings(_env_file=None)


@pytest.mark.parametrize("legacy,repoach", sorted(_LEGACY_TO_REPOACH_ALIAS.items()))
def test_alias_map_round_trip(legacy: str, repoach: str) -> None:
    """Every legacy key maps to ``REPOACH_<legacy>`` or an explicit override."""
    assert repoach.startswith("REPOACH_"), (
        f"REPOACH_ alias for {legacy} must start with REPOACH_, got {repoach}"
    )
    assert legacy != repoach, f"legacy {legacy!r} cannot equal repoach {repoach!r}"


@pytest.mark.parametrize("legacy,field", sorted(_LEGACY_TO_FIELD.items()))
def test_legacy_field_present_on_settings(legacy: str, field: str) -> None:
    """The map's field name is a declared Field on :class:`Settings`."""
    assert field in Settings.model_fields, (
        f"legacy {legacy} maps to field {field!r} which is not declared on Settings ; "
        f"either fix the map in test_settings_sharp_prefix_aliases or rename the field"
    )


def test_alias_map_covers_every_field_with_proxy_alias() -> None:
    """Every (legacy, repoach) entry in the source map is exercised in the test map."""
    missing = set(_LEGACY_TO_REPOACH_ALIAS) - set(_LEGACY_TO_FIELD)
    assert not missing, (
        f"_LEGACY_TO_REPOACH_ALIAS has entries not covered by _LEGACY_TO_FIELD : {missing}"
    )


@pytest.mark.parametrize(
    "legacy,field,value",
    [
        ("OPENROUTER_API_KEY", "open_router_api_key", "or-token-legacy"),
        ("NVIDIA_NIM_API_KEY", "nvidia_nim_api_key", "nim-token-legacy"),
        ("MODEL", "model", "nvidia_nim/qwen/qwen3.5-122b-a10b-20260224"),
        ("HOST", "host", "127.0.0.1"),
        ("PORT", "port", "9999"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic_auth_token", "anth-legacy"),
    ],
)
def test_legacy_alias_still_read(
    legacy: str, field: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the legacy bare key produces the same Settings value as before."""
    _clean_env_for_settings(monkeypatch)
    monkeypatch.setenv(legacy, value)
    settings = _build_settings(monkeypatch)
    actual = getattr(settings, field)
    expected: object = int(value) if field == "port" else value
    assert actual == expected, (
        f"legacy {legacy}={value!r} should land on settings.{field}={expected!r}, got {actual!r}"
    )


@pytest.mark.parametrize(
    "legacy,field,value",
    [
        ("OPENROUTER_API_KEY", "open_router_api_key", "or-token-repoach"),
        ("NVIDIA_NIM_API_KEY", "nvidia_nim_api_key", "nim-token-repoach"),
        ("MODEL", "model", "nvidia_nim/qwen/qwen3.5-122b-a10b-20260224"),
        ("HOST", "host", "localhost"),
        ("PORT", "port", "8083"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic_auth_token", "anth-repoach"),
    ],
)
def test_repoach_alias_read_through(
    legacy: str, field: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting the REPOACH_ name lands on the same Settings field."""
    _clean_env_for_settings(monkeypatch)
    repoach_key = _LEGACY_TO_REPOACH_ALIAS[legacy]
    monkeypatch.setenv(repoach_key, value)
    settings = _build_settings(monkeypatch)
    actual = getattr(settings, field)
    expected: object = int(value) if field == "port" else value
    assert actual == expected, (
        f"REPOACH {repoach_key}={value!r} should land on settings.{field}={expected!r}, "
        f"got {actual!r}"
    )


@pytest.mark.parametrize(
    "legacy,field",
    [
        ("OPENROUTER_API_KEY", "open_router_api_key"),
        ("HOST", "host"),
        ("ANTHROPIC_AUTH_TOKEN", "anthropic_auth_token"),
    ],
)
def test_repoach_wins_over_legacy(legacy: str, field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both names are present, the REPOACH_ value takes precedence."""
    _clean_env_for_settings(monkeypatch)
    repoach_key = _LEGACY_TO_REPOACH_ALIAS[legacy]
    if field == "host":
        legacy_value = "localhost"
        repoach_value = "::1"
    else:
        legacy_value = "legacy-value"
        repoach_value = "repoach-value"
    monkeypatch.setenv(legacy, legacy_value)
    monkeypatch.setenv(repoach_key, repoach_value)
    settings = _build_settings(monkeypatch)
    assert getattr(settings, field) == repoach_value, (
        f"with both {legacy} and {repoach_key} set, REPOACH_ must win for field {field!r}"
    )


def test_uses_process_anthropic_auth_token_reads_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare ``ANTHROPIC_AUTH_TOKEN`` from process env still flags as in-use."""
    _clean_env_for_settings(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anth-process-legacy")
    settings = _build_settings(monkeypatch)
    assert settings.uses_process_anthropic_auth_token() is True


def test_uses_process_anthropic_auth_token_reads_repoach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical ``REPOACH_ANTHROPIC_AUTH_TOKEN`` from process env flags as in-use."""
    _clean_env_for_settings(monkeypatch)
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "anth-process-repoach")
    settings = _build_settings(monkeypatch)
    assert settings.uses_process_anthropic_auth_token() is True


def test_uses_process_anthropic_auth_token_false_when_neither_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither name set → method returns False (no process-env auth)."""
    _clean_env_for_settings(monkeypatch)
    settings = _build_settings(monkeypatch)
    assert settings.uses_process_anthropic_auth_token() is False


def test_chain_status_window_h_alias_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default is 24.0 and REPOACH_CHAIN_STATUS_WINDOW_H overrides it."""
    _clean_env_for_settings(monkeypatch)
    settings = _build_settings(monkeypatch)
    assert settings.chain_status_window_h == 24.0, (
        f"default should be 24.0, got {settings.chain_status_window_h}"
    )

    monkeypatch.setenv("REPOACH_CHAIN_STATUS_WINDOW_H", "6")
    settings = _build_settings(monkeypatch)
    assert settings.chain_status_window_h == 6.0, (
        f"set to 6 should yield 6.0, got {settings.chain_status_window_h}"
    )


@pytest.mark.parametrize("dotenv_key", ["ANTHROPIC_AUTH_TOKEN", "REPOACH_ANTHROPIC_AUTH_TOKEN"])
def test_uses_process_anthropic_auth_token_false_when_dotenv_provides_either(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dotenv_key: str
) -> None:
    """Either alias coming from .env should NOT count as a process-env source.

    The method's purpose is to distinguish "auth came from a deployment-time
    file" (safe, audited) from "auth was injected by a shell session"
    (suspect, may be stale).  Either alias resolved via the dotenv path
    must therefore return False, regardless of whether the legacy bare
    or canonical REPOACH_ name is the one defined in the file.
    """
    _clean_env_for_settings(monkeypatch)
    env_file = tmp_path / "anth.env"
    env_file.write_text(f"{dotenv_key}=from-dotenv\n")
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: (env_file,))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-process-shadow")
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "repoach-process-shadow")
    settings = _build_settings(monkeypatch)
    assert settings.uses_process_anthropic_auth_token() is False, (
        f"dotenv presence on {dotenv_key} must shadow process-env values, "
        f"even when both names are set in the environment"
    )
    assert settings.anthropic_auth_token == "from-dotenv", (
        f"prefer_dotenv_anthropic_auth_token must update the field with the dotenv "
        f"value, not just flag the source ; got {settings.anthropic_auth_token!r}"
    )
