"""Unit test for the /health breaker view (SP-CHAIN-DEAD-HOP-QUARANTINE step 4).

Covers the route handler returning a ``breaker`` array built from
BreakerState.snapshot(now) with ref, reason, ttl_remaining_s, and
consecutive_failures.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from ferova.llm_proxy.api.app import create_app
from ferova.llm_proxy.routing import get_breaker, reset_breaker
from ferova.llm_proxy.routing.refs import ModelRef


@pytest.fixture(autouse=True)
def _hermetic_breaker() -> None:
    """Clear the process-level breaker before each test."""
    reset_breaker()


def _ref(spec: str) -> ModelRef:
    return ModelRef.parse(spec)


def test_health_reports_breaker_entries() -> None:
    """GET /health returns a breaker array with each down ref's reason,
    ttl_remaining_s, and consecutive_failures; empty when nothing is down."""
    app = create_app()
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["breaker"] == []

    breaker = get_breaker()
    now = time.monotonic()
    breaker.trip(_ref("groq/x"), now=now, ttl_s=60.0, reason="timeout")
    breaker.trip(_ref("kimi/y"), now=now, ttl_s=120.0, reason="provider_402")

    resp2 = client.get("/health")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["status"] == "healthy"
    breaker_arr = body2["breaker"]
    assert len(breaker_arr) == 2

    by_ref = {entry["ref"]: entry for entry in breaker_arr}
    assert by_ref["groq/x"]["reason"] == "timeout"
    assert by_ref["groq/x"]["consecutive_failures"] == 1
    assert 0 < by_ref["groq/x"]["ttl_remaining_s"] <= 60.0
    assert by_ref["kimi/y"]["reason"] == "provider_402"
    assert by_ref["kimi/y"]["consecutive_failures"] == 1
    assert 0 < by_ref["kimi/y"]["ttl_remaining_s"] <= 120.0

    breaker.recover(_ref("groq/x"))
    resp3 = client.get("/health")
    body3 = resp3.json()
    assert len(body3["breaker"]) == 1
    assert body3["breaker"][0]["ref"] == "kimi/y"
