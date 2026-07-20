"""Integration tests for SP-CREDITS-CHECK CLI path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from repoach.cli.main import app


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
        "repoach.cli.main._probe_client",
        lambda: httpx.AsyncClient(transport=transport),
    )
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("FEROVA_CREDITS_FLOOR_USD", "5.0")
    monkeypatch.setenv("MODEL", "nvidia_nim/x")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/thinker,claude_code/sonnet")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/haiku")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "k")
    monkeypatch.setenv("REPOACH_DB_PATH", "/tmp/test_repoach.db")

    result = CliRunner().invoke(app, ["monitor-chains"])

    assert result.exit_code == 1
    assert "LOW" in result.stdout
    assert len(transport.get_requests) >= 1


_REPO = Path(__file__).resolve().parents[2]


def test_chain_status_end_to_end_degraded_environment(tmp_path: Path) -> None:
    """Invoke ``repoach chain-status`` against a fresh db and an unbound proxy.

    Asserts exit code 0, the expected degraded digest lines, and no
    traceback on stderr (fail-open contract G4 of SP-CHAIN-STATUS-DIGEST).
    """
    db_path = tmp_path / "db" / "repoach.db"
    db_path.parent.mkdir()

    result = subprocess.run(
        [
            "repoach",
            "chain-status",
            "--db-path",
            str(db_path),
            "--proxy-url",
            "http://127.0.0.1:19999",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
        env={**os.environ, "FEROVA_OPENROUTER_API_KEY": ""},
    )

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "no probes in window" in result.stdout
    assert "proxy: unreachable" in result.stdout
    assert "Traceback (most recent call last)" not in result.stderr
