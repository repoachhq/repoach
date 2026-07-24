"""Tests for the failover health breaker (SP-PROXY-HEALTH-BREAKER).

Covers the BreakerState value logic, the router filtering a tripped ref
out of the resolved chain, and the service wiring that trips it.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger
from pydantic import ValidationError

from repoach.health.credits import reset_credits_cache
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_credits_client, get_settings
from repoach.llm_proxy.api.model_router import ModelRouter, ResolvedModel
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import (
    Settings,
)
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.breaker import (
    BreakerState,
    escalated_ttl,
    ttl_for_reason,
)
from repoach.llm_proxy.routing.refs import ModelRef


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
    for key in ("MODEL", "MODEL_SONNET", "REPOACH_PROXY_DEFAULT_MODEL", "REPOACH_MODEL_SONNET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/default/model")
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
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true" if enabled else "false")
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


def _clean_env_for_quarantine_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip quarantine-related env vars and disable env-file loading."""
    for key in (
        "BREAKER_TTL_QUARANTINE_S",
        "REPOACH_BREAKER_TTL_QUARANTINE_S",
        "BREAKER_QUARANTINE_THRESHOLD",
        "REPOACH_BREAKER_QUARANTINE_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings_module, "_env_files", lambda: ())
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())


def _build_settings() -> Settings:
    """Return a fresh Settings, bypassing the lru_cache + env file load."""
    return Settings(_env_file=None)


def test_quarantine_settings_defaults_and_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults are 21600.0 and 3; REPOACH_ and bare aliases override;
    threshold 0 is rejected."""
    _clean_env_for_quarantine_test(monkeypatch)
    settings = _build_settings()
    assert settings.breaker_ttl_quarantine_s == 21_600.0
    assert settings.breaker_quarantine_threshold == 3

    _clean_env_for_quarantine_test(monkeypatch)
    monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "3600.0")
    monkeypatch.setenv("REPOACH_BREAKER_QUARANTINE_THRESHOLD", "5")
    settings = _build_settings()
    assert settings.breaker_ttl_quarantine_s == 3600.0
    assert settings.breaker_quarantine_threshold == 5

    _clean_env_for_quarantine_test(monkeypatch)
    monkeypatch.setenv("BREAKER_TTL_QUARANTINE_S", "7200.0")
    monkeypatch.setenv("BREAKER_QUARANTINE_THRESHOLD", "7")
    settings = _build_settings()
    assert settings.breaker_ttl_quarantine_s == 7200.0
    assert settings.breaker_quarantine_threshold == 7

    _clean_env_for_quarantine_test(monkeypatch)
    monkeypatch.setenv("REPOACH_BREAKER_QUARANTINE_THRESHOLD", "0")
    with pytest.raises(ValidationError):
        _build_settings()


def _capture_loguru() -> tuple[list[Any], int]:
    """Install a loguru sink that buffers records; return (records, sink_id)."""
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    return records, sink_id


def test_trip_breaker_composes_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_trip_breaker composes the quarantine escalation policy.

    Three empty_completion trips on the same ref escalate the third to
    the quarantine TTL and emit breaker_quarantined exactly once.
    A provider_402 failure gets the quarantine TTL on the first trip.
    """
    records, sink_id = _capture_loguru()
    try:
        monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_S", "120")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "3600")
        monkeypatch.setenv("REPOACH_BREAKER_QUARANTINE_THRESHOLD", "3")
        settings = Settings(_env_file=None)
        service = ClaudeProxyService(
            settings=settings,
            provider_getter=lambda _provider_id: None,
            token_counter=lambda *_args, **_kwargs: 0,
        )
        candidate = _candidate()
        ref = _ref("groq/x")

        base = time.monotonic()

        service._trip_breaker(candidate, "empty_completion")
        assert get_breaker().is_down(ref, now=base + 60.0)
        assert not get_breaker().is_down(ref, now=base + 130.0)

        base2 = time.monotonic()
        service._trip_breaker(candidate, "empty_completion")
        assert get_breaker().is_down(ref, now=base2 + 60.0)
        assert not get_breaker().is_down(ref, now=base2 + 130.0)

        base3 = time.monotonic()
        service._trip_breaker(candidate, "empty_completion")
        assert get_breaker().is_down(ref, now=base3 + 130.0)
        assert not get_breaker().is_down(ref, now=base3 + 3610.0)

        quarantined = [r for r in records if r["message"] == "breaker_quarantined"]
        assert len(quarantined) == 1
        assert quarantined[0]["extra"]["ref"] == "groq/x"
        assert quarantined[0]["extra"]["reason"] == "empty_completion"
        assert quarantined[0]["extra"]["count"] == 3
        assert quarantined[0]["extra"]["ttl_s"] == 3600.0

        get_breaker().clear()
        records.clear()

        base4 = time.monotonic()
        service._trip_breaker(candidate, "provider_402")
        assert get_breaker().is_down(ref, now=base4 + 130.0)
        assert not get_breaker().is_down(ref, now=base4 + 3610.0)

        quarantined2 = [r for r in records if r["message"] == "breaker_quarantined"]
        assert len(quarantined2) == 1
        assert quarantined2[0]["extra"]["ref"] == "groq/x"
        assert quarantined2[0]["extra"]["reason"] == "provider_402"
        assert quarantined2[0]["extra"]["count"] == 1
        assert quarantined2[0]["extra"]["ttl_s"] == 3600.0
    finally:
        loguru_logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _hermetic_breaker() -> None:
    """Clear the process-level breaker before each test."""
    reset_breaker()


@pytest.fixture(autouse=True)
def _no_configured_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the real ``.env``'s ``anthropic_auth_token`` for this file.

    This file's ``/health`` tests exercise the breaker/credits mechanics,
    not auth (SP-PROXY-EDGE-HARDEN gates the detailed body behind
    ``require_api_key``); with no token configured that check stays a
    no-op, so every unauthenticated call here keeps seeing the full body
    regardless of the developer's local ``.env``.
    """
    monkeypatch.delenv("REPOACH_ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    settings_module.get_settings.cache_clear()


def test_health_reports_breaker_entries() -> None:
    """GET /health returns a breaker array with each down ref's reason,
    ttl_remaining_s, and consecutive_failures; empty when nothing is down."""
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
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


def test_record_success_k_of_n_window() -> None:
    """Three slow among the last five triggers; a fourth is still a
    trigger; the window truncates to exactly n."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    assert not breaker.record_success(ref, True, k=3, n=5)
    assert not breaker.record_success(ref, True, k=3, n=5)
    assert breaker.record_success(ref, True, k=3, n=5)

    assert breaker.record_success(ref, True, k=3, n=5)

    assert len(breaker._slow_history[ref]) == 4


def test_record_success_below_k_returns_false() -> None:
    """Two slow among five never trips when k=3."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    assert not breaker.record_success(ref, True, k=3, n=5)
    assert not breaker.record_success(ref, False, k=3, n=5)
    assert not breaker.record_success(ref, True, k=3, n=5)
    assert not breaker.record_success(ref, False, k=3, n=5)
    assert not breaker.record_success(ref, False, k=3, n=5)

    assert not breaker.record_success(ref, False, k=3, n=5)

    assert len(breaker._slow_history[ref]) == 5


def test_recover_clears_slow_history() -> None:
    """recover() pops the ref from _slow_history."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    breaker.record_success(ref, True, k=3, n=5)
    breaker.record_success(ref, True, k=3, n=5)
    assert ref in breaker._slow_history

    breaker.recover(ref)
    assert ref not in breaker._slow_history


def test_trip_slow_behavior() -> None:
    """trip_slow sets _down_until and _down_reason but does not touch
    _consecutive_failures; the ref is down after trip."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    breaker._consecutive_failures[ref] = 7

    breaker.trip_slow(ref, now=100.0, ttl_s=60.0)

    assert breaker._consecutive_failures.get(ref) == 7
    assert breaker._down_reason[ref] == "slow_completion"
    assert breaker.is_down(ref, now=120.0)
    assert not breaker.is_down(ref, now=161.0)

    breaker.trip_slow(ref, now=200.0, ttl_s=30.0, reason="custom_slow")
    assert breaker._down_reason[ref] == "custom_slow"
    assert breaker._consecutive_failures.get(ref) == 7


def test_slow_history_survives_slow_ttl_lapse() -> None:
    """AC2: a slow-completion trip's TTL lapse does NOT erase the ref's
    slow history. Trip via ``trip_slow`` with a tiny TTL, advance ``now``
    past the TTL, call ``down_refs`` to prune the lapsed trip window,
    then assert the ref's ``_slow_history`` is still populated and the
    next ``record_success`` call folds into the surviving window.
    """
    breaker = BreakerState()
    ref = _ref("groq/x")

    breaker.record_success(ref, True, k=3, n=5)
    breaker.record_success(ref, True, k=3, n=5)
    breaker.record_success(ref, True, k=3, n=5)

    breaker.trip_slow(ref, now=100.0, ttl_s=10.0)
    assert breaker.is_down(ref, now=105.0)

    breaker.down_refs(now=120.0)
    assert not breaker.is_down(ref, now=120.0)

    assert ref in breaker._slow_history
    assert len(breaker._slow_history[ref]) == 3

    triggered = breaker.record_success(ref, True, k=3, n=5)
    assert triggered
    assert len(breaker._slow_history[ref]) == 4


def test_slow_history_survives_down_refs_prune() -> None:
    """down_refs() prunes the trip window but leaves _slow_history intact."""
    breaker = BreakerState()
    ref = _ref("groq/x")

    breaker.record_success(ref, True, k=3, n=5)
    breaker.record_success(ref, True, k=3, n=5)
    breaker.record_success(ref, True, k=3, n=5)

    breaker.trip(ref, now=100.0, ttl_s=10.0, reason="timeout")
    assert breaker.is_down(ref, now=105.0)

    breaker.down_refs(now=120.0)
    assert not breaker.is_down(ref, now=120.0)

    assert ref in breaker._slow_history
    assert len(breaker._slow_history[ref]) == 3


_CREDITS_PAYLOAD = {"data": {"total_credits": 20.0, "total_usage": 10.0}}


def _make_credits_app(
    *,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int = 200,
    json_body: object = None,
) -> tuple[TestClient, list[httpx.Request]]:
    """Create a TestClient app with overridden credits client and settings.

    Returns the ``TestClient`` and a list that captures every GET
    request the transport receives so callers can assert call counts.
    """
    reset_credits_cache()
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json=json_body, request=request)

    transport = httpx.MockTransport(handler)
    credits_client = httpx.AsyncClient(transport=transport)

    app = create_app()
    monkeypatch.setenv("REPOACH_OPENROUTER_API_KEY", "test-key")
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    app.dependency_overrides[get_credits_client] = lambda: credits_client

    return TestClient(app), captured


def test_health_credits_cached_single_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two /health calls within the TTL perform exactly one upstream GET."""
    client, captured = _make_credits_app(monkeypatch=monkeypatch, json_body=_CREDITS_PAYLOAD)

    resp1 = client.get("/health")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["credits"] == {
        "open_router": {"total_credits": 20.0, "total_usage": 10.0, "remaining": 10.0},
    }
    assert body1["status"] == "healthy"
    assert isinstance(body1["breaker"], list)

    resp2 = client.get("/health")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["credits"] is not None

    assert len(captured) == 1


def test_health_credits_null_on_upstream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upstream 500 yields ``credits: null`` with HTTP 200."""
    client, _captured = _make_credits_app(monkeypatch=monkeypatch, status_code=500, json_body={})

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits"] is None
    assert body["status"] == "healthy"
    assert isinstance(body["breaker"], list)


def test_trip_breaker_propagates_account_fault_to_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider_402 on one open_router ref benches every sibling
    open_router ref in the resolved chain with reason
    provider_402_propagated, and emits exactly one
    breaker_provider_propagated log event."""
    records, sink_id = _capture_loguru()
    try:
        monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_S", "120")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "3600")
        settings = Settings(_env_file=None)
        service = ClaudeProxyService(
            settings=settings,
            provider_getter=lambda _provider_id: None,
            token_counter=lambda *_args, **_kwargs: 0,
        )
        chain = [
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="qwen/qwen3.7-max",
                provider_model_ref="open_router/qwen/qwen3.7-max",
            ),
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="anthropic/claude-sonnet-4",
                provider_model_ref="open_router/anthropic/claude-sonnet-4",
            ),
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="google/gemini-2.5-pro",
                provider_model_ref="open_router/google/gemini-2.5-pro",
            ),
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="claude_code",
                provider_model="sonnet",
                provider_model_ref="claude_code/sonnet",
            ),
        ]
        service._trip_breaker(chain[0], "provider_402", chain=chain)

        now = time.monotonic()
        for c in chain[:3]:
            ref = _ref(c.provider_model_ref)
            assert get_breaker().is_down(ref, now), (
                f"{ref} should be down after account-fault propagation"
            )
            assert get_breaker()._down_reason.get(ref) == "provider_402_propagated"
        claude_ref = _ref("claude_code/sonnet")
        assert not get_breaker().is_down(claude_ref, now), (
            "claude_code ref must not be affected by open_router propagation"
        )

        propagated_logs = [r for r in records if r["message"] == "breaker_provider_propagated"]
        assert len(propagated_logs) == 1
        assert propagated_logs[0]["extra"]["provider"] == "open_router"
        assert propagated_logs[0]["extra"]["ref_count"] == 3
        assert propagated_logs[0]["extra"]["ttl_s"] == 3600.0

        quarantined_logs = [r for r in records if r["message"] == "breaker_quarantined"]
        assert len(quarantined_logs) == 0, "account-fault path must not emit breaker_quarantined"
    finally:
        loguru_logger.remove(sink_id)


def test_trip_breaker_404_stays_single_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider_404 on an open_router ref benches only that ref;
    siblings of the same provider are untouched (G2)."""
    records, sink_id = _capture_loguru()
    try:
        monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_S", "120")
        monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "3600")
        settings = Settings(_env_file=None)
        service = ClaudeProxyService(
            settings=settings,
            provider_getter=lambda _provider_id: None,
            token_counter=lambda *_args, **_kwargs: 0,
        )
        chain = [
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="qwen/qwen3.7-max",
                provider_model_ref="open_router/qwen/qwen3.7-max",
            ),
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="anthropic/claude-sonnet-4",
                provider_model_ref="open_router/anthropic/claude-sonnet-4",
            ),
            ResolvedModel(
                original_model="claude-sonnet-4",
                provider_id="open_router",
                provider_model="google/gemini-2.5-pro",
                provider_model_ref="open_router/google/gemini-2.5-pro",
            ),
        ]
        service._trip_breaker(chain[0], "provider_404", chain=chain)

        now = time.monotonic()
        first_ref = _ref(chain[0].provider_model_ref)
        assert get_breaker().is_down(first_ref, now), "the failing ref must be down"
        assert get_breaker()._down_reason.get(first_ref) == "provider_404"

        for c in chain[1:]:
            ref = _ref(c.provider_model_ref)
            assert not get_breaker().is_down(ref, now), (
                f"{ref} must not be affected by provider_404 on a sibling"
            )

        propagated_logs = [r for r in records if r["message"] == "breaker_provider_propagated"]
        assert len(propagated_logs) == 0, (
            "provider_404 is not an account fault; propagation must not fire"
        )
    finally:
        loguru_logger.remove(sink_id)


def test_get_credits_client_returns_async_client() -> None:
    """The dependency returns an httpx.AsyncClient."""
    client = get_credits_client()
    assert isinstance(client, httpx.AsyncClient)
