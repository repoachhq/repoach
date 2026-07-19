"""Tests for the resolved-effort singleton (SP-CHAINPILOT-EFFORT-MAP, 2a-3-iv-a)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from repoach.llm_proxy.providers.cell_probe import CellHealth
from repoach.llm_proxy.providers.effort_map import (
    EffortMap,
    get_effort_map,
    reset_effort_map,
    seed_effort_map,
)
from repoach.llm_proxy.providers.effort_probe_store import record_effort_probes
from repoach.llm_proxy.providers.effort_sweep import EffortProbe

_T0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_singleton():
    reset_effort_map()
    yield
    reset_effort_map()


def test_effort_map_keeps_non_none_only() -> None:
    m = EffortMap({("groq", "g1"): "low", ("kimi", "k1"): None})
    assert m.effort_for("groq", "g1") == "low"
    assert m.effort_for("kimi", "k1") is None
    assert m.effort_for("x", "y") is None
    assert len(m) == 1


def test_singleton_default_empty_replace_and_reset() -> None:
    assert len(get_effort_map()) == 0
    get_effort_map().replace({("groq", "g1"): "low"})
    assert get_effort_map().effort_for("groq", "g1") == "low"
    reset_effort_map()
    assert len(get_effort_map()) == 0
    assert get_effort_map().effort_for("groq", "g1") is None


def _record(
    db: Path, model_id: str, *, reasoning_chars: int, content_chars: int, effort: str | None
):
    health = CellHealth(
        provider_id="groq",
        model_id=model_id,
        status="ok",
        latency_s=1.0,
        content_chars=content_chars,
        reasoning_chars=reasoning_chars,
        detail="ok",
    )
    record_effort_probes(db, [EffortProbe(health=health, effort_used=effort)], recorded_at=_T0)


def test_seed_installs_handled_cells(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    _record(db, "g1", reasoning_chars=5, content_chars=2, effort="low")

    wired = seed_effort_map(db_path=db)
    assert wired == 1
    assert get_effort_map().effort_for("groq", "g1") == "low"


def test_seed_skips_unresolved_cells(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    _record(db, "g1", reasoning_chars=0, content_chars=8, effort="low")

    wired = seed_effort_map(db_path=db)
    assert wired == 0
    assert get_effort_map().effort_for("groq", "g1") is None


def test_seed_empty_db_returns_zero(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    seed_effort_map(db_path=db)
    assert seed_effort_map(db_path=db) == 0
    assert len(get_effort_map()) == 0
