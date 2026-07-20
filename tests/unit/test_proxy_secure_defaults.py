"""Unit tests for SP-PROXY-SECURE-DEFAULTS.

Pins the secure-by-default posture of the proxy: loopback bind unless
a token is configured, constant-time API-key comparison preserving the
bearer / x-api-key / suffix-stripping behaviours, the ``env=prod``
boot guard on core settings, and the eradication of the ``freecc``
implicit shared secret. Proxy ``Settings`` are constructed hermetically
(env aliases cleared + dotenv lookups neutralised) per the established
pattern in ``test_settings_sharp_prefix_aliases.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import repoach.core.config as core_config
import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy.api.dependencies import require_api_key
from repoach.llm_proxy.config.settings import Settings as ProxySettings

_PROXY_ENV_KEYS = (
    "FEROVA_PROXY_HOST",
    "HOST",
    "FEROVA_ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
)


def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(proxy_settings_module, "_configured_env_files", lambda _cfg: ())


def _clean_core_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "REPOACH_ANTHROPIC_AUTH_TOKEN",
        "FEROVA_ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "REPOACH_ENV",
        "FEROVA_ENV",
        "ENV",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_proxy_env(monkeypatch)
    settings = ProxySettings(_env_file=None)
    assert settings.host == "127.0.0.1"


def test_public_bind_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_proxy_env(monkeypatch)
    monkeypatch.setenv("FEROVA_PROXY_HOST", "0.0.0.0")
    with pytest.raises(ValidationError, match="REPOACH_ANTHROPIC_AUTH_TOKEN"):
        ProxySettings(_env_file=None)


def test_public_bind_with_token_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_proxy_env(monkeypatch)
    monkeypatch.setenv("FEROVA_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("FEROVA_ANTHROPIC_AUTH_TOKEN", "tok")
    settings = ProxySettings(_env_file=None)
    assert settings.host == "0.0.0.0"
    assert settings.anthropic_auth_token == "tok"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_allows_empty_token(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_proxy_env(monkeypatch)
    monkeypatch.setenv("FEROVA_PROXY_HOST", host)
    settings = ProxySettings(_env_file=None)
    assert settings.host == host
    assert settings.anthropic_auth_token == ""


def _request_with(headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


def _auth_settings(token: str) -> SimpleNamespace:
    return SimpleNamespace(anthropic_auth_token=token)


@pytest.mark.parametrize(
    "headers",
    [
        {"x-api-key": "tok"},
        {"authorization": "Bearer tok"},
        {"authorization": "Bearer tok:claude-sonnet-4-6"},
        {"x-api-key": "tok:suffix"},
        {"anthropic-auth-token": "tok"},
    ],
)
def test_correct_token_forms_pass(headers: dict[str, str]) -> None:
    require_api_key(_request_with(headers), _auth_settings("tok"))


@pytest.mark.parametrize(
    "headers",
    [
        {"x-api-key": "wrong"},
        {"authorization": "Bearer wrong"},
        {},
    ],
)
def test_wrong_or_missing_token_rejected(headers: dict[str, str]) -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_api_key(_request_with(headers), _auth_settings("tok"))
    assert excinfo.value.status_code == 401


def test_empty_configured_token_is_noop() -> None:
    require_api_key(_request_with({}), _auth_settings(""))


def test_no_freecc_literal_in_src() -> None:
    offenders = [
        str(path)
        for path in Path("src").rglob("*.py")
        if "freecc" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_prod_boot_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_core_env(monkeypatch)
    with pytest.raises(ValidationError, match="env=prod requires"):
        core_config.Settings(_env_file=None, env="prod")


def test_prod_with_token_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_core_env(monkeypatch)
    monkeypatch.setenv("FEROVA_ANTHROPIC_AUTH_TOKEN", "tok")
    settings = core_config.Settings(_env_file=None, env="prod")
    assert settings.llm_proxy_auth_token is not None
    assert settings.llm_proxy_auth_token.get_secret_value() == "tok"


def test_dev_without_token_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_core_env(monkeypatch)
    settings = core_config.Settings(_env_file=None, env="dev")
    assert settings.llm_proxy_auth_token is None
