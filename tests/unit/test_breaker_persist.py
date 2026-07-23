"""Tests for breaker write-through persistence and rehydration (SP-PROXY-STATE-PERSIST).

Real :class:`BreakerState` instances and a real ``tmp_path`` SQLite file
throughout — no monkeypatching of repoach code. Covers the upsert-on-trip
write path, the wall-clock TTL arithmetic ``rehydrate_breaker_from_state``
performs on restore, and the edge cases (already-lapsed row pruned, clock
skew clamped, consecutive-failure count and slow-history window preserved
verbatim).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from repoach.llm_proxy.routing.breaker import BreakerState
from repoach.llm_proxy.routing.breaker_persist import (
    persist_state,
    rehydrate_breaker_from_state,
)
from repoach.llm_proxy.routing.refs import ModelRef

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_REF = ModelRef.parse("open_router/model-a")


def test_persist_state_writes_upsert_row_on_trip(tmp_path: Path) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=120.0, reason="timeout")

    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    restored = BreakerState()
    count = rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=2_000.0, wall_clock_now=_T0
    )

    assert count == 1
    assert restored.is_down(_REF, 2_000.0)
    entry = restored.snapshot(2_000.0)[0]
    assert entry.reason == "timeout"
    assert entry.ttl_remaining_s == 120.0


def test_persist_state_writes_upsert_row_on_trip_provider_for_every_sibling_ref(
    tmp_path: Path,
) -> None:
    db = tmp_path / "breaker.db"
    ref_a = ModelRef.parse("open_router/model-a")
    ref_b = ModelRef.parse("open_router/model-b")
    breaker = BreakerState()
    breaker.trip_provider(
        "open_router",
        [ref_a, ref_b],
        now=1_000.0,
        ttl_s=21_600.0,
        reason="provider_402_propagated",
    )

    for ref in (ref_a, ref_b):
        persist_state(breaker, ref, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    restored = BreakerState()
    count = rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=5_000.0, wall_clock_now=_T0
    )

    assert count == 2
    for ref in (ref_a, ref_b):
        assert restored.is_down(ref, 5_000.0)
        entry = next(e for e in restored.snapshot(5_000.0) if e.ref == ref)
        assert entry.reason == "provider_402_propagated"


def test_restore_computes_remaining_ttl_from_wall_clock_delta(tmp_path: Path) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=300.0, reason="timeout")
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    boot_wall_clock = _T0 + timedelta(seconds=100.0)
    restored = BreakerState()
    rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=50_000.0, wall_clock_now=boot_wall_clock
    )

    entry = restored.snapshot(50_000.0)[0]
    assert entry.ttl_remaining_s == 200.0


def test_restore_skips_and_prunes_row_whose_wall_clock_ttl_already_expired(
    tmp_path: Path,
) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=60.0, reason="timeout")
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    boot_wall_clock = _T0 + timedelta(seconds=600.0)
    restored = BreakerState()
    count = rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=50_000.0, wall_clock_now=boot_wall_clock
    )

    assert count == 0
    assert not restored.is_down(_REF, 50_000.0)

    reswept = BreakerState()
    second_count = rehydrate_breaker_from_state(
        reswept, db_path=db, monotonic_now=90_000.0, wall_clock_now=boot_wall_clock
    )
    assert second_count == 0, "the lapsed row must have been pruned, not merely skipped"


def test_restore_preserves_consecutive_failures_count_without_incrementing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=120.0, reason="timeout")
    breaker.trip(_REF, now=1_001.0, ttl_s=120.0, reason="timeout")
    breaker.trip(_REF, now=1_002.0, ttl_s=120.0, reason="timeout")
    assert breaker._consecutive_failures[_REF] == 3
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_002.0, wall_clock_now=_T0)

    restored = BreakerState()
    rehydrate_breaker_from_state(restored, db_path=db, monotonic_now=5_000.0, wall_clock_now=_T0)

    entry = restored.snapshot(5_000.0)[0]
    assert entry.consecutive_failures == 3

    rehydrate_breaker_from_state(restored, db_path=db, monotonic_now=5_001.0, wall_clock_now=_T0)
    assert restored._consecutive_failures[_REF] == 3, "a re-restore must not increment the count"


def test_restore_preserves_slow_history_window(tmp_path: Path) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.record_success(_REF, True, k=3, n=5)
    breaker.record_success(_REF, False, k=3, n=5)
    breaker.record_success(_REF, True, k=3, n=5)
    breaker.trip_slow(_REF, now=1_000.0, ttl_s=300.0, reason="slow_completion")
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    restored = BreakerState()
    rehydrate_breaker_from_state(restored, db_path=db, monotonic_now=5_000.0, wall_clock_now=_T0)

    assert restored._slow_history[_REF] == [True, False, True]


def test_restore_clamps_remaining_ttl_to_original_ceiling_on_clock_skew(tmp_path: Path) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=300.0, reason="timeout")
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    boot_wall_clock = _T0 - timedelta(seconds=10_000.0)
    restored = BreakerState()
    rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=50_000.0, wall_clock_now=boot_wall_clock
    )

    entry = restored.snapshot(50_000.0)[0]
    assert entry.ttl_remaining_s == 300.0, (
        "a backward clock step must clamp remaining TTL to the original ceiling, "
        f"got {entry.ttl_remaining_s}"
    )


def test_persist_state_deletes_row_when_ref_recovers(tmp_path: Path) -> None:
    db = tmp_path / "breaker.db"
    breaker = BreakerState()
    breaker.trip(_REF, now=1_000.0, ttl_s=120.0, reason="timeout")
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=_T0)

    breaker.recover(_REF)
    persist_state(breaker, _REF, db_path=db, monotonic_now=1_010.0, wall_clock_now=_T0)

    restored = BreakerState()
    count = rehydrate_breaker_from_state(
        restored, db_path=db, monotonic_now=5_000.0, wall_clock_now=_T0
    )
    assert count == 0
    assert not restored.is_down(_REF, 5_000.0)
