"""Tests for SP-PROVIDER-TRANSPORT-SPI.

Covers the shared user-facing error helper consolidated out of the two
transports, and the transport-driven factory that builds a provider with
no bespoke factory through its declared ``transport_type``.
"""

from __future__ import annotations

import httpx
import pytest

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.error_mapping import provider_error_message
from ferova.llm_proxy.providers.openai_generic import GenericOpenAIProvider
from ferova.llm_proxy.providers.registry import create_provider


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("upstream error", request=request, response=response)


def test_error_message_405_is_the_rejection_sentence() -> None:
    message = provider_error_message(
        _http_status_error(405),
        provider_name="TESTPROV",
        read_timeout_s=300.0,
    )
    assert message == (
        "Upstream provider TESTPROV rejected the request method or endpoint (HTTP 405)."
    )


def test_error_message_non_405_falls_back_to_user_facing_message() -> None:
    message = provider_error_message(
        _http_status_error(503),
        provider_name="TESTPROV",
        read_timeout_s=300.0,
    )
    assert isinstance(message, str)
    assert message
    assert "HTTP 405" not in message


def test_create_provider_builds_bespoke_nvidia_nim(monkeypatch: pytest.MonkeyPatch) -> None:
    from ferova.llm_proxy.providers.nvidia_nim import NvidiaNimProvider

    monkeypatch.setenv("FEROVA_NVIDIA_NIM_API_KEY", "test-key")
    provider = create_provider("nvidia_nim", Settings(_env_file=None))
    assert isinstance(provider, NvidiaNimProvider)


def test_create_provider_builds_generic_via_transport_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEROVA_KIMI_API_KEY", "test-key")
    provider = create_provider("kimi", Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
