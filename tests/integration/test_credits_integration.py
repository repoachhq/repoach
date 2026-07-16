"""Integration tests for SP-CREDITS-CHECK CLI path."""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from ferova.cli.main import app


class _MockTransport(httpx.MockTransport):
    """Fake transport answering NIM POSTs and a below-floor credits GET."""

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


def test_cli_credits_low_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full CliRunner monitor-chains with below-floor credits -> exit 1 + LOW line."""
    transport = _MockTransport(
        credits_status=200,
        credits_payload=_credits_payload(20.0, 18.5),
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
    assert "LOW" in result.stdout
    assert len(transport.get_requests) >= 1
