"""Integration test for SP-PROXY-EDGE-HARDEN F-HEALTH (AC2a).

Drives ``GET /health`` end-to-end through a real FastAPI app via
``TestClient``: with a token configured, an anonymous request gets
liveness only (no ``breaker``/``credits``); the same request presenting
a valid key gets the detailed body. Settings are injected hermetically
via ``app.dependency_overrides[get_settings]``, with the dotenv lookup
neutralised (per the established pattern in
``test_proxy_secure_defaults.py``) so the real ``.env``'s configured
token never leaks into the test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.config.settings import Settings


def _client_with_token(token: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(proxy_settings_module, "_configured_env_files", lambda _cfg: ())
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", token)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    return TestClient(app)


def test_health_internals_require_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with_token("REALTOKEN", monkeypatch)

    anon_resp = client.get("/health")
    assert anon_resp.status_code == 200
    anon_body = anon_resp.json()
    assert anon_body == {"status": "healthy"}
    assert "breaker" not in anon_body
    assert "credits" not in anon_body

    auth_resp = client.get("/health", headers={"x-api-key": "REALTOKEN"})
    assert auth_resp.status_code == 200
    auth_body = auth_resp.json()
    assert auth_body["status"] == "healthy"
    assert isinstance(auth_body["breaker"], list)
    assert "credits" in auth_body

    wrong_resp = client.get("/health", headers={"x-api-key": "WRONGTOKEN"})
    assert wrong_resp.status_code == 200
    wrong_body = wrong_resp.json()
    assert wrong_body == {"status": "healthy"}


def test_health_no_configured_token_always_full_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no token is configured, ``require_api_key`` is a documented
    no-op — every ``/health`` caller, authenticated or not, gets the
    full body (unchanged behavior for a loopback dev proxy)."""
    client = _client_with_token("", monkeypatch)

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["breaker"], list)
    assert "credits" in body
