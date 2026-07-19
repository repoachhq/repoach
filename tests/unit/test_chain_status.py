"""Unit tests for chain-status digest (SP-CHAIN-STATUS-DIGEST).

Uses ``httpx.AsyncClient(transport=httpx.MockTransport(...))`` as the
truthful boundary fake for /health and credits — no monkeypatching of ferova code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ferova.cli.chain_status import build_chain_status
from ferova.health.model_health import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_SLOW,
    ModelHealth,
)
from ferova.health.store import record_probes
from ferova.llm_proxy.config.settings import Settings


def _make_handler(
    *,
    health_status: int = 200,
    health_body: object = None,
    credits_status: int = 200,
    credits_body: object = None,
    credits_raises: Exception | None = None,
    health_raises: Exception | None = None,
):
    """Build an httpx MockTransport handler with configurable responses."""

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if health_raises is not None and "/health" in url and "openrouter" not in url:
            raise health_raises
        if "/health" in url and "openrouter" not in url:
            return httpx.Response(health_status, json=health_body, request=request)
        if credits_raises is not None:
            raise credits_raises
        return httpx.Response(credits_status, json=credits_body, request=request)

    return handler


def _make_client(**kwargs: object) -> httpx.AsyncClient:
    """Build an AsyncClient backed by MockTransport with the given handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(_make_handler(**kwargs)))


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """Build a Settings instance with explicit overrides for tier chains.

    Sets env vars via *monkeypatch* so ``chains.env`` cannot shadow
    caller-supplied values; ``Settings()`` reads env vars at
    construction time.
    """
    monkeypatch.setenv("MODEL_OPUS", "")
    monkeypatch.setenv("MODEL_SONNET", "")
    monkeypatch.setenv("MODEL_HAIKU", "")
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("FEROVA_CREDITS_FLOOR_USD", "2.0")
    for key, value in overrides.items():
        monkeypatch.setenv(key, str(value) if value is not None else "")
    return Settings()


def _record_sonnet_probes(
    db_path: Path,
    ok: int = 0,
    slow: int = 0,
    error: int = 0,
    empty: int = 0,
    skipped: int = 0,
    *,
    slow_latency_s: float = 10.0,
) -> None:
    """Seed sonnet-tier probe rows into the SQLite DB."""
    probes: list[ModelHealth] = []
    for _ in range(ok):
        probes.append(ModelHealth("sonnet", "nvidia_nim/sonnet-model", STATUS_OK, 1.5, 2, "ok"))
    for i in range(slow):
        lat = slow_latency_s + i
        probes.append(ModelHealth("sonnet", "nvidia_nim/sonnet-model", STATUS_SLOW, lat, 3, "slow"))
    for _ in range(error):
        probes.append(
            ModelHealth(
                "sonnet",
                "nvidia_nim/sonnet-model",
                STATUS_ERROR,
                None,
                0,
                "http=500",
            )
        )
    for _ in range(empty):
        probes.append(
            ModelHealth(
                "sonnet",
                "nvidia_nim/sonnet-model",
                STATUS_EMPTY,
                0.8,
                0,
                "http=200",
            )
        )
    for _ in range(skipped):
        probes.append(
            ModelHealth(
                "sonnet",
                "open_router/some-model",
                STATUS_SKIPPED,
                None,
                0,
                "non-NIM head",
            )
        )
    if probes:
        record_probes(db_path, probes, recorded_at=datetime.now(UTC))


async def test_nominal_tier_mix_and_avg_slow_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 ok / 2 slow / 2 error / 1 empty sonnet rows produce the exact mix and avg slow."""
    db = tmp_path / "test.db"
    _record_sonnet_probes(db, ok=5, slow=2, error=2, empty=1)

    client = _make_client(
        health_body={"status": "healthy", "breaker": []},
        credits_body={"data": {"total_credits": 20.0, "total_usage": 19.5}},
    )
    settings = _settings(
        monkeypatch,
        MODEL_SONNET="nvidia_nim/sonnet-model",
        FEROVA_OPENROUTER_API_KEY="test-key",
    )

    result = await build_chain_status(
        str(db), 24.0, proxy_url="http://127.0.0.1:8082", client=client, settings=settings
    )

    assert "50% ok · 20% slow · 30% err" in result
    assert "n=10" in result
    assert "avg slow 10.5s" in result
    assert "head=nvidia_nim/sonnet-model" in result


async def test_unmonitored_head_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-NIM sonnet head renders UNMONITORED."""
    db = tmp_path / "test.db"

    client = _make_client(
        health_body={"status": "healthy", "breaker": []},
        credits_body={"data": {"total_credits": 20.0, "total_usage": 0.0}},
    )
    settings = _settings(
        monkeypatch,
        MODEL_SONNET="open_router/anthropic/claude-sonnet-4-20250514",
        FEROVA_OPENROUTER_API_KEY="test-key",
    )

    result = await build_chain_status(
        str(db), 24.0, proxy_url="http://127.0.0.1:8082", client=client, settings=settings
    )

    assert "head=open_router/anthropic/claude-sonnet-4-20250514" in result
    assert "UNMONITORED (probe skips non-NIM heads)" in result


async def test_breaker_mapping_chained_and_unchained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A /health snapshot with chained and unchained refs renders on their respective lines."""
    db = tmp_path / "test.db"

    breaker_payload = [
        {
            "ref": "nvidia_nim/sonnet-degraded",
            "reason": "provider_400",
            "ttl_remaining_s": 15120.0,
            "consecutive_failures": 7,
        },
        {
            "ref": "nvidia_nim/unchained-model",
            "reason": "timeout",
            "ttl_remaining_s": 90.0,
            "consecutive_failures": 2,
        },
    ]
    client = _make_client(
        health_body={"status": "healthy", "breaker": breaker_payload},
        credits_body={"data": {"total_credits": 20.0, "total_usage": 0.0}},
    )
    settings = _settings(
        monkeypatch,
        model_sonnet="nvidia_nim/sonnet-degraded,claude_code/sonnet",
        FEROVA_OPENROUTER_API_KEY="test-key",
    )

    result = await build_chain_status(
        str(db), 24.0, proxy_url="http://127.0.0.1:8082", client=client, settings=settings
    )

    assert "  breaker: sonnet nvidia_nim/sonnet-degraded" in result
    assert "  breaker (unchained): nvidia_nim/unchained-model" in result


async def test_credits_none_renders_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credits fetch returning None renders 'credits: unavailable'."""
    db = tmp_path / "test.db"

    client = _make_client(
        health_body={"status": "healthy", "breaker": []},
        credits_status=500,
        credits_body={},
    )
    settings = _settings(
        monkeypatch,
        model_sonnet="nvidia_nim/sonnet-model",
        FEROVA_OPENROUTER_API_KEY="test-key",
    )

    result = await build_chain_status(
        str(db), 24.0, proxy_url="http://127.0.0.1:8082", client=client, settings=settings
    )

    assert "  credits: unavailable" in result
