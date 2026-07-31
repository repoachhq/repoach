"""Unit tests for SP-PROXY-EDGE-HARDEN F-AUTH.

Pins the tightened ``require_api_key`` credential match: the
``<token>:<suffix>`` acceptance form now requires the presented
candidate's leading ``len(token)`` characters to be an EXACT,
constant-time match for the configured token, followed by ``:`` and a
non-empty suffix — replacing the prior first-colon truncation
(``token.split(":", 1)[0]``), which cut at whatever colon the presented
value carried rather than at the configured token's own length. A
configured token that itself contains ``:`` is rejected at settings
construction (it would be un-presentable in full under the tightened
rule). ``Settings`` are built hermetically (``_env_file=None`` plus a
neutralised dotenv lookup) per the established pattern in
``test_proxy_secure_defaults.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy.api.dependencies import require_api_key
from repoach.llm_proxy.config.settings import Settings as ProxySettings

_PROXY_ENV_KEYS = (
    "REPOACH_PROXY_HOST",
    "HOST",
    "REPOACH_ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
)


def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(proxy_settings_module, "_configured_env_files", lambda _cfg: ())


def _request_with(headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


def _auth_settings(token: str) -> SimpleNamespace:
    return SimpleNamespace(anthropic_auth_token=token)


def _assert_rejected(headers: dict[str, str], token: str = "REALTOKEN") -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_api_key(_request_with(headers), _auth_settings(token))
    assert excinfo.value.status_code == 401


def test_suffix_strip_requires_exact_prefix() -> None:
    """``REALTOKEN`` and the ``<token>:<suffix>`` form authenticate;
    a prefix that only shares a suffix relationship with the real
    token — or an empty suffix — does not.
    """
    require_api_key(_request_with({"x-api-key": "REALTOKEN"}), _auth_settings("REALTOKEN"))
    require_api_key(
        _request_with({"x-api-key": "REALTOKEN:claude-sonnet"}),
        _auth_settings("REALTOKEN"),
    )
    require_api_key(
        _request_with({"authorization": "Bearer REALTOKEN:claude-sonnet"}),
        _auth_settings("REALTOKEN"),
    )

    _assert_rejected({"x-api-key": "WRONGTOKEN:REALTOKEN"})
    _assert_rejected({"x-api-key": "REALTOKEN:"})
    _assert_rejected({"x-api-key": "REALTOKENEXTRA"})
    _assert_rejected({"x-api-key": "REALTOKE"})
    _assert_rejected({"x-api-key": "wrongtoken"})
    _assert_rejected({})


def test_colon_bearing_token_rejected_at_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured ``anthropic_auth_token`` containing ``:`` is refused
    at settings construction — it would be un-presentable in full
    under the exact-prefix suffix rule."""
    _clean_proxy_env(monkeypatch)
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "REAL:TOKEN")
    with pytest.raises(ValidationError, match="anthropic_auth_token"):
        ProxySettings(_env_file=None)


def test_colon_free_token_still_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sanity control: a token without ``:`` is unaffected by the new
    validator."""
    _clean_proxy_env(monkeypatch)
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "REALTOKEN")
    settings = ProxySettings(_env_file=None)
    assert settings.anthropic_auth_token == "REALTOKEN"
