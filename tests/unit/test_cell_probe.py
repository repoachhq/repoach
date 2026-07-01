"""Tests for SP-CHAINPILOT-PROBE-CELL (Phase 2a-1).

Covers the provider-agnostic per-cell probe: classification over visible
content, the reasoning observation (handled / starved / not-observed), the
two reasoning-key shapes, request-body merging of ``extra_body``, and the
never-raises transport-failure path with api-key redaction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from ferova.llm_proxy.providers import cell_probe
from ferova.llm_proxy.providers.cell_probe import (
    CellHealth,
    classify_cell,
    probe_cell,
)

_BASE_URL = "https://upstream.example/v1"


def _client(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose requests are served by *handler*."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _probe(handler, **kwargs) -> CellHealth:
    async with _client(handler) as client:
        return await probe_cell(
            client,
            provider_id="nvidia_nim",
            base_url=_BASE_URL,
            api_key="secret-key",
            model="some/model",
            **kwargs,
        )


def _completion(*, content: str, reasoning_key: str | None = None, reasoning: str = "") -> dict:
    message: dict = {"content": content}
    if reasoning_key is not None:
        message[reasoning_key] = reasoning
    return {"choices": [{"message": message}]}


def test_classify_cell_covers_every_status() -> None:
    assert classify_cell(None, None, "ok", slow_threshold_s=8.0) == "error"
    assert classify_cell(500, 0.1, "ok", slow_threshold_s=8.0) == "error"
    assert classify_cell(200, 0.1, "   ", slow_threshold_s=8.0) == "empty"
    assert classify_cell(200, 9.0, "ok", slow_threshold_s=8.0) == "slow"
    assert classify_cell(200, 0.1, "ok", slow_threshold_s=8.0) == "ok"


async def test_visible_content_with_reasoning_is_handled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{_BASE_URL}/chat/completions")
        assert request.headers["Authorization"] == "Bearer secret-key"
        return httpx.Response(
            200, json=_completion(content="ok", reasoning_key="reasoning_content", reasoning="hmm")
        )

    health = await _probe(handler)

    assert health.status == "ok"
    assert health.content_chars > 0
    assert health.reasoning_chars > 0
    assert health.thinking_handled is True
    assert health.thinking_starved is False


async def test_reasoning_without_visible_content_is_starved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_completion(content="", reasoning_key="reasoning_content", reasoning="long")
        )

    health = await _probe(handler)

    assert health.status == "empty"
    assert health.reasoning_chars > 0
    assert health.thinking_starved is True
    assert health.thinking_handled is False


async def test_visible_content_without_reasoning_is_not_observed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content="ok"))

    health = await _probe(handler)

    assert health.thinking_observed is False
    assert health.reasoning_chars == 0
    assert health.status == "ok"


async def test_openrouter_reasoning_key_is_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_completion(content="ok", reasoning_key="reasoning", reasoning="trace")
        )

    health = await _probe(handler)

    assert health.reasoning_chars == len("trace")
    assert health.thinking_handled is True


async def test_transport_error_is_caught_redacted_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = MagicMock()
    monkeypatch.setattr(cell_probe, "_log", log)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused by secret-key host")

    health = await _probe(handler)

    assert health.status == "error"
    assert health.latency_s is None
    assert health.content_chars == 0
    assert "secret-key" not in health.detail
    log.warning.assert_called_once()


async def test_extra_body_is_merged_into_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion(content="ok"))

    await _probe(handler, extra_body={"reasoning_effort": "low", "max_tokens": 999})

    assert captured["reasoning_effort"] == "low"
    assert captured["max_tokens"] == 999


async def test_non_2xx_is_error_with_http_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="gone")

    health = await _probe(handler)

    assert health.status == "error"
    assert "410" in health.detail


async def test_unparseable_body_is_error_keeping_latency() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    health = await _probe(handler)

    assert health.status == "error"
    assert health.latency_s is not None
    assert "json_decode" in health.detail
