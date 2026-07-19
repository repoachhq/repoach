"""Tests for the startup effort-map seeding (SP-CHAINPILOT-EFFORT-APPLY, 2a-3-iv-b)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

from repoach.llm_proxy.api.runtime import AppRuntime
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers import effort_map as effort_map_mod
from repoach.llm_proxy.providers.cell_probe import CellHealth
from repoach.llm_proxy.providers.effort_map import get_effort_map, reset_effort_map
from repoach.llm_proxy.providers.effort_probe_store import record_effort_probes
from repoach.llm_proxy.providers.effort_sweep import EffortProbe

_T0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_effort_map():
    reset_effort_map()
    yield
    reset_effort_map()


def _record_handled_groq(db: Path) -> None:
    health = CellHealth(
        provider_id="groq",
        model_id="g1",
        status="ok",
        latency_s=1.0,
        content_chars=2,
        reasoning_chars=5,
        detail="ok",
    )
    record_effort_probes(db, [EffortProbe(health=health, effort_used="low")], recorded_at=_T0)


def _runtime(settings: Settings) -> AppRuntime:
    return AppRuntime(app=FastAPI(), settings=settings)


def test_seed_populates_map_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    _record_handled_groq(db)
    monkeypatch.setenv("FEROVA_BREAKER_PROBE_SEED_DB", str(db))

    _runtime(Settings(_env_file=None))._seed_effort_map()

    assert get_effort_map().effort_for("groq", "g1") == "low"


def test_seed_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    _record_handled_groq(db)
    monkeypatch.setenv("FEROVA_BREAKER_PROBE_SEED_DB", str(db))
    monkeypatch.setenv("FEROVA_EFFORT_MAP_SEED_ENABLED", "false")

    _runtime(Settings(_env_file=None))._seed_effort_map()

    assert len(get_effort_map()) == 0


def test_seed_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: object) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(effort_map_mod, "seed_effort_map", boom)

    _runtime(Settings(_env_file=None))._seed_effort_map()

    assert len(get_effort_map()) == 0
