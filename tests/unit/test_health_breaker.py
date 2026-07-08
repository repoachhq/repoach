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


def test_counter_survives_ttl_lapse_prune() -> None:
    """The consecutive-failure count survives TTL-lapse pruning in
    down_refs and resets only on recover or clear (spec G2).

    Trip a ref with a tiny TTL, advance now past the TTL, call
    down_refs (which prunes the lapsed trip window and reason but
    preserves the counter), trip again — the returned count must be 2,
    not 1.  Then recover and trip once more — the count restarts at 1.
    """
    breaker = get_breaker()
    ref = _ref("groq/x")
    now = time.monotonic()

    count1 = breaker.trip(ref, now=now, ttl_s=0.01, reason="timeout")
    assert count1 == 1

    now += 0.02
    breaker.down_refs(now)
    assert not breaker.is_down(ref, now)

    count2 = breaker.trip(ref, now=now, ttl_s=0.01, reason="timeout")
    assert count2 == 2, f"counter should survive TTL-lapse prune, got {count2} expected 2"

    breaker.recover(ref)
    count3 = breaker.trip(ref, now=now, ttl_s=0.01, reason="timeout")
    assert count3 == 1, f"counter should reset on recover, got {count3} expected 1"
