"""Tests for per-cell effort resolution (SP-CHAINPILOT-EFFORT-RESOLVE, 2a-3-iii)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ferova.llm_proxy.providers.cell_probe import CellHealth
from ferova.llm_proxy.providers.effort_probe_store import (
    EffortProbeRow,
    record_effort_probes,
)
from ferova.llm_proxy.providers.effort_resolve import (
    load_cell_efforts,
    resolve_cell_efforts,
)
from ferova.llm_proxy.providers.effort_sweep import EffortProbe

_T0 = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


def _row(
    provider_id: str,
    model_id: str,
    *,
    effort: str | None,
    reasoning_chars: int,
    content_chars: int,
) -> EffortProbeRow:
    return EffortProbeRow(
        recorded_at=_T0,
        provider_id=provider_id,
        model_id=model_id,
        status="ok",
        latency_s=1.0,
        content_chars=content_chars,
        reasoning_chars=reasoning_chars,
        detail="ok",
        effort_used=effort,
        model_used=model_id,
    )


def test_handled_rows_resolve_the_effort() -> None:
    rows = [
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=2),
        _row("groq", "g1", effort="low", reasoning_chars=4, content_chars=3),
    ]
    res = resolve_cell_efforts(rows)
    r = res[("groq", "g1")]
    assert r.effort == "low"
    assert r.handled == 2
    assert r.samples == 2


def test_starved_rows_resolve_none() -> None:
    rows = [
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=0),
        _row("groq", "g1", effort="low", reasoning_chars=6, content_chars=0),
    ]
    r = resolve_cell_efforts(rows)[("groq", "g1")]
    assert r.effort is None
    assert r.handled == 0


def test_never_reasoned_resolves_none() -> None:
    rows = [_row("groq", "g1", effort="low", reasoning_chars=0, content_chars=8)]
    assert resolve_cell_efforts(rows)[("groq", "g1")].effort is None


def test_min_samples_gate() -> None:
    one = [_row("deepseek", "d1", effort="high", reasoning_chars=5, content_chars=2)]
    assert resolve_cell_efforts(one, min_samples=2)[("deepseek", "d1")].effort is None

    two = [
        _row("deepseek", "d1", effort="high", reasoning_chars=5, content_chars=2),
        _row("deepseek", "d1", effort="high", reasoning_chars=4, content_chars=2),
    ]
    assert resolve_cell_efforts(two, min_samples=2)[("deepseek", "d1")].effort == "high"


def test_handled_fraction_boundary() -> None:
    half = [
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=2),
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=0),
    ]
    assert resolve_cell_efforts(half, handled_fraction=0.5)[("groq", "g1")].effort == "low"

    third = [
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=2),
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=0),
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=0),
    ]
    assert resolve_cell_efforts(third, handled_fraction=0.5)[("groq", "g1")].effort is None


def test_none_effort_rows_appear_with_none() -> None:
    rows = [_row("kimi", "k1", effort=None, reasoning_chars=5, content_chars=2)]
    res = resolve_cell_efforts(rows)
    assert ("kimi", "k1") in res
    assert res[("kimi", "k1")].effort is None


def test_empty_rows_empty_map() -> None:
    assert resolve_cell_efforts([]) == {}


def test_every_cell_appears_once() -> None:
    rows = [
        _row("groq", "g1", effort="low", reasoning_chars=5, content_chars=2),
        _row("deepseek", "d1", effort="high", reasoning_chars=0, content_chars=4),
        _row("nvidia_nim", "n1", effort=None, reasoning_chars=0, content_chars=4),
    ]
    res = resolve_cell_efforts(rows)
    assert set(res) == {("groq", "g1"), ("deepseek", "d1"), ("nvidia_nim", "n1")}


def test_load_matches_resolve(tmp_path: Path) -> None:
    db = tmp_path / "effort.db"
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

    loaded = load_cell_efforts(db)
    assert loaded[("groq", "g1")].effort == "low"
    assert loaded[("groq", "g1")].handled == 1
