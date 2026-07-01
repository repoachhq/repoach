"""Tests for the cell-health store (SP-CHAINPILOT-PROBE-SWEEP, Phase 2a-2).

Covers persistence of a sweep (one row per cell sharing recorded_at, empty is a
clean zero), idempotent schema creation, and the newest-first fetch with its
since / provider_id / model_id / limit filters plus naive-timestamp recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ferova.llm_proxy.providers.cell_probe import CellHealth
from ferova.llm_proxy.providers.cell_probe_store import (
    fetch_cell_probes,
    init_cell_health_schema,
    record_cell_probes,
)

_T0 = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 23, 13, 0, tzinfo=UTC)


def _health(provider: str, model: str, status: str = "ok", reasoning: int = 0) -> CellHealth:
    return CellHealth(provider, model, status, 1.2, 8, reasoning, "ok")


def test_record_writes_one_row_per_cell_sharing_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "probe.db"
    probes = [_health("nvidia_nim", "a"), _health("nvidia_nim", "b"), _health("kimi", "x")]

    written = record_cell_probes(db, probes, recorded_at=_T0)
    rows = fetch_cell_probes(db)

    assert written == 3
    assert len(rows) == 3
    assert {r.recorded_at for r in rows} == {_T0}
    assert {(r.provider_id, r.model_id) for r in rows} == {
        ("nvidia_nim", "a"),
        ("nvidia_nim", "b"),
        ("kimi", "x"),
    }


def test_empty_sweep_writes_zero_rows(tmp_path: Path) -> None:
    db = tmp_path / "probe.db"
    assert record_cell_probes(db, [], recorded_at=_T0) == 0
    assert fetch_cell_probes(db) == []


def test_schema_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "probe.db"
    init_cell_health_schema(db)
    init_cell_health_schema(db)
    record_cell_probes(db, [_health("groq", "g1")], recorded_at=_T0)
    assert len(fetch_cell_probes(db)) == 1


def test_fetch_is_newest_first_and_filters(tmp_path: Path) -> None:
    db = tmp_path / "probe.db"
    record_cell_probes(db, [_health("nvidia_nim", "a")], recorded_at=_T0)
    record_cell_probes(db, [_health("nvidia_nim", "b"), _health("kimi", "x")], recorded_at=_T1)

    newest = fetch_cell_probes(db)
    assert newest[0].recorded_at == _T1

    since = fetch_cell_probes(db, since=_T1)
    assert {r.model_id for r in since} == {"b", "x"}

    by_provider = fetch_cell_probes(db, provider_id="kimi")
    assert [r.model_id for r in by_provider] == ["x"]

    by_model = fetch_cell_probes(db, model_id="a")
    assert [(r.provider_id, r.model_id) for r in by_model] == [("nvidia_nim", "a")]

    assert fetch_cell_probes(db, model_id="unseen") == []
    assert len(fetch_cell_probes(db, limit=1)) == 1


def test_reasoning_chars_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "probe.db"
    record_cell_probes(db, [_health("nvidia_nim", "r", reasoning=42)], recorded_at=_T0)
    (row,) = fetch_cell_probes(db)
    assert row.reasoning_chars == 42
    assert row.recorded_at.tzinfo is not None
