"""Tests for SP-MODELS-ENDPOINT-TRUTHFUL.

``GET /v1/models`` used to serve a hand-written static list of Claude
ids — including ``claude-haiku-4-20250514``, a model that never
existed — unrelated to the chains the proxy is actually configured to
route (audit 2026-07-13 finding M24). These tests drive the real
``list_models`` handler (unit, AC1) and the real endpoint through
``TestClient`` with the settings dependency overridden (integration,
AC2) against a known ``MODEL`` / ``MODEL_OPUS`` / ``MODEL_SONNET`` /
``MODEL_HAIKU`` configuration, asserting the advertised set is exactly
the configured chain refs and that the fictional id never appears.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.api.routes import list_models
from repoach.llm_proxy.config.settings import Settings

_FICTIONAL_HAIKU_4_ID = "claude-haiku-4-20250514"

_CHAIN_ENV_KEYS = ("MODEL", "MODEL_OPUS", "MODEL_SONNET", "MODEL_HAIKU")

_EXPECTED_CONFIGURED_MODEL_IDS = [
    "nvidia_nim/default-model",
    "nvidia_nim/opus-primary",
    "claude_code/opus",
    "open_router/anthropic/claude-3-5-sonnet",
    "claude_code/haiku",
]


def _configure_known_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a known, non-trivial chain set for the four tiers.

    ``MODEL_HAIKU`` deliberately repeats ``claude_code/opus`` (already
    present in ``MODEL_OPUS``'s chain) so the assertions also cover the
    de-duplication edge case (spec ``Edge cases``): the duplicate must
    collapse to its first occurrence rather than appearing twice.
    """
    for key in _CHAIN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"REPOACH_{key}", raising=False)
    monkeypatch.setenv("MODEL", "nvidia_nim/default-model")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus-primary,claude_code/opus")
    monkeypatch.setenv("MODEL_SONNET", "open_router/anthropic/claude-3-5-sonnet")
    monkeypatch.setenv("MODEL_HAIKU", "claude_code/opus,claude_code/haiku")


def test_models_reflect_configured_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_known_chains(monkeypatch)
    settings = Settings(_env_file=None)

    response = asyncio.run(list_models(settings=settings))

    assert [entry.id for entry in response.data] == _EXPECTED_CONFIGURED_MODEL_IDS
    assert response.first_id == _EXPECTED_CONFIGURED_MODEL_IDS[0]
    assert response.last_id == _EXPECTED_CONFIGURED_MODEL_IDS[-1]
    assert response.has_more is False


def test_fictional_haiku_4_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_known_chains(monkeypatch)
    settings = Settings(_env_file=None)

    response = asyncio.run(list_models(settings=settings))

    assert _FICTIONAL_HAIKU_4_ID not in [entry.id for entry in response.data]


def test_models_endpoint_reflects_configured_chains_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_settings_module, "_configured_env_files", lambda _cfg: ())
    _configure_known_chains(monkeypatch)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    client = TestClient(app)

    resp = client.get("/v1/models")

    assert resp.status_code == 200
    body = resp.json()
    ids = [entry["id"] for entry in body["data"]]
    assert ids == _EXPECTED_CONFIGURED_MODEL_IDS
    assert _FICTIONAL_HAIKU_4_ID not in ids
    assert body["first_id"] == ids[0]
    assert body["last_id"] == ids[-1]
    assert body["has_more"] is False


def test_models_endpoint_empty_when_routing_table_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure scenario: an unbuildable routing table fails closed to an
    empty, truthful list rather than ever falling back to fictional ids.

    ``Settings.validate_model_format`` only checks the FIRST comma
    entry of a ``MODEL_*`` slot against the provider catalog, so a
    malformed second entry (an unregistered provider id) still passes
    ``Settings`` construction but trips ``ModelRef.parse`` — and so
    ``RoutingTable.from_settings`` — the first time the table is built.
    """
    for key in _CHAIN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"REPOACH_{key}", raising=False)
    monkeypatch.setenv("MODEL", "nvidia_nim/default-model")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus-primary,not_a_registered_provider/x")
    settings = Settings(_env_file=None)

    response = asyncio.run(list_models(settings=settings))

    assert response.data == []
    assert response.first_id is None
    assert response.last_id is None
    assert response.has_more is False
