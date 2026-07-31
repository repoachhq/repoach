"""Tests for the startup breaker-state rehydration (SP-PROXY-STATE-PERSIST).

Mirrors ``tests/unit/test_runtime_effort_seed.py``'s pattern: a real
:class:`AppRuntime` + real ``tmp_path`` SQLite file, never a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

from repoach.llm_proxy.api.runtime import AppRuntime
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.breaker import BreakerState
from repoach.llm_proxy.routing.breaker_persist import persist_state
from repoach.llm_proxy.routing.refs import ModelRef

_REF = ModelRef.parse("open_router/model-a")


@pytest.fixture(autouse=True)
def _isolate_breaker():
    reset_breaker()
    yield
    reset_breaker()


def _runtime(settings: Settings) -> AppRuntime:
    return AppRuntime(app=FastAPI(), settings=settings)


def _seed_db_with_live_trip(db: Path) -> None:
    source = BreakerState()
    source.trip(_REF, now=1_000.0, ttl_s=3_600.0, reason="timeout")
    persist_state(source, _REF, db_path=db, monotonic_now=1_000.0, wall_clock_now=datetime.now(UTC))


def test_seed_populates_breaker_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "breaker_state.db"
    _seed_db_with_live_trip(db)
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))

    _runtime(Settings(_env_file=None))._seed_breaker_from_persisted_state()

    import time

    assert get_breaker().is_down(_REF, time.monotonic())


def test_seed_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "breaker_state.db"
    _seed_db_with_live_trip(db)
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))
    monkeypatch.setenv("REPOACH_BREAKER_STATE_PERSIST_ENABLED", "false")

    _runtime(Settings(_env_file=None))._seed_breaker_from_persisted_state()

    import time

    assert not get_breaker().is_down(_REF, time.monotonic())


def test_seed_swallows_db_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("x", encoding="utf-8")
    db = blocking_file / "sub" / "breaker_state.db"
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))

    _runtime(Settings(_env_file=None))._seed_breaker_from_persisted_state()

    import time

    assert not get_breaker().is_down(_REF, time.monotonic())
