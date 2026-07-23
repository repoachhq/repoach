"""Integration test for SP-PROXY-STATE-PERSIST.

End-to-end across a SIMULATED proxy restart: every breaker mutation that
leaves a ref DOWN is mirrored to a real SQLite file (write-through), the
process-level singleton is wiped with ``reset_breaker()`` exactly as a
real process exit would wipe it, and ``rehydrate_breaker_from_state``
(standing in for the new process's ``AppRuntime.startup()``) restores it
with the same reason, failure count, and slow-history window — never
re-incremented, never reset. Hermetic — no network, no ``.env``.

``test_provider_quarantine_survives_simulated_restart`` drives a real
:meth:`ClaudeProxyService.create_message` call against a fake 402
provider boundary (the ``test_provider_scope_and_credits_gate.py``
style) so the provider-wide propagation, the write-through, AND the
rehydration all execute through the real dispatch path. The remaining
tests drive :class:`BreakerState` + ``breaker_persist`` + a real SQLite
file directly — the gaps this spec closes (non-head refs, consecutive
escalation, slow strikes, recovery) do not depend on the HTTP layer to
demonstrate.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from repoach.llm_proxy.api.models.anthropic import MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.exceptions import APIError
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.breaker_persist import persist_state, rehydrate_breaker_from_state
from repoach.llm_proxy.routing.refs import ModelRef


class _Failing402Provider(BaseProvider):
    """Provider whose stream_response always raises a 402 API error."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, call_log: list[str]) -> None:
        super().__init__(config)
        self._call_log = call_log

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        self._call_log.append(f"open_router/{request.model}")
        raise APIError("Insufficient credits", status_code=402)
        yield ""


class _HealthyProvider(BaseProvider):
    """Provider whose stream always yields real text content."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        yield (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"x","type":"message",'
            '"role":"assistant","content":[],"model":"test","stop_reason":null,'
            '"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        )
        yield (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":"hello"}}\n\n'
        )
        yield (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello from fallback"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        )
        yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


async def _drain(response: Any) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(chunks)


def _restart(db_path: Path) -> int:
    """Simulate a proxy restart: wipe the singleton, then rehydrate it.

    Stands in for a fresh process's ``AppRuntime.startup()`` calling
    :func:`rehydrate_breaker_from_state` against the same durable file.
    """
    reset_breaker()
    return rehydrate_breaker_from_state(
        get_breaker(),
        db_path=db_path,
        monotonic_now=time.monotonic(),
        wall_clock_now=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_provider_quarantine_survives_simulated_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 402 quarantines every open_router sibling ref; both survive a
    simulated restart with the same propagated reason and a remaining
    TTL close to the original 6h quarantine window.
    """
    reset_breaker()
    db = tmp_path / "breaker.db"
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "kimi/healthy-model")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "open_router/failing-model,open_router/second-model,kimi/healthy-model",
    )
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))
    settings = Settings(_env_file=None)

    call_log: list[str] = []
    failing_provider = _Failing402Provider(ProviderConfig(api_key="x"), call_log=call_log)
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    providers: dict[str, BaseProvider] = {
        "open_router": failing_provider,
        "kimi": healthy_provider,
    }

    service = ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
    )

    request_data = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=32,
        messages=[{"role": "user", "content": "ping"}],
    )

    response = service.create_message(request_data)
    body = await _drain(response)
    assert "Hello from fallback" in body

    failing_ref = ModelRef.parse("open_router/failing-model")
    second_ref = ModelRef.parse("open_router/second-model")

    before = get_breaker()
    assert before.is_down(failing_ref, time.monotonic())
    assert before.is_down(second_ref, time.monotonic())

    restored = _restart(db)

    assert restored == 2
    after = get_breaker()
    now = time.monotonic()
    assert after.is_down(failing_ref, now)
    assert after.is_down(second_ref, now)
    for ref in (failing_ref, second_ref):
        entry = next(e for e in after.snapshot(now) if e.ref == ref)
        assert entry.reason == "provider_402_propagated"
        assert entry.consecutive_failures == 1
        assert entry.ttl_remaining_s > 21_000.0, (
            f"remaining TTL for {ref} should still be close to the 6h quarantine "
            f"window, got {entry.ttl_remaining_s}"
        )


def test_consecutive_failure_escalation_survives_simulated_restart(tmp_path: Path) -> None:
    """A ref's escalated consecutive-failure count survives a restart
    verbatim — never incremented, never reset back to zero.
    """
    reset_breaker()
    db = tmp_path / "breaker.db"
    ref = ModelRef.parse("nvidia_nim/dead-model")
    breaker = get_breaker()

    for _attempt in range(3):
        breaker.trip(ref, now=time.monotonic(), ttl_s=120.0, reason="empty_completion")
        persist_state(
            breaker,
            ref,
            db_path=db,
            monotonic_now=time.monotonic(),
            wall_clock_now=datetime.now(UTC),
        )
    assert breaker._consecutive_failures[ref] == 3

    _restart(db)

    restored = get_breaker()
    entry = restored.snapshot(time.monotonic())[0]
    assert entry.consecutive_failures == 3
    assert entry.reason == "empty_completion"


def test_slow_strike_bench_survives_simulated_restart(tmp_path: Path) -> None:
    """A ``trip_slow`` bench (chronic slowness, not a hard failure) and its
    slow-history window survive a restart with the ``slow_completion``
    reason intact, and the failure counter stays untouched by it.
    """
    reset_breaker()
    db = tmp_path / "breaker.db"
    ref = ModelRef.parse("nvidia_nim/slow-model")
    breaker = get_breaker()

    breaker.record_success(ref, True, k=2, n=5)
    breaker.record_success(ref, True, k=2, n=5)
    breaker.trip_slow(ref, now=time.monotonic(), ttl_s=300.0, reason="slow_completion")
    persist_state(
        breaker, ref, db_path=db, monotonic_now=time.monotonic(), wall_clock_now=datetime.now(UTC)
    )

    _restart(db)

    restored = get_breaker()
    assert restored.is_down(ref, time.monotonic())
    entry = restored.snapshot(time.monotonic())[0]
    assert entry.reason == "slow_completion"
    assert entry.consecutive_failures == 0
    assert restored._slow_history[ref] == [True, True]


def test_recovered_ref_stays_up_after_simulated_restart(tmp_path: Path) -> None:
    """A ref that recovered before the restart never comes back down —
    the write-through call right after ``recover`` deletes its row.
    """
    reset_breaker()
    db = tmp_path / "breaker.db"
    ref = ModelRef.parse("nvidia_nim/flaky-model")
    breaker = get_breaker()

    breaker.trip(ref, now=time.monotonic(), ttl_s=120.0, reason="timeout")
    persist_state(
        breaker, ref, db_path=db, monotonic_now=time.monotonic(), wall_clock_now=datetime.now(UTC)
    )
    assert breaker.is_down(ref, time.monotonic())

    breaker.recover(ref)
    persist_state(
        breaker, ref, db_path=db, monotonic_now=time.monotonic(), wall_clock_now=datetime.now(UTC)
    )

    restored_count = _restart(db)

    assert restored_count == 0
    assert not get_breaker().is_down(ref, time.monotonic())


def test_non_head_chain_ref_trip_survives_simulated_restart(tmp_path: Path) -> None:
    """A trip on a non-head chain ref (position 2+, never seen by
    ``probe_seed.py``'s tier-head-only seed) restores identically to a
    head ref — rehydration has no notion of chain position.
    """
    reset_breaker()
    db = tmp_path / "breaker.db"
    head_ref = ModelRef.parse("nvidia_nim/tier-head")
    second_ref = ModelRef.parse("nvidia_nim/tier-second")
    breaker = get_breaker()

    breaker.trip(second_ref, now=time.monotonic(), ttl_s=120.0, reason="transport_error")
    persist_state(
        breaker,
        second_ref,
        db_path=db,
        monotonic_now=time.monotonic(),
        wall_clock_now=datetime.now(UTC),
    )
    assert breaker.is_down(second_ref, time.monotonic())
    assert not breaker.is_down(head_ref, time.monotonic())

    _restart(db)

    restored = get_breaker()
    assert restored.is_down(second_ref, time.monotonic())
    assert not restored.is_down(head_ref, time.monotonic())
    entry = restored.snapshot(time.monotonic())[0]
    assert entry.ref == second_ref
    assert entry.reason == "transport_error"
