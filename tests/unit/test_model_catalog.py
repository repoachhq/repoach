"""Tests for SP-CHAINPILOT-CATALOG-MODELS (Phase 1a).

Covers the per-provider ``/v1/models`` lister: the happy path, every failure
mode it must absorb without raising (non-2xx, transport error, malformed
body), and the sweepability predicate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from ferova.llm_proxy.providers import model_catalog
from ferova.llm_proxy.providers.model_catalog import (
    UNSWEPT_PROVIDERS,
    ListedModel,
    ProviderModelListing,
    is_sweepable,
    list_provider_models,
)

_BASE_URL = "https://upstream.example/v1"


def _client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose requests are served by *handler*."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _list(handler) -> ProviderModelListing:
    async with _client(handler) as client:
        return await list_provider_models(
            client,
            provider_id="nvidia_nim",
            base_url=_BASE_URL,
            api_key="secret-key",
        )


async def test_lists_all_models_on_valid_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{_BASE_URL}/models")
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    listing = await _list(handler)

    assert listing.ok is True
    assert listing.models == (ListedModel("a"), ListedModel("b"))
    assert listing.provider_id == "nvidia_nim"


async def test_non_2xx_is_captured_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="gone")

    listing = await _list(handler)

    assert listing.ok is False
    assert listing.models == ()
    assert "410" in listing.detail


async def test_transport_error_is_caught_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(model_catalog, "_log", log)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    listing = await _list(handler)

    assert listing.ok is False
    assert listing.models == ()
    assert "ConnectError" in listing.detail
    log.warning.assert_called_once()


async def test_malformed_body_skips_bad_entries_and_flags_missing_data() -> None:
    def missing_data(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    no_data = await _list(missing_data)
    assert no_data.ok is False
    assert no_data.models == ()
    assert "data" in no_data.detail

    def mixed_entries(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "good"}, {"no_id": True}, {"id": 7}, {"id": ""}]},
        )

    mixed = await _list(mixed_entries)
    assert mixed.ok is True
    assert mixed.models == (ListedModel("good"),)


async def test_empty_data_is_a_clean_zero_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    listing = await _list(handler)

    assert listing.ok is True
    assert listing.models == ()
    assert "0" in listing.detail


def test_sweepability_excludes_claude_code() -> None:
    assert is_sweepable("claude_code") is False
    assert is_sweepable("nvidia_nim") is True
    assert "claude_code" in UNSWEPT_PROVIDERS
    assert len(UNSWEPT_PROVIDERS) == 1
