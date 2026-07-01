"""Tests for the reasoned-at-effort store (SP-CHAINPILOT-EFFORT-SWEEP, 2a-3-ii).

Covers schema creation, effort_used / model_used round-trip, the shared
recorded_at, filtering, and the empty-sweep no-op — all on a temp SQLite file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ferova.llm_proxy.providers.cell_probe import CellHealth
from ferova.llm_proxy.providers.effort_probe_store import (
    fetch_effort_probes,
    record_effort_probes,
)
from ferova.llm_proxy.providers.effort_sweep import EffortProbe

_T0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


def _probe(
    provider_id: str, model_id: str, *, effort: str | None, status: str = "ok"
) -> EffortProbe:
    health = CellHealth(
        provider_id=provider_id,
        model_id=model_id,
        status=status,
        latency_s=1.5,
        content_chars=2,
        reasoning_chars=7,
        detail="ok",
    )
    return EffortProbe(health=health, effort_used=effort)


def test_record_and_fetch_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    written = record_effort_probes(
        db,
        [_probe("groq", "g1", effort="low"), _probe("nvidia_nim", "n1", effort=None)],
        recorded_at=_T0,
    )
    assert written == 2

    rows = fetch_effort_probes(db)
    by_model = {r.model_id: r for r in rows}
    assert by_model["g1"].effort_used == "low"
    assert by_model["g1"].model_used == "g1"
    assert by_model["g1"].reasoning_chars == 7
    assert by_model["n1"].effort_used is None
    assert by_model["n1"].model_used == "n1"
    assert all(r.recorded_at == _T0 for r in rows)


def test_fetch_filters_by_provider_and_model(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    record_effort_probes(
        db,
        [
            _probe("groq", "g1", effort="low"),
            _probe("deepseek", "d1", effort="high"),
            _probe("deepseek", "d2", effort="high"),
        ],
        recorded_at=_T0,
    )

    assert {r.model_id for r in fetch_effort_probes(db, provider_id="deepseek")} == {"d1", "d2"}
    only = fetch_effort_probes(db, model_id="g1")
    assert len(only) == 1
    assert only[0].effort_used == "low"
    assert len(fetch_effort_probes(db, limit=1)) == 1


def test_empty_sweep_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
    assert record_effort_probes(db, [], recorded_at=_T0) == 0
    assert fetch_effort_probes(db) == []
