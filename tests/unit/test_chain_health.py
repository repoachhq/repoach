"""Unit tests for SP-NIM-CHAIN-HEALTH.

The probe never touches the network: an injected fake ``httpx``-shaped
client serves scripted responses (content / empty / raised timeout), so
the classification and sweep control-flow are observable offline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ferova.cli.main import app
from ferova.review.chain_health import (
    ModelHealth,
    chain_head,
    check_tier_heads,
    classify,
    is_degraded,
    probe_nim_model,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Minimal async stand-in for ``httpx.AsyncClient`` keyed on model."""

    def __init__(
        self,
        *,
        responses: dict[str, _FakeResponse] | None = None,
        raises: dict[str, Exception] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._raises = raises or {}

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ):
        model = json["model"]
        if model in self._raises:
            raise self._raises[model]
        return self._responses[model]


def _content_payload(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}]}


def test_classify_rules() -> None:
    assert classify(200, 1.0, "ok", slow_threshold_s=8.0) == "ok"
    assert classify(200, 12.0, "ok", slow_threshold_s=8.0) == "slow"
    assert classify(200, 1.0, "", slow_threshold_s=8.0) == "empty"
    assert classify(200, 1.0, "   \n\t", slow_threshold_s=8.0) == "empty"
    assert classify(500, 1.0, "ok", slow_threshold_s=8.0) == "error"
    assert classify(None, None, "", slow_threshold_s=8.0) == "error"


def test_chain_head_splits_provider_and_nested_model() -> None:
    provider, model = chain_head("nvidia_nim/qwen/qwen3.5-122b-a10b,claude_code/sonnet")
    assert provider == "nvidia_nim"
    assert model == "qwen/qwen3.5-122b-a10b"


def test_probe_returns_error_on_transport_failure() -> None:
    client = _FakeClient(raises={"m": httpx.ReadTimeout("timed out")})

    result = asyncio.run(
        probe_nim_model(client, "https://nim/v1", "k", "m", tier="sonnet", timeout_s=1.0)
    )

    assert result.status == "error"
    assert result.latency_s is None
    assert "ReadTimeout" in result.detail


def test_probe_classifies_real_content_as_ok() -> None:
    client = _FakeClient(responses={"m": _FakeResponse(200, _content_payload("ok"))})

    result = asyncio.run(probe_nim_model(client, "https://nim/v1", "k", "m", tier="haiku"))

    assert result.status == "ok"
    assert result.content_chars == 2


def test_api_key_redacted_in_error_detail() -> None:
    key = "sk-secret-123"
    client = _FakeClient(raises={"m": httpx.ConnectError(f"failed with token {key}")})

    result = asyncio.run(probe_nim_model(client, "https://nim/v1", key, "m", tier="opus"))

    assert result.status == "error"
    assert key not in result.detail
    assert "***" in result.detail


def _settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    from ferova.llm_proxy.config.settings import Settings

    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "claude_code/opus")
    monkeypatch.setenv("MODEL_HAIKU", "claude_code/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    return Settings()


def test_non_nim_head_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    client = _FakeClient(responses={"thinker": _FakeResponse(200, _content_payload("ok"))})

    results = asyncio.run(check_tier_heads(settings, client=client))
    by_tier = {r.tier: r for r in results}

    assert by_tier["opus"].status == "skipped"
    assert by_tier["opus"].model == "claude_code/opus"
    assert by_tier["haiku"].status == "skipped"
    assert by_tier["sonnet"].status == "ok"


def test_empty_content_head_classified_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    client = _FakeClient(responses={"thinker": _FakeResponse(200, _content_payload(""))})

    results = asyncio.run(check_tier_heads(settings, client=client))
    sonnet = next(r for r in results if r.tier == "sonnet")

    assert sonnet.status == "empty"
    assert is_degraded(sonnet.status)


def test_cli_exit_code_reflects_worst_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")

    async def _fake_check(*args: Any, **kwargs: Any) -> list[ModelHealth]:
        return [
            ModelHealth("opus", "claude_code/opus", "skipped", None, 0, "non-NIM head"),
            ModelHealth("sonnet", "thinker", "empty", 0.4, 0, "http=200"),
        ]

    monkeypatch.setattr("ferova.review.chain_health.check_tier_heads", _fake_check)
    result = CliRunner().invoke(app, ["monitor-chains", "--json"])

    assert result.exit_code == 1
    assert '"status": "empty"' in result.stdout


def test_cli_exit_zero_when_all_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")

    async def _fake_check(*args: Any, **kwargs: Any) -> list[ModelHealth]:
        return [ModelHealth("sonnet", "thinker", "ok", 0.4, 2, "ok")]

    monkeypatch.setattr("ferova.review.chain_health.check_tier_heads", _fake_check)
    result = CliRunner().invoke(app, ["monitor-chains"])

    assert result.exit_code == 0


class _MockTransport(httpx.MockTransport):
    """Fake transport that answers both NIM POSTs and credits GET."""

    def __init__(
        self,
        *,
        credits_status: int = 200,
        credits_payload: dict[str, object] | None = None,
    ) -> None:
        self._credits_status = credits_status
        self._credits_payload = credits_payload
        self.get_requests: list[httpx.Request] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/api/v1/credits" in str(request.url):
            self.get_requests.append(request)
            return httpx.Response(
                self._credits_status,
                json=self._credits_payload,
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )


def _credits_payload(total: float, usage: float) -> dict[str, object]:
    return {"data": {"total_credits": total, "total_usage": usage}}


def test_credits_low_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy heads + remaining < floor -> exit 1, LOW line."""
    transport = _MockTransport(
        credits_status=200,
        credits_payload=_credits_payload(20.0, 19.0),
    )
    monkeypatch.setattr(
        "ferova.cli.main._probe_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("FEROVA_CREDITS_FLOOR_USD", "5.0")
    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    monkeypatch.setenv("FEROVA_DB_PATH", "/tmp/test_ferova.db")

    result = CliRunner().invoke(app, ["monitor-chains"])

    assert result.exit_code == 1
    assert "credits open_router [remaining=" in result.stdout
    assert "LOW" in result.stdout
    assert len(transport.get_requests) >= 1


def test_credits_ok_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy heads + sufficient credits -> exit 0, ok line."""
    transport = _MockTransport(
        credits_status=200,
        credits_payload=_credits_payload(20.0, 0.0),
    )
    monkeypatch.setattr(
        "ferova.cli.main._probe_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("FEROVA_CREDITS_FLOOR_USD", "2.0")
    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    monkeypatch.setenv("FEROVA_DB_PATH", "/tmp/test_ferova.db")

    result = CliRunner().invoke(app, ["monitor-chains"])

    assert result.exit_code == 0
    assert "credits open_router [remaining=" in result.stdout
    assert "ok" in result.stdout


def test_credits_skipped_when_key_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty key -> skipped line, no credits GET recorded."""
    transport = _MockTransport()
    monkeypatch.setattr(
        "ferova.cli.main._probe_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    monkeypatch.setenv("FEROVA_DB_PATH", "/tmp/test_ferova.db")

    result = CliRunner().invoke(app, ["monitor-chains"])

    assert result.exit_code == 0
    assert "credits open_router skipped" in result.stdout
    assert len(transport.get_requests) == 0


def test_credits_json_output_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """--json -> trailing kind=credits object shape."""
    transport = _MockTransport(
        credits_status=200,
        credits_payload=_credits_payload(20.0, 10.0),
    )
    monkeypatch.setattr(
        "ferova.cli.main._probe_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("FEROVA_CREDITS_FLOOR_USD", "2.0")
    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    monkeypatch.setenv("FEROVA_DB_PATH", "/tmp/test_ferova.db")

    import json

    result = CliRunner().invoke(app, ["monitor-chains", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    credits_elem = data[-1]
    assert credits_elem["kind"] == "credits"
    assert credits_elem["status"] == "ok"
    assert credits_elem["total_credits"] == 20.0
    assert credits_elem["total_usage"] == 10.0
    assert credits_elem["remaining"] == 10.0
    assert credits_elem["floor"] == 2.0
