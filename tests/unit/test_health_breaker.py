"""Tests for the failover health breaker (SP-PROXY-HEALTH-BREAKER).

Covers the BreakerState value logic, the router filtering a tripped ref
out of the resolved chain, and the service wiring that trips it.
"""

from __future__ import annotations

import time

import pytest

from ferova.llm_proxy.api.model_router import ModelRouter, ResolvedModel
from ferova.llm_proxy.api.services import ClaudeProxyService
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.routing import get_breaker
from ferova.llm_proxy.routing.breaker import (
    BreakerState,
    escalated_ttl,
    ttl_for_reason,
)
from ferova.llm_proxy.routing.refs import ModelRef


def _ref(spec: str) -> ModelRef:
    return ModelRef.parse(spec)


def test_trip_then_is_down_until_ttl() -> None:
    breaker = BreakerState()
    ref = _ref("groq/x")
    breaker.trip(ref, now=100.0, ttl_s=60.0)
    assert breaker.is_down(ref, now=120.0)
    assert not breaker.is_down(ref, now=161.0)


def test_down_refs_prunes_expired() -> None:
    breaker = BreakerState()
    breaker.trip(_ref("groq/x"), now=100.0, ttl_s=10.0)
    breaker.trip(_ref("kimi/y"), now=100.0, ttl_s=100.0)
    assert breaker.down_refs(now=120.0) == frozenset({_ref("kimi/y")})


def test_recover_clears_immediately() -> None:
    breaker = BreakerState()
    ref = _ref("groq/x")
    breaker.trip(ref, now=100.0, ttl_s=60.0)
    breaker.recover(ref)
    assert not breaker.is_down(ref, now=110.0)


def test_trip_extends_but_never_shortens() -> None:
    breaker = BreakerState()
    ref = _ref("groq/x")
    breaker.trip(ref, now=100.0, ttl_s=100.0)
    breaker.trip(ref, now=100.0, ttl_s=10.0)
    assert breaker.is_down(ref, now=150.0)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> ModelRouter:
    for key in ("MODEL", "MODEL_SONNET", "FEROVA_PROXY_DEFAULT_MODEL", "FEROVA_MODEL_SONNET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEROVA_PROXY_DEFAULT_MODEL", "nvidia_nim/default/model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/a/b,open_router/c,claude_code/sonnet")
    return ModelRouter(Settings(_env_file=None))


def _chain_refs(router: ModelRouter) -> list[str]:
    return [c.provider_model_ref for c in router.resolve_chain("claude-sonnet-4")]


def test_resolve_chain_excludes_tripped_ref(router: ModelRouter) -> None:
    get_breaker().trip(_ref("nvidia_nim/a/b"), now=time.monotonic(), ttl_s=1000.0)
    assert _chain_refs(router) == ["open_router/c", "claude_code/sonnet"]


def test_resolve_chain_all_down_falls_back_to_head(router: ModelRouter) -> None:
    for spec in ("nvidia_nim/a/b", "open_router/c", "claude_code/sonnet"):
        get_breaker().trip(_ref(spec), now=time.monotonic(), ttl_s=1000.0)
    assert _chain_refs(router) == ["nvidia_nim/a/b"]


def _service(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> ClaudeProxyService:
    monkeypatch.setenv("FEROVA_BREAKER_ENABLED", "true" if enabled else "false")
    return ClaudeProxyService(
        settings=Settings(_env_file=None),
        provider_getter=lambda _provider_id: None,
        token_counter=lambda *_args, **_kwargs: 0,
    )


def _candidate() -> ResolvedModel:
    return ResolvedModel(
        original_model="claude-sonnet-4",
        provider_id="groq",
        provider_model="x",
        provider_model_ref="groq/x",
    )


def test_trip_breaker_marks_candidate_down(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, enabled=True)
    service._trip_breaker(_candidate(), "timeout")
    assert get_breaker().is_down(_ref("groq/x"), now=time.monotonic())


def test_trip_breaker_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(monkeypatch, enabled=False)
    service._trip_breaker(_candidate(), "timeout")
    assert not get_breaker().is_down(_ref("groq/x"), now=time.monotonic())


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("provider_410", 604_800.0),
        ("timeout", 120.0),
        ("rate_limited", 120.0),
        ("provider_5xx", 120.0),
        ("transport_error", 120.0),
        ("empty_completion", 120.0),
        ("provider_404", 120.0),
        ("exception:RuntimeError", 120.0),
    ],
)
def test_ttl_for_reason_separates_terminal_from_transient(reason: str, expected: float) -> None:
    assert ttl_for_reason(reason, default_ttl_s=120.0, terminal_ttl_s=604_800.0) == expected


def test_trip_breaker_terminal_410_stays_down_for_terminal_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch, enabled=True)
    base = time.monotonic()
    service._trip_breaker(_candidate(), "provider_410")
    ref = _ref("groq/x")
    assert get_breaker().is_down(ref, now=base + 120.0 + 5.0)
    assert not get_breaker().is_down(ref, now=base + 604_800.0 + 5.0)


def test_trip_breaker_transient_timeout_recovers_after_transient_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(monkeypatch, enabled=True)
    base = time.monotonic()
    service._trip_breaker(_candidate(), "timeout")
    assert not get_breaker().is_down(_ref("groq/x"), now=base + 120.0 + 5.0)


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("auth_failed", 21_600.0),
        ("provider_401", 21_600.0),
        ("provider_402", 21_600.0),
        ("provider_403", 21_600.0),
        ("provider_404", 21_600.0),
        ("provider_410", 604_800.0),
        ("timeout", 120.0),
        ("empty_completion", 120.0),
        ("rate_limited", 120.0),
        ("unknown", 120.0),
    ],
)
def test_ttl_for_reason_quarantine_class(reason: str, expected: float) -> None:
    """Quarantine-class reasons get quarantine TTL; terminal beats quarantine;
    transient/unknown get default."""
    assert (
        ttl_for_reason(
            reason,
            default_ttl_s=120.0,
            terminal_ttl_s=604_800.0,
            quarantine_ttl_s=21_600.0,
        )
        == expected
    )


def test_consecutive_failures_escalate_to_quarantine() -> None:
    """Three trips with reason empty_completion escalate to quarantine TTL;
    the counter survives TTL lapse between trips."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    count1 = breaker.trip(ref, now=100.0, ttl_s=120.0, reason="empty_completion")
    assert count1 == 1
    assert breaker.is_down(ref, now=150.0)
    assert not breaker.is_down(ref, now=230.0)

    count2 = breaker.trip(ref, now=300.0, ttl_s=120.0, reason="empty_completion")
    assert count2 == 2
    assert breaker.is_down(ref, now=350.0)
    assert not breaker.is_down(ref, now=430.0)

    count3 = breaker.trip(ref, now=500.0, ttl_s=120.0, reason="empty_completion")
    assert count3 == 3
    escalated = escalated_ttl(count3, base_ttl_s=120.0, quarantine_ttl_s=21_600.0, threshold=3)
    assert escalated == 21_600.0

    breaker.trip(ref, now=500.0, ttl_s=escalated, reason="empty_completion")
    assert breaker.is_down(ref, now=500.0 + 120.0 + 1.0)
    assert not breaker.is_down(ref, now=500.0 + 21_600.0 + 1.0)


def test_recover_resets_counter() -> None:
    """Trip, trip, recover, trip — count restarts at 1."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    breaker.trip(ref, now=100.0, ttl_s=120.0, reason="timeout")
    breaker.trip(ref, now=200.0, ttl_s=120.0, reason="timeout")
    breaker.recover(ref)
    count = breaker.trip(ref, now=300.0, ttl_s=120.0, reason="timeout")
    assert count == 1


def test_snapshot_lists_down_refs() -> None:
    """Two tripped refs with distinct reasons — snapshot lists both with
    reason, remaining TTL, and count; a recovered ref disappears."""
    breaker = BreakerState()
    ref_a = _ref("groq/x")
    ref_b = _ref("kimi/y")

    breaker.trip(ref_a, now=100.0, ttl_s=60.0, reason="timeout")
    breaker.trip(ref_b, now=100.0, ttl_s=120.0, reason="provider_402")

    snap = breaker.snapshot(now=130.0)
    assert len(snap) == 2

    by_ref = {str(e.ref): e for e in snap}
    assert by_ref["groq/x"].reason == "timeout"
    assert by_ref["groq/x"].consecutive_failures == 1
    assert 25.0 < by_ref["groq/x"].ttl_remaining_s < 35.0
    assert by_ref["kimi/y"].reason == "provider_402"
    assert by_ref["kimi/y"].consecutive_failures == 1
    assert 85.0 < by_ref["kimi/y"].ttl_remaining_s < 95.0

    breaker.recover(ref_a)
    snap2 = breaker.snapshot(now=130.0)
    assert len(snap2) == 1
    assert str(snap2[0].ref) == "kimi/y"
