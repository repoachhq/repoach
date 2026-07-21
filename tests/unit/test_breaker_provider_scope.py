"""Tests for provider-scoped breaker tripping (SP-BREAKER-PROVIDER-SCOPE step 1).

Covers trip_provider semantics on BreakerState: account-fault
propagation, 404 single-ref behaviour, idempotency, and provider
isolation.
"""

from __future__ import annotations

from repoach.llm_proxy.routing.breaker import BreakerState
from repoach.llm_proxy.routing.refs import ModelRef


def _ref(spec: str) -> ModelRef:
    return ModelRef.parse(spec)


def test_account_fault_benches_all_provider_refs() -> None:
    """trip_provider with three open_router refs benches all three with
    the same ttl and reason."""
    breaker = BreakerState()
    ref_a = _ref("open_router/meta-llama/llama-4-maverick")
    ref_b = _ref("open_router/qwen/qwen3.7-max")
    ref_c = _ref("open_router/anthropic/claude-sonnet-4")
    ref_other = _ref("nvidia_nim/mistralai/mistral-medium")

    all_refs = [ref_a, ref_b, ref_c, ref_other]
    breaker.trip_provider(
        "open_router",
        all_refs,
        now=100.0,
        ttl_s=3600.0,
        reason="provider_402",
    )

    for ref in (ref_a, ref_b, ref_c):
        assert breaker.is_down(ref, now=110.0)
        assert not breaker.is_down(ref, now=4000.0)

    assert not breaker.is_down(ref_other, now=110.0)

    snap = breaker.snapshot(now=110.0)
    assert len(snap) == 3
    by_ref = {str(e.ref): e for e in snap}
    for key in (
        "open_router/meta-llama/llama-4-maverick",
        "open_router/qwen/qwen3.7-max",
        "open_router/anthropic/claude-sonnet-4",
    ):
        assert by_ref[key].reason == "provider_402"
        assert by_ref[key].consecutive_failures == 1
        assert 3580.0 < by_ref[key].ttl_remaining_s < 3600.0


def test_404_stays_single_ref() -> None:
    """Calling plain trip for provider_404 on one ref leaves siblings
    untouched."""
    breaker = BreakerState()
    ref_a = _ref("open_router/meta-llama/llama-4-maverick")
    ref_b = _ref("open_router/qwen/qwen3.7-max")
    ref_c = _ref("open_router/anthropic/claude-sonnet-4")

    breaker.trip(ref_a, now=100.0, ttl_s=3600.0, reason="provider_404")

    assert breaker.is_down(ref_a, now=110.0)
    assert not breaker.is_down(ref_b, now=110.0)
    assert not breaker.is_down(ref_c, now=110.0)

    snap = breaker.snapshot(now=110.0)
    assert len(snap) == 1
    assert str(snap[0].ref) == "open_router/meta-llama/llama-4-maverick"
    assert snap[0].reason == "provider_404"


def test_propagation_idempotent_on_rebench() -> None:
    """Calling trip_provider twice does not shorten or duplicate the trip
    state."""
    breaker = BreakerState()
    ref_a = _ref("open_router/meta-llama/llama-4-maverick")
    ref_b = _ref("open_router/qwen/qwen3.7-max")

    all_refs = [ref_a, ref_b]

    breaker.trip_provider("open_router", all_refs, now=100.0, ttl_s=3600.0, reason="provider_402")
    breaker.trip_provider("open_router", all_refs, now=200.0, ttl_s=10.0, reason="provider_402")

    assert breaker.is_down(ref_a, now=3500.0)
    assert breaker.is_down(ref_b, now=3500.0)

    snap = breaker.snapshot(now=210.0)
    assert len(snap) == 2
    by_ref = {str(e.ref): e for e in snap}
    assert by_ref["open_router/meta-llama/llama-4-maverick"].consecutive_failures == 2
    assert by_ref["open_router/qwen/qwen3.7-max"].consecutive_failures == 2


def test_other_provider_refs_untouched() -> None:
    """A claude_code ref stays up after an open_router trip_provider call."""
    breaker = BreakerState()
    ref_openrouter = _ref("open_router/meta-llama/llama-4-maverick")
    ref_claude = _ref("claude_code/sonnet")

    all_refs = [ref_openrouter, ref_claude]

    breaker.trip_provider("open_router", all_refs, now=100.0, ttl_s=3600.0, reason="provider_402")

    assert breaker.is_down(ref_openrouter, now=110.0)
    assert not breaker.is_down(ref_claude, now=110.0)

    snap = breaker.snapshot(now=110.0)
    assert len(snap) == 1
    assert str(snap[0].ref) == "open_router/meta-llama/llama-4-maverick"
