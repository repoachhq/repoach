"""Unit tests for the decision engine (SP-CHAINPILOT-DECISION)."""

from __future__ import annotations

from ferova.llm_proxy.providers.attribution import CellFault, FaultClass, FaultScope
from ferova.review.decision import MutationKind, PlannedMutation, plan_mutations
from ferova.review.perf_aggregate import GuardedMetric, ModelPerformance


def _fault(
    model: str,
    fault: FaultClass,
    *,
    provider: str = "prov",
    scope: FaultScope = FaultScope.NONE,
    n: int = 5,
) -> CellFault:
    return CellFault(provider, model, fault, scope, "reason", n)


def _gm(metric: str, value: float | None, n: int, *, confident: bool = True) -> GuardedMetric:
    return GuardedMetric(metric=metric, value=value, n=n, confident=confident)


def _perf(
    model: str, metrics: list[GuardedMetric], *, prior_rank: int | None = None
) -> ModelPerformance:
    return ModelPerformance(
        model=model, prior_rank=prior_rank, prior_score=None, metrics=tuple(metrics)
    )


def _of(muts: list[PlannedMutation], kind: MutationKind) -> list[PlannedMutation]:
    return [m for m in muts if m.kind is kind]


def test_empty_inputs_yield_empty() -> None:
    assert plan_mutations([], []) == []


def test_model_fault_evicts_once_across_cells() -> None:
    faults = [
        _fault("m", FaultClass.MODEL_FAULT, provider="a"),
        _fault("m", FaultClass.MODEL_FAULT, provider="b"),
    ]
    evicts = _of(plan_mutations(faults, []), MutationKind.EVICT_MODEL)
    assert len(evicts) == 1
    assert evicts[0].model == "m"


def test_provider_fault_drops_provider() -> None:
    muts = plan_mutations([_fault("m", FaultClass.PROVIDER_FAULT, provider="b")], [])
    drops = _of(muts, MutationKind.DROP_PROVIDER)
    assert len(drops) == 1
    assert (drops[0].model, drops[0].provider) == ("m", "b")


def test_our_fault_advises_only() -> None:
    muts = plan_mutations([_fault("m", FaultClass.OUR_FAULT, scope=FaultScope.SYSTEMIC)], [])
    assert [m.kind for m in muts] == [MutationKind.ADVISE]
    assert "systemic" in muts[0].reason


def test_healthy_and_inconclusive_yield_nothing() -> None:
    faults = [
        _fault("a", FaultClass.HEALTHY),
        _fault("b", FaultClass.INCONCLUSIVE),
    ]
    assert plan_mutations(faults, []) == []


def test_quality_promote_demote_when_separated() -> None:
    scoreboard = [
        _perf("strong", [_gm("coder_ci_green", 0.9, 20)]),
        _perf("weak", [_gm("coder_ci_green", 0.4, 20)]),
    ]
    muts = plan_mutations([], scoreboard)
    promotes = _of(muts, MutationKind.PROMOTE)
    demotes = _of(muts, MutationKind.DEMOTE)
    assert [m.model for m in promotes] == ["strong"]
    assert [m.model for m in demotes] == ["weak"]
    assert promotes[0].metric == "coder_ci_green"


def test_quality_no_move_within_noise() -> None:
    scoreboard = [
        _perf("a", [_gm("coder_ci_green", 0.62, 5)]),
        _perf("b", [_gm("coder_ci_green", 0.58, 5)]),
    ]
    muts = plan_mutations([], scoreboard)
    assert _of(muts, MutationKind.PROMOTE) == []
    assert _of(muts, MutationKind.DEMOTE) == []


def test_stuck_rate_lower_is_better() -> None:
    scoreboard = [
        _perf("clean", [_gm("coder_stuck", 0.1, 20)]),
        _perf("stuck", [_gm("coder_stuck", 0.6, 20)]),
    ]
    muts = plan_mutations([], scoreboard)
    assert [m.model for m in _of(muts, MutationKind.PROMOTE)] == ["clean"]
    assert [m.model for m in _of(muts, MutationKind.DEMOTE)] == ["stuck"]


def test_non_confident_metric_is_ignored() -> None:
    scoreboard = [
        _perf("strong", [_gm("coder_ci_green", 0.9, 20, confident=False)]),
        _perf("weak", [_gm("coder_ci_green", 0.4, 20, confident=False)]),
    ]
    assert plan_mutations([], scoreboard) == []


def test_rounds_to_green_never_drives_a_move() -> None:
    scoreboard = [
        _perf("fast", [_gm("coder_rounds_to_green", 1.0, 20)]),
        _perf("slow", [_gm("coder_rounds_to_green", 3.0, 20)]),
    ]
    assert plan_mutations([], scoreboard) == []


def test_single_candidate_metric_no_move() -> None:
    scoreboard = [_perf("lonely", [_gm("reviewer_precision", 0.9, 30)])]
    assert plan_mutations([], scoreboard) == []


def test_prior_rank_alone_drives_no_mutation() -> None:
    scoreboard = [
        _perf("newcomer", [], prior_rank=2),
        _perf("incumbent", [], prior_rank=1),
    ]
    assert plan_mutations([], scoreboard) == []


def test_output_is_deterministically_ordered() -> None:
    faults = [
        _fault("zmodel", FaultClass.MODEL_FAULT),
        _fault("amodel", FaultClass.PROVIDER_FAULT, provider="p"),
    ]
    muts = plan_mutations(faults, [])
    keys = [(m.kind.value, m.model) for m in muts]
    assert keys == sorted(keys)
