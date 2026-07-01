"""Unit tests for the fault attribution classifier (SP-CHAINPILOT-ATTRIBUTION)."""

from __future__ import annotations

from datetime import UTC, datetime

from ferova.health.model_health import STATUS_EMPTY, STATUS_ERROR, STATUS_OK, STATUS_SLOW
from ferova.llm_proxy.providers.attribution import (
    CellFault,
    FaultClass,
    FaultScope,
    attribute_faults,
    summarize_cells,
)
from ferova.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from ferova.llm_proxy.providers.cell_probe_store import CellProbeRow

_WHEN = datetime(2026, 1, 1, tzinfo=UTC)


def _probe(provider: str, model: str, status: str, *, content: int, reasoning: int) -> CellProbeRow:
    return CellProbeRow(
        recorded_at=_WHEN,
        provider_id=provider,
        model_id=model,
        status=status,
        latency_s=1.0,
        content_chars=content,
        reasoning_chars=reasoning,
        detail="",
    )


def _ok(provider: str, model: str, n: int = 3) -> list[CellProbeRow]:
    return [_probe(provider, model, STATUS_OK, content=10, reasoning=0) for _ in range(n)]


def _dead(provider: str, model: str, n: int = 3) -> list[CellProbeRow]:
    return [_probe(provider, model, STATUS_ERROR, content=0, reasoning=0) for _ in range(n)]


def _slow(provider: str, model: str, n: int = 3) -> list[CellProbeRow]:
    return [_probe(provider, model, STATUS_SLOW, content=10, reasoning=0) for _ in range(n)]


def _starved(provider: str, model: str, n: int = 3) -> list[CellProbeRow]:
    return [_probe(provider, model, STATUS_EMPTY, content=0, reasoning=20) for _ in range(n)]


def _verdict(faults: list[CellFault], provider: str, model: str) -> CellFault:
    return next(f for f in faults if f.provider_id == provider and f.model_id == model)


def test_empty_input_yields_empty() -> None:
    assert summarize_cells([]) == []
    assert attribute_faults([]) == []


def test_summarize_counts_each_bucket() -> None:
    probes = _ok("p", "m", 2) + _slow("p", "m", 1) + _starved("p", "m", 3) + _dead("p", "m", 4)
    summary = summarize_cells(probes)[0]
    assert (summary.n_ok, summary.n_slow, summary.n_starved, summary.n_dead) == (2, 1, 3, 4)
    assert summary.n_samples == 10


def test_below_min_samples_is_inconclusive() -> None:
    faults = attribute_faults(_dead("p", "m", 2), min_samples=3)
    assert faults[0].fault is FaultClass.INCONCLUSIVE
    assert faults[0].scope is FaultScope.NONE


def test_healthy_cell() -> None:
    faults = attribute_faults(_ok("p", "m", 3))
    assert faults[0].fault is FaultClass.HEALTHY
    assert faults[0].scope is FaultScope.NONE


def test_provider_fault_when_model_ok_elsewhere() -> None:
    faults = attribute_faults(_ok("a", "m") + _dead("b", "m"))
    assert _verdict(faults, "a", "m").fault is FaultClass.HEALTHY
    bad = _verdict(faults, "b", "m")
    assert bad.fault is FaultClass.PROVIDER_FAULT
    assert bad.scope is FaultScope.LOCAL


def test_model_fault_when_failing_everywhere() -> None:
    faults = attribute_faults(_dead("a", "m") + _dead("b", "m"))
    for provider in ("a", "b"):
        v = _verdict(faults, provider, "m")
        assert v.fault is FaultClass.MODEL_FAULT
        assert v.scope is FaultScope.SYSTEMIC


def test_our_fault_systemic_when_starved_everywhere() -> None:
    faults = attribute_faults(_starved("a", "m") + _starved("b", "m"))
    v = _verdict(faults, "a", "m")
    assert v.fault is FaultClass.OUR_FAULT
    assert v.scope is FaultScope.SYSTEMIC


def test_our_fault_local_when_starved_but_ok_elsewhere() -> None:
    faults = attribute_faults(_ok("a", "m") + _starved("b", "m"))
    assert _verdict(faults, "a", "m").fault is FaultClass.HEALTHY
    v = _verdict(faults, "b", "m")
    assert v.fault is FaultClass.OUR_FAULT
    assert v.scope is FaultScope.LOCAL


def test_slow_everywhere_is_healthy() -> None:
    faults = attribute_faults(_slow("a", "m") + _slow("b", "m"))
    for provider in ("a", "b"):
        assert _verdict(faults, provider, "m").fault is FaultClass.HEALTHY


def test_slow_with_minority_dead_stays_healthy() -> None:
    faults = attribute_faults(_ok("p", "m", 1) + _slow("p", "m", 2) + _dead("p", "m", 1))
    assert _verdict(faults, "p", "m").fault is FaultClass.HEALTHY


def test_dead_dominant_attributes_to_model_not_slow() -> None:
    faults = attribute_faults(_slow("a", "m", 2) + _dead("a", "m", 3) + _dead("b", "m", 3))
    v = _verdict(faults, "a", "m")
    assert v.fault is FaultClass.MODEL_FAULT
    assert "slow" not in v.reason


def test_one_dead_one_starved_provider_is_provider_fault_not_model() -> None:
    faults = attribute_faults(_dead("nvidia_nim", "m", 3) + _starved("open_router", "m", 3))
    nim = _verdict(faults, "nvidia_nim", "m")
    assert nim.fault is FaultClass.PROVIDER_FAULT
    assert "every provider" not in nim.reason


def test_under_sampled_second_provider_does_not_reach_model_fault_quorum() -> None:
    faults = attribute_faults(_dead("nvidia_nim", "m", 3) + _dead("open_router", "m", 1))
    nim = _verdict(faults, "nvidia_nim", "m")
    assert nim.fault is FaultClass.PROVIDER_FAULT


def test_single_provider_failing_is_provider_fault_not_model_fault() -> None:
    faults = attribute_faults(_dead("only", "m"))
    assert faults[0].fault is FaultClass.PROVIDER_FAULT
    assert faults[0].scope is FaultScope.LOCAL
    assert "not evicting the model" in faults[0].reason


_DEEPSEEK_EQUIV = EquivalenceTable(
    equivalences=(
        ModelEquivalence(canonical="deepseek-v4-pro", aliases=(), id_patterns=("deepseek-v4-pro",)),
    )
)


def test_canonical_grouping_enables_model_fault_across_provider_id_variants() -> None:
    probes = _dead("nvidia_nim", "deepseek-ai/deepseek-v4-pro") + _dead(
        "open_router", "deepseek/deepseek-v4-pro"
    )

    raw = _verdict(attribute_faults(probes), "nvidia_nim", "deepseek-ai/deepseek-v4-pro")
    assert raw.fault is FaultClass.PROVIDER_FAULT

    joined = attribute_faults(probes, equivalences=_DEEPSEEK_EQUIV)
    assert _verdict(joined, "nvidia_nim", "deepseek-ai/deepseek-v4-pro").fault is (
        FaultClass.MODEL_FAULT
    )


def test_canonical_grouping_keeps_model_healthy_on_another_provider_a_provider_fault() -> None:
    probes = (
        _dead("nvidia_nim", "deepseek-ai/deepseek-v4-pro")
        + _ok("deepseek", "deepseek/deepseek-v4-pro")
        + _ok("open_router", "deepseek/deepseek-v4-pro")
    )

    joined = attribute_faults(probes, equivalences=_DEEPSEEK_EQUIV)
    nim = _verdict(joined, "nvidia_nim", "deepseek-ai/deepseek-v4-pro")
    assert nim.fault is FaultClass.PROVIDER_FAULT
    assert "healthy on another provider" in nim.reason


def test_verdicts_ordered_by_provider_then_model() -> None:
    faults = attribute_faults(_ok("zeta", "m") + _ok("alpha", "m") + _ok("alpha", "a"))
    assert [(f.provider_id, f.model_id) for f in faults] == [
        ("alpha", "a"),
        ("alpha", "m"),
        ("zeta", "m"),
    ]
