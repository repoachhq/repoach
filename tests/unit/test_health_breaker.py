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
from ferova.llm_proxy.routing.breaker import BreakerState, ttl_for_reason
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
